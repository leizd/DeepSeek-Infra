//! Temporary record processing. This spool is never a durable writer authority.
//!
//! Resolve complete JSON/EOF and the last top-level media key before calling
//! the original normalizer. In particular, infrastructure failures must never
//! turn a valid existing library into an empty-store CREATE plan.
use crate::{
    app_error::AppError,
    media_stream_io::{DigestReader, DigestWriter, WriteSeek},
};
use retained::{Document, MediaRecord};
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

impl VisitError<Status> {
    fn status(self) -> Status {
        match self {
            Self::Storage(error) | Self::Visitor(error) => error,
        }
    }
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

struct StoredRecord {
    offset: i64,
    length: i64,
    digest: Vec<u8>,
    durable_unicode: bool,
}

impl StoredRecord {
    fn new(offset: i64, length: i64, content_digest: &[u8], durable_unicode: bool) -> Self {
        Self {
            offset,
            length,
            digest: Self::bind(content_digest, durable_unicode),
            durable_unicode,
        }
    }

    fn from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Self> {
        Ok(Self {
            offset: row.get(0)?,
            length: row.get(1)?,
            digest: row.get(2)?,
            durable_unicode: row.get(3)?,
        })
    }

    fn bind(content_digest: &[u8], durable_unicode: bool) -> Vec<u8> {
        // A damaged index must not make an unencodable record publishable.
        Sha256::new()
            .chain_update(content_digest)
            .chain_update([u8::from(durable_unicode)])
            .finalize()
            .to_vec()
    }

    fn matches(&self, content_digest: &[u8]) -> bool {
        self.digest == Self::bind(content_digest, self.durable_unicode)
    }
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
                digest BLOB NOT NULL CHECK(length(digest)=32),
                durable_unicode INTEGER NOT NULL CHECK(durable_unicode IN (0,1)));
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
            Document::normalize_media,
        )
    }

    fn parse(
        reader: impl Read,
        raw: RawRecords,
        mut normalized: Self,
        mut normalize: impl FnMut(&Document) -> Result<MediaRecord, AppError>,
    ) -> Result<Self, Status> {
        let validate_phase = crate::media_phase_timing::Phase::start("records.validate_raw");
        let (raw, _) = match document::read(reader, raw, false) {
            Ok(value) => value,
            Err(document::Error::Syntax) => return Ok(normalized),
            Err(document::Error::InvalidUtf8 | document::Error::SourceIo) => return Err(invalid()),
            Err(document::Error::Storage(error)) => return Err(error),
        };
        drop(validate_phase);
        let _normalize_phase = crate::media_phase_timing::Phase::start("records.normalize_spool");
        raw.visit_retained(|value| {
            if let Ok(record) = normalize(&value) {
                normalized.append_retained(&record)?;
            }
            Ok(())
        })
        .map_err(VisitError::status)?;
        Ok(normalized)
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
        let _normalize_phase = crate::media_phase_timing::Phase::start("records.normalize_spool");
        let mut recovered = Self::empty()?;
        recovered.publication = publication;
        raw.visit_bytes(|bytes| {
            let record = MediaRecord::from_spool(bytes).map_err(|_| invalid())?;
            if !record.has_durable_unicode() {
                return Err(invalid());
            }
            // Recovery must reconstruct the same intermediate image even
            // after an upgrade. Keep the generated record's value bytes and
            // order, including its original number spelling.
            let stored = recovered.store(|output| {
                output.write_all(bytes)?;
                Ok(true)
            })?;
            recovered.append_index(&record.id(), stored)
        })
        .map_err(VisitError::status)?;
        Ok(recovered)
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

    fn store(
        &mut self,
        write: impl FnOnce(&mut dyn Write) -> io::Result<bool>,
    ) -> Result<StoredRecord, Status> {
        let offset = self.writer.seek(SeekFrom::End(0)).map_err(|_| invalid())?;
        let mut output = BufWriter::with_capacity(BUFFER, DigestWriter::new(&mut self.writer));
        let durable_unicode = write(&mut output).map_err(|_| invalid())?;
        output.flush().map_err(|_| invalid())?;
        let (length, digest) = output.into_inner().map_err(|_| invalid())?.finish();
        Ok(StoredRecord::new(
            i64::try_from(offset).map_err(|_| invalid())?,
            i64::try_from(length).map_err(|_| invalid())?,
            &digest,
            durable_unicode,
        ))
    }

    pub fn append(&mut self, value: &Value) -> Result<(), Status> {
        let id = value
            .get("mediaId")
            .and_then(Value::as_str)
            .ok_or_else(invalid)?;
        let stored = self.store_value(value)?;
        self.append_index(id, stored)
    }

    fn store_value(&mut self, value: &Value) -> Result<StoredRecord, Status> {
        self.store(|output| {
            serde_json::to_writer(output, value).map_err(io::Error::other)?;
            Ok(true)
        })
    }

    fn append_retained(&mut self, record: &MediaRecord) -> Result<(), Status> {
        let stored = self.store(|output| record.write_spool(output))?;
        self.append_index(&record.id(), stored)
    }

    fn append_index(&mut self, id: &str, stored: StoredRecord) -> Result<(), Status> {
        self.connection
            .execute(
                "INSERT INTO records VALUES(?1,?2,?3,?4,?5,?6)",
                params![
                    self.next,
                    id,
                    stored.offset,
                    stored.length,
                    stored.digest,
                    stored.durable_unicode
                ],
            )
            .map_err(|_| invalid())?;
        self.next = self.next.checked_add(1).ok_or_else(invalid)?;
        Ok(())
    }

    fn read_record(&self, record: &StoredRecord) -> Result<Value, Status> {
        let bytes = self.read_record_bytes(record)?;
        document::decode_stored_value(&bytes).map_err(|_| invalid())
    }

    fn read_record_bytes(&self, record: &StoredRecord) -> Result<Vec<u8>, Status> {
        let offset = u64::try_from(record.offset).map_err(|_| invalid())?;
        let length = usize::try_from(record.length).map_err(|_| invalid())?;
        let mut file = fs::File::open(self.data.path()).map_err(|_| invalid())?;
        file.seek(SeekFrom::Start(offset)).map_err(|_| invalid())?;
        // Materialize one record, never the aggregate library. Parsing its
        // contiguous bytes avoids per-byte Read overhead in the second pass.
        let mut bytes = Vec::new();
        bytes.try_reserve_exact(length).map_err(|_| invalid())?;
        bytes.resize(length, 0);
        file.read_exact(&mut bytes).map_err(|_| invalid())?;
        if !record.matches(&Sha256::digest(&bytes)) {
            return Err(invalid());
        }
        Ok(bytes)
    }

    /// Visit the complete stored sequence without accumulating its values.
    pub fn visit_source<E>(
        &self,
        mut visit: impl FnMut(Value) -> Result<(), E>,
    ) -> Result<(), VisitError<E>> {
        self.visit_bytes(|bytes| {
            let value =
                document::decode_stored_value(bytes).map_err(|_| VisitError::Storage(invalid()))?;
            visit(value).map_err(VisitError::Visitor)
        })
    }

    fn visit_bytes<E>(
        &self,
        mut visit: impl FnMut(&[u8]) -> Result<(), VisitError<E>>,
    ) -> Result<(), VisitError<E>> {
        let storage = |_: rusqlite::Error| VisitError::Storage(invalid());
        let mut statement = self
            .connection
            .prepare("SELECT record_offset,record_bytes,digest,durable_unicode FROM records ORDER BY record_index")
            .map_err(storage)?;
        let mut rows = statement.query([]).map_err(storage)?;
        while let Some(row) = rows.next().map_err(storage)? {
            let record = StoredRecord::from_row(row).map_err(storage)?;
            let bytes = self
                .read_record_bytes(&record)
                .map_err(VisitError::Storage)?;
            visit(&bytes)?;
        }
        Ok(())
    }

    /// Resolve an index from a caller-owned ordering index and verify its bytes.
    pub fn get(&self, index: i64) -> Result<Option<Value>, Status> {
        self.record_at(index)?
            .map(|record| self.read_record(&record))
            .transpose()
    }

    fn record_at(&self, index: i64) -> Result<Option<StoredRecord>, Status> {
        self.connection
            .query_row(
                "SELECT record_offset,record_bytes,digest,durable_unicode FROM records WHERE record_index=?1",
                [index],
                StoredRecord::from_row,
            )
            .optional()
            .map_err(|_| invalid())
    }

    pub fn first(&self, id: &str) -> Result<Option<(i64, Value)>, Status> {
        let Some(index) = self.first_index(id)? else {
            return Ok(None);
        };
        Ok(Some((index, self.get(index)?.ok_or_else(invalid)?)))
    }

    fn retained_at(&self, index: i64) -> Result<Option<MediaRecord>, Status> {
        self.record_at(index)?
            .map(|record| {
                let bytes = self.read_record_bytes(&record)?;
                MediaRecord::from_spool(&bytes).map_err(|_| invalid())
            })
            .transpose()
    }

    pub fn first_retained(&self, id: &str) -> Result<Option<(i64, MediaRecord)>, Status> {
        let Some(index) = self.first_index(id)? else {
            return Ok(None);
        };
        Ok(Some((index, self.retained_at(index)?.ok_or_else(invalid)?)))
    }

    pub fn first_index(&self, id: &str) -> Result<Option<i64>, Status> {
        self.connection
            .query_row(
                "SELECT record_index FROM records WHERE media_id=?1 ORDER BY record_index LIMIT 1",
                [id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|_| invalid())
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
        let stored = self.store_value(value)?;
        self.replace_index(index, id, stored)
    }

    pub fn replace_retained(&mut self, index: i64, value: &MediaRecord) -> Result<(), Status> {
        let stored = self.store(|out| value.write_spool(out))?;
        self.replace_index(index, &value.id(), stored)
    }

    fn replace_index(&self, index: i64, id: &str, stored: StoredRecord) -> Result<(), Status> {
        if self.connection.execute(
            "UPDATE records SET media_id=?1,record_offset=?2,record_bytes=?3,digest=?4,durable_unicode=?5 WHERE record_index=?6",
            params![id, stored.offset, stored.length, stored.digest, stored.durable_unicode, index],
        ).map_err(|_| invalid())? != 1 {
            return Err(invalid());
        }
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

    pub fn has_unencodable_retained_text(&self) -> Result<bool, Status> {
        self.connection
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM (SELECT durable_unicode FROM records
              ORDER BY record_index DESC LIMIT 1000) WHERE durable_unicode=0)",
                [],
                |row| row.get(0),
            )
            .map_err(|_| invalid())
    }

    pub fn write_retained_array(&self, output: &mut (impl Write + ?Sized)) -> Result<(), Status> {
        let _phase = crate::media_phase_timing::Phase::start("records.emit_array");
        let skip = (self.len()? - 1000).max(0);
        let mut statement = self.connection.prepare(
            "SELECT record_offset,record_bytes,digest,durable_unicode FROM records ORDER BY record_index LIMIT 1000 OFFSET ?1"
        ).map_err(|_| invalid())?;
        let mut rows = statement.query([skip]).map_err(|_| invalid())?;
        output.write_all(b"[").map_err(|_| invalid())?;
        let mut first = true;
        while let Some(row) = rows.next().map_err(|_| invalid())? {
            let record = StoredRecord::from_row(row).map_err(|_| invalid())?;
            // A row dropped by DELETE or by the retention cap cannot make
            // the original ensure_ascii=False store fail to encode.
            if !record.durable_unicode {
                return Err(invalid());
            }
            if !first {
                output.write_all(b",").map_err(|_| invalid())?;
            }
            first = false;
            let mut file = fs::File::open(self.data.path()).map_err(|_| invalid())?;
            file.seek(SeekFrom::Start(
                u64::try_from(record.offset).map_err(|_| invalid())?,
            ))
            .map_err(|_| invalid())?;
            let length = u64::try_from(record.length).map_err(|_| invalid())?;
            let mut reader = DigestReader::new(file.take(length));
            io::copy(&mut reader, output).map_err(|_| invalid())?;
            let (bytes, actual) = reader.finish();
            if bytes != length || !record.matches(&actual) {
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
