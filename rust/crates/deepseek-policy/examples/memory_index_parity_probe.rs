//! Memory-index read-path parity probe, Rust side.
//!
//! Reads the `rag.sqlite3` that `tasks/native-runtime/memory_index_parity_probe.py`
//! builds through the real `save_memories` -> `sync_memories` write path, replays the
//! same queries through `deepseek_policy::memory_index` and
//! `deepseek_policy::memory::retrieve_memories`, and prints canonical JSON.
//!
//! **The Python side must run first** — it creates and populates the fixture. Both
//! sides default to the same directory, so the documented commands need no argument:
//!
//! ```text
//! python tasks/native-runtime/memory_index_parity_probe.py > python.json
//! cd rust && cargo run -p deepseek-policy --example memory_index_parity_probe > ../rust.json
//! diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)
//! ```
//!
//! Three layers are compared, so a failure names which one moved:
//!
//! - **pure** — `hash_text_embedding`, `normalize_search_query`, `bm25_scores`,
//!   `parse_embedding`;
//! - **store** — the ordered `(id, score, vector_score, keyword_score, name,
//!   chunk_index)` list `search_memories_index` returns, and the `id -> max(score)` map
//!   `retrieve_memories` builds from it;
//! - **turn** — `retrieve_memories` with the provider and with `None`. Python produces
//!   the second by making `search_memories_index` raise, which is the oracle's own
//!   degrade path; Rust passes `None`. The two must agree on **both**, which is what
//!   makes this pair an acceptance test rather than a smoke test: it pins that the
//!   bonus the Rust provider supplies is the bonus the oracle applies, and that its
//!   absence degrades the same way.

use std::collections::{BTreeMap, HashMap};
use std::path::PathBuf;

use deepseek_policy::attachment_context::hash_text_embedding;
use deepseek_policy::core_utils::query_tokens;
use deepseek_policy::memory::{MEMORY_RETRIEVE_LIMIT, VectorHits, retrieve_memories};
use deepseek_policy::memory_index::{
    COLLECTION_MEMORY, LOCAL_RAG_BM25_B, LOCAL_RAG_BM25_K1, LOCAL_RAG_EMBEDDING_DIMENSIONS,
    MemoryIndex, bm25_scores, memory_vector_hits, parse_embedding, python_normalize_query,
};
use deepseek_policy::python_json::OrderedJson;
use serde_json::{Map, Value, json};

const QUERIES: [(&str, &[&str]); 8] = [
    ("react", &["global"]),
    ("React 组件", &["global"]),
    ("你记得什么", &["global"]),
    ("我的生日", &["global"]),
    ("后端", &["global"]),
    ("react", &["global", "project:abc"]),
    ("pnpm", &["global", "project:abc"]),
    ("kubernetes", &["global"]),
];

const EMBEDDING_CASES: [&str; 7] = [
    "react",
    "React 组件",
    "你记得什么",
    "The user prefers concise answers in English meetings",
    "",
    "   ",
    "kubernetes、terraform",
];

const NORMALIZE_CASES: [&str; 6] = [
    "  HeLLo   World  ",
    "项目\t用\nRust",
    "",
    "   ",
    "React  组件",
    "ÄÖÜ  ß",
];

const BM25_QUERY: &str = "rust react 组件";
const BM25_DOCS: [&str; 6] = [
    "rust 组件",
    "react",
    "rust react 组件 组件",
    "python",
    "",
    "rust rust react 组件 组件 组件",
];

const PARSE_CASES: [&str; 10] = [
    "[3, 4]",
    "[0.5, -0.5]",
    "[]",
    "{not json",
    "42",
    "[\"x\"]",
    "[1, null, 3]",
    "[1, 2, 3, 4]",
    "[0, 0]",
    "",
];

fn default_root() -> PathBuf {
    std::env::temp_dir().join("deepseek-memory-index-parity")
}

/// `f"{query}::{','.join(scopes)}"` — the key both sides label a case with.
fn key_of(query: &str, scopes: &[String]) -> String {
    let joined: Vec<&str> = scopes.iter().map(String::as_str).collect();
    format!("{query}::{}", joined.join(","))
}

fn scopes_of(scopes: &[&str]) -> Vec<String> {
    scopes.iter().map(|scope| (*scope).to_string()).collect()
}

fn ids_of(items: &[Value]) -> Vec<Value> {
    items
        .iter()
        .map(|item| {
            Value::String(
                item.get("id")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
            )
        })
        .collect()
}

/// The start of `search_memories_index`'s result objects, in the oracle's own key
/// order — the probe renders sorted, but the field set has to match Python's exactly.
fn hit_to_value(hit: &deepseek_policy::memory_index::IndexHit) -> Value {
    let id = hit
        .metadata
        .get("id")
        .and_then(Value::as_str)
        .filter(|id| !id.is_empty())
        .unwrap_or(hit.source_id.as_str());
    let mut row = Map::new();
    row.insert("id".to_string(), json!(id));
    row.insert("score".to_string(), json!(hit.score));
    row.insert("vector_score".to_string(), json!(hit.vector_score));
    row.insert("keyword_score".to_string(), json!(hit.keyword_score));
    row.insert("name".to_string(), json!(hit.name));
    row.insert("chunk_index".to_string(), json!(hit.chunk_index));
    Value::Object(row)
}

fn main() {
    let root: PathBuf = std::env::args()
        .nth(1)
        .map(PathBuf::from)
        .unwrap_or_else(default_root);
    eprintln!("probe root: {}", root.display());
    let mut out: BTreeMap<String, Value> = BTreeMap::new();

    // --- pure -----------------------------------------------------------------------
    for (index, text) in EMBEDDING_CASES.iter().enumerate() {
        out.insert(
            format!("pure::embedding-{index}"),
            json!(hash_text_embedding(text, LOCAL_RAG_EMBEDDING_DIMENSIONS)),
        );
    }
    for (index, query) in NORMALIZE_CASES.iter().enumerate() {
        out.insert(
            format!("pure::normalize-{index}"),
            json!(python_normalize_query(query)),
        );
    }
    let tokens = query_tokens(&python_normalize_query(BM25_QUERY));
    let docs_terms: Vec<Vec<String>> = BM25_DOCS.iter().map(|text| query_tokens(text)).collect();
    out.insert("pure::bm25-query-tokens".to_string(), json!(tokens));
    out.insert("pure::bm25-doc-tokens".to_string(), json!(docs_terms));
    out.insert(
        "pure::bm25".to_string(),
        json!(bm25_scores(
            &tokens,
            &docs_terms,
            LOCAL_RAG_BM25_K1,
            LOCAL_RAG_BM25_B
        )),
    );
    for (index, raw) in PARSE_CASES.iter().enumerate() {
        out.insert(
            format!("pure::parse-{index}"),
            json!(parse_embedding(raw, LOCAL_RAG_EMBEDDING_DIMENSIONS)),
        );
    }

    // --- store ----------------------------------------------------------------------
    let index = MemoryIndex::open(&root).expect(
        "no .local-rag/rag.sqlite3 under the probe root: run \
         tasks/native-runtime/memory_index_parity_probe.py first to build the fixture",
    );
    out.insert("store::dimensions".to_string(), json!(index.dimensions));
    out.insert(
        "store::vector-table-present".to_string(),
        json!(index.vector_table_ready),
    );
    out.insert(
        "store::memory-rows".to_string(),
        json!(index.row_count(COLLECTION_MEMORY).unwrap_or(0)),
    );
    let limit = MEMORY_RETRIEVE_LIMIT * 2;
    out.insert("store::limit".to_string(), json!(limit));

    for (query, raw_scopes) in QUERIES.iter() {
        let scopes = scopes_of(raw_scopes);
        let key = key_of(query, &scopes);

        let results = index
            .search_memories(query, &scopes, limit)
            .expect("the fixture carries no rag_vec table, so the read must succeed");
        out.insert(
            format!("index::{key}"),
            Value::Array(results.iter().map(hit_to_value).collect()),
        );

        let hits = memory_vector_hits(&index, query, &scopes, limit)
            .expect("the fixture carries no rag_vec table, so the read must succeed");
        out.insert(
            format!("hits::{key}"),
            json!(hits.into_iter().collect::<BTreeMap<String, i64>>()),
        );
    }

    // --- turn -----------------------------------------------------------------------
    let provider = |query: &str, scopes: &[String]| -> HashMap<String, i64> {
        memory_vector_hits(&index, query, scopes, limit).unwrap_or_default()
    };
    let borrowed: &VectorHits<'_> = &provider;

    let mut differing: usize = 0;
    for (query, raw_scopes) in QUERIES.iter() {
        let scopes = scopes_of(raw_scopes);
        let key = key_of(query, &scopes);
        let live = retrieve_memories(query, Some(&scopes), &root, Some(borrowed));
        let without = retrieve_memories(query, Some(&scopes), &root, None);
        if ids_of(&live) != ids_of(&without) {
            differing += 1;
        }
        out.insert(format!("retrieve::{key}"), Value::Array(ids_of(&live)));
        out.insert(
            format!("retrieve-none::{key}"),
            Value::Array(ids_of(&without)),
        );
    }
    out.insert("turn::differing".to_string(), json!(differing));
    out.insert("turn::queries".to_string(), json!(QUERIES.len()));

    let value = Value::Object(out.into_iter().collect::<Map<String, Value>>());
    let rendered = OrderedJson::from_value_with_order(&value, &[]).render_indent_2();
    println!("{rendered}");
}
