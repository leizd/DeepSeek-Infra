//! Digest-checked, private normalized records. Never a durable media writer.
use super::{Records, retained::MediaRecord};
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
        self.inner.retained_at(ordinal)
    }
}
