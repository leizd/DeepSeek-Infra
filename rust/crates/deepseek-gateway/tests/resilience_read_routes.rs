//! Read-only `/api/workspace/resilience/{journal,capacity-forecast,waves}` integration tests.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use rusqlite::Connection;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "resilience-read-test-token";
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

async fn call_get(app: &axum::Router, method: &str, uri: &str) -> (StatusCode, Value) {
    let req = Request::builder()
        .method(method)
        .uri(uri)
        .header("host", "127.0.0.1")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
        .body(Body::empty())
        .unwrap();
    let response = app.clone().oneshot(req).await.unwrap();
    let status = response.status();
    let bytes = to_bytes(response.into_body(), 65_536).await.unwrap();
    let body = if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    (status, body)
}

#[tokio::test]
async fn resilience_reads_return_empty_defaults_without_creating_sqlite_stores() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static, _env, app) = build_app(root.path());

    let (js, jb) = call_get(&app, "GET", "/api/workspace/resilience/journal").await;
    assert_eq!(js, StatusCode::OK);
    assert_eq!(jb, json!([]));

    let (cs, cb) = call_get(&app, "GET", "/api/workspace/resilience/capacity-forecast").await;
    assert_eq!(cs, StatusCode::OK);
    assert_eq!(cb["horizonDays"], 90);
    assert_eq!(cb["targets"], json!([]));

    let (ws, wb) = call_get(&app, "GET", "/api/workspace/resilience/waves").await;
    assert_eq!(ws, StatusCode::OK);
    assert_eq!(wb, json!({"schedule": null, "waves": [], "actions": []}));

    for dot_dir in [
        ".resilience-journal",
        ".resilience-capacity",
        ".resilience-scheduler",
        ".resilience-waves",
    ] {
        assert!(!root.path().join(dot_dir).exists(), "{dot_dir} created");
    }

    let (post_j, _) = call_get(&app, "POST", "/api/workspace/resilience/journal").await;
    assert_eq!(post_j, StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn resilience_reads_query_sqlite_tables_and_filters() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();

    std::fs::create_dir_all(root.path().join(".resilience-journal")).unwrap();
    let jconn = Connection::open(root.path().join(".resilience-journal/journal.sqlite3")).unwrap();
    jconn
        .execute_batch(
            "CREATE TABLE resilience_actions (
                 action_id TEXT PRIMARY KEY, plan_id TEXT, action_type TEXT NOT NULL,
                 created_by TEXT NOT NULL, input_risk_digest TEXT, plan_digest TEXT,
                 state TEXT NOT NULL, parameters_json TEXT, owner_instance_id TEXT,
                 lease_until TEXT, claim_token TEXT, execution_epoch INTEGER NOT NULL DEFAULT 0,
                 effect_class TEXT, compensation_state TEXT, effect_handle_json TEXT,
                 risk_subject_json TEXT, expected_effect TEXT, severity_before TEXT,
                 coordination_plan_id TEXT, execution_result_json TEXT, verification_result_json TEXT,
                 decision_proof_json TEXT, risk_before_digest TEXT, risk_after_digest TEXT,
                 error_message TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
             );",
        )
        .unwrap();
    jconn
        .execute(
            "INSERT INTO resilience_actions (action_id, action_type, created_by, state, parameters_json, created_at, updated_at)
             VALUES ('act-1', 'REBALANCE', 'planner', 'PENDING', '{\"x\":1}', '2026-11-17T00:00:00Z', '2026-11-17T00:00:00Z')",
            [],
        )
        .unwrap();

    std::fs::create_dir_all(root.path().join(".resilience-capacity")).unwrap();
    let cconn =
        Connection::open(root.path().join(".resilience-capacity/capacity.sqlite3")).unwrap();
    cconn
        .execute_batch(
            "CREATE TABLE resilience_capacity_observations (
                 observation_key TEXT PRIMARY KEY, target_id TEXT NOT NULL,
                 target_incarnation TEXT NOT NULL, capacity_revision TEXT NOT NULL,
                 observed_at TEXT NOT NULL
             );
             CREATE TABLE resilience_forecast_records (
                 forecast_id TEXT PRIMARY KEY, target_id TEXT NOT NULL,
                 target_incarnation TEXT NOT NULL, capacity_revision TEXT NOT NULL,
                 horizon_days INTEGER NOT NULL, forecasted_at TEXT NOT NULL,
                 evaluation_due_at TEXT NOT NULL, p50_free_bytes INTEGER NOT NULL,
                 p90_free_bytes INTEGER NOT NULL, forecast_digest TEXT NOT NULL,
                 observation_set_digest TEXT NOT NULL, forecast_json TEXT NOT NULL,
                 status TEXT NOT NULL, actual_observation_key TEXT, backtest_key TEXT,
                 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
             );",
        )
        .unwrap();
    cconn
        .execute(
            "INSERT INTO resilience_capacity_observations VALUES ('obs-1', 't-a', 'inc-1', 'rev-1', '2026-11-17T01:00:00Z')",
            [],
        )
        .unwrap();
    cconn
        .execute(
            "INSERT INTO resilience_forecast_records VALUES ('fc-1', 't-a', 'inc-1', 'rev-1', 30, '2026-11-17T01:00:00Z', '2026-12-17T01:00:00Z', 500, 400, 'fd', 'od', '{\"ok\":true}', 'ACTIVE', '', '', '2026-11-17T01:00:00Z', '2026-11-17T01:00:00Z')",
            [],
        )
        .unwrap();

    std::fs::create_dir_all(root.path().join(".resilience-scheduler")).unwrap();
    let sconn =
        Connection::open(root.path().join(".resilience-scheduler/service.sqlite3")).unwrap();
    sconn
        .execute_batch(
            "CREATE TABLE resilience_scheduler_runs (schedule_id TEXT PRIMARY KEY, schedule_json TEXT NOT NULL, scheduled_at TEXT NOT NULL);
             INSERT INTO resilience_scheduler_runs VALUES ('sched-1', '{\"scheduleId\":\"sched-1\"}', '2026-11-17T01:00:00Z');",
        )
        .unwrap();

    std::fs::create_dir_all(root.path().join(".resilience-waves")).unwrap();
    let wconn = Connection::open(root.path().join(".resilience-waves/waves.sqlite3")).unwrap();
    wconn
        .execute_batch(
            "CREATE TABLE resilience_wave_schedules (schedule_id TEXT PRIMARY KEY, schedule_digest TEXT NOT NULL, status TEXT NOT NULL, risk_digest TEXT NOT NULL, authority_head_digest TEXT, schedule_json TEXT NOT NULL, stale_reason TEXT, execution_epoch INTEGER NOT NULL, owner_instance_id TEXT, lease_until TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
             CREATE TABLE resilience_wave_states (schedule_id TEXT NOT NULL, wave_index INTEGER NOT NULL, status TEXT NOT NULL, revalidation_json TEXT, admitted_at TEXT, verified_at TEXT, execution_epoch INTEGER NOT NULL, owner_instance_id TEXT, lease_until TEXT, updated_at TEXT NOT NULL);
             CREATE TABLE resilience_wave_actions (schedule_id TEXT NOT NULL, wave_index INTEGER NOT NULL, action_id TEXT NOT NULL, action_json TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT, execution_epoch INTEGER NOT NULL, schedule_execution_epoch INTEGER NOT NULL, wave_execution_epoch INTEGER NOT NULL, owner_instance_id TEXT, lease_until TEXT, journal_execution_epoch INTEGER NOT NULL, effect_handle_json TEXT, terminal_json TEXT, updated_at TEXT NOT NULL);
             INSERT INTO resilience_wave_schedules VALUES ('sched-1', 'sd', 'PLANNED', 'rd', NULL, '{\"x\":1}', NULL, 1, NULL, NULL, '2026-11-17T01:00:00Z', '2026-11-17T01:00:00Z');
             INSERT INTO resilience_wave_states VALUES ('sched-1', 0, 'PENDING', NULL, NULL, NULL, 1, NULL, NULL, '2026-11-17T01:00:00Z');
             INSERT INTO resilience_wave_actions VALUES ('sched-1', 0, 'act-1', '{\"type\":\"REBALANCE\"}', 'PENDING', NULL, 1, 1, 1, NULL, NULL, 0, NULL, NULL, '2026-11-17T01:00:00Z');",
        )
        .unwrap();

    let (_static, _env, app) = build_app(root.path());

    let (js, jb) = call_get(
        &app,
        "GET",
        "/api/workspace/resilience/journal?type=rebalance",
    )
    .await;
    assert_eq!(js, StatusCode::OK);
    assert_eq!(jb.as_array().unwrap().len(), 1);
    assert_eq!(jb[0]["actionId"], "act-1");

    let (cs, cb) = call_get(
        &app,
        "GET",
        "/api/workspace/resilience/capacity-forecast?horizonDays=30",
    )
    .await;
    assert_eq!(cs, StatusCode::OK);
    assert_eq!(cb["targets"].as_array().unwrap().len(), 1);
    assert_eq!(cb["targets"][0]["forecastId"], "fc-1");

    let (ws, wb) = call_get(&app, "GET", "/api/workspace/resilience/waves").await;
    assert_eq!(ws, StatusCode::OK);
    assert_eq!(wb["schedule"]["scheduleId"], "sched-1");
    assert_eq!(wb["waves"].as_array().unwrap().len(), 1);
    assert_eq!(wb["actions"].as_array().unwrap().len(), 1);
}

#[tokio::test]
async fn resilience_readiness_aggregates_risks_slo_burn_and_evidence() {
    let _lock = EnvLock::acquire();
    let root = tempfile::tempdir().unwrap();
    let (_static, _env, app) = build_app(root.path());

    // Empty workspace -> DEGRADED without touching disk.
    let (st_empty, empty_body) = call_get(&app, "GET", "/api/workspace/resilience/readiness").await;
    assert_eq!(st_empty, StatusCode::OK);
    assert_eq!(empty_body["fleetReadiness"], "DEGRADED");
    assert_eq!(empty_body["riskDebt"]["openCount"], 0);
    assert_eq!(empty_body["burnRate"]["status"], "OK");
    assert!(!root.path().join(".resilience-slo").exists());
    assert!(!root.path().join(".resilience-risk").exists());

    let (st_head, _) = call_get(&app, "HEAD", "/api/workspace/resilience/readiness").await;
    assert_eq!(st_head, StatusCode::OK);
    let (st_post, _) = call_get(&app, "POST", "/api/workspace/resilience/readiness").await;
    assert_eq!(st_post, StatusCode::METHOD_NOT_ALLOWED);

    // Seed risk observation + SLO sample + burn rate + schedule snapshot.
    std::fs::create_dir_all(root.path().join(".resilience-risk")).unwrap();
    let rconn = Connection::open(root.path().join(".resilience-risk/risk.sqlite3")).unwrap();
    rconn
        .execute_batch(
            "CREATE TABLE resilience_risk_observations (
                risk_subject_digest TEXT PRIMARY KEY, subject_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, open_since_at TEXT, last_seen_at TEXT NOT NULL,
                observation_count INTEGER NOT NULL, current_severity TEXT NOT NULL,
                peak_severity TEXT NOT NULL, status TEXT NOT NULL, last_cleared_at TEXT,
                reopen_count INTEGER NOT NULL DEFAULT 0, policy_id TEXT, backup_id TEXT,
                target_id TEXT, failure_domain TEXT, last_snapshot_digest TEXT,
                last_snapshot_observation_key TEXT, closure_reason TEXT, updated_at TEXT NOT NULL
            );
            INSERT INTO resilience_risk_observations VALUES (
                'dig-1', '{}', '2026-11-17T00:00:00Z', '2026-11-17T00:00:00Z', '2026-11-17T01:00:00Z',
                2, 'critical', 'critical', 'OPEN', NULL, 0, NULL, NULL, NULL, NULL, NULL, NULL, NULL, '2026-11-17T01:00:00Z'
            );",
        )
        .unwrap();

    std::fs::create_dir_all(root.path().join(".resilience-slo")).unwrap();
    let sconn = Connection::open(root.path().join(".resilience-slo/slo.sqlite3")).unwrap();
    sconn
        .execute_batch(
            "CREATE TABLE resilience_slo_samples (
                sample_id INTEGER PRIMARY KEY AUTOINCREMENT, sample_key TEXT NOT NULL UNIQUE,
                metric_name TEXT NOT NULL, value REAL NOT NULL, policy_id TEXT, action_id TEXT,
                risk_subject_digest TEXT, outcome TEXT, metadata_json TEXT NOT NULL, observed_at TEXT NOT NULL
            );
            CREATE TABLE resilience_slo_burn_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT, observation_key TEXT NOT NULL UNIQUE,
                indicator TEXT NOT NULL, bad_units REAL NOT NULL, total_units REAL NOT NULL,
                metadata_json TEXT NOT NULL, started_at TEXT NOT NULL, observed_at TEXT NOT NULL
            );
            INSERT INTO resilience_slo_samples (sample_key, metric_name, value, metadata_json, observed_at)
            VALUES ('ev-1', 'evidence_freshness_seconds', 10.0, '{\"proofSha256\":\"abc\",\"scenario\":\"dr-test\"}', '2026-11-17T01:00:00Z');",
        )
        .unwrap();

    let (st_crit, crit_body) = call_get(&app, "GET", "/api/workspace/resilience/readiness").await;
    assert_eq!(st_crit, StatusCode::OK);
    assert_eq!(crit_body["fleetReadiness"], "CRITICAL");
    assert_eq!(crit_body["riskDebt"]["openCount"], 1);
    assert_eq!(crit_body["riskDebt"]["critical"], 1);
    assert_eq!(crit_body["evidence"]["proofSha256"], "abc");
    assert_eq!(crit_body["evidence"]["scenario"], "dr-test");
}
