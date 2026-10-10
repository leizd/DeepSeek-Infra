use axum::{
    Json, Router,
    body::{Body, to_bytes},
    http::{Request, StatusCode},
    routing::{get, patch},
};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

static ENVIRONMENT_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

struct Environment(Vec<(&'static str, Option<std::ffi::OsString>)>);

impl Environment {
    fn set(values: &[(&'static str, &str)]) -> Self {
        Self(
            values
                .iter()
                .map(|&(name, value)| {
                    let previous = std::env::var_os(name);
                    unsafe { std::env::set_var(name, value) };
                    (name, previous)
                })
                .collect(),
        )
    }
}

impl Drop for Environment {
    fn drop(&mut self) {
        for (name, previous) in &self.0 {
            unsafe {
                match previous {
                    Some(value) => std::env::set_var(name, value),
                    None => std::env::remove_var(name),
                }
            }
        }
    }
}

async fn request_backup(
    app: &Router,
    path: &str,
    host: &str,
    token: Option<&str>,
) -> (StatusCode, Value) {
    request_backup_method(app, "GET", path, host, token).await
}

async fn request_backup_method(
    app: &Router,
    method: &str,
    path: &str,
    host: &str,
    token: Option<&str>,
) -> (StatusCode, Value) {
    let mut request = Request::builder()
        .method(method)
        .uri(path)
        .header("host", host);
    if let Some(token) = token {
        request = request.header("authorization", format!("Bearer {token}"));
    }
    let response = app
        .clone()
        .oneshot(request.body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let body = to_bytes(response.into_body(), 4096).await.unwrap();
    (status, serde_json::from_slice(&body).unwrap())
}

#[tokio::test]
async fn backup_public_reads_check_original_host_and_auth_before_go() {
    let _lock = ENVIRONMENT_LOCK.lock().await;
    let upstream = Router::new()
        .route(
            "/api/workspace/backup-policies",
            get(|| async { Json(json!({"policies": [], "nextRuns": {}})) }),
        )
        .route(
            "/api/workspace/backup-targets",
            get(|| async { Json(json!({"targets": [], "health": []})) }),
        )
        .route(
            "/api/workspace/backup-mirrors",
            get(|| async { Json(json!({"mirrors": []})) }),
        );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, upstream).await.unwrap() });
    let _environment = Environment::set(&[
        ("GO_CONTROL_ADDR", &format!("http://{address}")),
        ("AUTH_DISABLED", "false"),
        ("AUTH_TOKEN", "policy-edge-secret"),
        ("AUTH_ALLOWED_HOSTS", ""),
        ("HOST", "127.0.0.1"),
    ]);
    let static_root = tempfile::tempdir().unwrap();
    std::fs::create_dir(static_root.path().join("ui")).unwrap();
    std::fs::write(static_root.path().join("ui/index.html"), "native UI").unwrap();
    let app = create_production_app(static_root.path()).unwrap();

    for (path, expected) in [
        (
            "/api/workspace/backup-policies",
            json!({"policies": [], "nextRuns": {}}),
        ),
        (
            "/api/workspace/backup-targets",
            json!({"targets": [], "health": []}),
        ),
        ("/api/workspace/backup-mirrors", json!({"mirrors": []})),
    ] {
        let (status, body) = request_backup(&app, path, "foreign.example", None).await;
        assert_eq!(status, StatusCode::FORBIDDEN);
        assert_eq!(
            body,
            json!({"error": "Host not allowed", "code": "forbidden"})
        );

        let (status, body) = request_backup(&app, path, "localhost:8787", None).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED);
        assert_eq!(
            body,
            json!({"error": "Auth required", "code": "unauthorized"})
        );

        let (status, body) =
            request_backup(&app, path, "localhost:8787", Some("policy-edge-secret")).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, expected);

        let _disabled = Environment::set(&[("AUTH_DISABLED", "true")]);
        let disabled_app = create_production_app(static_root.path()).unwrap();
        let (status, body) = request_backup(&disabled_app, path, "foreign.example", None).await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(body, expected);
    }
    server.abort();
}

/// The oracle checks the original `Host` on the item paths too (`PATCH`/`DELETE
/// /api/workspace/backup-policies/{id}` and the target equivalents), not only on the
/// collection. The proxy rewrites `Host` when it forwards, so the edge is the only layer
/// that can refuse a foreign host here — a Go-side check would read the listener's own
/// host and always pass.
#[tokio::test]
async fn backup_public_item_routes_check_original_host_and_auth_before_go() {
    let _lock = ENVIRONMENT_LOCK.lock().await;
    let upstream = Router::new().route(
        "/api/workspace/backup-policies/:policy_id",
        patch(|| async { Json(json!({"policyId": "p-1", "policyRevision": 2})) })
            .delete(|| async { Json(json!({"deleted": true, "policyId": "p-1"})) }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move { axum::serve(listener, upstream).await.unwrap() });
    let _environment = Environment::set(&[
        ("GO_CONTROL_ADDR", &format!("http://{address}")),
        ("AUTH_DISABLED", "false"),
        ("AUTH_TOKEN", "policy-edge-secret"),
        ("AUTH_ALLOWED_HOSTS", ""),
        ("HOST", "127.0.0.1"),
    ]);
    let static_root = tempfile::tempdir().unwrap();
    std::fs::create_dir(static_root.path().join("ui")).unwrap();
    std::fs::write(static_root.path().join("ui/index.html"), "native UI").unwrap();
    let app = create_production_app(static_root.path()).unwrap();

    for (method, path, expected) in [
        (
            "PATCH",
            "/api/workspace/backup-policies/p-1",
            json!({"policyId": "p-1", "policyRevision": 2}),
        ),
        (
            "DELETE",
            "/api/workspace/backup-policies/p-1",
            json!({"deleted": true, "policyId": "p-1"}),
        ),
    ] {
        let (status, body) =
            request_backup_method(&app, method, path, "foreign.example", None).await;
        assert_eq!(
            (method, status, body),
            (
                method,
                StatusCode::FORBIDDEN,
                json!({"error": "Host not allowed", "code": "forbidden"})
            )
        );

        let (status, body) =
            request_backup_method(&app, method, path, "localhost:8787", None).await;
        assert_eq!(
            (method, status, body),
            (
                method,
                StatusCode::UNAUTHORIZED,
                json!({"error": "Auth required", "code": "unauthorized"})
            )
        );

        // The legal request is still proxied: the check refuses a bad host, not the verb.
        let (status, body) = request_backup_method(
            &app,
            method,
            path,
            "localhost:8787",
            Some("policy-edge-secret"),
        )
        .await;
        assert_eq!((method, status, body), (method, StatusCode::OK, expected));
    }
    server.abort();
}
