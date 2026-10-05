//! `GET /api/workspace/backup-runs` through the production router.
//!
//! The handler reads `.backup-scheduler/scheduler.db` and does not create it.

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "backup-run-route-token";

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
                unsafe { std::env::set_var(name, value) }
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
                root.path().to_str().expect("utf-8").to_string(),
            ),
            ("AUTH_TOKEN", TEST_TOKEN.to_string()),
            ("DEEPSEEK_RUNTIME_MODE", mode.to_string()),
        ]);
        let app = create_production_app(static_root.path()).expect("app");
        Self {
            root,
            _static_root: static_root,
            _env: env,
            app,
        }
    }

    fn db_path(&self) -> std::path::PathBuf {
        self.root
            .path()
            .join(".backup-scheduler")
            .join("scheduler.db")
    }

    async fn request(&self, uri: &str, auth: bool) -> (StatusCode, Value) {
        let mut request = Request::builder()
            .method("GET")
            .uri(uri)
            .header(header::HOST, "127.0.0.1");
        if auth {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        let response = self
            .app
            .clone()
            .oneshot(request.body(Body::empty()).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (
            status,
            serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        )
    }
}

fn seed(path: &Path) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    let conn = Connection::open(path).unwrap();
    conn.execute_batch(
        "CREATE TABLE backup_runs (
            run_id TEXT PRIMARY KEY,
            policy_id TEXT NOT NULL,
            schedule_slot TEXT NOT NULL,
            phase TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            owner_instance_id TEXT,
            fencing_token INTEGER,
            lease_until TEXT,
            reason TEXT,
            error TEXT,
            backup_id TEXT,
            filename TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );",
    )
    .unwrap();
    conn.execute(
        "INSERT INTO backup_runs(
            run_id, policy_id, schedule_slot, phase, attempt, owner_instance_id,
            fencing_token, lease_until, reason, error, backup_id, filename, created_at, updated_at
        ) VALUES
            ('run-old', 'pol-a', 'slot-1', 'complete', 1, 'worker-a', 4, NULL,
             'finished', NULL, 'bk-1', 'bk-1.age',
             '2026-10-03T00:00:01Z', '2026-10-03T00:00:02Z'),
            ('run-new', 'pol-b', 'slot-2', 'blocked-retryable', 2, NULL, NULL,
             '2026-10-03T01:00:00Z', 'disk', 'full', NULL, NULL,
             '2026-10-03T00:00:03Z', '2026-10-03T00:00:04Z')",
        [],
    )
    .unwrap();
}

fn directory_files(path: &Path) -> Vec<String> {
    let mut names: Vec<String> = std::fs::read_dir(path)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

#[tokio::test]
async fn backup_runs_list_reads_the_scheduler_database_without_creating_one() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new("");

    let (status, empty) = fixture.request("/api/workspace/backup-runs", true).await;
    assert_eq!(status, StatusCode::OK, "{empty}");
    assert_eq!(empty, json!({"runs": []}));
    assert!(!fixture.db_path().parent().unwrap().exists());

    seed(&fixture.db_path());
    let before = directory_files(fixture.db_path().parent().unwrap());

    let (status, listed) = fixture.request("/api/workspace/backup-runs", true).await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    let runs = listed["runs"].as_array().unwrap();
    assert_eq!(runs.len(), 2);
    assert_eq!(runs[0]["runId"], "run-new");
    assert_eq!(runs[0]["phase"], "blocked-retryable");
    assert_eq!(runs[0]["attempt"], 2);
    assert!(runs[0]["ownerInstanceId"].is_null());
    assert!(runs[0]["fencingToken"].is_null());
    assert_eq!(runs[0]["leaseUntil"], "2026-10-03T01:00:00Z");
    assert_eq!(runs[0]["nextRetryAt"], "2026-10-03T01:00:00Z");
    assert_eq!(runs[0]["blockedReason"], "disk");
    assert_eq!(runs[0]["reason"], "disk");
    assert_eq!(runs[0]["error"], "full");
    assert!(runs[0]["backupId"].is_null());
    assert_eq!(runs[1]["runId"], "run-old");
    assert_eq!(runs[1]["phase"], "complete");
    assert_eq!(runs[1]["attempt"], 1);
    assert_eq!(runs[1]["fencingToken"], 4);
    assert_eq!(runs[1]["ownerInstanceId"], "worker-a");
    assert!(runs[1]["nextRetryAt"].is_null());
    assert!(runs[1]["blockedReason"].is_null());
    assert_eq!(runs[1]["reason"], "finished");
    assert_eq!(runs[1]["backupId"], "bk-1");
    assert_eq!(runs[1]["filename"], "bk-1.age");

    let (status, filtered) = fixture
        .request("/api/workspace/backup-runs?policyId=pol-a", true)
        .await;
    assert_eq!(status, StatusCode::OK, "{filtered}");
    assert_eq!(filtered["runs"].as_array().unwrap().len(), 1);
    assert_eq!(filtered["runs"][0]["runId"], "run-old");

    let (status, blank) = fixture
        .request("/api/workspace/backup-runs?policyId=", true)
        .await;
    assert_eq!(status, StatusCode::OK, "{blank}");
    assert_eq!(blank["runs"].as_array().unwrap().len(), 2);

    assert_eq!(directory_files(fixture.db_path().parent().unwrap()), before);

    let (status, unauthenticated) = fixture.request("/api/workspace/backup-runs", false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
}
