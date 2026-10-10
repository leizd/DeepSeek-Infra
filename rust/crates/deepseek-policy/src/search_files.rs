//! `search_files` — the local file-index branch of `execute_tool_call`.
//!
//! Mirrors `tools.search_files` in `infra/tool_runtime/tools.py`. Two retrieval
//! paths, merged by `(fileId, projectId, chunkIndex)` keeping the higher score:
//!
//! 1. **json_hybrid** — walk `<root>/.file-cache/*.json` and
//!    `<root>/.projects/*/files/*.json`, score each chunk with `score_chunk` and
//!    cosine over the hash embedding. This path is complete and needs no sqlite.
//! 2. **local_rag** — read-only `search_files_index` over `.local-rag/rag.sqlite3`
//!    collection `files`, the same cosine+BM25 path [`crate::memory_index`] uses
//!    for memories. **This module never writes the RAG database.** The oracle
//!    calls `index_file_payload` first (a write); until a files-index domain is
//!    cut over, Python remains the writer and a native search that indexed would
//!    be a second writer. json_hybrid still finds anything sitting in the cache
//!    JSON, which is the source `index_file_payload` itself reads.
//!
//! A missing sqlite file degrades to json_hybrid only, matching the oracle's
//! `search` `except Exception: return []` when the database is absent *and* this
//! process refuses to create it.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};

use serde_json::{Map, Value, json};

use crate::app_error::{AppError, codes};
use crate::attachment_context::{
    LOCAL_RAG_EMBEDDING_DIMENSIONS, cosine_similarity, hash_text_embedding,
};
use crate::core_utils::{python_int_opt, python_truthy, query_tokens, score_chunk, text_or_empty};
use crate::memory_index::{MemoryIndex, python_round};
use crate::python_json::value_str;

/// Default `limit` the dispatcher applies (`safe_limit(..., default=5, maximum=10)`).
pub const SEARCH_FILES_DEFAULT_LIMIT: i64 = 5;
/// `compact_snippet` default character budget.
pub const SNIPPET_LIMIT: usize = 700;

/// `<root>/.file-cache`.
pub fn file_cache_dir(root: &Path) -> PathBuf {
    root.join(".file-cache")
}

/// `<root>/.projects`.
pub fn projects_dir(root: &Path) -> PathBuf {
    root.join(".projects")
}

/// Mirrors `iter_cached_file_paths`.
pub fn iter_cached_file_paths(root: &Path) -> Vec<(PathBuf, String)> {
    let mut paths = Vec::new();
    let cache = file_cache_dir(root);
    if cache.is_dir() {
        if let Ok(entries) = std::fs::read_dir(&cache) {
            for entry in entries.filter_map(Result::ok) {
                let path = entry.path();
                if path.extension().and_then(|ext| ext.to_str()) == Some("json") {
                    paths.push((path, String::new()));
                }
            }
        }
    }
    let projects = projects_dir(root);
    if projects.is_dir() {
        if let Ok(project_entries) = std::fs::read_dir(&projects) {
            for project in project_entries.filter_map(Result::ok) {
                let files = project.path().join("files");
                if !files.is_dir() {
                    continue;
                }
                let project_id = project.file_name().to_string_lossy().into_owned();
                if let Ok(entries) = std::fs::read_dir(&files) {
                    for entry in entries.filter_map(Result::ok) {
                        let path = entry.path();
                        if path.extension().and_then(|ext| ext.to_str()) == Some("json") {
                            paths.push((path, project_id.clone()));
                        }
                    }
                }
            }
        }
    }
    paths
}

/// Mirrors `read_cached_file`.
pub fn read_cached_file(path: &Path) -> Option<Map<String, Value>> {
    let raw = std::fs::read_to_string(path).ok()?;
    let value: Value = serde_json::from_str(&raw).ok()?;
    value.as_object().cloned()
}

/// Mirrors `compact_snippet`. Lengths are code points, matching `len(str)`.
pub fn compact_snippet(text: &str, query: &str, limit: usize) -> String {
    let collapsed = regex::Regex::new(r"\s+")
        .expect("static regex")
        .replace_all(text, " ");
    let value = collapsed.trim().to_string();
    let chars: Vec<char> = value.chars().collect();
    if chars.len() <= limit {
        return value;
    }
    let tokens = query_tokens(query);
    let lowered: Vec<char> = value.to_lowercase().chars().collect();
    let mut positions: Vec<usize> = Vec::new();
    for token in &tokens {
        if token.is_empty() {
            continue;
        }
        let needle: Vec<char> = token.chars().collect();
        if let Some(found) = lowered
            .windows(needle.len())
            .position(|window| window == needle.as_slice())
        {
            positions.push(found);
        }
    }
    let center = positions.into_iter().min().unwrap_or(0);
    let start = center.saturating_sub(limit / 3);
    let end = (start + limit).min(chars.len());
    chars[start..end]
        .iter()
        .collect::<String>()
        .trim()
        .to_string()
}

/// `int(value or 0)` for the fields this branch reads. A truthy unparseable
/// value is treated as 0 rather than failing the whole search — the oracle would
/// raise `ValueError` and `execute_tool_call` would map it to `internal`; the
/// cache writer never emits that shape.
fn int_or_zero(value: Option<&Value>) -> i64 {
    match value {
        Some(found) if python_truthy(found) => python_int_opt(Some(found)).unwrap_or(0),
        _ => 0,
    }
}

fn round_4(value: f64) -> f64 {
    python_round(value * 10_000.0) / 10_000.0
}

fn cosine_f64(left: &[f64], right: &[f64]) -> f64 {
    let left_values: Vec<Value> = left.iter().copied().map(|item| json!(item)).collect();
    let right_values: Vec<Value> = right.iter().copied().map(|item| json!(item)).collect();
    cosine_similarity(&left_values, &right_values)
}

fn vector_from_chunk(chunk: &Map<String, Value>, text: &str) -> Vec<f64> {
    match chunk.get("vector") {
        Some(Value::Array(items)) => {
            let numbers: Vec<f64> = items
                .iter()
                .filter_map(|item| {
                    item.as_f64()
                        .or_else(|| item.as_i64().map(|value| value as f64))
                })
                .collect();
            if numbers.is_empty() {
                hash_text_embedding(text, LOCAL_RAG_EMBEDDING_DIMENSIONS)
            } else {
                numbers
            }
        }
        _ => hash_text_embedding(text, LOCAL_RAG_EMBEDDING_DIMENSIONS),
    }
}

/// Python fallback of `chunk_lineage` (the rust sidecar citation matches it for
/// valid ranges).
pub fn chunk_lineage(
    item_id: &str,
    source_id: &str,
    project_id: &str,
    name: &str,
    metadata: &Value,
) -> Value {
    let meta = metadata.as_object();
    let line_start = meta
        .and_then(|fields| fields.get("lineStart"))
        .and_then(|value| {
            if value.is_i64() || value.is_u64() {
                python_int_opt(Some(value))
            } else {
                None
            }
        });
    let line_end = meta
        .and_then(|fields| fields.get("lineEnd"))
        .and_then(|value| {
            if value.is_i64() || value.is_u64() {
                python_int_opt(Some(value))
            } else {
                None
            }
        });
    let source = if source_id.is_empty() {
        name
    } else {
        source_id
    };
    let citation = match (line_start, line_end) {
        (Some(start), Some(end)) if end >= start => format!("{source}:L{start}-L{end}"),
        (Some(start), _) => format!("{source}:L{start}"),
        _ => source.to_string(),
    };
    json!({
        "chunkId": item_id,
        "docId": source_id,
        "projectId": project_id,
        "page": int_or_zero(meta.and_then(|fields| fields.get("page"))),
        "startChar": int_or_zero(meta.and_then(|fields| fields.get("start"))),
        "endChar": int_or_zero(meta.and_then(|fields| fields.get("end"))),
        "hash": meta.and_then(|fields| fields.get("hash")).map(value_str).unwrap_or_default(),
        "docVersion": meta
            .and_then(|fields| fields.get("docVersion"))
            .map(value_str)
            .unwrap_or_default(),
        "citation": citation,
    })
}

type MatchKey = (String, String, i64);

fn json_hybrid_matches(
    root: &Path,
    query: &str,
    tokens: &[String],
    query_vector: &[f64],
) -> HashMap<MatchKey, Value> {
    let mut matches: HashMap<MatchKey, Value> = HashMap::new();
    for (path, project_id) in iter_cached_file_paths(root) {
        let Some(cached) = read_cached_file(&path) else {
            continue;
        };
        let Some(Value::Array(chunks)) = cached.get("chunks") else {
            continue;
        };
        for chunk in chunks {
            let Some(chunk) = chunk.as_object() else {
                continue;
            };
            let text = text_or_empty(chunk.get("text"));
            let keyword_score = score_chunk(&text, tokens);
            let vector = vector_from_chunk(chunk, &text);
            let vector_score = cosine_f64(query_vector, &vector);
            let score = keyword_score * 10 + (vector_score * 100.0).trunc() as i64;
            if score <= 0 {
                continue;
            }
            let file_id = {
                let raw = text_or_empty(cached.get("id"));
                if raw.is_empty() {
                    path.file_stem()
                        .map(|stem| stem.to_string_lossy().into_owned())
                        .unwrap_or_default()
                } else {
                    raw
                }
            };
            let chunk_index = int_or_zero(chunk.get("index")) + 1;
            let key = (file_id.clone(), project_id.clone(), chunk_index);
            if let Some(existing) = matches.get(&key) {
                if int_or_zero(existing.get("score")) >= score {
                    continue;
                }
            }
            let name = {
                let raw = text_or_empty(cached.get("name"));
                if raw.is_empty() {
                    path.file_name()
                        .map(|name| name.to_string_lossy().into_owned())
                        .unwrap_or_default()
                } else {
                    raw
                }
            };
            let kind = {
                let raw = text_or_empty(cached.get("kind"));
                if raw.is_empty() {
                    "text".to_string()
                } else {
                    raw
                }
            };
            matches.insert(
                key,
                json!({
                    "score": score,
                    "fileId": file_id,
                    "projectId": project_id,
                    "name": name,
                    "kind": kind,
                    "chunkIndex": chunk_index,
                    "lineStart": int_or_zero(chunk.get("lineStart")),
                    "lineEnd": int_or_zero(chunk.get("lineEnd")),
                    "snippet": compact_snippet(&text, query, SNIPPET_LIMIT),
                    "retrieval": {
                        "source": "json_hybrid",
                        "vectorScore": round_4(vector_score),
                        "keywordScore": keyword_score,
                    },
                }),
            );
        }
    }
    matches
}

fn local_rag_matches(root: &Path, query: &str, limit: usize) -> HashMap<MatchKey, Value> {
    let mut matches: HashMap<MatchKey, Value> = HashMap::new();
    let Some(index) = MemoryIndex::open(root) else {
        return matches;
    };
    let Ok(hits) = index.search_files(query, (limit * 4).max(limit)) else {
        // `rag_vec` present: refuse the sqlite path rather than serving the
        // cosine fallback. json_hybrid still runs.
        return matches;
    };
    for hit in hits {
        let chunk_index = hit.chunk_index + 1;
        let key = (hit.source_id.clone(), hit.project_id.clone(), chunk_index);
        let line_start = int_or_zero(hit.metadata.get("lineStart"));
        let line_end = int_or_zero(hit.metadata.get("lineEnd"));
        matches.insert(
            key,
            json!({
                "score": hit.score,
                "fileId": hit.source_id,
                "projectId": hit.project_id,
                "name": hit.name,
                "kind": hit.kind,
                "chunkIndex": chunk_index,
                "lineStart": line_start,
                "lineEnd": line_end,
                "snippet": compact_snippet(&hit.text, query, SNIPPET_LIMIT),
                "lineage": chunk_lineage(
                    &hit.item_id,
                    &hit.source_id,
                    &hit.project_id,
                    &hit.name,
                    &hit.metadata,
                ),
                "retrieval": {
                    "source": "local_rag",
                    "vectorScore": round_4(hit.vector_score),
                    "keywordScore": hit.keyword_score,
                },
            }),
        );
    }
    matches
}

/// Mirrors `search_files`.
pub fn search_files(query: &str, limit: usize, root: &Path) -> Result<Value, AppError> {
    let query = query.trim();
    if query.is_empty() {
        return Err(AppError {
            message: "search_files query is empty".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let tokens = query_tokens(query);
    let query_vector = hash_text_embedding(query, LOCAL_RAG_EMBEDDING_DIMENSIONS);
    let mut matches_by_key = local_rag_matches(root, query, limit);
    let hybrid = json_hybrid_matches(root, query, &tokens, &query_vector);
    for (key, candidate) in hybrid {
        match matches_by_key.get(&key) {
            Some(existing)
                if int_or_zero(existing.get("score")) >= int_or_zero(candidate.get("score")) => {}
            _ => {
                matches_by_key.insert(key, candidate);
            }
        }
    }
    let mut matches: Vec<Value> = matches_by_key.into_values().collect();
    matches.sort_by(|left, right| {
        let left_score = int_or_zero(left.get("score"));
        let right_score = int_or_zero(right.get("score"));
        right_score
            .cmp(&left_score)
            .then_with(|| {
                let left_name = left.get("name").and_then(Value::as_str).unwrap_or_default();
                let right_name = right
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or_default();
                left_name.cmp(right_name)
            })
            .then_with(|| {
                int_or_zero(left.get("chunkIndex")).cmp(&int_or_zero(right.get("chunkIndex")))
            })
    });
    let searched: BTreeMap<&str, ()> = matches
        .iter()
        .filter_map(|item| item.get("fileId").and_then(Value::as_str))
        .map(|id| (id, ()))
        .collect();
    let searched_files = searched.len();
    matches.truncate(limit);
    Ok(json!({
        "query": query,
        "matches": matches,
        "searchedFiles": searched_files as i64,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    fn write_cache(root: &Path, relative: &str, body: &str) {
        let path = root.join(relative);
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent).unwrap();
        }
        fs::write(path, body).unwrap();
    }

    #[test]
    fn empty_query_is_invalid_payload() {
        let root = std::env::temp_dir().join(format!("search-files-empty-{}", std::process::id()));
        let err = search_files("  ", 5, &root).unwrap_err();
        assert_eq!(err.code, codes::INVALID_PAYLOAD);
        assert_eq!(err.message, "search_files query is empty");
    }

    #[test]
    fn compact_snippet_keeps_short_text_and_windows_long_text() {
        assert_eq!(
            compact_snippet("  hello   world  ", "hello", 700),
            "hello world"
        );
        let text = "a".repeat(2000);
        let snippet = compact_snippet(&text, "a", 700);
        assert!(snippet.chars().count() <= 700);
        assert!(snippet.contains('a'));
    }

    #[test]
    fn search_files_reads_temporary_and_project_indexes() {
        let root = std::env::temp_dir().join(format!("search-files-idx-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        write_cache(
            &root,
            ".file-cache/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
            r#"{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"react-notes.txt","kind":"text","chunks":[{"index":0,"text":"useMemo 可以缓存计算结果","lineStart":1,"lineEnd":1}]}"#,
        );
        write_cache(
            &root,
            ".projects/proj_1/files/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json",
            r#"{"id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","name":"project-guide.txt","kind":"text","chunks":[{"index":0,"text":"项目空间里的 useMemo 笔记","lineStart":2,"lineEnd":3}]}"#,
        );
        let result = search_files("useMemo", 10, &root).unwrap();
        assert_eq!(result["searchedFiles"], 2);
        let matches = result["matches"].as_array().unwrap();
        assert_eq!(matches.len(), 2);
        let projects: BTreeMap<_, ()> = matches
            .iter()
            .map(|item| (item["projectId"].as_str().unwrap(), ()))
            .collect();
        assert!(projects.contains_key(""));
        assert!(projects.contains_key("proj_1"));
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn corrupt_cache_is_skipped_and_a_real_chunk_is_kept() {
        let root = std::env::temp_dir().join(format!("search-files-bad-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        write_cache(&root, ".file-cache/not-json.json", "not json");
        write_cache(&root, ".file-cache/array.json", "[]");
        write_cache(
            &root,
            ".projects/proj/files/cccccccccccccccccccccccccccccccc.json",
            r#"{"id":"file","name":"notes","kind":"text","chunks":[null,{"index":0,"text":"needle text","lineStart":1,"lineEnd":2}]}"#,
        );
        let result = search_files("needle", 2, &root).unwrap();
        assert_eq!(result["searchedFiles"], 1);
        assert_eq!(result["matches"][0]["projectId"], "proj");
        assert_eq!(result["matches"][0]["retrieval"]["source"], "json_hybrid");
        let _ = fs::remove_dir_all(&root);
    }
}
