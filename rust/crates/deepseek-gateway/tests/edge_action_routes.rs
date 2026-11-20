//! `POST /api/edge/reload` and `POST /api/edge/route-preview` integration tests.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "edge-action-route-token";
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

fn build_app(static_root: &std::path::Path) -> axum::Router {
    let ui = static_root.join("ui");
    std::fs::create_dir_all(&ui).unwrap();
    std::fs::write(ui.join("index.html"), "<main>ui</main>").unwrap();
    create_production_app(static_root).unwrap()
}

async fn call_route(
    app: &axum::Router,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let raw = match body {
        Some(val) => serde_json::to_vec(&val).unwrap(),
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
    let bytes = to_bytes(response.into_body(), 32_768).await.unwrap();
    let parsed = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, parsed)
}

#[tokio::test]
async fn edge_reload_handles_unload_reload_invalid_and_methods() {
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
        ("EDGE_INFERENCE_ENABLED", "1".to_string()),
        ("EDGE_PROVIDER", "fake".to_string()),
        ("EDGE_INFERENCE_PROVIDER", String::new()),
    ]);
    let app = build_app(static_root.path());

    let (get_status, _) = call_route(&app, "GET", "/api/edge/reload", None).await;
    assert_eq!(get_status, StatusCode::NOT_FOUND);

    let (del_status, _) = call_route(&app, "DELETE", "/api/edge/reload", None).await;
    assert_eq!(del_status, StatusCode::METHOD_NOT_ALLOWED);

    let (unload_status, unload_body) =
        call_route(&app, "POST", "/api/edge/reload", Some(json!({}))).await;
    assert_eq!(unload_status, StatusCode::OK);
    assert_eq!(unload_body["ok"], true);
    assert_eq!(unload_body["edgeInference"]["loaded"], false);
    assert_eq!(unload_body["edgeInference"]["provider"], "fake");

    let (reload_status, _) = call_route(
        &app,
        "POST",
        "/api/edge/reload",
        Some(json!({"action": "reload"})),
    )
    .await;
    assert_eq!(reload_status, StatusCode::OK);

    let (bad_status, bad_body) = call_route(
        &app,
        "POST",
        "/api/edge/reload",
        Some(json!({"action": "drop-db"})),
    )
    .await;
    assert_eq!(bad_status, StatusCode::BAD_REQUEST);
    assert_eq!(bad_body["code"], "invalid_payload");
}

#[tokio::test]
async fn edge_route_preview_covers_decision_tree_and_409_refusal() {
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
        ("DEEPSEEK_API_KEY", "sk-test-cloud-key".to_string()),
        ("EDGE_INFERENCE_ENABLED", "1".to_string()),
        ("EDGE_PROVIDER", "fake".to_string()),
        ("EDGE_INFERENCE_PROVIDER", String::new()),
        ("EDGE_MODE", "auto".to_string()),
    ]);
    let app = build_app(static_root.path());

    let (s1, b1) = call_route(
        &app,
        "POST",
        "/api/edge/route-preview",
        Some(json!({
            "edgeMode": "cloud",
            "messages": [{"role": "user", "content": "hi"}],
        })),
    )
    .await;
    assert_eq!(s1, StatusCode::OK);
    assert_eq!(b1["useEdge"], false);
    assert_eq!(b1["reason"], "cloud_forced");

    let (s2, b2) = call_route(
        &app,
        "POST",
        "/api/edge/route-preview",
        Some(json!({
            "messages": [{"role": "user", "content": "hello"}],
        })),
    )
    .await;
    assert_eq!(s2, StatusCode::OK);
    assert_eq!(b2["useEdge"], true);
    assert_eq!(b2["reason"], "simple_task_local");

    let (s3, b3) = call_route(
        &app,
        "POST",
        "/api/edge/route-preview",
        Some(json!({
            "messages": [{"role": "user", "content": "please write python code for quicksort"}],
        })),
    )
    .await;
    assert_eq!(s3, StatusCode::OK);
    assert_eq!(b3["useEdge"], false);
    assert_eq!(b3["reason"], "complex_task_cloud");

    let (s4, b4) = call_route(
        &app,
        "POST",
        "/api/edge/route-preview",
        Some(json!({
            "agentMode": true,
            "messages": [{"role": "user", "content": "hi"}],
        })),
    )
    .await;
    assert_eq!(s4, StatusCode::OK);
    assert_eq!(b4["useEdge"], false);
    assert_eq!(b4["reason"], "unsupported_payload");

    let (s5, b5) = call_route(
        &app,
        "POST",
        "/api/edge/route-preview",
        Some(json!({
            "edgeMode": "local",
            "edgeProvider": "unknown_backend",
            "messages": [{"role": "user", "content": "hi"}],
        })),
    )
    .await;
    assert_eq!(s5, StatusCode::CONFLICT);
    assert_eq!(b5["code"], "invalid_payload");
    assert!(
        b5["error"]
            .as_str()
            .unwrap()
            .contains("Unsupported edge inference provider")
    );
}
