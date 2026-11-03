//! Complete, private read snapshots; only one record and one response chunk are resident.
use super::{accepted_media_id, media_sort_key, normalize_media_record, server_error};
use axum::{
    body::{Body, Bytes},
    http::{StatusCode, header},
    response::{IntoResponse, Response},
};
use deepseek_policy::{
    app_error::AppError,
    media_records::{Records, SourceReadError, VisitError},
    media_stream_io::{DigestReader, DigestWriter},
};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::{
    fs::File,
    io::{self, BufWriter, Read, Seek, SeekFrom, Write},
    path::Path,
};

const BUFFER: usize = 64 * 1024;

fn raw(path: &Path) -> Result<Records, AppError> {
    let source = match File::open(path) {
        Ok(source) => source,
        Err(_) => return Records::empty().map_err(|_| server_error()),
    };
    match Records::from_raw_reader(source) {
        Ok(records) => Ok(records),
        Err(SourceReadError::SourceIo) => Records::empty().map_err(|_| server_error()),
        Err(SourceReadError::InvalidUtf8 | SourceReadError::Storage(_)) => Err(server_error()),
    }
}

fn visited(result: Result<(), VisitError<AppError>>) -> Result<(), AppError> {
    result.map_err(|error| match error {
        VisitError::Storage(_) => server_error(),
        VisitError::Visitor(error) => error,
    })
}

fn public(
    path: &Path,
    mut visit: impl FnMut(Value) -> Result<(), AppError>,
) -> Result<(), AppError> {
    let records = raw(path)?;
    visited(records.visit_source(|value| {
        let Ok(stored) = normalize_media_record(&value) else {
            return Ok(());
        };
        // Every row completes the second normalization, even after a match or
        // outside a filter/window. Its AppError must propagate, as before.
        visit(normalize_media_record(&stored)?)
    }))
}

pub(super) fn find(path: &Path, id: &str) -> Result<Option<Value>, AppError> {
    let mut found = None;
    public(path, |value| {
        if found.is_none() && value["mediaId"].as_str() == Some(id) {
            found = Some(value);
        }
        Ok(())
    })?;
    Ok(found)
}

pub(super) fn contains(path: &Path, id: &str) -> Result<bool, AppError> {
    // Parsing has already consumed and validated the full original source.
    // Membership reproduces the raw first-normalization acceptance check.
    match raw(path)?.visit_source(|value| {
        if accepted_media_id(&value).as_deref() == Some(id) {
            Err(())
        } else {
            Ok(())
        }
    }) {
        Ok(()) => Ok(false),
        Err(VisitError::Visitor(())) => Ok(true),
        Err(VisitError::Storage(_)) => Err(server_error()),
    }
}

pub(super) struct PublicIndex {
    // Close SQLite before NamedTempFile removes its path on Windows.
    order: Connection,
    records: Records,
    _order_file: tempfile::NamedTempFile,
    count: usize,
}
impl PublicIndex {
    pub(super) fn load(path: &Path) -> Result<Self, AppError> {
        let order_file = tempfile::NamedTempFile::new().map_err(|_| server_error())?;
        let order = Connection::open(order_file.path()).map_err(|_| server_error())?;
        order.execute_batch(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-2048; PRAGMA temp_store=FILE;
             CREATE TABLE ordering (ordinal INTEGER PRIMARY KEY, sort_key TEXT COLLATE BINARY NOT NULL);
             CREATE INDEX media_order ON ordering(sort_key DESC,ordinal ASC); BEGIN;"
        ).map_err(|_| server_error())?;
        let mut records = Records::empty().map_err(|_| server_error())?;
        let mut count = 0usize;
        public(path, |value| {
            let ordinal = i64::try_from(count).map_err(|_| server_error())?;
            let sort_key = media_sort_key(&value);
            records.append(&value).map_err(|_| server_error())?;
            order
                .execute(
                    "INSERT INTO ordering VALUES(?1,?2)",
                    params![ordinal, sort_key],
                )
                .map_err(|_| server_error())?;
            count = count.checked_add(1).ok_or_else(server_error)?;
            Ok(())
        })?;
        Ok(Self {
            order,
            records,
            _order_file: order_file,
            count,
        })
    }

    fn visit_sorted(
        &self,
        mut visit: impl FnMut(Value) -> Result<(), AppError>,
    ) -> Result<(), AppError> {
        let mut statement = self
            .order
            .prepare("SELECT ordinal FROM ordering ORDER BY sort_key DESC,ordinal ASC")
            .map_err(|_| server_error())?;
        let mut rows = statement.query([]).map_err(|_| server_error())?;
        while let Some(row) = rows.next().map_err(|_| server_error())? {
            let ordinal: i64 = row.get(0).map_err(|_| server_error())?;
            let value = self
                .records
                .get(ordinal)
                .map_err(|_| server_error())?
                .ok_or_else(server_error)?;
            visit(value)?;
        }
        Ok(())
    }

    pub(super) fn window(&self, limit: usize) -> Result<(Vec<Value>, usize), AppError> {
        let mut values = Vec::new();
        // All source rows were normalized during load, before selecting a window.
        let mut statement = self
            .order
            .prepare("SELECT ordinal FROM ordering ORDER BY sort_key DESC,ordinal ASC LIMIT ?1")
            .map_err(|_| server_error())?;
        let limit = i64::try_from(limit.min(self.count)).map_err(|_| server_error())?;
        let mut rows = statement.query([limit]).map_err(|_| server_error())?;
        while let Some(row) = rows.next().map_err(|_| server_error())? {
            let ordinal: i64 = row.get(0).map_err(|_| server_error())?;
            values.push(
                self.records
                    .get(ordinal)
                    .map_err(|_| server_error())?
                    .ok_or_else(server_error)?,
            );
        }
        Ok((values, self.count))
    }

    pub(super) fn collection(
        &self,
        mut include: impl FnMut(&Value) -> bool,
    ) -> Result<CompletedJson, AppError> {
        let envelope =
            serde_json::to_vec(&json!({"ok":true,"media":[]})).map_err(|_| server_error())?;
        let split = envelope
            .windows(2)
            .position(|bytes| bytes == b"[]")
            .ok_or_else(server_error)?;
        let mut file = tempfile::NamedTempFile::new().map_err(|_| server_error())?;
        let (length, hash) = {
            let mut output =
                BufWriter::with_capacity(BUFFER, DigestWriter::new(file.as_file_mut()));
            output
                .write_all(&envelope[..split + 1])
                .map_err(|_| server_error())?;
            let mut first = true;
            self.visit_sorted(|value| {
                if !include(&value) {
                    return Ok(());
                }
                if !first {
                    output.write_all(b",").map_err(|_| server_error())?;
                }
                first = false;
                serde_json::to_writer(&mut output, &value).map_err(|_| server_error())
            })?;
            output
                .write_all(&envelope[split + 1..])
                .map_err(|_| server_error())?;
            output.flush().map_err(|_| server_error())?;
            output.into_inner().map_err(|_| server_error())?.finish()
        };
        file.as_file_mut()
            .seek(SeekFrom::Start(0))
            .map_err(|_| server_error())?;
        Ok(CompletedJson { file, length, hash })
    }
}

pub(super) struct CompletedJson {
    file: tempfile::NamedTempFile,
    length: u64,
    hash: Vec<u8>,
}
impl CompletedJson {
    fn stream(self) -> (Body, tokio::task::JoinHandle<()>) {
        let (sender, mut receiver) = tokio::sync::mpsc::channel::<io::Result<Bytes>>(1);
        let producer = tokio::task::spawn_blocking(move || {
            // The producer owns the named file until every reader has closed.
            let mut file = self.file;
            let mut input = DigestReader::new(file.as_file_mut());
            loop {
                let mut bytes = vec![0u8; BUFFER];
                match input.read(&mut bytes) {
                    Ok(0) => break,
                    Ok(count) => {
                        bytes.truncate(count);
                        if sender.blocking_send(Ok(Bytes::from(bytes))).is_err() {
                            return;
                        }
                    }
                    Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                    Err(error) => {
                        let _ = sender.blocking_send(Err(error));
                        return;
                    }
                }
            }
            let (length, hash) = input.finish();
            if length != self.length || hash != self.hash {
                let _ = sender.blocking_send(Err(io::Error::other("media JSON response changed")));
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
