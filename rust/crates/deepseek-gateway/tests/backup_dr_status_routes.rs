//! Read-only `GET /api/workspace/disaster-recovery/status` integration tests.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "dr-status-test-token";
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

async fn call_status(app: &axum::Router, method: &str) -> (StatusCode, Value) {
    let req = Request::builder()
        .method(method)
        .uri("/api/workspace/disaster-recovery/status")
        .header("host", "127.0.0.1")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), 131_072).await.unwrap();
    let val = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, val)
}

#[tokio::test]
async fn disaster_recovery_status_empty_is_read_only_and_warns() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static, _env, app) = build_app(root.path());

    let (st_get, body) = call_status(&app, "GET").await;
    assert_eq!(st_get, StatusCode::OK);
    assert_eq!(body["status"], "warning");
    assert_eq!(body["recoveryPoint"]["status"], "unavailable");
    assert_eq!(body["rtoEstimate"]["status"], "unavailable");
    assert_eq!(body["health"]["target"]["status"], "unavailable");
    assert!(!root.path().join(".backup-dr").exists());
    assert!(!root.path().join(".backup-component-cache").exists());
    assert!(!root.path().join(".restore-staging").exists());

    let (st_head, _) = call_status(&app, "HEAD").await;
    assert_eq!(st_head, StatusCode::OK);
    let (st_post, _) = call_status(&app, "POST").await;
    assert_eq!(st_post, StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn disaster_recovery_status_reads_seeded_scopes_dr_ledger_and_cache() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();

    std::fs::create_dir_all(root.path().join(".backup-policies")).unwrap();
    std::fs::write(
        root.path().join(".backup-policies/pol-1.json"),
        serde_json::to_string(&json!({
            "policyId": "pol-1",
            "targetId": "managed-local",
            "recoveryObjectives": {"maxRpoSeconds": 999999999}
        }))
        .unwrap(),
    )
    .unwrap();

    std::fs::create_dir_all(root.path().join(".backup-dr")).unwrap();
    let conn = Connection::open(root.path().join(".backup-dr/evidence.sqlite3")).unwrap();
    conn.execute_batch(
        "CREATE TABLE recovery_points (
            target_id TEXT, policy_id TEXT, backup_id TEXT, committed_at TEXT,
            snapshot_kind TEXT, parent_backup_id TEXT, chain_digest TEXT, chain_length INTEGER,
            ciphertext_bytes INTEGER, logical_bytes INTEGER, recoverable INTEGER,
            verified_at TEXT, storage_protocol TEXT, metadata_json TEXT
        );
        CREATE TABLE recovery_point_copies (
            target_id TEXT, policy_id TEXT, backup_id TEXT, logical_id TEXT,
            object_set_digest TEXT, committed_at TEXT, recoverable INTEGER, state TEXT
        );
        INSERT INTO recovery_points VALUES (
            'managed-local', 'pol-1', 'bkp-1', '2025-01-01T00:00:00Z',
            'full', NULL, 'cd', 1, 1024, 2048, 1, '2025-01-01T00:00:00Z', 'object-set-v1', '{}'
        );
        INSERT INTO recovery_point_copies VALUES (
            'managed-local', 'pol-1', 'bkp-1', 'log-1', 'cd', '2025-01-01T00:00:00Z', 1, 'healthy'
        );",
    )
    .unwrap();

    let cache_obj = root.path().join(".backup-component-cache/sha256/aa");
    std::fs::create_dir_all(&cache_obj).unwrap();
    std::fs::write(cache_obj.join("deadbeef.age"), b"0123456789").unwrap();

    let (_static, _env, app) = build_app(root.path());
    let (st, body) = call_status(&app, "GET").await;
    assert_eq!(st, StatusCode::OK);
    assert_eq!(body["status"], "available");
    assert_eq!(body["recoveryPoint"]["status"], "available");
    assert_eq!(body["recoveryPoint"]["backupId"], "bkp-1");
    assert_eq!(body["topology"]["healthyCopies"], 1);
    assert_eq!(body["topology"]["status"], "healthy");
    assert_eq!(body["cache"]["status"], "ok");
    assert_eq!(body["cache"]["entries"], 1);
    assert_eq!(body["cache"]["bytes"], 10);
}
