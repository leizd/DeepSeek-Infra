//! Read-only `POST /api/rag/verify-citation` and `POST /api/rag/eval`.
//!
//! Mirrors `local_rag.verify_citation` and `local_rag.evaluate_recall` directly
//! over `.local-rag/rag.sqlite3` via `deepseek_policy::memory_index` and
//! `deepseek_rag::citation::format_citation` without starting a Python process
//! or creating `.local-rag/`.

use std::collections::HashSet;

use axum::Json;
use axum::Router;
use axum::body::Bytes;
use axum::http::{HeaderMap, Method};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::{python_truthy, query_tokens};
use deepseek_policy::memory_index::{MemoryIndex, local_rag_db, python_normalize_query};
use deepseek_policy::python_json::value_str;
use deepseek_rag::citation::format_citation;
use rusqlite::OptionalExtension;
use serde_json::{Value, json};

use crate::backup_catalog_routes::open_readonly_sqlite;
use crate::route_support;
use crate::scheduler_routes::env_bool;

const MAX_JSON_BODY_BYTES: i64 = 2_000_000;

pub fn router() -> Router {
    Router::new()
        .route("/api/rag/verify-citation", any(dispatch_verify_citation))
        .route("/api/rag/eval", any(dispatch_eval))
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
