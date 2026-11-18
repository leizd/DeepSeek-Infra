//! Integration tests for S5 native remote restore routes (`restore_staging_store`):
//! - `POST /api/workspace/restores/from-target`
//! - `POST /api/workspace/restores/from-target/preview`
//! - `POST /api/workspace/restores/{restore_id}/fetch`
//! - `POST /api/workspace/restores/{restore_id}/preflight`
//! - `POST /api/workspace/restores/{restore_id}/materialize`

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
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use tower::ServiceExt;

const TEST_TOKEN: &str = "s5-remote-restore-token";
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

fn write_object(backups_root: &Path, cipher_bytes: &[u8]) -> (String, u64) {
    let digest = sha256_hex(cipher_bytes);
    let obj_dir = backups_root
        .join("objects")
        .join("sha256")
        .join(&digest[..2]);
    std::fs::create_dir_all(&obj_dir).unwrap();
    std::fs::write(obj_dir.join(format!("{digest}.age")), cipher_bytes).unwrap();
    (digest, cipher_bytes.len() as u64)
}

fn signed_commit(mut obj: Map<String, Value>) -> Value {
    obj.remove("commitHash");
    let canonical = dumps_compact(&Value::Object(obj.clone()));
    let hash = sha256_hex(canonical.as_bytes());
    obj.insert("commitHash".into(), Value::String(hash));
    Value::Object(obj)
}

fn seed_object_set_backup(backups_root: &Path, work: &Path) -> (String, String, String) {
    let (identity_secret, recipient) = ephemeral_identity();
    let backup_id = "bk_obj_s5_1".to_string();
    let app_rel = "payload/projects/p1/note.txt";
    let mem_rel = "payload/memory/facts.json";
    let app_bytes = b"hello object set project\n";
    let mem_bytes = b"{\"k\":\"v\"}\n";

    let build_component = |name: &str, files: &[(&str, &[u8])]| {
        let comp_stage = work.join(format!("stage-{name}"));
        for (rel, bytes) in files {
            let full = comp_stage.join(rel);
            std::fs::create_dir_all(full.parent().unwrap()).unwrap();
            std::fs::write(full, bytes).unwrap();
        }
        let plain_zip = work.join(format!("{name}.zip"));
        write_zip_tree(&comp_stage, &plain_zip).unwrap();
        let plain_size = std::fs::metadata(&plain_zip).unwrap().len();
        let plain_sha = sha256_file(&plain_zip).unwrap();
        let age_path = work.join(format!("{name}.age"));
        encrypt_tree_zip_native(
            &comp_stage,
            &age_path,
            "age-recipient",
            None,
            &[recipient.clone()],
        )
        .unwrap();
        let age_bytes = std::fs::read(&age_path).unwrap();
        let (cipher_sha, cipher_size) = write_object(backups_root, &age_bytes);
        (cipher_sha, cipher_size, plain_size, plain_sha)
    };

    let (p_sha, p_size, p_psize, p_psha) = build_component("proj-p1", &[(app_rel, app_bytes)]);
    let (m_sha, m_size, m_psize, m_psha) = build_component("mem", &[(mem_rel, mem_bytes)]);

    let records = vec![
        FileRecord {
            contributor_id: "projects".into(),
            logical_path: app_rel.into(),
            size: app_bytes.len() as u64,
            sha256: sha256_hex(app_bytes),
        },
        FileRecord {
            contributor_id: "memory".into(),
            logical_path: mem_rel.into(),
            size: mem_bytes.len() as u64,
            sha256: sha256_hex(mem_bytes),
        },
    ];
    let root_digest = snapshot_root(&records);
    let manifest_bytes = serde_json::to_vec(&json!({
        "schemaVersion": BACKUP_SCHEMA,
        "purpose": PACKAGE_PURPOSE,
        "backupId": backup_id,
        "snapshotKind": "full",
        "storageProtocol": "object-set-v1",
        "source": {"version": version_info().version, "revision": "rev-s5"},
        "snapshot": {"rootDigest": root_digest},
        "contributors": [
            {"id": "projects", "schemaVersion": 1},
            {"id": "memory", "schemaVersion": 1},
        ],
        "files": [
            {"contributorId": "projects", "path": app_rel, "size": app_bytes.len(), "sha256": sha256_hex(app_bytes)},
            {"contributorId": "memory", "path": mem_rel, "size": mem_bytes.len(), "sha256": sha256_hex(mem_bytes)},
        ],
    }))
    .unwrap();
    let component_map_bytes = serde_json::to_vec(&json!({
        "schemaVersion": 1,
        "paths": {
            app_rel: "proj-p1",
            mem_rel: "mem",
        },
        "components": {
            "proj-p1": [app_rel],
            "mem": [mem_rel],
        }
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
                "plaintextSize": p_psize,
                "plaintextSha256": p_psha,
            },
            "mem": {
                "ciphertextDigest": m_sha,
                "ciphertextSize": m_size,
                "plaintextSize": m_psize,
                "plaintextSha256": m_psha,
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

    let ctrl_stage = work.join("stage-control");
    std::fs::create_dir_all(&ctrl_stage).unwrap();
    std::fs::write(ctrl_stage.join("component-map.json"), &component_map_bytes).unwrap();
    std::fs::write(ctrl_stage.join("manifest.json"), &manifest_bytes).unwrap();
    std::fs::write(ctrl_stage.join("payload-index.json"), &payload_index_bytes).unwrap();
    std::fs::write(ctrl_stage.join("checksums.sha256"), sums.as_bytes()).unwrap();
    let ctrl_age = work.join("ctrl.age");
    encrypt_tree_zip_native(&ctrl_stage, &ctrl_age, "age-recipient", None, &[recipient]).unwrap();
    let ctrl_bytes = std::fs::read(&ctrl_age).unwrap();
    let (c_sha, c_size) = write_object(backups_root, &ctrl_bytes);

    let inv = vec![
        ObjectInventoryEntry {
            digest: c_sha.clone(),
            size: c_size,
        },
        ObjectInventoryEntry {
            digest: p_sha,
            size: p_size,
        },
        ObjectInventoryEntry {
            digest: m_sha,
            size: m_size,
        },
    ];
    let osd = object_inventory_digest(&inv).unwrap();
    let mut objects_vec: Vec<Value> = inv
        .into_iter()
        .map(|o| json!({"digest": o.digest, "size": o.size}))
        .collect();
    objects_vec.sort_by(|a, b| a["digest"].as_str().cmp(&b["digest"].as_str()));

    let receipt = json!({
        "schemaVersion": 4,
        "backupId": backup_id,
        "filename": format!("{backup_id}.object-set"),
        "policyId": "pol_s5",
        "targetId": "managed-local",
        "createdAt": "2026-10-10T00:00:00Z",
        "snapshotKind": "full",
        "storageProtocol": "object-set-v1",
        "controlObjectDigest": c_sha,
        "objectSetDigest": osd,
        "objects": objects_vec,
        "size": c_size + p_size + m_size,
        "logicalBytes": app_bytes.len() + mem_bytes.len(),
        "pinned": false,
    });
    let receipt_bytes = serde_json::to_vec_pretty(&receipt).unwrap();
    let receipts_dir = backups_root.join("receipts");
    std::fs::create_dir_all(&receipts_dir).unwrap();
    std::fs::write(
        receipts_dir.join(format!("{backup_id}.json")),
        &receipt_bytes,
    )
    .unwrap();
    let receipt_digest = sha256_hex(&receipt_bytes);

    let mut cmap: BTreeMap<String, Value> = BTreeMap::new();
    cmap.insert("schemaVersion".into(), json!(4));
    cmap.insert("storageProtocol".into(), json!("object-set-v1"));
    cmap.insert("backupId".into(), json!(backup_id));
    cmap.insert("policyId".into(), json!("pol_s5"));
    cmap.insert("objectSetDigest".into(), json!(osd));
    cmap.insert("controlObjectDigest".into(), json!(c_sha));
    cmap.insert("receiptDigest".into(), json!(receipt_digest));
    cmap.insert("targetGeneration".into(), json!(1));
    cmap.insert("previousCommitHash".into(), json!("0".repeat(64)));
    let commit = signed_commit(cmap.into_iter().collect());
    let commits_dir = backups_root.join("commits").join("pol_s5");
    std::fs::create_dir_all(&commits_dir).unwrap();
    std::fs::write(
        commits_dir.join(format!("{backup_id}.json")),
        serde_json::to_vec(&commit).unwrap(),
    )
    .unwrap();
    std::fs::write(
        backups_root.join("catalog.jsonl"),
        format!("{}\n", dumps_compact(&receipt)),
    )
    .unwrap();

    (backup_id, identity_secret, c_sha)
}

#[tokio::test]
async fn default_mode_denies_all_remote_restore_routes_without_touching_disk() {
    let fixture = Fixture::new("default");
    for (path, body) in [
        (
            "/api/workspace/restores/from-target",
            json!({"targetId": "managed-local", "backupId": "bk_1"}),
        ),
        (
            "/api/workspace/restores/from-target/preview",
            json!({"targetId": "managed-local", "backupId": "bk_1", "selection": {"contributors": ["projects"]}}),
        ),
        ("/api/workspace/restores/restore_abc123/fetch", json!({})),
        (
            "/api/workspace/restores/restore_abc123/preflight",
            json!({}),
        ),
        (
            "/api/workspace/restores/restore_abc123/materialize",
            json!({}),
        ),
    ] {
        let (st, payload) = fixture.call("POST", path, Some(body)).await;
        assert_eq!(st, StatusCode::CONFLICT, "{path} -> {payload}");
        assert_eq!(
            payload["code"],
            "NATIVE_RESTORE_STAGING_STORE_WRITE_NOT_OWNED"
        );
    }
    assert!(!fixture.root().join(".restore-staging").exists());
    assert!(!fixture.root().join(".backup-component-cache").exists());
}

#[tokio::test]
async fn object_set_from_target_preview_fetch_preflight_materialize_e2e() {
    let fixture = Fixture::new("python_disabled");
    let backups_root = fixture.root().join(".backups");
    let work = fixture.root().join("work-build");
    let (backup_id, identity_secret, _) = seed_object_set_backup(&backups_root, &work);

    // 1. Create remote restore session with frozen selective projection
    let (st, created) = fixture
        .call(
            "POST",
            "/api/workspace/restores/from-target",
            Some(json!({
                "targetId": "managed-local",
                "backupId": backup_id,
                "selection": {"contributors": ["projects"], "projectIds": ["p1"]},
            })),
        )
        .await;
    assert_eq!(st, StatusCode::OK, "created={created}");
    let restore_id = created["restoreId"].as_str().unwrap().to_string();
    assert_eq!(created["phase"], "fetching-controls");
    assert_eq!(created["storageProtocol"], "object-set-v1");

    // Resume with mismatched selection -> 409
    let (st_mismatch, _) = fixture
        .call(
            "POST",
            "/api/workspace/restores/from-target",
            Some(json!({
                "targetId": "managed-local",
                "backupId": backup_id,
                "restoreId": restore_id,
                "selection": {"contributors": ["memory"]},
            })),
        )
        .await;
    assert_eq!(st_mismatch, StatusCode::CONFLICT);

    // 2. Preview requires secret first after control fetch, then plans selective component closure
    let (st_prev1, prev1) = fixture
        .call(
            "POST",
            "/api/workspace/restores/from-target/preview",
            Some(json!({
                "targetId": "managed-local",
                "backupId": backup_id,
                "restoreId": restore_id,
                "selection": {"contributors": ["projects"], "projectIds": ["p1"]},
            })),
        )
        .await;
    assert_eq!(st_prev1, StatusCode::OK, "prev1={prev1}");
    assert_eq!(prev1["requiresSecret"], true);

    // Arm secret via restore secret route
    let (st_sec, _) = fixture
        .call(
            "PUT",
            &format!("/api/workspace/restores/{restore_id}/secret"),
            Some(json!({"kind": "age-identity", "secret": identity_secret})),
        )
        .await;
    assert_eq!(st_sec, StatusCode::OK);

    // Preflight rejects client override payload with 400
    let (st_pre_bad, _) = fixture
        .call(
            "POST",
            &format!("/api/workspace/restores/{restore_id}/preflight"),
            Some(json!({"freeDiskBytes": 999})),
        )
        .await;
    assert_eq!(st_pre_bad, StatusCode::BAD_REQUEST);

    // 3. Preview plans projection & verified-plan.json (selects 1 of 2 components)
    let (st_prev2, prev2) = fixture
        .call(
            "POST",
            "/api/workspace/restores/from-target/preview",
            Some(json!({
                "targetId": "managed-local",
                "backupId": backup_id,
                "restoreId": restore_id,
                "selection": {"contributors": ["projects"], "projectIds": ["p1"]},
            })),
        )
        .await;
    assert_eq!(st_prev2, StatusCode::OK, "prev2={prev2}");
    assert_eq!(prev2["phase"], "preview-planned");
    assert_eq!(prev2["projection"]["selectiveFetchSupported"], true);
    assert_eq!(prev2["projection"]["networkSelective"], true);
    assert_eq!(prev2["projection"]["requiredComponents"], 1);
    assert_eq!(prev2["projection"]["totalComponents"], 2);
    assert!(
        fixture
            .root()
            .join(".restore-staging")
            .join(&restore_id)
            .join("verified-plan.json")
            .is_file()
    );

    // 4. Preflight evaluates component availability & disk headroom cleanly
    let (st_pre, pre) = fixture
        .call(
            "POST",
            &format!("/api/workspace/restores/{restore_id}/preflight"),
            Some(json!({})),
        )
        .await;
    assert_eq!(st_pre, StatusCode::OK, "pre={pre}");
    assert_eq!(pre["ready"], true);
    assert_eq!(pre["closure"]["requiredComponents"], 1);

    // 5. Fetch selected component cleanly into component cache
    let (st_fetch, fetched) = fixture
        .call(
            "POST",
            &format!("/api/workspace/restores/{restore_id}/fetch"),
            Some(json!({})),
        )
        .await;
    assert_eq!(st_fetch, StatusCode::OK, "fetched={fetched}");
    assert_eq!(fetched["phase"], "components-fetched");
    assert_eq!(fetched["requiredComponents"], 1);

    // 6. Materialize + prepare federated transaction
    let (st_mat, mat) = fixture
        .call(
            "POST",
            &format!("/api/workspace/restores/{restore_id}/materialize"),
            Some(json!({"mode": "merge", "previousEpoch": "legacy"})),
        )
        .await;
    assert_eq!(st_mat, StatusCode::OK, "mat={mat}");
    assert_eq!(mat["phase"], "prepared");
    assert_eq!(mat["materializedTreeVerified"], true);
    let restored_file = fixture
        .root()
        .join(".restore-staging")
        .join(&restore_id)
        .join("extracted/payload/projects/p1/note.txt");
    assert_eq!(
        std::fs::read(&restored_file).unwrap(),
        b"hello object set project\n"
    );
    assert!(
        !fixture
            .root()
            .join(".restore-staging")
            .join(&restore_id)
            .join("extracted/payload/memory/facts.json")
            .exists()
    );
}
