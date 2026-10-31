//! One original CREATE's prepared processing effects. No admission or new fence.
//! The caller holds the media journal lock and workspace mutation scope. A
//! partial write remains unknown until its retained bytes/transaction are read
//! and confirmed; absence never grants a second business request.
use crate::{media_create, media_library, media_values};
use deepseek_policy::{
    core_utils::utc_now_iso,
    entropy::{Entropy, SystemEntropy},
    media_processing::{index_items, prepare_segments, segment_metadata},
    media_schema::{normalize_media_metadata, normalize_media_record},
    memory_index, sqlite_vec_runtime,
};
use deepseek_protocol::generated::deepseek::media::v1::{
    MediaArray, MediaCreatePreparation, MediaIndexItem, MediaObject, MediaPatchClaim,
    MediaPatchOutcome, MediaProcessingPreparation, MediaValue, media_value::Kind,
};
use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use serde_json::{Value, json};
use std::fmt::Write as _;
use std::{fs, path::Path, time::Duration};
use tonic::Status;

fn unknown() -> Status {
    Status::unavailable("EFFECT_UNKNOWN")
}
fn object(value: &Value) -> Result<MediaObject, Status> {
    match media_values::from_json_legacy(value)?.kind {
        Some(Kind::ObjectValue(object)) => Ok(object),
        _ => Err(media_values::invalid()),
    }
}
fn value(object: &MediaObject) -> Result<Value, Status> {
    media_values::to_json(&MediaValue {
        kind: Some(Kind::ObjectValue(object.clone())),
    })
}

/// Pure extraction runs before the dispatch permit. JSON registration has no
/// OCR argument in the original API and process_media receives None, so this
/// path does not invoke OCR engines or external providers before admission.
pub(super) fn prepare(
    prepared: MediaCreatePreparation,
    now: i64,
) -> Result<MediaCreatePreparation, Status> {
    prepare_with_ocr(prepared, now, false)
}

pub(super) fn prepare_with_ocr(
    mut prepared: MediaCreatePreparation,
    now: i64,
    ocr_enabled: bool,
) -> Result<MediaCreatePreparation, Status> {
    let enabled = std::env::var("LOCAL_RAG_ENABLED")
        .map(|v| ["1", "true", "yes", "on"].contains(&v.trim().to_lowercase().as_str()))
        .unwrap_or(true);
    let backend = std::env::var("LOCAL_RAG_BACKEND")
        .ok()
        .map(|v| v.trim().to_lowercase())
        .filter(|v| v == "sqlite" || v == "sqlite_vec")
        .unwrap_or_else(|| "sqlite_vec".into());
    let dimensions = std::env::var("LOCAL_RAG_EMBEDDING_DIMENSIONS")
        .ok()
        .and_then(|v| v.trim().parse::<i64>().ok())
        .unwrap_or(64)
        .clamp(8, 4096) as usize;
    if enabled
        && std::env::var("LOCAL_RAG_EMBEDDING_PROVIDER")
            .is_ok_and(|v| v.trim().to_lowercase() == "onnx")
    {
        return Err(Status::unimplemented("MEDIA_EMBEDDING_PROVIDER_NOT_READY"));
    }
    let mut processing = value(prepared.record.as_ref().ok_or_else(media_values::invalid)?)?;
    processing["status"] = json!("processing");
    processing["updatedAt"] = json!(utc_now_iso(now).replace("+00:00", "Z"));
    let mut final_record = processing.clone();
    let mut metadata = processing["metadata"]
        .as_object()
        .cloned()
        .unwrap_or_default();
    let mut effect = MediaProcessingPreparation {
        schema_version: 1,
        processing_record: Some(object(&processing)?),
        index_enabled: enabled,
        index_backend: backend,
        embedding_dimensions: dimensions as u32,
        final_http_status: 200,
        ..Default::default()
    };
    match prepare_segments(
        &processing,
        &prepared.source_bytes,
        ocr_enabled,
        &SystemEntropy,
    ) {
        Ok(segments) => {
            let rows = if enabled {
                index_items(
                    &processing,
                    &segments,
                    dimensions,
                    SystemEntropy.now_millis(),
                )
            } else {
                vec![]
            };
            let stats = segment_metadata(&segments, rows.len());
            metadata.extend(stats.as_object().ok_or_else(media_values::invalid)?.clone());
            let array = media_values::from_json_legacy(&json!(segments))?;
            let Some(Kind::ArrayValue(array)) = array.kind else {
                return Err(media_values::invalid());
            };
            effect.segments = Some(array);
            for row in rows {
                let embedding = row["embedding"]
                    .as_array()
                    .ok_or_else(media_values::invalid)?
                    .iter()
                    .map(|v| v.as_f64().ok_or_else(media_values::invalid))
                    .collect::<Result<Vec<_>, _>>()?;
                effect.index_items.push(MediaIndexItem {
                    item_id: row["item_id"]
                        .as_str()
                        .ok_or_else(media_values::invalid)?
                        .into(),
                    collection: "media".into(),
                    source_id: processing["mediaId"]
                        .as_str()
                        .ok_or_else(media_values::invalid)?
                        .into(),
                    project_id: processing["projectId"].as_str().unwrap_or("").into(),
                    chunk_index: row["chunk_index"]
                        .as_u64()
                        .ok_or_else(media_values::invalid)? as u32,
                    name: row["name"]
                        .as_str()
                        .ok_or_else(media_values::invalid)?
                        .into(),
                    kind: row["kind"]
                        .as_str()
                        .ok_or_else(media_values::invalid)?
                        .into(),
                    scope: String::new(),
                    text: row["text"]
                        .as_str()
                        .ok_or_else(media_values::invalid)?
                        .into(),
                    embedding_f64le: embedding.into_iter().flat_map(f64::to_le_bytes).collect(),
                    metadata: Some(object(&row["metadata"])?),
                    updated_at_millis: row["updated_at"]
                        .as_i64()
                        .ok_or_else(media_values::invalid)?,
                    vector_id: row["vector_id"]
                        .as_i64()
                        .ok_or_else(media_values::invalid)?,
                });
            }
            final_record["status"] = json!("ready");
        }
        Err(error) => {
            final_record["status"] = json!("failed");
            metadata.insert(
                "error".into(),
                json!(error.message.chars().take(500).collect::<String>()),
            );
            effect.final_http_status = error.status as u32;
            effect.final_error_code = error.code.into();
            effect.final_error_message = error.message;
        }
    }
    final_record["metadata"] = normalize_media_metadata(Some(&Value::Object(metadata)));
    prepared.record = Some(object(
        &normalize_media_record(&final_record).map_err(|_| media_values::invalid())?,
    )?);
    prepared.processing = Some(effect);
    Ok(prepared)
}

pub(super) fn has_processing(
    staging: &Path,
    claim: &MediaPatchClaim,
    outcome: &MediaPatchOutcome,
) -> Result<bool, Status> {
    if outcome.created.is_none() {
        return Ok(false);
    }
    Ok(media_create::retained_preparation(staging, claim, outcome)?
        .processing
        .is_some())
}

pub(super) fn validate(
    prepared: &MediaCreatePreparation,
    claim: &MediaPatchClaim,
    outcome: &MediaPatchOutcome,
) -> Result<(), Status> {
    let effect = prepared.processing.as_ref().ok_or_else(unknown)?;
    let id = &claim.intent.as_ref().ok_or_else(unknown)?.media_id;
    let processing = value(effect.processing_record.as_ref().ok_or_else(unknown)?)?;
    let final_record = value(prepared.record.as_ref().ok_or_else(unknown)?)?;
    if effect.schema_version != 1
        || effect.force
            != claim
                .intent
                .as_ref()
                .and_then(|i| i.process.as_ref())
                .is_some_and(|p| p.force)
        || processing["mediaId"].as_str() != Some(id)
        || final_record["mediaId"] != processing["mediaId"]
        || processing["status"] != "processing"
        || effect.final_http_status != outcome.http_status
        || effect.final_error_code != outcome.error_code
        || effect.final_error_message != outcome.error_message
        || !(8..=4096).contains(&effect.embedding_dimensions)
        || !["sqlite", "sqlite_vec"].contains(&effect.index_backend.as_str())
    {
        return Err(unknown());
    }
    if effect.final_http_status == 200 {
        if final_record["status"] != "ready"
            || effect.segments.is_none()
            || !effect.final_error_code.is_empty()
            || !effect.final_error_message.is_empty()
        {
            return Err(unknown());
        }
    } else if !(400..600).contains(&effect.final_http_status)
        || final_record["status"] != "failed"
        || effect.segments.is_some()
        || !effect.index_items.is_empty()
        || effect.final_error_code.is_empty()
        || effect.final_error_code == "EFFECT_UNKNOWN"
        || effect.final_error_message.is_empty()
    {
        return Err(unknown());
    }
    if !effect.index_enabled && !effect.index_items.is_empty() {
        return Err(unknown());
    }
    for item in &effect.index_items {
        if item.collection != "media"
            || &item.source_id != id
            || item.project_id != processing["projectId"].as_str().unwrap_or("")
            || !item.scope.is_empty()
            || item.embedding_f64le.len() != effect.embedding_dimensions as usize * 8
            || item.vector_id < 0
            || item.metadata.is_none()
            || item.text.chars().count() > 12_000
        {
            return Err(unknown());
        }
        if item
            .embedding_f64le
            .chunks_exact(8)
            .any(|b| !f64::from_le_bytes(b.try_into().expect("eight bytes")).is_finite())
        {
            return Err(unknown());
        }
    }
    Ok(())
}

pub(super) fn intermediate(
    after: &[u8],
    effect: &MediaProcessingPreparation,
    id: &str,
) -> Result<Vec<u8>, Status> {
    let mut document: Value = serde_json::from_slice(after).map_err(|_| unknown())?;
    let items = document["media"].as_array_mut().ok_or_else(unknown)?;
    let item = items
        .iter_mut()
        .find(|item| item["mediaId"].as_str() == Some(id))
        .ok_or_else(unknown)?;
    *item = value(effect.processing_record.as_ref().ok_or_else(unknown)?)?;
    document["nativePublication"]["phase"] = json!("PROCESSING");
    serde_json::to_vec_pretty(&document).map_err(|_| unknown())
}

pub(super) fn schema(
    connection: &Connection,
    effect: &MediaProcessingPreparation,
) -> Result<(), Status> {
    let triggers:i64=connection.query_row("SELECT COUNT(*) FROM sqlite_schema WHERE type='trigger' AND tbl_name IN ('rag_items','rag_meta','rag_vec')",[],|r|r.get(0)).map_err(|_|unknown())?;
    if triggers != 0 {
        return Err(unknown());
    }
    for name in ["rag_items", "rag_meta"] {
        let definition: Option<(String, String)> = connection
            .query_row(
                "SELECT type,sql FROM sqlite_schema WHERE name=?1",
                [name],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(|_| unknown())?;
        if definition.is_some_and(|(kind, sql)| {
            kind != "table" || sql.to_ascii_uppercase().contains("VIRTUAL TABLE")
        }) {
            return Err(unknown());
        }
    }
    connection.execute_batch("CREATE TABLE IF NOT EXISTS rag_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rag_items(item_id TEXT PRIMARY KEY,vector_id INTEGER NOT NULL UNIQUE,collection TEXT NOT NULL,source_id TEXT NOT NULL,project_id TEXT NOT NULL,chunk_index INTEGER NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL,scope TEXT NOT NULL,text TEXT NOT NULL,embedding TEXT NOT NULL,metadata TEXT NOT NULL,updated_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_rag_items_collection ON rag_items(collection);
        CREATE INDEX IF NOT EXISTS idx_rag_items_source ON rag_items(collection,source_id,project_id);
        CREATE INDEX IF NOT EXISTS idx_rag_items_scope ON rag_items(collection,scope);").map_err(|_|unknown())?;
    let dimensions: Option<String> = connection
        .query_row(
            "SELECT value FROM rag_meta WHERE key='embedding_dimensions'",
            [],
            |r| r.get(0),
        )
        .optional()
        .map_err(|_| unknown())?;
    if dimensions.is_some_and(|s| s != effect.embedding_dimensions.to_string()) {
        return Err(unknown());
    }
    if effect.index_backend == "sqlite_vec" {
        let definition: Option<(String, String)> = connection
            .query_row(
                "SELECT type,sql FROM sqlite_schema WHERE name='rag_vec'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(|_| unknown())?;
        if definition.is_some_and(|(kind, sql)| {
            let compact: String = sql
                .chars()
                .filter(|c| !c.is_ascii_whitespace())
                .collect::<String>()
                .to_lowercase();
            kind != "table" || !compact.starts_with("createvirtualtablerag_vecusingvec0(")
        }) {
            return Err(unknown());
        }
        connection.execute_batch(&format!("CREATE VIRTUAL TABLE IF NOT EXISTS rag_vec USING vec0(item_id TEXT,collection TEXT,source_id TEXT,project_id TEXT,scope TEXT,embedding float[{}]);",effect.embedding_dimensions)).map_err(|_|unknown())?;
    }
    Ok(())
}

pub(super) fn row_values(item: &MediaIndexItem) -> Result<(String, String, Vec<u8>), Status> {
    let embedding = item
        .embedding_f64le
        .chunks_exact(8)
        .map(|b| f64::from_le_bytes(b.try_into().expect("eight bytes")))
        .collect::<Vec<_>>();
    let blob = embedding
        .iter()
        .flat_map(|v| (*v as f32).to_le_bytes())
        .collect();
    Ok((
        serde_json::to_string(&embedding).map_err(|_| unknown())?,
        serde_json::to_string(&value(item.metadata.as_ref().ok_or_else(unknown)?)?)
            .map_err(|_| unknown())?,
        blob,
    ))
}

fn confirm_index(
    connection: &Connection,
    id: &str,
    effect: &MediaProcessingPreparation,
) -> Result<(), Status> {
    let count: i64 = connection
        .query_row(
            "SELECT COUNT(*) FROM rag_items WHERE collection='media' AND source_id=?1",
            [id],
            |r| r.get(0),
        )
        .map_err(|_| unknown())?;
    if count as usize != effect.index_items.len() {
        return Err(unknown());
    }
    for item in &effect.index_items {
        let expected = row_values(item)?;
        let row=connection.query_row("SELECT vector_id,collection,source_id,project_id,chunk_index,name,kind,scope,text,embedding,metadata,updated_at FROM rag_items WHERE item_id=?1",[&item.item_id],|r|Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,r.get::<_,String>(3)?,r.get::<_,u32>(4)?,r.get::<_,String>(5)?,r.get::<_,String>(6)?,r.get::<_,String>(7)?,r.get::<_,String>(8)?,r.get::<_,String>(9)?,r.get::<_,String>(10)?,r.get::<_,i64>(11)?))).map_err(|_|unknown())?;
        if row
            != (
                item.vector_id,
                item.collection.clone(),
                item.source_id.clone(),
                item.project_id.clone(),
                item.chunk_index,
                item.name.clone(),
                item.kind.clone(),
                item.scope.clone(),
                item.text.clone(),
                expected.0,
                expected.1,
                item.updated_at_millis,
            )
        {
            return Err(unknown());
        }
        if effect.index_backend == "sqlite_vec" {
            let row:(String,String,String,String,String,Vec<u8>)=connection.query_row("SELECT item_id,collection,source_id,project_id,scope,embedding FROM rag_vec WHERE rowid=?1",[item.vector_id],|r|Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?))).map_err(|_|unknown())?;
            if row
                != (
                    item.item_id.clone(),
                    item.collection.clone(),
                    item.source_id.clone(),
                    item.project_id.clone(),
                    item.scope.clone(),
                    expected.2,
                )
            {
                return Err(unknown());
            }
        }
    }
    Ok(())
}

fn finish_index(
    root: &Path,
    claim: &MediaPatchClaim,
    outcome: &MediaPatchOutcome,
    effect: &MediaProcessingPreparation,
) -> Result<(), Status> {
    if !effect.index_enabled {
        return Ok(());
    }
    let path = memory_index::local_rag_db(root);
    for p in [
        path.clone(),
        path.with_file_name("rag.sqlite3-wal"),
        path.with_file_name("rag.sqlite3-shm"),
        path.with_file_name("rag.sqlite3-journal"),
    ] {
        media_library::reject_links(&p).map_err(|_| unknown())?;
    }
    fs::create_dir_all(path.parent().ok_or_else(unknown)?).map_err(|_| unknown())?;
    media_library::reject_links(&path).map_err(|_| unknown())?;
    sqlite_vec_runtime::register().map_err(|_| unknown())?;
    let mut connection = Connection::open_with_flags(
        &path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
    )
    .map_err(|_| unknown())?;
    connection
        .busy_timeout(Duration::from_secs(5))
        .map_err(|_| unknown())?;
    connection
        .execute_batch("PRAGMA trusted_schema=OFF; PRAGMA synchronous=FULL;")
        .map_err(|_| unknown())?;
    schema(&connection, effect)?;
    let intent = claim.intent.as_ref().ok_or_else(unknown)?;
    let key = format!("native_media_processing:{}", intent.operation_id);
    let digest = outcome
        .created
        .as_ref()
        .ok_or_else(unknown)?
        .preparation_sha256
        .iter()
        .fold(String::with_capacity(64), |mut output, byte| {
            write!(&mut output, "{byte:02x}").expect("writing hexadecimal to String cannot fail");
            output
        });
    let transaction = connection
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|_| unknown())?;
    let retained: Option<String> = transaction
        .query_row("SELECT value FROM rag_meta WHERE key=?1", [&key], |r| {
            r.get(0)
        })
        .optional()
        .map_err(|_| unknown())?;
    if let Some(retained) = retained {
        if retained != digest {
            return Err(unknown());
        }
        confirm_index(&transaction, &intent.media_id, effect)?;
    } else {
        let existing: i64 = transaction
            .query_row(
                "SELECT COUNT(*) FROM rag_items WHERE collection='media' AND source_id=?1",
                [&intent.media_id],
                |r| r.get(0),
            )
            .map_err(|_| unknown())?;
        if existing != 0 {
            return Err(unknown());
        }
        for item in &effect.index_items {
            let (embedding, metadata, blob) = row_values(item)?;
            transaction.execute("INSERT INTO rag_items(item_id,vector_id,collection,source_id,project_id,chunk_index,name,kind,scope,text,embedding,metadata,updated_at) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13)",params![item.item_id,item.vector_id,item.collection,item.source_id,item.project_id,item.chunk_index,item.name,item.kind,item.scope,item.text,embedding,metadata,item.updated_at_millis]).map_err(|_|unknown())?;
            if effect.index_backend == "sqlite_vec" {
                transaction.execute("INSERT INTO rag_vec(rowid,item_id,collection,source_id,project_id,scope,embedding) VALUES(?1,?2,?3,?4,?5,?6,?7)",params![item.vector_id,item.item_id,item.collection,item.source_id,item.project_id,item.scope,blob]).map_err(|_|unknown())?;
            }
        }
        transaction
            .execute(
                "INSERT OR REPLACE INTO rag_meta(key,value) VALUES('embedding_dimensions',?1)",
                [effect.embedding_dimensions.to_string()],
            )
            .map_err(|_| unknown())?;
        transaction
            .execute(
                "INSERT OR REPLACE INTO rag_meta(key,value) VALUES('embedding_provider','hash')",
                [],
            )
            .map_err(|_| unknown())?;
        transaction
            .execute(
                "INSERT INTO rag_meta(key,value) VALUES(?1,?2)",
                params![key, digest],
            )
            .map_err(|_| unknown())?;
        confirm_index(&transaction, &intent.media_id, effect)?;
    }
    transaction.commit().map_err(|_| unknown())?;
    fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&path)
        .and_then(|f| f.sync_all())
        .map_err(|_| unknown())?;
    media_create::sync_directory(path.parent().ok_or_else(unknown)?)?;
    Ok(())
}

/// After a crash, only a retained processing/final publication is positive
/// evidence that this operation reached its effect phase. A still-old library
/// cannot be reinterpreted as permission to republish an uncertain effect.
pub(super) fn publish(
    root: &Path,
    staging: &Path,
    claim: &MediaPatchClaim,
    outcome: &MediaPatchOutcome,
    before: &[u8],
    after: &[u8],
    initial: bool,
) -> Result<bool, Status> {
    let prepared = media_create::retained_preparation(staging, claim, outcome)?;
    validate(&prepared, claim, outcome)?;
    let effect = prepared.processing.as_ref().ok_or_else(unknown)?;
    let id = &claim.intent.as_ref().ok_or_else(unknown)?.media_id;
    let processing = intermediate(after, effect, id)?;
    let current = media_library::read_library(root)?;
    let final_published = current == after;
    if !final_published && current != processing {
        if !initial || current != before {
            return Err(unknown());
        }
        if !media_library::publish(root, &processing, Some(claim.lease_until_unix))
            .map_err(|_| unknown())?
        {
            return Ok(false);
        }
    } else {
        media_library::confirm_publication(root, &current).map_err(|_| unknown())?;
    }
    media_create::finish(root, staging, claim, outcome)?;
    if let Some(segments) = &effect.segments {
        let json = media_values::to_json(&MediaValue {
            kind: Some(Kind::ArrayValue(MediaArray {
                values: segments.values.clone(),
            })),
        })?;
        let bytes = serde_json::to_vec_pretty(&json!({"segments":json})).map_err(|_| unknown())?;
        media_create::persist_bound(
            &root.join(".media/segments").join(format!("{id}.json")),
            &bytes,
        )
        .map_err(|_| unknown())?;
        finish_index(root, claim, outcome, effect)?;
    }
    if final_published {
        media_library::confirm_publication(root, after).map_err(|_| unknown())?;
    } else if !media_library::publish(root, after, None).map_err(|_| unknown())? {
        return Err(unknown());
    }
    Ok(true)
}
