//! Digest-checked, private normalized records. Never a durable media writer.
use super::{Records, invalid, retained::MediaRecord};
use tonic::Status;

pub struct RetainedRecords {
    inner: Records,
}

impl RetainedRecords {
    pub fn empty() -> Result<Self, Status> {
        Ok(Self {
            inner: Records::empty()?,
        })
    }

    pub fn append(&mut self, record: &MediaRecord) -> Result<(), Status> {
        self.inner.append_retained(record)
    }

    pub fn get(&self, ordinal: i64) -> Result<Option<MediaRecord>, Status> {
        self.inner
            .record_at(ordinal)?
            .map(|record| {
                let bytes = self.inner.read_record_bytes(&record)?;
                MediaRecord::from_spool(&bytes).map_err(|_| invalid())
            })
            .transpose()
    }
}
