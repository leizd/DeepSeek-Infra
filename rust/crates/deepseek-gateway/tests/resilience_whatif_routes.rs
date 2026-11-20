//! Integration tests for `POST /api/workspace/resilience/whatif` (Slice 7a).

use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use deepseek_policy::backup_projection::sha256_bytes;
use deepseek_policy::python_json::dumps_compact;
use rusqlite::Connection;
use serde_json::{Map, Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "resilience-whatif-test-token";
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
    fn set(pairs: &[(&'static str, Option<&str>)]) -> Self {
        let saved = pairs
            .iter()
            .map(|(name, value)| {
                let previous = std::env::var(name).ok();
                match value {
                    Some(v) => unsafe { std::env::set_var(name, v) },
                    None => unsafe { std::env::remove_var(name) },
                }
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

fn build_app(root: &Path) -> (tempfile::TempDir, EnvGuard, axum::Router) {
    let static_root = tempfile::tempdir().unwrap();
    let ui = static_root.path().join("ui");
    fs::create_dir_all(&ui).unwrap();
    fs::write(ui.join("index.html"), "<main>ui</main>").unwrap();
    let root_str = root.to_string_lossy().into_owned();
    let env = EnvGuard::set(&[
        ("DEEPSEEK_INFRA_ROOT", Some(&root_str)),
        ("AUTH_TOKEN", Some(TEST_TOKEN)),
        ("AUTH_DISABLED", Some("")),
        ("AUTH_ALLOWED_HOSTS", Some("")),
        ("HOST", Some("")),
        ("DEEPSEEK_CONTROL_AUTHORITY_MODE", None),
        ("DEEPSEEK_CONTROL_AUTHORITY_BOOTSTRAP", None),
        ("DEEPSEEK_CONTROL_AUTHORITY_REPLICAS", None),
        ("DEEPSEEK_CONTROL_AUTHORITY_MIN_DURABLE_REPLICAS", None),
    ]);
    let app = create_production_app(static_root.path()).unwrap();
    (static_root, env, app)
}

async fn call_json(
    app: &axum::Router,
    method: &str,
    uri: &str,
    body_bytes: Option<&[u8]>,
) -> (StatusCode, Value) {
    let builder = Request::builder()
        .method(method)
        .uri(uri)
        .header(header::HOST, "127.0.0.1:8080")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
    let req = if let Some(bytes) = body_bytes {
        builder
            .header(header::CONTENT_TYPE, "application/json")
            .header(header::CONTENT_LENGTH, bytes.len().to_string())
            .body(Body::from(bytes.to_vec()))
            .unwrap()
    } else {
        builder.body(Body::empty()).unwrap()
    };
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let raw = to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap()
        .to_vec();
    let val = if raw.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&raw).unwrap_or(Value::Null)
    };
    (status, val)
}

fn digest_compact(value: &Value) -> String {
    sha256_bytes(dumps_compact(value).as_bytes())
}

fn seed_whatif_workspace(root: &Path, target_dir: &Path, with_healthy_copy: bool) {
    fs::create_dir_all(target_dir).unwrap();
    fs::write(
        target_dir.join(".deepseek-infra-backup-target.json"),
        serde_json::to_vec(&json!({
            "schemaVersion": 3,
            "targetId": "target_a",
            "targetNonce": "nonce-a",
            "targetGeneration": 1,
        }))
        .unwrap(),
    )
    .unwrap();
    fs::write(target_dir.join("sample.enc"), b"hello-world").unwrap();

    fs::create_dir_all(root.join(".backup-control")).unwrap();
    let ctrl = Connection::open(root.join(".backup-control").join("control.sqlite3")).unwrap();
    let target_payload = json!({
        "targetId": "target_a",
        "label": "Target A",
        "kind": "filesystem",
        "path": target_dir.to_string_lossy(),
        "targetNonce": "nonce-a",
        "failureDomain": "fd-a",
    });
    let policy_payload = json!({
        "policyId": "policy_a",
        "name": "Policy A",
        "policyRevision": 1,
        "targetId": "target_a",
        "schedule": {"cron": "0 * * * *", "timezone": "UTC"},
        "replication": {"enabled": true, "minCommittedCopies": 1, "minFailureDomains": 1},
        "placement": {"minFreeBytes": 1000},
    });
    let head_digest = digest_compact(&json!({"gen": 1}));
    let payload_digest = digest_compact(&json!({"p": 1}));
    ctrl.execute_batch(
        "CREATE TABLE control_boot_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            boot_epoch INTEGER NOT NULL,
            recovery_state TEXT NOT NULL,
            reason TEXT,
            updated_at TEXT NOT NULL
         );
         CREATE TABLE control_authority_head (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            authority_generation INTEGER NOT NULL,
            authority_digest TEXT NOT NULL,
            previous_digest TEXT,
            payload_digest TEXT NOT NULL,
            updated_at TEXT NOT NULL
         );
         CREATE TABLE control_policies (
            policy_id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
         );
         CREATE TABLE control_targets (
            target_id TEXT PRIMARY KEY,
            generation INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
         );
         CREATE TABLE target_index_coverage (
            target_id TEXT PRIMARY KEY,
            state TEXT NOT NULL
         );",
    )
    .unwrap();
    ctrl.execute(
        "INSERT INTO control_boot_state VALUES (1, 1, 'active', NULL, '2026-11-18T00:00:00Z')",
        [],
    )
    .unwrap();
    ctrl.execute(
        "INSERT INTO control_authority_head VALUES (1, 1, ?, NULL, ?, '2026-11-18T00:00:00Z')",
        rusqlite::params![head_digest, payload_digest],
    )
    .unwrap();
    ctrl.execute(
        "INSERT INTO control_policies VALUES ('policy_a', 1, ?, '2026-11-18T00:00:00Z')",
        [policy_payload.to_string()],
    )
    .unwrap();
    ctrl.execute(
        "INSERT INTO control_targets VALUES ('target_a', 1, ?, '2026-11-18T00:00:00Z')",
        [target_payload.to_string()],
    )
    .unwrap();
    ctrl.execute(
        "INSERT INTO target_index_coverage VALUES ('target_a', 'complete')",
        [],
    )
    .unwrap();

    fs::create_dir_all(root.join(".resilience-capacity")).unwrap();
    let cap = Connection::open(root.join(".resilience-capacity").join("capacity.sqlite3")).unwrap();
    let fc_json = json!({
        "targetId": "target_a",
        "horizonDays": 90,
        "forecastStatus": "OK",
        "p50FreeBytes": 60_000_000_000i64,
        "p90FreeBytes": 50_000_000_000i64,
        "forecastDigest": digest_compact(&json!({"fc": 1})),
        "capacityObservationSetDigest": digest_compact(&json!({"obs": 1})),
    });
    cap.execute_batch(
        "CREATE TABLE resilience_capacity_observations (
            observation_key TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            target_incarnation TEXT NOT NULL,
            capacity_revision TEXT NOT NULL,
            observed_at TEXT NOT NULL
         );
         CREATE TABLE resilience_forecast_records (
            forecast_id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            target_incarnation TEXT NOT NULL,
            capacity_revision TEXT NOT NULL,
            horizon_days INTEGER NOT NULL,
            forecasted_at TEXT NOT NULL,
            evaluation_due_at TEXT NOT NULL,
            p50_free_bytes INTEGER NOT NULL,
            p90_free_bytes INTEGER NOT NULL,
            forecast_digest TEXT NOT NULL,
            observation_set_digest TEXT NOT NULL,
            forecast_json TEXT NOT NULL,
            status TEXT NOT NULL,
            actual_observation_key TEXT,
            backtest_key TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
         );",
    )
    .unwrap();
    cap.execute(
        "INSERT INTO resilience_capacity_observations VALUES ('obs-1', 'target_a', 'inc-1', 'rev-1', '2026-11-18T00:00:00Z')",
        [],
    )
    .unwrap();
    cap.execute(
        "INSERT INTO resilience_forecast_records VALUES (
            'fc-1', 'target_a', 'inc-1', 'rev-1', 90,
            '2026-11-18T00:00:00Z', '2027-02-16T00:00:00Z',
            60000000000, 50000000000, ?, ?, ?, 'ACTIVE', NULL, NULL,
            '2026-11-18T00:00:00Z', '2026-11-18T00:00:00Z'
         )",
        rusqlite::params![
            fc_json["forecastDigest"].as_str().unwrap(),
            fc_json["capacityObservationSetDigest"].as_str().unwrap(),
            fc_json.to_string(),
        ],
    )
    .unwrap();

    fs::create_dir_all(root.join(".resilience-cost")).unwrap();
    let cost = Connection::open(root.join(".resilience-cost").join("cost.sqlite3")).unwrap();
    let cat_body = json!({
        "priceCatalogVersion": 1,
        "targets": {
            "target_a": {"storagePerGiBMonth": 0.02, "egressPerGiB": 0.01}
        }
    });
    let cat_dig = digest_compact(&cat_body);
    cost.execute_batch(
        "CREATE TABLE resilience_price_catalogs (
            price_catalog_version INTEGER PRIMARY KEY,
            catalog_json TEXT NOT NULL,
            catalog_digest TEXT NOT NULL,
            created_at TEXT NOT NULL
         );",
    )
    .unwrap();
    cost.execute(
        "INSERT INTO resilience_price_catalogs VALUES (1, ?, ?, '2026-11-18T00:00:00Z')",
        rusqlite::params![cat_body.to_string(), cat_dig],
    )
    .unwrap();

    if with_healthy_copy {
        fs::create_dir_all(root.join(".backup-dr")).unwrap();
        let dr = Connection::open(root.join(".backup-dr").join("evidence.sqlite3")).unwrap();
        dr.execute_batch(
            "CREATE TABLE recovery_points (
                target_id TEXT NOT NULL,
                policy_id TEXT NOT NULL,
                backup_id TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                snapshot_kind TEXT NOT NULL,
                parent_backup_id TEXT,
                chain_digest TEXT,
                chain_length INTEGER NOT NULL,
                ciphertext_bytes INTEGER NOT NULL,
                logical_bytes INTEGER NOT NULL,
                recoverable INTEGER NOT NULL,
                verified_at TEXT NOT NULL,
                storage_protocol TEXT,
                metadata_json TEXT
             );
             CREATE TABLE recovery_point_copies (
                logical_id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id TEXT NOT NULL,
                policy_id TEXT NOT NULL,
                backup_id TEXT NOT NULL,
                committed_at TEXT NOT NULL,
                recoverable INTEGER NOT NULL,
                state TEXT NOT NULL,
                object_set_digest TEXT,
                role TEXT NOT NULL,
                metadata_json TEXT
             );",
        )
        .unwrap();
        dr.execute(
            "INSERT INTO recovery_points VALUES (
                'target_a', 'policy_a', 'backup_1', '2026-11-18T01:00:00Z',
                'full', NULL, NULL, 1, 100, 100, 1, '2026-11-18T01:00:00Z',
                'object-set-v1', '{}'
             )",
            [],
        )
        .unwrap();
        dr.execute(
            "INSERT INTO recovery_point_copies (
                target_id, policy_id, backup_id, committed_at, recoverable,
                state, object_set_digest, role, metadata_json
             ) VALUES (
                'target_a', 'policy_a', 'backup_1', '2026-11-18T01:00:00Z',
                1, 'healthy', NULL, 'primary', '{}'
             )",
            [],
        )
        .unwrap();
    }
}

#[tokio::test]
async fn whatif_method_and_payload_validation_match_oracle() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app(root.path());

    let (st_get, _) = call_json(&app, "GET", "/api/workspace/resilience/whatif", None).await;
    assert_eq!(st_get, StatusCode::NOT_FOUND);

    let (st_opt, _) = call_json(&app, "OPTIONS", "/api/workspace/resilience/whatif", None).await;
    assert_eq!(st_opt, StatusCode::NO_CONTENT);

    let (st_put, _) = call_json(&app, "PUT", "/api/workspace/resilience/whatif", None).await;
    assert_eq!(st_put, StatusCode::METHOD_NOT_ALLOWED);

    let bad_bodies: Vec<(Vec<u8>, &str)> = vec![
        (b"{}".to_vec(), "candidate only"),
        (
            b"{\"candidate\":{\"policyId\":\"p\"},\"baseline\":{\"committedCopies\":99}}".to_vec(),
            "candidate only",
        ),
        (b"{\"candidate\":{}}".to_vec(), "candidate is required"),
        (
            b"{\"candidate\":{\"targetId\":\"t\"}}".to_vec(),
            "policyId is required",
        ),
        (
            b"{\"candidate\":{\"policyId\":\"p\"}}".to_vec(),
            "targetId or candidate.destTargetId is required",
        ),
        (
            b"{\"candidate\":{\"policyId\":\"p\",\"targetId\":\"t\",\"operation\":\"DELETE\"}}"
                .to_vec(),
            "KEEP, ADD_REPLICA, or REBALANCE",
        ),
        (
            b"{\"candidate\":{\"policyId\":\"p\",\"targetId\":\"t\",\"forecastHorizonDays\":7}}"
                .to_vec(),
            "must be 30 or 90",
        ),
        (
            b"{\"candidate\":{\"policyId\":\"p\",\"targetId\":\"t\",\"storedBytes\":true}}"
                .to_vec(),
            "must be an integer",
        ),
        (
            b"{\"candidate\":{\"policyId\":\"p\",\"targetId\":\"t\",\"storedBytes\":-1}}".to_vec(),
            "must be non-negative",
        ),
        (
            b"{\"candidate\":{\"policyId\":\"p\",\"targetId\":\"t\",\"baseline\":{}}}".to_vec(),
            "caller-controlled present truth is forbidden",
        ),
    ];

    for (raw, needle) in bad_bodies {
        let (st, val) =
            call_json(&app, "POST", "/api/workspace/resilience/whatif", Some(&raw)).await;
        assert_eq!(
            st,
            StatusCode::BAD_REQUEST,
            "expected 400 for {needle}: {val}"
        );
        assert!(
            val["error"].as_str().unwrap_or_default().contains(needle),
            "expected '{needle}' in {}",
            val
        );
    }
}

#[tokio::test]
async fn whatif_missing_truth_fails_closed_503_without_creating_dirs() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app(root.path());

    let raw = serde_json::to_vec(&json!({
        "candidate": {"policyId": "missing", "targetId": "missing", "operation": "KEEP"}
    }))
    .unwrap();
    let (st, val) = call_json(&app, "POST", "/api/workspace/resilience/whatif", Some(&raw)).await;
    assert_eq!(st, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(val["error"], "POLICY_TRUTH_UNAVAILABLE");
    assert_eq!(val["code"], "invalid_request");

    for dotdir in [
        ".backup-control",
        ".backup-dr",
        ".resilience-capacity",
        ".resilience-cost",
        ".resilience-journal",
        ".resilience-risk",
        ".resilience-slo",
    ] {
        assert!(
            !root.path().join(dotdir).exists(),
            "{dotdir} must not be created"
        );
    }
}

#[tokio::test]
async fn whatif_full_simulation_is_zero_mutation_and_digest_bound() {
    let _lock = EnvLock::acquire();
    let base = tempfile::tempdir().unwrap();
    let workspace = base.path().join("workspace");
    let target_dir = base.path().join("ext_targets").join("target_a");
    fs::create_dir_all(&workspace).unwrap();

    // First verify REJECTED when no healthy copy exists yet.
    seed_whatif_workspace(&workspace, &target_dir, false);
    let (_static_a, _env_a, app_a) = build_app(&workspace);
    let req_body = serde_json::to_vec(&json!({
        "candidate": {
            "policyId": "policy_a",
            "targetId": "target_a",
            "operation": "KEEP",
            "storedBytes": 1_073_741_824i64,
        }
    }))
    .unwrap();
    let (st_rej, body_rej) = call_json(
        &app_a,
        "POST",
        "/api/workspace/resilience/whatif",
        Some(&req_body),
    )
    .await;
    assert_eq!(st_rej, StatusCode::OK, "body: {body_rej}");
    assert_eq!(body_rej["status"], "REJECTED");
    assert_eq!(body_rej["candidatePlan"]["status"], "NO_SAFE_CANDIDATE");
    assert_eq!(body_rej["cost"]["monthlyCost"], 0.02);
    assert_eq!(body_rej["simulation"]["stateUnchanged"], true);
    assert_eq!(body_rej["sideEffectsObserved"], 0);
    assert_eq!(
        body_rej["simulation"]["storageInventoryBefore"][0]["objectCount"],
        2
    );
    assert!(!workspace.join(".resilience-risk").exists());
    assert!(!workspace.join(".resilience-slo").exists());

    // Now seed healthy copy and verify OK + exact whatIfDigest recomputation.
    drop(_env_a);
    let ws_ok = base.path().join("workspace_ok");
    let target_ok = base.path().join("ext_targets_ok").join("target_a");
    fs::create_dir_all(&ws_ok).unwrap();
    seed_whatif_workspace(&ws_ok, &target_ok, true);
    let (_static_b, _env_b, app_b) = build_app(&ws_ok);
    let (st_ok, body_ok) = call_json(
        &app_b,
        "POST",
        "/api/workspace/resilience/whatif",
        Some(&req_body),
    )
    .await;
    assert_eq!(st_ok, StatusCode::OK, "body: {body_ok}");
    assert_eq!(body_ok["status"], "OK");
    assert_eq!(body_ok["candidatePlan"]["status"], "OK");
    assert_eq!(body_ok["blastRadius"]["passed"], true);
    assert_eq!(
        body_ok["simulation"]["preStateDigests"],
        body_ok["simulation"]["postStateDigests"]
    );
    assert_eq!(body_ok["sideEffectsObserved"], 0);
    assert_eq!(body_ok["s3PutCount"], 0);
    assert_eq!(body_ok["s3DeleteCount"], 0);
    assert_eq!(body_ok["authorityMutationCount"], 0);
    assert_eq!(body_ok["actionJournalMutationCount"], 0);
    assert_eq!(body_ok["policyMutationCount"], 0);
    assert_eq!(body_ok["targetMutationCount"], 0);

    let obj = body_ok.as_object().unwrap();
    let mut preimage = Map::new();
    for (k, v) in obj {
        if k != "whatIfDigest" {
            preimage.insert(k.clone(), v.clone());
        }
    }
    assert_eq!(
        body_ok["whatIfDigest"].as_str().unwrap(),
        digest_compact(&Value::Object(preimage))
    );
}
