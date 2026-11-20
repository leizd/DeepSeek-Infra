//! `GET /api/workspace/artifacts/{artifact_id}/download` and
//! `GET /api/workspace/exports/{export_id}/download` integration tests.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::json;
use tower::ServiceExt;

const TEST_TOKEN: &str = "workspace-download-test-token";
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

fn build_app_with_mode(
    root: &std::path::Path,
    mode: &str,
) -> (tempfile::TempDir, EnvGuard, axum::Router) {
    let static_root = tempfile::tempdir().unwrap();
    let ui = static_root.path().join("ui");
    std::fs::create_dir_all(&ui).unwrap();
    std::fs::write(ui.join("index.html"), "<main>ui</main>").unwrap();
    let mut pairs = vec![
        ("DEEPSEEK_INFRA_ROOT", root.to_string_lossy().into_owned()),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
    ];
    if !mode.is_empty() {
        pairs.push(("DEEPSEEK_RUNTIME_MODE", mode.to_string()));
    }
    let env = EnvGuard::set(&pairs);
    let app = create_production_app(static_root.path()).unwrap();
    (static_root, env, app)
}

fn build_app(root: &std::path::Path) -> (tempfile::TempDir, EnvGuard, axum::Router) {
    build_app_with_mode(root, "")
}

async fn call_json(
    app: &axum::Router,
    method: &str,
    uri: &str,
    payload: Option<&serde_json::Value>,
) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
    let builder = Request::builder()
        .method(method)
        .uri(uri)
        .header("host", "127.0.0.1")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
    let body = if let Some(val) = payload {
        let encoded = serde_json::to_vec(val).unwrap();
        let req = builder
            .header(header::CONTENT_TYPE, "application/json")
            .header(header::CONTENT_LENGTH, encoded.len().to_string())
            .body(Body::from(encoded))
            .unwrap();
        let response = app.clone().oneshot(req).await.unwrap();
        let status = response.status();
        let headers = response.headers().clone();
        let bytes = to_bytes(response.into_body(), 262_144)
            .await
            .unwrap()
            .to_vec();
        return (status, headers, bytes);
    } else {
        Body::empty()
    };
    let req = builder.body(body).unwrap();
    let response = app.clone().oneshot(req).await.unwrap();
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = to_bytes(response.into_body(), 262_144)
        .await
        .unwrap()
        .to_vec();
    (status, headers, bytes)
}

async fn call_raw(
    app: &axum::Router,
    method: &str,
    uri: &str,
) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header("host", "127.0.0.1")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = app.clone().oneshot(req).await.unwrap();
    let status = response.status();
    let headers = response.headers().clone();
    let bytes = to_bytes(response.into_body(), 65_536)
        .await
        .unwrap()
        .to_vec();
    (status, headers, bytes)
}

#[tokio::test]
async fn artifact_and_export_downloads_serve_files_without_creating_missing_stores() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static, _env, app) = build_app(root.path());

    let (miss_art, _, _) =
        call_raw(&app, "GET", "/api/workspace/artifacts/art_missing/download").await;
    assert_eq!(miss_art, StatusCode::NOT_FOUND);
    let (miss_exp, _, _) =
        call_raw(&app, "GET", "/api/workspace/exports/exp_missing/download").await;
    assert_eq!(miss_exp, StatusCode::NOT_FOUND);
    assert!(!root.path().join(".projects").exists());
    assert!(!root.path().join(".generated").exists());

    let (post_art, _, _) = call_raw(
        &app,
        "POST",
        "/api/workspace/artifacts/art_missing/download",
    )
    .await;
    assert_eq!(post_art, StatusCode::METHOD_NOT_ALLOWED);
    let (post_exp, _, _) =
        call_raw(&app, "POST", "/api/workspace/exports/exp_missing/download").await;
    assert_eq!(post_exp, StatusCode::METHOD_NOT_ALLOWED);

    let proj_dir = root.path().join(".projects").join("proj_1");
    std::fs::create_dir_all(&proj_dir).unwrap();
    std::fs::write(
        proj_dir.join("project.json"),
        serde_json::to_vec(
            &json!({"id": "proj_1", "name": "Demo", "createdAt": 1, "updatedAt": 2}),
        )
        .unwrap(),
    )
    .unwrap();
    let gen_dir = root.path().join(".generated");
    std::fs::create_dir_all(&gen_dir).unwrap();
    let art_file = gen_dir.join("report.md");
    std::fs::write(&art_file, b"# Hello Artifact\n").unwrap();
    std::fs::write(
        proj_dir.join("artifacts.json"),
        serde_json::to_vec(&json!({
            "artifacts": [{
                "artifactId": "art_1",
                "projectId": "proj_1",
                "type": "markdown",
                "title": "My Report",
                "path": ".generated/report.md",
                "createdAtMs": 1000,
                "updatedAtMs": 2000,
            }]
        }))
        .unwrap(),
    )
    .unwrap();

    let (art_s, art_h, art_bytes) = call_raw(
        &app,
        "GET",
        "/api/workspace/artifacts/art_1/download?projectId=proj_1",
    )
    .await;
    assert_eq!(art_s, StatusCode::OK);
    assert_eq!(art_bytes, b"# Hello Artifact\n");
    assert_eq!(art_h[header::CACHE_CONTROL], "no-store");
    assert!(
        art_h[header::CONTENT_DISPOSITION]
            .to_str()
            .unwrap()
            .contains("My-Report.md")
    );

    let (head_s, _, head_bytes) =
        call_raw(&app, "HEAD", "/api/workspace/artifacts/art_1/download").await;
    assert_eq!(head_s, StatusCode::OK);
    assert!(head_bytes.is_empty());

    let exports_dir = gen_dir.join("workspace-exports");
    std::fs::create_dir_all(&exports_dir).unwrap();
    let exp_file = exports_dir.join("bundle.json");
    std::fs::write(&exp_file, br#"{"bundle":true}"#).unwrap();
    std::fs::write(
        exports_dir.join("exports.json"),
        serde_json::to_vec(&json!({
            "exports": [{
                "exportId": "exp_1",
                "projectId": "",
                "filename": "workspace-bundle.json",
                "path": "bundle.json",
            }]
        }))
        .unwrap(),
    )
    .unwrap();

    let (exp_s, exp_h, exp_bytes) =
        call_raw(&app, "GET", "/api/workspace/exports/exp_1/download").await;
    assert_eq!(exp_s, StatusCode::OK);
    assert_eq!(exp_bytes, br#"{"bundle":true}"#);
    assert_eq!(exp_h[header::CONTENT_TYPE], "application/json");
    assert_eq!(exp_h[header::CACHE_CONTROL], "no-store");
    assert!(
        exp_h[header::CONTENT_DISPOSITION]
            .to_str()
            .unwrap()
            .contains("workspace-bundle.json")
    );
}

#[tokio::test]
async fn default_mode_refuses_create_export_without_touching_disk() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app_with_mode(root.path(), "python_authoritative");

    let (status, _, body) = call_json(
        &app,
        "POST",
        "/api/workspace/exports",
        Some(&json!({
            "kind": "conversation",
            "format": "markdown",
            "conversation": {"id": "c1", "title": "Deny", "messages": []}
        })),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    let parsed: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(parsed["code"], "NATIVE_GENERATED_FILES_WRITE_NOT_OWNED");
    assert!(!root.path().join(".generated").exists());
    assert!(!root.path().join(".projects").exists());
}

#[tokio::test]
async fn python_disabled_create_export_and_download_roundtrip() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app_with_mode(root.path(), "python_disabled");

    let (st_get, _, _) = call_json(&app, "GET", "/api/workspace/exports", None).await;
    assert_eq!(st_get, StatusCode::NOT_FOUND);
    let (st_del, _, _) = call_json(&app, "DELETE", "/api/workspace/exports", None).await;
    assert_eq!(st_del, StatusCode::METHOD_NOT_ALLOWED);

    let (st_inline, _, inline_bytes) = call_json(
        &app,
        "POST",
        "/api/workspace/exports",
        Some(&json!({
            "kind": "conversation",
            "format": "markdown",
            "conversation": {
                "id": "conv-inline",
                "title": "Inline",
                "messages": [{"role": "assistant", "content": "hello sk-1234567890abcdefghij"}]
            }
        })),
    )
    .await;
    assert_eq!(st_inline, StatusCode::OK);
    let inline_val: serde_json::Value = serde_json::from_slice(&inline_bytes).unwrap();
    let export_id = inline_val["export"]["exportId"].as_str().unwrap();
    assert_eq!(inline_val["export"]["kind"], "conversation");

    let (st_dl, dl_headers, dl_bytes) = call_raw(
        &app,
        "GET",
        &format!("/api/workspace/exports/{export_id}/download"),
    )
    .await;
    assert_eq!(st_dl, StatusCode::OK);
    assert_eq!(dl_headers[header::CONTENT_TYPE], "text/markdown");
    let dl_text = String::from_utf8(dl_bytes).unwrap();
    assert!(dl_text.contains("# Inline"));
    assert!(dl_text.contains("[redacted-secret]"));
    assert!(!dl_text.contains("sk-1234567890abcdefghij"));

    let (st_proj, _, proj_bytes) = call_json(
        &app,
        "POST",
        "/api/workspace/projects",
        Some(&json!({"name": "ExpProj"})),
    )
    .await;
    assert_eq!(st_proj, StatusCode::OK);
    let proj_val: serde_json::Value = serde_json::from_slice(&proj_bytes).unwrap();
    let pid = proj_val["project"]["id"].as_str().unwrap();

    let (st_pexp, _, pexp_bytes) = call_json(
        &app,
        "POST",
        "/api/workspace/exports",
        Some(&json!({"projectId": pid, "kind": "project", "format": "html"})),
    )
    .await;
    assert_eq!(st_pexp, StatusCode::OK);
    let pexp_val: serde_json::Value = serde_json::from_slice(&pexp_bytes).unwrap();
    let p_export_id = pexp_val["export"]["exportId"].as_str().unwrap();
    let (st_pdl, _, pdl_bytes) = call_raw(
        &app,
        "GET",
        &format!("/api/workspace/exports/{p_export_id}/download?projectId={pid}"),
    )
    .await;
    assert_eq!(st_pdl, StatusCode::OK);
    let html_body = String::from_utf8(pdl_bytes).unwrap();
    assert!(html_body.contains("# ExpProj"));
}
