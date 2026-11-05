//! Opaque read-only raw rows. Only normalized Records expose output/mutation APIs.
use super::{Records, VisitError, document, invalid};
use crate::media_stream_io::{DigestWriter, WriteSeek};
use rusqlite::params;
use serde_json::Value;
use std::io::{self, BufWriter, Seek, SeekFrom, Write};
use tonic::Status;

pub struct RawRecords {
    pub(super) inner: Records,
}

/// The writer is temporarily owned by the lexical cursor during one record.
pub(super) struct Capture {
    output: BufWriter<DigestWriter<Box<dyn WriteSeek>>>,
    offset: i64,
}

impl Write for Capture {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.output.write(bytes)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.output.flush()
    }
}

struct CapturedRecord {
    writer: Box<dyn WriteSeek>,
    offset: i64,
    length: i64,
    hash: Vec<u8>,
}

impl Capture {
    fn finish(mut self) -> Result<CapturedRecord, Status> {
        // Both the buffer and underlying sink must flush before any index entry
        // is committed or a later top-level media key can truncate the spool.
        self.output.flush().map_err(|_| invalid())?;
        let digest = self.output.into_inner().map_err(|_| invalid())?;
        let (writer, length, hash) = digest.finish_into_inner().map_err(|_| invalid())?;
        Ok(CapturedRecord {
            writer,
            offset: self.offset,
            length: i64::try_from(length).map_err(|_| invalid())?,
            hash,
        })
    }
}

struct Unavailable;
impl Write for Unavailable {
    fn write(&mut self, _: &[u8]) -> io::Result<usize> {
        Err(io::Error::other("raw capture owns the writer"))
    }
    fn flush(&mut self) -> io::Result<()> {
        Err(io::Error::other("raw capture owns the writer"))
    }
}
impl Seek for Unavailable {
    fn seek(&mut self, _: SeekFrom) -> io::Result<u64> {
        Err(io::Error::other("raw capture owns the writer"))
    }
}

impl RawRecords {
    pub fn empty() -> Result<Self, Status> {
        Ok(Self {
            inner: Records::empty()?,
        })
    }

    pub(super) fn begin_capture(&mut self) -> Result<Capture, Status> {
        self.inner.writer.flush().map_err(|_| invalid())?;
        let offset = self
            .inner
            .writer
            .seek(SeekFrom::End(0))
            .map_err(|_| invalid())?;
        let writer = std::mem::replace(&mut self.inner.writer, Box::new(Unavailable));
        Ok(Capture {
            output: BufWriter::with_capacity(super::BUFFER, DigestWriter::new(writer)),
            offset: i64::try_from(offset).map_err(|_| invalid())?,
        })
    }

    pub(super) fn finish_capture(&mut self, capture: Capture) -> Result<(), Status> {
        let CapturedRecord {
            writer,
            offset,
            length,
            hash,
        } = capture.finish()?;
        self.inner.writer = writer;
        self.inner
            .connection
            .execute(
                "INSERT INTO records VALUES(?1,'',?2,?3,?4)",
                params![self.inner.next, offset, length, hash],
            )
            .map_err(|_| invalid())?;
        self.inner.next = self.inner.next.checked_add(1).ok_or_else(invalid)?;
        Ok(())
    }

    pub(super) fn clear(&mut self) -> Result<(), Status> {
        self.inner.writer.flush().map_err(|_| invalid())?;
        self.inner.clear()
    }

    pub(super) fn read_record(
        &self,
        offset: i64,
        length: i64,
        hash: Vec<u8>,
    ) -> Result<Value, Status> {
        let bytes = self.inner.read_record_bytes(offset, length, hash)?;
        document::decode_raw_record(&bytes).map_err(|_| invalid())
    }

    /// There is deliberately no raw emission, append, replacement or retention API.
    pub fn visit_source<E>(
        &self,
        mut visit: impl FnMut(Value) -> Result<(), E>,
    ) -> Result<(), VisitError<E>> {
        let storage = |_: rusqlite::Error| VisitError::Storage(invalid());
        let mut statement = self
            .inner
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

    /// Read original accepted values without crossing the strict JSON response
    /// boundary. The opaque raw spool still has no emission or mutation API.
    pub fn visit_retained<E>(
        &self,
        mut visit: impl FnMut(super::retained::Document) -> Result<(), E>,
    ) -> Result<(), VisitError<E>> {
        let storage = |_: rusqlite::Error| VisitError::Storage(invalid());
        let mut statement = self
            .inner
            .connection
            .prepare("SELECT record_offset,record_bytes,digest FROM records ORDER BY record_index")
            .map_err(storage)?;
        let mut rows = statement.query([]).map_err(storage)?;
        while let Some(row) = rows.next().map_err(storage)? {
            let bytes = self
                .inner
                .read_record_bytes(
                    row.get(0).map_err(storage)?,
                    row.get(1).map_err(storage)?,
                    row.get(2).map_err(storage)?,
                )
                .map_err(VisitError::Storage)?;
            let value = super::retained::Document::decode(&bytes)
                .map_err(|_| VisitError::Storage(invalid()))?;
            visit(value).map_err(VisitError::Visitor)?;
        }
        Ok(())
    }
}
