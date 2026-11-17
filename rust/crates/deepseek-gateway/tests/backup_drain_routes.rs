//! Target drain route integration tests through `create_production_app`.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "drain-route-test-token";
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

struct Fixture {
    root: tempfile::TempDir,
    _static_root: tempfile::TempDir,
    _env: EnvGuard,
    app: axum::Router,
}
impl Fixture {
    fn new(mode: &str) -> Self {
        let root = tempfile::tempdir().expect("workspace");
        let static_root = tempfile::tempdir().expect("static");
        std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
        std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
        let env = EnvGuard::set(&[
            (
                "DEEPSEEK_INFRA_ROOT",
                root.path().to_string_lossy().into_owned(),
            ),
            ("AUTH_TOKEN", TEST_TOKEN.to_string()),
            ("AUTH_DISABLED", String::new()),
            ("AUTH_ALLOWED_HOSTS", String::new()),
            ("HOST", String::new()),
            ("DEEPSEEK_RUNTIME_MODE", mode.to_string()),
            ("GO_CONTROL_ADDR", String::new()),
            ("DEEPSEEK_GO_CONTROL_URL", String::new()),
        ]);
        let app = create_production_app(static_root.path()).unwrap();
        Self {
            root,
            _static_root: static_root,
            _env: env,
            app,
        }
    }
}

async fn call_json(
    fix: &Fixture,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let raw = match body {
        Some(v) => serde_json::to_vec(&v).unwrap(),
        None => Vec::new(),
    };
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header("host", "127.0.0.1")
        .header("authorization", format!("Bearer {TEST_TOKEN}"));
    if !raw.is_empty() {
        builder = builder
            .header(header::CONTENT_TYPE, "application/json")
            .header(header::CONTENT_LENGTH, raw.len().to_string());
    }
    let response = fix
        .app
        .clone()
        .oneshot(builder.body(Body::from(raw)).unwrap())
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

fn seed_target(root: &std::path::Path, target_id: &str) {
    let dir = root.join(".backup-targets");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join(format!("{target_id}.json")),
        serde_json::to_vec(&json!({
            "targetId": target_id,
            "kind": "filesystem",
            "path": "/tmp/mock-unused",
            "drainState": "active",
        }))
        .unwrap(),
    )
    .unwrap();
}

#[tokio::test]
async fn drain_read_never_creates_dir_and_default_mode_refuses_writes() {
    let _lock = EnvLock::acquire();
    let fix = Fixture::new("go_authoritative");
    seed_target(fix.root.path(), "target_d1");

    let (get_s, get_b) = call_json(
        &fix,
        "GET",
        "/api/workspace/backup-targets/target_d1/drain",
        None,
    )
    .await;
    assert_eq!(get_s, StatusCode::NOT_FOUND);
    assert_eq!(get_b["code"], "not_found");
    assert!(!fix.root.path().join(".backup-drains").exists());

    let (post_s, post_b) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-targets/target_d1/drain",
        Some(json!({"reason": "maintenance"})),
    )
    .await;
    assert_eq!(post_s, StatusCode::CONFLICT);
    assert_eq!(post_b["code"], "NATIVE_BACKUP_DRAIN_WRITE_NOT_OWNED");
    assert!(!fix.root.path().join(".backup-drains").exists());

    let (cancel_s, cancel_b) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-targets/target_d1/drain/cancel",
        Some(json!({"reason": "abort"})),
    )
    .await;
    assert_eq!(cancel_s, StatusCode::CONFLICT);
    assert_eq!(cancel_b["code"], "NATIVE_BACKUP_DRAIN_WRITE_NOT_OWNED");
    assert!(!fix.root.path().join(".backup-drains").exists());
}

#[tokio::test]
async fn drain_lifecycle_works_in_python_disabled_and_guards_methods() {
    let _lock = EnvLock::acquire();
    let fix = Fixture::new("python_disabled");
    seed_target(fix.root.path(), "target_d2");

    // Unregistered target returns 404.
    let (nf_s, _) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-targets/target_missing/drain",
        Some(json!({"reason": "x"})),
    )
    .await;
    assert_eq!(nf_s, StatusCode::NOT_FOUND);

    // Start drain.
    let (s_start, b_start) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-targets/target_d2/drain",
        Some(json!({"reason": "disk-swap"})),
    )
    .await;
    assert_eq!(s_start, StatusCode::OK);
    assert_eq!(b_start["targetId"], "target_d2");
    assert_eq!(b_start["phase"], "draining");
    assert_eq!(b_start["drainState"], "draining");
    assert_eq!(b_start["reason"], "disk-swap");
    assert!(b_start["drainId"].as_str().unwrap().starts_with("drain_"));

    // Read back via GET.
    let (s_get, b_get) = call_json(
        &fix,
        "GET",
        "/api/workspace/backup-targets/target_d2/drain",
        None,
    )
    .await;
    assert_eq!(s_get, StatusCode::OK);
    assert_eq!(b_get["drainId"], b_start["drainId"]);
    assert_eq!(b_get["phase"], "draining");

    // Cancel drain.
    let (s_cancel, b_cancel) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-targets/target_d2/drain/cancel",
        Some(json!({"reason": "false-alarm"})),
    )
    .await;
    assert_eq!(s_cancel, StatusCode::OK);
    assert_eq!(b_cancel["phase"], "cancelled");
    assert_eq!(b_cancel["drainState"], "cancelled");
    assert_eq!(b_cancel["error"], "false-alarm");

    // Disallowed methods.
    let (del_s, _) = call_json(
        &fix,
        "DELETE",
        "/api/workspace/backup-targets/target_d2/drain",
        None,
    )
    .await;
    assert_eq!(del_s, StatusCode::METHOD_NOT_ALLOWED);

    let (get_cancel_s, _) = call_json(
        &fix,
        "GET",
        "/api/workspace/backup-targets/target_d2/drain/cancel",
        None,
    )
    .await;
    assert_eq!(get_cancel_s, StatusCode::METHOD_NOT_ALLOWED);
}
