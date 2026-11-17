//! Backup rebalance route integration tests through `create_production_app`.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "rebalance-route-test-token";
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

fn seed_local_target(
    workspace: &std::path::Path,
    dir: &std::path::Path,
    target_id: &str,
    failure_domain: &str,
    drain_state: &str,
) {
    std::fs::create_dir_all(dir).unwrap();
    std::fs::write(
        dir.join(".deepseek-infra-backup-target.json"),
        serde_json::to_vec(&json!({
            "schemaVersion": 3,
            "targetId": target_id,
            "targetNonce": format!("nonce_{target_id}"),
            "incarnationId": format!("inc_{target_id}"),
            "targetGeneration": 0,
            "latestCommitHash": "0000000000000000000000000000000000000000000000000000000000000000",
        }))
        .unwrap(),
    )
    .unwrap();
    let reg = workspace.join(".backup-targets");
    std::fs::create_dir_all(&reg).unwrap();
    std::fs::write(
        reg.join(format!("{target_id}.json")),
        serde_json::to_vec(&json!({
            "targetId": target_id,
            "kind": "filesystem",
            "path": dir.to_string_lossy(),
            "targetNonce": format!("nonce_{target_id}"),
            "failureDomain": failure_domain,
            "drainState": drain_state,
        }))
        .unwrap(),
    )
    .unwrap();
}

#[tokio::test]
async fn rebalance_list_is_read_only_and_default_mode_refuses_post() {
    let _lock = EnvLock::acquire();
    let fix = Fixture::new("python_authoritative");

    let (s_get, b_get) = call_json(&fix, "GET", "/api/workspace/backup-rebalances", None).await;
    assert_eq!(s_get, StatusCode::OK);
    assert_eq!(b_get, json!({"rebalances": []}));
    assert!(!fix.root.path().join(".backup-rebalance").exists());

    let (s_post, b_post) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-rebalances",
        Some(json!({"policyId": "pol_missing"})),
    )
    .await;
    assert_eq!(s_post, StatusCode::CONFLICT);
    assert_eq!(b_post["code"], "NATIVE_BACKUP_REPLICATION_WRITE_NOT_OWNED");
    assert!(!fix.root.path().join(".backup-rebalance").exists());
}

#[tokio::test]
async fn rebalance_handles_skipped_validation_and_local_copy_under_python_disabled() {
    let _lock = EnvLock::acquire();
    let fix = Fixture::new("python_disabled");

    // Empty policyId -> 400.
    let (s_bad, _) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-rebalances",
        Some(json!({"policyId": ""})),
    )
    .await;
    assert_eq!(s_bad, StatusCode::BAD_REQUEST);

    // Disabled replication -> skipped.
    let pol_dir = fix.root.path().join(".backup-policies");
    std::fs::create_dir_all(&pol_dir).unwrap();
    std::fs::write(
        pol_dir.join("pol_off.json"),
        serde_json::to_vec(&json!({
            "policyId": "pol_off",
            "replication": {"enabled": false}
        }))
        .unwrap(),
    )
    .unwrap();
    let (s_skip, b_skip) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-rebalances",
        Some(json!({"policyId": "pol_off"})),
    )
    .await;
    assert_eq!(s_skip, StatusCode::OK);
    assert_eq!(b_skip["status"], "skipped");
    assert_eq!(b_skip["reason"], "replication-disabled");

    // Rebalance draining source copy to healthy destination in distinct failure domain.
    let ext = tempfile::tempdir().unwrap();
    let src_dir = ext.path().join("src_t");
    let dst_dir = ext.path().join("dst_t");
    seed_local_target(fix.root.path(), &src_dir, "target_src", "fd-1", "draining");
    seed_local_target(fix.root.path(), &dst_dir, "target_dst", "fd-2", "active");

    std::fs::create_dir_all(src_dir.join("backups")).unwrap();
    std::fs::write(
        src_dir.join("backups/bak_r1.dsibackup.age"),
        b"cipher-bytes",
    )
    .unwrap();
    let cat_dir = src_dir.join("catalog");
    std::fs::create_dir_all(&cat_dir).unwrap();
    let receipt = json!({
        "type": "receipt",
        "payload": {
            "backupId": "bak_r1",
            "policyId": "pol_reb",
            "filename": "bak_r1.dsibackup.age",
            "size": 12,
        }
    });
    std::fs::write(
        cat_dir.join("catalog-v1.jsonl"),
        format!("{}\n", serde_json::to_string(&receipt).unwrap()),
    )
    .unwrap();

    let dr_dir = fix.root.path().join(".backup-dr");
    std::fs::create_dir_all(&dr_dir).unwrap();
    let conn = Connection::open(dr_dir.join("evidence.sqlite3")).unwrap();
    conn.execute_batch(
        "CREATE TABLE recovery_point_copies (
            target_id TEXT NOT NULL, policy_id TEXT NOT NULL, backup_id TEXT NOT NULL,
            logical_id TEXT NOT NULL, object_set_digest TEXT, committed_at TEXT NOT NULL,
            recoverable INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL DEFAULT 'primary',
            mode TEXT NOT NULL DEFAULT 'required', state TEXT NOT NULL DEFAULT 'healthy',
            verified_at TEXT, metadata_json TEXT,
            PRIMARY KEY (target_id, policy_id, backup_id)
        );
        INSERT INTO recovery_point_copies VALUES
            ('target_src', 'pol_reb', 'bak_r1', 'log_r1', NULL, '2026-10-01T00:00:00Z', 1, 'primary', 'required', 'healthy', NULL, '{}');",
    )
    .unwrap();
    drop(conn);

    std::fs::write(
        pol_dir.join("pol_reb.json"),
        serde_json::to_vec(&json!({
            "policyId": "pol_reb",
            "replication": {
                "enabled": true,
                "minFailureDomains": 2,
                "targets": [
                    {"targetId": "target_src", "mode": "required"},
                    {"targetId": "target_dst", "mode": "required"}
                ]
            }
        }))
        .unwrap(),
    )
    .unwrap();

    let (s_run, b_run) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-rebalances",
        Some(json!({"policyId": "pol_reb"})),
    )
    .await;
    assert_eq!(s_run, StatusCode::OK);
    assert_eq!(b_run["status"], "completed");
    assert_eq!(b_run["jobsCreated"], 1);
    assert!(dst_dir.join("backups/bak_r1.dsibackup.age").is_file());

    let (s_list, b_list) = call_json(
        &fix,
        "GET",
        "/api/workspace/backup-rebalances?policyId=pol_reb",
        None,
    )
    .await;
    assert_eq!(s_list, StatusCode::OK);
    let jobs = b_list["rebalances"].as_array().unwrap();
    assert_eq!(jobs.len(), 1);
    assert_eq!(jobs[0]["destTargetId"], "target_dst");
    assert_eq!(jobs[0]["phase"], "complete");
    assert_eq!(jobs[0]["bytesTransferred"], 12);
}
