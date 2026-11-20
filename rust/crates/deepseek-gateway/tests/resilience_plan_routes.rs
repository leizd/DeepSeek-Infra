//! Integration tests for Slice 7b resilience snapshot, assess, plan, and coordination routes:
//! - `GET /api/workspace/resilience/snapshot`
//! - `POST /api/workspace/resilience/assess`
//! - `POST /api/workspace/resilience/plan`
//! - `GET|POST /api/workspace/resilience/coordination-plan`

use std::fs;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use deepseek_policy::backup_projection::sha256_bytes;
use deepseek_policy::python_json::{dumps_compact, value_str};
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "resilience-plan-test-token";
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

fn compute_expected_coord_digest(plan: &Value) -> String {
    let mut objectives: Vec<String> = plan["objectives"]
        .as_array()
        .unwrap()
        .iter()
        .map(value_str)
        .collect();
    objectives.sort();
    let mut acts: Vec<(String, String, String, String, String, Value)> = plan["actions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|a| {
            let aid = a.get("actionId").map(value_str).unwrap_or_default();
            let atype = a.get("type").map(value_str).unwrap_or_default();
            let pid = a
                .get("policyId")
                .filter(|v| !v.is_null())
                .map(value_str)
                .unwrap_or_default();
            let bid = a
                .get("backupId")
                .filter(|v| !v.is_null())
                .map(value_str)
                .unwrap_or_default();
            let src = a
                .get("source")
                .filter(|v| !v.is_null())
                .map(value_str)
                .unwrap_or_default();
            let dst = a
                .get("destination")
                .filter(|v| !v.is_null())
                .map(value_str)
                .unwrap_or_default();
            let req = a["requiresApproval"].as_bool().unwrap_or(false);
            let item = json!({
                "actionId": aid,
                "type": atype,
                "policyId": pid,
                "backupId": bid,
                "source": src,
                "destination": dst,
                "requiresApproval": req,
            });
            (atype, pid, bid, src, dst, item)
        })
        .collect();
    acts.sort_by(|a, b| (&a.0, &a.1, &a.2, &a.3, &a.4).cmp(&(&b.0, &b.1, &b.2, &b.3, &b.4)));
    let sorted_actions: Vec<Value> = acts.into_iter().map(|entry| entry.5).collect();
    let sort_pairs = |key: &str| -> Vec<Vec<String>> {
        let mut pairs: Vec<Vec<String>> = plan[key]
            .as_array()
            .unwrap()
            .iter()
            .map(|p| {
                let mut v = vec![value_str(&p[0]), value_str(&p[1])];
                v.sort();
                v
            })
            .collect();
        pairs.sort();
        pairs
    };
    digest_compact(&json!({
        "coordinationPlanVersion": plan["coordinationPlanVersion"],
        "riskDigest": plan["riskDigest"],
        "objectives": objectives,
        "actions": sorted_actions,
        "dependencies": sort_pairs("dependencies"),
        "conflicts": sort_pairs("conflicts"),
    }))
}

fn seed_replica_deficit_workspace(root: &Path, t1_dir: &Path, t2_dir: &Path) {
    fs::create_dir_all(t1_dir).unwrap();
    fs::create_dir_all(t2_dir).unwrap();
    for (tid, dir) in [("t1", t1_dir), ("t2", t2_dir)] {
        fs::write(
            dir.join(".deepseek-infra-backup-target.json"),
            serde_json::to_vec(&json!({
                "schemaVersion": 3,
                "targetId": tid,
                "targetNonce": format!("nonce-{tid}"),
                "targetGeneration": 1,
            }))
            .unwrap(),
        )
        .unwrap();
    }

    let t1_payload = json!({
        "targetId": "t1",
        "label": "Target 1",
        "kind": "filesystem",
        "path": t1_dir.to_string_lossy(),
        "status": "active",
        "failureDomain": "fd-a",
    });
    let t2_payload = json!({
        "targetId": "t2",
        "label": "Target 2",
        "kind": "filesystem",
        "path": t2_dir.to_string_lossy(),
        "status": "active",
        "failureDomain": "fd-b",
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

    fs::create_dir_all(root.join(".backup-control")).unwrap();
    let ctrl = Connection::open(root.join(".backup-control").join("control.sqlite3")).unwrap();
    ctrl.execute_batch(
        "CREATE TABLE control_boot_state (id INTEGER PRIMARY KEY, boot_epoch INTEGER NOT NULL, recovery_state TEXT NOT NULL, reason TEXT);
         INSERT INTO control_boot_state VALUES (1, 1, 'active', NULL);
         CREATE TABLE control_authority_head (id INTEGER PRIMARY KEY, authority_generation INTEGER NOT NULL, authority_digest TEXT);
         INSERT INTO control_authority_head VALUES (1, 1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa');
         CREATE TABLE control_targets (target_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);",
    )
    .unwrap();
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
         );
         INSERT INTO recovery_points VALUES (
            't1', 'pol_rep', 'bk_1', '2026-11-18T01:00:00Z',
            'full', NULL, NULL, 1, 128, 128, 1, '2026-11-18T01:00:00Z',
            'object-set-v1', '{}'
         );
         INSERT INTO recovery_point_copies (
            target_id, policy_id, backup_id, committed_at, recoverable,
            state, object_set_digest, role, metadata_json
         ) VALUES (
            't1', 'pol_rep', 'bk_1', '2026-11-18T01:00:00Z',
            1, 'healthy', NULL, 'primary', '{}'
         );",
    )
    .unwrap();
}

#[tokio::test]
async fn resilience_plan_snapshot_and_coordination_get_are_readonly_without_creating_dirs() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app(root.path(), None);

    let (st_snap, snap) = call_json(&app, "GET", "/api/workspace/resilience/snapshot", None).await;
    assert_eq!(st_snap, StatusCode::OK, "body: {snap}");
    assert_eq!(snap["score"], 75);
    assert_eq!(snap["grade"], "C");
    assert_eq!(snap["overallRisk"], "critical");
    assert_eq!(snap["criticalRisks"], 1);
    assert_eq!(snap["plannedActions"], 0);
    assert_eq!(snap["runningActions"], 0);
    assert_eq!(snap["factorBreakdown"]["drDrill"]["score"], 0);
    assert_eq!(snap["factorBreakdown"]["replicaHealth"]["score"], 25);
    assert_eq!(snap["factorBreakdown"]["capacity"]["score"], 20);
    assert_eq!(snap["factorBreakdown"]["restorePerformance"]["score"], 15);
    assert_eq!(snap["factorBreakdown"]["authorityIntegrity"]["score"], 15);
    assert_eq!(snap["riskDigest"].as_str().unwrap_or_default().len(), 64);

    let (st_coord, coord) = call_json(
        &app,
        "GET",
        "/api/workspace/resilience/coordination-plan",
        None,
    )
    .await;
    assert_eq!(st_coord, StatusCode::OK, "body: {coord}");
    assert_eq!(coord["coordinationPlanVersion"], 1);
    assert!(
        coord["coordinationPlanId"]
            .as_str()
            .unwrap_or_default()
            .starts_with("coord_")
    );
    assert_eq!(coord["status"], "PROPOSED");
    assert_eq!(coord["budget"]["maxConcurrentActions"], 3);
    assert_eq!(coord["blastRadiusSimulation"]["passed"], true);
    assert_eq!(
        coord["planDigest"].as_str().unwrap_or_default(),
        compute_expected_coord_digest(&coord)
    );

    // Method dispatch guarantees
    assert_eq!(
        call_json(
            &app,
            "POST",
            "/api/workspace/resilience/snapshot",
            Some(b"{}")
        )
        .await
        .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        call_json(&app, "PUT", "/api/workspace/resilience/snapshot", None)
            .await
            .0,
        StatusCode::METHOD_NOT_ALLOWED
    );
    assert_eq!(
        call_json(&app, "GET", "/api/workspace/resilience/assess", None)
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        call_json(&app, "GET", "/api/workspace/resilience/plan", None)
            .await
            .0,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        call_json(
            &app,
            "DELETE",
            "/api/workspace/resilience/coordination-plan",
            None
        )
        .await
        .0,
        StatusCode::METHOD_NOT_ALLOWED
    );

    for dotdir in [
        ".resilience-risk",
        ".resilience-slo",
        ".resilience-journal",
        ".resilience-policy",
        ".backup-control",
        ".backup-dr",
    ] {
        assert!(
            !root.path().join(dotdir).exists(),
            "{dotdir} must not be created on GET"
        );
    }
}

#[tokio::test]
async fn resilience_plan_writes_refuse_without_python_disabled_and_create_no_dirs() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static_root, _env, app) = build_app(root.path(), Some("python_authoritative"));

    let (st_bad_json, _) = call_json(
        &app,
        "POST",
        "/api/workspace/resilience/assess",
        Some(b"{not-json"),
    )
    .await;
    assert_eq!(st_bad_json, StatusCode::BAD_REQUEST);

    for (uri, payload) in [
        ("/api/workspace/resilience/assess", None),
        (
            "/api/workspace/resilience/assess",
            Some(&b"{\"probe\":false}"[..]),
        ),
        (
            "/api/workspace/resilience/plan",
            Some(&b"{\"probe\":false}"[..]),
        ),
        (
            "/api/workspace/resilience/plan",
            Some(&b"{\"probe\":false,\"materialize\":false}"[..]),
        ),
        (
            "/api/workspace/resilience/coordination-plan",
            Some(&b"{\"probe\":false}"[..]),
        ),
    ] {
        let (st, val) = call_json(&app, "POST", uri, payload).await;
        assert_eq!(st, StatusCode::CONFLICT, "expected 409 for {uri}: {val}");
        assert_eq!(val["code"], "NATIVE_RESILIENCE_STORE_WRITE_NOT_OWNED");
    }

    for dotdir in [".resilience-risk", ".resilience-slo", ".resilience-journal"] {
        assert!(
            !root.path().join(dotdir).exists(),
            "{dotdir} must not be created when write is refused"
        );
    }
}

#[tokio::test]
async fn resilience_plan_writes_succeed_and_persist_under_python_disabled() {
    let _lock = EnvLock::acquire();
    let base = tempfile::tempdir().unwrap();
    let workspace = base.path().join("workspace");
    let t1 = base.path().join("targets").join("t1");
    let t2 = base.path().join("targets").join("t2");
    fs::create_dir_all(&workspace).unwrap();
    seed_replica_deficit_workspace(&workspace, &t1, &t2);

    let (_static_root, _env, app) = build_app(&workspace, Some("python_disabled"));

    // 1. POST /assess writes risk observation + SLO ledger and enriches snapshot
    let (st_assess, assess) = call_json(
        &app,
        "POST",
        "/api/workspace/resilience/assess",
        Some(b"{\"probe\":false}"),
    )
    .await;
    assert_eq!(st_assess, StatusCode::OK, "body: {assess}");
    assert_eq!(assess["riskSnapshot"]["overallRisk"], "critical");
    assert!(
        assess["resilienceScore"]["score"]
            .as_i64()
            .is_some_and(|s| s < 100)
    );
    let risks = assess["riskSnapshot"]["risks"].as_array().unwrap();
    let rep_risk = risks
        .iter()
        .find(|r| r["type"] == "REPLICA_LAG")
        .expect("REPLICA_LAG risk present");
    assert_eq!(rep_risk["severity"], "degraded");
    assert_eq!(rep_risk["riskObservation"]["status"], "OPEN");
    assert_eq!(rep_risk["riskObservation"]["observationCount"], 1);
    assert!(workspace.join(".resilience-risk/risk.sqlite3").is_file());
    // `assess` writes the risk observation only. The SLO ledger is written when
    // an action settles: `resilience_action_journal` records REPAIR_TIME_MS /
    // REBALANCE_TIME_MS and the failed-remediation burn on execution, and
    // `resilience_slo_ledger` has no caller on the assess path.
    // Asserting the database here pinned a file the oracle never creates.
    assert!(!workspace.join(".resilience-slo/slo.sqlite3").is_file());

    // 2. POST /plan with materialize=false proposes repair + DR drill without journal write
    let (st_prop, prop) = call_json(
        &app,
        "POST",
        "/api/workspace/resilience/plan",
        Some(b"{\"probe\":false,\"materialize\":false}"),
    )
    .await;
    assert_eq!(st_prop, StatusCode::OK, "body: {prop}");
    assert_eq!(prop["status"], "PROPOSED");
    let prop_actions = prop["actions"].as_array().unwrap();
    let repair_act = prop_actions
        .iter()
        .find(|a| a["type"] == "CREATE_REPAIR_JOB")
        .expect("repair action proposed");
    assert_eq!(repair_act["destination"], "t2");
    assert_eq!(repair_act["requiresApproval"], false);
    assert_eq!(repair_act["riskLifecycleStatus"], "OPEN");
    // The observation key is `sha256({"generatedAt", "riskDigest"})` and
    // `generatedAt` is second-resolution on both sides, so an assess and a plan
    // that land inside the same second share a key. `observe_risk_snapshot`
    // returns the stored row unchanged in that case, which is why the count is
    // 1 here; a repeat only increments once the snapshot key changes.
    assert!(repair_act["riskObservationCount"].as_i64().unwrap_or(0) >= 1);
    assert!(
        !workspace
            .join(".resilience-journal/journal.sqlite3")
            .exists()
    );

    // 3. POST /plan with default materialize=true persists plan + actions in journal
    let (st_mat, mat) = call_json(
        &app,
        "POST",
        "/api/workspace/resilience/plan",
        Some(b"{\"probe\":false}"),
    )
    .await;
    assert_eq!(st_mat, StatusCode::OK, "body: {mat}");
    assert_eq!(mat["status"], "MATERIALIZED");
    let mat_actions = mat["actions"].as_array().unwrap();
    assert!(mat_actions.len() >= 2);
    assert!(
        mat_actions
            .iter()
            .any(|a| a["type"] == "CREATE_REPAIR_JOB" && a["state"] == "PENDING")
    );
    assert!(
        workspace
            .join(".resilience-journal/journal.sqlite3")
            .is_file()
    );

    let j_conn = Connection::open(workspace.join(".resilience-journal/journal.sqlite3")).unwrap();
    let plan_count: i64 = j_conn
        .query_row("SELECT COUNT(*) FROM resilience_plans", [], |r| r.get(0))
        .unwrap();
    let act_count: i64 = j_conn
        .query_row("SELECT COUNT(*) FROM resilience_actions", [], |r| r.get(0))
        .unwrap();
    assert_eq!(plan_count, 1);
    assert_eq!(act_count, mat_actions.len() as i64);

    // 4. Subsequent GET /snapshot reflects materialized plannedActions & criticalRisks
    let (st_snap, snap) = call_json(&app, "GET", "/api/workspace/resilience/snapshot", None).await;
    assert_eq!(st_snap, StatusCode::OK);
    assert!(snap["criticalRisks"].as_i64().unwrap_or(0) >= 1);
    assert_eq!(snap["plannedActions"], mat_actions.len() as i64);

    // 5. POST /coordination-plan produces deterministic digest and valid DAG
    let (st_coord, coord) = call_json(
        &app,
        "POST",
        "/api/workspace/resilience/coordination-plan",
        Some(b"{\"probe\":false}"),
    )
    .await;
    assert_eq!(st_coord, StatusCode::OK, "body: {coord}");
    assert_eq!(coord["status"], "PROPOSED");
    assert!(
        coord["objectives"]
            .as_array()
            .unwrap()
            .iter()
            .any(|o| o == "restore-replica-durability")
    );
    assert_eq!(coord["blastRadiusSimulation"]["passed"], true);
    assert_eq!(
        coord["planDigest"].as_str().unwrap_or_default(),
        compute_expected_coord_digest(&coord)
    );
}
