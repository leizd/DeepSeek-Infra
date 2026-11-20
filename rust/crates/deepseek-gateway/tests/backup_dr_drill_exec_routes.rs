//! Integration tests for S6 isolated recovery drills:
//! - `POST /api/workspace/disaster-recovery/drills`
//! - `POST /api/workspace/disaster-recovery/drills/schedule/{policy_id}`

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_core::version_info;
use deepseek_gateway::create_production_app;
use deepseek_policy::backup_mirror::sha256_file;
use deepseek_policy::backup_projection::{FileRecord, snapshot_root};
use deepseek_policy::backup_unlock::{
    BACKUP_SCHEMA, PACKAGE_PURPOSE, encrypt_tree_zip_native, ephemeral_identity, write_zip_tree,
};
use deepseek_policy::python_json::dumps_compact;
use deepseek_storage::object_set::{ObjectInventoryEntry, object_inventory_digest};
use rusqlite::Connection;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use tower::ServiceExt;

const TEST_TOKEN: &str = "s6-drill-exec-token";
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

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn seed_drill_backup(backups_root: &Path, work: &Path) -> (String, String) {
    let (identity_secret, recipient) = ephemeral_identity();
    // `_session_dir` in backups.py requires the `backup_` prefix with an
    // alphanumeric-only remainder, so this fixture id must satisfy the same
    // rule the route validates before it looks at the seeded session.
    let backup_id = "backup_drillobject1".to_string();
    let rel = "payload/projects/p1/drill.txt";
    let body = b"isolated recovery drill payload\n";

    let comp_stage = work.join("stage-comp");
    std::fs::create_dir_all(comp_stage.join("payload/projects/p1")).unwrap();
    std::fs::write(comp_stage.join(rel), body).unwrap();
    let plain_zip = work.join("comp.zip");
    write_zip_tree(&comp_stage, &plain_zip).unwrap();
    let plain_size = std::fs::metadata(&plain_zip).unwrap().len();
    let plain_sha = sha256_file(&plain_zip).unwrap();
    let comp_age = work.join("comp.age");
    encrypt_tree_zip_native(
        &comp_stage,
        &comp_age,
        "age-recipient",
        None,
        &[recipient.clone()],
    )
    .unwrap();
    let comp_bytes = std::fs::read(&comp_age).unwrap();
    let p_sha = sha256_hex(&comp_bytes);
    let p_size = comp_bytes.len() as u64;
    let p_dir = backups_root.join("objects/sha256").join(&p_sha[..2]);
    std::fs::create_dir_all(&p_dir).unwrap();
    std::fs::write(p_dir.join(format!("{p_sha}.age")), &comp_bytes).unwrap();

    let rec = vec![FileRecord {
        contributor_id: "projects".into(),
        logical_path: rel.into(),
        size: body.len() as u64,
        sha256: sha256_hex(body),
    }];
    let manifest_bytes = serde_json::to_vec(&json!({
        "schemaVersion": BACKUP_SCHEMA,
        "purpose": PACKAGE_PURPOSE,
        "backupId": backup_id,
        "snapshotKind": "full",
        "storageProtocol": "object-set-v1",
        "source": {"version": version_info().version, "revision": "rev-drill"},
        "snapshot": {"rootDigest": snapshot_root(&rec)},
        "contributors": [{"id": "projects", "schemaVersion": 1}],
        "files": [{"contributorId": "projects", "path": rel, "size": body.len(), "sha256": sha256_hex(body)}],
    }))
    .unwrap();
    let component_map_bytes = serde_json::to_vec(&json!({
        "schemaVersion": 1,
        "paths": {rel: "proj-p1"},
        "components": {"proj-p1": [rel]},
    }))
    .unwrap();
    let payload_index_bytes = serde_json::to_vec(&json!({
        "schemaVersion": 1,
        "storageProtocol": "object-set-v1",
        "snapshotFormat": "full",
        "payloadComponents": {
            "proj-p1": {
                "ciphertextDigest": p_sha,
                "ciphertextSize": p_size,
                "plaintextSize": plain_size,
                "plaintextSha256": plain_sha,
            }
        }
    }))
    .unwrap();

    let mut sums = String::new();
    for (name, bytes) in [
        ("component-map.json", component_map_bytes.as_slice()),
        ("manifest.json", manifest_bytes.as_slice()),
        ("payload-index.json", payload_index_bytes.as_slice()),
    ] {
        use std::fmt::Write as _;
        let _ = writeln!(sums, "{}  {name}", sha256_hex(bytes));
    }

    let ctrl_stage = work.join("stage-ctrl");
    std::fs::create_dir_all(&ctrl_stage).unwrap();
    std::fs::write(ctrl_stage.join("component-map.json"), &component_map_bytes).unwrap();
    std::fs::write(ctrl_stage.join("manifest.json"), &manifest_bytes).unwrap();
    std::fs::write(ctrl_stage.join("payload-index.json"), &payload_index_bytes).unwrap();
    std::fs::write(ctrl_stage.join("checksums.sha256"), sums.as_bytes()).unwrap();
    let ctrl_age = work.join("ctrl.age");
    encrypt_tree_zip_native(&ctrl_stage, &ctrl_age, "age-recipient", None, &[recipient]).unwrap();
    let ctrl_bytes = std::fs::read(&ctrl_age).unwrap();
    let c_sha = sha256_hex(&ctrl_bytes);
    let c_size = ctrl_bytes.len() as u64;
    let c_dir = backups_root.join("objects/sha256").join(&c_sha[..2]);
    std::fs::create_dir_all(&c_dir).unwrap();
    std::fs::write(c_dir.join(format!("{c_sha}.age")), &ctrl_bytes).unwrap();

    let inv = vec![
        ObjectInventoryEntry {
            digest: c_sha.clone(),
            size: c_size,
        },
        ObjectInventoryEntry {
            digest: p_sha.clone(),
            size: p_size,
        },
    ];
    let osd = object_inventory_digest(&inv).unwrap();
    let mut objs: Vec<Value> = inv
        .into_iter()
        .map(|o| json!({"digest": o.digest, "size": o.size}))
        .collect();
    objs.sort_by(|a, b| a["digest"].as_str().cmp(&b["digest"].as_str()));
    let receipt = json!({
        "schemaVersion": 4,
        "backupId": backup_id,
        "policyId": "pol_drill",
        "targetId": "managed-local",
        "createdAt": "2026-10-10T00:00:00Z",
        "snapshotKind": "full",
        "storageProtocol": "object-set-v1",
        "controlObjectDigest": c_sha,
        "objectSetDigest": osd,
        "objects": objs,
        "size": c_size + p_size,
        "logicalBytes": body.len(),
        "pinned": false,
    });
    let r_bytes = serde_json::to_vec_pretty(&receipt).unwrap();
    std::fs::create_dir_all(backups_root.join("receipts")).unwrap();
    std::fs::write(
        backups_root
            .join("receipts")
            .join(format!("{backup_id}.json")),
        &r_bytes,
    )
    .unwrap();
    let r_dig = sha256_hex(&r_bytes);
    let mut cmap: BTreeMap<String, Value> = BTreeMap::new();
    cmap.insert("schemaVersion".into(), json!(4));
    cmap.insert("storageProtocol".into(), json!("object-set-v1"));
    cmap.insert("backupId".into(), json!(backup_id));
    cmap.insert("policyId".into(), json!("pol_drill"));
    cmap.insert("objectSetDigest".into(), json!(osd));
    cmap.insert("controlObjectDigest".into(), json!(c_sha));
    cmap.insert("receiptDigest".into(), json!(r_dig));
    cmap.insert("targetGeneration".into(), json!(1));
    let mut obj: Map<String, Value> = cmap.into_iter().collect();
    let hash = sha256_hex(dumps_compact(&Value::Object(obj.clone())).as_bytes());
    obj.insert("commitHash".into(), Value::String(hash));
    std::fs::create_dir_all(backups_root.join("commits/pol_drill")).unwrap();
    std::fs::write(
        backups_root.join("commits/pol_drill/c1.json"),
        serde_json::to_vec(&Value::Object(obj)).unwrap(),
    )
    .unwrap();
    (backup_id, identity_secret)
}

#[tokio::test]
async fn default_mode_denies_drill_and_schedule_routes_without_touching_disk() {
    let fixture = Fixture::new("default");
    let (st1, b1) = fixture
        .call(
            "POST",
            "/api/workspace/disaster-recovery/drills",
            Some(json!({"restoreId": "restore_abc123"})),
        )
        .await;
    assert_eq!(st1, StatusCode::CONFLICT);
    assert_eq!(b1["code"], "NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED");

    let (st2, b2) = fixture
        .call(
            "POST",
            "/api/workspace/disaster-recovery/drills/schedule/pol_1",
            Some(json!({})),
        )
        .await;
    assert_eq!(st2, StatusCode::CONFLICT);
    assert_eq!(b2["code"], "NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED");
    assert!(!fixture.root().join(".restore-staging").exists());
    assert!(!fixture.root().join(".backup-dr").exists());
}

#[tokio::test]
async fn manual_drill_executes_scrubs_plaintext_and_persists_ledger_evidence() {
    let fixture = Fixture::new("python_disabled");
    let backups_root = fixture.root().join(".backups");
    let (backup_id, identity_secret) =
        seed_drill_backup(&backups_root, &fixture.root().join("work"));

    // Reject unknown/missing key
    let (st_bad, _) = fixture
        .call(
            "POST",
            "/api/workspace/disaster-recovery/drills",
            Some(json!({"restoreId": "restore_1", "extra": true})),
        )
        .await;
    assert_eq!(st_bad, StatusCode::BAD_REQUEST);

    let (st_c, created) = fixture
        .call(
            "POST",
            "/api/workspace/restores/from-target",
            Some(json!({"targetId": "managed-local", "backupId": backup_id})),
        )
        .await;
    assert_eq!(st_c, StatusCode::OK, "created={created}");
    let restore_id = created["restoreId"].as_str().unwrap().to_string();

    let (st_s, _) = fixture
        .call(
            "PUT",
            &format!("/api/workspace/restores/{restore_id}/secret"),
            Some(json!({"kind": "age-identity", "secret": identity_secret})),
        )
        .await;
    assert_eq!(st_s, StatusCode::OK);

    let (st_d, drill) = fixture
        .call(
            "POST",
            "/api/workspace/disaster-recovery/drills",
            Some(json!({"restoreId": restore_id})),
        )
        .await;
    assert_eq!(st_d, StatusCode::OK, "drill={drill}");
    assert_eq!(drill["result"], "success");
    assert_eq!(drill["cleanupCompleted"], true);
    assert_eq!(drill["verifiedContributors"], 1);

    let session_dir = fixture.root().join(".restore-staging").join(&restore_id);
    assert!(session_dir.join("drill-result.json").is_file());
    assert!(!session_dir.join("drill-running.json").exists());
    assert!(!session_dir.join("extracted").exists());
    assert!(!session_dir.join("plan.json").exists());
    assert!(!session_dir.join("verified-plan.json").exists());

    // Idempotent second call returns persisted drill-result.json without re-needing secret
    let (st_d2, drill2) = fixture
        .call(
            "POST",
            "/api/workspace/disaster-recovery/drills",
            Some(json!({"restoreId": restore_id})),
        )
        .await;
    assert_eq!(st_d2, StatusCode::OK);
    assert_eq!(drill2["workspaceDigest"], drill["workspaceDigest"]);

    // Verify ledger row in .backup-dr/evidence.sqlite3
    let conn = Connection::open(fixture.root().join(".backup-dr/evidence.sqlite3")).unwrap();
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM drill_evidence WHERE drill_id = ? AND result = 'success'",
            [&restore_id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(count, 1);
}

#[tokio::test]
async fn scheduled_drill_skipped_when_disabled_and_blocked_without_credential() {
    let fixture = Fixture::new("python_disabled");
    let pol_dir = fixture.root().join(".backup-policies");
    std::fs::create_dir_all(&pol_dir).unwrap();
    std::fs::write(
        pol_dir.join("pol_skip.json"),
        serde_json::to_vec(&json!({
            "policyId": "pol_skip",
            "targetId": "managed-local",
            "recoveryDrill": {"enabled": false}
        }))
        .unwrap(),
    )
    .unwrap();
    std::fs::write(
        pol_dir.join("pol_block.json"),
        serde_json::to_vec(&json!({
            "policyId": "pol_block",
            "targetId": "managed-local",
            "recoveryDrill": {"enabled": true, "provider": "memory", "credentialRef": "missing-ref"}
        }))
        .unwrap(),
    )
    .unwrap();

    let (st_miss, _) = fixture
        .call(
            "POST",
            "/api/workspace/disaster-recovery/drills/schedule/pol_none",
            None,
        )
        .await;
    assert_eq!(st_miss, StatusCode::NOT_FOUND);

    let (st_skip, skip) = fixture
        .call(
            "POST",
            "/api/workspace/disaster-recovery/drills/schedule/pol_skip",
            None,
        )
        .await;
    assert_eq!(st_skip, StatusCode::OK);
    assert_eq!(skip["status"], "skipped");
    assert_eq!(skip["reason"], "drill-disabled");

    let (st_blk, blk) = fixture
        .call(
            "POST",
            "/api/workspace/disaster-recovery/drills/schedule/pol_block",
            None,
        )
        .await;
    assert_eq!(st_blk, StatusCode::OK);
    assert_eq!(blk["status"], "blocked");
    assert_eq!(blk["reason"], "unlock-required");
}
