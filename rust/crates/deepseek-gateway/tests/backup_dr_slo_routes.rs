//! `GET /api/workspace/disaster-recovery/slo` integration tests.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "dr-slo-test-token";
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

async fn call_slo(app: &axum::Router, method: &str) -> (StatusCode, Value) {
    let req = Request::builder()
        .method(method)
        .uri("/api/workspace/disaster-recovery/slo")
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
async fn dr_slo_defaults_on_empty_workspace_without_creating_dirs() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static, _env, app) = build_app(root.path());

    let (status, body) = call_slo(&app, "GET").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["restoreSuccessRate"], 1.0);
    assert_eq!(body["totalDrillsTested"], 0);
    assert_eq!(body["overallSloCompliant"], true);
    assert_eq!(body["rtoSeconds"]["p50"], 1.0);
    assert!(!root.path().join(".backup-dr").exists());
    assert!(!root.path().join(".restore-staging").exists());

    let (post_status, _) = call_slo(&app, "POST").await;
    assert_eq!(post_status, StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn dr_slo_aggregates_sqlite_and_staging_drill_evidence() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let dr_dir = root.path().join(".backup-dr");
    std::fs::create_dir_all(&dr_dir).unwrap();
    let conn = Connection::open(dr_dir.join("evidence.sqlite3")).unwrap();
    conn.execute_batch(
        "CREATE TABLE drill_evidence (
             drill_id TEXT PRIMARY KEY,
             target_id TEXT NOT NULL,
             policy_id TEXT NOT NULL,
             backup_id TEXT NOT NULL,
             observed_at TEXT NOT NULL,
             result TEXT NOT NULL,
             duration_ms INTEGER NOT NULL,
             details_json TEXT
         );",
    )
    .unwrap();
    conn.execute(
        "INSERT INTO drill_evidence VALUES ('d1', 'managed-local', 'pol-1', 'b-1', '2026-11-17T08:00:00Z', 'success', 2000, '{}')",
        [],
    )
    .unwrap();

    let staging = root.path().join(".restore-staging").join("restore_abc123");
    std::fs::create_dir_all(&staging).unwrap();
    std::fs::write(
        staging.join("drill-result.json"),
        serde_json::to_vec(&json!({
            "status": "failed",
            "proof": {"restoreDurationMs": 4000}
        }))
        .unwrap(),
    )
    .unwrap();

    let (_static, _env, app) = build_app(root.path());
    let (status, body) = call_slo(&app, "GET").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["totalDrillsTested"], 2);
    assert_eq!(body["restoreSuccessRate"], 0.5);
    assert_eq!(body["successRateHealthy"], false);
    assert_eq!(body["overallSloCompliant"], false);
    assert_eq!(body["rtoSeconds"]["p50"], 4.0);
}
