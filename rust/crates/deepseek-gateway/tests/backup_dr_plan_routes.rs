use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "test-dr-plan-token";

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

async fn call_raw(app: &axum::Router, method: &str, uri: &str) -> StatusCode {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header(header::HOST, "127.0.0.1:8080")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    app.clone().oneshot(req).await.unwrap().status()
}

#[tokio::test]
async fn plan_validates_payload_and_does_not_create_dr_dirs() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app(root.path());

    assert_eq!(
        call_raw(&app, "GET", "/api/workspace/disaster-recovery/plan").await,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        call_raw(&app, "DELETE", "/api/workspace/disaster-recovery/plan").await,
        StatusCode::METHOD_NOT_ALLOWED
    );

    let (status, body) = post_json(&app, "/api/workspace/disaster-recovery/plan", &json!({})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    assert_eq!(body["code"], "invalid_payload");

    let (status_404, body_404) = post_json(
        &app,
        "/api/workspace/disaster-recovery/plan",
        &json!({"policyId": "pol_missing"}),
    )
    .await;
    assert_eq!(status_404, StatusCode::NOT_FOUND);
    assert_eq!(body_404["code"], "not_found");
    assert!(!root.path().join(".backup-dr").exists());
}

#[tokio::test]
async fn plan_ranks_replicas_and_filters_broken_and_audit_failed_targets() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();

    let policies_dir = root.path().join(".backup-policies");
    std::fs::create_dir_all(&policies_dir).unwrap();
    std::fs::write(
        policies_dir.join("pol_pl.json"),
        serde_json::to_vec(&json!({
            "policyId": "pol_pl",
            "targetId": "target_p",
            "replication": {
                "enabled": true,
                "targets": [
                    {"targetId": "target_broken", "mode": "required"},
                    {"targetId": "target_aud", "mode": "best-effort"},
                    {"targetId": "target_r2", "mode": "best-effort"}
                ]
            }
        }))
        .unwrap(),
    )
    .unwrap();

    let targets_dir = root.path().join(".backup-targets");
    std::fs::create_dir_all(&targets_dir).unwrap();
    for (tid, kind) in [
        ("target_p", "s3"),
        ("target_r2", "filesystem"),
        ("target_broken", "filesystem"),
        ("target_aud", "s3"),
    ] {
        std::fs::write(
            targets_dir.join(format!("{tid}.json")),
            serde_json::to_vec(&json!({"targetId": tid, "kind": kind})).unwrap(),
        )
        .unwrap();
    }

    std::fs::create_dir_all(root.path().join(".backup-dr")).unwrap();
    let dr_conn =
        Connection::open(root.path().join(".backup-dr").join("evidence.sqlite3")).unwrap();
    dr_conn
        .execute_batch(
            "CREATE TABLE recovery_points (
                target_id TEXT, policy_id TEXT, backup_id TEXT, committed_at TEXT,
                snapshot_kind TEXT, parent_backup_id TEXT, chain_digest TEXT,
                chain_length INTEGER, ciphertext_bytes INTEGER, logical_bytes INTEGER,
                recoverable INTEGER, verified_at TEXT, storage_protocol TEXT, metadata_json TEXT
             );
             CREATE TABLE recovery_point_copies (
                target_id TEXT, policy_id TEXT, backup_id TEXT, logical_id TEXT,
                object_set_digest TEXT, committed_at TEXT, recoverable INTEGER,
                role TEXT, mode TEXT, state TEXT, metadata_json TEXT
             );
             CREATE TABLE target_evidence (
                target_id TEXT PRIMARY KEY, observed_at TEXT, scheduled_ready INTEGER, status TEXT
             );
             CREATE TABLE audit_evidence (
                audit_id TEXT PRIMARY KEY, target_id TEXT, started_at TEXT, status TEXT
             );
             CREATE TABLE scrub_evidence (
                target_id TEXT, backup_id TEXT, policy_id TEXT, observed_at TEXT, result TEXT
             );
             CREATE TABLE drill_evidence (
                drill_id TEXT, target_id TEXT, policy_id TEXT, observed_at TEXT,
                result TEXT, details_json TEXT
             );
             INSERT INTO recovery_points VALUES
                ('target_p', 'pol_pl', 'bk_1', '2026-08-15T00:00:00Z', 'full', NULL, 'cd1', 1, 512, 1024, 1, '2026-08-15T00:00:00Z', 'object-set-v1', '{\"objectSetDigest\":\"osd_1\"}'),
                ('target_r2', 'pol_pl', 'bk_1', '2026-08-15T00:00:00Z', 'full', NULL, 'cd1', 1, 512, 1024, 1, '2026-08-15T00:00:00Z', 'object-set-v1', '{\"objectSetDigest\":\"osd_1\"}'),
                ('target_broken', 'pol_pl', 'bk_1', '2026-08-15T00:00:00Z', 'full', NULL, 'cd1', 1, 512, 1024, 1, '2026-08-15T00:00:00Z', 'object-set-v1', '{\"objectSetDigest\":\"osd_1\"}'),
                ('target_aud', 'pol_pl', 'bk_1', '2026-08-15T00:00:00Z', 'full', NULL, 'cd1', 1, 512, 1024, 1, '2026-08-15T00:00:00Z', 'object-set-v1', '{\"objectSetDigest\":\"osd_1\"}');
             INSERT INTO recovery_point_copies VALUES
                ('target_p', 'pol_pl', 'bk_1', 'log_1', 'osd_1', '2026-08-15T00:00:00Z', 1, 'primary', 'required', 'healthy', '{}'),
                ('target_r2', 'pol_pl', 'bk_1', 'log_1', 'osd_1', '2026-08-15T00:00:00Z', 1, 'replica', 'best-effort', 'healthy', '{}'),
                ('target_broken', 'pol_pl', 'bk_1', 'log_1', 'osd_1', '2026-08-15T00:00:00Z', 1, 'replica', 'required', 'healthy', '{}'),
                ('target_aud', 'pol_pl', 'bk_1', 'log_1', 'osd_1', '2026-08-15T00:00:00Z', 1, 'replica', 'best-effort', 'healthy', '{}');
             INSERT INTO target_evidence VALUES
                ('target_p', '2026-08-15T00:00:00Z', 1, 'ok'),
                ('target_r2', '2026-08-15T00:00:00Z', 1, 'ok'),
                ('target_broken', '2026-08-15T00:00:00Z', 0, 'error'),
                ('target_aud', '2026-08-15T00:00:00Z', 1, 'ok');
             INSERT INTO audit_evidence VALUES
                ('aud_1', 'target_aud', '2026-08-15T00:00:00Z', 'failed');
             INSERT INTO scrub_evidence VALUES
                ('target_r2', 'bk_1', 'pol_pl', '2026-08-15T01:00:00Z', 'success');
             INSERT INTO drill_evidence VALUES
                ('dr_1', 'target_r2', 'pol_pl', '2026-08-15T01:00:00Z', 'success', '{\"drillKind\":\"scheduled\"}');",
        )
        .unwrap();

    let (_static_root, _env, app) = build_app(root.path());

    let (status, body) = post_json(
        &app,
        "/api/workspace/disaster-recovery/plan",
        &json!({
            "policyId": "pol_pl",
            "backupId": "bk_1",
            "preferredTargetId": "target_r2"
        }),
    )
    .await;
    assert_eq!(status, StatusCode::OK, "body={body}");
    assert_eq!(body["schemaVersion"], 1);
    assert_eq!(body["policyId"], "pol_pl");
    assert_eq!(body["selectedTargetId"], "target_r2");
    assert_eq!(body["remoteIo"], false);
    assert_eq!(body["source"], "dr-evidence-ledger");
    assert_eq!(body["maxFailovers"], 3);
    assert_eq!(body["logicalRecoveryPoint"]["backupId"], "bk_1");
    assert_eq!(body["logicalRecoveryPoint"]["objectSetDigest"], "osd_1");
    assert_eq!(body["logicalRecoveryPoint"]["logicalBytes"], 1024);

    let ordered = body["orderedCandidates"].as_array().unwrap();
    assert_eq!(ordered.len(), 2);
    assert_eq!(ordered[0]["targetId"], "target_r2");
    assert_eq!(ordered[0]["kind"], "filesystem");
    assert_eq!(
        ordered[0]["rankKey"],
        json!([0, 0, 1, 0, 0, 999_999, "target_r2"])
    );
    assert_eq!(ordered[1]["targetId"], "target_p");
    assert_eq!(ordered[1]["kind"], "s3");

    let rejected = body["rejectedCandidates"].as_array().unwrap();
    assert_eq!(
        rejected,
        &vec![
            json!({"targetId": "target_broken", "reasons": ["target-known-broken"]}),
            json!({"targetId": "target_aud", "reasons": ["audit-known-failed"]}),
        ]
    );
}
