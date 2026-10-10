//! Real HTTP coverage for `GET /api/file-source`.
//!
//! The helper rules (`clean_filename`, `content_disposition_header`,
//! `original_file_media_type`, `cached_file_source`) are pinned in `deepseek-policy` and
//! compared with the oracle by `tasks/native-runtime/file_routes_parity_probe.py`. What
//! this suite adds is that the **production router** serves the route, that the bytes on
//! the wire are the bytes on disk, and that the four headers are the oracle's.
//!
//! # What is actually being checked
//!
//! 1. **The original bytes, not the extracted text.** The route's whole purpose is to
//!    hand back the upload; a route that served the index JSON would pass a status-only
//!    assertion.
//! 2. **A missing source is `410`, not `404`.** The index exists and the bytes are
//!    gone, which is a different repair for the user.
//! 3. **`download` is a string parse.** `download=false` is inline, because the web
//!    layer's `truthy` accepts only `1/true/yes/on`.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "unit-file-source-token";
/// A 32-character lowercase-hex id, which is what the oracle accepts.
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

/// A workspace with one cached file, plus the production router bound to it.
struct Fixture {
    root: tempfile::TempDir,
    _env: EnvGuard,
    app: axum::Router,
}

impl Fixture {
    /// `index` is the cached-file JSON; `source` is the original bytes, or `None` to
    /// leave the source missing.
    fn new(index: Value, source: Option<&[u8]>) -> Self {
        let root = tempfile::tempdir().expect("a temp workspace root");
        let cache_dir = root.path().join(".file-cache");
        std::fs::create_dir_all(&cache_dir).unwrap();
        std::fs::write(cache_dir.join(format!("{FILE_ID}.json")), index.to_string()).unwrap();
        if let Some(bytes) = source {
            std::fs::write(cache_dir.join(format!("{FILE_ID}.source")), bytes).unwrap();
        }
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

    async fn get(&self, uri: &str) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
        let request = Request::builder()
            .uri(uri)
            .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
            .body(Body::empty())
            .unwrap();
        let response = self.app.clone().oneshot(request).await.unwrap();
        let status = response.status();
        let headers = response.headers().clone();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (status, headers, bytes.to_vec())
    }
}

#[tokio::test]
async fn the_original_bytes_are_served_with_the_oracles_headers() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new(
        json!({"name": "report.pdf", "kind": "pdf", "type": "application/pdf"}),
        Some(b"%PDF-1.4 original bytes"),
    );

    let (status, headers, bytes) = fixture
        .get(&format!("/api/file-source?fileId={FILE_ID}"))
        .await;
    assert_eq!(status, StatusCode::OK);
    // The original bytes, not the index JSON.
    assert_eq!(bytes, b"%PDF-1.4 original bytes");
    assert_eq!(headers[header::CONTENT_TYPE], "application/pdf");
    assert_eq!(headers["x-content-type-options"], "nosniff");
    assert_eq!(headers[header::CACHE_CONTROL], "no-store");
    assert_eq!(
        headers[header::CONTENT_DISPOSITION],
        "inline; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"
    );
}

#[tokio::test]
async fn the_download_parameter_is_the_web_layers_string_parse() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new(
        json!({"name": "notes.txt", "kind": "txt", "type": "text/plain"}),
        Some(b"hello"),
    );

    // Absent: inline. `download=false` is *also* inline, because `truthy` accepts only
    // `1/true/yes/on` — this is the case Python truthiness gets wrong.
    for suffix in [
        "",
        "&download=",
        "&download=false",
        "&download=0",
        "&download=no",
    ] {
        let (_, headers, _) = fixture
            .get(&format!("/api/file-source?fileId={FILE_ID}{suffix}"))
            .await;
        assert!(
            headers[header::CONTENT_DISPOSITION]
                .to_str()
                .unwrap_or_default()
                .starts_with("inline;"),
            "download{suffix} must be inline"
        );
    }
    for suffix in [
        "&download=1",
        "&download=true",
        "&download=yes",
        "&download=on",
    ] {
        let (_, headers, _) = fixture
            .get(&format!("/api/file-source?fileId={FILE_ID}{suffix}"))
            .await;
        assert!(
            headers[header::CONTENT_DISPOSITION]
                .to_str()
                .unwrap_or_default()
                .starts_with("attachment;"),
            "download{suffix} must be an attachment"
        );
    }
}

#[tokio::test]
async fn the_media_type_follows_the_cached_index() {
    let _env_lock = EnvLock::acquire();
    for (index, expected) in [
        (
            json!({"name": "a.md", "kind": "md", "type": "text/markdown"}),
            "text/markdown; charset=utf-8",
        ),
        (
            json!({"name": "a.bin", "kind": "zip", "type": "application/zip"}),
            "application/octet-stream",
        ),
        (
            json!({"name": "a.png", "kind": "image", "type": "image/png"}),
            "image/png",
        ),
    ] {
        let fixture = Fixture::new(index, Some(b"payload"));
        let (status, headers, bytes) = fixture
            .get(&format!("/api/file-source?fileId={FILE_ID}"))
            .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(bytes, b"payload");
        assert_eq!(headers[header::CONTENT_TYPE], expected);
    }
}

#[tokio::test]
async fn a_missing_source_is_a_410_and_a_bad_id_is_a_400() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new(json!({"name": "gone.pdf", "kind": "pdf"}), None);

    let (status, _, bytes) = fixture
        .get(&format!("/api/file-source?fileId={FILE_ID}"))
        .await;
    assert_eq!(status, StatusCode::GONE, "a missing source is 410, not 404");
    let body: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    assert_eq!(body["code"], "file_index_expired");
    assert_eq!(
        body["error"],
        "Original uploaded file has expired or is missing"
    );

    for file_id in [
        "",
        "deadbeef",
        "../escape",
        "0123456789ABCDEF0123456789ABCDEF",
    ] {
        let (status, _, bytes) = fixture
            .get(&format!("/api/file-source?fileId={file_id}"))
            .await;
        assert_eq!(status, StatusCode::BAD_REQUEST, "fileId={file_id}");
        let body: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        assert_eq!(body["code"], "invalid_payload", "fileId={file_id}");
    }
    // Nothing leaked from outside the cache directory.
    assert!(!fixture.root.path().join("secret").exists());
}

#[tokio::test]
async fn a_project_scoped_file_is_read_from_its_own_directory() {
    let _env_lock = EnvLock::acquire();
    let root = tempfile::tempdir().expect("a temp workspace root");
    let project_dir = root.path().join(".projects/proj1/files");
    std::fs::create_dir_all(&project_dir).unwrap();
    std::fs::write(
        project_dir.join(format!("{FILE_ID}.json")),
        json!({"name": "scoped.txt", "kind": "txt"}).to_string(),
    )
    .unwrap();
    std::fs::write(
        project_dir.join(format!("{FILE_ID}.source")),
        b"project bytes",
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

    let request = Request::builder()
        .uri(format!("/api/file-source?fileId={FILE_ID}&projectId=proj1"))
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = app.oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(bytes.as_ref(), b"project bytes");
}

#[tokio::test]
async fn the_file_source_route_is_behind_the_production_auth_layer() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new(json!({"name": "a.txt", "kind": "txt"}), Some(b"x"));
    let request = Request::builder()
        .uri(format!("/api/file-source?fileId={FILE_ID}"))
        .body(Body::empty())
        .unwrap();
    let response = fixture.app.clone().oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}
