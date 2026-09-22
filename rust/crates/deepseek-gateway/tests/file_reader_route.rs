//! Real HTTP coverage for `POST /api/file-reader` and `POST /api/file-chunk`.
//!
//! The windowing rules are unit-tested in `deepseek-policy` against the oracle's own
//! edge cases; what this suite adds is that the **production router** serves both
//! routes, that the request bodies are parsed the oracle's way, and that the two error
//! envelopes are the oracle's.
//!
//! # Why the bodies are asserted, not just the statuses
//!
//! Both routes take the internal payload, where a falsy value means "use the default"
//! (`chunkStart or 1`), and both report 1-based indices. A route that parsed `0` as
//! "chunk zero" or treated a missing `chunkCount` as "one chunk" would still answer
//! `200`.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "unit-file-reader-token";
const FILE_ID: &str = "0123456789abcdef0123456789abcdef";

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
        let mut saved = Vec::new();
        for (name, value) in pairs {
            saved.push((*name, std::env::var(name).ok()));
            unsafe {
                std::env::set_var(name, value);
            }
        }
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

/// A workspace with one cached file holding `chunk_count` chunks.
struct Fixture {
    _root: tempfile::TempDir,
    _env: EnvGuard,
    app: axum::Router,
}

impl Fixture {
    fn new(chunk_count: usize) -> Self {
        let root = tempfile::tempdir().expect("a temp workspace root");
        let cache_dir = root.path().join(".file-cache");
        std::fs::create_dir_all(&cache_dir).unwrap();
        let chunks: Vec<Value> = (0..chunk_count)
            .map(|index| {
                json!({
                    "index": index,
                    "start": index * 10,
                    "end": index * 10 + 9,
                    "lineStart": index + 1,
                    "lineEnd": index + 1,
                    "text": format!("chunk {index}"),
                })
            })
            .collect();
        std::fs::write(
            cache_dir.join(format!("{FILE_ID}.json")),
            json!({
                "name": "a.txt",
                "kind": "txt",
                "type": "text/plain",
                "size": 100,
                "charCount": 90,
                "chunks": chunks,
            })
            .to_string(),
        )
        .unwrap();
        let env = EnvGuard::set(&[
            (
                "DEEPSEEK_INFRA_ROOT",
                root.path().to_str().expect("utf-8 temp path").to_string(),
            ),
            ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ]);
        let static_root = tempfile::tempdir().expect("a static root");
        std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
        std::fs::write(
            static_root.path().join("ui/index.html"),
            "<!doctype html><main>native ui</main>",
        )
        .unwrap();
        let app = create_production_app(static_root.path()).expect("production app");
        Self {
            _root: root,
            _env: env,
            app,
        }
    }

    async fn post(&self, uri: &str, payload: Value) -> (StatusCode, Value) {
        let request = Request::builder()
            .method("POST")
            .uri(uri)
            .header(header::CONTENT_TYPE, "application/json")
            .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
            .body(Body::from(payload.to_string()))
            .unwrap();
        let response = self.app.clone().oneshot(request).await.unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (
            status,
            serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        )
    }
}

#[tokio::test]
async fn the_reader_serves_a_one_based_window() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new(20);

    // No `chunkStart`/`chunkCount`: the defaults are 1 and 6.
    let (status, body) = fixture
        .post("/api/file-reader", json!({"fileId": FILE_ID}))
        .await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["window"]["chunkStart"], 1);
    assert_eq!(body["window"]["chunkEnd"], 6);
    assert_eq!(body["window"]["chunkCount"], 6);
    assert_eq!(body["window"]["totalChunks"], 20);
    assert_eq!(body["window"]["hasPrevious"], false);
    assert_eq!(body["window"]["hasNext"], true);
    assert_eq!(body["chunks"][0]["index"], 1);
    assert_eq!(body["chunks"][0]["text"], "chunk 0");
    assert_eq!(body["chunks"][5]["index"], 6);
    assert_eq!(body["file"]["name"], "a.txt");
    assert_eq!(body["file"]["kind"], "txt");
    assert_eq!(body["file"]["chunkCount"], 20);
    assert_eq!(body["file"]["fileId"], FILE_ID);

    // A later window, and the last one has no next.
    let (status, body) = fixture
        .post(
            "/api/file-reader",
            json!({"fileId": FILE_ID, "chunkStart": 19, "chunkCount": 6}),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["window"]["chunkStart"], 19);
    assert_eq!(body["window"]["chunkEnd"], 20);
    assert_eq!(body["window"]["chunkCount"], 2);
    assert_eq!(body["window"]["hasPrevious"], true);
    assert_eq!(body["window"]["hasNext"], false);
    assert_eq!(body["chunks"][0]["index"], 19);
}

#[tokio::test]
async fn the_reader_defaults_on_falsy_values_and_refuses_non_numeric_ones() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new(20);

    // `chunkStart or 1` / `chunkCount or 6`: null, 0 and "" all take the default.
    for falsy in [Value::Null, json!(0), json!("")] {
        let (status, body) = fixture
            .post(
                "/api/file-reader",
                json!({"fileId": FILE_ID, "chunkStart": falsy, "chunkCount": falsy}),
            )
            .await;
        assert_eq!(status, StatusCode::OK, "falsy={falsy} body: {body}");
        assert_eq!(body["window"]["chunkStart"], 1, "falsy={falsy}");
        assert_eq!(body["window"]["chunkCount"], 6, "falsy={falsy}");
    }

    // A non-numeric value is the oracle's 400, with its own message per field.
    let (status, body) = fixture
        .post(
            "/api/file-reader",
            json!({"fileId": FILE_ID, "chunkStart": "x"}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_payload");
    assert_eq!(body["error"], "Invalid reader start");

    let (status, body) = fixture
        .post(
            "/api/file-reader",
            json!({"fileId": FILE_ID, "chunkCount": "x"}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["error"], "Invalid reader count");

    // The count is capped at 12.
    let (status, body) = fixture
        .post(
            "/api/file-reader",
            json!({"fileId": FILE_ID, "chunkCount": 99}),
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["window"]["chunkCount"], 12);
}

#[tokio::test]
async fn an_empty_chunk_list_is_its_own_shape() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new(0);

    let (status, body) = fixture
        .post("/api/file-reader", json!({"fileId": FILE_ID}))
        .await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["window"]["chunkStart"], 0);
    assert_eq!(body["window"]["chunkEnd"], 0);
    assert_eq!(body["window"]["totalChunks"], 0);
    assert_eq!(body["window"]["hasPrevious"], false);
    assert_eq!(body["window"]["hasNext"], false);
    assert_eq!(body["chunks"], json!([]));
}

#[tokio::test]
async fn one_chunk_is_one_based_and_bounds_checked() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new(3);

    let (status, body) = fixture
        .post(
            "/api/file-chunk",
            json!({"fileId": FILE_ID, "chunkIndex": 2}),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["chunk"]["index"], 1);
    assert_eq!(body["chunk"]["text"], "chunk 1");
    assert_eq!(body["file"]["name"], "a.txt");
    assert_eq!(body["file"]["kind"], "txt");
    assert_eq!(body["file"]["fileId"], FILE_ID);
    assert_eq!(body["file"]["projectId"], "");

    // `chunkIndex` absent, null or 0 all mean the first chunk.
    for index in [Value::Null, json!(0)] {
        let (status, body) = fixture
            .post(
                "/api/file-chunk",
                json!({"fileId": FILE_ID, "chunkIndex": index}),
            )
            .await;
        assert_eq!(status, StatusCode::OK, "index={index}");
        assert_eq!(body["chunk"]["text"], "chunk 0");
    }
    let (status, body) = fixture
        .post("/api/file-chunk", json!({"fileId": FILE_ID}))
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["chunk"]["text"], "chunk 0");

    // Past the end is a 404; a non-numeric index is a 400.
    let (status, body) = fixture
        .post(
            "/api/file-chunk",
            json!({"fileId": FILE_ID, "chunkIndex": 4}),
        )
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    assert_eq!(body["code"], "not_found");
    assert_eq!(body["error"], "Chunk not found");

    let (status, body) = fixture
        .post(
            "/api/file-chunk",
            json!({"fileId": FILE_ID, "chunkIndex": "x"}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_payload");
    assert_eq!(body["error"], "Invalid chunk index");
}

#[tokio::test]
async fn an_unknown_file_id_is_the_oracles_400() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new(3);

    for uri in ["/api/file-reader", "/api/file-chunk"] {
        let (status, body) = fixture.post(uri, json!({"fileId": "../escape"})).await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "{uri}");
        assert_eq!(body["code"], "invalid_payload", "{uri}");
    }
    // A well-formed id with no index is the 410 the cache raises.
    let missing = "ffffffffffffffffffffffffffffffff";
    let (status, body) = fixture
        .post("/api/file-reader", json!({"fileId": missing}))
        .await;
    assert_eq!(status, StatusCode::GONE);
    assert_eq!(body["code"], "file_index_expired");
}

#[tokio::test]
async fn both_reader_routes_are_behind_the_production_auth_layer() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new(3);
    for uri in ["/api/file-reader", "/api/file-chunk"] {
        let request = Request::builder()
            .method("POST")
            .uri(uri)
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(json!({"fileId": FILE_ID}).to_string()))
            .unwrap();
        let response = fixture.app.clone().oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED, "{uri}");
    }
}
