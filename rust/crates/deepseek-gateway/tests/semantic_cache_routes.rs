//! `GET /api/semantic-cache/status` integration tests.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use rusqlite::Connection;
use serde_json::Value;
use tower::ServiceExt;

const TEST_TOKEN: &str = "semantic-cache-test-token";
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

fn build_app(root: &std::path::Path, static_root: &std::path::Path) -> axum::Router {
    let ui = static_root.join("ui");
    std::fs::create_dir_all(&ui).unwrap();
    std::fs::write(ui.join("index.html"), "<main>ui</main>").unwrap();
    let _ = root;
    create_production_app(static_root).unwrap()
}

async fn call_status(app: &axum::Router, method: &str, token: Option<&str>) -> (StatusCode, Value) {
    let mut builder = Request::builder()
        .method(method)
        .uri("/api/semantic-cache/status")
        .header("host", "127.0.0.1");
    if let Some(bearer) = token {
        builder = builder.header(header::AUTHORIZATION, format!("Bearer {bearer}"));
    }
    let response = app
        .clone()
        .oneshot(builder.body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 32_768).await.unwrap();
    let body = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, body)
}

#[tokio::test]
async fn semantic_cache_status_reads_defaults_without_creating_db() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let static_root = tempfile::tempdir().unwrap();
    let _env = EnvGuard::set(&[
        (
            "DEEPSEEK_INFRA_ROOT",
            root.path().to_string_lossy().into_owned(),
        ),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
        ("SEMANTIC_CACHE_ENABLED", "1".to_string()),
        ("SEMANTIC_CACHE_VERSION", String::new()),
        ("LOCAL_RAG_EMBEDDING_PROVIDER", "hash".to_string()),
        ("LOCAL_RAG_EMBEDDING_DIMENSIONS", String::new()),
    ]);
    let app = build_app(root.path(), static_root.path());

    let (unauth, _) = call_status(&app, "GET", None).await;
    assert_eq!(unauth, StatusCode::UNAUTHORIZED);

    let (status, body) = call_status(&app, "GET", Some(TEST_TOKEN)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["ok"], true);
    assert_eq!(body["semanticCache"]["enabled"], true);
    assert_eq!(body["semanticCache"]["items"], 0);
    assert_eq!(body["semanticCache"]["hits"], 0);
    assert_eq!(body["semanticCache"]["embeddingProvider"], "hash");
    assert_eq!(body["semanticCache"]["embeddingDimensions"], 128);
    assert_eq!(body["semanticCache"]["cacheVersion"], "1:hash:128");
    assert_eq!(body["semanticCache"]["lastError"], "");
    assert!(!root.path().join(".semantic-cache").exists());

    let (head_status, head_body) = call_status(&app, "HEAD", Some(TEST_TOKEN)).await;
    assert_eq!(head_status, StatusCode::OK);
    assert_eq!(head_body, Value::Null);

    let (post_status, _) = call_status(&app, "POST", Some(TEST_TOKEN)).await;
    assert_eq!(post_status, StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn semantic_cache_status_aggregates_sqlite_counts_and_onnx_fallback() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let static_root = tempfile::tempdir().unwrap();
    let cache_dir = root.path().join(".semantic-cache");
    std::fs::create_dir_all(&cache_dir).unwrap();
    let db_path = cache_dir.join("cache.sqlite3");
    {
        let conn = Connection::open(&db_path).unwrap();
        conn.execute(
            "CREATE TABLE semantic_cache_items (id TEXT PRIMARY KEY, hit_count INTEGER NOT NULL)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO semantic_cache_items (id, hit_count) VALUES ('a', 3), ('b', 4)",
            [],
        )
        .unwrap();
    }
    let _env = EnvGuard::set(&[
        (
            "DEEPSEEK_INFRA_ROOT",
            root.path().to_string_lossy().into_owned(),
        ),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
        ("SEMANTIC_CACHE_ENABLED", "true".to_string()),
        ("SEMANTIC_CACHE_VERSION", "v9".to_string()),
        ("LOCAL_RAG_EMBEDDING_PROVIDER", "onnx".to_string()),
        (
            "LOCAL_RAG_ONNX_MODEL_PATH",
            root.path()
                .join("missing.onnx")
                .to_string_lossy()
                .into_owned(),
        ),
        ("LOCAL_RAG_EMBEDDING_DIMENSIONS", "256".to_string()),
    ]);
    let app = build_app(root.path(), static_root.path());

    let (status, body) = call_status(&app, "GET", Some(TEST_TOKEN)).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["semanticCache"]["items"], 2);
    assert_eq!(body["semanticCache"]["hits"], 7);
    assert_eq!(body["semanticCache"]["embeddingProvider"], "hash");
    assert_eq!(body["semanticCache"]["embeddingDimensions"], 256);
    assert_eq!(body["semanticCache"]["cacheVersion"], "v9:hash:256");
    assert!(
        body["semanticCache"]["lastError"]
            .as_str()
            .unwrap()
            .contains("LOCAL_RAG_ONNX_MODEL_PATH")
    );
}
