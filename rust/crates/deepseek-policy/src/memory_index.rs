//! The memory vector index read path — `local_rag`'s `memory` collection, read-only.
//!
//! `retrieve_memories` adds a vector bonus from `local_rag.search_memories_index`
//! (`infra/data/memory.py:439`). The assembly wiring plan's §4 measured that the
//! bonus is **not bounded** — with `LOCAL_RAG_ENABLED` defaulting to true and the
//! embedding provider to `hash`, the index is live offline, and 7 of 8 queries
//! changed order *and set* between an index-live and an index-absent run. So a
//! production caller needs a real provider, which is this module.
//!
//! # Scope: measured, not assumed
//!
//! `local_rag.search_memories_index` is `search(collection="memory", scopes=…,
//! limit=…)` (`local_rag.py:950`), and `_search_db` (`:812`) has two branches:
//!
//! - **sqlite-vec** (`rag_vec` `MATCH`) when the extension loaded;
//! - **cosine fallback** over the `rag_items.embedding` JSON column otherwise.
//!
//! `sqlite_vec` is not a dependency of this repository (not in `requirements*.txt`,
//! `pyproject.toml` or any Compose file; `find_spec` is `None`), so the fallback is
//! the branch every shipped deployment takes, and it is the one implemented here in
//! full.
//!
//! The vec branch is **not** reimplemented, because it cannot be: `vec0` is an
//! extension loaded into the Python connection and `rusqlite`'s bundled SQLite has
//! no such module. [`MemoryIndex::vector_table_ready`] is read from the schema
//! exactly as the oracle decides it, and when it is set the read returns
//! [`MemoryIndexError::VectorTableNotReadable`] rather than quietly computing the
//! fallback — the oracle would have blended `1/(1+distance)` into every score, so
//! the two answers differ in membership, not just in order.
//!
//! # Ownership
//!
//! This module **reads** `.local-rag/rag.sqlite3` and never writes it. Python
//! remains the writer (`sync_memories` from `save_memories`) until a `memory`
//! domain is declared, so `one_table_one_authoritative_writer` holds by
//! construction: there is no second writer to fence.
//!
//! The pure halves (`hash_text_embedding`, `normalize_vector`, `cosine_similarity`)
//! already live in [`crate::attachment_context`] and are reused rather than
//! duplicated. `bm25_scores` and the Python query normalization are ported here
//! because they are only reachable through this path.

use std::path::{Path, PathBuf};

use rusqlite::{Connection, Row, types::ValueRef};
use serde_json::{Map, Value, json};

use crate::attachment_context::{cosine_similarity, hash_text_embedding, normalize_vector};
use crate::core_utils::{python_float_opt, query_tokens};

/// `LOCAL_RAG_EMBEDDING_DIMENSIONS` — the default vector width.
pub const LOCAL_RAG_EMBEDDING_DIMENSIONS: usize = 64;
/// `LOCAL_RAG_BM25_K1`.
pub const LOCAL_RAG_BM25_K1: f64 = 1.5;
/// `LOCAL_RAG_BM25_B`.
pub const LOCAL_RAG_BM25_B: f64 = 0.75;
/// `COLLECTION_MEMORY`.
pub const COLLECTION_MEMORY: &str = "memory";
/// `ITEM_TABLE`.
pub const ITEM_TABLE: &str = "rag_items";
/// `VECTOR_TABLE`.
pub const VECTOR_TABLE: &str = "rag_vec";
/// `META_TABLE`.
pub const META_TABLE: &str = "rag_meta";

/// `LOCAL_RAG_DIR / "rag.sqlite3"`.
pub fn local_rag_dir(root: &Path) -> PathBuf {
    root.join(".local-rag")
}

/// `LOCAL_RAG_DB`.
pub fn local_rag_db(root: &Path) -> PathBuf {
    local_rag_dir(root).join("rag.sqlite3")
}

/// `_python_normalize_query`: whitespace-collapsed, lowercased.
///
/// This is **not** [`deepseek_rag`]'s `normalize_query`, which is the sidecar
/// contract (ASCII-only lowercasing); the oracle's own fallback lowercases the
/// whole string and collapses every whitespace run.
pub fn python_normalize_query(query: &str) -> String {
    query
        .to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// Mirrors `bm25_scores` — Okapi BM25 over the candidate corpus.
///
/// `doc_freq` counts each query term **once per document**, and `idf` uses the
/// `log(1 + (N - df + 0.5) / (df + 0.5))` form, so a term in every document still
/// scores above zero. The average length floors at `1.0` when it would be zero.
pub fn bm25_scores(
    query_terms: &[String],
    docs_terms: &[Vec<String>],
    k1: f64,
    b: f64,
) -> Vec<f64> {
    let total = docs_terms.len();
    if total == 0 || query_terms.is_empty() {
        return vec![0.0; total];
    }
    let mut unique_query: Vec<&str> = query_terms.iter().map(String::as_str).collect();
    unique_query.sort_unstable();
    unique_query.dedup();
    let in_query = |term: &str| unique_query.binary_search(&term).is_ok();

    let lengths: Vec<usize> = docs_terms.iter().map(Vec::len).collect();
    let sum: usize = lengths.iter().sum();
    let avgdl = {
        let average = sum as f64 / total as f64;
        if average == 0.0 { 1.0 } else { average }
    };

    let mut doc_freq: std::collections::HashMap<&str, i64> = std::collections::HashMap::new();
    for terms in docs_terms {
        // `unique_query.intersection(terms)` — each term at most once per document.
        let mut seen: Vec<&str> = Vec::new();
        for term in terms {
            if in_query(term) && !seen.contains(&term.as_str()) {
                seen.push(term.as_str());
                *doc_freq.entry(term.as_str()).or_insert(0) += 1;
            }
        }
    }

    let mut scores: Vec<f64> = Vec::with_capacity(total);
    for (index, terms) in docs_terms.iter().enumerate() {
        let length = if lengths[index] == 0 {
            1
        } else {
            lengths[index]
        };
        let mut term_freq: std::collections::HashMap<&str, i64> = std::collections::HashMap::new();
        for term in terms {
            if in_query(term) {
                *term_freq.entry(term.as_str()).or_insert(0) += 1;
            }
        }
        let mut score = 0.0f64;
        for (term, freq) in &term_freq {
            let df = *doc_freq.get(term).unwrap_or(&0) as f64;
            let idf = (1.0 + (total as f64 - df + 0.5) / (df + 0.5)).ln();
            let denominator = *freq as f64 + k1 * (1.0 - b + b * (length as f64 / avgdl));
            if denominator != 0.0 {
                score += idf * (*freq as f64 * (k1 + 1.0)) / denominator;
            }
        }
        scores.push(score);
    }
    scores
}

/// `parse_embedding` — a JSON array of numbers, normalized.
///
/// The three outcomes are distinct in the oracle and are kept distinct here:
///
/// - a decode error returns the **raw** empty list (`return []`), which is *not*
///   normalized — not `dimensions` zeros;
/// - a value that is not an array normalizes `[]`, which *is* `dimensions` zeros;
/// - an array normalizes to `dimensions` components.
///
/// `str(value or "[]")` is part of it: an empty string is falsy, so it is parsed as
/// `[]` and lands in the second branch rather than the first.
pub fn parse_embedding(value: &str, dimensions: usize) -> Vec<f64> {
    let text = if value.is_empty() { "[]" } else { value };
    let Ok(parsed) = serde_json::from_str::<Value>(text) else {
        return Vec::new();
    };
    let Value::Array(items) = parsed else {
        return normalize_vector(&[], dimensions);
    };
    normalize_vector(&items, dimensions)
}

/// One candidate row, as `sqlite3.Row` is read by the oracle.
#[derive(Debug, Clone)]
pub struct CandidateRow {
    pub item_id: String,
    pub collection: String,
    pub source_id: String,
    pub project_id: String,
    pub chunk_index: i64,
    pub name: String,
    pub kind: String,
    pub scope: String,
    pub text: String,
    pub embedding: String,
    pub metadata: Value,
}

/// `RAGSearchResult` — the fields the memory path consumes.
///
/// `name` and `chunk_index` are not decoration: together with the score they are the
/// oracle's whole sort key (`-score, name, chunk_index`), so a hit that dropped them
/// could not be ordered identically.
#[derive(Debug, Clone)]
pub struct IndexHit {
    pub item_id: String,
    pub source_id: String,
    pub name: String,
    pub chunk_index: i64,
    pub score: i64,
    pub vector_score: f64,
    pub keyword_score: i64,
    pub metadata: Value,
}

/// An open, **read-only** handle on the RAG index.
pub struct MemoryIndex {
    connection: Connection,
    /// `vector_table_ready` — the oracle's own decision, read from the schema.
    pub vector_table_ready: bool,
    pub dimensions: usize,
}

impl MemoryIndex {
    /// Opens `rag.sqlite3` read-only.
    ///
    /// The oracle opens read-write and creates the directory; a reader must not,
    /// so a missing file is `None` rather than an empty index — `search` on a
    /// missing database degrades to `[]` in the oracle only because `db_ready`
    /// creates it, which is a *write* this module deliberately does not perform.
    pub fn open(root: &Path) -> Option<Self> {
        let path = local_rag_db(root);
        if !path.exists() {
            return None;
        }
        let connection =
            Connection::open_with_flags(&path, rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).ok()?;
        let vector_table_ready = table_exists(&connection, VECTOR_TABLE);
        let dimensions = read_dimensions(&connection).unwrap_or(LOCAL_RAG_EMBEDDING_DIMENSIONS);
        Some(Self {
            connection,
            vector_table_ready,
            dimensions,
        })
    }

    /// `search_memories_index(query, scopes=…, limit=…)` for one query.
    ///
    /// Mirrors `search` + `_search_db`: a blank query returns nothing, and a read
    /// that fails degrades to an empty list rather than raising — the oracle's own
    /// `try/except Exception` around `_search_db`.
    ///
    /// The **one** case that is not a degrade is the vec branch: when `rag_vec`
    /// exists the oracle blends `1/(1+distance)` into every score, and this process
    /// cannot evaluate a `vec0` `MATCH`, so serving the cosine fallback would
    /// silently change which memories reach the prompt. That is
    /// [`MemoryIndexError::VectorTableNotReadable`], and it is narrow — it fires
    /// only in a deployment that installed `sqlite-vec`.
    pub fn search_memories(
        &self,
        query: &str,
        scopes: &[String],
        limit: usize,
    ) -> Result<Vec<IndexHit>, MemoryIndexError> {
        if query.trim().is_empty() {
            return Ok(Vec::new());
        }
        let query_vector = hash_text_embedding(query, self.dimensions);

        // The `rag_vec` branch: `distance` per item, when the extension is present.
        let mut vector_distances: std::collections::HashMap<String, f64> =
            std::collections::HashMap::new();
        if self.vector_table_ready {
            vector_distances = self
                .vector_match(&query_vector, scopes, limit)
                .map_err(|error| MemoryIndexError::VectorTableNotReadable(error.to_string()))?;
        }

        Ok(self
            .search_db(query, &query_vector, scopes, limit, &vector_distances)
            .unwrap_or_default())
    }

    /// How many rows the index holds for one collection.
    ///
    /// This is the fixture check that tells "no hits" from "no index": a caller (and
    /// the parity probe) uses it to prove it is reading the store it thinks it is.
    pub fn row_count(&self, collection: &str) -> Result<usize, rusqlite::Error> {
        self.connection
            .query_row(
                &format!("SELECT COUNT(*) FROM {ITEM_TABLE} WHERE collection = ?"),
                [collection],
                |row| row.get::<_, i64>(0),
            )
            .map(|count| count.max(0) as usize)
    }

    fn search_db(
        &self,
        query: &str,
        query_vector: &[f64],
        scopes: &[String],
        limit: usize,
        vector_distances: &std::collections::HashMap<String, f64>,
    ) -> Result<Vec<IndexHit>, rusqlite::Error> {
        let rows = self.load_candidate_rows(scopes, vector_distances)?;
        let normalized_query = python_normalize_query(query);
        let tokens = query_tokens(&normalized_query);
        let docs_terms: Vec<Vec<String>> = rows.iter().map(|row| query_tokens(&row.text)).collect();
        let lexical_scores = bm25_scores(&tokens, &docs_terms, LOCAL_RAG_BM25_K1, LOCAL_RAG_BM25_B);

        let mut results: Vec<IndexHit> = Vec::new();
        for (index, row) in rows.iter().enumerate() {
            let embedding = parse_embedding(&row.embedding, self.dimensions);
            let embedding_values: Vec<Value> = embedding.iter().map(|item| json!(item)).collect();
            let query_values: Vec<Value> = query_vector.iter().map(|item| json!(item)).collect();
            let cosine = cosine_similarity(&query_values, &embedding_values);
            let vector_score = match vector_distances.get(&row.item_id) {
                Some(distance) => {
                    let distance = distance.max(0.0);
                    cosine.max(1.0 / (1.0 + distance))
                }
                None => cosine,
            };
            let keyword_score = lexical_scores.get(index).copied().unwrap_or(0.0);
            // `int(round(vector_score * 100 + keyword_score * 10))` — Python's
            // `round` is ties-to-even, which `f64::round` is not.
            let score = python_round(vector_score * 100.0 + keyword_score * 10.0) as i64;
            if score <= 0 {
                continue;
            }
            results.push(IndexHit {
                item_id: row.item_id.clone(),
                source_id: row.source_id.clone(),
                name: row.name.clone(),
                chunk_index: row.chunk_index,
                score,
                vector_score,
                keyword_score: python_round(keyword_score) as i64,
                metadata: row.metadata.clone(),
            });
        }
        // `results.sort(key=lambda item: (-item.score, item.name, item.chunk_index))`.
        // `sort_by` is stable, matching Python's `list.sort`, so an exact tie keeps the
        // candidate order the `SELECT` returned.
        results.sort_by(|left, right| {
            right
                .score
                .cmp(&left.score)
                .then_with(|| left.name.cmp(&right.name))
                .then_with(|| left.chunk_index.cmp(&right.chunk_index))
        });
        results.truncate(limit);
        Ok(results)
    }

    fn vector_match(
        &self,
        query_vector: &[f64],
        scopes: &[String],
        limit: usize,
    ) -> Result<std::collections::HashMap<String, f64>, rusqlite::Error> {
        let blob = vector_blob(query_vector);
        let mut clauses = vec![
            "embedding MATCH ?".to_string(),
            "k = ?".to_string(),
            "collection = ?".to_string(),
        ];
        let mut parameters: Vec<Box<dyn rusqlite::ToSql>> = vec![
            Box::new(blob),
            Box::new((limit * 4).max(limit) as i64),
            Box::new(COLLECTION_MEMORY.to_string()),
        ];
        if !scopes.is_empty() {
            let placeholders = vec!["?"; scopes.len()].join(", ");
            clauses.push(format!("scope IN ({placeholders})"));
            for scope in scopes {
                parameters.push(Box::new(scope.clone()));
            }
        }
        let statement = format!(
            "SELECT item_id, distance FROM {VECTOR_TABLE} WHERE {}",
            clauses.join(" AND ")
        );
        let mut prepared = self.connection.prepare(&statement)?;
        let borrowed: Vec<&dyn rusqlite::ToSql> =
            parameters.iter().map(|item| item.as_ref()).collect();
        let mut rows = prepared.query(borrowed.as_slice())?;
        let mut distances = std::collections::HashMap::new();
        while let Some(row) = rows.next()? {
            let item_id: String = row.get(0)?;
            let distance: f64 = row.get(1)?;
            distances.insert(item_id, distance);
        }
        Ok(distances)
    }

    /// `load_candidate_rows` — the scoped `SELECT`, plus any item the vector branch
    /// returned that the scope filter did not already include.
    fn load_candidate_rows(
        &self,
        scopes: &[String],
        vector_distances: &std::collections::HashMap<String, f64>,
    ) -> Result<Vec<CandidateRow>, rusqlite::Error> {
        let mut clauses = vec!["collection = ?".to_string()];
        let mut parameters: Vec<Box<dyn rusqlite::ToSql>> =
            vec![Box::new(COLLECTION_MEMORY.to_string())];
        if !scopes.is_empty() {
            let placeholders = vec!["?"; scopes.len()].join(", ");
            clauses.push(format!("scope IN ({placeholders})"));
            for scope in scopes {
                parameters.push(Box::new(scope.clone()));
            }
        }
        let statement = format!("SELECT * FROM {ITEM_TABLE} WHERE {}", clauses.join(" AND "));
        let mut prepared = self.connection.prepare(&statement)?;
        let borrowed: Vec<&dyn rusqlite::ToSql> =
            parameters.iter().map(|item| item.as_ref()).collect();
        let mut rows = prepared.query(borrowed.as_slice())?;
        let mut candidates: Vec<CandidateRow> = Vec::new();
        while let Some(row) = rows.next()? {
            candidates.push(row_to_candidate(row)?);
        }

        if !vector_distances.is_empty() {
            let present: std::collections::HashSet<&str> =
                candidates.iter().map(|row| row.item_id.as_str()).collect();
            let missing: Vec<&String> = vector_distances
                .keys()
                .filter(|item_id| !present.contains(item_id.as_str()))
                .collect();
            if !missing.is_empty() {
                let placeholders = vec!["?"; missing.len()].join(", ");
                let statement =
                    format!("SELECT * FROM {ITEM_TABLE} WHERE item_id IN ({placeholders})");
                let mut prepared = self.connection.prepare(&statement)?;
                let borrowed: Vec<&dyn rusqlite::ToSql> = missing
                    .iter()
                    .map(|item_id| *item_id as &dyn rusqlite::ToSql)
                    .collect();
                let mut rows = prepared.query(borrowed.as_slice())?;
                while let Some(row) = rows.next()? {
                    candidates.push(row_to_candidate(row)?);
                }
            }
        }
        Ok(candidates)
    }
}

/// Why a read from this index cannot be served faithfully.
///
/// The distinction that matters: every *other* failure mode of the read path is the
/// oracle's own degrade-to-empty (`search`'s `except Exception`), so reproducing it
/// with an empty list is faithful. This one is not a failure the oracle ever takes —
/// it is a branch this process cannot evaluate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MemoryIndexError {
    /// `rag_vec` exists, so the oracle in this deployment blends its `vec0` distances
    /// into every score, and this process cannot evaluate a `vec0` `MATCH`: the
    /// extension is loaded into the Python connection, not into `rusqlite`'s bundled
    /// SQLite. Serving the cosine fallback would silently change both the order and
    /// the membership of the retrieved set, so the read refuses instead.
    ///
    /// Narrow by construction: `initialize_schema` creates `rag_vec` only when
    /// `sqlite-vec` is importable, so a deployment without that optional extra — which
    /// is every deployment this repository ships and every CI leg — cannot reach it.
    VectorTableNotReadable(String),
}

impl std::fmt::Display for MemoryIndexError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::VectorTableNotReadable(detail) => write!(
                formatter,
                "the memory index has a rag_vec table, so the oracle would blend sqlite-vec \
                 distances into every score, but this process cannot evaluate a vec0 MATCH \
                 ({detail}); refusing rather than serving the cosine fallback"
            ),
        }
    }
}

impl std::error::Error for MemoryIndexError {}

fn row_to_candidate(row: &Row<'_>) -> Result<CandidateRow, rusqlite::Error> {
    let metadata_raw: String = row.get("metadata")?;
    let metadata = serde_json::from_str::<Value>(&metadata_raw)
        .ok()
        .filter(Value::is_object)
        .unwrap_or_else(|| Value::Object(Map::new()));
    Ok(CandidateRow {
        item_id: row.get("item_id")?,
        collection: row.get("collection")?,
        source_id: row.get("source_id")?,
        project_id: row.get("project_id")?,
        chunk_index: row.get("chunk_index")?,
        name: row.get("name")?,
        kind: row.get("kind")?,
        scope: row.get("scope")?,
        text: row.get("text")?,
        embedding: row.get("embedding")?,
        metadata,
    })
}

/// `struct.pack(f"{len}f", …)` — the little-endian float blob `rag_vec` stores.
pub fn vector_blob(vector: &[f64]) -> Vec<u8> {
    let mut out = Vec::with_capacity(vector.len() * 4);
    for item in vector {
        out.extend_from_slice(&(*item as f32).to_le_bytes());
    }
    out
}

/// Python's `round`: ties to even, and no half-away-from-zero surprise.
pub fn python_round(value: f64) -> f64 {
    let floor = value.floor();
    let difference = value - floor;
    if (difference - 0.5).abs() < f64::EPSILON {
        // A tie goes to the even neighbour.
        if (floor as i64) % 2 == 0 {
            floor
        } else {
            floor + 1.0
        }
    } else {
        value.round()
    }
}

fn table_exists(connection: &Connection, table: &str) -> bool {
    connection
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
            [table],
            |_| Ok(()),
        )
        .is_ok()
}

fn read_dimensions(connection: &Connection) -> Option<usize> {
    let value: String = connection
        .query_row(
            &format!("SELECT value FROM {META_TABLE} WHERE key = 'embedding_dimensions'"),
            [],
            |row| row.get(0),
        )
        .ok()?;
    value.trim().parse::<usize>().ok().filter(|size| *size > 0)
}

/// The `VectorHits` provider shape `retrieve_memories` takes, bound to one index.
///
/// `local_rag.search_memories_index` returns results in `(-score, name, chunk_index)`
/// order; `retrieve_memories` keeps the **maximum** score per memory id, so the
/// provider returns a map rather than a list.
///
/// Fallible on purpose. `VectorHits` itself is an infallible closure, so a caller that
/// wants to bind this index must call this first and refuse the turn on `Err` — the
/// alternative is a `None`-or-empty map, which is exactly the silent divergence
/// [`MemoryIndexError::VectorTableNotReadable`] exists to prevent.
pub fn memory_vector_hits(
    index: &MemoryIndex,
    query: &str,
    scopes: &[String],
    limit: usize,
) -> Result<std::collections::HashMap<String, i64>, MemoryIndexError> {
    let mut hits: std::collections::HashMap<String, i64> = std::collections::HashMap::new();
    for hit in index.search_memories(query, scopes, limit)? {
        let memory_id = hit
            .metadata
            .get("id")
            .and_then(Value::as_str)
            .filter(|id| !id.is_empty())
            .map(str::to_string)
            .unwrap_or_else(|| hit.source_id.clone());
        if memory_id.is_empty() {
            continue;
        }
        let entry = hits.entry(memory_id).or_insert(0);
        if hit.score > *entry {
            *entry = hit.score;
        }
    }
    Ok(hits)
}

/// `_score_chunks_with_rust`'s Python fallback needs the raw row value shape; this
/// exposes the column reader for tests and for callers that shape their own view.
pub fn row_value(row: &Row<'_>, column: &str) -> Result<Value, rusqlite::Error> {
    let index = row.as_ref().column_index(column)?;
    Ok(match row.get_ref(index)? {
        ValueRef::Null => Value::Null,
        ValueRef::Integer(int) => json!(int),
        ValueRef::Real(float) => json!(python_float_opt(&json!(float)).unwrap_or(float)),
        ValueRef::Text(text) => json!(String::from_utf8_lossy(text).to_string()),
        ValueRef::Blob(blob) => json!(blob.len()),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn query_normalization_collapses_and_lowercases_unicode() {
        assert_eq!(python_normalize_query("  HeLLo   World  "), "hello world");
        assert_eq!(python_normalize_query("项目\t用\nRust"), "项目 用 rust");
        assert_eq!(python_normalize_query(""), "");
    }

    #[test]
    fn bm25_is_zero_without_terms_or_documents() {
        assert_eq!(bm25_scores(&[], &[vec!["a".into()]], 1.5, 0.75), vec![0.0]);
        assert!(bm25_scores(&["a".into()], &[], 1.5, 0.75).is_empty());
    }

    #[test]
    fn bm25_rewards_rarer_terms_and_shorter_documents() {
        let query = vec!["rust".to_string()];
        let docs = vec![
            vec!["rust".to_string(), "web".to_string()],
            vec!["rust".to_string()],
            vec!["python".to_string()],
        ];
        let scores = bm25_scores(&query, &docs, 1.5, 0.75);
        // The document without the term scores zero.
        assert_eq!(scores[2], 0.0);
        // The shorter matching document scores higher than the longer one.
        assert!(scores[1] > scores[0], "{scores:?}");
        // A term present in every document still scores above zero.
        let everywhere = bm25_scores(&query, &[query.clone(), query.clone()], 1.5, 0.75);
        assert!(everywhere.iter().all(|score| *score > 0.0));
    }

    #[test]
    fn bm25_counts_each_query_term_once_per_document() {
        let query = vec!["rust".to_string(), "rust".to_string()];
        let docs = vec![vec!["rust".to_string()]];
        let scores = bm25_scores(&query, &docs, 1.5, 0.75);
        // `unique_query` is a set, so the duplicate does not double the idf.
        let single = bm25_scores(&["rust".to_string()], &docs, 1.5, 0.75);
        assert_eq!(scores, single);
    }

    #[test]
    fn embedding_parsing_matches_the_oracle_fallbacks() {
        let parsed = parse_embedding("[3, 4]", 2);
        assert_eq!(parsed, vec![0.6, 0.8]);
        // A decode error is the oracle's raw `return []`, which is not normalized.
        assert!(parse_embedding("{not json", 2).is_empty());
        // Not an array, and a non-numeric member: both normalize `[]`.
        assert_eq!(parse_embedding("42", 2), vec![0.0, 0.0]);
        assert_eq!(parse_embedding("[\"x\"]", 1), vec![0.0]);
        // `str(value or "[]")`: the empty string is falsy, so it parses as `[]`.
        assert_eq!(parse_embedding("", 2), vec![0.0, 0.0]);
    }

    #[test]
    fn python_round_is_ties_to_even() {
        assert_eq!(python_round(0.5), 0.0);
        assert_eq!(python_round(1.5), 2.0);
        assert_eq!(python_round(2.5), 2.0);
        assert_eq!(python_round(-0.5), 0.0);
        assert_eq!(python_round(2.4), 2.0);
    }

    #[test]
    fn vector_blob_is_little_endian_floats() {
        let blob = vector_blob(&[1.0, -1.0]);
        assert_eq!(blob.len(), 8);
        assert_eq!(&blob[0..4], &1.0f32.to_le_bytes());
        assert_eq!(&blob[4..8], &(-1.0f32).to_le_bytes());
    }

    #[test]
    fn a_missing_database_is_no_index_rather_than_an_empty_one() {
        let root =
            std::env::temp_dir().join(format!("memory-index-missing-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        assert!(MemoryIndex::open(&root).is_none());
        let _ = std::fs::remove_dir_all(&root);
    }

    fn scratch(tag: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "memory-index-{tag}-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        root
    }

    /// A minimal `rag.sqlite3` carrying the columns the read path touches, built here
    /// rather than by the oracle because the parity probe is what pairs the two real
    /// implementations; these tests only pin the two branches.
    fn write_fixture(root: &Path, with_vector_table: bool) {
        let directory = local_rag_dir(root);
        std::fs::create_dir_all(&directory).unwrap();
        let connection = Connection::open(directory.join("rag.sqlite3")).unwrap();
        connection
            .execute_batch(
                "CREATE TABLE rag_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                 CREATE TABLE rag_items (
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
                 INSERT INTO rag_meta(key, value) VALUES ('embedding_dimensions', '64');",
            )
            .unwrap();
        if with_vector_table {
            // Enough for `table_exists` to fire and for `vector_match` to fail: the
            // point under test is the refusal, not a working `vec0`.
            connection
                .execute_batch("CREATE TABLE rag_vec (item_id TEXT, distance REAL);")
                .unwrap();
        }
        let content = "我喜欢简洁直接的回答风格";
        connection
            .execute(
                "INSERT INTO rag_items VALUES ('memory-1', 1, 'memory', 'm-style', '', 0, \
                 'preference', 'memory', 'global', ?1, ?2, ?3, 0)",
                rusqlite::params![
                    content,
                    serde_json::to_string(&hash_text_embedding(content, 64)).unwrap(),
                    "{\"id\": \"m-style\"}"
                ],
            )
            .unwrap();
        connection.close().unwrap();
    }

    #[test]
    fn the_cosine_fallback_scores_the_row_it_reads() {
        let root = scratch("cosine");
        write_fixture(&root, false);
        let index = MemoryIndex::open(&root).expect("the fixture index opens");
        assert!(!index.vector_table_ready);
        assert_eq!(index.row_count(COLLECTION_MEMORY).unwrap(), 1);
        assert_eq!(index.row_count("files").unwrap(), 0);

        let scopes = vec!["global".to_string()];
        let content = "我喜欢简洁直接的回答风格";
        let hits =
            memory_vector_hits(&index, content, &scopes, 24).expect("the fallback is readable");
        // The identical text makes the cosine 1.0, so the vector half alone is 100.
        assert!(hits.get("m-style").copied().unwrap_or(0) >= 100, "{hits:?}");
        // A blank query is the oracle's own early return, not a read.
        assert!(
            index
                .search_memories("   ", &scopes, 24)
                .unwrap()
                .is_empty()
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn a_vec_table_is_refused_rather_than_silently_fallen_back_to() {
        let root = scratch("vec");
        write_fixture(&root, true);
        let index = MemoryIndex::open(&root).expect("the fixture index opens");
        assert!(index.vector_table_ready);

        let scopes = vec!["global".to_string()];
        let error = index
            .search_memories("react", &scopes, 24)
            .expect_err("a vec0 MATCH cannot be served from bundled SQLite");
        assert!(matches!(error, MemoryIndexError::VectorTableNotReadable(_)));
        // The refusal is a `Display`, so a caller can render it into the notice the
        // way every other refusal in this crate is rendered.
        assert!(error.to_string().contains("vec0"));
        // And it survives the provider adapter rather than becoming a silent `{}`.
        assert!(memory_vector_hits(&index, "react", &scopes, 24).is_err());
        let _ = std::fs::remove_dir_all(&root);
    }
}
