use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use deepseek_policy::python_json::dumps_compact;
use rusqlite::Connection;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use tower::ServiceExt;

const TEST_TOKEN: &str = "test-dr-audit-token";
const GENESIS_HASH: &str = "0000000000000000000000000000000000000000000000000000000000000000";

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

async fn post_json(app: &axum::Router, uri: &str, body: Option<&Value>) -> (StatusCode, Value) {
    let builder = Request::builder()
        .method("POST")
        .uri(uri)
        .header(header::HOST, "127.0.0.1:8080")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
    let req = if let Some(val) = body {
        let bytes = serde_json::to_vec(val).unwrap();
        builder
            .header(header::CONTENT_TYPE, "application/json")
            .header(header::CONTENT_LENGTH, bytes.len().to_string())
            .body(Body::from(bytes))
            .unwrap()
    } else {
        builder.body(Body::empty()).unwrap()
    };
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let raw = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let parsed: Value = serde_json::from_slice(&raw).unwrap_or(Value::Null);
    (status, parsed)
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn signed_commit(mut fields: Map<String, Value>) -> Value {
    let canonical = dumps_compact(&Value::Object(fields.clone()));
    fields.insert(
        "commitHash".to_string(),
        json!(sha256_hex(canonical.as_bytes())),
    );
    Value::Object(fields)
}

#[tokio::test]
async fn default_mode_refuses_audit_writes_and_reads_completed_resume_safely() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app(root.path(), "");

    let (status, body) = post_json(
        &app,
        "/api/workspace/disaster-recovery/audit",
        Some(&json!({"targetId": "managed-local"})),
    )
    .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "NATIVE_BACKUP_DR_LEDGER_WRITE_NOT_OWNED");
    assert!(!root.path().join(".backup-dr").exists());

    let (miss_status, miss_body) = post_json(
        &app,
        "/api/workspace/disaster-recovery/audit/audit_missing/resume",
        None,
    )
    .await;
    assert_eq!(miss_status, StatusCode::NOT_FOUND);
    assert_eq!(miss_body["code"], "not_found");
    assert!(!root.path().join(".backup-dr").exists());

    std::fs::create_dir_all(root.path().join(".backup-dr")).unwrap();
    let conn = Connection::open(root.path().join(".backup-dr").join("evidence.sqlite3")).unwrap();
    conn.execute_batch(
        "CREATE TABLE audit_jobs (
            audit_id TEXT PRIMARY KEY, target_id TEXT NOT NULL, phase TEXT NOT NULL,
            cursor TEXT, target_generation INTEGER, previous_commit_hash TEXT,
            records_checked INTEGER NOT NULL DEFAULT 0, anomalies_json TEXT,
            started_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT, details_json TEXT
         );
         INSERT INTO audit_jobs VALUES (
            'audit_done', 'managed-local', 'completed', NULL, 1, 'abc', 2, '[]',
            '2026-08-15T00:00:00Z', '2026-08-15T00:01:00Z', '2026-08-15T00:01:00Z', '{}'
         );
         INSERT INTO audit_jobs VALUES (
            'audit_open', 'managed-local', 'scanning', 'cur1', NULL, NULL, 1, '[]',
            '2026-08-15T00:00:00Z', '2026-08-15T00:01:00Z', NULL, '{}'
         );",
    )
    .unwrap();

    let (done_status, done_body) = post_json(
        &app,
        "/api/workspace/disaster-recovery/audit/audit_done/resume",
        None,
    )
    .await;
    assert_eq!(done_status, StatusCode::OK);
    assert_eq!(done_body["resumed"], false);
    assert_eq!(done_body["status"], "completed");

    let (open_status, open_body) = post_json(
        &app,
        "/api/workspace/disaster-recovery/audit/audit_open/resume",
        None,
    )
    .await;
    assert_eq!(open_status, StatusCode::CONFLICT);
    assert_eq!(open_body["code"], "NATIVE_BACKUP_DR_LEDGER_WRITE_NOT_OWNED");
}

#[tokio::test]
async fn python_disabled_audits_managed_local_and_paginated_filesystem_targets() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();

    // 1. Seed managed-local backup + one malformed commit
    let ml_commits = root.path().join(".backups").join("commits").join("pol_ml");
    let ml_receipts = root.path().join(".backups").join("receipts");
    let ml_obj_dir = root
        .path()
        .join(".backups")
        .join("objects")
        .join("sha256")
        .join("bb");
    std::fs::create_dir_all(&ml_commits).unwrap();
    std::fs::create_dir_all(&ml_receipts).unwrap();
    std::fs::create_dir_all(&ml_obj_dir).unwrap();
    let obj_digest = "b".repeat(64);
    std::fs::write(ml_obj_dir.join(format!("{obj_digest}.age")), b"ciphertext").unwrap();
    let ml_receipt_bytes = serde_json::to_vec_pretty(&json!({
        "backupId": "bk_local_1",
        "policyId": "pol_ml",
        "targetId": "managed-local",
        "snapshotKind": "full",
        "chainLength": 1,
        "size": 100,
        "logicalBytes": 4200,
        "storageProtocol": "object-set-v1",
        "objectSetDigest": "a".repeat(64),
        "objects": [{"digest": obj_digest, "size": 100}],
        "createdAt": "2026-08-15T00:00:00Z",
    }))
    .unwrap();
    std::fs::write(ml_receipts.join("bk_local_1.json"), &ml_receipt_bytes).unwrap();
    std::fs::write(
        ml_commits.join("20260815T000000Z.json"),
        serde_json::to_vec(&json!({
            "backupId": "bk_local_1",
            "policyId": "pol_ml",
            "commitHash": "hash_local_1",
            "committedAt": "2026-08-15T00:00:00Z",
        }))
        .unwrap(),
    )
    .unwrap();
    std::fs::write(ml_commits.join("bad.json"), b"not-json").unwrap();

    // 2. Seed registered filesystem target with 2 chained commits + control/head.json
    let rem_root = root.path().join("remote-fs");
    let rem_commits = rem_root.join("commits").join("pol_rem");
    let rem_receipts = rem_root.join("receipts");
    let rem_control = rem_root.join("control");
    std::fs::create_dir_all(&rem_commits).unwrap();
    std::fs::create_dir_all(&rem_receipts).unwrap();
    std::fs::create_dir_all(&rem_control).unwrap();

    let targets_dir = root.path().join(".backup-targets");
    std::fs::create_dir_all(&targets_dir).unwrap();
    std::fs::write(
        targets_dir.join("target_fs.json"),
        serde_json::to_vec(&json!({
            "targetId": "target_fs",
            "targetNonce": "nonce-fs-1",
            "kind": "filesystem",
            "path": rem_root.to_string_lossy(),
        }))
        .unwrap(),
    )
    .unwrap();
    std::fs::write(
        rem_root.join(".deepseek-infra-backup-target.json"),
        serde_json::to_vec(&json!({
            "targetId": "target_fs",
            "targetNonce": "nonce-fs-1",
        }))
        .unwrap(),
    )
    .unwrap();

    let r1 = serde_json::to_vec(&json!({
        "backupId": "bk_rem_1",
        "policyId": "pol_rem",
        "targetId": "target_fs",
        "snapshotKind": "full",
        "chainLength": 1,
        "size": 200,
        "logicalBytes": 500,
        "createdAt": "2026-08-15T01:00:00Z",
    }))
    .unwrap();
    std::fs::write(rem_receipts.join("bk_rem_1.json"), &r1).unwrap();
    let c1 = signed_commit(
        json!({
            "schemaVersion": 4,
            "backupId": "bk_rem_1",
            "policyId": "pol_rem",
            "targetGeneration": 1,
            "previousCommitHash": GENESIS_HASH,
            "committedAt": "2026-08-15T01:00:00Z",
            "receiptDigest": sha256_hex(&r1),
        })
        .as_object()
        .unwrap()
        .clone(),
    );
    std::fs::write(
        rem_commits.join("c1.json"),
        serde_json::to_vec(&c1).unwrap(),
    )
    .unwrap();

    let r2 = serde_json::to_vec(&json!({
        "backupId": "bk_rem_2",
        "policyId": "pol_rem",
        "targetId": "target_fs",
        "snapshotKind": "incremental",
        "parentBackupId": "bk_rem_1",
        "chainLength": 2,
        "size": 300,
        "logicalBytes": 800,
        "createdAt": "2026-08-15T02:00:00Z",
    }))
    .unwrap();
    std::fs::write(rem_receipts.join("bk_rem_2.json"), &r2).unwrap();
    let c2 = signed_commit(
        json!({
            "schemaVersion": 4,
            "backupId": "bk_rem_2",
            "policyId": "pol_rem",
            "targetGeneration": 2,
            "previousCommitHash": c1["commitHash"],
            "committedAt": "2026-08-15T02:00:00Z",
            "receiptDigest": sha256_hex(&r2),
        })
        .as_object()
        .unwrap()
        .clone(),
    );
    std::fs::write(
        rem_commits.join("c2.json"),
        serde_json::to_vec(&c2).unwrap(),
    )
    .unwrap();
    std::fs::write(
        rem_control.join("head.json"),
        serde_json::to_vec(&json!({
            "targetGeneration": 2,
            "latestCommitHash": c2["commitHash"],
        }))
        .unwrap(),
    )
    .unwrap();

    let (_static_root, _env, app) = build_app(root.path(), "python_disabled");

    // Managed-local audit
    let (ml_status, ml_body) = post_json(
        &app,
        "/api/workspace/disaster-recovery/audit",
        Some(&json!({"targetId": "managed-local"})),
    )
    .await;
    assert_eq!(ml_status, StatusCode::OK, "body={ml_body}");
    assert_eq!(ml_body["status"], "completed");
    assert_eq!(ml_body["recoveryPointsFound"], 1);
    assert_eq!(ml_body["anomalies"].as_array().unwrap().len(), 1);

    // Paged filesystem audit: page 1
    let (p1_status, p1_body) = post_json(
        &app,
        "/api/workspace/disaster-recovery/audit",
        Some(&json!({"targetId": "target_fs", "pageSize": 1})),
    )
    .await;
    assert_eq!(p1_status, StatusCode::OK, "body={p1_body}");
    assert_eq!(p1_body["status"], "in-progress");
    assert_eq!(p1_body["cursor"], "commits/pol_rem/c1.json");
    assert_eq!(p1_body["recoveryPointsFound"], 1);
    let aid = p1_body["auditId"].as_str().unwrap().to_string();

    // Resume via /audit/{audit_id}/resume -> finishes page 2 and validates chain
    let (p2_status, p2_body) = post_json(
        &app,
        &format!("/api/workspace/disaster-recovery/audit/{aid}/resume"),
        None,
    )
    .await;
    assert_eq!(p2_status, StatusCode::OK, "body={p2_body}");
    assert_eq!(p2_body["auditId"], aid);
    assert_eq!(p2_body["status"], "completed");
    assert_eq!(p2_body["recoveryPointsFound"], 2);
    assert_eq!(p2_body["anomalies"], json!([]));
    assert_eq!(p2_body["targetGeneration"], 2);

    let conn = Connection::open(root.path().join(".backup-dr").join("evidence.sqlite3")).unwrap();
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM recovery_points WHERE target_id = 'target_fs' AND recoverable = 1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(count, 2);
}
