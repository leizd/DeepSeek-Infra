//! Opaque read-only raw rows. Only normalized Records expose output/mutation APIs.
use super::{Records, StoredRecord, VisitError, document, invalid};
use crate::media_stream_io::{DigestWriter, WriteSeek};
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
            .append_index("", StoredRecord::new(offset, length, &hash, false))
    }

    pub(super) fn clear(&mut self) -> Result<(), Status> {
        self.inner.writer.flush().map_err(|_| invalid())?;
        self.inner.clear()
    }

    /// There is deliberately no raw emission, append, replacement or retention API.
    pub fn visit_source<E>(
        &self,
        mut visit: impl FnMut(Value) -> Result<(), E>,
    ) -> Result<(), VisitError<E>> {
        self.inner.visit_bytes(|bytes| {
            let value =
                document::decode_raw_record(bytes).map_err(|_| VisitError::Storage(invalid()))?;
            visit(value).map_err(VisitError::Visitor)
        })
    }

    /// Read original accepted values without crossing the strict JSON response
    /// boundary. The opaque raw spool still has no emission or mutation API.
    pub fn visit_retained<E>(
        &self,
        mut visit: impl FnMut(super::retained::Document) -> Result<(), E>,
    ) -> Result<(), VisitError<E>> {
        self.inner.visit_bytes(|bytes| {
            let value = super::retained::Document::decode(bytes)
                .map_err(|_| VisitError::Storage(invalid()))?;
            visit(value).map_err(VisitError::Visitor)
        })
    }

    pub(super) fn visit_bytes<E>(
        &self,
        mut visit: impl FnMut(&[u8]) -> Result<(), E>,
    ) -> Result<(), VisitError<E>> {
        self.inner
            .visit_bytes(|bytes| visit(bytes).map_err(VisitError::Visitor))
    }
}
