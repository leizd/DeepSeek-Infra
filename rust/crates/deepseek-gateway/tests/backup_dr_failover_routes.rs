use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "test-dr-failover-token";

static ENV_BUSY: AtomicBool = AtomicBool::new(false);
struct EnvLock;
impl EnvLock {
    fn acquire() -> Self {
        while ENV_BUSY
            .compare_exchange_weak(false, true, Ordering::Acquire, Ordering::Relaxed)
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

fn build_app(root: &std::path::Path, mode: &str) -> (tempfile::TempDir, EnvGuard, axum::Router) {
    let static_root = tempfile::tempdir().unwrap();
    let ui = static_root.path().join("ui");
    std::fs::create_dir_all(&ui).unwrap();
    std::fs::write(ui.join("index.html"), "<main>ui</main>").unwrap();
    let env = EnvGuard::set(&[
        ("DEEPSEEK_INFRA_ROOT", root.to_string_lossy().into_owned()),
        ("DEEPSEEK_RUNTIME_MODE", mode.to_string()),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
    ]);
    let app = create_production_app(static_root.path()).unwrap();
    (static_root, env, app)
}

async fn post_json(app: &axum::Router, uri: &str, body: &Value) -> (StatusCode, Value) {
    let bytes = serde_json::to_vec(body).unwrap();
    let req = Request::builder()
        .method("POST")
        .uri(uri)
        .header(header::HOST, "127.0.0.1:8080")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::CONTENT_LENGTH, bytes.len().to_string())
        .body(Body::from(bytes))
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let raw = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let parsed: Value = serde_json::from_slice(&raw).unwrap_or(Value::Null);
    (status, parsed)
}

#[tokio::test]
async fn default_mode_refuses_failover_without_modifying_staging() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app(root.path(), "");

    let (status, body) = post_json(
        &app,
        "/api/workspace/disaster-recovery/failover/restore_1",
        &json!({"reason": "network-unavailable"}),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED");
    assert!(!root.path().join(".restore-staging").exists());
}

#[tokio::test]
async fn python_disabled_switches_target_creates_new_hold_and_releases_old() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();

    let ml_hold_dir = root.path().join(".backups").join("holds").join("restore");
    std::fs::create_dir_all(&ml_hold_dir).unwrap();
    let old_hold_path = ml_hold_dir.join("restore_fo1.json");
    std::fs::write(&old_hold_path, b"{}").unwrap();

    let t2_root = root.path().join("target_r2");
    std::fs::create_dir_all(&t2_root).unwrap();
    std::fs::write(
        t2_root.join(".deepseek-infra-backup-target.json"),
        serde_json::to_vec(&json!({"targetId": "target_r2", "targetNonce": "nonce-r2"})).unwrap(),
    )
    .unwrap();
    let targets_dir = root.path().join(".backup-targets");
    std::fs::create_dir_all(&targets_dir).unwrap();
    std::fs::write(
        targets_dir.join("target_r2.json"),
        serde_json::to_vec(&json!({
            "targetId": "target_r2",
            "targetNonce": "nonce-r2",
            "kind": "filesystem",
            "path": t2_root.to_string_lossy(),
        }))
        .unwrap(),
    )
    .unwrap();

    let session_dir = root.path().join(".restore-staging").join("restore_fo1");
    std::fs::create_dir_all(&session_dir).unwrap();
    std::fs::write(
        session_dir.join("remote-fetch.json"),
        serde_json::to_vec_pretty(&json!({
            "restoreId": "restore_fo1",
            "phase": "fetching",
            "targetId": "managed-local",
            "activeSourceTargetId": "managed-local",
            "backupId": "bk_1",
            "storageProtocol": "object-set-v1",
            "holdKeys": ["holds/restore/restore_fo1.json"],
            "downloadedBytes": 128,
            "attemptedSourceTargets": ["managed-local"],
            "failoverCount": 0,
            "recoveryPlan": {
                "maxFailovers": 3,
                "orderedCandidates": [
                    {"targetId": "managed-local"},
                    {"targetId": "target_r2"}
                ]
            }
        }))
        .unwrap(),
    )
    .unwrap();

    let (_static_root, _env, app) = build_app(root.path(), "python_disabled");

    // Non-retryable reason -> 409
    let (bad_reason_status, _) = post_json(
        &app,
        "/api/workspace/disaster-recovery/failover/restore_fo1",
        &json!({"reason": "identity-error"}),
    )
    .await;
    assert_eq!(bad_reason_status, StatusCode::CONFLICT);
    assert!(old_hold_path.is_file());

    // Valid failover
    let (status, body) = post_json(
        &app,
        "/api/workspace/disaster-recovery/failover/restore_fo1",
        &json!({"reason": "network-unavailable"}),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "body={body}");
    assert_eq!(body["activeSourceTargetId"], "target_r2");
    assert_eq!(body["previousSourceTargetId"], "managed-local");
    assert_eq!(body["failoverCount"], 1);
    assert_eq!(body["phase"], "fetching-controls");
    assert_eq!(body["newHoldBeforeOldRelease"], true);
    let new_hold = t2_root
        .join("holds")
        .join("restore")
        .join("restore_fo1-fo1.json");
    assert!(new_hold.is_file());
    assert!(!old_hold_path.exists());

    // Persisted session updated
    let saved: Value = serde_json::from_str(
        &std::fs::read_to_string(session_dir.join("remote-fetch.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(saved["activeSourceTargetId"], "target_r2");
    assert_eq!(saved["downloadedBytes"], 0);
    assert_eq!(saved["failoverCount"], 1);
}
