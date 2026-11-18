//! Integration tests for read-only resilience forecast and optimizer routes:
//! - `GET|HEAD /api/workspace/resilience/forecast`
//! - `GET|HEAD /api/workspace/resilience/optimizer`

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "resilience-advisory-test-token";
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

async fn call_raw(app: &axum::Router, method: &str, uri: &str) -> (StatusCode, Vec<u8>) {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header(header::HOST, "127.0.0.1:8080")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap()
        .to_vec();
    (status, bytes)
}

#[tokio::test]
async fn forecast_returns_unconstrained_local_and_constrained_quota_target() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let targets_dir = root.path().join(".backup-targets");
    std::fs::create_dir_all(&targets_dir).unwrap();
    std::fs::write(
        targets_dir.join("target_fast.json"),
        serde_json::to_vec(&json!({
            "targetId": "target_fast",
            "kind": "mock",
            "quotaBytes": 1000,
        }))
        .unwrap(),
    )
    .unwrap();
    std::fs::create_dir_all(root.path().join(".backup-dr")).unwrap();
    let dr_conn =
        Connection::open(root.path().join(".backup-dr").join("evidence.sqlite3")).unwrap();
    dr_conn
        .execute_batch(
            "CREATE TABLE logical_recovery_copies (
                policy_id TEXT, backup_id TEXT, target_id TEXT,
                recoverable INTEGER, state TEXT, metadata_json TEXT
             );
             INSERT INTO logical_recovery_copies VALUES
                ('pol_1', 'b1', 'target_fast', 1, 'healthy', '{\"ciphertextBytes\":920}');",
        )
        .unwrap();

    let (_static_root, _env, app) = build_app(root.path());

    let (status, bytes) = call_raw(&app, "GET", "/api/workspace/resilience/forecast").await;
    assert_eq!(status, StatusCode::OK);
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["overallStatus"], "critical");
    let targets = body["targets"].as_array().unwrap();
    assert_eq!(targets.len(), 2);
    assert_eq!(targets[0]["targetId"], "managed-local");
    assert_eq!(targets[0]["status"], "unconstrained");
    assert_eq!(targets[1]["targetId"], "target_fast");
    assert_eq!(targets[1]["status"], "critical");
    assert_eq!(targets[1]["currentUsedBytes"], 920);
    assert_eq!(
        targets[1]["recommendations"],
        json!(["CREATE_REBALANCE_JOB", "EXPAND_TARGET_STORAGE"])
    );

    let (head_status, head_bytes) =
        call_raw(&app, "HEAD", "/api/workspace/resilience/forecast").await;
    assert_eq!(head_status, StatusCode::OK);
    assert!(head_bytes.is_empty());

    let (post_status, _) = call_raw(&app, "POST", "/api/workspace/resilience/forecast").await;
    assert_eq!(post_status, StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn optimizer_computes_percentiles_and_preferred_target_advisories() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(root.path().join(".backup-dr")).unwrap();
    let dr_conn =
        Connection::open(root.path().join(".backup-dr").join("evidence.sqlite3")).unwrap();
    dr_conn
        .execute_batch(
            "CREATE TABLE drill_evidence (
                drill_id TEXT, target_id TEXT, policy_id TEXT, backup_id TEXT,
                observed_at TEXT, result TEXT, duration_ms INTEGER, details_json TEXT
             );
             INSERT INTO drill_evidence VALUES
                ('d1', 'target_fast', 'pol_a', 'b1', '2026-11-17T00:00:00Z', 'success', 1000, '{}'),
                ('d2', 'target_fast', 'pol_a', 'b2', '2026-11-17T01:00:00Z', 'success', 2000, '{}'),
                ('d3', 'target_slow', 'pol_slow', 'b3', '2026-11-17T02:00:00Z', 'success', 9000, '{}'),
                ('d4', 'target_slow', 'pol_slow', 'b4', '2026-11-17T03:00:00Z', 'success', 10000, '{}');",
        )
        .unwrap();

    let pol_dir = root.path().join(".backup-policies");
    std::fs::create_dir_all(&pol_dir).unwrap();
    std::fs::write(
        pol_dir.join("pol_slow.json"),
        serde_json::to_vec(&json!({
            "policyId": "pol_slow",
            "targetId": "target_slow",
        }))
        .unwrap(),
    )
    .unwrap();

    let (_static_root, _env, app) = build_app(root.path());

    let (status, bytes) = call_raw(&app, "GET", "/api/workspace/resilience/optimizer").await;
    assert_eq!(status, StatusCode::OK);
    let body: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(body["optimizerVersion"], 1);
    assert_eq!(body["targetPerformance"]["target_fast"]["sampleCount"], 2);
    assert_eq!(
        body["targetPerformance"]["target_fast"]["p90LatencySeconds"],
        2.0
    );
    assert_eq!(
        body["targetPerformance"]["target_slow"]["p90LatencySeconds"],
        10.0
    );
    let recs = body["recommendations"].as_array().unwrap();
    assert_eq!(recs.len(), 1);
    assert_eq!(recs[0]["type"], "PREFERRED_RESTORE_TARGET_ADVISORY");
    assert_eq!(recs[0]["policyId"], "pol_slow");
    assert_eq!(recs[0]["currentPrimaryTarget"], "target_slow");
    assert_eq!(recs[0]["recommendedTarget"], "target_fast");

    let (post_status, _) = call_raw(&app, "POST", "/api/workspace/resilience/optimizer").await;
    assert_eq!(post_status, StatusCode::METHOD_NOT_ALLOWED);
}

async fn call_post_json(app: &axum::Router, uri: &str, payload: &Value) -> (StatusCode, Value) {
    let encoded = serde_json::to_vec(payload).unwrap();
    let req = Request::builder()
        .method("POST")
        .uri(uri)
        .header(header::HOST, "127.0.0.1:8080")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .header(header::CONTENT_TYPE, "application/json")
        .header(header::CONTENT_LENGTH, encoded.len().to_string())
        .body(Body::from(encoded))
        .unwrap();
    let resp = app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let bytes = to_bytes(resp.into_body(), usize::MAX).await.unwrap();
    let val = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    (status, val)
}

#[tokio::test]
async fn simulate_and_explain_evaluate_topology_and_action_reasons() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(root.path().join(".backup-dr")).unwrap();
    std::fs::create_dir_all(root.path().join(".backup-control")).unwrap();
    std::fs::create_dir_all(root.path().join(".backup-policies")).unwrap();
    std::fs::create_dir_all(root.path().join(".resilience-journal")).unwrap();

    let dr_conn =
        Connection::open(root.path().join(".backup-dr").join("evidence.sqlite3")).unwrap();
    dr_conn
        .execute_batch(
            "CREATE TABLE recovery_points (
                target_id TEXT, policy_id TEXT, backup_id TEXT, committed_at TEXT
             );
             CREATE TABLE recovery_point_copies (
                target_id TEXT, policy_id TEXT, backup_id TEXT, committed_at TEXT, state TEXT
             );
             INSERT INTO recovery_points VALUES ('target-a', 'pol1', 'bk1', '2026-11-18T00:00:00Z');
             INSERT INTO recovery_point_copies VALUES
                ('target-a', 'pol1', 'bk1', '2026-11-18T00:00:00Z', 'committed'),
                ('target-b', 'pol1', 'bk1', '2026-11-18T00:00:00Z', 'healthy');",
        )
        .unwrap();

    let ctrl_conn =
        Connection::open(root.path().join(".backup-control").join("control.sqlite3")).unwrap();
    ctrl_conn
        .execute_batch(
            "CREATE TABLE control_targets (target_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
             INSERT INTO control_targets VALUES
                ('target-a', '{\"targetId\":\"target-a\",\"kind\":\"filesystem\",\"status\":\"active\"}'),
                ('target-b', '{\"targetId\":\"target-b\",\"kind\":\"filesystem\",\"status\":\"active\"}');
             CREATE TABLE backup_target_capacity (
                observation_id TEXT, target_id TEXT, observed_at TEXT,
                total_bytes INTEGER, used_bytes INTEGER, free_bytes INTEGER, payload_json TEXT
             );
             INSERT INTO backup_target_capacity VALUES
                ('o1', 'target-a', '2026-11-18T00:00:00Z', 1000, 900, 100,
                 '{\"targetId\":\"target-a\",\"totalBytes\":1000,\"usedBytes\":900,\"freeBytes\":100,\"freePercent\":10.0}'),
                ('o2', 'target-b', '2026-11-18T00:00:00Z', 1000, 100, 900,
                 '{\"targetId\":\"target-b\",\"totalBytes\":1000,\"usedBytes\":100,\"freeBytes\":900,\"freePercent\":90.0}');",
        )
        .unwrap();

    std::fs::write(
        root.path().join(".backup-policies").join("pol1.json"),
        serde_json::to_vec(&json!({"policyId": "pol1", "targetId": "target-a"})).unwrap(),
    )
    .unwrap();

    let j_conn = Connection::open(
        root.path()
            .join(".resilience-journal")
            .join("journal.sqlite3"),
    )
    .unwrap();
    j_conn
        .execute_batch(
            "CREATE TABLE resilience_actions (
                action_id TEXT PRIMARY KEY, plan_id TEXT, action_type TEXT, created_by TEXT,
                input_risk_digest TEXT, plan_digest TEXT, state TEXT, parameters_json TEXT,
                owner_instance_id TEXT, lease_until TEXT, claim_token TEXT, execution_epoch INTEGER,
                effect_class TEXT, compensation_state TEXT, effect_handle_json TEXT,
                risk_subject_json TEXT, expected_effect TEXT, severity_before TEXT,
                coordination_plan_id TEXT, execution_result_json TEXT, verification_result_json TEXT,
                decision_proof_json TEXT, risk_before_digest TEXT, risk_after_digest TEXT,
                error_message TEXT, created_at TEXT, updated_at TEXT
             );
             INSERT INTO resilience_actions VALUES (
                'act-1', 'plan-1', 'REBALANCE', 'planner', NULL, NULL, 'PENDING',
                '{\"sourceTargetId\":\"target-a\",\"reason\":\"low free space\"}',
                NULL, NULL, NULL, 1, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                NULL, NULL, NULL, NULL, NULL, NULL,
                '2026-11-18T00:00:00Z', '2026-11-18T00:00:00Z'
             );",
        )
        .unwrap();

    let (_static_root, _env, app) = build_app(root.path());

    let (sim_st, sim_val) = call_post_json(
        &app,
        "/api/workspace/resilience/simulate",
        &json!({"scenario": "AZ_FAILURE", "excludedTargets": ["target-a"], "policyId": "pol1"}),
    )
    .await;
    assert_eq!(sim_st, StatusCode::OK, "simulate failed: {sim_val}");
    assert_eq!(sim_val["survivable"], json!(true));
    assert_eq!(sim_val["lostCopies"], 1);
    assert_eq!(sim_val["survivingCopies"], 1);
    assert_eq!(sim_val["viableTargets"], json!(["target-b"]));
    assert_eq!(sim_val["simulationPassed"], json!(true));

    let (exp_st, exp_val) = call_post_json(
        &app,
        "/api/workspace/resilience/explain",
        &json!({"actionId": "act-1"}),
    )
    .await;
    assert_eq!(exp_st, StatusCode::OK, "explain failed: {exp_val}");
    assert_eq!(exp_val["targetId"], "target-a");
    let reasons = exp_val["reasons"].as_array().unwrap();
    assert!(reasons.contains(&json!("REBALANCE")));
    assert!(reasons.contains(&json!("low free space")));
    assert!(reasons.contains(&json!("capacity watermark exceeded")));
    assert!(reasons.contains(&json!("destination satisfies topology")));

    let (get_s, _) = call_raw(&app, "GET", "/api/workspace/resilience/simulate").await;
    assert_eq!(get_s, StatusCode::NOT_FOUND);
}
