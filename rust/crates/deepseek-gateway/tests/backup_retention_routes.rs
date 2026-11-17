//! Integration tests for S3 scheduled backup retention routes:
//! - `POST /api/workspace/retention/preview`
//! - `POST /api/workspace/retention/apply`

use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::backup_commit::commit_marker_valid;
use deepseek_gateway::create_production_app;
use deepseek_policy::python_json::dumps_compact;
use rusqlite::Connection;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use tower::ServiceExt;

const TEST_TOKEN: &str = "backup-retention-test-token";
static ENV_LOCK: Mutex<()> = Mutex::new(());

struct EnvGuard {
    _lock: MutexGuard<'static, ()>,
    saved: Vec<(String, Option<String>)>,
}

impl EnvGuard {
    fn set(vars: &[(&str, String)]) -> Self {
        let lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let mut saved = Vec::with_capacity(vars.len());
        for (name, value) in vars {
            saved.push(((*name).to_string(), std::env::var(name).ok()));
            if value.is_empty() {
                unsafe { std::env::remove_var(name) };
            } else {
                unsafe { std::env::set_var(name, value) };
            }
        }
        Self { _lock: lock, saved }
    }
}

impl Drop for EnvGuard {
    fn drop(&mut self) {
        for (name, value) in &self.saved {
            match value {
                Some(v) => unsafe { std::env::set_var(name, v) },
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
    fn new(runtime_mode: &str) -> Self {
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
            ("AUTH_DISABLED", String::new()),
            ("AUTH_ALLOWED_HOSTS", String::new()),
            ("HOST", String::new()),
            ("DEEPSEEK_RUNTIME_MODE", runtime_mode.to_string()),
            ("GO_CONTROL_ADDR", String::new()),
            ("DEEPSEEK_GO_CONTROL_URL", String::new()),
        ]);
        let app = create_production_app(static_root.path()).expect("app");
        Self {
            root,
            _static_root: static_root,
            _env: env,
            app,
        }
    }

    fn path(&self) -> &Path {
        self.root.path()
    }

    fn write_policy(&self, policy_id: &str, payload: Value) {
        let dir = self.path().join(".backup-policies");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join(format!("{policy_id}.json")),
            serde_json::to_string_pretty(&payload).unwrap(),
        )
        .unwrap();
    }

    fn write_retention_policy(&self, retention_id: &str, payload: Value) {
        let dir = self.path().join(".backup-retention");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join(format!("{retention_id}.json")),
            serde_json::to_string_pretty(&payload).unwrap(),
        )
        .unwrap();
    }

    async fn call(
        &self,
        method: &str,
        uri: &str,
        body: Option<Value>,
        auth: bool,
    ) -> (StatusCode, Value) {
        let raw_body = match body {
            Some(v) => Body::from(serde_json::to_vec(&v).unwrap()),
            None => Body::empty(),
        };
        let mut builder = Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, "127.0.0.1")
            .header(header::CONTENT_TYPE, "application/json");
        if auth {
            builder = builder.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        let resp = self
            .app
            .clone()
            .oneshot(builder.body(raw_body).unwrap())
            .await
            .unwrap();
        let status = resp.status();
        let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
        let parsed: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
        (status, parsed)
    }
}

fn sha256_bytes(data: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(data);
    format!("{:x}", hasher.finalize())
}

fn append_catalog_entry(root: &Path, entry_type: &str, payload: Value) {
    let catalog_dir = root.join("catalog");
    std::fs::create_dir_all(&catalog_dir).unwrap();
    let catalog_path = catalog_dir.join("catalog-v1.jsonl");
    let genesis = "0000000000000000000000000000000000000000000000000000000000000000";
    let existing = if catalog_path.is_file() {
        std::fs::read_to_string(&catalog_path).unwrap()
    } else {
        String::new()
    };
    let prev = existing
        .lines()
        .rfind(|line| !line.trim().is_empty())
        .and_then(|line| serde_json::from_str::<Value>(line).ok())
        .and_then(|val| {
            val.get("entryHash")
                .and_then(Value::as_str)
                .map(ToString::to_string)
        })
        .unwrap_or_else(|| genesis.to_string());

    let mut body = Map::new();
    body.insert("type".to_string(), json!(entry_type));
    body.insert("previousEntryHash".to_string(), json!(prev));
    body.insert("payload".to_string(), payload.clone());
    let entry_hash = sha256_bytes(dumps_compact(&Value::Object(body)).as_bytes());

    let mut full = Map::new();
    full.insert("type".to_string(), json!(entry_type));
    full.insert("previousEntryHash".to_string(), json!(prev));
    full.insert("payload".to_string(), payload);
    full.insert("recordedAt".to_string(), json!("2026-06-15T00:00:00Z"));
    full.insert("entryHash".to_string(), json!(entry_hash));

    let line = format!("{}\n", dumps_compact(&Value::Object(full)));
    let next = format!("{existing}{line}");
    std::fs::write(&catalog_path, next).unwrap();
}

fn seed_backup(
    root: &Path,
    policy_id: &str,
    backup_id: &str,
    created_at: &str,
    age_payload: &[u8],
) -> PathBuf {
    let filename = format!("{backup_id}.dsibackup.age");
    let backups_dir = root.join("backups");
    std::fs::create_dir_all(&backups_dir).unwrap();
    let file_path = backups_dir.join(&filename);
    std::fs::write(&file_path, age_payload).unwrap();

    let digest = sha256_bytes(age_payload);
    append_catalog_entry(
        root,
        "receipt",
        json!({
            "schemaVersion": 1,
            "backupId": backup_id,
            "runId": format!("run_{backup_id}"),
            "policyId": policy_id,
            "targetId": "managed-local",
            "scheduleSlot": "slot-1",
            "filename": filename,
            "size": age_payload.len(),
            "ciphertextSha256": digest,
            "manifestDigest": "b".repeat(64),
            "coverageDigest": "c".repeat(64),
            "creationVerified": true,
            "createdAt": created_at,
        }),
    );
    file_path
}

#[tokio::test]
async fn preview_is_read_only_and_handles_errors_and_guards() {
    let fix = Fixture::new("");

    // Auth guard
    let (status, _) = fix
        .call(
            "POST",
            "/api/workspace/retention/preview",
            Some(json!({"policyId": "p1"})),
            false,
        )
        .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);

    // Method guards
    for uri in [
        "/api/workspace/retention/preview",
        "/api/workspace/retention/apply",
    ] {
        let (get_status, _) = fix.call("GET", uri, None, true).await;
        assert_eq!(get_status, StatusCode::NOT_FOUND);
        let (put_status, _) = fix.call("PUT", uri, None, true).await;
        assert_eq!(put_status, StatusCode::METHOD_NOT_ALLOWED);
        let (opt_status, _) = fix.call("OPTIONS", uri, None, true).await;
        assert_eq!(opt_status, StatusCode::NO_CONTENT);
    }

    // Invalid policyId & missing policy
    let (bad_id_status, bad_id_body) = fix
        .call(
            "POST",
            "/api/workspace/retention/preview",
            Some(json!({"policyId": "../bad"})),
            true,
        )
        .await;
    assert_eq!(bad_id_status, StatusCode::BAD_REQUEST);
    assert_eq!(bad_id_body["code"], "invalid_payload");

    let (missing_status, missing_body) = fix
        .call(
            "POST",
            "/api/workspace/retention/preview",
            Some(json!({"policyId": "missing-policy"})),
            true,
        )
        .await;
    assert_eq!(missing_status, StatusCode::NOT_FOUND);
    assert_eq!(missing_body["code"], "not_found");

    // Bad timezone
    fix.write_policy(
        "p-badtz",
        json!({"policyId": "p-badtz", "targetId": "managed-local", "timezone": "Mars/Olympus"}),
    );
    let (tz_status, tz_body) = fix
        .call(
            "POST",
            "/api/workspace/retention/preview",
            Some(json!({"policyId": "p-badtz"})),
            true,
        )
        .await;
    assert_eq!(tz_status, StatusCode::BAD_REQUEST);
    assert!(
        tz_body["error"]
            .as_str()
            .unwrap_or("")
            .contains("Unknown IANA timezone")
    );

    // Read-only preview on clean policy must not create runtime dotdirs
    fix.write_policy(
        "p1",
        json!({"policyId": "p1", "targetId": "managed-local", "timezone": "UTC"}),
    );
    let (ok_status, ok_body) = fix
        .call(
            "POST",
            "/api/workspace/retention/preview",
            Some(json!({"policyId": "p1"})),
            true,
        )
        .await;
    assert_eq!(ok_status, StatusCode::OK);
    assert_eq!(ok_body["keep"], json!([]));
    assert_eq!(ok_body["trash"], json!([]));
    assert_eq!(ok_body["protected"], json!([]));
    assert!(ok_body.get("trashRecords").is_none());
    assert!(!fix.path().join(".backups").exists());
    assert!(!fix.path().join(".backup-control").exists());
    assert!(!fix.path().join(".backup-scheduler").exists());
    assert!(!fix.path().join(".backup-retention").exists());

    // Remote S3 target refuses with 503 NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED
    let targets_dir = fix.path().join(".backup-targets");
    std::fs::create_dir_all(&targets_dir).unwrap();
    std::fs::write(
        targets_dir.join("s3-rem.json"),
        json!({"targetId": "s3-rem", "kind": "s3", "bucket": "bkt"}).to_string(),
    )
    .unwrap();
    fix.write_policy(
        "p-s3",
        json!({"policyId": "p-s3", "targetId": "s3-rem", "timezone": "UTC"}),
    );
    let (s3_status, s3_body) = fix
        .call(
            "POST",
            "/api/workspace/retention/preview",
            Some(json!({"policyId": "p-s3"})),
            true,
        )
        .await;
    assert_eq!(s3_status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(
        s3_body["code"],
        "NATIVE_BACKUP_CATALOG_TARGET_STORE_UNMIGRATED"
    );
}

#[tokio::test]
async fn apply_refuses_in_default_mode_without_mutating_disk() {
    let fix = Fixture::new("");
    fix.write_policy(
        "p1",
        json!({"policyId": "p1", "targetId": "managed-local", "timezone": "UTC"}),
    );
    fix.write_retention_policy(
        "default",
        json!({
            "keepLast": 0,
            "keepHourly": 0,
            "keepDaily": 0,
            "keepWeekly": 0,
            "keepMonthly": 0,
            "minimumHealthyCopies": 1,
        }),
    );

    let valid_age = b"age-encryption.org/v1\n-> scrypt\n--- deadbeef\npayload";
    let root = fix.path().join(".backups");
    let old_file = seed_backup(&root, "p1", "bak_old", "2025-01-01T00:00:00Z", valid_age);
    let _new_file = seed_backup(&root, "p1", "bak_new", "2026-11-16T00:00:00Z", valid_age);

    let (status, body) = fix
        .call(
            "POST",
            "/api/workspace/retention/apply",
            Some(json!({"policyId": "p1"})),
            true,
        )
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    assert_eq!(body["code"], "NATIVE_BACKUP_RETENTION_WRITE_NOT_OWNED");
    assert!(old_file.is_file());
    assert!(!root.join(".trash").exists());
    assert!(!fix.path().join(".backup-scheduler").exists());
}

#[tokio::test]
async fn apply_executes_lifecycle_and_fail_closed_checks_under_python_disabled() {
    let fix = Fixture::new("python_disabled");
    fix.write_policy(
        "p1",
        json!({"policyId": "p1", "targetId": "managed-local", "timezone": "UTC"}),
    );
    fix.write_retention_policy(
        "default",
        json!({
            "keepLast": 1,
            "keepHourly": 0,
            "keepDaily": 0,
            "keepWeekly": 0,
            "keepMonthly": 0,
            "minimumHealthyCopies": 1,
            "trashGraceHours": 1,
        }),
    );

    let valid_age = b"age-encryption.org/v1\n-> scrypt\n--- deadbeef\npayload";
    let root = fix.path().join(".backups");
    let old_file = seed_backup(&root, "p1", "bak_old", "2024-01-01T00:00:00Z", valid_age);
    let mid_file = seed_backup(&root, "p1", "bak_mid", "2024-06-01T00:00:00Z", valid_age);
    let new_file = seed_backup(&root, "p1", "bak_new", "2026-11-16T00:00:00Z", valid_age);

    // Pin mid backup so it is protected
    append_catalog_entry(&root, "pin", json!({"backupId": "bak_mid", "pinned": true}));

    // Preview shows old -> trash, mid -> protected pinned, new -> keep
    let (prev_status, prev_body) = fix
        .call(
            "POST",
            "/api/workspace/retention/preview",
            Some(json!({"policyId": "p1"})),
            true,
        )
        .await;
    assert_eq!(prev_status, StatusCode::OK);
    assert_eq!(prev_body["trash"], json!(["bak_old"]));
    let keep = prev_body["keep"].as_array().unwrap();
    assert!(keep.contains(&json!("bak_mid")));
    assert!(keep.contains(&json!("bak_new")));

    // Dry run returns envelope without mutating disk
    let (dry_status, dry_body) = fix
        .call(
            "POST",
            "/api/workspace/retention/apply",
            Some(json!({"policyId": "p1", "dryRun": true, "preview": prev_body})),
            true,
        )
        .await;
    assert_eq!(dry_status, StatusCode::OK);
    assert_eq!(dry_body["applied"]["trashed"], json!([]));
    assert!(old_file.is_file());

    // Live apply moves bak_old to .trash and appends catalog entry + scheduler row
    let (apply_status, apply_body) = fix
        .call(
            "POST",
            "/api/workspace/retention/apply",
            Some(json!({"policyId": "p1", "preview": prev_body})),
            true,
        )
        .await;
    assert_eq!(apply_status, StatusCode::OK);
    assert_eq!(apply_body["applied"]["trashed"], json!(["bak_old"]));
    assert!(!old_file.exists());
    assert!(mid_file.is_file());
    assert!(new_file.is_file());
    assert!(root.join(".trash/bak_old/bak_old.dsibackup.age").is_file());
    assert!(root.join(".trash/bak_old/trash.journal.json").is_file());

    let sched_conn = Connection::open(fix.path().join(".backup-scheduler/scheduler.db")).unwrap();
    let run_status: String = sched_conn
        .query_row(
            "SELECT status FROM retention_runs WHERE policy_id = 'p1' LIMIT 1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(run_status, "completed");

    // Re-applying with stale preview returns 409 retention-stale-snapshot
    let (stale_status, stale_body) = fix
        .call(
            "POST",
            "/api/workspace/retention/apply",
            Some(json!({"policyId": "p1", "preview": prev_body})),
            true,
        )
        .await;
    assert_eq!(stale_status, StatusCode::CONFLICT);
    assert!(
        stale_body["error"]
            .as_str()
            .unwrap_or("")
            .contains("retention-stale-snapshot")
    );
}

#[tokio::test]
async fn apply_recovers_crash_journals_finalizes_expired_trash_and_rescues_pinned() {
    let fix = Fixture::new("python_disabled");
    fix.write_policy(
        "p1",
        json!({"policyId": "p1", "targetId": "managed-local", "timezone": "UTC"}),
    );
    fix.write_retention_policy(
        "default",
        json!({
            "keepLast": 1,
            "keepHourly": 0,
            "keepDaily": 0,
            "keepWeekly": 0,
            "keepMonthly": 0,
            "minimumHealthyCopies": 1,
            "trashGraceHours": 1,
        }),
    );

    let valid_age = b"age-encryption.org/v1\n-> scrypt\n--- deadbeef\npayload";
    let root = fix.path().join(".backups");
    let _keep = seed_backup(&root, "p1", "bak_keep", "2026-11-16T00:00:00Z", valid_age);

    // 1. Expired trashed backup -> should be permanently deleted during finalize
    let _exp_file = seed_backup(&root, "p1", "bak_exp", "2024-01-01T00:00:00Z", valid_age);
    let exp_trash_dir = root.join(".trash/bak_exp");
    std::fs::create_dir_all(&exp_trash_dir).unwrap();
    std::fs::rename(
        root.join("backups/bak_exp.dsibackup.age"),
        exp_trash_dir.join("bak_exp.dsibackup.age"),
    )
    .unwrap();
    append_catalog_entry(
        &root,
        "trash",
        json!({"backupId": "bak_exp", "trashedAt": "2024-02-01T00:00:00Z", "retentionRunId": "rr_prev"}),
    );

    // 2. Pinned while in trash -> should be rescued back to backups/
    let _pin_file = seed_backup(&root, "p1", "bak_rescue", "2024-01-02T00:00:00Z", valid_age);
    let pin_trash_dir = root.join(".trash/bak_rescue");
    std::fs::create_dir_all(&pin_trash_dir).unwrap();
    std::fs::rename(
        root.join("backups/bak_rescue.dsibackup.age"),
        pin_trash_dir.join("bak_rescue.dsibackup.age"),
    )
    .unwrap();
    append_catalog_entry(
        &root,
        "trash",
        json!({"backupId": "bak_rescue", "trashedAt": "2024-02-01T00:00:00Z", "retentionRunId": "rr_prev"}),
    );
    append_catalog_entry(
        &root,
        "pin",
        json!({"backupId": "bak_rescue", "pinned": true}),
    );

    // 3. Interrupted trash journal in intent phase -> should finish move & catalog append
    let _crash_file = seed_backup(&root, "p1", "bak_crash", "2024-01-03T00:00:00Z", valid_age);
    let crash_dir = root.join(".trash/bak_crash");
    std::fs::create_dir_all(&crash_dir).unwrap();
    std::fs::write(
        crash_dir.join("trash.journal.json"),
        json!({
            "schemaVersion": 1,
            "backupId": "bak_crash",
            "retentionRunId": "rr_crash",
            "filename": "bak_crash.dsibackup.age",
            "payloadNames": ["bak_crash.dsibackup.age"],
            "receiptNames": [],
            "phase": "intent",
            "recordedAt": "2026-11-16T00:00:00Z",
        })
        .to_string(),
    )
    .unwrap();

    let (status, body) = fix
        .call(
            "POST",
            "/api/workspace/retention/apply",
            Some(json!({"policyId": "p1"})),
            true,
        )
        .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(body["applied"]["recoveredTrash"], json!(["bak_crash"]));
    assert_eq!(body["finalized"]["deleted"], json!(["bak_exp"]));
    assert!(!root.join(".trash/bak_exp").exists());
    assert!(root.join("backups/bak_rescue.dsibackup.age").is_file());
    assert!(crash_dir.join("bak_crash.dsibackup.age").is_file());
    assert_eq!(
        serde_json::from_str::<Value>(
            &std::fs::read_to_string(crash_dir.join("trash.journal.json")).unwrap()
        )
        .unwrap()["phase"],
        "event-committed"
    );
}

#[tokio::test]
async fn apply_refuses_corrupt_catalog_and_lease_contention() {
    let fix = Fixture::new("python_disabled");
    fix.write_policy(
        "p1",
        json!({"policyId": "p1", "targetId": "managed-local", "timezone": "UTC"}),
    );
    let root = fix.path().join(".backups");
    let catalog_dir = root.join("catalog");
    std::fs::create_dir_all(&catalog_dir).unwrap();
    std::fs::write(catalog_dir.join("catalog-v1.jsonl"), "{corrupt-json\n").unwrap();

    let (corrupt_status, corrupt_body) = fix
        .call(
            "POST",
            "/api/workspace/retention/apply",
            Some(json!({"policyId": "p1"})),
            true,
        )
        .await;
    assert_eq!(corrupt_status, StatusCode::CONFLICT);
    assert!(
        corrupt_body["error"]
            .as_str()
            .unwrap_or("")
            .contains("Backup catalog is corrupt")
    );

    // Fix catalog, place active writer lease -> expect 423 Locked
    std::fs::remove_file(catalog_dir.join("catalog-v1.jsonl")).unwrap();
    std::fs::write(
        root.join(".writer-lease.json"),
        json!({
            "schemaVersion": 1,
            "targetId": "managed-local",
            "ownerRunId": "active-other",
            "ownerInstanceId": "other-inst",
            "fencingToken": 99,
            "acquiredAt": "2026-01-01T00:00:00Z",
            "leaseUntil": "2099-01-01T00:00:00Z",
        })
        .to_string(),
    )
    .unwrap();
    let (lease_status, _) = fix
        .call(
            "POST",
            "/api/workspace/retention/apply",
            Some(json!({"policyId": "p1"})),
            true,
        )
        .await;
    assert_eq!(lease_status, StatusCode::LOCKED);
}

#[test]
fn dummy_commit_marker_helper_linked() {
    assert!(!commit_marker_valid(&json!({})));
}
