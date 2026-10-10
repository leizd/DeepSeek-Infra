//! `GET /api/automation/templates` and
//! `POST /api/automation/templates/{template_id}` through the production router.
//!
//! The catalog body is `registry.list_templates` and does not create
//! `.automation`. Creation is `registry.create_from_template` and writes only
//! when `DEEPSEEK_RUNTIME_MODE=python_disabled`.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "automation-template-route-token";
const TEMPLATES: &str = "/api/automation/templates";

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
    fn new() -> Self {
        Self::with_mode("")
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

fn oracle_templates() -> Value {
    let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf();
    let output = std::process::Command::new("python")
        .arg("-c")
        .arg(
            "import json; from deepseek_infra.infra.automation.registry import list_templates; \
             print(json.dumps({'ok': True, 'templates': list_templates()}, ensure_ascii=False))",
        )
        .current_dir(&repo)
        .output()
        .expect("python oracle");
    assert!(
        output.status.success(),
        "oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("oracle json")
}

#[tokio::test]
async fn automation_templates_match_the_builtin_catalog_without_creating_a_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let expected = oracle_templates();

    let (status, listed) = fixture.request("GET", TEMPLATES, true).await;
    assert_eq!(status, StatusCode::OK, "{listed}");
    assert_eq!(listed, expected);
    assert_eq!(listed["templates"].as_array().unwrap().len(), 6);
    assert!(!fixture.automation_dir().exists());
    assert!(!fixture.root.path().join(".resilience-journal").exists());

    let (status, posted) = fixture.request("POST", TEMPLATES, true).await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{posted}");
    assert!(!fixture.automation_dir().exists());

    let create_uri = format!("{TEMPLATES}/daily_project_summary");
    let (status, create) = fixture.request("POST", &create_uri, true).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{create}");
    assert_eq!(create["error"], "Request body is empty");
    assert_eq!(create["code"], "invalid_payload");
    assert!(!fixture.automation_dir().exists());

    let (status, refused) = fixture
        .request_json("POST", &create_uri, &json!({"projectId": "proj_ok"}))
        .await;
    assert_eq!(status, StatusCode::CONFLICT, "{refused}");
    assert_eq!(refused["code"], "NATIVE_AUTOMATION_WRITE_NOT_OWNED");
    assert_eq!(
        refused["error"],
        "The automation store is still written by the Python runtime, so this gateway refuses to mutate it."
    );
    assert!(!fixture.automation_dir().exists());

    let (status, unknown) = fixture
        .request_json("POST", &format!("{TEMPLATES}/missing_template"), &json!({}))
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{unknown}");
    assert_eq!(unknown["error"], "Automation template not found");
    assert_eq!(unknown["code"], "not_found");
    assert!(!fixture.automation_dir().exists());

    let (status, bad_override) = fixture
        .request_json(
            "POST",
            &create_uri,
            &json!({"overrides": {"trigger": {"type": "nope"}}}),
        )
        .await;
    assert_eq!(status, StatusCode::CONFLICT, "{bad_override}");
    assert_eq!(bad_override["code"], "NATIVE_AUTOMATION_WRITE_NOT_OWNED");
    assert!(!fixture.automation_dir().exists());

    let (status, get_item, headers) = fixture
        .request_parts("GET", &create_uri, true, None, None)
        .await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{get_item}");
    assert_eq!(get_item["detail"], "Method Not Allowed");
    assert_eq!(headers[header::ALLOW], "POST");
    assert!(!fixture.automation_dir().exists());

    let (status, unauthenticated) = fixture.request("GET", TEMPLATES, false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
    assert_eq!(unauthenticated["error"]["message"], "Auth required");
    let (status, unauthenticated_post) = fixture.request("POST", &create_uri, false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated_post}");
    assert!(!fixture.automation_dir().exists());
    assert!(
        !fixture
            .root
            .path()
            .join(".workspace-mutation.lock")
            .exists()
    );

    fixture.write_store(&json!([{
        "automationId": "auto_keep",
        "action": {"type": "save_item"}
    }]));
    let stored = std::fs::read(fixture.store_path()).unwrap();
    let (status, refused_existing) = fixture.request_json("POST", &create_uri, &json!({})).await;
    assert_eq!(status, StatusCode::CONFLICT, "{refused_existing}");
    assert_eq!(
        refused_existing["code"],
        "NATIVE_AUTOMATION_WRITE_NOT_OWNED"
    );
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), stored);
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf()
}

fn mutation_oracle(root: &Path, cases: &Value) -> Vec<Value> {
    let script = r#"
import json, sys
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.automation.registry import create_from_template
plan = json.loads(sys.stdin.read())
results = []
for case in plan:
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    try:
        automation = create_from_template(
            case["templateId"],
            project_id=str(payload.get("projectId") or ""),
            overrides=payload.get("overrides") if isinstance(payload.get("overrides"), dict) else {},
        )
        results.append({"status": 200, "body": {"ok": True, "automation": automation}})
    except AppError as exc:
        results.append({"status": exc.status, "body": {"error": str(exc), "code": exc.code.value}})
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

fn strip_identity(body: &mut Value) {
    let Some(automation) = body.get_mut("automation").and_then(Value::as_object_mut) else {
        return;
    };
    for key in [
        "automationId",
        "id",
        "createdAt",
        "createdAtMs",
        "updatedAt",
        "updatedAtMs",
    ] {
        automation.remove(key);
    }
}

fn assert_same_automation(native: &Value, expected: &Value) {
    let mut native = native.clone();
    let mut expected = expected.clone();
    strip_identity(&mut native);
    strip_identity(&mut expected);
    assert_eq!(native, expected);
}

fn assert_minted_id(automation: &Value) {
    let automation_id = automation["automationId"].as_str().expect("automationId");
    assert_eq!(automation["id"].as_str(), Some(automation_id));
    let Some(suffix) = automation_id.strip_prefix("auto_") else {
        panic!("id {automation_id} has no auto_ prefix");
    };
    assert_eq!(suffix.len(), 16, "{automation_id}");
    assert!(
        suffix
            .chars()
            .all(|ch| ch.is_ascii_hexdigit() && !ch.is_ascii_uppercase()),
        "{automation_id}"
    );
}

fn assert_fresh_clock(automation: &Value) {
    let created = automation["createdAtMs"].as_i64().expect("createdAtMs");
    let updated = automation["updatedAtMs"].as_i64().expect("updatedAtMs");
    assert_eq!(created, updated);
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock")
        .as_millis() as i64;
    assert!(
        (now - updated).abs() < 15_000,
        "updatedAtMs {updated} is not the request clock {now}"
    );
    let rendered =
        deepseek_policy::workspace_schema::timestamp_ms_to_iso(Some(&Value::from(updated)));
    assert_eq!(automation["updatedAt"], rendered);
    assert_eq!(automation["createdAt"], rendered);
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
async fn create_from_template_matches_the_oracle_only_after_python_is_disabled() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::with_mode("python_disabled");
    let create_uri = format!("{TEMPLATES}/daily_project_summary");

    let (status, unauthenticated) = fixture.request("POST", &create_uri, false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
    assert!(!fixture.automation_dir().exists());

    let (status, empty) = fixture.request("POST", &create_uri, true).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{empty}");
    assert_eq!(empty["error"], "Request body is empty");
    let (status, not_object) = fixture
        .request_raw("POST", &create_uri, b"[]".to_vec(), "2")
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{not_object}");
    assert_eq!(not_object["error"], "Request body must be a JSON object");
    let (status, bad_json) = fixture
        .request_raw("POST", &create_uri, b"{".to_vec(), "1")
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{bad_json}");
    assert!(
        bad_json["error"]
            .as_str()
            .unwrap()
            .starts_with("Invalid JSON:"),
        "{bad_json}"
    );
    let (status, bad_length) = fixture
        .request_raw("POST", &create_uri, b"{}".to_vec(), "nope")
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{bad_length}");
    assert_eq!(bad_length["error"], "Invalid Content-Length");
    let (status, too_large) = fixture
        .request_raw("POST", &create_uri, Vec::new(), "2000001")
        .await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE, "{too_large}");
    assert_eq!(too_large["code"], "upload_too_large");
    let (status, bad_utf8) = fixture
        .request_raw("POST", &create_uri, vec![0xff], "1")
        .await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{bad_utf8}");
    assert_eq!(
        bad_utf8,
        json!({"error": "Server error", "code": "internal"})
    );
    assert!(!fixture.automation_dir().exists());

    let refused = mutation_oracle(
        oracle_workspace(fixture.root.path()).path(),
        &json!([
            {"templateId": "missing_template", "payload": {}},
            {"templateId": "daily_project_summary", "payload": {"overrides": {"trigger": {"type": "nope"}}}},
            {"templateId": "daily_project_summary", "payload": {"projectId": "proj_missing"}}
        ]),
    );
    let (status, unknown) = fixture
        .request_json("POST", &format!("{TEMPLATES}/missing_template"), &json!({}))
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{unknown}");
    assert_eq!(unknown, refused[0]["body"]);
    let (status, bad_trigger) = fixture
        .request_json(
            "POST",
            &create_uri,
            &json!({"overrides": {"trigger": {"type": "nope"}}}),
        )
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{bad_trigger}");
    assert_eq!(bad_trigger, refused[1]["body"]);
    let (status, missing_project) = fixture
        .request_json("POST", &create_uri, &json!({"projectId": "proj_missing"}))
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{missing_project}");
    assert_eq!(missing_project, refused[2]["body"]);
    assert_eq!(missing_project["error"], "Project not found");
    assert!(!fixture.automation_dir().exists());
    assert!(!fixture.root.path().join(".projects").exists());
    assert!(
        !fixture
            .root
            .path()
            .join(".workspace-mutation.lock")
            .exists()
    );
    assert!(!fixture.automation_dir().join("history.json").exists());

    std::fs::create_dir_all(fixture.root.path().join(".projects/proj_ok")).unwrap();
    std::fs::write(
        fixture.root.path().join(".projects/proj_ok/project.json"),
        b"{\"id\":\"proj_ok\",\"name\":\"Demo\",\"createdAt\":10,\"updatedAt\":10}\n",
    )
    .unwrap();
    let legal = json!({
        "projectId": "proj_missing",
        "overrides": {"name": "Morning Digest", "projectId": "proj_ok"}
    });
    let oracle_root = oracle_workspace(fixture.root.path());
    let expected = mutation_oracle(
        oracle_root.path(),
        &json!([{"templateId": "daily_project_summary", "payload": legal}]),
    );
    let (status, created) = fixture.request_json("POST", &create_uri, &legal).await;
    assert_eq!(status, StatusCode::OK, "{created}");
    assert_eq!(expected[0]["status"], 200);
    assert_same_automation(&created, &expected[0]["body"]);
    assert_minted_id(&created["automation"]);
    assert_fresh_clock(&created["automation"]);
    assert_eq!(created["automation"]["name"], "Morning Digest");
    assert_eq!(created["automation"]["projectId"], "proj_ok");
    assert_eq!(created["automation"]["trigger"]["type"], "schedule");
    assert_eq!(created["automation"]["action"]["type"], "project_summary");
    let created_id = created["automation"]["automationId"].as_str().unwrap();
    let (status, reread) = fixture
        .request("GET", &format!("/api/automation/{created_id}"), true)
        .await;
    assert_eq!(status, StatusCode::OK, "{reread}");
    assert_eq!(reread, created);
    assert!(!fixture.automation_dir().join("history.json").exists());
    assert!(
        fixture
            .root
            .path()
            .join(".workspace-mutation.lock")
            .exists()
    );
    assert!(fixture.root.path().join(".workspace-generation").exists());
    let project: Value = serde_json::from_str(
        &std::fs::read_to_string(fixture.root.path().join(".projects/proj_ok/project.json"))
            .unwrap(),
    )
    .unwrap();
    assert!(project["updatedAt"].as_i64().unwrap() > 10, "{project}");
    let oracle_project: Value = serde_json::from_str(
        &std::fs::read_to_string(oracle_root.path().join(".projects/proj_ok/project.json"))
            .unwrap(),
    )
    .unwrap();
    assert!(oracle_project["updatedAt"].as_i64().unwrap() > 10);

    let stored = std::fs::read(fixture.store_path()).unwrap();
    let duplicate_payload = json!({"overrides": {"automationId": created_id}});
    let duplicate_oracle = mutation_oracle(
        oracle_workspace(fixture.root.path()).path(),
        &json!([{"templateId": "daily_project_summary", "payload": duplicate_payload}]),
    );
    let (status, duplicate) = fixture
        .request_json("POST", &create_uri, &duplicate_payload)
        .await;
    assert_eq!(status, StatusCode::CONFLICT, "{duplicate}");
    assert_eq!(duplicate, duplicate_oracle[0]["body"]);
    assert_eq!(duplicate["error"], "Automation already exists");
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), stored);

    let mut window = vec![json!({
        "automationId": "auto_drop",
        "action": {"type": "save_item"},
        "createdAt": "dropped",
        "createdAtMs": 1_700_000_000_000i64,
        "updatedAtMs": 1_700_000_000_000i64
    })];
    window.extend(numbered(500));
    fixture.write_store(&Value::Array(window));
    let capped = std::fs::read(fixture.store_path()).unwrap();
    let cap_oracle = mutation_oracle(
        oracle_workspace(fixture.root.path()).path(),
        &json!([{"templateId": "daily_project_summary", "payload": {}}]),
    );
    let (status, capped_body) = fixture.request_json("POST", &create_uri, &json!({})).await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE, "{capped_body}");
    assert_eq!(capped_body, cap_oracle[0]["body"]);
    assert_eq!(capped_body["error"], "Too many automations");
    assert_eq!(capped_body["code"], "upload_too_large");
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), capped);
    let persisted = String::from_utf8(capped.clone()).unwrap();
    assert!(persisted.contains("auto_drop"), "{persisted}");
}
