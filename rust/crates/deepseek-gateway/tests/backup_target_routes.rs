//! Explicit Rust gateway route forwarding tests for `/api/workspace/backup-targets*`.

use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};

use axum::{
    Router,
    body::{Body, to_bytes},
    extract::{OriginalUri, State},
    http::{Method, Request, StatusCode, header},
    routing::any,
};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "target-proxy-test-token";
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

type RecordedRequests = Arc<Mutex<Vec<(String, String)>>>;

async fn start_stub_go() -> (String, RecordedRequests) {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let state = Arc::clone(&seen);
    let stub = Router::new()
        .route(
            "/api/*rest",
            any(
                |State(log): State<RecordedRequests>,
                 method: Method,
                 OriginalUri(uri): OriginalUri| async move {
                    log.lock()
                        .unwrap()
                        .push((method.as_str().to_string(), uri.path().to_string()));
                    (StatusCode::OK, axum::Json(json!({"proxied": true})))
                },
            ),
        )
        .with_state(state);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let _ = axum::serve(listener, stub).await;
    });
    (format!("http://{addr}"), seen)
}

#[tokio::test]
async fn backup_target_routes_proxy_explicitly_to_go() {
    let _lock = EnvLock::acquire();
    let (go_url, seen) = start_stub_go().await;
    let root = tempfile::tempdir().unwrap();
    let static_root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
    let _env = EnvGuard::set(&[
        (
            "DEEPSEEK_INFRA_ROOT",
            root.path().to_string_lossy().into_owned(),
        ),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
        ("GO_CONTROL_ADDR", go_url),
        ("DEEPSEEK_GO_CONTROL_URL", String::new()),
    ]);
    let app = create_production_app(static_root.path()).unwrap();

    for (method, path) in [
        ("GET", "/api/workspace/backup-targets"),
        ("POST", "/api/workspace/backup-targets"),
        ("POST", "/api/workspace/backup-targets/register-new"),
        ("DELETE", "/api/workspace/backup-targets/target_1"),
        ("POST", "/api/workspace/backup-targets/target_1/adopt"),
        ("POST", "/api/workspace/backup-targets/target_1/activate"),
        ("POST", "/api/workspace/backup-targets/target_1/probe"),
    ] {
        let req = Request::builder()
            .method(method)
            .uri(path)
            .header("host", "127.0.0.1")
            .header("authorization", format!("Bearer {TEST_TOKEN}"))
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from("{}"))
            .unwrap();
        let resp = app.clone().oneshot(req).await.unwrap();
        assert_eq!(resp.status(), StatusCode::OK, "{method} {path}");
        let raw = to_bytes(resp.into_body(), 4096).await.unwrap();
        let val: Value = serde_json::from_slice(&raw).unwrap();
        assert_eq!(val["proxied"], Value::Bool(true), "{method} {path}");
    }

    let recorded = seen.lock().unwrap().clone();
    assert_eq!(
        recorded,
        vec![
            ("GET".into(), "/api/workspace/backup-targets".into()),
            ("POST".into(), "/api/workspace/backup-targets".into()),
            (
                "POST".into(),
                "/api/workspace/backup-targets/register-new".into()
            ),
            (
                "DELETE".into(),
                "/api/workspace/backup-targets/target_1".into()
            ),
            (
                "POST".into(),
                "/api/workspace/backup-targets/target_1/adopt".into()
            ),
            (
                "POST".into(),
                "/api/workspace/backup-targets/target_1/activate".into()
            ),
            (
                "POST".into(),
                "/api/workspace/backup-targets/target_1/probe".into()
            ),
        ]
    );
}
