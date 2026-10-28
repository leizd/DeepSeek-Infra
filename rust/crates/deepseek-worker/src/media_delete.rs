//! Data cleanup for an already Go-admitted, durably published original DELETE.
//! Callers hold the library journal lock and workspace mutation scope. This
//! module cannot admit, replace or re-execute an operation.
use crate::media_library::reject_links;
use deepseek_policy::{memory_index, sqlite_vec_runtime};
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior};
use std::{fs, io::ErrorKind, path::Path, time::Duration};
use tonic::Status;

fn unknown() -> Status {
    Status::unavailable("EFFECT_UNKNOWN")
}

fn index_connection(root: &Path, writable: bool) -> Result<Option<Connection>, Status> {
    // Deletion of an existing copy is independent of search/index enablement.
    // A changed environment on recovery cannot waive the original cleanup.
    let path = memory_index::local_rag_db(root);
    reject_links(&path).map_err(|_| unknown())?;
    for suffix in ["-wal", "-shm", "-journal"] {
        reject_links(&path.with_file_name(format!("rag.sqlite3{suffix}")))
            .map_err(|_| unknown())?;
    }
    match fs::symlink_metadata(&path) {
        Err(error) if error.kind() == ErrorKind::NotFound => return Ok(None),
        Ok(metadata) if metadata.is_file() => {}
        _ => return Err(unknown()),
    }
    sqlite_vec_runtime::register().map_err(|_| unknown())?;
    let flags = if writable {
        OpenFlags::SQLITE_OPEN_READ_WRITE
    } else {
        OpenFlags::SQLITE_OPEN_READ_ONLY
    };
    let connection = Connection::open_with_flags(&path, flags).map_err(|_| unknown())?;
    connection
        .busy_timeout(Duration::from_secs(5))
        .map_err(|_| unknown())?;
    connection
        .execute_batch("PRAGMA trusted_schema=OFF;")
        .map_err(|_| unknown())?;
    let table: Option<(String, String)> = connection
        .query_row(
            "SELECT type,sql FROM sqlite_schema WHERE name='rag_items'",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|_| unknown())?;
    let Some((kind, definition)) = table else {
        return Ok(None);
    };
    if kind != "table" || definition.to_ascii_uppercase().contains("VIRTUAL TABLE") {
        return Err(unknown());
    }
    let triggers: i64 = connection.query_row(
        "SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND tbl_name IN ('rag_items','rag_vec')", [], |row| row.get(0),
    ).map_err(|_| unknown())?;
    if triggers != 0 {
        return Err(unknown());
    }
    // Preparing this statement proves the target columns are real columns; no
    // untrusted view or trigger is allowed to widen the cleanup's write scope.
    connection
        .prepare("SELECT vector_id,collection,source_id FROM rag_items LIMIT 0")
        .map_err(|_| unknown())?;
    Ok(Some(connection))
}

pub(super) fn preflight(root: &Path, media_id: &str) -> Result<(), Status> {
    for target in [
        root.join(".media/segments")
            .join(format!("{media_id}.json")),
        root.join(".media/objects").join(media_id),
    ] {
        reject_links(&target).map_err(|_| unknown())?;
    }
    // This connection is read-only. A refusal here precedes library publication.
    let _ = index_connection(root, false)?;
    Ok(())
}

#[cfg(unix)]
fn sync_parent(path: &Path) -> Result<(), Status> {
    let parent = path.parent().ok_or_else(unknown)?;
    match fs::File::open(parent) {
        Ok(file) => file.sync_all().map_err(|_| unknown()),
        Err(error) if error.kind() == ErrorKind::NotFound => Ok(()),
        Err(_) => Err(unknown()),
    }
}

#[cfg(not(unix))]
fn sync_parent(_path: &Path) -> Result<(), Status> {
    // As with library publication, portable Windows directory synchronization
    // is not claimed. Actual Windows process/restart evidence remains separate.
    Ok(())
}

pub(super) fn finish(root: &Path, media_id: &str) -> Result<(), Status> {
    preflight(root, media_id)?;
    let segments = root
        .join(".media/segments")
        .join(format!("{media_id}.json"));
    match fs::remove_file(&segments) {
        Ok(()) => {}
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(_) => return Err(unknown()),
    }
    sync_parent(&segments)?;
    let objects = root.join(".media/objects").join(media_id);
    match fs::remove_dir_all(&objects) {
        Ok(()) => {}
        Err(error) if error.kind() == ErrorKind::NotFound => {}
        Err(_) => return Err(unknown()),
    }
    sync_parent(&objects)?;
    if let Some(mut connection) = index_connection(root, true)? {
        connection
            .execute_batch("PRAGMA synchronous=FULL;")
            .map_err(|_| unknown())?;
        let transaction = connection
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|_| unknown())?;
        let vectors: Vec<i64> = {
            let mut statement = transaction
                .prepare(
                    "SELECT vector_id FROM rag_items WHERE collection='media' AND source_id=?1",
                )
                .map_err(|_| unknown())?;
            statement
                .query_map([media_id], |row| row.get(0))
                .map_err(|_| unknown())?
                .collect::<Result<_, _>>()
                .map_err(|_| unknown())?
        };
        let vector_table: bool = transaction
            .query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE name='rag_vec' AND type='table')",
                [],
                |row| row.get(0),
            )
            .map_err(|_| unknown())?;
        if vector_table {
            for id in vectors {
                transaction
                    .execute("DELETE FROM rag_vec WHERE rowid=?1", [id])
                    .map_err(|_| unknown())?;
            }
        }
        transaction
            .execute(
                "DELETE FROM rag_items WHERE collection='media' AND source_id=?1",
                [media_id],
            )
            .map_err(|_| unknown())?;
        transaction.commit().map_err(|_| unknown())?;
        sync_parent(&memory_index::local_rag_db(root))?;
    }
    Ok(())
}
