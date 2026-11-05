//! Digest-checked, private normalized records. Never a durable media writer.
use super::{Records, invalid, retained::MediaRecord};
use crate::media_stream_io::DigestWriter;
use rusqlite::{OptionalExtension, params};
use std::io::{BufWriter, Seek, SeekFrom, Write};
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
        let offset = self
            .inner
            .writer
            .seek(SeekFrom::End(0))
            .map_err(|_| invalid())?;
        let mut output =
            BufWriter::with_capacity(super::BUFFER, DigestWriter::new(&mut self.inner.writer));
        record.write_spool(&mut output).map_err(|_| invalid())?;
        output.flush().map_err(|_| invalid())?;
        let (length, hash) = output.into_inner().map_err(|_| invalid())?.finish();
        self.inner
            .connection
            .execute(
                "INSERT INTO records VALUES(?1,?2,?3,?4,?5)",
                params![
                    self.inner.next,
                    record.id(),
                    i64::try_from(offset).map_err(|_| invalid())?,
                    i64::try_from(length).map_err(|_| invalid())?,
                    hash
                ],
            )
            .map_err(|_| invalid())?;
        self.inner.next = self.inner.next.checked_add(1).ok_or_else(invalid)?;
        Ok(())
    }

    pub fn get(&self, ordinal: i64) -> Result<Option<MediaRecord>, Status> {
        let row = self
            .inner
            .connection
            .query_row(
                "SELECT record_offset,record_bytes,digest FROM records WHERE record_index=?1",
                [ordinal],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, Vec<u8>>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(|_| invalid())?;
        row.map(|(offset, length, hash)| {
            let bytes = self.inner.read_record_bytes(offset, length, hash)?;
            MediaRecord::from_spool(&bytes).map_err(|_| invalid())
        })
        .transpose()
    }
}
