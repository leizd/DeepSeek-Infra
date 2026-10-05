//! `GET`, `PATCH`, and `DELETE /api/automation/{automation_id}` through the
//! production router.
//!
//! GET is `registry.get_automation`. A missing `.automation/automations.json`
//! stays missing and answers 404. PATCH and DELETE write that file only when
//! `DEEPSEEK_RUNTIME_MODE=python_disabled`. Otherwise they answer 409 and leave
//! the store untouched.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "automation-definition-route-token";
const AUTO: &str = "auto_keep";

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
    root: tempfile::TempDir,
    _static_root: tempfile::TempDir,
    _env: EnvGuard,
    app: axum::Router,
}

impl Fixture {
    fn automation_dir(&self) -> PathBuf {
        self.root.path().join(".automation")
    }

    fn store_path(&self) -> PathBuf {
        self.automation_dir().join("automations.json")
    }

    fn write_store(&self, automations: &Value) {
        std::fs::create_dir_all(self.automation_dir()).unwrap();
        std::fs::write(
            self.store_path(),
            serde_json::to_vec(&json!({"automations": automations})).unwrap(),
        )
        .unwrap();
    }

    fn with_mode(mode: &str) -> Self {
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
            ("DEEPSEEK_RUNTIME_MODE", mode.to_string()),
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

    fn new() -> Self {
        Self::with_mode("")
    }

    async fn request(&self, method: &str, uri: &str, auth: bool) -> (StatusCode, Value) {
        let (status, body, _) = self.request_parts(method, uri, auth, None, None).await;
        (status, body)
    }

    async fn request_json(&self, method: &str, uri: &str, payload: &Value) -> (StatusCode, Value) {
        let bytes = serde_json::to_vec(payload).unwrap();
        let length = bytes.len().to_string();
        let (status, body, _) = self
            .request_parts(method, uri, true, Some(bytes), Some(length))
            .await;
        (status, body)
    }

    async fn request_raw(
        &self,
        method: &str,
        uri: &str,
        payload: Vec<u8>,
        content_length: &str,
    ) -> (StatusCode, Value) {
        let (status, body, _) = self
            .request_parts(
                method,
                uri,
                true,
                Some(payload),
                Some(content_length.to_string()),
            )
            .await;
        (status, body)
    }

    async fn request_parts(
        &self,
        method: &str,
        uri: &str,
        auth: bool,
        payload: Option<Vec<u8>>,
        content_length: Option<String>,
    ) -> (StatusCode, Value, axum::http::HeaderMap) {
        let mut request = Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, "127.0.0.1");
        if auth {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        if let Some(length) = &content_length {
            request = request.header(header::CONTENT_LENGTH, length);
        }
        let response = self
            .app
            .clone()
            .oneshot(
                request
                    .body(Body::from(payload.unwrap_or_default()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = response.status();
        let headers = response.headers().clone();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        let body = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        (status, body, headers)
    }
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf()
}

fn oracle(root: &Path, cases: &Value) -> Vec<Value> {
    let script = r#"
import json, sys
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.automation.registry import get_automation
plan = json.loads(sys.stdin.read())
results = []
for case in plan:
    try:
        body = {"ok": True, "automation": get_automation(case["automationId"])}
        results.append({"status": 200, "body": body})
    except AppError as exc:
        results.append({"status": exc.status, "body": {"error": str(exc), "code": exc.code.value}})
    except UnicodeDecodeError:
        results.append({"status": 500, "body": {"error": "Server error", "code": "internal"}})
print(json.dumps(results))
"#;
    let mut child = std::process::Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("python oracle");
    {
        use std::io::Write;
        child
            .stdin
            .as_mut()
            .expect("stdin")
            .write_all(&serde_json::to_vec(cases).unwrap())
            .unwrap();
    }
    let output = child.wait_with_output().expect("oracle wait");
    assert!(
        output.status.success(),
        "oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("oracle json")
}

fn item_uri(automation_id: &str, query: &str) -> String {
    if query.is_empty() {
        format!("/api/automation/{automation_id}")
    } else {
        format!("/api/automation/{automation_id}?{query}")
    }
}

async fn expect_oracle(fixture: &Fixture, automation_id: &str, query: &str, expected: &Value) {
    let (status, body) = fixture
        .request("GET", &item_uri(automation_id, query), true)
        .await;
    let expected_status =
        StatusCode::from_u16(expected["status"].as_u64().unwrap() as u16).unwrap();
    assert_eq!(
        status, expected_status,
        "id={automation_id} body={body} expected={expected}"
    );
    assert_eq!(body, expected["body"], "id={automation_id}");
}

fn rich_automations() -> Value {
    json!([
        {
            "automationId": " auto_keep ",
            "id": "ignored_alias",
            "projectId": " proj_ok ",
            "name": "  Hello   World ",
            "description": " line\r\n two ",
            "enabled": "yes",
            "trigger": {
                "type": " EVENT ",
                "cron": "  0 1 * * *  ",
                "intervalSeconds": 90,
                "event": " Media.Ready ",
                "extra": "dropped"
            },
            "condition": {
                "type": "Project_Changed",
                "sinceLastRun": "on",
                "projectChanged": 0,
                "ignored": true
            },
            "action": {
                "type": "Save_Item",
                "input": {"task": "write"},
                "note": 1
            },
            "output": {
                "artifactType": " .MD ",
                "saveToProject": "false",
                "createArtifact": null
            },
            "policy": {
                "requiresConfirmation": "1",
                "maxRunsPerDay": "12",
                "timeoutSeconds": 10,
                "allowBrowser": "true",
                "browserMode": "DISABLED",
                "allowNetwork": "yes",
                "allowPrivateHosts": false,
                "retry": {"maxAttempts": 9, "backoffSeconds": -4}
            },
            "metadata": {"keep": "yes", "nested": {"a": 1}},
            "createdAt": "stored-created",
            "updatedAt": "stored-updated-ignored",
            "createdAtMs": 1_700_000_000_000i64,
            "updatedAtMs": 1_700_000_005_000i64
        },
        {
            "automationId": "auto_keep",
            "name": "Second",
            "action": {"type": "save_item"},
            "createdAt": "second",
            "createdAtMs": 1_700_000_000_000i64,
            "updatedAtMs": 1_700_000_000_000i64
        },
        {
            "id": "auto_alias",
            "action": {"type": "project_summary"},
            "createdAt": "alias",
            "createdAtMs": 1_700_000_000_000i64,
            "updatedAtMs": 1_700_000_000_000i64
        },
        {
            "automationId": "auto_nullproj",
            "projectId": null,
            "action": {"type": "save_item"},
            "createdAt": "null-project",
            "createdAtMs": 1_700_000_000_000i64,
            "updatedAtMs": 1_700_000_000_000i64
        },
        {
            "automationId": "auto_defaults",
            "action": {"type": "project_summary"},
            "createdAt": "defaults",
            "createdAtMs": 1_700_000_000_000i64,
            "updatedAtMs": 1_700_000_000_000i64
        },
        {
            "automationId": "auto_bad",
            "action": {"type": "save_item"},
            "trigger": {"type": "nope"},
            "createdAtMs": 1_700_000_000_000i64,
            "updatedAtMs": 1_700_000_000_000i64
        },
        {
            "automationId": "auto_bad_input",
            "action": {"type": "save_item", "input": "text"},
            "createdAtMs": 1_700_000_000_000i64,
            "updatedAtMs": 1_700_000_000_000i64
        },
        "not-an-object"
    ])
}

fn numbered(count: usize) -> Vec<Value> {
    (0..count)
        .map(|index| {
            json!({
                "automationId": format!("auto_{index:04}"),
                "action": {"type": "save_item"},
                "createdAt": "kept",
                "createdAtMs": 1_700_000_000_000i64,
                "updatedAtMs": 1_700_000_000_000i64
            })
        })
        .collect()
}

#[tokio::test]
async fn automation_get_reads_the_registry_without_creating_it_or_blocking_writes() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let item = format!("/api/automation/{AUTO}");

    let (status, unauthenticated) = fixture.request("GET", &item, false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
    assert_eq!(unauthenticated["error"]["message"], "Auth required");
    assert!(!fixture.automation_dir().exists());

    let missing = oracle(
        fixture.root.path(),
        &json!([
            {"automationId": AUTO},
            {"automationId": "no"},
            {"automationId": "   "}
        ]),
    );
    expect_oracle(&fixture, AUTO, "", &missing[0]).await;
    expect_oracle(&fixture, "no", "", &missing[1]).await;
    expect_oracle(&fixture, "%20%20%20", "", &missing[2]).await;
    assert!(!fixture.automation_dir().exists());

    let (status, patched_missing) = fixture.request("PATCH", &item, true).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{patched_missing}");
    assert_eq!(patched_missing["error"], "Request body is empty");
    assert_eq!(patched_missing["code"], "invalid_payload");
    let (status, deleted_missing) = fixture.request("DELETE", &item, true).await;
    assert_eq!(status, StatusCode::CONFLICT, "{deleted_missing}");
    assert_eq!(deleted_missing["code"], "NATIVE_AUTOMATION_WRITE_NOT_OWNED");
    assert!(!fixture.automation_dir().exists());

    fixture.write_store(&rich_automations());
    let rich_bytes = std::fs::read(fixture.store_path()).unwrap();
    let rich = oracle(
        fixture.root.path(),
        &json!([
            {"automationId": AUTO},
            {"automationId": " auto_keep "},
            {"automationId": "auto_alias"},
            {"automationId": "auto_nullproj"},
            {"automationId": "auto_defaults"},
            {"automationId": "auto_bad"},
            {"automationId": "auto_bad_input"},
            {"automationId": "auto_missing"}
        ]),
    );
    expect_oracle(&fixture, AUTO, "", &rich[0]).await;
    expect_oracle(&fixture, "%20auto_keep%20", "", &rich[1]).await;
    expect_oracle(
        &fixture,
        AUTO,
        "projectId=other&includeDisabled=0",
        &rich[0],
    )
    .await;
    expect_oracle(&fixture, "auto_alias", "", &rich[2]).await;
    expect_oracle(&fixture, "auto_nullproj", "", &rich[3]).await;
    expect_oracle(&fixture, "auto_defaults", "", &rich[4]).await;
    expect_oracle(&fixture, "auto_bad", "", &rich[5]).await;
    expect_oracle(&fixture, "auto_bad_input", "", &rich[6]).await;
    expect_oracle(&fixture, "auto_missing", "", &rich[7]).await;
    assert_eq!(rich[0]["body"]["automation"]["automationId"], AUTO);
    assert_eq!(rich[0]["body"]["automation"]["name"], "Hello World");
    assert_ne!(rich[0]["body"]["automation"]["name"], "Second");
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), rich_bytes);
    assert!(!fixture.automation_dir().join("history.json").exists());

    let (status, patched) = fixture.request("PATCH", &item, true).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{patched}");
    assert_eq!(patched["error"], "Request body is empty");
    let (status, renamed) = fixture
        .request_json("PATCH", &item, &json!({"name": "Renamed"}))
        .await;
    assert_eq!(status, StatusCode::CONFLICT, "{renamed}");
    assert_eq!(renamed["code"], "NATIVE_AUTOMATION_WRITE_NOT_OWNED");
    assert_eq!(
        renamed["error"],
        "The automation store is still written by the Python runtime, so this gateway refuses to mutate it."
    );
    let (status, deleted) = fixture.request("DELETE", &item, true).await;
    assert_eq!(status, StatusCode::CONFLICT, "{deleted}");
    assert_eq!(deleted["code"], "NATIVE_AUTOMATION_WRITE_NOT_OWNED");
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), rich_bytes);
    let (status, posted) = fixture.request("POST", &item, true).await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{posted}");
    assert_eq!(posted["detail"], "Method Not Allowed");
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), rich_bytes);

    let mut truncated = vec![json!({
        "automationId": "auto_drop",
        "action": {"type": "save_item"},
        "createdAt": "dropped",
        "createdAtMs": 1_700_000_000_000i64,
        "updatedAtMs": 1_700_000_000_000i64
    })];
    truncated.extend(numbered(500));
    fixture.write_store(&Value::Array(truncated));
    let truncated_bytes = std::fs::read(fixture.store_path()).unwrap();
    let window = oracle(
        fixture.root.path(),
        &json!([
            {"automationId": "auto_drop"},
            {"automationId": "auto_0000"},
            {"automationId": "auto_0499"}
        ]),
    );
    expect_oracle(&fixture, "auto_drop", "", &window[0]).await;
    expect_oracle(&fixture, "auto_0000", "", &window[1]).await;
    expect_oracle(&fixture, "auto_0499", "", &window[2]).await;
    assert_eq!(window[0]["status"], 404);
    assert_eq!(window[1]["status"], 200);
    assert_eq!(window[2]["status"], 200);
    assert_eq!(
        std::fs::read(fixture.store_path()).unwrap(),
        truncated_bytes
    );

    fixture.write_store(&json!([{
        "automationId": "auto_fresh",
        "action": {"type": "save_item"}
    }]));
    let fresh_bytes = std::fs::read(fixture.store_path()).unwrap();
    let fresh_oracle = oracle(
        fixture.root.path(),
        &json!([{"automationId": "auto_fresh"}]),
    );
    let (status, fresh) = fixture
        .request("GET", "/api/automation/auto_fresh", true)
        .await;
    assert_eq!(status, StatusCode::OK, "{fresh}");
    let mut fresh_body = fresh.clone();
    let mut oracle_body = fresh_oracle[0]["body"].clone();
    for body in [&mut fresh_body, &mut oracle_body] {
        let automation = body["automation"].as_object_mut().unwrap();
        for key in ["createdAt", "createdAtMs", "updatedAt", "updatedAtMs"] {
            automation.remove(key);
        }
    }
    assert_eq!(fresh_body, oracle_body);
    let started = fresh["automation"]["createdAtMs"].as_i64().unwrap();
    assert!(started > 1_700_000_000_000, "{fresh}");
    assert!(
        fresh["automation"]["createdAt"]
            .as_str()
            .unwrap()
            .ends_with('Z')
    );
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), fresh_bytes);

    std::fs::write(fixture.store_path(), b"{").unwrap();
    let bad_json = oracle(fixture.root.path(), &json!([{"automationId": AUTO}]));
    expect_oracle(&fixture, AUTO, "", &bad_json[0]).await;
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), b"{");

    std::fs::write(fixture.store_path(), b"[]").unwrap();
    let array_root = oracle(fixture.root.path(), &json!([{"automationId": AUTO}]));
    expect_oracle(&fixture, AUTO, "", &array_root[0]).await;

    std::fs::write(fixture.store_path(), b"{\"automations\": {}}").unwrap();
    let bad_list = oracle(fixture.root.path(), &json!([{"automationId": AUTO}]));
    expect_oracle(&fixture, AUTO, "", &bad_list[0]).await;

    std::fs::write(fixture.store_path(), [0xff, 0xfe, b'{']).unwrap();
    let bad_utf8 = oracle(fixture.root.path(), &json!([{"automationId": AUTO}]));
    expect_oracle(&fixture, AUTO, "", &bad_utf8[0]).await;
    assert_eq!(bad_utf8[0]["status"], 500);
    assert_eq!(
        std::fs::read(fixture.store_path()).unwrap(),
        vec![0xff, 0xfe, b'{']
    );

    std::fs::remove_file(fixture.store_path()).unwrap();
    std::fs::create_dir(fixture.store_path()).unwrap();
    let directory = oracle(fixture.root.path(), &json!([{"automationId": AUTO}]));
    expect_oracle(&fixture, AUTO, "", &directory[0]).await;
    assert_eq!(directory[0]["status"], 404);
    assert!(fixture.store_path().is_dir());

    let (status, templates) = fixture
        .request("GET", "/api/automation/templates", true)
        .await;
    assert_eq!(status, StatusCode::OK, "{templates}");
    assert_eq!(templates["templates"].as_array().unwrap().len(), 6);
    let (status, post_templates) = fixture
        .request("POST", "/api/automation/templates", true)
        .await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{post_templates}");
    let (status, run_once) = fixture
        .request("POST", &format!("/api/automation/{AUTO}/run"), true)
        .await;
    // The store path is a directory, so the registry is empty and the run is
    // `404` before any history write.
    assert_eq!(status, StatusCode::NOT_FOUND, "{run_once}");
    assert_eq!(
        run_once,
        json!({"error": "Automation not found", "code": "not_found"})
    );
    let (status, runs) = fixture
        .request("GET", &format!("/api/automation/{AUTO}/runs"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{runs}");
    assert_eq!(runs, json!({"ok": true, "runs": []}));
    assert!(!fixture.automation_dir().join("history.json").exists());
}

fn mutation_oracle(root: &Path, cases: &Value) -> Vec<Value> {
    let script = r#"
import json, sys
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.automation.registry import delete_automation, get_automation, update_automation
plan = json.loads(sys.stdin.read())
results = []
for case in plan:
    try:
        if case["op"] == "update":
            body = {"ok": True, "automation": update_automation(case["automationId"], case["patch"])}
        elif case["op"] == "delete":
            body = {"ok": True, "deleted": delete_automation(case["automationId"])}
        else:
            body = {"ok": True, "automation": get_automation(case["automationId"])}
        results.append({"status": 200, "body": body})
    except AppError as exc:
        results.append({"status": exc.status, "body": {"error": str(exc), "code": exc.code.value}})
    except UnicodeDecodeError:
        results.append({"status": 500, "body": {"error": "Server error", "code": "internal"}})
print(json.dumps(results))
"#;
    let mut child = std::process::Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env_remove("DEEPSEEK_RUNTIME_MODE")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .expect("python oracle");
    {
        use std::io::Write;
        child
            .stdin
            .as_mut()
            .expect("stdin")
            .write_all(&serde_json::to_vec(cases).unwrap())
            .unwrap();
    }
    let output = child.wait_with_output().expect("oracle wait");
    assert!(
        output.status.success(),
        "oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("oracle json")
}

fn copy_dir(from: &Path, to: &Path) {
    std::fs::create_dir_all(to).unwrap();
    for entry in std::fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let dest = to.join(entry.file_name());
        if entry.path().is_dir() {
            copy_dir(&entry.path(), &dest);
        } else {
            std::fs::copy(entry.path(), &dest).unwrap();
        }
    }
}

fn oracle_workspace(source: &Path) -> tempfile::TempDir {
    let copy = tempfile::tempdir().expect("oracle workspace");
    for name in [".automation", ".projects"] {
        let from = source.join(name);
        if from.exists() {
            copy_dir(&from, &copy.path().join(name));
        }
    }
    copy
}

fn strip_clock(body: &mut Value) {
    let Some(automation) = body.get_mut("automation") else {
        return;
    };
    let Some(fields) = automation.as_object_mut() else {
        return;
    };
    fields.remove("updatedAt");
    fields.remove("updatedAtMs");
}

fn assert_same_automation(native: &Value, expected: &Value) {
    let mut native = native.clone();
    let mut expected = expected.clone();
    strip_clock(&mut native);
    strip_clock(&mut expected);
    assert_eq!(native, expected);
}

fn assert_fresh_clock(automation: &Value) {
    let millis = automation["updatedAtMs"].as_i64().expect("updatedAtMs");
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock")
        .as_millis() as i64;
    assert!(
        (now - millis).abs() < 15_000,
        "updatedAtMs {millis} is not the request clock {now}"
    );
    let rendered =
        deepseek_policy::workspace_schema::timestamp_ms_to_iso(Some(&Value::from(millis)));
    assert_eq!(automation["updatedAt"], rendered);
}

#[tokio::test]
async fn automation_patch_and_delete_match_the_oracle_only_after_python_is_disabled() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::with_mode("python_disabled");
    let item = format!("/api/automation/{AUTO}");

    let (status, unauthenticated) = fixture.request("PATCH", &item, false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
    let (status, unauthenticated_delete) = fixture.request("DELETE", &item, false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated_delete}");
    assert!(!fixture.automation_dir().exists());

    let (status, missing_patch) = fixture
        .request_json("PATCH", &item, &json!({"name": "Renamed"}))
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{missing_patch}");
    assert_eq!(missing_patch["error"], "Automation not found");
    assert_eq!(missing_patch["code"], "not_found");
    let (status, missing_delete) = fixture.request("DELETE", &item, true).await;
    assert_eq!(status, StatusCode::OK, "{missing_delete}");
    assert_eq!(missing_delete, json!({"ok": true, "deleted": 0}));
    let (status, bad_id) = fixture
        .request_json("PATCH", "/api/automation/no", &json!({"name": "Renamed"}))
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{bad_id}");
    assert_eq!(bad_id["error"], "Invalid automation id");
    assert!(!fixture.automation_dir().exists());
    assert!(!fixture.root.path().join(".projects").exists());

    fixture.write_store(&json!([{
        "automationId": AUTO,
        "name": "Keep",
        "action": {"type": "save_item"},
        "createdAt": "stored-created",
        "createdAtMs": 1_700_000_000_000i64,
        "updatedAtMs": 1_700_000_005_000i64
    }]));
    let original = std::fs::read(fixture.store_path()).unwrap();

    let (status, empty_object) = fixture
        .request_raw("PATCH", &item, b"[]".to_vec(), "2")
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{empty_object}");
    assert_eq!(empty_object["error"], "Request body must be a JSON object");
    let (status, bad_json) = fixture
        .request_raw("PATCH", &item, b"{".to_vec(), "1")
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{bad_json}");
    assert_eq!(bad_json["code"], "invalid_payload");
    assert!(
        bad_json["error"]
            .as_str()
            .unwrap()
            .starts_with("Invalid JSON:"),
        "{bad_json}"
    );
    let (status, bad_length) = fixture
        .request_raw("PATCH", &item, b"{}".to_vec(), "nope")
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{bad_length}");
    assert_eq!(bad_length["error"], "Invalid Content-Length");
    let (status, too_large) = fixture
        .request_raw("PATCH", &item, Vec::new(), "2000001")
        .await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE, "{too_large}");
    assert_eq!(too_large["error"], "Request body is too large");
    assert_eq!(too_large["code"], "upload_too_large");
    let (status, bad_utf8) = fixture.request_raw("PATCH", &item, vec![0xff], "1").await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{bad_utf8}");
    assert_eq!(
        bad_utf8,
        json!({"error": "Server error", "code": "internal"})
    );
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), original);

    let refused = mutation_oracle(
        oracle_workspace(fixture.root.path()).path(),
        &json!([
            {"op": "update", "automationId": AUTO, "patch": {"trigger": {"type": "nope"}}},
            {"op": "update", "automationId": AUTO, "patch": {"projectId": "proj_missing"}},
            {"op": "update", "automationId": "auto_missing", "patch": {"name": "Renamed"}}
        ]),
    );
    let (status, bad_trigger) = fixture
        .request_json("PATCH", &item, &json!({"trigger": {"type": "nope"}}))
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{bad_trigger}");
    assert_eq!(bad_trigger, refused[0]["body"]);
    let (status, missing_project) = fixture
        .request_json("PATCH", &item, &json!({"projectId": "proj_missing"}))
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{missing_project}");
    assert_eq!(missing_project, refused[1]["body"]);
    assert_eq!(missing_project["error"], "Project not found");
    let (status, missing_row) = fixture
        .request_json(
            "PATCH",
            "/api/automation/auto_missing",
            &json!({"name": "Renamed"}),
        )
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{missing_row}");
    assert_eq!(missing_row, refused[2]["body"]);
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), original);
    assert!(!fixture.root.path().join(".projects").exists());

    let oracle_root = oracle_workspace(fixture.root.path());
    let expected = mutation_oracle(
        oracle_root.path(),
        &json!([{"op": "update", "automationId": AUTO, "patch": {"name": "Renamed"}}]),
    );
    let (status, renamed) = fixture
        .request_json("PATCH", &item, &json!({"name": "Renamed"}))
        .await;
    assert_eq!(status, StatusCode::OK, "{renamed}");
    assert_eq!(expected[0]["status"], 200);
    assert_same_automation(&renamed, &expected[0]["body"]);
    assert_fresh_clock(&renamed["automation"]);
    assert_eq!(renamed["automation"]["name"], "Renamed");
    assert_eq!(renamed["automation"]["createdAt"], "stored-created");
    assert_eq!(renamed["automation"]["projectId"], "");
    let (status, head, _) = fixture.request_parts("HEAD", &item, true, None, None).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(head, Value::Null);
    let (status, reread) = fixture.request("GET", &item, true).await;
    assert_eq!(status, StatusCode::OK, "{reread}");
    assert_eq!(reread, renamed);
    assert!(!fixture.automation_dir().join("history.json").exists());
    assert!(!fixture.root.path().join(".projects").exists());

    std::fs::create_dir_all(fixture.root.path().join(".projects/proj_ok")).unwrap();
    std::fs::write(
        fixture.root.path().join(".projects/proj_ok/project.json"),
        b"{\"id\":\"proj_ok\",\"name\":\"Demo\",\"createdAt\":10,\"updatedAt\":10}\n",
    )
    .unwrap();
    let project_oracle = oracle_workspace(fixture.root.path());
    let moved = mutation_oracle(
        project_oracle.path(),
        &json!([{"op": "update", "automationId": AUTO, "patch": {"projectId": "proj_ok"}}]),
    );
    let (status, moved_native) = fixture
        .request_json("PATCH", &item, &json!({"projectId": "proj_ok"}))
        .await;
    assert_eq!(status, StatusCode::OK, "{moved_native}");
    assert_same_automation(&moved_native, &moved[0]["body"]);
    assert_eq!(moved_native["automation"]["projectId"], "proj_ok");
    let project_text =
        std::fs::read_to_string(fixture.root.path().join(".projects/proj_ok/project.json"))
            .unwrap();
    let project: Value = serde_json::from_str(&project_text).unwrap();
    assert!(project["updatedAt"].as_i64().unwrap() > 10, "{project}");
    let oracle_project: Value = serde_json::from_str(
        &std::fs::read_to_string(project_oracle.path().join(".projects/proj_ok/project.json"))
            .unwrap(),
    )
    .unwrap();
    assert!(oracle_project["updatedAt"].as_i64().unwrap() > 10);

    let deleted_expected = mutation_oracle(
        oracle_workspace(fixture.root.path()).path(),
        &json!([{"op": "delete", "automationId": AUTO}]),
    );
    let (status, deleted) = fixture.request("DELETE", &item, true).await;
    assert_eq!(status, StatusCode::OK, "{deleted}");
    assert_eq!(deleted, deleted_expected[0]["body"]);
    assert_eq!(deleted, json!({"ok": true, "deleted": 1}));
    let (status, gone) = fixture.request("GET", &item, true).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{gone}");
    let after_delete = std::fs::read(fixture.store_path()).unwrap();
    let (status, deleted_again) = fixture.request("DELETE", &item, true).await;
    assert_eq!(status, StatusCode::OK, "{deleted_again}");
    assert_eq!(deleted_again, json!({"ok": true, "deleted": 0}));
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), after_delete);

    let mut window = vec![json!({
        "automationId": "auto_drop",
        "action": {"type": "save_item"},
        "createdAt": "dropped",
        "createdAtMs": 1_700_000_000_000i64,
        "updatedAtMs": 1_700_000_000_000i64
    })];
    window.extend(numbered(500));
    fixture.write_store(&Value::Array(window));
    let window_bytes = std::fs::read(fixture.store_path()).unwrap();
    let hidden = mutation_oracle(
        oracle_workspace(fixture.root.path()).path(),
        &json!([{"op": "update", "automationId": "auto_drop", "patch": {"name": "Hidden"}}]),
    );
    let (status, hidden_native) = fixture
        .request_json(
            "PATCH",
            "/api/automation/auto_drop",
            &json!({"name": "Hidden"}),
        )
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{hidden_native}");
    assert_eq!(hidden_native, hidden[0]["body"]);
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), window_bytes);

    let kept = mutation_oracle(
        oracle_workspace(fixture.root.path()).path(),
        &json!([{"op": "update", "automationId": "auto_0499", "patch": {"name": "Last"}}]),
    );
    let (status, last) = fixture
        .request_json(
            "PATCH",
            "/api/automation/auto_0499",
            &json!({"name": "Last"}),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{last}");
    assert_same_automation(&last, &kept[0]["body"]);
    assert_eq!(last["automation"]["name"], "Last");
    let stored: Value =
        serde_json::from_slice(&std::fs::read(fixture.store_path()).unwrap()).unwrap();
    let ids: Vec<&str> = stored["automations"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|item| item["automationId"].as_str())
        .collect();
    assert_eq!(ids.len(), 500);
    assert!(!ids.contains(&"auto_drop"));
    assert!(ids.contains(&"auto_0499"));
    let (status, dropped) = fixture
        .request("GET", "/api/automation/auto_drop", true)
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{dropped}");
}
