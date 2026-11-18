//! Integration tests for S4 manual policy backup execution:
//! - `POST /api/workspace/backup-policies/{policy_id}/run`

use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use deepseek_policy::backup_unlock::ephemeral_identity;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "s4-policy-run-test-token";
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
    _lock: EnvLock,
    _env: EnvGuard,
    dir: tempfile::TempDir,
    static_dir: tempfile::TempDir,
}

impl Fixture {
    fn new(mode: &str) -> Self {
        let lock = EnvLock::acquire();
        let dir = tempfile::tempdir().unwrap();
        let static_dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(static_dir.path().join("ui")).unwrap();
        std::fs::write(static_dir.path().join("ui/index.html"), "<!doctype html>").unwrap();
        let root = dir.path();
        std::fs::write(root.join(".auth-token"), TEST_TOKEN).unwrap();
        let env = EnvGuard::set(&[
            ("DEEPSEEK_INFRA_ROOT", root.to_string_lossy().into_owned()),
            ("DEEPSEEK_RUNTIME_MODE", mode.to_string()),
            ("AUTH_TOKEN", TEST_TOKEN.to_string()),
            ("AUTH_DISABLED", String::new()),
            ("AUTH_ALLOWED_HOSTS", String::new()),
            ("HOST", String::new()),
            (
                "DEEPSEEK_CONTROL_AUTHORITY_URL",
                "http://127.0.0.1:9".to_string(),
            ),
            ("DEEPSEEK_INTERNAL_BEARER", "unused".to_string()),
        ]);
        Self {
            _lock: lock,
            _env: env,
            dir,
            static_dir,
        }
    }

    fn root(&self) -> &Path {
        self.dir.path()
    }

    async fn call(&self, method: &str, uri: &str, body: Option<Value>) -> (StatusCode, Value) {
        let app = create_production_app(self.static_dir.path()).unwrap();
        let mut builder = Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, "127.0.0.1:8080")
            .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        let req_body = match body {
            Some(val) => {
                let bytes = serde_json::to_vec(&val).unwrap();
                builder = builder
                    .header(header::CONTENT_TYPE, "application/json")
                    .header(header::CONTENT_LENGTH, bytes.len().to_string());
                Body::from(bytes)
            }
            None => Body::empty(),
        };
        let response = app.oneshot(builder.body(req_body).unwrap()).await.unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), 1024 * 1024)
            .await
            .unwrap();
        let parsed = serde_json::from_slice::<Value>(&bytes).unwrap_or(Value::Null);
        (status, parsed)
    }
}

fn write_policy(root: &Path, policy_id: &str, recipient: &str, target_id: &str, max_attempts: i64) {
    let dir = root.join(".backup-policies");
    std::fs::create_dir_all(&dir).unwrap();
    let policy = json!({
        "schemaVersion": 2,
        "policyId": policy_id,
        "name": format!("Policy {policy_id}"),
        "enabled": true,
        "targetId": target_id,
        "schedule": {"cron": "0 3 * * *", "timezone": "UTC"},
        "scope": {"mode": "full", "coveragePolicy": "best-effort", "includeExternalState": false},
        "frontendMirror": {"mode": "best-effort"},
        "protection": {"mode": "age-recipient", "recipients": [recipient]},
        "retry": {"maxAttempts": max_attempts, "initialBackoffSeconds": 30, "maxBackoffSeconds": 60},
    });
    std::fs::write(
        dir.join(format!("{policy_id}.json")),
        serde_json::to_vec_pretty(&policy).unwrap(),
    )
    .unwrap();
}

#[tokio::test]
async fn default_mode_denies_policy_run_and_enforces_method_guards_without_disk_writes() {
    let fixture = Fixture::new("default");

    let (st_get, _) = fixture
        .call("GET", "/api/workspace/backup-policies/pol_1/run", None)
        .await;
    assert_eq!(st_get, StatusCode::NOT_FOUND);

    let (st_opt, _) = fixture
        .call("OPTIONS", "/api/workspace/backup-policies/pol_1/run", None)
        .await;
    assert_eq!(st_opt, StatusCode::NO_CONTENT);

    let (st_del, _) = fixture
        .call("DELETE", "/api/workspace/backup-policies/pol_1/run", None)
        .await;
    assert_eq!(st_del, StatusCode::METHOD_NOT_ALLOWED);

    let (st_post, body) = fixture
        .call("POST", "/api/workspace/backup-policies/pol_1/run", None)
        .await;
    assert_eq!(st_post, StatusCode::CONFLICT);
    assert_eq!(body["code"], "NATIVE_BACKUP_CATALOG_WRITE_NOT_OWNED");

    assert!(!fixture.root().join(".backup-scheduler").exists());
    assert!(!fixture.root().join(".backup-run-plans").exists());
    assert!(!fixture.root().join(".backup-spool").exists());
    assert!(!fixture.root().join(".backup-dr").exists());
    assert!(!fixture.root().join(".backup-index").exists());
    assert!(!fixture.root().join(".backups").exists());
}

#[tokio::test]
async fn missing_policy_returns_404_and_unavailable_target_records_blocked_run() {
    let fixture = Fixture::new("python_disabled");
    let (_, recipient) = ephemeral_identity();

    let (st_miss, _) = fixture
        .call(
            "POST",
            "/api/workspace/backup-policies/pol_missing/run",
            Some(json!({})),
        )
        .await;
    assert_eq!(st_miss, StatusCode::NOT_FOUND);

    // Target directory exists in registry, but its marker file is missing
    let target_dir = fixture.root().join("offline_target");
    std::fs::create_dir_all(&target_dir).unwrap();
    let targets_dir = fixture.root().join(".backup-targets");
    std::fs::create_dir_all(&targets_dir).unwrap();
    std::fs::write(
        targets_dir.join("target_offline1.json"),
        serde_json::to_vec(&json!({
            "targetId": "target_offline1",
            "kind": "filesystem",
            "path": target_dir.to_string_lossy(),
            "targetNonce": "nonce-1234",
        }))
        .unwrap(),
    )
    .unwrap();

    write_policy(
        fixture.root(),
        "pol_retry",
        &recipient,
        "target_offline1",
        2,
    );
    let (st_retry, retry_body) = fixture
        .call("POST", "/api/workspace/backup-policies/pol_retry/run", None)
        .await;
    assert_eq!(st_retry, StatusCode::OK, "retry={retry_body}");
    assert_eq!(retry_body["phase"], "blocked-retryable");
    assert_eq!(retry_body["blockedReason"], "blocked-target-unavailable");

    write_policy(fixture.root(), "pol_term", &recipient, "target_offline1", 1);
    let (st_term, term_body) = fixture
        .call("POST", "/api/workspace/backup-policies/pol_term/run", None)
        .await;
    assert_eq!(st_term, StatusCode::OK, "term={term_body}");
    assert_eq!(term_body["phase"], "blocked-terminal");

    let sched = Connection::open(fixture.root().join(".backup-scheduler/scheduler.db")).unwrap();
    let count: i64 = sched
        .query_row("SELECT COUNT(*) FROM backup_runs", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 2);
}

#[tokio::test]
async fn policy_manual_run_executes_object_set_commit_ledger_and_passes_scrub_and_unlock() {
    let fixture = Fixture::new("python_disabled");
    let (identity_secret, recipient) = ephemeral_identity();

    std::fs::create_dir_all(fixture.root().join(".memory")).unwrap();
    std::fs::write(
        fixture.root().join(".memory/memories.json"),
        r#"{"items":[{"id":"m1","text":"remember S4"}]}"#,
    )
    .unwrap();
    std::fs::create_dir_all(fixture.root().join(".projects/p1")).unwrap();
    std::fs::write(
        fixture.root().join(".projects/p1/note.txt"),
        "project workspace file\n",
    )
    .unwrap();
    write_policy(
        fixture.root(),
        "pol_nightly",
        &recipient,
        "managed-local",
        2,
    );

    let (status, outcome) = fixture
        .call(
            "POST",
            "/api/workspace/backup-policies/pol_nightly/run",
            Some(json!({})),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "outcome={outcome}");
    assert_eq!(outcome["phase"], "complete");
    assert_eq!(outcome["policyId"], "pol_nightly");
    assert_eq!(outcome["targetId"], "managed-local");
    assert_eq!(outcome["storageProtocol"], "object-set-v1");
    assert_eq!(outcome["targetGeneration"], 1);

    let backup_id = outcome["backupId"].as_str().unwrap().to_string();
    assert!(backup_id.starts_with("bk_pol_"));
    let backups_root = fixture.root().join(".backups");
    assert!(
        backups_root
            .join("receipts")
            .join(format!("{backup_id}.json"))
            .is_file()
    );
    assert!(
        backups_root
            .join("commits/pol_nightly")
            .join(format!("{backup_id}.json"))
            .is_file()
    );
    assert!(backups_root.join("catalog/catalog-v1.jsonl").is_file());
    assert!(backups_root.join("events").is_dir());
    assert!(fixture.root().join(".backup-dr/evidence.sqlite3").is_file());
    assert!(fixture.root().join(".backup-index/index.sqlite3").is_file());
    assert!(
        fixture
            .root()
            .join(".backup-scheduler/scheduler.db")
            .is_file()
    );
    assert!(
        fixture
            .root()
            .join(".backup-run-plans/pol_nightly")
            .is_dir()
    );

    // No unencrypted staging files left behind in .backup-spool/manual
    let spool_manual = fixture.root().join(".backup-spool/manual");
    let remaining_spool = std::fs::read_dir(&spool_manual)
        .map(|it| it.count())
        .unwrap_or(0);
    assert_eq!(remaining_spool, 0);

    // DR ledger row recorded as recoverable primary copy
    let dr_conn = Connection::open(fixture.root().join(".backup-dr/evidence.sqlite3")).unwrap();
    let rp_count: i64 = dr_conn
        .query_row(
            "SELECT COUNT(*) FROM recovery_points WHERE backup_id = ?1 AND recoverable = 1",
            [&backup_id],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(rp_count, 1);

    // Round-trip verify with S2 scrub & verify-unlock routes
    let (st_scrub, scrub) = fixture
        .call(
            "POST",
            &format!("/api/workspace/backups/{backup_id}/scrub"),
            None,
        )
        .await;
    assert_eq!(st_scrub, StatusCode::OK, "scrub={scrub}");
    assert_eq!(scrub["ok"], true);

    let (st_unlock, unlock) = fixture
        .call(
            "POST",
            &format!("/api/workspace/backups/{backup_id}/verify-unlock"),
            Some(json!({"identity": identity_secret})),
        )
        .await;
    assert_eq!(st_unlock, StatusCode::OK, "unlock={unlock}");
    assert_eq!(unlock["ok"], true);
}
