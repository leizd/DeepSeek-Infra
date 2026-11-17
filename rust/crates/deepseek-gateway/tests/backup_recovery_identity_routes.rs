//! `POST /api/workspace/backups/recovery-identities` through the production router.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::Json;
use axum::Router;
use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode};
use axum::routing::get;
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "recovery-identity-route-token";
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

async fn send(
    app: &Router,
    method: &str,
    path: &str,
    host: &str,
    token: Option<&str>,
) -> (StatusCode, Value) {
    let mut builder = Request::builder()
        .method(method)
        .uri(path)
        .header("host", host);
    if let Some(tok) = token {
        builder = builder.header("authorization", format!("Bearer {tok}"));
    }
    let response = app
        .clone()
        .oneshot(builder.body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 16_384).await.unwrap();
    let value = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, value)
}

#[tokio::test]
async fn recovery_identities_generates_valid_pair_enforces_auth_and_preserves_item_proxy() {
    let _lock = EnvLock::acquire();
    let upstream = Router::new().route(
        "/api/workspace/backups/recovery-identities",
        get(|| async {
            (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": "Invalid backup id", "code": "invalid_payload"})),
            )
        })
        .delete(|| async {
            (
                StatusCode::BAD_REQUEST,
                Json(json!({"error": "Invalid backup id", "code": "invalid_payload"})),
            )
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, upstream).await.unwrap() });

    let root = tempfile::tempdir().unwrap();
    let static_root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
    let _env = EnvGuard::set(&[
        (
            "DEEPSEEK_INFRA_ROOT",
            root.path().to_string_lossy().into_owned(),
        ),
        ("AUTH_DISABLED", "false".to_string()),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", "127.0.0.1".to_string()),
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled".to_string()),
        ("GO_CONTROL_ADDR", format!("http://{addr}")),
        ("DEEPSEEK_GO_CONTROL_URL", String::new()),
    ]);
    let app = create_production_app(static_root.path()).unwrap();

    // Host and auth enforcement.
    let (status, _) = send(
        &app,
        "POST",
        "/api/workspace/backups/recovery-identities",
        "foreign.example",
        Some(TEST_TOKEN),
    )
    .await;
    assert_eq!(status, StatusCode::FORBIDDEN);

    let (status, _) = send(
        &app,
        "POST",
        "/api/workspace/backups/recovery-identities",
        "127.0.0.1",
        None,
    )
    .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);

    // Valid POST generates distinct Age x25519 pairs with zero disk side effects.
    let (status1, body1) = send(
        &app,
        "POST",
        "/api/workspace/backups/recovery-identities",
        "127.0.0.1",
        Some(TEST_TOKEN),
    )
    .await;
    let (status2, body2) = send(
        &app,
        "POST",
        "/api/workspace/backups/recovery-identities",
        "127.0.0.1",
        Some(TEST_TOKEN),
    )
    .await;
    assert_eq!(status1, StatusCode::OK);
    assert_eq!(status2, StatusCode::OK);
    assert_eq!(body1["ok"], true);
    assert_eq!(body1["displayedOnce"], true);
    let id1 = body1["identity"].as_str().unwrap();
    let rec1 = body1["recipient"].as_str().unwrap();
    assert!(id1.starts_with("AGE-SECRET-KEY-1"));
    assert!(rec1.starts_with("age1"));
    assert_ne!(id1, body2["identity"].as_str().unwrap());
    assert_ne!(rec1, body2["recipient"].as_str().unwrap());
    assert!(!root.path().join(".backups").exists());
    assert!(!root.path().join(".restore-staging").exists());

    // Method parity: GET/DELETE proxy to /api/workspace/backups/:backup_id, PUT/PATCH 405, OPTIONS 204.
    let (get_s, get_b) = send(
        &app,
        "GET",
        "/api/workspace/backups/recovery-identities",
        "127.0.0.1",
        Some(TEST_TOKEN),
    )
    .await;
    assert_eq!(get_s, StatusCode::BAD_REQUEST);
    assert_eq!(get_b["code"], "invalid_payload");

    let (del_s, _) = send(
        &app,
        "DELETE",
        "/api/workspace/backups/recovery-identities",
        "127.0.0.1",
        Some(TEST_TOKEN),
    )
    .await;
    assert_eq!(del_s, StatusCode::BAD_REQUEST);

    for disallowed in ["PUT", "PATCH"] {
        let (s, _) = send(
            &app,
            disallowed,
            "/api/workspace/backups/recovery-identities",
            "127.0.0.1",
            Some(TEST_TOKEN),
        )
        .await;
        assert_eq!(s, StatusCode::METHOD_NOT_ALLOWED);
    }

    let (opt_s, _) = send(
        &app,
        "OPTIONS",
        "/api/workspace/backups/recovery-identities",
        "127.0.0.1",
        Some(TEST_TOKEN),
    )
    .await;
    assert_eq!(opt_s, StatusCode::NO_CONTENT);

    server.abort();
}
