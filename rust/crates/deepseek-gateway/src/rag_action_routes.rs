//! `POST /api/rag/reindex`, `POST /api/rag/verify-citation`, and `POST /api/rag/eval`.
//!
//! Mirrors `local_rag.rebuild_index`, `local_rag.verify_citation`, and
//! `local_rag.evaluate_recall` directly over `.local-rag/rag.sqlite3` via
//! `deepseek_policy::memory_index`, `deepseek_policy::sqlite_vec_runtime`, and
//! `deepseek_rag::citation::format_citation` without starting a Python process.

use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::Router;
use axum::body::Bytes;
use axum::http::{HeaderMap, Method};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use blake2::Blake2bVar;
use blake2::digest::{Update, VariableOutput};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::attachment_context::hash_text_embedding;
use deepseek_policy::core_utils::{python_float_opt, python_int_opt, python_truthy, query_tokens};
use deepseek_policy::memory_index::{
    COLLECTION_FILES, COLLECTION_MEDIA, COLLECTION_MEMORY, ITEM_TABLE,
    LOCAL_RAG_EMBEDDING_DIMENSIONS, META_TABLE, MemoryIndex, VECTOR_TABLE, local_rag_db,
    local_rag_dir, local_rag_status, python_normalize_query, vector_blob,
};
use deepseek_policy::python_json::value_str;
use deepseek_policy::sqlite_vec_runtime;
use deepseek_rag::citation::format_citation;
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::{Map, Value, json};

use crate::backup_catalog_routes::open_readonly_sqlite;
use crate::media_segment_routes;
use crate::route_support;
use crate::scheduler_routes::env_bool;

const MAX_JSON_BODY_BYTES: i64 = 2_000_000;
const MAX_RAG_TEXT_CHARS: usize = 12_000;

static REINDEX_LOCK: Mutex<()> = Mutex::new(());

pub fn router() -> Router {
    Router::new()
        .route("/api/rag/reindex", any(dispatch_reindex))
        .route("/api/rag/verify-citation", any(dispatch_verify_citation))
        .route("/api/rag/eval", any(dispatch_eval))
}

async fn dispatch_reindex(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_JSON_BODY_BYTES) {
        Ok(object) => Value::Object(object),
        Err(error) => return route_support::app_error_response(error),
    };
    let joined = tokio::task::spawn_blocking(move || reindex_payload(&payload)).await;
    match joined {
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn dispatch_verify_citation(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_JSON_BODY_BYTES) {
        Ok(object) => Value::Object(object),
        Err(error) => return route_support::app_error_response(error),
    };
    let joined = tokio::task::spawn_blocking(move || verify_citation_payload(&payload)).await;
    match joined {
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

async fn dispatch_eval(method: Method, headers: HeaderMap, body: Bytes) -> Response {
    if method != Method::POST {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let payload = match route_support::read_json_object(&headers, &body, MAX_JSON_BODY_BYTES) {
        Ok(object) => Value::Object(object),
        Err(error) => return route_support::app_error_response(error),
    };
    let joined = tokio::task::spawn_blocking(move || eval_recall_payload(&payload)).await;
    match joined {
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) => route_support::app_error_response(error),
        Err(_) => route_support::app_error_response(route_support::unhandled_error()),
    }
}

fn truthy_str(value: Option<&Value>) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => String::new(),
    }
}

fn verify_citation_payload(payload: &Value) -> Result<Value, AppError> {
    let item_id = truthy_str(payload.get("itemId")).trim().to_string();
    if item_id.is_empty() {
        return Err(AppError {
            message: "itemId is required".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let snippet = truthy_str(payload.get("snippet"));
    if !env_bool("LOCAL_RAG_ENABLED", true) {
        return Ok(json!({
            "ok": true,
            "citation": {
                "grounded": false,
                "itemId": item_id,
                "coverage": 0.0,
                "reason": "chunk_not_found",
            }
        }));
    }
    let root = crate::data_routes::workspace_root();
    let db = local_rag_db(&root);
    let Some(conn) = open_readonly_sqlite(&db)? else {
        return Ok(json!({
            "ok": true,
            "citation": {
                "grounded": false,
                "itemId": item_id,
                "coverage": 0.0,
                "reason": "chunk_not_found",
            }
        }));
    };

    let row_opt = conn
        .query_row(
            "SELECT item_id, source_id, project_id, name, text, metadata \
             FROM rag_items WHERE item_id = ?",
            [&item_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, String>(5)?,
                ))
            },
        )
        .optional()
        .unwrap_or(None);

    let Some((row_item_id, source_id, project_id, name, row_text, metadata_raw)) = row_opt else {
        return Ok(json!({
            "ok": true,
            "citation": {
                "grounded": false,
                "itemId": item_id,
                "coverage": 0.0,
                "reason": "chunk_not_found",
            }
        }));
    };

    let metadata: Value = serde_json::from_str(&metadata_raw).unwrap_or_else(|_| json!({}));
    let line_start = metadata.get("lineStart").and_then(Value::as_i64);
    let line_end = metadata.get("lineEnd").and_then(Value::as_i64);
    let source = if !source_id.is_empty() {
        source_id.as_str()
    } else if !name.is_empty() {
        name.as_str()
    } else {
        ""
    };

    let formatted_via_crate = match (
        line_start.and_then(|val| u32::try_from(val).ok()),
        line_end.and_then(|val| u32::try_from(val).ok()),
    ) {
        (Some(start), Some(end)) => format_citation(source, Some(start), Some(end)).ok(),
        (Some(start), None) if line_end.is_none() => {
            format_citation(source, Some(start), None).ok()
        }
        (None, _) if line_start.is_none() => format_citation(source, None, None).ok(),
        _ => None,
    };
    let citation = formatted_via_crate.unwrap_or_else(|| {
        let mut parts = vec![source.to_string()];
        if let (Some(start), Some(end)) = (line_start, line_end) {
            if end >= start {
                parts.push(format!("L{start}-L{end}"));
            }
        } else if let Some(start) = line_start {
            parts.push(format!("L{start}"));
        }
        parts.join(":")
    });

    let lineage = json!({
        "chunkId": row_item_id,
        "docId": source_id,
        "projectId": project_id,
        "page": metadata.get("page").and_then(Value::as_i64).unwrap_or(0),
        "startChar": metadata.get("start").and_then(Value::as_i64).unwrap_or(0),
        "endChar": metadata.get("end").and_then(Value::as_i64).unwrap_or(0),
        "hash": truthy_str(metadata.get("hash")),
        "docVersion": truthy_str(metadata.get("docVersion")),
        "citation": citation,
    });

    let snippet_norm = python_normalize_query(&snippet);
    if snippet_norm.is_empty() {
        return Ok(json!({
            "ok": true,
            "citation": {
                "grounded": false,
                "itemId": item_id,
                "coverage": 0.0,
                "reason": "empty_snippet",
                "lineage": lineage,
            }
        }));
    }
    let chunk_norm = python_normalize_query(&row_text);
    if chunk_norm.contains(&snippet_norm) {
        return Ok(json!({
            "ok": true,
            "citation": {
                "grounded": true,
                "itemId": item_id,
                "coverage": 1.0,
                "reason": "",
                "lineage": lineage,
            }
        }));
    }

    let snippet_terms = query_tokens(&snippet);
    let chunk_terms: HashSet<String> = query_tokens(&row_text).into_iter().collect();
    let coverage = if snippet_terms.is_empty() {
        0.0
    } else {
        let matched = snippet_terms
            .iter()
            .filter(|term| chunk_terms.contains(*term))
            .count();
        (matched as f64) / (snippet_terms.len() as f64)
    };
    let grounded = coverage >= 0.8;
    let rounded = (coverage * 1000.0).round() / 1000.0;
    Ok(json!({
        "ok": true,
        "citation": {
            "grounded": grounded,
            "itemId": item_id,
            "coverage": rounded,
            "reason": if grounded { "" } else { "snippet_not_grounded" },
            "lineage": lineage,
        }
    }))
}

fn eval_recall_payload(payload: &Value) -> Result<Value, AppError> {
    let Some(cases) = payload.get("cases").and_then(Value::as_array) else {
        return Err(AppError {
            message: "cases must be a list".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    };
    let k_value = match payload.get("k") {
        Some(Value::Number(num)) if num.is_i64() || num.is_u64() => {
            num.as_i64().filter(|val| *val > 0).unwrap_or(5) as usize
        }
        Some(Value::Bool(true)) => 1,
        _ => 5,
    };

    let enabled = env_bool("LOCAL_RAG_ENABLED", true);
    let root = crate::data_routes::workspace_root();
    let index = if enabled {
        MemoryIndex::open(&root)
    } else {
        None
    };

    let mut details = Vec::new();
    let mut hits = 0usize;
    let mut reciprocal = 0.0f64;
    let mut evaluated = 0usize;

    for case in cases {
        let Some(case_obj) = case.as_object() else {
            continue;
        };
        let query = truthy_str(case_obj.get("query")).trim().to_string();
        let raw_relevant = case_obj
            .get("relevant")
            .filter(|val| python_truthy(val))
            .or_else(|| {
                case_obj
                    .get("relevantSourceIds")
                    .filter(|val| python_truthy(val))
            });
        let relevant: HashSet<String> = match raw_relevant {
            Some(Value::Array(items)) => items
                .iter()
                .map(value_str)
                .filter(|item| !item.is_empty())
                .collect(),
            _ => HashSet::new(),
        };
        if query.is_empty() || relevant.is_empty() {
            continue;
        }
        evaluated += 1;
        let results = match &index {
            Some(opened) => opened
                .search_files(&query, k_value)
                .map_err(|error| AppError {
                    message: error.to_string(),
                    code: codes::INTERNAL,
                    status: 500,
                })?,
            None => Vec::new(),
        };
        let mut rank_hit = 0usize;
        let mut top_sources = Vec::new();
        for (idx, hit) in results.iter().take(k_value).enumerate() {
            if rank_hit == 0
                && (relevant.contains(&hit.source_id) || relevant.contains(&hit.item_id))
            {
                rank_hit = idx + 1;
            }
            top_sources.push(hit.source_id.clone());
        }
        if rank_hit > 0 {
            hits += 1;
            reciprocal += 1.0 / (rank_hit as f64);
        }
        details.push(json!({
            "query": query,
            "hit": rank_hit > 0,
            "rank": rank_hit,
            "topSourceIds": top_sources,
        }));
    }

    let round4 = |val: f64| (val * 10_000.0).round() / 10_000.0;
    let recall_at_k = if evaluated > 0 {
        round4((hits as f64) / (evaluated as f64))
    } else {
        0.0
    };
    let mrr = if evaluated > 0 {
        round4(reciprocal / (evaluated as f64))
    } else {
        0.0
    };

    Ok(json!({
        "ok": true,
        "eval": {
            "k": k_value,
            "cases": evaluated,
            "recallAtK": recall_at_k,
            "mrr": mrr,
            "details": details,
        }
    }))
}

fn hex_bytes(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        use std::fmt::Write;
        let _ = write!(out, "{b:02x}");
    }
    out
}

fn blake2b_hex(data: &[u8], len: usize) -> String {
    let mut hasher = Blake2bVar::new(len).expect("valid blake2b digest size");
    hasher.update(data);
    let mut buf = vec![0u8; len];
    hasher
        .finalize_variable(&mut buf)
        .expect("buffer matches digest size");
    hex_bytes(&buf)
}

fn stable_vector_id(item_id: &str) -> i64 {
    let mut hasher = Blake2bVar::new(8).expect("8 is valid blake2b digest size");
    hasher.update(item_id.as_bytes());
    let mut buf = [0u8; 8];
    hasher
        .finalize_variable(&mut buf)
        .expect("buffer matches 8 bytes");
    (i64::from_be_bytes(buf)) & 0x7FFF_FFFF_FFFF_FFFF
}

fn chunk_hash(text: &str) -> String {
    blake2b_hex(text.trim().as_bytes(), 12)
}

fn doc_version(hashes: &BTreeMap<i64, String>) -> String {
    let mut hasher = Blake2bVar::new(12).expect("12 is valid blake2b digest size");
    for (idx, h) in hashes {
        hasher.update(format!("{idx}:{h}\0").as_bytes());
    }
    let mut buf = [0u8; 12];
    hasher
        .finalize_variable(&mut buf)
        .expect("buffer matches 12 bytes");
    hex_bytes(&buf)
}

fn now_unix_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| i64::try_from(d.as_millis()).unwrap_or(i64::MAX))
        .unwrap_or(0)
}

struct PreparedRagItem {
    item_id: String,
    collection: String,
    source_id: String,
    project_id: String,
    chunk_index: i64,
    name: String,
    kind: String,
    scope: String,
    text: String,
    metadata: Value,
}

fn iter_cached_file_paths(root: &Path) -> Vec<(PathBuf, String)> {
    let mut out = Vec::new();
    let file_cache = root.join(".file-cache");
    if file_cache.is_dir() {
        if let Ok(entries) = fs::read_dir(&file_cache) {
            let mut paths: Vec<PathBuf> = entries
                .flatten()
                .map(|e| e.path())
                .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("json"))
                .collect();
            paths.sort();
            for p in paths {
                out.push((p, String::new()));
            }
        }
    }
    let projects_dir = root.join(".projects");
    if projects_dir.is_dir() {
        if let Ok(proj_entries) = fs::read_dir(&projects_dir) {
            let mut projs: Vec<PathBuf> = proj_entries
                .flatten()
                .map(|e| e.path())
                .filter(|p| p.is_dir())
                .collect();
            projs.sort();
            for proj in projs {
                let project_id = proj
                    .file_name()
                    .and_then(|s| s.to_str())
                    .unwrap_or_default()
                    .to_string();
                let files_dir = proj.join("files");
                if files_dir.is_dir() {
                    if let Ok(entries) = fs::read_dir(&files_dir) {
                        let mut paths: Vec<PathBuf> = entries
                            .flatten()
                            .map(|e| e.path())
                            .filter(|p| p.extension().and_then(|s| s.to_str()) == Some("json"))
                            .collect();
                        paths.sort();
                        for p in paths {
                            out.push((p, project_id.clone()));
                        }
                    }
                }
            }
        }
    }
    out
}

fn read_json_object_file(path: &Path) -> Option<Map<String, Value>> {
    let raw = fs::read_to_string(path).ok()?;
    let val = serde_json::from_str::<Value>(&raw).ok()?;
    val.as_object().cloned()
}

fn read_json_object_list_file(path: &Path) -> Vec<Map<String, Value>> {
    let Ok(raw) = fs::read_to_string(path) else {
        return Vec::new();
    };
    let Ok(Value::Array(items)) = serde_json::from_str::<Value>(&raw) else {
        return Vec::new();
    };
    items
        .into_iter()
        .filter_map(|v| v.as_object().cloned())
        .collect()
}

fn prepare_file_items(cached: &Map<String, Value>, project_id: &str) -> Vec<PreparedRagItem> {
    let file_id = truthy_str(cached.get("id")).trim().to_string();
    if file_id.is_empty() {
        return Vec::new();
    }
    let Some(Value::Array(raw_chunks)) = cached.get("chunks") else {
        return Vec::new();
    };
    let mut prepared = Vec::new();
    let mut hash_by_index = BTreeMap::new();
    for (fallback_idx, raw_chunk) in raw_chunks.iter().enumerate() {
        let Some(chunk) = raw_chunk.as_object() else {
            continue;
        };
        let text = truthy_str(chunk.get("text")).trim().to_string();
        if text.is_empty() {
            continue;
        }
        let chunk_index = chunk
            .get("index")
            .and_then(|v| python_int_opt(Some(v)))
            .unwrap_or_else(|| i64::try_from(fallback_idx).unwrap_or(i64::MAX));
        let h = chunk_hash(&text);
        hash_by_index.insert(chunk_index, h.clone());
        prepared.push((chunk_index, text, chunk, h));
    }
    if prepared.is_empty() {
        return Vec::new();
    }
    let new_ver = doc_version(&hash_by_index);
    let name_str = truthy_str(cached.get("name"));
    let name = if name_str.is_empty() {
        file_id.clone()
    } else {
        name_str
    };
    let kind_str = truthy_str(cached.get("kind"));
    let kind = if kind_str.is_empty() {
        "text".to_string()
    } else {
        kind_str
    };
    let proj_seg = if project_id.is_empty() {
        "_"
    } else {
        project_id
    };
    let mut out = Vec::with_capacity(prepared.len());
    for (chunk_index, text, chunk, content_hash) in prepared {
        out.push(PreparedRagItem {
            item_id: format!("file:{proj_seg}:{file_id}:{chunk_index}"),
            collection: COLLECTION_FILES.to_string(),
            source_id: file_id.clone(),
            project_id: project_id.to_string(),
            chunk_index,
            name: name.clone(),
            kind: kind.clone(),
            scope: String::new(),
            text,
            metadata: json!({
                "lineStart": python_int_opt(chunk.get("lineStart")).unwrap_or(0),
                "lineEnd": python_int_opt(chunk.get("lineEnd")).unwrap_or(0),
                "start": python_int_opt(chunk.get("start")).unwrap_or(0),
                "end": python_int_opt(chunk.get("end")).unwrap_or(0),
                "page": python_int_opt(chunk.get("page")).unwrap_or(0),
                "hash": content_hash,
                "docVersion": new_ver,
            }),
        });
    }
    out
}

fn prepare_memory_items(memories: &[Map<String, Value>]) -> Vec<PreparedRagItem> {
    let mut out = Vec::new();
    for item in memories {
        let content = truthy_str(item.get("content")).trim().to_string();
        let memory_id = truthy_str(item.get("id")).trim().to_string();
        if content.is_empty() || memory_id.is_empty() {
            continue;
        }
        let raw_scope = truthy_str(item.get("scope"));
        let scope = if raw_scope.is_empty() {
            "global".to_string()
        } else {
            raw_scope
        };
        let raw_cat = truthy_str(item.get("category"));
        let category = if raw_cat.is_empty() {
            "fact".to_string()
        } else {
            raw_cat.clone()
        };
        let name = if raw_cat.is_empty() {
            "memory".to_string()
        } else {
            raw_cat
        };
        out.push(PreparedRagItem {
            item_id: format!("memory:{memory_id}"),
            collection: COLLECTION_MEMORY.to_string(),
            source_id: memory_id.clone(),
            project_id: String::new(),
            chunk_index: 0,
            name,
            kind: "memory".to_string(),
            scope,
            text: content,
            metadata: json!({
                "id": memory_id,
                "category": category,
                "source": truthy_str(item.get("source")),
                "pinned": item.get("pinned").is_some_and(python_truthy),
                "createdAt": truthy_str(item.get("createdAt")),
                "updatedAt": truthy_str(item.get("updatedAt")),
            }),
        });
    }
    out
}

fn fallback_media_citation(
    media: &Map<String, Value>,
    seg: &Map<String, Value>,
    ordinal: usize,
) -> (String, String) {
    let media_id = truthy_str(media.get("mediaId"));
    let media_type = truthy_str(media.get("type"));
    let seg_id = truthy_str(seg.get("segmentId"));
    let page = python_int_opt(seg.get("page")).unwrap_or(0);
    let time_range = seg
        .get("timeRange")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let locator = if page != 0 {
        format!("page={page}")
    } else if time_range.len() >= 2 {
        let start = python_float_opt(&time_range[0]).unwrap_or(0.0);
        format!("t={start:.3}")
    } else if !seg_id.is_empty() {
        format!("segment={seg_id}")
    } else {
        String::new()
    };
    let uri = if locator.is_empty() {
        format!("media://{media_id}")
    } else {
        format!("media://{media_id}#{locator}")
    };
    let prefix = if media_type == "webpage" { "W" } else { "M" };
    let base = format!("{prefix}{}", ordinal.max(1));
    let label = if page != 0 {
        format!("{base}-p{page}")
    } else if time_range.len() >= 2 {
        let secs = python_float_opt(&time_range[0]).unwrap_or(0.0).max(0.0) as u64;
        let h = secs / 3600;
        let m = (secs % 3600) / 60;
        let s = secs % 60;
        format!("{base}-{h:02}:{m:02}:{s:02}")
    } else {
        base
    };
    (uri, label)
}

fn prepare_media_items(media: &Value, segments: &[Value]) -> Vec<PreparedRagItem> {
    let Some(media_obj) = media.as_object() else {
        return Vec::new();
    };
    let media_id = truthy_str(media_obj.get("mediaId"));
    let project_id = truthy_str(media_obj.get("projectId"));
    let title = truthy_str(media_obj.get("title"));
    let name = if title.is_empty() {
        media_id.clone()
    } else {
        title
    };
    let raw_kind = truthy_str(media_obj.get("type"));
    let kind = if raw_kind.is_empty() {
        "media".to_string()
    } else {
        raw_kind
    };
    let mut out = Vec::new();
    for (idx, raw_seg) in segments.iter().enumerate() {
        let Some(seg) = raw_seg.as_object() else {
            continue;
        };
        let text = truthy_str(seg.get("text")).trim().to_string();
        let seg_id = truthy_str(seg.get("segmentId"));
        if text.is_empty() || seg_id.is_empty() {
            continue;
        }
        let (fallback_uri, fallback_label) = fallback_media_citation(media_obj, seg, idx + 1);
        let cit_obj = seg.get("citation").and_then(Value::as_object);
        let cit_uri = cit_obj
            .and_then(|c| c.get("uri"))
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or(fallback_uri);
        let cit_label = cit_obj
            .and_then(|c| c.get("label"))
            .filter(|v| python_truthy(v))
            .map(value_str)
            .unwrap_or(fallback_label);
        let time_range = seg
            .get("timeRange")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        out.push(PreparedRagItem {
            item_id: format!("media:{media_id}:{seg_id}:{idx}"),
            collection: COLLECTION_MEDIA.to_string(),
            source_id: media_id.clone(),
            project_id: project_id.clone(),
            chunk_index: i64::try_from(idx).unwrap_or(i64::MAX),
            name: name.clone(),
            kind: kind.clone(),
            scope: String::new(),
            metadata: json!({
                "sourceType": "media",
                "mediaId": media_id,
                "segmentId": seg_id,
                "segmentType": truthy_str(seg.get("type")),
                "page": python_int_opt(seg.get("page")).unwrap_or(0),
                "timeRange": Value::Array(time_range),
                "framePath": truthy_str(seg.get("framePath")),
                "citation": cit_uri,
                "citationLabel": cit_label,
                "hash": chunk_hash(&text),
            }),
            text,
        });
    }
    out
}

fn initialize_rag_schema(conn: &Connection) -> Result<bool, rusqlite::Error> {
    conn.execute_batch(&format!(
        "CREATE TABLE IF NOT EXISTS {META_TABLE} (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {ITEM_TABLE} (
            item_id TEXT PRIMARY KEY,
            vector_id INTEGER NOT NULL UNIQUE,
            collection TEXT NOT NULL,
            source_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            text TEXT NOT NULL,
            embedding TEXT NOT NULL,
            metadata TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_{ITEM_TABLE}_collection ON {ITEM_TABLE}(collection);
        CREATE INDEX IF NOT EXISTS idx_{ITEM_TABLE}_source ON {ITEM_TABLE}(collection, source_id, project_id);
        CREATE INDEX IF NOT EXISTS idx_{ITEM_TABLE}_scope ON {ITEM_TABLE}(collection, scope);"
    ))?;

    let configured_dim = LOCAL_RAG_EMBEDDING_DIMENSIONS.to_string();
    let existing_dim: Option<String> = conn
        .query_row(
            &format!("SELECT value FROM {META_TABLE} WHERE key = 'embedding_dimensions'"),
            [],
            |r| r.get(0),
        )
        .optional()?;
    if let Some(dim) = existing_dim {
        if dim != configured_dim {
            conn.execute(&format!("DELETE FROM {ITEM_TABLE}"), [])?;
            conn.execute(&format!("DROP TABLE IF EXISTS {VECTOR_TABLE}"), [])?;
        }
    }
    conn.execute(
        &format!(
            "INSERT OR REPLACE INTO {META_TABLE}(key, value) VALUES ('embedding_dimensions', ?1)"
        ),
        [&configured_dim],
    )?;
    conn.execute(
        &format!(
            "INSERT OR REPLACE INTO {META_TABLE}(key, value) VALUES ('embedding_provider', 'hash')"
        ),
        [],
    )?;

    let vec_sql = format!(
        "CREATE VIRTUAL TABLE IF NOT EXISTS {VECTOR_TABLE} USING vec0(
            item_id TEXT,
            collection TEXT,
            source_id TEXT,
            project_id TEXT,
            scope TEXT,
            embedding float[{LOCAL_RAG_EMBEDDING_DIMENSIONS}]
        )"
    );
    Ok(conn.execute_batch(&vec_sql).is_ok())
}

fn upsert_rag_items(
    tx: &rusqlite::Transaction<'_>,
    items: &[PreparedRagItem],
    vector_table_ready: bool,
    updated_at: i64,
) -> Result<usize, rusqlite::Error> {
    if items.is_empty() {
        return Ok(0);
    }
    let item_sql = format!(
        "INSERT INTO {ITEM_TABLE}
        (item_id, vector_id, collection, source_id, project_id, chunk_index, name, kind, scope, text, embedding, metadata, updated_at)
        VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)
        ON CONFLICT(item_id) DO UPDATE SET
            vector_id=excluded.vector_id,
            collection=excluded.collection,
            source_id=excluded.source_id,
            project_id=excluded.project_id,
            chunk_index=excluded.chunk_index,
            name=excluded.name,
            kind=excluded.kind,
            scope=excluded.scope,
            text=excluded.text,
            embedding=excluded.embedding,
            metadata=excluded.metadata,
            updated_at=excluded.updated_at"
    );
    let del_vec_sql = format!("DELETE FROM {VECTOR_TABLE} WHERE rowid = ?1");
    let ins_vec_sql = format!(
        "INSERT INTO {VECTOR_TABLE}(rowid, item_id, collection, source_id, project_id, scope, embedding)
        VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)"
    );
    let mut item_stmt = tx.prepare(&item_sql)?;
    for item in items {
        let truncated: String = item.text.chars().take(MAX_RAG_TEXT_CHARS).collect();
        let vec = hash_text_embedding(&truncated, LOCAL_RAG_EMBEDDING_DIMENSIONS);
        let emb_json = serde_json::to_string(&vec).map_err(|_| rusqlite::Error::InvalidQuery)?;
        let meta_json =
            serde_json::to_string(&item.metadata).map_err(|_| rusqlite::Error::InvalidQuery)?;
        let vid = stable_vector_id(&item.item_id);
        item_stmt.execute(params![
            item.item_id,
            vid,
            item.collection,
            item.source_id,
            item.project_id,
            item.chunk_index,
            item.name,
            item.kind,
            item.scope,
            truncated,
            emb_json,
            meta_json,
            updated_at,
        ])?;
        if vector_table_ready {
            let blob = vector_blob(&vec);
            let _ = tx.execute(&del_vec_sql, [vid]);
            let _ = tx.execute(
                &ins_vec_sql,
                params![
                    vid,
                    item.item_id,
                    item.collection,
                    item.source_id,
                    item.project_id,
                    item.scope,
                    blob
                ],
            );
        }
    }
    Ok(items.len())
}

fn reindex_payload(payload: &Value) -> Result<Value, AppError> {
    let raw_action = truthy_str(payload.get("action"))
        .trim()
        .to_ascii_lowercase();
    let action = if raw_action.is_empty() {
        "reindex".to_string()
    } else {
        raw_action
    };
    if action != "reindex" && action != "rebuild" {
        return Err(AppError::invalid_payload("Unsupported RAG action"));
    }

    let _guard = REINDEX_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let root = crate::data_routes::workspace_root();
    let enabled = env_bool("LOCAL_RAG_ENABLED", true);

    let file_paths = iter_cached_file_paths(&root);
    let mut files_count = 0usize;
    let mut file_items = Vec::new();
    for (path, project_id) in file_paths {
        let Some(cached) = read_json_object_file(&path) else {
            continue;
        };
        files_count += 1;
        if enabled {
            file_items.extend(prepare_file_items(&cached, &project_id));
        }
    }

    let memories = if enabled {
        read_json_object_list_file(&root.join(".memory").join("memories.json"))
    } else {
        Vec::new()
    };
    let memory_items = prepare_memory_items(&memories);

    let ready_media = media_segment_routes::ready_media_with_segments()?;
    let media_count = ready_media.len();
    let mut media_items = Vec::new();
    if enabled {
        for (media, segments) in &ready_media {
            media_items.extend(prepare_media_items(media, segments));
        }
    }

    let (chunks_count, memories_count, media_chunks) = if !enabled {
        (0usize, 0usize, 0usize)
    } else {
        let dir = local_rag_dir(&root);
        fs::create_dir_all(&dir).map_err(|_| route_support::unhandled_error())?;
        let _ = sqlite_vec_runtime::register();
        let db = local_rag_db(&root);
        let mut conn = Connection::open(&db).map_err(|e| AppError {
            message: e.to_string(),
            code: codes::INTERNAL,
            status: 500,
        })?;
        let _ = conn.busy_timeout(Duration::from_secs(5));
        let vector_ready = initialize_rag_schema(&conn).map_err(|e| AppError {
            message: e.to_string(),
            code: codes::INTERNAL,
            status: 500,
        })?;

        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| AppError {
                message: e.to_string(),
                code: codes::INTERNAL,
                status: 500,
            })?;
        if vector_ready {
            let rowids: Vec<i64> = {
                let mut stmt = tx
                    .prepare(&format!(
                        "SELECT vector_id FROM {ITEM_TABLE} WHERE collection IN ('files','media','memory')"
                    ))
                    .map_err(|e| AppError {
                        message: e.to_string(),
                        code: codes::INTERNAL,
                        status: 500,
                    })?;
                let mut out = Vec::new();
                let mut rows = stmt.query([]).map_err(|e| AppError {
                    message: e.to_string(),
                    code: codes::INTERNAL,
                    status: 500,
                })?;
                while let Ok(Some(row)) = rows.next() {
                    if let Ok(id) = row.get::<_, i64>(0) {
                        out.push(id);
                    }
                }
                out
            };
            for vid in rowids {
                let _ = tx.execute(
                    &format!("DELETE FROM {VECTOR_TABLE} WHERE rowid = ?1"),
                    [vid],
                );
            }
        }
        tx.execute(
            &format!("DELETE FROM {ITEM_TABLE} WHERE collection IN ('files','media','memory')"),
            [],
        )
        .map_err(|e| AppError {
            message: e.to_string(),
            code: codes::INTERNAL,
            status: 500,
        })?;

        let now_ms = now_unix_ms();
        let c = upsert_rag_items(&tx, &file_items, vector_ready, now_ms).map_err(|e| AppError {
            message: e.to_string(),
            code: codes::INTERNAL,
            status: 500,
        })?;
        let m =
            upsert_rag_items(&tx, &memory_items, vector_ready, now_ms).map_err(|e| AppError {
                message: e.to_string(),
                code: codes::INTERNAL,
                status: 500,
            })?;
        let mc =
            upsert_rag_items(&tx, &media_items, vector_ready, now_ms).map_err(|e| AppError {
                message: e.to_string(),
                code: codes::INTERNAL,
                status: 500,
            })?;
        tx.commit().map_err(|e| AppError {
            message: e.to_string(),
            code: codes::INTERNAL,
            status: 500,
        })?;
        (c, m, mc)
    };

    Ok(json!({
        "ok": true,
        "files": files_count,
        "chunks": chunks_count,
        "media": media_count,
        "mediaChunks": media_chunks,
        "memories": memories_count,
        "localRag": local_rag_status(&root),
    }))
}
