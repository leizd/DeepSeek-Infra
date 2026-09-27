//! Real HTTP coverage for `POST /api/file-page-text`.
//!
//! The page rules are unit-tested in `deepseek-policy` and compared with the
//! unmodified oracle by `file_routes_parity_probe.py`. What this suite adds is that
//! the **production router** serves the route, that a legal page is the oracle's
//! body, and that an illegal request is refused without touching the cache.

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::SystemTime;

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "unit-file-page-text-token";
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

/// Every file under `root`, as `(relative path, bytes, mtime)`.
fn tree_snapshot(root: &Path) -> Vec<(String, Vec<u8>, SystemTime)> {
    fn walk(dir: &Path, root: &Path, out: &mut Vec<(String, Vec<u8>, SystemTime)>) {
        let mut entries: Vec<_> = std::fs::read_dir(dir)
            .unwrap()
            .map(|entry| entry.unwrap())
            .collect();
        entries.sort_by_key(|entry| entry.file_name());
        for entry in entries {
            let path = entry.path();
            if path.is_dir() {
                walk(&path, root, out);
            } else {
                let relative = path
                    .strip_prefix(root)
                    .unwrap()
                    .to_string_lossy()
                    .replace('\\', "/");
                let modified = std::fs::metadata(&path).unwrap().modified().unwrap();
                out.push((relative, std::fs::read(&path).unwrap(), modified));
            }
        }
    }
    let mut out = Vec::new();
    walk(root, root, &mut out);
    out
}

struct Fixture {
    root: tempfile::TempDir,
    _env: EnvGuard,
    app: axum::Router,
}

impl Fixture {
    fn new(index: Value) -> Self {
        let root = tempfile::tempdir().expect("a temp workspace root");
        let cache_dir = root.path().join(".file-cache");
        std::fs::create_dir_all(&cache_dir).unwrap();
        std::fs::write(cache_dir.join(format!("{FILE_ID}.json")), index.to_string()).unwrap();
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
            root,
            _env: env,
            app,
        }
    }

    fn raised() -> Self {
        Self::new(json!({
            "name": "a.pdf",
            "kind": "pdf",
            "type": "application/pdf",
            "size": 120,
            "charCount": 18,
            "pageCount": 2,
            "sourceAvailable": true,
            "pageTexts": [
                {"page": 1, "text": "page one"},
                {"page": 5, "text": "page five"},
            ],
            "chunks": [{"index": 0, "text": "chunk text"}],
        }))
    }

    async fn search(&self, query: &str) -> (StatusCode, Value) {
        let request = Request::builder()
            .method("GET")
            .uri(format!(
                "/api/file-page-search?fileId={FILE_ID}&query={query}"
            ))
            .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
            .body(Body::empty())
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

    async fn post(&self, payload: Value) -> (StatusCode, Value) {
        let request = Request::builder()
            .method("POST")
            .uri("/api/file-page-text")
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
async fn a_legal_page_is_the_extracted_text_and_writes_nothing() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::raised();
    let before = tree_snapshot(fixture.root.path());

    let (status, body) = fixture.post(json!({"fileId": FILE_ID, "page": 1})).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["file"]["name"], "a.pdf");
    assert_eq!(body["file"]["kind"], "pdf");
    assert_eq!(body["file"]["type"], "application/pdf");
    assert_eq!(body["file"]["size"], 120);
    assert_eq!(body["file"]["charCount"], 18);
    assert_eq!(body["file"]["chunkCount"], 1);
    assert_eq!(body["file"]["pageCount"], 2);
    assert_eq!(body["file"]["fileId"], FILE_ID);
    assert_eq!(body["file"]["projectId"], "");
    assert_eq!(body["file"]["sourceAvailable"], true);
    assert_eq!(body["page"]["index"], 1);
    assert_eq!(body["page"]["pageCount"], 5);
    assert_eq!(body["page"]["text"], "page one");
    assert_eq!(body["page"]["hasText"], true);
    assert_eq!(tree_snapshot(fixture.root.path()), before);

    // Past the raised count clamps to the last named page.
    let (status, body) = fixture.post(json!({"fileId": FILE_ID, "page": 99})).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["page"]["index"], 5);
    assert_eq!(body["page"]["text"], "page five");
    assert_eq!(body["page"]["hasText"], true);
    assert_eq!(tree_snapshot(fixture.root.path()), before);
}

#[tokio::test]
async fn a_page_without_text_falls_back_to_the_chunk_split() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::raised();

    // pageCount is raised to 5. The chunk text "chunk text" (10 chars) splits into
    // 2 characters per page, so page 3 is text[4:6] stripped to "k".
    let (status, body) = fixture.post(json!({"fileId": FILE_ID, "page": 3})).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["page"]["index"], 3);
    assert_eq!(body["page"]["pageCount"], 5);
    assert_eq!(body["page"]["text"], "k");
    assert_eq!(body["page"]["hasText"], true);
}

#[tokio::test]
async fn falsy_pages_default_and_illegal_pages_leave_the_cache_untouched() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::raised();
    let before = tree_snapshot(fixture.root.path());

    for falsy in [Value::Null, json!(0), json!(""), json!(false)] {
        let (status, body) = fixture
            .post(json!({"fileId": FILE_ID, "page": falsy}))
            .await;
        assert_eq!(status, StatusCode::OK, "falsy={falsy} body: {body}");
        assert_eq!(body["page"]["index"], 1, "falsy={falsy}");
        assert_eq!(body["page"]["text"], "page one", "falsy={falsy}");
    }
    let (status, body) = fixture.post(json!({"fileId": FILE_ID})).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["page"]["index"], 1);

    for refused in [json!("x"), json!("1.5"), json!("True")] {
        let (status, body) = fixture
            .post(json!({"fileId": FILE_ID, "page": refused}))
            .await;
        assert_eq!(
            status,
            StatusCode::BAD_REQUEST,
            "page={refused} body: {body}"
        );
        assert_eq!(body["code"], "invalid_payload");
        assert_eq!(body["error"], "Invalid page");
        assert!(body.get("page").is_none(), "page={refused}");
    }
    assert_eq!(tree_snapshot(fixture.root.path()), before);
}

#[tokio::test]
async fn an_empty_extraction_reports_one_page_without_text() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new(json!({"name": "b.txt", "chunks": []}));

    let (status, body) = fixture.post(json!({"fileId": FILE_ID, "page": 4})).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["page"]["index"], 1);
    assert_eq!(body["page"]["pageCount"], 1);
    assert_eq!(body["page"]["text"], "");
    assert_eq!(body["page"]["hasText"], false);
    assert_eq!(body["file"]["name"], "b.txt");
    assert_eq!(body["file"]["chunkCount"], 0);
}

#[tokio::test]
async fn a_bad_id_and_a_missing_index_are_refused_without_creating_a_file() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::raised();
    let before = tree_snapshot(fixture.root.path());

    let (status, body) = fixture
        .post(json!({"fileId": "../escape", "page": 1}))
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_payload");
    assert_eq!(body["error"], "Invalid file id");

    let missing = "ffffffffffffffffffffffffffffffff";
    let (status, body) = fixture.post(json!({"fileId": missing, "page": 1})).await;
    assert_eq!(status, StatusCode::GONE);
    assert_eq!(body["code"], "file_index_expired");
    assert_eq!(
        body["error"],
        "Uploaded file index has expired or is missing"
    );
    assert!(
        !fixture
            .root
            .path()
            .join(".file-cache")
            .join(format!("{missing}.json"))
            .exists()
    );
    assert_eq!(tree_snapshot(fixture.root.path()), before);
}

#[tokio::test]
async fn a_project_scoped_page_is_read_from_its_own_directory() {
    let _env_lock = EnvLock::acquire();
    let root = tempfile::tempdir().expect("a temp workspace root");
    let global = root.path().join(".file-cache");
    std::fs::create_dir_all(&global).unwrap();
    std::fs::write(
        global.join(format!("{FILE_ID}.json")),
        json!({"name": "global.txt", "pageCount": 1, "pageTexts": [{"page": 1, "text": "global"}]})
            .to_string(),
    )
    .unwrap();
    let project_dir = root.path().join(".projects/proj1/files");
    std::fs::create_dir_all(&project_dir).unwrap();
    std::fs::write(
        project_dir.join(format!("{FILE_ID}.json")),
        json!({"name": "scoped.pdf", "kind": "pdf", "pageCount": 1, "pageTexts": [{"page": 1, "text": "scoped"}]}).to_string(),
    )
    .unwrap();
    let _env = EnvGuard::set(&[
        (
            "DEEPSEEK_INFRA_ROOT",
            root.path().to_str().expect("utf-8 temp path").to_string(),
        ),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
    ]);
    let static_root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
    let app = create_production_app(static_root.path()).unwrap();
    let before = tree_snapshot(root.path());

    let request = Request::builder()
        .method("POST")
        .uri("/api/file-page-text")
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::from(
            json!({"fileId": FILE_ID, "projectId": "proj1", "page": 1}).to_string(),
        ))
        .unwrap();
    let response = app.clone().oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body["page"]["text"], "scoped");
    assert_eq!(body["file"]["name"], "scoped.pdf");
    assert_eq!(body["file"]["projectId"], "proj1");
    assert_ne!(body["page"]["text"], "global");

    let request = Request::builder()
        .method("POST")
        .uri("/api/file-page-text")
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::from(
            json!({"fileId": FILE_ID, "projectId": "../escape", "page": 1}).to_string(),
        ))
        .unwrap();
    let response = app.oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    let body: Value = serde_json::from_slice(
        &axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap(),
    )
    .unwrap();
    assert_eq!(body["code"], "invalid_payload");
    assert_eq!(body["error"], "Invalid project id");
    assert_eq!(tree_snapshot(root.path()), before);
}

#[tokio::test]
async fn a_page_search_finds_a_match_and_a_blank_query_writes_nothing() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::raised();
    let before = tree_snapshot(fixture.root.path());

    let (status, body) = fixture.search("page").await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["ok"], true);
    assert_eq!(body["query"], "page");
    assert_eq!(body["pageCount"], 5);
    assert_eq!(body["truncated"], false);
    assert_eq!(body["matches"][0]["page"], 1);
    assert_eq!(body["matches"][0]["text"], "page");
    assert_eq!(body["matches"][1]["page"], 5);
    assert_eq!(body["matches"][1]["text"], "page");
    assert_eq!(tree_snapshot(fixture.root.path()), before);

    let (status, body) = fixture.search("").await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "body: {body}");
    assert_eq!(body["code"], "invalid_payload");
    assert_eq!(body["error"], "Search query is required");
    assert!(body.get("matches").is_none());
    assert_eq!(tree_snapshot(fixture.root.path()), before);

    let request = Request::builder()
        .method("GET")
        .uri(format!("/api/file-page-search?fileId={FILE_ID}&query=page"))
        .body(Body::empty())
        .unwrap();
    let response = fixture.app.clone().oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(tree_snapshot(fixture.root.path()), before);
}

#[tokio::test]
async fn the_page_route_is_behind_the_production_auth_layer() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::raised();
    let before = tree_snapshot(fixture.root.path());
    let request = Request::builder()
        .method("POST")
        .uri("/api/file-page-text")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(
            json!({"fileId": FILE_ID, "page": 1}).to_string(),
        ))
        .unwrap();
    let response = fixture.app.clone().oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(tree_snapshot(fixture.root.path()), before);
}
