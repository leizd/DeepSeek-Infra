//! Read-only trace routes through the production router.
//!
//! The Python oracle seeds the real WAL-mode trace store through the
//! `observability` writer, then both sides read the same workspace root. The
//! native edge never creates the directory, the database or its schema, so the
//! assertions cover the exact list/detail/export bodies (with the export
//! timestamp normalised), the not-found refusals and the method routing.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "trace-route-token";

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
            ("AUTH_DISABLED", String::new()),
            ("AUTH_ALLOWED_HOSTS", String::new()),
            ("HOST", String::new()),
            ("TRACE_ENABLED", "1".to_string()),
            ("DEEPSEEK_RUNTIME_MODE", String::new()),
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

    async fn request(
        &self,
        method: &str,
        uri: &str,
        token: bool,
    ) -> (
        StatusCode,
        Vec<u8>,
        Option<String>,
        Option<String>,
        Option<String>,
    ) {
        let mut request = Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, "127.0.0.1");
        if token {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        let response = self
            .app
            .clone()
            .oneshot(request.body(Body::empty()).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let header_value = |name: axum::http::HeaderName| {
            response
                .headers()
                .get(name)
                .and_then(|value| value.to_str().ok())
                .map(str::to_string)
        };
        let allow = header_value(header::ALLOW);
        let access_control_methods = header_value(header::ACCESS_CONTROL_ALLOW_METHODS);
        let content_disposition = header_value(header::CONTENT_DISPOSITION);
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (
            status,
            bytes.to_vec(),
            allow,
            access_control_methods,
            content_disposition,
        )
    }
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf()
}

fn tree(root: &Path) -> Vec<String> {
    let mut found = Vec::new();
    walk(root, root, &mut found);
    found.sort();
    found
}

fn walk(root: &Path, dir: &Path, found: &mut Vec<String>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let relative = path
            .strip_prefix(root)
            .unwrap_or(&path)
            .to_string_lossy()
            .replace('\\', "/");
        found.push(relative);
        if path.is_dir() {
            walk(root, &path, found);
        }
    }
}

/// Removes the trace store's WAL side files after the Python writer exited, so
/// the read-only native connection opens the plain database.
fn drop_wal_side_files(root: &Path) {
    for name in ["traces.sqlite3-wal", "traces.sqlite3-shm"] {
        let path = root.join(".traces").join(name);
        if path.exists() {
            let _ = std::fs::remove_file(path);
        }
    }
}

fn oracle(root: &Path) -> Value {
    let script = r#"
import json, sys
from fastapi.testclient import TestClient
from deepseek_infra.infra.observability import observability as obs
from deepseek_infra.web.server import create_app

old_trace = obs.start_trace(kind="a2a", title="Old run", metadata={"peer": "fleet-a"})
old_span = obs.start_span(old_trace, name="handshake", kind="a2a_peer_call", input_data={"peer": "fleet-a"})
old_span.finish(status="ok", output_data={"ok": True})
obs.finish_trace(old_trace, status="completed")

trace_id = obs.start_trace(kind="chat", title="Seed run", metadata={"agent": "oracle", "apiKey": "secret-value", "note": "x" * 40})
span1 = obs.start_span(trace_id, name="model", kind="deepseek_api", input_data={"prompt": "hello", "api_key": "sk-abcdefghijk12345"})
span1.finish(status="ok", output_data={"text": "world " + "y" * 1500}, usage={"total_tokens": 12, "prompt_tokens": 7}, diagnostics={"cache": "miss"})
span2 = obs.start_span(trace_id, name="tool", kind="tool", input_data={"authorization": "Bearer abcdefghijklmnop"})
span2.finish(status="error", output_data={"error": "boom"}, error="tool failed")
obs.finish_trace(trace_id, status="completed")

client = TestClient(create_app(), base_url="http://127.0.0.1", raise_server_exceptions=False)
headers = {"Host": "127.0.0.1", "Authorization": "Bearer trace-route-token"}
cases = [
    ("list-limit", "GET", "/api/traces?limit=5", True),
    ("list-invalid-limit", "GET", "/api/traces?limit=abc", True),
    ("detail", "GET", "/api/traces/" + trace_id, True),
    ("export", "GET", "/api/traces/" + trace_id + "/export.json", True),
    ("detail-missing", "GET", "/api/traces/doesnotexist", True),
    ("export-missing", "GET", "/api/traces/doesnotexist/export.json", True),
    ("put-not-allowed", "PUT", "/api/traces", True),
    ("post-not-found", "POST", "/api/traces", True),
    ("options-global", "OPTIONS", "/api/traces", True),
    ("no-token", "GET", "/api/traces", False),
]
results = []
for name, method, uri, token in cases:
    request_headers = {"Host": "127.0.0.1"}
    if token:
        request_headers["Authorization"] = "Bearer trace-route-token"
    try:
        response = client.request(method, uri, headers=request_headers)
        results.append({"name": name, "uri": uri, "method": method, "token": token,
                        "status": response.status_code, "body": response.text,
                        "allow": response.headers.get("allow", ""),
                        "acam": response.headers.get("access-control-allow-methods", ""),
                        "contentDisposition": response.headers.get("content-disposition", "")})
    except Exception as error:
        results.append({"name": name, "uri": uri, "method": method, "token": token,
                        "status": 599, "body": type(error).__name__, "allow": "", "acam": "",
                        "contentDisposition": ""})
client.close()
print(json.dumps({"traceId": trace_id, "results": results}))
"#;
    let child = Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env("AUTH_TOKEN", TEST_TOKEN)
        .env("TRACE_ENABLED", "1")
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("python");
    let output = child.wait_with_output().expect("oracle");
    assert!(
        output.status.success(),
        "oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("oracle json")
}

fn parse_body(bytes: &[u8]) -> Value {
    if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(bytes).unwrap_or(Value::Null)
    }
}

fn strip_export_timestamp(value: &mut Value) {
    if let Some(export) = value.get_mut("_export") {
        if let Some(object) = export.as_object_mut() {
            object.insert("exportedAt".to_string(), json!("<normalised>"));
        }
    }
}

fn is_iso_z(value: &Value) -> bool {
    let Some(text) = value.as_str() else {
        return false;
    };
    // 2026-11-07T09:51:25.281675Z or without microseconds.
    text.len() >= 21 && text.ends_with('Z') && text.as_bytes().get(10) == Some(&b'T')
}

#[tokio::test]
async fn trace_routes_match_the_oracle_without_writing_the_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.root.path().to_path_buf();
    let expected = oracle(&root);
    let trace_id = expected["traceId"].as_str().expect("trace id").to_string();
    drop_wal_side_files(&root);
    let baseline = tree(&root);

    for want in expected["results"].as_array().expect("oracle results") {
        let name = want["name"].as_str().expect("case name");
        let uri = want["uri"].as_str().expect("uri").to_string();
        let method = want["method"].as_str().expect("method");
        let token = want["token"].as_bool().expect("token");
        let (status, body, allow, access_control_methods, content_disposition) =
            fixture.request(method, &uri, token).await;
        assert_eq!(
            status.as_u16(),
            want["status"].as_u64().expect("oracle status") as u16,
            "{name}"
        );
        let want_body: Value =
            serde_json::from_str(want["body"].as_str().unwrap_or_default()).unwrap_or(Value::Null);
        let mut native_body = parse_body(&body);
        if name == "export" {
            let native_exported_at = native_body["_export"]["exportedAt"].clone();
            let mut want_export = want_body.clone();
            strip_export_timestamp(&mut want_export);
            strip_export_timestamp(&mut native_body);
            assert_eq!(native_body, want_export, "{name}");
            assert!(is_iso_z(&native_exported_at), "export timestamp shape");
            assert_eq!(
                content_disposition.as_deref().unwrap_or(""),
                want["contentDisposition"].as_str().unwrap_or(""),
                "{name}: content disposition"
            );
        } else {
            assert_eq!(native_body, want_body, "{name}");
        }
        assert_eq!(
            allow.as_deref().unwrap_or(""),
            want["allow"].as_str().unwrap_or(""),
            "{name}: allow"
        );
        if name == "options-global" {
            assert_eq!(status, StatusCode::NO_CONTENT, "{name}: status");
            assert_eq!(
                access_control_methods.as_deref().unwrap_or(""),
                want["acam"].as_str().unwrap_or(""),
                "{name}: access-control-allow-methods"
            );
        }
    }

    let list = parse_body(&fixture.request("GET", "/api/traces?limit=5", true).await.1);
    assert_eq!(
        list["traces"][0]["traceId"].as_str(),
        Some(trace_id.as_str()),
        "newest trace first"
    );

    // A read-only native request must not create unrelated workspace files; the
    // SQLite WAL companion files are the only tolerated read-side artifacts.
    let final_tree: Vec<String> = tree(&root)
        .into_iter()
        .filter(|path| {
            !path.ends_with("traces.sqlite3-shm") && !path.ends_with("traces.sqlite3-wal")
        })
        .collect();
    let baseline_tree: Vec<String> = baseline
        .into_iter()
        .filter(|path| {
            !path.ends_with("traces.sqlite3-shm") && !path.ends_with("traces.sqlite3-wal")
        })
        .collect();
    assert_eq!(
        final_tree, baseline_tree,
        "native trace reads changed the workspace"
    );
}
