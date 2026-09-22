//! Real HTTP coverage for `GET /api/download`.
//!
//! The route serves the `downloadUrl` the document/deck/mindmap tools hand back, and it
//! was a `503` on the native edge (the Go `/api/*` catch-all) before this slice. These
//! cases drive the **production** router, so the registration order ahead of that
//! catch-all, the auth layer and the response headers are all measured rather than
//! assumed.
//!
//! # What is actually being checked
//!
//! 1. **The bytes that arrive are the bytes on disk.** A route that reported `200` with
//!    an empty body would pass a status-only assertion.
//! 2. **The id is the only thing that reaches a path.** `../` and a wrong-length id must
//!    be `404`, not a read of a file outside `.generated/`.
//! 3. **The disposition rule is the oracle's**: `inline` only for `.svg` *and* a truthy
//!    `inline` parameter, which the web layer parses as a string over `1/true/yes/on`.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::Value;
use tower::ServiceExt;

const TEST_TOKEN: &str = "unit-download-route-token";

/// `DEEPSEEK_INFRA_ROOT` is process-wide, so the cases that set it must not overlap.
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

/// A workspace with one generated file, plus the production app bound to it.
///
/// The fixture owns its [`EnvGuard`], so `DEEPSEEK_INFRA_ROOT` and `AUTH_TOKEN` are set
/// before the router is built — `DownloadRouteState::from_env` reads the root at
/// construction — and restored when the case ends. Holding the guard here rather than in
/// each test is what keeps the two in the right order.
struct Fixture {
    _root: tempfile::TempDir,
    _env: EnvGuard,
    app: axum::Router,
}

impl Fixture {
    fn new(extension: &str, body: &[u8], id: &str) -> Self {
        let root = tempfile::tempdir().expect("a temp workspace root");
        let generated = root.path().join(".generated");
        std::fs::create_dir_all(&generated).unwrap();
        std::fs::write(generated.join(format!("{id}.{extension}")), body).unwrap();
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

    async fn get(&self, uri: &str) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
        let request = Request::builder()
            .method("GET")
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

/// A 32-character lowercase-hex id, which is what the oracle accepts.
const ID: &str = "0123456789abcdef0123456789abcdef";

fn body_of(bytes: &[u8]) -> Value {
    serde_json::from_slice(bytes).unwrap_or(Value::Null)
}

#[tokio::test]
async fn a_generated_svg_is_served_with_the_oracles_headers() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new("svg", b"<svg><rect/></svg>", ID);

    let (status, headers, bytes) = fixture.get(&format!("/api/download?id={ID}")).await;
    assert_eq!(status, StatusCode::OK);
    // The bytes on the wire are the bytes on disk.
    assert_eq!(bytes, b"<svg><rect/></svg>");
    assert_eq!(headers[header::CONTENT_TYPE], "image/svg+xml");
    assert_eq!(
        headers[header::CONTENT_DISPOSITION],
        "attachment; filename=\"mindmap.svg\""
    );
    assert_eq!(headers[header::CACHE_CONTROL], "no-store");
}

#[tokio::test]
async fn only_an_svg_with_a_truthy_inline_parameter_is_rendered_in_place() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new("svg", b"<svg/>", ID);

    // Absent: attachment.
    let (_, headers, _) = fixture.get(&format!("/api/download?id={ID}")).await;
    assert_eq!(
        headers[header::CONTENT_DISPOSITION],
        "attachment; filename=\"mindmap.svg\""
    );
    // `inline=1`: inline.
    let (_, headers, _) = fixture
        .get(&format!("/api/download?id={ID}&inline=1"))
        .await;
    assert_eq!(
        headers[header::CONTENT_DISPOSITION],
        "inline; filename=\"mindmap.svg\""
    );
    // `inline=false` is *false*: the web layer's `truthy` is a string parse over
    // `1/true/yes/on`, so anything else downloads. The first version of this test
    // asserted the opposite — it had used Python truthiness — and was corrected
    // against the oracle.
    let (_, headers, _) = fixture
        .get(&format!("/api/download?id={ID}&inline=false"))
        .await;
    assert_eq!(
        headers[header::CONTENT_DISPOSITION],
        "attachment; filename=\"mindmap.svg\""
    );
    // The values the oracle's helper accepts are all true.
    for value in ["yes", "on", "TRUE", "1"] {
        let (_, headers, _) = fixture
            .get(&format!("/api/download?id={ID}&inline={value}"))
            .await;
        assert_eq!(
            headers[header::CONTENT_DISPOSITION],
            "inline; filename=\"mindmap.svg\"",
            "inline={value} must render in place"
        );
    }
    // An empty value is the oracle's falsy empty string, so it stays an attachment.
    let (_, headers, _) = fixture.get(&format!("/api/download?id={ID}&inline=")).await;
    assert_eq!(
        headers[header::CONTENT_DISPOSITION],
        "attachment; filename=\"mindmap.svg\""
    );
}

#[tokio::test]
async fn a_non_svg_is_never_inline_even_when_asked() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new("pptx", b"PK\x03\x04deck", ID);

    let (status, headers, bytes) = fixture
        .get(&format!("/api/download?id={ID}&inline=1"))
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(bytes, b"PK\x03\x04deck");
    assert_eq!(
        headers[header::CONTENT_TYPE],
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    );
    assert_eq!(
        headers[header::CONTENT_DISPOSITION],
        "attachment; filename=\"presentation.pptx\""
    );
}

#[tokio::test]
async fn every_registered_type_gets_the_oracles_name_and_media_type() {
    let _env_lock = EnvLock::acquire();
    for (extension, media_type, name) in [
        (
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "document.docx",
        ),
        ("pdf", "application/pdf", "document.pdf"),
        ("md", "text/markdown; charset=utf-8", "notes.md"),
    ] {
        let fixture = Fixture::new(extension, b"payload", ID);
        let (status, headers, bytes) = fixture.get(&format!("/api/download?id={ID}")).await;
        assert_eq!(status, StatusCode::OK, "{extension}");
        assert_eq!(bytes, b"payload", "{extension}");
        assert_eq!(headers[header::CONTENT_TYPE], media_type, "{extension}");
        assert_eq!(
            headers[header::CONTENT_DISPOSITION],
            format!("attachment; filename=\"{name}\""),
            "{extension}"
        );
    }
}

#[tokio::test]
async fn an_unknown_or_traversing_id_is_the_oracles_404() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new("svg", b"<svg/>", ID);

    // Write a file *outside* `.generated/` that a traversal would reach.
    std::fs::write(fixture._root.path().join("secret.svg"), b"secret").unwrap();

    for uri in [
        "/api/download".to_string(),
        "/api/download?id=".to_string(),
        "/api/download?id=deadbeef".to_string(),
        "/api/download?id=0123456789ABCDEF0123456789ABCDEF".to_string(),
        // A traversal cannot be expressed as a valid id, so it is simply not found.
        "/api/download?id=../secret".to_string(),
        "/api/download?id=..%2Fsecret".to_string(),
        "/api/download?id=0123456789abcdef0123456789abcde%2F..".to_string(),
    ] {
        let (status, _, bytes) = fixture.get(&uri).await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{uri}");
        assert_eq!(body_of(&bytes)["code"], "not_found", "{uri}");
        assert_eq!(
            body_of(&bytes)["error"],
            "File does not exist or has expired",
            "{uri}"
        );
        assert!(
            !bytes
                .windows(6)
                .any(|window| window == b"secret".as_slice()),
            "{uri} leaked a file outside .generated/"
        );
    }
}

#[tokio::test]
async fn the_download_route_is_behind_the_production_auth_layer() {
    let _env_lock = EnvLock::acquire();
    let fixture = Fixture::new("svg", b"<svg/>", ID);
    let request = Request::builder()
        .uri(format!("/api/download?id={ID}"))
        .body(Body::empty())
        .unwrap();
    let response = fixture.app.clone().oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}
