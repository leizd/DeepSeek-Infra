//! Complete private read snapshots; strict JSON encoding follows normalization.
use super::server_error;
#[cfg(test)]
use super::{media_sort_key, normalize_media_record};
use axum::{
    body::{Body, Bytes},
    http::{StatusCode, header},
    response::{IntoResponse, Response},
};
use deepseek_policy::{
    app_error::AppError,
    media_records::{
        RawRecords, Records, RetainedRecords, SourceReadError, VisitError, retained::MediaRecord,
    },
    media_stream_io::DigestWriter,
};
use rusqlite::{Connection, params};
use serde_json::Value;
#[cfg(test)]
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    io::{self, BufWriter, Seek, SeekFrom, Write},
    path::Path,
};
use tokio::io::AsyncReadExt;

const BUFFER: usize = 64 * 1024;

fn raw(path: &Path) -> Result<RawRecords, AppError> {
    let source = match File::open(path) {
        Ok(source) => source,
        Err(_) => return RawRecords::empty().map_err(|_| server_error()),
    };
    match Records::from_raw_reader(source) {
        Ok(records) => Ok(records),
        Err(SourceReadError::SourceIo) => RawRecords::empty().map_err(|_| server_error()),
        Err(SourceReadError::InvalidUtf8 | SourceReadError::Storage(_)) => Err(server_error()),
    }
}

fn visited(result: Result<(), VisitError<AppError>>) -> Result<(), AppError> {
    result.map_err(|error| match error {
        VisitError::Storage(_) => server_error(),
        VisitError::Visitor(error) => error,
    })
}

fn stored(
    path: &Path,
    mut visit: impl FnMut(MediaRecord) -> Result<(), AppError>,
) -> Result<(), AppError> {
    let records = raw(path)?;
    visited(records.visit_retained(|value| {
        let Ok(stored) = value.normalize_media() else {
            return Ok(());
        };
        visit(stored)
    }))
}

pub(super) fn find(path: &Path, id: &str) -> Result<Option<MediaRecord>, AppError> {
    let mut found = None;
    stored(path, |value| {
        if found.is_none() && value.field_equals("mediaId", id) {
            found = Some(value);
        }
        Ok(())
    })?;
    found.map(|value| value.normalize_again()).transpose()
}

#[cfg(test)]
pub(super) fn contains(path: &Path, id: &str) -> Result<bool, AppError> {
    let mut found = false;
    stored(path, |value| {
        found |= value.field_equals("mediaId", id);
        Ok(())
    })?;
    Ok(found)
}

pub(super) struct PublicIndex {
    records: RetainedRecords,
    count: usize,
}

impl PublicIndex {
    pub(super) fn load(path: &Path) -> Result<Self, AppError> {
        let mut records = RetainedRecords::empty().map_err(|_| server_error())?;
        let mut count = 0usize;
        stored(path, |value| {
            records.append(&value).map_err(|_| server_error())?;
            count = count.checked_add(1).ok_or_else(server_error)?;
            Ok(())
        })?;
        Ok(Self { records, count })
    }

    fn selected_public(
        &self,
        mut include: impl FnMut(&MediaRecord) -> bool,
    ) -> Result<OrderedPublic, AppError> {
        let mut public = OrderedPublic::empty()?;
        // The original public-normalizes selected records in source order.
        for ordinal in 0..self.count {
            let ordinal = i64::try_from(ordinal).map_err(|_| server_error())?;
            let stored = self
                .records
                .get(ordinal)
                .map_err(|_| server_error())?
                .ok_or_else(server_error)?;
            if include(&stored) {
                public.append(&stored.normalize_again()?)?;
            }
        }
        Ok(public)
    }

    #[cfg(test)]
    fn visit_sorted(
        &self,
        visit: impl FnMut(MediaRecord) -> Result<(), AppError>,
    ) -> Result<(), AppError> {
        self.selected_public(|_| true)?.visit_sorted(-1, visit)
    }

    pub(super) fn window(&self, limit: usize) -> Result<(Vec<MediaRecord>, usize), AppError> {
        let public = self.selected_public(|_| true)?;
        let mut values = Vec::new();
        public.visit_sorted(i64::try_from(limit).unwrap_or(i64::MAX), |value| {
            values.push(value);
            Ok(())
        })?;
        // Encoding is later, after every other workspace-home loader too.
        Ok((values, self.count))
    }

    pub(super) fn collection(
        &self,
        include: impl FnMut(&MediaRecord) -> bool,
    ) -> Result<CompletedJson, AppError> {
        let public = self.selected_public(include)?;
        CompletedJson::build(|output| {
            output
                .write_all(b"{\"ok\":true,\"media\":[")
                .map_err(|_| server_error())?;
            let mut first = true;
            public.visit_sorted(-1, |value| {
                if !first {
                    output.write_all(b",").map_err(|_| server_error())?;
                }
                first = false;
                value.write_response(output).map_err(|_| server_error())
            })?;
            output.write_all(b"]}").map_err(|_| server_error())
        })
    }
}

struct OrderedPublic {
    order: Connection,
    records: RetainedRecords,
    _order_file: tempfile::NamedTempFile,
    ordinal: i64,
}

impl OrderedPublic {
    fn empty() -> Result<Self, AppError> {
        let order_file = tempfile::NamedTempFile::new().map_err(|_| server_error())?;
        let order = Connection::open(order_file.path()).map_err(|_| server_error())?;
        order.execute_batch(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-2048; PRAGMA temp_store=FILE;
             CREATE TABLE ordering (ordinal INTEGER PRIMARY KEY, sort_key BLOB NOT NULL);
             CREATE INDEX media_order ON ordering(sort_key DESC,ordinal ASC); BEGIN;"
        ).map_err(|_| server_error())?;
        Ok(Self {
            order,
            records: RetainedRecords::empty().map_err(|_| server_error())?,
            _order_file: order_file,
            ordinal: 0,
        })
    }

    fn append(&mut self, value: &MediaRecord) -> Result<(), AppError> {
        self.records.append(value).map_err(|_| server_error())?;
        self.order
            .execute(
                "INSERT INTO ordering VALUES(?1,?2)",
                params![self.ordinal, value.sort_key()],
            )
            .map_err(|_| server_error())?;
        self.ordinal = self.ordinal.checked_add(1).ok_or_else(server_error)?;
        Ok(())
    }

    fn visit_sorted(
        &self,
        limit: i64,
        mut visit: impl FnMut(MediaRecord) -> Result<(), AppError>,
    ) -> Result<(), AppError> {
        let mut statement = self
            .order
            .prepare("SELECT ordinal FROM ordering ORDER BY sort_key DESC,ordinal ASC LIMIT ?1")
            .map_err(|_| server_error())?;
        let mut rows = statement.query([limit]).map_err(|_| server_error())?;
        while let Some(row) = rows.next().map_err(|_| server_error())? {
            let ordinal = row.get::<_, i64>(0).map_err(|_| server_error())?;
            let value = self
                .records
                .get(ordinal)
                .map_err(|_| server_error())?
                .ok_or_else(server_error)?;
            visit(value)?;
        }
        Ok(())
    }
}

pub(crate) struct CompletedJson {
    file: tempfile::NamedTempFile,
    length: u64,
    hash: Vec<u8>,
}
impl CompletedJson {
    pub(crate) fn build(
        work: impl FnOnce(&mut dyn Write) -> Result<(), AppError>,
    ) -> Result<Self, AppError> {
        let mut file = tempfile::NamedTempFile::new().map_err(|_| server_error())?;
        let (length, hash) = {
            let mut output =
                BufWriter::with_capacity(BUFFER, DigestWriter::new(file.as_file_mut()));
            work(&mut output)?;
            output.flush().map_err(|_| server_error())?;
            output.into_inner().map_err(|_| server_error())?.finish()
        };
        file.as_file_mut()
            .seek(SeekFrom::Start(0))
            .map_err(|_| server_error())?;
        Ok(Self { file, length, hash })
    }

    pub(super) fn item(record: &MediaRecord) -> Result<Self, AppError> {
        Self::build(|output| {
            output
                .write_all(b"{\"ok\":true,\"media\":")
                .map_err(|_| server_error())?;
            record.write_response(output).map_err(|_| server_error())?;
            output.write_all(b"}").map_err(|_| server_error())
        })
    }

    pub(crate) fn home(value: &Value, media: &[MediaRecord]) -> Result<Self, AppError> {
        let fields = value.as_object().ok_or_else(server_error)?;
        let recent = value
            .get("recent")
            .and_then(Value::as_object)
            .ok_or_else(server_error)?;
        if recent.get("media") != Some(&Value::Array(Vec::new())) {
            return Err(server_error());
        }
        // Walk known structural fields, never search raw JSON bytes for a
        // placeholder that user-controlled strings could impersonate.
        Self::build(|output| {
            output.write_all(b"{").map_err(|_| server_error())?;
            for (index, (key, value)) in fields.iter().enumerate() {
                if index != 0 {
                    output.write_all(b",").map_err(|_| server_error())?;
                }
                serde_json::to_writer(&mut *output, key).map_err(|_| server_error())?;
                output.write_all(b":").map_err(|_| server_error())?;
                if key != "recent" {
                    serde_json::to_writer(&mut *output, value).map_err(|_| server_error())?;
                    continue;
                }
                output.write_all(b"{").map_err(|_| server_error())?;
                for (index, (key, value)) in recent.iter().enumerate() {
                    if index != 0 {
                        output.write_all(b",").map_err(|_| server_error())?;
                    }
                    serde_json::to_writer(&mut *output, key).map_err(|_| server_error())?;
                    output.write_all(b":").map_err(|_| server_error())?;
                    if key == "media" {
                        output.write_all(b"[").map_err(|_| server_error())?;
                        for (index, record) in media.iter().enumerate() {
                            if index != 0 {
                                output.write_all(b",").map_err(|_| server_error())?;
                            }
                            record.write_response(output).map_err(|_| server_error())?;
                        }
                        output.write_all(b"]").map_err(|_| server_error())?;
                    } else {
                        serde_json::to_writer(&mut *output, value).map_err(|_| server_error())?;
                    }
                }
                output.write_all(b"}").map_err(|_| server_error())?;
            }
            output.write_all(b"}").map_err(|_| server_error())
        })
    }

    fn stream(self) -> (Body, tokio::task::JoinHandle<()>) {
        let (sender, mut receiver) = tokio::sync::mpsc::channel::<io::Result<Bytes>>(1);
        let producer = tokio::spawn(async move {
            // Drop the async reader before the owning named file on Windows.
            let owned = self.file;
            let mut input = match owned.as_file().try_clone() {
                Ok(file) => tokio::fs::File::from_std(file),
                Err(error) => {
                    let _ = sender.send(Err(error)).await;
                    return;
                }
            };
            let mut digest = Sha256::new();
            let mut length = 0u64;
            let mut final_chunk = None;
            loop {
                let mut bytes = vec![0u8; BUFFER];
                match input.read(&mut bytes).await {
                    Ok(0) => break,
                    Ok(count) => {
                        let Some(next) = length
                            .checked_add(count as u64)
                            .filter(|next| *next <= self.length)
                        else {
                            let _ = sender
                                .send(Err(io::Error::other("media JSON response length changed")))
                                .await;
                            return;
                        };
                        length = next;
                        bytes.truncate(count);
                        digest.update(&bytes);
                        let bytes = Bytes::from(bytes);
                        if length == self.length {
                            // Hyper can stop at Content-Length without polling
                            // EOF. Keep the final bytes until integrity is known.
                            final_chunk = Some(bytes);
                        } else if sender.send(Ok(bytes)).await.is_err() {
                            return;
                        }
                    }
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                    Err(error) => {
                        let _ = sender.send(Err(error)).await;
                        return;
                    }
                }
            }
            if length != self.length || digest.finalize().to_vec() != self.hash {
                let _ = sender
                    .send(Err(io::Error::other("media JSON response changed")))
                    .await;
            } else if let Some(bytes) = final_chunk {
                let _ = sender.send(Ok(bytes)).await;
            }
        });
        let body = Body::from_stream(async_stream::stream! {
            while let Some(chunk)=receiver.recv().await { yield chunk; }
        });
        (body, producer)
    }
}
impl IntoResponse for CompletedJson {
    fn into_response(self) -> Response {
        let length = self.length;
        let (body, _producer) = self.stream();
        (
            StatusCode::OK,
            [
                (header::CONTENT_TYPE, "application/json"),
                (header::CONTENT_LENGTH, &length.to_string()),
            ],
            body,
        )
            .into_response()
    }
}

#[cfg(test)]
#[path = "media_read_spool_tests.rs"]
mod tests;
