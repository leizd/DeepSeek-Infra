//! `GET /api/automation` and `POST /api/automation` integration tests.

use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::{Body, to_bytes};
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "automation-root-test-token";
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

fn build_app(mode: &str) -> (tempfile::TempDir, tempfile::TempDir, EnvGuard, axum::Router) {
    let root = tempfile::tempdir().unwrap();
    let static_root = tempfile::tempdir().unwrap();
    let ui = static_root.path().join("ui");
    std::fs::create_dir_all(&ui).unwrap();
    std::fs::write(ui.join("index.html"), "<main>ui</main>").unwrap();
    let env = EnvGuard::set(&[
        (
            "DEEPSEEK_INFRA_ROOT",
            root.path().to_string_lossy().into_owned(),
        ),
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("AUTH_DISABLED", String::new()),
        ("AUTH_ALLOWED_HOSTS", String::new()),
        ("HOST", String::new()),
        ("DEEPSEEK_RUNTIME_MODE", mode.to_string()),
    ]);
    let app = create_production_app(static_root.path()).unwrap();
    (root, static_root, env, app)
}

async fn call_api(
    app: &axum::Router,
    method: &str,
    uri: &str,
    body: Option<Value>,
) -> (StatusCode, Value) {
    let raw = match body {
        Some(value) => serde_json::to_vec(&value).unwrap(),
        None => Vec::new(),
    };
    let mut builder = Request::builder()
        .method(method)
        .uri(uri)
        .header("host", "127.0.0.1")
        .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
    if !raw.is_empty() {
        builder = builder
            .header(header::CONTENT_TYPE, "application/json")
            .header(header::CONTENT_LENGTH, raw.len().to_string());
    }
    let response = app
        .clone()
        .oneshot(builder.body(Body::from(raw)).unwrap())
        .await
        .unwrap();
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
async fn automation_root_gates_mutations_in_default_mode_and_supports_reads() {
    let _lock = EnvLock::acquire();
    let (root, _static, _env, app) = build_app("python_authoritative");

    let (get_s, get_b) = call_api(&app, "GET", "/api/automation", None).await;
    assert_eq!(get_s, StatusCode::OK);
    assert_eq!(get_b, json!({"ok": true, "automations": []}));

    let (tpl_s, tpl_b) = call_api(
        &app,
        "POST",
        "/api/automation",
        Some(json!({"action": "templates"})),
    )
    .await;
    assert_eq!(tpl_s, StatusCode::OK);
    assert!(tpl_b["templates"].as_array().unwrap().len() >= 6);

    let (create_s, create_b) = call_api(
        &app,
        "POST",
        "/api/automation",
        Some(json!({"action": "create", "automation": {"name": "deny"}})),
    )
    .await;
    assert_eq!(create_s, StatusCode::CONFLICT);
    assert_eq!(create_b["code"], "NATIVE_AUTOMATION_WRITE_NOT_OWNED");
    assert!(!root.path().join(".automation").exists());

    let (bad_s, bad_b) = call_api(
        &app,
        "POST",
        "/api/automation",
        Some(json!({"action": "invalid_action"})),
    )
    .await;
    assert_eq!(bad_s, StatusCode::BAD_REQUEST);
    assert_eq!(bad_b["code"], "invalid_payload");

    let (put_s, _) = call_api(&app, "PUT", "/api/automation", None).await;
    assert_eq!(put_s, StatusCode::METHOD_NOT_ALLOWED);
}

#[tokio::test]
async fn automation_root_lifecycle_simulate_run_rerun_and_run_due_under_python_disabled() {
    let _lock = EnvLock::acquire();
    let (_root, _static, _env, app) = build_app("python_disabled");

    let (c_s, c_b) = call_api(
        &app,
        "POST",
        "/api/automation",
        Some(json!({
            "action": "create",
            "automation": {
                "name": "Event Digest",
                "trigger": {"type": "event", "event": "media.ready"},
                "condition": {"type": "always"},
                "action": {"type": "save_item", "title": "digest"},
                "policy": {"maxRunsPerDay": 5}
            }
        })),
    )
    .await;
    assert_eq!(c_s, StatusCode::OK);
    let aid = c_b["automation"]["automationId"]
        .as_str()
        .unwrap()
        .to_string();

    let (sim_s, sim_b) = call_api(
        &app,
        "POST",
        "/api/automation",
        Some(json!({
            "action": "simulate",
            "automationId": aid,
            "event": {"type": "media.ready"}
        })),
    )
    .await;
    assert_eq!(sim_s, StatusCode::OK);
    assert_eq!(sim_b["wouldRun"], true);

    let (dis_s, dis_b) = call_api(
        &app,
        "POST",
        "/api/automation",
        Some(json!({"action": "disable", "automationId": aid})),
    )
    .await;
    assert_eq!(dis_s, StatusCode::OK);
    assert_eq!(dis_b["automation"]["enabled"], false);

    let (filt_s, filt_b) = call_api(&app, "GET", "/api/automation?includeDisabled=0", None).await;
    assert_eq!(filt_s, StatusCode::OK);
    assert_eq!(filt_b["automations"].as_array().unwrap().len(), 0);

    let (en_s, en_b) = call_api(
        &app,
        "POST",
        "/api/automation",
        Some(json!({"action": "enable", "automationId": aid})),
    )
    .await;
    assert_eq!(en_s, StatusCode::OK);
    assert_eq!(en_b["automation"]["enabled"], true);

    let (due_s, due_b) = call_api(
        &app,
        "POST",
        "/api/automation",
        Some(json!({"action": "run_due", "event": {"type": "media.ready"}})),
    )
    .await;
    assert_eq!(due_s, StatusCode::OK);
    assert_eq!(due_b["dueCount"], 1);
    let run_id = due_b["runs"][0]["runId"].as_str().unwrap().to_string();

    let (runs_s, runs_b) = call_api(
        &app,
        "POST",
        "/api/automation",
        Some(json!({"action": "list_runs", "automationId": aid, "limit": 5})),
    )
    .await;
    assert_eq!(runs_s, StatusCode::OK);
    assert_eq!(runs_b["runs"].as_array().unwrap().len(), 1);

    let (rerun_s, rerun_b) = call_api(
        &app,
        "POST",
        "/api/automation",
        Some(json!({"action": "rerun", "runId": run_id})),
    )
    .await;
    assert_eq!(rerun_s, StatusCode::OK);
    assert_ne!(rerun_b["run"]["runId"], run_id);

    let (del_s, del_b) = call_api(
        &app,
        "POST",
        "/api/automation",
        Some(json!({"action": "delete", "automationId": aid})),
    )
    .await;
    assert_eq!(del_s, StatusCode::OK);
    assert_eq!(del_b["deleted"], 1);
}
