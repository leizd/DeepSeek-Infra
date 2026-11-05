//! Temporary record processing. This spool is never a durable writer authority.
//!
//! Resolve complete JSON/EOF and the last top-level media key before calling
//! the original normalizer. In particular, infrastructure failures must never
//! turn a valid existing library into an empty-store CREATE plan.
use crate::{
    app_error::AppError,
    media_schema::normalize_media_record,
    media_stream_io::{DigestReader, DigestWriter, WriteSeek},
};
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{self, BufWriter, Read, Seek, SeekFrom, Write},
};
use tonic::Status;

fn invalid() -> Status {
    Status::failed_precondition("MEDIA_MUTATION_INVALID")
}

#[path = "media_json_document.rs"]
mod document;
#[path = "media_raw_records.rs"]
mod raw;
pub use raw::RawRecords;

#[path = "media_retained_value.rs"]
pub mod retained;
#[path = "media_retained_records.rs"]
mod retained_records;
#[path = "media_retained_schema.rs"]
mod retained_schema;
pub use retained_records::RetainedRecords;

const BUFFER: usize = 64 * 1024;

/// Storage failures and caller policy errors must not collapse into one result.
#[derive(Debug)]
pub enum VisitError<E> {
    Storage(Status),
    Visitor(E),
}

/// The gateway preserves different source-read and UTF-8 behavior from workers.
#[derive(Debug)]
pub enum SourceReadError {
    SourceIo,
    InvalidUtf8,
    Storage(Status),
}

pub struct Records {
    // Windows must close handles before their owning temporary files unlink.
    connection: Connection,
    writer: Box<dyn WriteSeek>,
    data: tempfile::NamedTempFile,
    _index: tempfile::NamedTempFile,
    next: i64,
    pub publication: Option<Value>,
}
impl Records {
    pub fn empty() -> Result<Self, Status> {
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

    pub fn from_reader(reader: impl Read) -> Result<Self, Status> {
        Self::parse(
            reader,
            RawRecords::empty()?,
            Self::empty()?,
            normalize_media_record,
        )
    }

    fn parse(
        reader: impl Read,
        raw: RawRecords,
        normalized: Self,
        normalize: impl FnMut(&Value) -> Result<Value, AppError>,
    ) -> Result<Self, Status> {
        let validate_phase = crate::media_phase_timing::Phase::start("records.validate_raw");
        let (raw, publication) = match document::read(reader, raw, false) {
            Ok(value) => value,
            Err(document::Error::Syntax) => return Ok(normalized),
            Err(document::Error::InvalidUtf8 | document::Error::SourceIo) => return Err(invalid()),
            Err(document::Error::Storage(error)) => return Err(error),
        };
        drop(validate_phase);
        Self::normalize_raw(&raw, normalized, publication, normalize)
    }

    /// Preserve complete validation while leaving normalization to the caller.
    pub fn from_raw_reader(reader: impl Read) -> Result<RawRecords, SourceReadError> {
        let empty = RawRecords::empty().map_err(SourceReadError::Storage)?;
        let raw = RawRecords::empty().map_err(SourceReadError::Storage)?;
        let _phase = crate::media_phase_timing::Phase::start("records.validate_raw");
        match document::read_value(reader, raw, false) {
            Ok((raw, _)) => Ok(raw),
            Err(document::Error::Syntax) => Ok(empty),
            Err(document::Error::InvalidUtf8) => Err(SourceReadError::InvalidUtf8),
            Err(document::Error::SourceIo) => Err(SourceReadError::SourceIo),
            Err(document::Error::Storage(error)) => Err(SourceReadError::Storage(error)),
        }
    }

    pub fn from_image(reader: impl Read) -> Result<Self, Status> {
        let validate_phase = crate::media_phase_timing::Phase::start("records.validate_raw");
        let raw = RawRecords::empty()?;
        let (raw, publication) =
            document::read(reader, raw, true).map_err(|error| match error {
                document::Error::Storage(error) => error,
                _ => invalid(),
            })?;
        drop(validate_phase);
        Self::normalize_raw(&raw, Self::empty()?, publication, |value| Ok(value.clone()))
    }

    fn normalize_raw(
        raw: &RawRecords,
        mut normalized: Self,
        publication: Option<Value>,
        mut normalize: impl FnMut(&Value) -> Result<Value, AppError>,
    ) -> Result<Self, Status> {
        let _normalize_phase = crate::media_phase_timing::Phase::start("records.normalize_spool");
        normalized.publication = publication;
        let mut statement = raw
            .inner
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

    pub fn append(&mut self, value: &Value) -> Result<(), Status> {
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
        let bytes = self.read_record_bytes(offset, length, digest)?;
        document::decode_value_slice(&bytes).map_err(|_| invalid())
    }

    fn read_record_bytes(
        &self,
        offset: i64,
        length: i64,
        digest: Vec<u8>,
    ) -> Result<Vec<u8>, Status> {
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
        if Sha256::digest(&bytes).to_vec() != digest {
            return Err(invalid());
        }
        Ok(bytes)
    }

    /// Visit the complete stored sequence without accumulating its values.
    pub fn visit_source<E>(
        &self,
        mut visit: impl FnMut(Value) -> Result<(), E>,
    ) -> Result<(), VisitError<E>> {
        let storage = |_: rusqlite::Error| VisitError::Storage(invalid());
        let mut statement = self
            .connection
            .prepare("SELECT record_offset,record_bytes,digest FROM records ORDER BY record_index")
            .map_err(storage)?;
        let mut rows = statement.query([]).map_err(storage)?;
        while let Some(row) = rows.next().map_err(storage)? {
            let value = self
                .read_record(
                    row.get(0).map_err(storage)?,
                    row.get(1).map_err(storage)?,
                    row.get(2).map_err(storage)?,
                )
                .map_err(VisitError::Storage)?;
            visit(value).map_err(VisitError::Visitor)?;
        }
        Ok(())
    }

    /// Resolve an index from a caller-owned ordering index and verify its bytes.
    pub fn get(&self, index: i64) -> Result<Option<Value>, Status> {
        let row: Option<(i64, i64, Vec<u8>)> = self
            .connection
            .query_row(
                "SELECT record_offset,record_bytes,digest FROM records WHERE record_index=?1",
                [index],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()
            .map_err(|_| invalid())?;
        row.map(|(offset, length, digest)| self.read_record(offset, length, digest))
            .transpose()
    }

    pub fn first(&self, id: &str) -> Result<Option<(i64, Value)>, Status> {
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
    pub fn contains(&self, id: &str) -> Result<bool, Status> {
        self.connection
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM records WHERE media_id=?1)",
                [id],
                |r| r.get(0),
            )
            .map_err(|_| invalid())
    }
    pub fn replace(&mut self, index: i64, value: &Value) -> Result<(), Status> {
        let id = value
            .get("mediaId")
            .and_then(Value::as_str)
            .ok_or_else(invalid)?;
        let (offset, length, digest) = self.store(value)?;
        if self.connection.execute("UPDATE records SET media_id=?1,record_offset=?2,record_bytes=?3,digest=?4 WHERE record_index=?5",
            params![id,offset,length,digest,index]).map_err(|_| invalid())? != 1 { return Err(invalid()); }
        Ok(())
    }
    pub fn remove_all(&mut self, id: &str) -> Result<usize, Status> {
        self.connection
            .execute("DELETE FROM records WHERE media_id=?1", [id])
            .map_err(|_| invalid())
    }
    pub fn is_empty(&self) -> Result<bool, Status> {
        self.len().map(|length| length == 0)
    }

    pub fn len(&self) -> Result<i64, Status> {
        self.connection
            .query_row("SELECT count(*) FROM records", [], |r| r.get(0))
            .map_err(|_| invalid())
    }

    pub fn write_retained_array(&self, output: &mut (impl Write + ?Sized)) -> Result<(), Status> {
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
#[cfg(test)]
#[path = "media_records_tests.rs"]
mod tests;
