//! `GET /api/workspace/projects/{project_id}/provenance` integration tests.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "workspace-provenance-test-token";
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
        ("DEEPSEEK_INFRA_ROOT", root.to_string_lossy().into_owned()),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
    ]);
    let app = create_production_app(static_root.path()).unwrap();
    (static_root, env, app)
}

async fn call_provenance(
    app: &axum::Router,
    method: &str,
    project_id: &str,
) -> (StatusCode, Value) {
    let req = Request::builder()
        .method(method)
        .uri(format!("/api/workspace/projects/{project_id}/provenance"))
        .header("host", "127.0.0.1")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = app.clone().oneshot(req).await.unwrap();
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
async fn project_provenance_builds_graph_without_creating_missing_stores() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static, _env, app) = build_app(root.path());

    let (miss_s, _) = call_provenance(&app, "GET", "missing_proj").await;
    assert_eq!(miss_s, StatusCode::NOT_FOUND);
    assert!(!root.path().join(".projects").exists());

    let proj_dir = root.path().join(".projects").join("proj_graph");
    std::fs::create_dir_all(&proj_dir).unwrap();
    std::fs::write(
        proj_dir.join("project.json"),
        serde_json::to_vec(&json!({
            "id": "proj_graph",
            "name": "Graph Project",
            "createdAt": 1000,
            "updatedAt": 2000,
            "conversations": [{
                "conversationId": "conv_1",
                "title": "Chat 1",
                "sourceRef": {"kind": "chat", "refId": "orig_ref"},
                "messages": [{"id": "msg_1", "role": "user"}]
            }]
        }))
        .unwrap(),
    )
    .unwrap();

    let exports_dir = proj_dir.join("exports");
    std::fs::create_dir_all(&exports_dir).unwrap();
    std::fs::write(
        exports_dir.join("exports.json"),
        serde_json::to_vec(&json!({
            "exports": [{
                "exportId": "exp_1",
                "filename": "graph.zip",
                "includes": {"conversationIds": ["conv_1"]},
                "createdAt": "2026-11-01T00:00:00Z"
            }]
        }))
        .unwrap(),
    )
    .unwrap();

    let (ok_s, body) = call_provenance(&app, "GET", "proj_graph").await;
    assert_eq!(ok_s, StatusCode::OK);
    assert_eq!(body["ok"], true);
    assert_eq!(body["projectId"], "proj_graph");
    assert!(body["summary"]["nodes"].as_u64().unwrap() >= 5);
    assert!(body["summary"]["edges"].as_u64().unwrap() >= 4);
    assert_eq!(body["summary"]["types"]["project"], 1);
    assert_eq!(body["summary"]["types"]["conversation"], 1);
    assert_eq!(body["summary"]["types"]["message"], 1);
    assert_eq!(body["summary"]["types"]["export"], 1);

    let (post_s, _) = call_provenance(&app, "POST", "proj_graph").await;
    assert_eq!(post_s, StatusCode::METHOD_NOT_ALLOWED);
}
