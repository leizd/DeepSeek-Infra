//! `GET /api/workspace/backup-capacity/summary` integration tests.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "backup-capacity-test-token";
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

async fn call_summary(app: &axum::Router, method: &str) -> (StatusCode, Value) {
    let req = Request::builder()
        .method(method)
        .uri("/api/workspace/backup-capacity/summary")
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
async fn capacity_summary_handles_empty_workspace_without_creating_control_db() {
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
    ]);
    let app = build_app(static_root.path());

    let (status, body) = call_summary(&app, "GET").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["overallStatus"], "healthy");
    assert_eq!(body["targets"], json!([]));
    assert!(!root.path().join(".backup-control").exists());

    let (post_status, _) = call_summary(&app, "POST").await;
    assert_eq!(post_status, StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn capacity_summary_evaluates_quota_object_accounting_and_growth_horizon() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let static_root = tempfile::tempdir().unwrap();
    let control_dir = root.path().join(".backup-control");
    std::fs::create_dir_all(&control_dir).unwrap();
    let db_path = control_dir.join("control.sqlite3");
    {
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE control_targets (target_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
             CREATE TABLE target_objects (
                 target_id TEXT NOT NULL,
                 object_key TEXT NOT NULL,
                 state TEXT NOT NULL,
                 live_ref_count INTEGER NOT NULL,
                 retired_ref_count INTEGER NOT NULL,
                 size_bytes INTEGER NOT NULL,
                 ciphertext_digest TEXT,
                 is_physical INTEGER NOT NULL DEFAULT 1
             );
             CREATE TABLE capacity_growth_observations (
                 observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                 target_id TEXT NOT NULL,
                 physical_stored_bytes INTEGER NOT NULL,
                 live_referenced_bytes INTEGER NOT NULL,
                 retired_pending_gc_bytes INTEGER NOT NULL,
                 new_committed_bytes INTEGER NOT NULL,
                 observed_at TEXT NOT NULL
             );",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO control_targets (target_id, payload_json) VALUES (?, ?)",
            [
                "quota-target",
                &serde_json::to_string(&json!({
                    "targetId": "quota-target",
                    "label": "Quota Target",
                    "kind": "remote-quota",
                    "quotaBytes": 1_000_000,
                }))
                .unwrap(),
            ],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO target_objects (target_id, object_key, state, live_ref_count, retired_ref_count, size_bytes, ciphertext_digest, is_physical)
             VALUES ('quota-target', 'obj-1', 'active', 1, 0, 920000, 'dig-1', 1)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO capacity_growth_observations (target_id, physical_stored_bytes, live_referenced_bytes, retired_pending_gc_bytes, new_committed_bytes, observed_at)
             VALUES ('quota-target', 820000, 820000, 0, 0, '2026-11-01T00:00:00Z'),
                    ('quota-target', 920000, 920000, 0, 100000, '2026-11-02T00:00:00Z')",
            [],
        )
        .unwrap();
    }
    let _env = EnvGuard::set(&[
        (
            "DEEPSEEK_INFRA_ROOT",
            root.path().to_string_lossy().into_owned(),
        ),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
    ]);
    let app = build_app(static_root.path());

    let (status, body) = call_summary(&app, "GET").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["overallStatus"], "critical");
    assert_eq!(body["targets"].as_array().unwrap().len(), 1);
    let item = &body["targets"][0];
    assert_eq!(item["targetId"], "quota-target");
    assert_eq!(item["label"], "Quota Target");
    assert_eq!(item["capacity"]["totalBytes"], 1_000_000);
    assert_eq!(item["capacity"]["usedBytes"], 920_000);
    assert_eq!(item["capacity"]["freeBytes"], 80_000);
    assert_eq!(item["capacity"]["freePercent"], 8.0);
    assert_eq!(item["capacity"]["source"], "physical-object-index");
    assert_eq!(item["horizon"]["status"], "critical");
    assert_eq!(item["horizon"]["bytesPerDayP50"], 100_000);
    assert_eq!(item["horizon"]["estimatedDaysToFull"], 0);
}
