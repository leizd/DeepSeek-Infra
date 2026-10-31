//! Conditional local index replacement for an already admitted PROCESS.
//! Both the old stored column codecs and each original vector row are retained.
//! Each transaction records its own original-operation phase before returning.
use crate::{media_create, media_library, media_processing};
use deepseek_policy::{memory_index, sqlite_vec_runtime};
use deepseek_protocol::generated::deepseek::media::v1::{
    MediaIndexItem, MediaPatchClaim, MediaPatchOutcome, MediaProcessPreparation,
    MediaProcessingPreparation, MediaStoredIndexRow, MediaStoredVectorRow,
};
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use std::{fs, path::Path, time::Duration};
use tonic::Status;

fn unknown() -> Status {
    Status::unavailable("EFFECT_UNKNOWN")
}

fn check_paths(root: &Path) -> Result<(), Status> {
    let path = memory_index::local_rag_db(root);
    for p in [
        path.clone(),
        path.with_file_name("rag.sqlite3-wal"),
        path.with_file_name("rag.sqlite3-shm"),
        path.with_file_name("rag.sqlite3-journal"),
    ] {
        media_library::reject_links(&p).map_err(|_| unknown())?;
    }
    Ok(())
}

fn has_table(c: &Connection, name: &str, vector: bool) -> Result<bool, Status> {
    let definition: Option<(String, String)> = c
        .query_row(
            "SELECT type,sql FROM sqlite_schema WHERE name=?1",
            [name],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(|_| unknown())?;
    let Some((kind, sql)) = definition else {
        return Ok(false);
    };
    let compact = sql
        .chars()
        .filter(|c| !c.is_ascii_whitespace())
        .collect::<String>()
        .to_ascii_lowercase();
    if kind != "table"
        || (vector && !compact.starts_with("createvirtualtablerag_vecusingvec0("))
        || (!vector && compact.contains("virtualtable"))
    {
        return Err(unknown());
    }
    Ok(true)
}

fn stored_rows(
    c: &Connection,
    id: &str,
    project: &str,
    effect: &MediaProcessingPreparation,
) -> Result<Vec<MediaStoredIndexRow>, Status> {
    let triggers:i64=c.query_row("SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND tbl_name IN ('rag_items','rag_meta','rag_vec')",[],|r|r.get(0)).map_err(|_|unknown())?;
    if triggers != 0 {
        return Err(unknown());
    }
    has_table(c, "rag_meta", false)?;
    let items = has_table(c, "rag_items", false)?;
    let vector = effect.index_backend == "sqlite_vec" && has_table(c, "rag_vec", true)?;
    if !items {
        return Ok(vec![]);
    }
    let mut stmt=c.prepare("SELECT item_id,vector_id,collection,source_id,project_id,chunk_index,name,kind,scope,text,embedding,metadata,updated_at FROM rag_items WHERE collection='media' AND source_id=?1 AND (?2='' OR project_id=?2) ORDER BY item_id").map_err(|_|unknown())?;
    let mut rows = stmt
        .query_map(params![id, project], |r| {
            Ok(MediaStoredIndexRow {
                record: Some(MediaIndexItem {
                    item_id: r.get(0)?,
                    vector_id: r.get(1)?,
                    collection: r.get(2)?,
                    source_id: r.get(3)?,
                    project_id: r.get(4)?,
                    chunk_index: 0,
                    name: r.get(6)?,
                    kind: r.get(7)?,
                    scope: r.get(8)?,
                    text: r.get(9)?,
                    updated_at_millis: r.get(12)?,
                    ..Default::default()
                }),
                embedding_json: r.get(10)?,
                metadata_json: r.get(11)?,
                vector: None,
                chunk_index: r.get(5)?,
            })
        })
        .map_err(|_| unknown())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|_| unknown())?;
    if vector {
        for row in &mut rows {
            let record = row.record.as_ref().ok_or_else(unknown)?;
            row.vector=c.query_row("SELECT rowid,item_id,collection,source_id,project_id,scope,embedding FROM rag_vec WHERE rowid=?1",[record.vector_id],|r|Ok(MediaStoredVectorRow{row_id:r.get(0)?,item_id:r.get(1)?,collection:r.get(2)?,source_id:r.get(3)?,project_id:r.get(4)?,scope:r.get(5)?,embedding:r.get(6)?})).optional().map_err(|_|unknown())?;
        }
    }
    Ok(rows)
}

pub(super) fn snapshot(
    root: &Path,
    id: &str,
    project: &str,
    effect: &MediaProcessingPreparation,
) -> Result<Vec<MediaStoredIndexRow>, Status> {
    if !effect.index_enabled || (!effect.force && effect.final_http_status != 200) {
        return Ok(vec![]);
    }
    check_paths(root)?;
    let path = memory_index::local_rag_db(root);
    if !path.exists() {
        return Ok(vec![]);
    }
    if !path.is_file() {
        return Err(unknown());
    }
    sqlite_vec_runtime::register().map_err(|_| unknown())?;
    let c = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|_| unknown())?;
    c.busy_timeout(Duration::from_secs(5))
        .map_err(|_| unknown())?;
    c.execute_batch("PRAGMA trusted_schema=OFF;")
        .map_err(|_| unknown())?;
    stored_rows(&c, id, project, effect)
}

fn expected_rows(effect: &MediaProcessingPreparation) -> Result<Vec<MediaStoredIndexRow>, Status> {
    let mut rows = Vec::new();
    for item in &effect.index_items {
        let (embedding_json, metadata_json, blob) = media_processing::row_values(item)?;
        let mut stored = item.clone();
        stored.embedding_f64le.clear();
        stored.metadata = None;
        stored.chunk_index = 0;
        rows.push(MediaStoredIndexRow {
            record: Some(stored),
            embedding_json,
            metadata_json,
            chunk_index: i64::from(item.chunk_index),
            vector: (effect.index_backend == "sqlite_vec").then(|| MediaStoredVectorRow {
                row_id: item.vector_id,
                item_id: item.item_id.clone(),
                collection: item.collection.clone(),
                source_id: item.source_id.clone(),
                project_id: item.project_id.clone(),
                scope: item.scope.clone(),
                embedding: blob,
            }),
        });
    }
    rows.sort_by(|a, b| {
        a.record
            .as_ref()
            .map(|v| &v.item_id)
            .cmp(&b.record.as_ref().map(|v| &v.item_id))
    });
    Ok(rows)
}

fn confirm(
    c: &Connection,
    id: &str,
    project: &str,
    effect: &MediaProcessingPreparation,
    expected: &[MediaStoredIndexRow],
) -> Result<(), Status> {
    if stored_rows(c, id, project, effect)? != expected {
        return Err(unknown());
    }
    Ok(())
}

fn remove(c: &Connection, rows: &[MediaStoredIndexRow]) -> Result<(), Status> {
    for row in rows {
        let item = row.record.as_ref().ok_or_else(unknown)?;
        if c.execute("DELETE FROM rag_items WHERE item_id=?1", [&item.item_id])
            .map_err(|_| unknown())?
            != 1
        {
            return Err(unknown());
        }
        if let Some(vector) = &row.vector {
            if c.execute("DELETE FROM rag_vec WHERE rowid=?1", [vector.row_id])
                .map_err(|_| unknown())?
                != 1
            {
                return Err(unknown());
            }
        }
    }
    Ok(())
}

fn insert(c: &Connection, effect: &MediaProcessingPreparation) -> Result<(), Status> {
    for item in &effect.index_items {
        let (embedding, metadata, blob) = media_processing::row_values(item)?;
        c.execute("INSERT INTO rag_items(item_id,vector_id,collection,source_id,project_id,chunk_index,name,kind,scope,text,embedding,metadata,updated_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13)",params![item.item_id,item.vector_id,item.collection,item.source_id,item.project_id,item.chunk_index,item.name,item.kind,item.scope,item.text,embedding,metadata,item.updated_at_millis]).map_err(|_|unknown())?;
        if effect.index_backend == "sqlite_vec" {
            c.execute("INSERT INTO rag_vec(rowid,item_id,collection,source_id,project_id,scope,embedding) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![item.vector_id,item.item_id,item.collection,item.source_id,item.project_id,item.scope,blob]).map_err(|_|unknown())?;
        }
    }
    Ok(())
}

#[derive(Clone, Copy, PartialEq)]
pub(super) enum Phase {
    Clear,
    Final,
}

pub(super) fn finish(
    root: &Path,
    claim: &MediaPatchClaim,
    outcome: &MediaPatchOutcome,
    prepared: &MediaProcessPreparation,
    phase: Phase,
) -> Result<(), Status> {
    let effects = prepared.effects.as_ref().ok_or_else(unknown)?;
    let effect = effects.processing.as_ref().ok_or_else(unknown)?;
    if !effect.index_enabled {
        return Ok(());
    }
    let intent = claim.intent.as_ref().ok_or_else(unknown)?;
    let record = effect.processing_record.as_ref().ok_or_else(unknown)?;
    let record = crate::media_values::to_json(
        &deepseek_protocol::generated::deepseek::media::v1::MediaValue {
            kind: Some(
                deepseek_protocol::generated::deepseek::media::v1::media_value::Kind::ObjectValue(
                    record.clone(),
                ),
            ),
        },
    )?;
    let project = record["projectId"].as_str().unwrap_or("");
    if phase == Phase::Clear && !effect.force {
        return Err(unknown());
    }
    if phase == Phase::Final && effect.final_http_status != 200 {
        return Err(unknown());
    }
    check_paths(root)?;
    let path = memory_index::local_rag_db(root);
    fs::create_dir_all(path.parent().ok_or_else(unknown)?).map_err(|_| unknown())?;
    check_paths(root)?;
    sqlite_vec_runtime::register().map_err(|_| unknown())?;
    let mut c = Connection::open_with_flags(
        &path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
    )
    .map_err(|_| unknown())?;
    c.busy_timeout(Duration::from_secs(5))
        .map_err(|_| unknown())?;
    c.execute_batch("PRAGMA trusted_schema=OFF; PRAGMA synchronous=FULL;")
        .map_err(|_| unknown())?;
    media_processing::schema(&c, effect)?;
    let digest = outcome
        .processed
        .as_ref()
        .ok_or_else(unknown)?
        .preparation_sha256
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect::<String>();
    let clear_key = format!("native_media_process:{}:clear", intent.operation_id);
    let final_key = format!("native_media_process:{}:final", intent.operation_id);
    let tx = c
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| unknown())?;
    let marker = |key: &str| -> Result<Option<String>, Status> {
        tx.query_row("SELECT value FROM rag_meta WHERE key=?1", [key], |r| {
            r.get(0)
        })
        .optional()
        .map_err(|_| unknown())
    };
    let cleared = marker(&clear_key)?;
    let finished = marker(&final_key)?;
    if cleared.as_ref().is_some_and(|v| v != &digest)
        || finished.as_ref().is_some_and(|v| v != &digest)
        || (finished.is_some() && effect.force && cleared.is_none())
    {
        return Err(unknown());
    }
    let expected = expected_rows(effect)?;
    if finished.is_some() {
        // A replay of the earlier clear phase must never remove final rows.
        confirm(&tx, &intent.media_id, project, effect, &expected)?;
    } else if phase == Phase::Clear {
        if cleared.is_some() {
            confirm(&tx, &intent.media_id, project, effect, &[])?;
        } else {
            confirm(
                &tx,
                &intent.media_id,
                project,
                effect,
                &prepared.before_index_items,
            )?;
            remove(&tx, &prepared.before_index_items)?;
            tx.execute(
                "INSERT INTO rag_meta(key,value) VALUES(?1,?2)",
                params![clear_key, digest],
            )
            .map_err(|_| unknown())?;
            confirm(&tx, &intent.media_id, project, effect, &[])?;
        }
    } else {
        if effect.force {
            if cleared.is_none() {
                return Err(unknown());
            }
            confirm(&tx, &intent.media_id, project, effect, &[])?;
        } else {
            confirm(
                &tx,
                &intent.media_id,
                project,
                effect,
                &prepared.before_index_items,
            )?;
            remove(&tx, &prepared.before_index_items)?;
        }
        insert(&tx, effect)?;
        tx.execute(
            "INSERT OR REPLACE INTO rag_meta(key,value) VALUES('embedding_dimensions',?1)",
            [effect.embedding_dimensions.to_string()],
        )
        .map_err(|_| unknown())?;
        tx.execute(
            "INSERT OR REPLACE INTO rag_meta(key,value) VALUES('embedding_provider','hash')",
            [],
        )
        .map_err(|_| unknown())?;
        tx.execute(
            "INSERT INTO rag_meta(key,value) VALUES(?1,?2)",
            params![final_key, digest],
        )
        .map_err(|_| unknown())?;
        confirm(&tx, &intent.media_id, project, effect, &expected)?;
    }
    tx.commit().map_err(|_| unknown())?;
    fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&path)
        .and_then(|f| f.sync_all())
        .map_err(|_| unknown())?;
    media_create::sync_directory(path.parent().ok_or_else(unknown)?)?;
    Ok(())
}
