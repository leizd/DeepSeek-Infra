//! `GET /api/automation/{automation_id}/runs` and `POST .../run` through the
//! production router.
//!
//! The list body is `history.list_runs` after the route's `int(limit or 100)`.
//! A missing `.automation/history.json` stays missing. `POST .../run` is
//! `runner.run_once`: a legal `save_item` persists history and the saved item,
//! and an illegal request is refused before that write.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "automation-run-route-token";
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

    fn history_path(&self) -> PathBuf {
        self.automation_dir().join("history.json")
    }

    fn write_history(&self, runs: &Value) {
        std::fs::create_dir_all(self.automation_dir()).unwrap();
        std::fs::write(
            self.history_path(),
            serde_json::to_vec(&json!({"runs": runs})).unwrap(),
        )
        .unwrap();
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
        let (status, _headers, body) = self.exchange(method, uri, auth, None, &[]).await;
        (status, body)
    }

    async fn request_json(&self, uri: &str, payload: &Value) -> (StatusCode, Value) {
        let bytes = serde_json::to_vec(payload).unwrap();
        let (status, _headers, body) = self
            .exchange("POST", uri, true, Some(&bytes.len().to_string()), &bytes)
            .await;
        (status, body)
    }

    async fn exchange(
        &self,
        method: &str,
        uri: &str,
        auth: bool,
        content_length: Option<&str>,
        body: &[u8],
    ) -> (StatusCode, axum::http::HeaderMap, Value) {
        let mut request = Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, "127.0.0.1");
        if auth {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        if let Some(length) = content_length {
            request = request.header(header::CONTENT_LENGTH, length);
        }
        let response = self
            .app
            .clone()
            .oneshot(request.body(Body::from(body.to_vec())).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let headers = response.headers().clone();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        let parsed = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        (status, headers, parsed)
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
from deepseek_infra.infra.automation.history import list_runs
plan = json.loads(sys.stdin.read())
results = []
for case in plan:
    automation_id = case["automationId"]
    try:
        if "limit" in case:
            limit = int(case["limit"] or 100)
        else:
            limit = int(100)
        body = {"ok": True, "runs": list_runs(automation_id=automation_id, limit=limit)}
        results.append({"status": 200, "body": body})
    except AppError as exc:
        results.append({"status": exc.status, "body": {"error": str(exc), "code": exc.code.value}})
    except (TypeError, ValueError):
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
        let stdin = child.stdin.as_mut().expect("stdin");
        stdin
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

fn runs_uri(automation_id: &str, limit: Option<&str>, extra: &str) -> String {
    match limit {
        None => format!("/api/automation/{automation_id}/runs{extra}"),
        Some(limit) => {
            let encoded = urlencoding_query(limit);
            if extra.is_empty() {
                format!("/api/automation/{automation_id}/runs?limit={encoded}")
            } else {
                format!("/api/automation/{automation_id}/runs?limit={encoded}&{extra}")
            }
        }
    }
}

fn urlencoding_query(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                encoded.push(byte as char);
            }
            _ => encoded.push_str(&format!("%{byte:02X}")),
        }
    }
    encoded
}

async fn expect_oracle(
    fixture: &Fixture,
    automation_id: &str,
    limit: Option<&str>,
    extra: &str,
    expected: &Value,
) {
    let (status, body) = fixture
        .request("GET", &runs_uri(automation_id, limit, extra), true)
        .await;
    let expected_status =
        StatusCode::from_u16(expected["status"].as_u64().unwrap() as u16).unwrap();
    assert_eq!(
        status, expected_status,
        "limit={limit:?} body={body} expected={expected}"
    );
    assert_eq!(body, expected["body"], "limit={limit:?}");
}

fn rich_runs() -> Value {
    let long_trace = "测".repeat(130);
    let long_log = "L".repeat(1_005);
    let long_id = "a".repeat(130);
    json!([
        {
            "runId": "run_early",
            "automationId": "auto_keep",
            "projectId": "proj_a",
            "status": "SUCCESS",
            "startedAtMs": 1000,
            "finishedAtMs": 2500,
            "startedAt": "",
            "finishedAt": "",
            "trigger": "nope",
            "outputs": {
                "artifactIds": [" art_1 ", "art_1", "", "  ", long_id, 5, true],
                "savedItemIds": "not-a-list",
                "mediaIds": [],
                "exportIds": [null, "exp_1", "exp_1"]
            },
            "traceId": long_trace,
            "attempts": 0,
            "error": false,
            "skippedReason": 0,
            "logs": [null, "hello", 5, true, ["x"], long_log],
            "evidence": []
        },
        {
            "runId": "run_tie_a",
            "automationId": " auto_keep ",
            "projectId": "",
            "status": "skipped",
            "startedAtMs": "2000",
            "finishedAtMs": "2000",
            "startedAt": "kept-a",
            "finishedAt": "kept-a",
            "durationMs": -4,
            "trigger": {"type": "schedule", "cron": "0 1 * * *", "extra": 1},
            "evidence": {"score": 1.5, "note": "ok"}
        },
        {
            "runId": "run_tie_b",
            "automationId": "auto_keep",
            "projectId": "proj_b",
            "status": "canceled",
            "startedAtMs": 2000,
            "finishedAtMs": 2000,
            "startedAt": "kept-b",
            "finishedAt": "kept-b",
            "attempts": "3",
            "error": true,
            "logs": "nope"
        },
        {
            "runId": "run_late",
            "automationId": "auto_keep",
            "status": "requires_confirmation",
            "startedAtMs": 3000,
            "finishedAtMs": 3000,
            "startedAt": "kept-late",
            "finishedAt": "kept-late",
            "trigger": {},
            "evidence": {}
        },
        {
            "runId": "run_other",
            "automationId": "auto_other",
            "status": "success",
            "startedAtMs": 9000,
            "finishedAtMs": 9000,
            "startedAt": "other",
            "finishedAt": "other"
        },
        {"runId": "no", "automationId": "auto_keep", "startedAtMs": 8000},
        {"runId": "run_bad_auto", "automationId": "x", "startedAtMs": 8000},
        {"runId": "run_bad_project", "automationId": "auto_keep", "projectId": "ab", "startedAtMs": 8000},
        "not-an-object",
        {"runId": true, "automationId": "auto_keep", "status": "success", "startedAtMs": 1500, "finishedAtMs": 1500, "startedAt": "from-bool", "finishedAt": "from-bool"}
    ])
}

fn numbered_runs(count: usize, automation_id: &str, started_from: i64) -> Vec<Value> {
    (0..count)
        .map(|index| {
            let started = started_from + index as i64;
            let run_id = format!("run_{index:04}");
            json!({
                "runId": run_id,
                "automationId": automation_id,
                "projectId": "proj_ok",
                "status": "success",
                "startedAtMs": started,
                "finishedAtMs": started,
                "startedAt": "kept",
                "finishedAt": "kept"
            })
        })
        .collect()
}

#[tokio::test]
async fn automation_runs_match_list_runs_and_do_not_create_a_missing_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let runs = format!("/api/automation/{AUTO}/runs");

    let (status, unauthenticated) = fixture.request("GET", &runs, false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
    assert_eq!(unauthenticated["error"]["message"], "Auth required");
    assert!(!fixture.automation_dir().exists());

    let (status, missing) = fixture.request("GET", &runs, true).await;
    assert_eq!(status, StatusCode::OK, "{missing}");
    assert_eq!(missing, json!({"ok": true, "runs": []}));
    assert!(!fixture.automation_dir().exists());

    let before_write = oracle(
        fixture.root.path(),
        &json!([
            {"automationId": "no"},
            {"automationId": "no", "limit": "abc"},
            {"automationId": AUTO, "limit": "abc"},
            {"automationId": AUTO, "limit": "1.5"},
            {"automationId": AUTO, "limit": "  "},
            {"automationId": AUTO}
        ]),
    );
    expect_oracle(&fixture, "no", None, "", &before_write[0]).await;
    assert!(!fixture.automation_dir().exists());
    expect_oracle(&fixture, "no", Some("abc"), "", &before_write[1]).await;
    assert!(!fixture.automation_dir().exists());
    expect_oracle(&fixture, AUTO, Some("abc"), "", &before_write[2]).await;
    expect_oracle(&fixture, AUTO, Some("1.5"), "", &before_write[3]).await;
    expect_oracle(&fixture, AUTO, Some("  "), "", &before_write[4]).await;
    expect_oracle(&fixture, AUTO, None, "", &before_write[5]).await;
    assert!(!fixture.automation_dir().exists());

    std::fs::create_dir_all(fixture.automation_dir()).unwrap();
    let (status, empty_dir) = fixture.request("GET", &runs, true).await;
    assert_eq!(status, StatusCode::OK, "{empty_dir}");
    assert_eq!(empty_dir, json!({"ok": true, "runs": []}));
    assert!(fixture.automation_dir().is_dir());
    assert!(!fixture.history_path().exists());

    fixture.write_history(&rich_runs());
    let rich_bytes = std::fs::read(fixture.history_path()).unwrap();
    let rich_cases = json!([
        {"automationId": AUTO},
        {"automationId": AUTO, "limit": ""},
        {"automationId": AUTO, "limit": "0"},
        {"automationId": AUTO, "limit": "-1"},
        {"automationId": AUTO, "limit": "1"},
        {"automationId": AUTO, "limit": "2001"},
        {"automationId": AUTO, "limit": "1_0"},
        {"automationId": AUTO, "limit": "+2"},
        {"automationId": " auto_keep "},
        {"automationId": "auto_other", "limit": "10"}
    ]);
    let rich_expected = oracle(fixture.root.path(), &rich_cases);
    expect_oracle(&fixture, AUTO, None, "", &rich_expected[0]).await;
    expect_oracle(&fixture, AUTO, Some(""), "", &rich_expected[1]).await;
    expect_oracle(&fixture, AUTO, Some("0"), "", &rich_expected[2]).await;
    expect_oracle(&fixture, AUTO, Some("-1"), "", &rich_expected[3]).await;
    expect_oracle(&fixture, AUTO, Some("1"), "", &rich_expected[4]).await;
    expect_oracle(&fixture, AUTO, Some("2001"), "", &rich_expected[5]).await;
    expect_oracle(&fixture, AUTO, Some("1_0"), "", &rich_expected[6]).await;
    expect_oracle(&fixture, AUTO, Some("+2"), "", &rich_expected[7]).await;
    expect_oracle(&fixture, "%20auto_keep%20", None, "", &rich_expected[8]).await;
    expect_oracle(
        &fixture,
        AUTO,
        None,
        "?projectId=proj_a&status=failed",
        &rich_expected[0],
    )
    .await;
    expect_oracle(&fixture, "auto_other", Some("10"), "", &rich_expected[9]).await;
    assert_eq!(std::fs::read(fixture.history_path()).unwrap(), rich_bytes);
    assert_eq!(
        rich_expected[0]["body"]["runs"].as_array().unwrap().len(),
        5
    );
    assert_eq!(rich_expected[4]["body"]["runs"][0]["runId"], "run_late");

    let mut many = numbered_runs(150, AUTO, 1);
    fixture.write_history(&Value::Array(many.clone()));
    let many_bytes = std::fs::read(fixture.history_path()).unwrap();
    let many_expected = oracle(
        fixture.root.path(),
        &json!([
            {"automationId": AUTO, "limit": "0"},
            {"automationId": AUTO, "limit": "-5"},
            {"automationId": AUTO},
            {"automationId": AUTO, "limit": "2001"}
        ]),
    );
    expect_oracle(&fixture, AUTO, Some("0"), "", &many_expected[0]).await;
    expect_oracle(&fixture, AUTO, Some("-5"), "", &many_expected[1]).await;
    expect_oracle(&fixture, AUTO, None, "", &many_expected[2]).await;
    expect_oracle(&fixture, AUTO, Some("2001"), "", &many_expected[3]).await;
    assert_eq!(
        many_expected[0]["body"]["runs"].as_array().unwrap().len(),
        100
    );
    assert_eq!(
        many_expected[1]["body"]["runs"].as_array().unwrap().len(),
        150
    );
    assert_eq!(
        many_expected[2]["body"]["runs"].as_array().unwrap().len(),
        100
    );
    assert_eq!(
        many_expected[3]["body"]["runs"].as_array().unwrap().len(),
        150
    );
    assert_eq!(std::fs::read(fixture.history_path()).unwrap(), many_bytes);
    let _ = many.pop();

    let mut truncated = vec![json!({
        "runId": "run_dropped",
        "automationId": AUTO,
        "projectId": "proj_ok",
        "status": "success",
        "startedAtMs": 9_000_000,
        "finishedAtMs": 9_000_000,
        "startedAt": "dropped",
        "finishedAt": "dropped"
    })];
    truncated.extend(numbered_runs(2_000, "auto_other", 1));
    fixture.write_history(&Value::Array(truncated));
    let truncated_bytes = std::fs::read(fixture.history_path()).unwrap();
    let truncated_expected = oracle(
        fixture.root.path(),
        &json!([
            {"automationId": AUTO, "limit": "-1"},
            {"automationId": "auto_other", "limit": "-1"}
        ]),
    );
    expect_oracle(&fixture, AUTO, Some("-1"), "", &truncated_expected[0]).await;
    expect_oracle(
        &fixture,
        "auto_other",
        Some("-1"),
        "",
        &truncated_expected[1],
    )
    .await;
    assert!(
        truncated_expected[0]["body"]["runs"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert_eq!(
        truncated_expected[1]["body"]["runs"]
            .as_array()
            .unwrap()
            .len(),
        2_000
    );
    assert_eq!(
        std::fs::read(fixture.history_path()).unwrap(),
        truncated_bytes
    );

    fixture.write_history(&json!([
        {
            "automationId": AUTO,
            "status": "success",
            "finishedAtMs": 4_000,
            "startedAt": "",
            "finishedAt": ""
        }
    ]));
    let fresh_bytes = std::fs::read(fixture.history_path()).unwrap();
    let (status, fresh) = fixture.request("GET", &runs, true).await;
    assert_eq!(status, StatusCode::OK, "{fresh}");
    let generated = &fresh["runs"][0];
    let run_id = generated["runId"].as_str().unwrap();
    assert!(run_id.starts_with("auto_run_"), "{run_id}");
    assert_eq!(run_id.len(), "auto_run_".len() + 16);
    assert!(generated["startedAtMs"].as_i64().unwrap() > 1_700_000_000_000);
    assert!(generated["startedAt"].as_str().unwrap().ends_with('Z'));
    assert_eq!(std::fs::read(fixture.history_path()).unwrap(), fresh_bytes);

    std::fs::write(fixture.history_path(), b"{").unwrap();
    let (status, bad_json) = fixture.request("GET", &runs, true).await;
    assert_eq!(status, StatusCode::OK, "{bad_json}");
    assert_eq!(bad_json, json!({"ok": true, "runs": []}));
    assert_eq!(std::fs::read(fixture.history_path()).unwrap(), b"{");

    std::fs::write(fixture.history_path(), b"[]").unwrap();
    let (status, array_root) = fixture.request("GET", &runs, true).await;
    assert_eq!(status, StatusCode::OK, "{array_root}");
    assert_eq!(array_root, json!({"ok": true, "runs": []}));

    std::fs::write(fixture.history_path(), b"{\"runs\": {}}").unwrap();
    let (status, bad_runs) = fixture.request("GET", &runs, true).await;
    assert_eq!(status, StatusCode::OK, "{bad_runs}");
    assert_eq!(bad_runs, json!({"ok": true, "runs": []}));

    std::fs::write(fixture.history_path(), [0xff, 0xfe, b'{']).unwrap();
    let (status, bad_utf8) = fixture.request("GET", &runs, true).await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{bad_utf8}");
    assert_eq!(
        bad_utf8,
        json!({"error": "Server error", "code": "internal"})
    );
    assert_eq!(
        std::fs::read(fixture.history_path()).unwrap(),
        vec![0xff, 0xfe, b'{']
    );

    std::fs::remove_file(fixture.history_path()).unwrap();
    std::fs::create_dir(fixture.history_path()).unwrap();
    let (status, history_dir) = fixture.request("GET", &runs, true).await;
    assert_eq!(status, StatusCode::OK, "{history_dir}");
    assert_eq!(history_dir, json!({"ok": true, "runs": []}));
    assert!(fixture.history_path().is_dir());

    let (status, posted) = fixture.request("POST", &runs, true).await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{posted}");

    let (status, run_once) = fixture
        .request("POST", &format!("/api/automation/{AUTO}/run"), true)
        .await;
    // `history.json` is a directory and the registry is absent, so this is a
    // missing automation, not a write and not the Go proxy.
    assert_eq!(status, StatusCode::NOT_FOUND, "{run_once}");
    assert_eq!(
        run_once,
        json!({"error": "Automation not found", "code": "not_found"})
    );
    assert!(fixture.history_path().is_dir());
    assert!(!fixture.automation_dir().join("automations.json").exists());

    let (status, templates) = fixture
        .request("GET", "/api/automation/templates", true)
        .await;
    assert_eq!(status, StatusCode::OK, "{templates}");
    assert_eq!(templates["ok"], true);
    assert_eq!(templates["templates"].as_array().unwrap().len(), 6);
}

const SENTENCE: &str = "Automation Keep notes completed successfully with 1 saved items.";

fn write_project(fixture: &Fixture) {
    let dir = fixture.root.path().join(".projects/proj_ok");
    std::fs::create_dir_all(&dir).unwrap();
    std::fs::write(
        dir.join("project.json"),
        b"{\"id\":\"proj_ok\",\"name\":\"Demo\",\"createdAt\":10,\"updatedAt\":10}\n",
    )
    .unwrap();
}

fn project_updated_at(fixture: &Fixture) -> i64 {
    let value: Value = serde_json::from_slice(
        &std::fs::read(fixture.root.path().join(".projects/proj_ok/project.json")).unwrap(),
    )
    .unwrap();
    value["updatedAt"].as_i64().expect("updatedAt")
}

fn saved_items(fixture: &Fixture) -> Vec<Value> {
    let path = fixture
        .root
        .path()
        .join(".projects/proj_ok/saved-items.json");
    if !path.exists() {
        return Vec::new();
    }
    let value: Value = serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
    value
        .get("items")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

fn memory_sentence_count(root: &Path) -> usize {
    let Some(value) = read_memories(root) else {
        return 0;
    };
    let mut count = 0usize;
    walk_contents(&value, &mut count);
    count
}

/// Identical success sentences share `memory_fingerprint(content, scope)`, so the
/// second save updates that one row. Its `source.runId` is the later run.
fn memory_source_run_id(root: &Path) -> String {
    let Some(value) = read_memories(root) else {
        return String::new();
    };
    source_run_id(&value).unwrap_or_default()
}

fn read_memories(root: &Path) -> Option<Value> {
    let path = root.join(".memory/memories.json");
    if !path.is_file() {
        return None;
    }
    serde_json::from_slice(&std::fs::read(path).ok()?).ok()
}

fn source_run_id(value: &Value) -> Option<String> {
    match value {
        Value::Object(fields) => {
            if fields.get("content").and_then(Value::as_str) == Some(SENTENCE) {
                if let Some(run_id) = fields
                    .get("source")
                    .and_then(|source| source.get("runId"))
                    .and_then(Value::as_str)
                {
                    return Some(run_id.to_string());
                }
            }
            for child in fields.values() {
                if let Some(found) = source_run_id(child) {
                    return Some(found);
                }
            }
            None
        }
        Value::Array(items) => items.iter().find_map(source_run_id),
        _ => None,
    }
}

fn walk_contents(value: &Value, count: &mut usize) {
    match value {
        Value::Object(fields) => {
            if fields.get("content").and_then(Value::as_str) == Some(SENTENCE) {
                *count += 1;
            }
            for child in fields.values() {
                walk_contents(child, count);
            }
        }
        Value::Array(items) => {
            for child in items {
                walk_contents(child, count);
            }
        }
        _ => {}
    }
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

fn run_oracle(root: &Path, cases: &Value) -> Vec<Value> {
    let script = r#"
import json, sys
from datetime import datetime, timezone
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.automation.runner import run_once

def trigger_payload(payload):
    value = payload.get("trigger")
    return value if isinstance(value, dict) else {"type": "manual"}

def now_payload(payload):
    value = payload.get("now")
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return datetime.fromtimestamp(float(int(value)) / 1000, tz=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AppError("now must be an ISO timestamp or epoch milliseconds", code=ErrorCode.INVALID_PAYLOAD) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def as_bool(payload, key):
    if key not in payload:
        return False
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

plan = json.loads(sys.stdin.read())
results = []
for case in plan:
    payload = case.get("payload") if isinstance(case.get("payload"), dict) else {}
    try:
        run = run_once(
            case["automationId"],
            trigger=trigger_payload(payload),
            event=payload.get("event") if isinstance(payload.get("event"), dict) else None,
            now=now_payload(payload),
            confirmed=as_bool(payload, "confirmed"),
            force=as_bool(payload, "force"),
        )
        results.append({"status": 200, "body": {"ok": True, "run": run}})
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
        .expect("python run oracle");
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
        "run oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("run oracle json")
}

fn strip_run(run: &Value) -> Value {
    let mut run = run.clone();
    let Some(fields) = run.as_object_mut() else {
        return run;
    };
    for key in [
        "runId",
        "traceId",
        "startedAt",
        "finishedAt",
        "startedAtMs",
        "finishedAtMs",
        "durationMs",
    ] {
        fields.remove(key);
    }
    if let Some(outputs) = fields.get_mut("outputs").and_then(Value::as_object_mut) {
        for key in ["artifactIds", "savedItemIds", "mediaIds", "exportIds"] {
            if let Some(ids) = outputs.get(key).and_then(Value::as_array) {
                let length = ids.len();
                outputs.insert(key.to_string(), json!(length));
            }
        }
    }
    if let Some(evidence) = fields.get_mut("evidence") {
        strip_evidence(evidence);
    }
    run
}

fn strip_evidence(evidence: &mut Value) {
    let Some(fields) = evidence.as_object_mut() else {
        return;
    };
    if let Some(runtime) = fields.get_mut("runtime").and_then(Value::as_object_mut) {
        runtime.remove("timeoutCheckedAtMs");
        if let Some(attempts) = runtime
            .get_mut("attemptErrors")
            .and_then(Value::as_array_mut)
        {
            for attempt in attempts {
                if let Some(item) = attempt.as_object_mut() {
                    item.remove("timeoutCheckedAtMs");
                }
            }
        }
    }
    let Some(saved) = fields
        .get_mut("action")
        .and_then(|action| action.get_mut("savedItem"))
        .and_then(Value::as_object_mut)
    else {
        return;
    };
    saved.remove("savedId");
    saved.remove("createdAt");
    saved.remove("createdAtMs");
    if let Some(source) = saved.get_mut("sourceRef").and_then(Value::as_object_mut) {
        source.insert("runId".to_string(), Value::String(String::new()));
    }
}

fn assert_listed(fixture_runs: &Value, run: &Value) {
    let run_id = run["runId"].as_str().expect("runId");
    let found = fixture_runs
        .as_array()
        .expect("runs")
        .iter()
        .find(|item| item["runId"].as_str() == Some(run_id))
        .expect("listed run");
    assert_eq!(found, run);
}

fn seed_automations() -> Value {
    json!([
        {
            "automationId": "auto_keep",
            "name": "Keep notes",
            "enabled": true,
            "projectId": "proj_ok",
            "condition": {"type": "always"},
            "action": {
                "type": "save_item",
                "title": "Kept note",
                "content": "hello from automation"
            }
        },
        {
            "automationId": "auto_off",
            "name": "Off",
            "enabled": false,
            "projectId": "proj_ok",
            "action": {
                "type": "save_item",
                "title": "Should not save",
                "content": "nope"
            }
        },
        {
            "automationId": "auto_noproj",
            "name": "No project",
            "enabled": true,
            "projectId": "",
            "action": {
                "type": "save_item",
                "title": "Missing project",
                "content": "x"
            }
        }
    ])
}

#[tokio::test]
async fn post_run_once_persists_save_item_and_matches_python() {
    let _lock = EnvLock::acquire();
    let _automation_env = EnvGuard::set(&[
        ("AUTOMATION_ENABLED", "1".to_string()),
        ("AUTOMATION_MAX_RUNS_PER_DAY", "50".to_string()),
        ("AUTOMATION_MIN_INTERVAL_SECONDS", "300".to_string()),
        ("AUTOMATION_ALLOW_BROWSER", "0".to_string()),
        (
            "AUTOMATION_REQUIRE_CONFIRM_FOR_BROWSER_WRITE",
            "1".to_string(),
        ),
        ("AUTOMATION_RUN_TIMEOUT_SECONDS", "1800".to_string()),
    ]);
    let fixture = Fixture::with_mode("python_disabled");
    write_project(&fixture);
    fixture.write_store(&seed_automations());
    let registry_bytes = std::fs::read(fixture.store_path()).unwrap();
    assert!(!fixture.history_path().exists());
    assert!(saved_items(&fixture).is_empty());

    // Exact cron match first. A skipped run in the same civil minute would make
    // the later match return schedule_already_ran and hide the save.
    let cases = json!([
        {
            "automationId": "auto_keep",
            "payload": {
                "trigger": {"type": "schedule", "cron": "34 12 4 10 *"},
                "now": "2026-10-04T12:34:00+00:00"
            }
        },
        {
            "automationId": "auto_keep",
            "payload": {
                "trigger": {"type": "schedule", "cron": "0 0 * * *"},
                "now": "2026-10-04T15:00:00+00:00"
            }
        },
        {"automationId": "auto_off", "payload": {}},
        {"automationId": "auto_noproj", "payload": {"trigger": {"type": "manual"}}},
        {"automationId": "auto_keep", "payload": {}}
    ]);
    let copy = oracle_workspace(fixture.root.path());
    let expected = run_oracle(copy.path(), &cases);

    let keep = format!("/api/automation/{AUTO}/run");
    let (status, exact) = fixture
        .request_json(
            &keep,
            &json!({
                "trigger": {"type": "schedule", "cron": "34 12 4 10 *"},
                "now": "2026-10-04T12:34:00+00:00"
            }),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{exact}");
    assert_eq!(exact["ok"], true);
    assert_eq!(exact["run"]["status"], "success");
    assert_eq!(exact["run"]["skippedReason"], "");
    assert_eq!(exact["run"]["error"], "");
    assert_eq!(exact["run"]["traceId"], "");
    assert_eq!(exact["run"]["logs"], json!(["saveItem"]));
    assert_eq!(
        exact["run"]["startedAtMs"],
        expected[0]["body"]["run"]["startedAtMs"]
    );
    let items = saved_items(&fixture);
    assert_eq!(items.len(), 1);
    assert_eq!(items[0]["title"], "Kept note");
    assert_eq!(items[0]["content"], "hello from automation");
    assert_eq!(items[0]["type"], "assistant_answer");
    assert_eq!(items[0]["purpose"], "reference");
    assert_eq!(items[0]["tags"], json!(["automation"]));
    let saved_id = items[0]["savedId"].as_str().unwrap();
    assert!(saved_id.starts_with("save_"), "{saved_id}");
    assert_eq!(saved_id.len(), "save_".len() + 16);
    assert!(
        saved_id[5..]
            .chars()
            .all(|ch| ch.is_ascii_hexdigit() && !ch.is_ascii_uppercase())
    );
    let updated_after_save = project_updated_at(&fixture);
    assert!(updated_after_save > 10, "{updated_after_save}");
    assert_eq!(memory_sentence_count(fixture.root.path()), 1);
    let exact_run_id = exact["run"]["runId"].as_str().unwrap().to_string();
    assert_eq!(memory_source_run_id(fixture.root.path()), exact_run_id);
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), registry_bytes);

    let (status, not_due) = fixture
        .request_json(
            &keep,
            &json!({
                "trigger": {"type": "schedule", "cron": "0 0 * * *"},
                "now": "2026-10-04T15:00:00+00:00"
            }),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{not_due}");
    assert_eq!(not_due["run"]["status"], "skipped");
    assert_eq!(not_due["run"]["skippedReason"], "schedule_not_due");
    assert_eq!(not_due["run"]["logs"], json!(["schedule_not_due"]));
    assert_eq!(
        not_due["run"]["startedAtMs"],
        expected[1]["body"]["run"]["startedAtMs"]
    );
    assert_eq!(saved_items(&fixture).len(), 1);
    assert_eq!(project_updated_at(&fixture), updated_after_save);
    assert_eq!(memory_sentence_count(fixture.root.path()), 1);

    let (status, disabled) = fixture
        .request_json("/api/automation/auto_off/run", &json!({}))
        .await;
    assert_eq!(status, StatusCode::OK, "{disabled}");
    assert_eq!(disabled["run"]["status"], "skipped");
    assert_eq!(disabled["run"]["skippedReason"], "automation_disabled");
    assert_eq!(disabled["run"]["logs"], json!(["automation disabled"]));
    assert_eq!(saved_items(&fixture).len(), 1);
    assert!(
        saved_items(&fixture)
            .iter()
            .all(|item| item["title"] != "Should not save")
    );
    assert_eq!(project_updated_at(&fixture), updated_after_save);
    assert_eq!(memory_sentence_count(fixture.root.path()), 1);

    let (status, missing_project) = fixture
        .request_json(
            "/api/automation/auto_noproj/run",
            &json!({"trigger": {"type": "manual"}}),
        )
        .await;
    assert_eq!(status, StatusCode::OK, "{missing_project}");
    assert_eq!(missing_project["run"]["status"], "failed");
    assert_eq!(
        missing_project["run"]["error"],
        "save_item action requires projectId"
    );
    assert_eq!(
        missing_project["run"]["logs"],
        json!(["attempt 1 failed: save_item action requires projectId"])
    );
    assert_eq!(saved_items(&fixture).len(), 1);
    assert_eq!(project_updated_at(&fixture), updated_after_save);
    assert_eq!(memory_sentence_count(fixture.root.path()), 1);

    let (status, manual) = fixture.request("POST", &keep, true).await;
    assert_eq!(status, StatusCode::OK, "{manual}");
    assert_eq!(manual["run"]["status"], "success");
    assert_eq!(manual["run"]["trigger"], json!({"type": "manual"}));
    assert_eq!(manual["run"]["logs"], json!(["saveItem"]));
    assert_eq!(manual["run"]["traceId"], "");
    let run_id = manual["run"]["runId"].as_str().unwrap();
    assert!(run_id.starts_with("auto_run_"), "{run_id}");
    assert_eq!(run_id.len(), "auto_run_".len() + 16);
    assert!(
        run_id[9..]
            .chars()
            .all(|ch| ch.is_ascii_hexdigit() && !ch.is_ascii_uppercase())
    );
    let started = manual["run"]["startedAtMs"].as_i64().unwrap();
    let wall = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    assert!(
        (wall - started).abs() < 15_000,
        "started={started} wall={wall}"
    );
    assert_eq!(saved_items(&fixture).len(), 2);
    assert!(project_updated_at(&fixture) >= updated_after_save);
    // The sentence is identical, so the store updates the first memory instead of
    // inserting a second row. The source run id moving to this run is the write.
    assert_eq!(memory_sentence_count(fixture.root.path()), 1);
    assert_eq!(memory_source_run_id(fixture.root.path()), run_id);
    assert_eq!(memory_sentence_count(copy.path()), 1);
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), registry_bytes);
    assert!(!fixture.root.path().join(".traces").exists());

    let rust_runs = [&exact, &not_due, &disabled, &missing_project, &manual];
    for (index, (rust_body, oracle_body)) in rust_runs.iter().zip(expected.iter()).enumerate() {
        assert_eq!(
            oracle_body["status"], 200,
            "oracle case {index}: {oracle_body}"
        );
        assert_eq!(
            strip_run(&rust_body["run"]),
            strip_run(&oracle_body["body"]["run"]),
            "case {index}"
        );
    }

    for automation_id in [AUTO, "auto_off", "auto_noproj"] {
        let (status, listed) = fixture
            .request(
                "GET",
                &format!("/api/automation/{automation_id}/runs"),
                true,
            )
            .await;
        assert_eq!(status, StatusCode::OK, "{listed}");
        for body in rust_runs {
            if body["run"]["automationId"].as_str() == Some(automation_id) {
                assert_listed(&listed["runs"], &body["run"]);
            }
        }
    }
}

#[tokio::test]
async fn post_run_refuses_illegal_requests_without_rewriting_the_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::with_mode("");
    let missing = "/api/automation/auto_gone/run";

    let (status, unauthenticated) = fixture.request("POST", missing, false).await;
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
    assert_eq!(unauthenticated["error"]["message"], "Auth required");
    assert!(!fixture.automation_dir().exists());

    let (status, bad_id) = fixture
        .request("POST", "/api/automation/no/run", true)
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{bad_id}");
    assert_eq!(
        bad_id,
        json!({"error": "Invalid automation id", "code": "invalid_payload"})
    );
    assert!(!fixture.automation_dir().exists());

    let (status, absent) = fixture.request("POST", missing, true).await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{absent}");
    assert_eq!(
        absent,
        json!({"error": "Automation not found", "code": "not_found"})
    );
    assert!(!fixture.automation_dir().exists());

    let (status, _headers, negative) = fixture
        .exchange("POST", missing, true, Some("-1"), b"not-json")
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND, "{negative}");
    assert_eq!(
        negative,
        json!({"error": "Automation not found", "code": "not_found"})
    );
    assert!(!fixture.automation_dir().exists());

    let (status, bad_now) = fixture
        .request_json(missing, &json!({"now": "yesterday"}))
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{bad_now}");
    assert_eq!(
        bad_now,
        json!({
            "error": "now must be an ISO timestamp or epoch milliseconds",
            "code": "invalid_payload"
        })
    );
    assert!(!fixture.automation_dir().exists());
    assert!(!fixture.root.path().join(".projects").exists());
    assert!(!fixture.root.path().join(".memory").exists());

    write_project(&fixture);
    fixture.write_store(&json!([{
        "automationId": "auto_keep",
        "name": "Keep notes",
        "enabled": true,
        "projectId": "proj_ok",
        "action": {"type": "save_item", "title": "Kept note", "content": "hello from automation"}
    }]));
    let registry_bytes = std::fs::read(fixture.store_path()).unwrap();
    let project_bytes =
        std::fs::read(fixture.root.path().join(".projects/proj_ok/project.json")).unwrap();
    let keep = format!("/api/automation/{AUTO}/run");

    let (status, refused) = fixture.request("POST", &keep, true).await;
    assert_eq!(status, StatusCode::CONFLICT, "{refused}");
    assert_eq!(refused["code"], "NATIVE_AUTOMATION_WRITE_NOT_OWNED");
    assert_eq!(
        refused["error"],
        "The automation store is still written by the Python runtime, so this gateway refuses to mutate it."
    );
    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), registry_bytes);
    assert!(!fixture.history_path().exists());
    assert!(saved_items(&fixture).is_empty());
    assert_eq!(
        std::fs::read(fixture.root.path().join(".projects/proj_ok/project.json")).unwrap(),
        project_bytes
    );
    assert!(!fixture.root.path().join(".memory").exists());

    let (status, _headers, zero_length) =
        fixture.exchange("POST", &keep, true, Some("0"), b"{").await;
    assert_eq!(status, StatusCode::CONFLICT, "{zero_length}");
    assert_eq!(zero_length["code"], "NATIVE_AUTOMATION_WRITE_NOT_OWNED");

    let (status, _headers, bad_json) = fixture.exchange("POST", &keep, true, Some("1"), b"{").await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{bad_json}");
    assert_eq!(bad_json["code"], "invalid_payload");
    assert!(
        bad_json["error"]
            .as_str()
            .unwrap()
            .starts_with("Invalid JSON:"),
        "{bad_json}"
    );

    let (status, _headers, not_object) = fixture
        .exchange("POST", &keep, true, Some("2"), b"[]")
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{not_object}");
    assert_eq!(
        not_object,
        json!({"error": "Request body must be a JSON object", "code": "invalid_payload"})
    );

    let (status, _headers, bad_utf8) = fixture
        .exchange("POST", &keep, true, Some("1"), &[0xff])
        .await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{bad_utf8}");
    assert_eq!(
        bad_utf8,
        json!({"error": "Server error", "code": "internal"})
    );

    let (status, _headers, bad_length) = fixture
        .exchange("POST", &keep, true, Some("nope"), b"{}")
        .await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR, "{bad_length}");
    assert_eq!(
        bad_length,
        json!({"error": "Server error", "code": "internal"})
    );

    let (status, _headers, too_large) = fixture
        .exchange("POST", &keep, true, Some("2000001"), b"{}")
        .await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE, "{too_large}");
    assert_eq!(
        too_large,
        json!({"error": "Request body is too large", "code": "upload_too_large"})
    );

    let (status, owned_now) = fixture
        .request_json(&keep, &json!({"now": "yesterday"}))
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "{owned_now}");
    assert_eq!(
        owned_now,
        json!({
            "error": "now must be an ISO timestamp or epoch milliseconds",
            "code": "invalid_payload"
        })
    );

    let (status, headers, get_run) = fixture.exchange("GET", &keep, true, None, &[]).await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{get_run}");
    assert_eq!(headers[header::ALLOW], "POST");
    assert_eq!(get_run, json!({"detail": "Method Not Allowed"}));
    let (status, headers, delete_run) = fixture.exchange("DELETE", &keep, true, None, &[]).await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{delete_run}");
    assert_eq!(headers[header::ALLOW], "POST");

    assert_eq!(std::fs::read(fixture.store_path()).unwrap(), registry_bytes);
    assert!(!fixture.history_path().exists());
    assert!(saved_items(&fixture).is_empty());
    assert_eq!(
        std::fs::read(fixture.root.path().join(".projects/proj_ok/project.json")).unwrap(),
        project_bytes
    );
    assert!(!fixture.root.path().join(".memory").exists());
}
