//! Write continuity & primary promotion route tests through `create_production_app`.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "continuity-route-test-token";
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

fn seed_policy(
    root: &std::path::Path,
    policy_id: &str,
    primary_target: &str,
    replica_target: &str,
) {
    let dir = root.join(".backup-policies");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join(format!("{policy_id}.json")),
        serde_json::to_vec(&json!({
            "policyId": policy_id,
            "name": "Continuity policy",
            "policyRevision": 1,
            "targetId": primary_target,
            "primaryTargetId": primary_target,
            "replication": {
                "enabled": true,
                "targets": [{"targetId": replica_target, "mode": "required"}],
            }
        }))
        .unwrap(),
    )
    .unwrap();
}

fn seed_ready_target(
    workspace: &std::path::Path,
    target_dir: &std::path::Path,
    target_id: &str,
    drain_state: &str,
) {
    std::fs::create_dir_all(target_dir).unwrap();
    std::fs::write(
        target_dir.join(".deepseek-infra-backup-target.json"),
        serde_json::to_vec(&json!({
            "schemaVersion": 3,
            "targetId": target_id,
            "targetNonce": "nonce_abc",
            "incarnationId": "inc_1",
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
            "path": target_dir.to_string_lossy(),
            "targetNonce": "nonce_abc",
            "drainState": drain_state,
        }))
        .unwrap(),
    )
    .unwrap();
}

#[tokio::test]
async fn continuity_read_never_creates_dir_and_default_mode_refuses_promote() {
    let _lock = EnvLock::acquire();
    let fix = Fixture::new("python_authoritative");
    seed_policy(fix.root.path(), "pol_c1", "managed-local", "target_sec");

    let (s_get, b_get) = call_json(
        &fix,
        "GET",
        "/api/workspace/backup-policies/pol_c1/continuity",
        None,
    )
    .await;
    assert_eq!(s_get, StatusCode::OK);
    assert_eq!(b_get["policyId"], "pol_c1");
    assert_eq!(b_get["configuredPrimaryTargetId"], "managed-local");
    assert_eq!(b_get["failoverEpoch"], 0);
    assert!(!fix.root.path().join(".backup-continuity").exists());

    let (s_prom, b_prom) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-policies/pol_c1/promote-primary",
        Some(json!({"targetId": "target_sec"})),
    )
    .await;
    assert_eq!(s_prom, StatusCode::CONFLICT);
    assert_eq!(
        b_prom["code"],
        "NATIVE_BACKUP_WRITE_CONTINUITY_WRITE_NOT_OWNED"
    );
    assert!(!fix.root.path().join(".backup-continuity").exists());
}

#[tokio::test]
async fn promote_primary_enforces_cas_preconditions_and_succeeds_in_python_disabled() {
    let _lock = EnvLock::acquire();
    let fix = Fixture::new("python_disabled");
    let external = tempfile::tempdir().unwrap();
    seed_ready_target(
        fix.root.path(),
        &external.path().join("sec"),
        "target_sec",
        "active",
    );
    seed_policy(fix.root.path(), "pol_c2", "managed-local", "target_sec");

    // Missing targetId -> 400.
    let (s_bad, _) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-policies/pol_c2/promote-primary",
        Some(json!({})),
    )
    .await;
    assert_eq!(s_bad, StatusCode::BAD_REQUEST);

    // CAS mismatch -> 412.
    let (s_cas, _) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-policies/pol_c2/promote-primary",
        Some(json!({"targetId": "target_sec", "expectedPolicyRevision": 9})),
    )
    .await;
    assert_eq!(s_cas, StatusCode::PRECONDITION_FAILED);

    // Seed DR ledger where global latest exists ONLY on managed-local -> reject 409.
    let dr_dir = fix.root.path().join(".backup-dr");
    std::fs::create_dir_all(&dr_dir).unwrap();
    let conn = Connection::open(dr_dir.join("evidence.sqlite3")).unwrap();
    conn.execute_batch(
        "CREATE TABLE recovery_point_copies (
            target_id TEXT NOT NULL, policy_id TEXT NOT NULL, backup_id TEXT NOT NULL,
            logical_id TEXT NOT NULL, object_set_digest TEXT, committed_at TEXT NOT NULL,
            recoverable INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL DEFAULT 'primary',
            mode TEXT NOT NULL DEFAULT 'required', state TEXT NOT NULL DEFAULT 'healthy',
            PRIMARY KEY (target_id, policy_id, backup_id)
        );
        INSERT INTO recovery_point_copies VALUES
            ('managed-local', 'pol_c2', 'bak_1', 'log_1', 'osd1', '2026-10-01T00:00:00Z', 1, 'primary', 'required', 'healthy');",
    )
    .unwrap();

    let (s_stale, b_stale) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-policies/pol_c2/promote-primary",
        Some(json!({"targetId": "target_sec"})),
    )
    .await;
    assert_eq!(s_stale, StatusCode::CONFLICT);
    assert!(
        b_stale["error"]
            .as_str()
            .unwrap()
            .contains("primary-promotion-rejected")
    );

    // Add global latest copy on target_sec -> promote succeeds!
    conn.execute(
        "INSERT INTO recovery_point_copies VALUES ('target_sec', 'pol_c2', 'bak_1', 'log_1', 'osd1', '2026-10-01T00:00:00Z', 1, 'replica', 'required', 'healthy')",
        [],
    )
    .unwrap();
    drop(conn);

    let (s_ok, b_ok) = call_json(
        &fix,
        "POST",
        "/api/workspace/backup-policies/pol_c2/promote-primary",
        Some(json!({
            "targetId": "target_sec",
            "expectedPolicyRevision": 1,
            "expectedFailoverEpoch": 0,
            "expectedLatestBackupId": "bak_1",
            "expectedLatestObjectSetDigest": "osd1",
        })),
    )
    .await;
    assert_eq!(s_ok, StatusCode::OK);
    assert_eq!(b_ok["status"], "promoted");
    assert_eq!(b_ok["policyRevision"], 2);
    assert_eq!(b_ok["failoverEpoch"], 1);
    assert_eq!(b_ok["newPrimaryTargetId"], "target_sec");

    // Read updated continuity back via GET.
    let (s_after, b_after) = call_json(
        &fix,
        "GET",
        "/api/workspace/backup-policies/pol_c2/continuity",
        None,
    )
    .await;
    assert_eq!(s_after, StatusCode::OK);
    assert_eq!(b_after["configuredPrimaryTargetId"], "target_sec");
    assert_eq!(b_after["failoverEpoch"], 1);
}
