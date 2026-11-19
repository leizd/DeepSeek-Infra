//! Integration tests for Slice 7c closed-loop execution & wave orchestration routes:
//! - `POST /api/workspace/resilience/execute`
//! - `POST /api/workspace/resilience/waves/admit`
//! - `POST /api/workspace/resilience/waves/run`

use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::backup_commit::{GENESIS_COMMIT_HASH, commit_hash};
use deepseek_gateway::create_production_app;
use deepseek_policy::backup_projection::sha256_bytes;
use deepseek_policy::python_json::dumps_compact;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "resilience-wave-test-token";
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

fn build_app(
    root: &Path,
    runtime_mode: Option<&str>,
) -> (tempfile::TempDir, EnvGuard, axum::Router) {
    let static_root = tempfile::tempdir().unwrap();
    let ui = static_root.path().join("ui");
    fs::create_dir_all(&ui).unwrap();
    fs::write(ui.join("index.html"), "<main>ui</main>").unwrap();
    let root_str = root.to_string_lossy().into_owned();
    let env = EnvGuard::set(&[
        ("DEEPSEEK_INFRA_ROOT", Some(&root_str)),
        ("DEEPSEEK_RUNTIME_MODE", runtime_mode),
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
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header(header::HOST, "127.0.0.1")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
    let body = match body_bytes {
        Some(raw) => {
            builder = builder
                .header(header::CONTENT_TYPE, "application/json")
                .header(header::CONTENT_LENGTH, raw.len().to_string());
            Body::from(raw.to_vec())
        }
        None => Body::empty(),
    };
    let response = app
        .clone()
        .oneshot(builder.body(body).unwrap())
        .await
        .unwrap();
    let status = response.status();
    let raw = to_bytes(response.into_body(), usize::MAX).await.unwrap();
    let parsed = serde_json::from_slice::<Value>(&raw).unwrap_or(Value::Null);
    (status, parsed)
}

async fn post_json(app: &axum::Router, uri: &str, payload: &Value) -> (StatusCode, Value) {
    let bytes = serde_json::to_vec(payload).unwrap();
    call_json(app, "POST", uri, Some(&bytes)).await
}

fn seed_journal_schema(conn: &Connection) {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS resilience_actions (
            action_id TEXT PRIMARY KEY,
            plan_id TEXT,
            action_type TEXT NOT NULL,
            created_by TEXT NOT NULL,
            input_risk_digest TEXT NOT NULL,
            plan_digest TEXT NOT NULL,
            state TEXT NOT NULL,
            parameters_json TEXT,
            owner_instance_id TEXT,
            lease_until TEXT,
            claim_token TEXT,
            execution_epoch INTEGER NOT NULL DEFAULT 0,
            effect_class TEXT,
            compensation_state TEXT,
            effect_handle_json TEXT,
            risk_subject_json TEXT,
            expected_effect TEXT,
            severity_before TEXT,
            coordination_plan_id TEXT,
            execution_result_json TEXT,
            verification_result_json TEXT,
            decision_proof_json TEXT,
            risk_before_digest TEXT,
            risk_after_digest TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            preempted_by_action_id TEXT,
            preemption_reason TEXT,
            preempted_at TEXT,
            execution_outcome TEXT
        );",
    )
    .unwrap();
}

/// Seed the control database with a readable authority head.
///
/// `build_fresh_state_bundle_readonly` refuses with
/// `AUTHORITY_OBSERVATION_UNAVAILABLE` unless `control_db_is_healthy` passes and
/// `control_authority_head` yields a plain sha256, so any test that reaches
/// `/waves/admit` needs this. The connection is returned so it stays open: the
/// reader only skips the `immutable=1` URI while the `-wal`/`-shm` side files
/// exist, and `PRAGMA quick_check` needs the ordinary connection.
fn seed_control_authority(root: &Path) -> Connection {
    fs::create_dir_all(root.join(".backup-control")).unwrap();
    let ctrl = Connection::open(root.join(".backup-control").join("control.sqlite3")).unwrap();
    ctrl.execute_batch("PRAGMA journal_mode=WAL;").unwrap();
    ctrl.execute_batch(
        "CREATE TABLE IF NOT EXISTS control_boot_state (id INTEGER PRIMARY KEY, boot_epoch INTEGER NOT NULL, recovery_state TEXT NOT NULL, reason TEXT);
         INSERT OR REPLACE INTO control_boot_state VALUES (1, 1, 'active', NULL);
         CREATE TABLE IF NOT EXISTS control_authority_head (id INTEGER PRIMARY KEY, authority_generation INTEGER NOT NULL, authority_digest TEXT);
         INSERT OR REPLACE INTO control_authority_head VALUES (1, 1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
         CREATE TABLE IF NOT EXISTS control_targets (target_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);",
    )
    .unwrap();
    ctrl
}

fn seed_repair_source_fixture(root: &Path) -> (String, String, String, Connection) {
    let t1_dir = root.join("ext-t1");
    let t2_dir = root.join("ext-t2");
    fs::create_dir_all(&t1_dir).unwrap();
    fs::create_dir_all(&t2_dir).unwrap();

    let payload = b"encrypted-repair-segment-payload-v1";
    let obj_digest = sha256_bytes(payload);
    let obj_rel = format!(
        "objects/{}/{}/{}.age",
        &obj_digest[..2],
        &obj_digest[2..4],
        obj_digest
    );
    let obj_path = t1_dir.join(&obj_rel);
    fs::create_dir_all(obj_path.parent().unwrap()).unwrap();
    fs::write(&obj_path, payload).unwrap();

    let receipt = json!({
        "schemaVersion": 4,
        "policyId": "pol_rep",
        "backupId": "bk_1",
        "targetId": "t1",
        "objectSetDigest": obj_digest,
        "objects": [{"digest": obj_digest, "size": payload.len()}],
    });
    let receipt_bytes = serde_json::to_vec(&receipt).unwrap();
    let r_dir = t1_dir.join("receipts");
    fs::create_dir_all(&r_dir).unwrap();
    fs::write(r_dir.join("bk_1.json"), &receipt_bytes).unwrap();
    let r_digest = sha256_bytes(&receipt_bytes);

    let mut commit = json!({
        "schemaVersion": 4,
        "targetGeneration": 1,
        "previousCommitHash": GENESIS_COMMIT_HASH,
        "policyId": "pol_rep",
        "backupId": "bk_1",
        "objectSetDigest": obj_digest,
        "receiptDigest": r_digest,
        "committedAt": "2026-11-18T01:00:00Z",
    });
    let c_hash = commit_hash(&commit);
    commit["commitHash"] = Value::String(c_hash);
    let c_dir = t1_dir.join("commits").join("pol_rep");
    fs::create_dir_all(&c_dir).unwrap();
    fs::write(c_dir.join("bk_1.json"), dumps_compact(&commit)).unwrap();

    let t1_payload = json!({
        "targetId": "t1",
        "kind": "filesystem",
        "path": t1_dir.to_string_lossy().into_owned(),
        "failureDomain": "fd-a",
        "enabled": true,
        "capacity": {"totalBytes": 100_000_000_000_i64, "usedBytes": 1024, "freeBytes": 99_999_998_976_i64}
    });
    let t2_payload = json!({
        "targetId": "t2",
        "kind": "filesystem",
        "path": t2_dir.to_string_lossy().into_owned(),
        "failureDomain": "fd-b",
        "enabled": true,
        "capacity": {"totalBytes": 100_000_000_000_i64, "usedBytes": 1024, "freeBytes": 99_999_998_976_i64}
    });
    fs::create_dir_all(root.join(".backup-targets")).unwrap();
    fs::write(
        root.join(".backup-targets").join("t1.json"),
        serde_json::to_vec(&t1_payload).unwrap(),
    )
    .unwrap();
    fs::write(
        root.join(".backup-targets").join("t2.json"),
        serde_json::to_vec(&t2_payload).unwrap(),
    )
    .unwrap();

    // A filesystem target is only usable when its directory carries the
    // ownership marker `verify_registered_target_ready` requires. The registry
    // entries above declare no `targetNonce`, so an empty marker nonce matches
    // the same empty expectation.
    for (target_id, dir) in [("t1", &t1_dir), ("t2", &t2_dir)] {
        fs::write(
            dir.join(".deepseek-infra-backup-target.json"),
            serde_json::to_vec(&json!({"targetId": target_id, "targetNonce": ""})).unwrap(),
        )
        .unwrap();
    }

    fs::create_dir_all(root.join(".backup-policies")).unwrap();
    fs::write(
        root.join(".backup-policies").join("pol_rep.json"),
        serde_json::to_vec(&json!({
            "policyId": "pol_rep",
            "name": "Replicated Policy",
            "targetId": "t1",
            "replication": {
                "enabled": true,
                "minCommittedCopies": 2,
                "minFailureDomains": 2,
                "targetIds": ["t1", "t2"],
            }
        }))
        .unwrap(),
    )
    .unwrap();

    let ctrl = seed_control_authority(root);
    ctrl.execute(
        "INSERT INTO control_targets VALUES ('t1', ?)",
        [t1_payload.to_string()],
    )
    .unwrap();
    ctrl.execute(
        "INSERT INTO control_targets VALUES ('t2', ?)",
        [t2_payload.to_string()],
    )
    .unwrap();

    // Readers open the control database read-only. `open_readonly_sqlite` picks
    // `immutable=1` only when no `-wal`/`-shm` side files exist; with them it
    // opens a normal read-only connection. `PRAGMA quick_check`, which
    // `control_db_is_healthy` relies on before it reads the authority head,
    // needs the ordinary path, so keep the write log in place rather than
    // folding it away.
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
            't1', 'pol_rep', 'bk_1', '2026-11-18T01:00:00Z',
            'full', NULL, NULL, 1, ?, ?, 1, '2026-11-18T01:00:00Z',
            'object-set-v1', '{}'
         )",
        rusqlite::params![payload.len() as i64, payload.len() as i64],
    )
    .unwrap();
    // `pol_rep` requires `minCommittedCopies: 2` across two failure domains, so
    // the ledger must already hold both copies: the verifier reads the ledger
    // and never writes a copy itself (`resilience_outcome_verifier` and
    // `verify_action_outcome` both only filter the rows they are given). t1 is
    // the primary; t2 is the copy the repair is asked to restore.
    for (target, role, fd) in [("t1", "primary", "fd-a"), ("t2", "replica", "fd-b")] {
        dr.execute(
            "INSERT INTO recovery_point_copies (
                target_id, policy_id, backup_id, committed_at, recoverable,
                state, object_set_digest, role, metadata_json
             ) VALUES (
                ?, 'pol_rep', 'bk_1', '2026-11-18T01:00:00Z',
                1, 'healthy', ?, ?, ?
             )",
            rusqlite::params![
                target,
                &obj_digest,
                role,
                format!("{{\"failureDomain\":\"{fd}\"}}")
            ],
        )
        .unwrap();
    }

    (
        obj_digest,
        obj_rel,
        t2_dir.to_string_lossy().into_owned(),
        ctrl,
    )
}

fn seed_wave_schedule_and_reservations(
    root: &Path,
    schedule_id: &str,
    risk_digest: &str,
    authority_digest: &str,
    wave0_action: &Value,
    wave1_action: Option<&Value>,
) {
    fs::create_dir_all(root.join(".resilience-waves")).unwrap();
    let wconn = Connection::open(root.join(".resilience-waves").join("waves.sqlite3")).unwrap();
    wconn
        .execute_batch(
            "CREATE TABLE IF NOT EXISTS resilience_wave_schedules (
                schedule_id TEXT PRIMARY KEY,
                schedule_digest TEXT,
                status TEXT NOT NULL,
                risk_digest TEXT NOT NULL,
                authority_head_digest TEXT,
                schedule_json TEXT NOT NULL,
                stale_reason TEXT,
                execution_epoch INTEGER NOT NULL DEFAULT 0,
                owner_instance_id TEXT,
                lease_until TEXT,
                runner_token TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS resilience_wave_states (
                schedule_id TEXT NOT NULL,
                wave_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                revalidation_json TEXT,
                admitted_at TEXT,
                verified_at TEXT,
                execution_epoch INTEGER NOT NULL DEFAULT 0,
                schedule_execution_epoch INTEGER NOT NULL DEFAULT 0,
                owner_instance_id TEXT,
                lease_until TEXT,
                runner_token TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (schedule_id, wave_index)
            );
            CREATE TABLE IF NOT EXISTS resilience_wave_actions (
                schedule_id TEXT NOT NULL,
                wave_index INTEGER NOT NULL,
                action_id TEXT NOT NULL,
                action_json TEXT NOT NULL,
                status TEXT NOT NULL,
                outcome TEXT,
                execution_epoch INTEGER NOT NULL DEFAULT 0,
                schedule_execution_epoch INTEGER NOT NULL DEFAULT 0,
                wave_execution_epoch INTEGER NOT NULL DEFAULT 0,
                owner_instance_id TEXT,
                lease_until TEXT,
                runner_token TEXT,
                journal_execution_epoch INTEGER NOT NULL DEFAULT 0,
                effect_handle_json TEXT,
                terminal_json TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (schedule_id, action_id)
            );",
        )
        .unwrap();

    let now_iso = "2026-11-18T12:00:00Z";
    let mut waves_arr = vec![json!({"waveIndex": 0, "actions": [wave0_action]})];
    if let Some(w1) = wave1_action {
        waves_arr.push(json!({"waveIndex": 1, "actions": [w1]}));
    }
    let sched_payload = json!({
        "scheduleId": schedule_id,
        "status": "PLANNED",
        "riskDigest": risk_digest,
        "authorityHeadDigest": authority_digest,
        "waves": waves_arr,
    });
    wconn
        .execute(
            "INSERT INTO resilience_wave_schedules (
                schedule_id, schedule_digest, status, risk_digest, authority_head_digest,
                schedule_json, stale_reason, execution_epoch, created_at, updated_at
             ) VALUES (?, NULL, 'PLANNED', ?, ?, ?, NULL, 0, ?, ?)",
            rusqlite::params![
                schedule_id,
                risk_digest,
                authority_digest,
                dumps_compact(&sched_payload),
                now_iso,
                now_iso,
            ],
        )
        .unwrap();

    let mut action_pairs: Vec<(i64, &Value)> = vec![(0, wave0_action)];
    if let Some(w1) = wave1_action {
        action_pairs.push((1, w1));
    }
    fs::create_dir_all(root.join(".resilience-scheduler")).unwrap();
    let sconn =
        Connection::open(root.join(".resilience-scheduler").join("service.sqlite3")).unwrap();
    sconn
        .execute_batch(
            "CREATE TABLE IF NOT EXISTS resilience_service_reservations (
                action_id TEXT PRIMARY KEY,
                schedule_id TEXT NOT NULL,
                wave_index INTEGER NOT NULL,
                execution_epoch INTEGER NOT NULL,
                policy_id TEXT NOT NULL,
                estimated_bytes INTEGER NOT NULL,
                reserved_units REAL NOT NULL,
                status TEXT NOT NULL,
                actual_bytes INTEGER,
                actual_duration_ms REAL,
                actual_traffic_class TEXT,
                outcome TEXT,
                release_reason TEXT,
                effect_handle_json TEXT,
                transfer_id TEXT,
                telemetry_digest TEXT,
                settlement_started_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );",
        )
        .unwrap();

    for (idx, act) in action_pairs {
        let aid = act["actionId"].as_str().unwrap();
        wconn
            .execute(
                "INSERT INTO resilience_wave_states (
                    schedule_id, wave_index, status, updated_at
                 ) VALUES (?, ?, 'PENDING', ?)",
                rusqlite::params![schedule_id, idx, now_iso],
            )
            .unwrap();
        wconn
            .execute(
                "INSERT INTO resilience_wave_actions (
                    schedule_id, wave_index, action_id, action_json, status, updated_at
                 ) VALUES (?, ?, ?, ?, 'PENDING', ?)",
                rusqlite::params![schedule_id, idx, aid, dumps_compact(act), now_iso],
            )
            .unwrap();
        sconn
            .execute(
                "INSERT OR REPLACE INTO resilience_service_reservations (
                    action_id, schedule_id, wave_index, execution_epoch, policy_id,
                    estimated_bytes, reserved_units, status, created_at, updated_at
                 ) VALUES (?, ?, ?, 1, 'default', 1024, 1.0, 'RESERVED', ?, ?)",
                rusqlite::params![aid, schedule_id, idx, now_iso, now_iso],
            )
            .unwrap();
    }
}

#[tokio::test]
async fn slice7c_write_gate_validation_and_blocked_safety_floor() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();

    // Default mode rejects all 3 endpoints with 409 without creating dot-dirs
    {
        let (_static_root, _env, app) = build_app(root.path(), None);
        for uri in [
            "/api/workspace/resilience/execute",
            "/api/workspace/resilience/waves/admit",
            "/api/workspace/resilience/waves/run",
        ] {
            // `route_support::unallowed_api_method(method, Method::GET)` answers
            // 404 for GET and 405 for anything else, mirroring the original
            // router where only POST was registered on these paths.
            let (st_get, _) = call_json(&app, "GET", uri, None).await;
            assert_eq!(st_get, StatusCode::NOT_FOUND, "uri={uri}");

            let (st, body) = post_json(&app, uri, &json!({})).await;
            assert_eq!(st, StatusCode::CONFLICT, "{uri}: {body}");
            assert_eq!(body["code"], "NATIVE_RESILIENCE_STORE_WRITE_NOT_OWNED");
        }
        assert!(!root.path().join(".resilience-journal").exists());
        assert!(!root.path().join(".resilience-waves").exists());
        assert!(!root.path().join(".resilience-scheduler").exists());
    }

    // python_disabled validation and NEVER_AUTONOMOUS safety floor
    {
        let (_static_root, _env, app) = build_app(root.path(), Some("python_disabled"));

        let (st_missing_aid, body_missing_aid) =
            post_json(&app, "/api/workspace/resilience/execute", &json!({})).await;
        assert_eq!(
            st_missing_aid,
            StatusCode::BAD_REQUEST,
            "{body_missing_aid}"
        );

        let (st_raw, body_raw) = post_json(
            &app,
            "/api/workspace/resilience/execute",
            &json!({"type": "START_DR_DRILL"}),
        )
        .await;
        assert_eq!(st_raw, StatusCode::BAD_REQUEST, "{body_raw}");
        assert!(
            body_raw["error"]
                .as_str()
                .unwrap_or_default()
                .contains("Raw action execution not permitted")
        );

        let (st_not_found, _) = post_json(
            &app,
            "/api/workspace/resilience/execute",
            &json!({"actionId": "act_does_not_exist"}),
        )
        .await;
        assert_eq!(st_not_found, StatusCode::NOT_FOUND);

        let (st_bad_admit, _) =
            post_json(&app, "/api/workspace/resilience/waves/admit", &json!({})).await;
        assert_eq!(st_bad_admit, StatusCode::BAD_REQUEST);

        let (st_bad_idx, _) = post_json(
            &app,
            "/api/workspace/resilience/waves/admit",
            &json!({"scheduleId": "sched_1", "waveIndex": "not-an-int"}),
        )
        .await;
        assert_eq!(st_bad_idx, StatusCode::BAD_REQUEST);

        let (st_admit_missing, _) = post_json(
            &app,
            "/api/workspace/resilience/waves/admit",
            &json!({"scheduleId": "sched_missing"}),
        )
        .await;
        assert_eq!(st_admit_missing, StatusCode::INTERNAL_SERVER_ERROR);

        let (st_bad_run, _) =
            post_json(&app, "/api/workspace/resilience/waves/run", &json!({})).await;
        assert_eq!(st_bad_run, StatusCode::BAD_REQUEST);

        let (st_run_missing, _) = post_json(
            &app,
            "/api/workspace/resilience/waves/run",
            &json!({"scheduleId": "sched_missing"}),
        )
        .await;
        assert_eq!(st_run_missing, StatusCode::INTERNAL_SERVER_ERROR);

        // Seed unapproved PRIMARY_PROMOTION -> BLOCKED (403)
        fs::create_dir_all(root.path().join(".resilience-journal")).unwrap();
        let jconn = Connection::open(
            root.path()
                .join(".resilience-journal")
                .join("journal.sqlite3"),
        )
        .unwrap();
        seed_journal_schema(&jconn);
        let dig = "0".repeat(64);
        jconn
            .execute(
                "INSERT INTO resilience_actions (
                    action_id, action_type, created_by, input_risk_digest, plan_digest,
                    state, parameters_json, created_at, updated_at
                 ) VALUES ('act_prom_1', 'PRIMARY_PROMOTION', 'test', ?, ?, 'PENDING', '{}', '2026-11-18T12:00:00Z', '2026-11-18T12:00:00Z')",
                [&dig, &dig],
            )
            .unwrap();
        let (st_blocked, body_blocked) = post_json(
            &app,
            "/api/workspace/resilience/execute",
            &json!({"actionId": "act_prom_1"}),
        )
        .await;
        assert_eq!(st_blocked, StatusCode::FORBIDDEN, "{body_blocked}");
        let recorded_state: String = jconn
            .query_row(
                "SELECT state FROM resilience_actions WHERE action_id = 'act_prom_1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(recorded_state, "BLOCKED");
    }
}

#[tokio::test]
async fn slice7c_execute_dr_drill_repair_and_reconciling_effect_unknown() {
    let _lock = EnvLock::acquire();

    // 1. START_DR_DRILL closed-loop execution + Gate C EFFECT_UNKNOWN
    {
        let root = tempfile::tempdir().unwrap();
        let (_static_root, _env, app) = build_app(root.path(), Some("python_disabled"));

        let (st_plan, plan) = post_json(
            &app,
            "/api/workspace/resilience/plan",
            &json!({"probe": false}),
        )
        .await;
        assert_eq!(st_plan, StatusCode::OK, "{plan}");
        let actions = plan["actions"].as_array().unwrap();
        let drill_act = actions
            .iter()
            .find(|a| a["type"] == "START_DR_DRILL")
            .expect("START_DR_DRILL materialized");
        let drill_aid = drill_act["actionId"].as_str().unwrap();

        let (st_exec, executed) = post_json(
            &app,
            "/api/workspace/resilience/execute",
            &json!({"actionId": drill_aid}),
        )
        .await;
        assert_eq!(st_exec, StatusCode::OK, "{executed}");
        assert_eq!(executed["state"], "SUCCEEDED");
        assert_eq!(executed["decisionProof"]["executionVerified"], true);
        assert_eq!(executed["decisionProof"]["effectObserved"], true);
        assert_eq!(
            executed["decisionProof"]["executedActionType"],
            "START_DR_DRILL"
        );
        let drill_id = executed["executionResult"]["drillId"].as_str().unwrap();
        assert!(
            root.path()
                .join(".restore-staging")
                .join(drill_id)
                .join("drill-result.json")
                .is_file()
        );

        // Gate C: RECONCILING action with missing effect_handle_json fails closed to EFFECT_UNKNOWN
        let jconn = Connection::open(
            root.path()
                .join(".resilience-journal")
                .join("journal.sqlite3"),
        )
        .unwrap();
        let dig = "1".repeat(64);
        jconn
            .execute(
                "INSERT INTO resilience_actions (
                    action_id, action_type, created_by, input_risk_digest, plan_digest,
                    state, parameters_json, owner_instance_id, lease_until, claim_token,
                    execution_epoch, effect_class, created_at, updated_at
                 ) VALUES (
                    'act_reconcile_missing', 'CREATE_REPAIR_JOB', 'crashed-worker', ?, ?,
                    'RECONCILING', '{\"policyId\":\"pol_rep\",\"backupId\":\"bk_1\",\"target\":\"t2\"}',
                    'dead-node', '2000-01-01T00:00:00Z', 'tok', 1, 'REMOTE_MUTATION',
                    '2026-11-18T10:00:00Z', '2026-11-18T10:00:00Z'
                 )",
                [&dig, &dig],
            )
            .unwrap();
        let (st_unk, body_unk) = post_json(
            &app,
            "/api/workspace/resilience/execute",
            &json!({"actionId": "act_reconcile_missing"}),
        )
        .await;
        assert_eq!(st_unk, StatusCode::INTERNAL_SERVER_ERROR, "{body_unk}");
        let (unk_state, comp_state): (String, Option<String>) = jconn
            .query_row(
                "SELECT state, compensation_state FROM resilience_actions WHERE action_id = 'act_reconcile_missing'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(unk_state, "EFFECT_UNKNOWN");
        assert_eq!(comp_state.as_deref(), Some("REMOTE_EFFECT_UNCERTAIN"));
    }

    // 2. End-to-end CREATE_REPAIR_JOB ciphertext streaming + Receipt/Commit v4 + DR ledger + scoped risk clear
    {
        let root = tempfile::tempdir().unwrap();
        let (_obj_dig, obj_rel, t2_dir_str, _ctrl) = seed_repair_source_fixture(root.path());
        let (_static_root, _env, app) = build_app(root.path(), Some("python_disabled"));

        let (st_plan, plan) = post_json(
            &app,
            "/api/workspace/resilience/plan",
            &json!({"probe": false}),
        )
        .await;
        assert_eq!(st_plan, StatusCode::OK, "{plan}");
        let repair_act = plan["actions"]
            .as_array()
            .unwrap()
            .iter()
            .find(|a| a["type"] == "CREATE_REPAIR_JOB")
            .expect("CREATE_REPAIR_JOB materialized");
        let repair_aid = repair_act["actionId"].as_str().unwrap();

        let (st_exec, executed) = post_json(
            &app,
            "/api/workspace/resilience/execute",
            &json!({"actionId": repair_aid}),
        )
        .await;
        assert_eq!(st_exec, StatusCode::OK, "{executed}");
        assert_eq!(executed["state"], "SUCCEEDED");
        assert_eq!(executed["decisionProof"]["executionVerified"], true);
        assert_eq!(executed["decisionProof"]["effectObserved"], true);
        let t2_root = Path::new(&t2_dir_str);
        assert!(t2_root.join(&obj_rel).is_file());
        assert!(t2_root.join("receipts").join("bk_1.json").is_file());
        assert!(
            t2_root
                .join("commits")
                .join("pol_rep")
                .join("bk_1.json")
                .is_file()
        );
        assert!(t2_root.join("control").join("head.json").is_file());
    }
}

#[tokio::test]
async fn slice7c_waves_admit_stale_replan_predecessor_guard_and_full_run_settlement() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app(root.path(), Some("python_disabled"));
    let _ctrl = seed_control_authority(root.path());

    // Materialize a START_DR_DRILL action via POST /plan
    let (st_plan, plan) = post_json(
        &app,
        "/api/workspace/resilience/plan",
        &json!({"probe": false}),
    )
    .await;
    assert_eq!(st_plan, StatusCode::OK, "{plan}");
    let drill_act = plan["actions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|a| a["type"] == "START_DR_DRILL")
        .unwrap()
        .clone();

    // 1. Stale schedule transitions to PAUSED_REPLAN, releases reservations, and returns fresh bindings
    seed_wave_schedule_and_reservations(
        root.path(),
        "sched_stale",
        &"f".repeat(64),
        &"e".repeat(64),
        &drill_act,
        None,
    );
    let (st_stale, stale_res) = post_json(
        &app,
        "/api/workspace/resilience/waves/admit",
        &json!({"scheduleId": "sched_stale", "waveIndex": 0}),
    )
    .await;
    assert_eq!(st_stale, StatusCode::OK, "{stale_res}");
    assert_eq!(stale_res["admitted"], false);
    assert_eq!(stale_res["status"], "PAUSED_REPLAN", "{stale_res}");
    assert_eq!(stale_res["reason"], "STALE");

    let sconn = Connection::open(
        root.path()
            .join(".resilience-scheduler")
            .join("service.sqlite3"),
    )
    .unwrap();
    let (rel_st, rel_reason): (String, Option<String>) = sconn
        .query_row(
            "SELECT status, release_reason FROM resilience_service_reservations WHERE action_id = ?",
            [drill_act["actionId"].as_str().unwrap()],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(rel_st, "RELEASED");
    assert_eq!(rel_reason.as_deref(), Some("STALE"));

    let fresh_risk = stale_res["revalidation"]["riskDigest"]
        .as_str()
        .unwrap()
        .to_string();
    let fresh_auth = stale_res["revalidation"]["authorityHeadDigest"]
        .as_str()
        .unwrap()
        .to_string();

    // 2. Fresh 2-wave schedule: predecessor guard rejects wave 1; wave 0 admits + runs to settlement
    let dummy_w1 = json!({
        "actionId": "act_wave1_noop",
        "type": "START_DR_DRILL",
        "parameters": {},
    });
    seed_wave_schedule_and_reservations(
        root.path(),
        "sched_live",
        &fresh_risk,
        &fresh_auth,
        &drill_act,
        Some(&dummy_w1),
    );

    let (st_w1, w1_res) = post_json(
        &app,
        "/api/workspace/resilience/waves/admit",
        &json!({"scheduleId": "sched_live", "waveIndex": 1}),
    )
    .await;
    assert_eq!(st_w1, StatusCode::OK, "{w1_res}");
    assert_eq!(w1_res["admitted"], false);
    assert_eq!(w1_res["reason"], "PREDECESSOR_WAVE_NOT_VERIFIED");

    let (st_w0, w0_res) = post_json(
        &app,
        "/api/workspace/resilience/waves/admit",
        &json!({"scheduleId": "sched_live", "waveIndex": 0}),
    )
    .await;
    assert_eq!(st_w0, StatusCode::OK, "{w0_res}");
    assert_eq!(w0_res["admitted"], true);
    assert_eq!(w0_res["status"], "CLAIMING");

    let (st_run, run_res) = post_json(
        &app,
        "/api/workspace/resilience/waves/run",
        &json!({"scheduleId": "sched_live"}),
    )
    .await;
    assert_eq!(st_run, StatusCode::OK, "{run_res}");
    assert_eq!(run_res["ran"], true);
    assert_eq!(run_res["status"], "COMPLETED");
    assert_eq!(run_res["waveIndex"], 0);

    let res_st: String = sconn
        .query_row(
            "SELECT status FROM resilience_service_reservations WHERE action_id = ?",
            [drill_act["actionId"].as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(res_st, "CONSUMED");
}
