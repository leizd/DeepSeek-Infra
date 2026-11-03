//! Temporary record processing. This spool is never a durable writer authority.
//!
//! Resolve complete JSON/EOF and the last top-level media key before calling
//! the original normalizer. In particular, infrastructure failures must never
//! turn a valid existing library into an empty-store CREATE plan.
use crate::{
    media_stream_io::{DigestReader, DigestWriter, WriteSeek},
    media_values::invalid,
};
use deepseek_policy::{app_error::AppError, media_schema::normalize_media_record};
use rusqlite::{Connection, OptionalExtension, params};
use serde::{
    Deserialize,
    de::{self, DeserializeSeed, IgnoredAny, MapAccess, SeqAccess, Visitor},
};
use serde_json::Value;
use std::{
    fmt, fs,
    io::{self, BufReader, BufWriter, Read, Seek, SeekFrom, Write},
};
use tonic::Status;

const BUFFER: usize = 64 * 1024;
pub(super) struct Records {
    // Windows must close handles before their owning temporary files unlink.
    connection: Connection,
    writer: Box<dyn WriteSeek>,
    data: tempfile::NamedTempFile,
    _index: tempfile::NamedTempFile,
    next: i64,
    pub(super) publication: Option<Value>,
}
impl Records {
    fn empty() -> Result<Self, Status> {
        let index = tempfile::NamedTempFile::new().map_err(|_| invalid())?;
        let data = tempfile::NamedTempFile::new().map_err(|_| invalid())?;
        let writer = Box::new(data.as_file().try_clone().map_err(|_| invalid())?);
        let connection = Connection::open(index.path()).map_err(|_| invalid())?;
        connection.execute_batch(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-2048; PRAGMA temp_store=FILE;
             CREATE TABLE records (record_index INTEGER PRIMARY KEY, media_id TEXT NOT NULL,
                record_offset INTEGER NOT NULL CHECK(record_offset>=0), record_bytes INTEGER NOT NULL CHECK(record_bytes>0),
                digest BLOB NOT NULL CHECK(length(digest)=32));
             CREATE INDEX records_by_id ON records(media_id,record_index);
             BEGIN;"
        ).map_err(|_| invalid())?;
        Ok(Self {
            connection,
            writer,
            data,
            _index: index,
            next: 0,
            publication: None,
        })
    }

    pub(super) fn from_reader(reader: impl Read) -> Result<Self, Status> {
        Self::parse(
            reader,
            Self::empty()?,
            Self::empty()?,
            normalize_media_record,
        )
    }

    fn parse(
        reader: impl Read,
        raw: Self,
        normalized: Self,
        normalize: impl FnMut(&Value) -> Result<Value, AppError>,
    ) -> Result<Self, Status> {
        Self::parse_mode(reader, raw, normalized, normalize, false, false)
    }
    pub(super) fn from_image(reader: impl Read) -> Result<Self, Status> {
        Self::parse_mode(
            reader,
            Self::empty()?,
            Self::empty()?,
            |value| Ok(value.clone()),
            true,
            true,
        )
    }
    fn parse_mode(
        reader: impl Read,
        mut raw: Self,
        mut normalized: Self,
        mut normalize: impl FnMut(&Value) -> Result<Value, AppError>,
        strict: bool,
        capture_publication: bool,
    ) -> Result<Self, Status> {
        let validate_phase = crate::media_phase_timing::Phase::start("records.validate_raw");
        let mut failure = None;
        let mut publication = None;
        let mut input = BufReader::with_capacity(BUFFER, reader);
        let mut decoder = serde_json::Deserializer::from_reader(&mut input);
        let parsed = RootSeed {
            records: &mut raw,
            failure: &mut failure,
            publication: &mut publication,
            capture_publication,
        }
        .deserialize(&mut decoder);
        let parsed = parsed.and_then(|_| decoder.end());
        if let Some(error) = failure {
            return Err(error);
        }
        if let Err(error) = parsed {
            // The old reader treats a malformed *complete document* as empty.
            // Source I/O and private-spool failures retain their error instead.
            if strict || error.is_io() {
                return Err(invalid());
            }
            // As with the old full-file read, an early JSON error cannot hide
            // later storage I/O or the image reader's final digest failure.
            drop(decoder);
            io::copy(&mut input, &mut io::sink()).map_err(|_| invalid())?;
            return Ok(normalized);
        }
        drop(validate_phase);
        let _normalize_phase = crate::media_phase_timing::Phase::start("records.normalize_spool");
        normalized.publication = publication;
        let mut statement = raw
            .connection
            .prepare("SELECT record_offset,record_bytes,digest FROM records ORDER BY record_index")
            .map_err(|_| invalid())?;
        let mut rows = statement.query([]).map_err(|_| invalid())?;
        while let Some(row) = rows.next().map_err(|_| invalid())? {
            let value = raw.read_record(
                row.get(0).map_err(|_| invalid())?,
                row.get(1).map_err(|_| invalid())?,
                row.get(2).map_err(|_| invalid())?,
            )?;
            if let Ok(value) = normalize(&value) {
                normalized.append(&value)?;
            }
        }
        Ok(normalized)
    }

    fn clear(&mut self) -> Result<(), Status> {
        self.connection
            .execute("DELETE FROM records", [])
            .map_err(|_| invalid())?;
        self.data.as_file().set_len(0).map_err(|_| invalid())?;
        self.writer
            .seek(SeekFrom::Start(0))
            .map_err(|_| invalid())?;
        self.next = 0;
        Ok(())
    }

    fn store(&mut self, value: &Value) -> Result<(i64, i64, Vec<u8>), Status> {
        let offset = self.writer.seek(SeekFrom::End(0)).map_err(|_| invalid())?;
        let mut output = BufWriter::with_capacity(BUFFER, DigestWriter::new(&mut self.writer));
        serde_json::to_writer(&mut output, value).map_err(|_| invalid())?;
        output.flush().map_err(|_| invalid())?;
        let (length, digest) = output.into_inner().map_err(|_| invalid())?.finish();
        Ok((
            i64::try_from(offset).map_err(|_| invalid())?,
            i64::try_from(length).map_err(|_| invalid())?,
            digest,
        ))
    }

    fn append_raw(&mut self, value: &Value) -> Result<(), Status> {
        self.append_with_id(value, "")
    }
    pub(super) fn append(&mut self, value: &Value) -> Result<(), Status> {
        let id = value
            .get("mediaId")
            .and_then(Value::as_str)
            .ok_or_else(invalid)?;
        self.append_with_id(value, id)
    }
    fn append_with_id(&mut self, value: &Value, id: &str) -> Result<(), Status> {
        let (offset, length, digest) = self.store(value)?;
        self.connection
            .execute(
                "INSERT INTO records VALUES(?1,?2,?3,?4,?5)",
                params![self.next, id, offset, length, digest],
            )
            .map_err(|_| invalid())?;
        self.next = self.next.checked_add(1).ok_or_else(invalid)?;
        Ok(())
    }

    fn read_record(&self, offset: i64, length: i64, digest: Vec<u8>) -> Result<Value, Status> {
        let offset = u64::try_from(offset).map_err(|_| invalid())?;
        let length = u64::try_from(length).map_err(|_| invalid())?;
        let mut file = fs::File::open(self.data.path()).map_err(|_| invalid())?;
        file.seek(SeekFrom::Start(offset)).map_err(|_| invalid())?;
        // Materialize one record, never the aggregate library. Parsing its
        // contiguous bytes avoids per-byte Read overhead in the second pass.
        let mut bytes = Vec::new();
        let length = usize::try_from(length).map_err(|_| invalid())?;
        bytes.try_reserve_exact(length).map_err(|_| invalid())?;
        bytes.resize(length, 0);
        file.read_exact(&mut bytes).map_err(|_| invalid())?;
        if crate::media_library::sha(&bytes) != digest {
            return Err(invalid());
        }
        serde_json::from_slice(&bytes).map_err(|_| invalid())
    }

    pub(super) fn first(&self, id: &str) -> Result<Option<(i64, Value)>, Status> {
        let row = self.connection.query_row(
            "SELECT record_index,record_offset,record_bytes,digest FROM records WHERE media_id=?1 ORDER BY record_index LIMIT 1",
            [id], |r| Ok((r.get::<_,i64>(0)?,r.get::<_,i64>(1)?,r.get::<_,i64>(2)?,r.get::<_,Vec<u8>>(3)?))
        ).optional().map_err(|_| invalid())?;
        row.map(|(index, offset, length, digest)| {
            self.read_record(offset, length, digest)
                .map(|value| (index, value))
        })
        .transpose()
    }
    pub(super) fn contains(&self, id: &str) -> Result<bool, Status> {
        self.connection
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM records WHERE media_id=?1)",
                [id],
                |r| r.get(0),
            )
            .map_err(|_| invalid())
    }
    pub(super) fn replace(&mut self, index: i64, value: &Value) -> Result<(), Status> {
        let id = value
            .get("mediaId")
            .and_then(Value::as_str)
            .ok_or_else(invalid)?;
        let (offset, length, digest) = self.store(value)?;
        if self.connection.execute("UPDATE records SET media_id=?1,record_offset=?2,record_bytes=?3,digest=?4 WHERE record_index=?5",
            params![id,offset,length,digest,index]).map_err(|_| invalid())? != 1 { return Err(invalid()); }
        Ok(())
    }
    pub(super) fn remove_all(&mut self, id: &str) -> Result<usize, Status> {
        self.connection
            .execute("DELETE FROM records WHERE media_id=?1", [id])
            .map_err(|_| invalid())
    }
    pub(super) fn len(&self) -> Result<i64, Status> {
        self.connection
            .query_row("SELECT count(*) FROM records", [], |r| r.get(0))
            .map_err(|_| invalid())
    }

    pub(super) fn write_retained_array(
        &self,
        output: &mut (impl Write + ?Sized),
    ) -> Result<(), Status> {
        let _phase = crate::media_phase_timing::Phase::start("records.emit_array");
        let skip = (self.len()? - 1000).max(0);
        let mut statement = self.connection.prepare(
            "SELECT record_offset,record_bytes,digest FROM records ORDER BY record_index LIMIT 1000 OFFSET ?1"
        ).map_err(|_| invalid())?;
        let mut rows = statement.query([skip]).map_err(|_| invalid())?;
        output.write_all(b"[").map_err(|_| invalid())?;
        let mut first = true;
        while let Some(row) = rows.next().map_err(|_| invalid())? {
            if !first {
                output.write_all(b",").map_err(|_| invalid())?;
            }
            first = false;
            let offset: i64 = row.get(0).map_err(|_| invalid())?;
            let length: i64 = row.get(1).map_err(|_| invalid())?;
            let expected: Vec<u8> = row.get(2).map_err(|_| invalid())?;
            let mut file = fs::File::open(self.data.path()).map_err(|_| invalid())?;
            file.seek(SeekFrom::Start(
                u64::try_from(offset).map_err(|_| invalid())?,
            ))
            .map_err(|_| invalid())?;
            let mut reader =
                DigestReader::new(file.take(u64::try_from(length).map_err(|_| invalid())?));
            io::copy(&mut reader, output).map_err(|_| invalid())?;
            let (bytes, actual) = reader.finish();
            if bytes != length as u64 || actual != expected {
                return Err(invalid());
            }
        }
        output.write_all(b"]").map_err(|_| invalid())?;
        Ok(())
    }
}

// These visitors intentionally do not normalize. A discarded media array and
// any malformed/trailing input must consume zero original normalization clocks.
macro_rules! empty_scalars {
    () => {
        fn visit_unit<E: de::Error>(self) -> Result<(), E> {
            Ok(())
        }
        fn visit_bool<E: de::Error>(self, _: bool) -> Result<(), E> {
            Ok(())
        }
        fn visit_i64<E: de::Error>(self, _: i64) -> Result<(), E> {
            Ok(())
        }
        fn visit_u64<E: de::Error>(self, _: u64) -> Result<(), E> {
            Ok(())
        }
        fn visit_f64<E: de::Error>(self, _: f64) -> Result<(), E> {
            Ok(())
        }
        fn visit_str<E: de::Error>(self, _: &str) -> Result<(), E> {
            Ok(())
        }
    };
}
fn storage_error<E: de::Error>(failure: &mut Option<Status>, error: Status) -> E {
    *failure = Some(error);
    E::custom("media temporary spool unavailable")
}
struct RootSeed<'a> {
    records: &'a mut Records,
    failure: &'a mut Option<Status>,
    publication: &'a mut Option<Value>,
    capture_publication: bool,
}
struct MediaSeed<'a> {
    records: &'a mut Records,
    failure: &'a mut Option<Status>,
}
struct RecordSeed<'a> {
    records: &'a mut Records,
    failure: &'a mut Option<Status>,
}
struct ValidatedSkip;
impl<'de> DeserializeSeed<'de> for ValidatedSkip {
    type Value = ();
    fn deserialize<D: de::Deserializer<'de>>(self, decoder: D) -> Result<(), D::Error> {
        decoder.deserialize_any(self)
    }
}
impl<'de> Visitor<'de> for ValidatedSkip {
    type Value = ();
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a valid JSON value")
    }
    empty_scalars!();
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<(), A::Error> {
        while seq.next_element_seed(ValidatedSkip)?.is_some() {}
        Ok(())
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<(), A::Error> {
        while map.next_key::<IgnoredAny>()?.is_some() {
            map.next_value_seed(ValidatedSkip)?;
        }
        Ok(())
    }
}
impl<'de> DeserializeSeed<'de> for RootSeed<'_> {
    type Value = ();
    fn deserialize<D: de::Deserializer<'de>>(self, decoder: D) -> Result<(), D::Error> {
        decoder.deserialize_any(self)
    }
}
impl<'de> Visitor<'de> for RootSeed<'_> {
    type Value = ();
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a media document")
    }
    empty_scalars!();
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<(), A::Error> {
        while seq.next_element_seed(ValidatedSkip)?.is_some() {}
        Ok(())
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<(), A::Error> {
        while let Some(key) = map.next_key::<String>()? {
            if key == "media" {
                self.records
                    .clear()
                    .map_err(|e| storage_error(self.failure, e))?;
                map.next_value_seed(MediaSeed {
                    records: &mut *self.records,
                    failure: &mut *self.failure,
                })?;
            } else if key == "nativePublication" && self.capture_publication {
                *self.publication = Some(map.next_value::<Value>()?);
            } else {
                map.next_value_seed(ValidatedSkip)?;
            }
        }
        Ok(())
    }
}
impl<'de> DeserializeSeed<'de> for MediaSeed<'_> {
    type Value = ();
    fn deserialize<D: de::Deserializer<'de>>(self, decoder: D) -> Result<(), D::Error> {
        decoder.deserialize_any(self)
    }
}
impl<'de> Visitor<'de> for MediaSeed<'_> {
    type Value = ();
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a media array")
    }
    empty_scalars!();
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<(), A::Error> {
        while map.next_key::<IgnoredAny>()?.is_some() {
            map.next_value_seed(ValidatedSkip)?;
        }
        Ok(())
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<(), A::Error> {
        while seq
            .next_element_seed(RecordSeed {
                records: &mut *self.records,
                failure: &mut *self.failure,
            })?
            .is_some()
        {}
        Ok(())
    }
}
impl<'de> DeserializeSeed<'de> for RecordSeed<'_> {
    type Value = ();
    fn deserialize<D: de::Deserializer<'de>>(self, decoder: D) -> Result<(), D::Error> {
        decoder.deserialize_any(self)
    }
}
impl<'de> Visitor<'de> for RecordSeed<'_> {
    type Value = ();
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a media record")
    }
    empty_scalars!();
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<(), A::Error> {
        while seq.next_element_seed(ValidatedSkip)?.is_some() {}
        Ok(())
    }
    fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<(), A::Error> {
        let value = Value::deserialize(de::value::MapAccessDeserializer::new(map))?;
        if value.is_object() {
            self.records
                .append_raw(&value)
                .map_err(|e| storage_error(self.failure, e))?;
        }
        Ok(())
    }
}

#[cfg(test)]
#[path = "media_records_tests.rs"]
mod tests;
