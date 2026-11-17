//! `POST /api/rag/verify-citation` and `POST /api/rag/eval` integration tests.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use deepseek_policy::attachment_context::{LOCAL_RAG_EMBEDDING_DIMENSIONS, hash_text_embedding};
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "rag-action-test-token";
static ENV_BUSY: AtomicBool = AtomicBool::new(false);

struct EnvLock;
impl EnvLock {
    fn acquire() -> Self {
        while ENV_BUSY
            .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
            .is_err()
        {
            std::thread::yield_now();
        }
        Self
    }
}
impl Drop for EnvLock {
    fn drop(&mut self) {
        ENV_BUSY.store(false, Ordering::Release);
    }
}

struct EnvGuard {
    saved: Vec<(&'static str, Option<String>)>,
}
impl EnvGuard {
    fn set(pairs: &[(&'static str, String)]) -> Self {
        let saved = pairs
            .iter()
            .map(|(name, value)| {
                let previous = std::env::var(name).ok();
                unsafe { std::env::set_var(name, value) };
                (*name, previous)
            })
            .collect();
        Self { saved }
    }
}
impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (name, value) in &self.saved {
            match value {
                Some(value) => unsafe { std::env::set_var(name, value) },
                None => unsafe { std::env::remove_var(name) },
            }
        }
    }
}

fn build_app(root: &std::path::Path) -> (tempfile::TempDir, EnvGuard, axum::Router) {
    let static_root = tempfile::tempdir().unwrap();
    let ui = static_root.path().join("ui");
    std::fs::create_dir_all(&ui).unwrap();
    std::fs::write(ui.join("index.html"), "<main>ui</main>").unwrap();
    let env = EnvGuard::set(&[
        (
            "DEEPSEEK_INFRA_ROOT",
            root.path().to_string_lossy().into_owned(),
        ),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
        ("LOCAL_RAG_ENABLED", "1".to_string()),
    ]);
    let app = create_production_app(static_root.path()).unwrap();
    (static_root, env, app)
}

async fn call_rag(
    app: &axum::Router,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let raw = match body {
        Some(value) => serde_json::to_vec(&value).unwrap(),
        None => Vec::new(),
    };
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header("host", "127.0.0.1")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
    if !raw.is_empty() {
        builder = builder
            .header(header::CONTENT_TYPE, "application/json")
            .header(header::CONTENT_LENGTH, raw.len().to_string());
    }
    let response = app
        .clone()
        .oneshot(builder.body(Body::from(raw)).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 65_536).await.unwrap();
    let body = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, body)
}

#[tokio::test]
async fn rag_verify_citation_and_eval_work_against_readonly_sqlite() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static, _env, app) = build_app(root.path());

    let (miss_s, miss_b) = call_rag(
        &app,
        "POST",
        "/api/rag/verify-citation",
        Some(json!({"itemId": "missing", "snippet": "hello"})),
    )
    .await;
    assert_eq!(miss_s, StatusCode::OK);
    assert_eq!(miss_b["citation"]["reason"], "chunk_not_found");
    assert!(!root.path().join(".local-rag").exists());

    let (bad_id_s, bad_id_b) = call_rag(
        &app,
        "POST",
        "/api/rag/verify-citation",
        Some(json!({"itemId": " ", "snippet": "hello"})),
    )
    .await;
    assert_eq!(bad_id_s, StatusCode::BAD_REQUEST);
    assert_eq!(bad_id_b["code"], "invalid_payload");

    let (bad_cases_s, _) =
        call_rag(&app, "POST", "/api/rag/eval", Some(json!({"cases": "no"}))).await;
    assert_eq!(bad_cases_s, StatusCode::BAD_REQUEST);

    let (get_s, _) = call_rag(&app, "GET", "/api/rag/eval", None).await;
    assert_eq!(get_s, StatusCode::NOT_FOUND);

    let rag_dir = root.path().join(".local-rag");
    std::fs::create_dir_all(&rag_dir).unwrap();
    let conn = Connection::open(rag_dir.join("rag.sqlite3")).unwrap();
    conn.execute_batch(
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
         );",
    )
    .unwrap();
    let text = "FastCDC deduplication reduces cloud egress across incremental snapshots";
    let emb =
        serde_json::to_string(&hash_text_embedding(text, LOCAL_RAG_EMBEDDING_DIMENSIONS)).unwrap();
    conn.execute(
        "INSERT INTO rag_items VALUES (?, 1, 'files', 'doc-fastcdc', 'p1', 0, 'arch.md', 'file', 'project:p1', ?, ?, ?, 1000)",
        [
            "chunk-1",
            text,
            &emb,
            &json!({"lineStart": 10, "lineEnd": 14, "page": 2, "hash": "abc"}).to_string(),
        ],
    )
    .unwrap();

    let (cit_s, cit_b) = call_rag(
        &app,
        "POST",
        "/api/rag/verify-citation",
        Some(json!({"itemId": "chunk-1", "snippet": "FastCDC deduplication"})),
    )
    .await;
    assert_eq!(cit_s, StatusCode::OK);
    assert_eq!(cit_b["citation"]["grounded"], true);
    assert_eq!(cit_b["citation"]["coverage"], 1.0);
    assert_eq!(
        cit_b["citation"]["lineage"]["citation"],
        "doc-fastcdc:L10-L14"
    );

    let (eval_s, eval_b) = call_rag(
        &app,
        "POST",
        "/api/rag/eval",
        Some(json!({"cases": [{"query": "FastCDC deduplication", "relevant": ["doc-fastcdc"]}], "k": 3})),
    )
    .await;
    assert_eq!(eval_s, StatusCode::OK);
    assert_eq!(eval_b["eval"]["cases"], 1);
    assert_eq!(eval_b["eval"]["recallAtK"], 1.0);
    assert_eq!(eval_b["eval"]["mrr"], 1.0);
}
