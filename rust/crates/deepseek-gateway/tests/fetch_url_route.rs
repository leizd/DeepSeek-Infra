//! `POST /api/fetch-url` through the production router.
//!
//! The SSRF/URL refusals and the cache-hit success envelope are compared with a
//! fresh Python application under the identical workspace root. The cache is
//! seeded by both sides' real cache writer, so the success path needs no network.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use deepseek_policy::fetch_url::{fetch_url_cache_path, save_fetch_url_cache};
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "fetch-url-route-token";
const CACHED_URL: &str = "https://example.com/cached-page";

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

    fn seed_cache(&self) {
        let result = json!({
            "url": CACHED_URL,
            "contentType": "text/html",
            "text": "Cached page text.",
            "charCount": 17,
        });
        save_fetch_url_cache(
            self.root.path(),
            CACHED_URL,
            &result,
            deepseek_policy::fetch_url::system_now(),
        )
        .expect("seed cache");
    }

    fn expected_cache_relative(&self) -> String {
        fetch_url_cache_path(self.root.path(), CACHED_URL)
            .strip_prefix(self.root.path())
            .expect("cache under root")
            .to_string_lossy()
            .replace('\\', "/")
    }

    async fn request(
        &self,
        method: &str,
        body: Option<&[u8]>,
        token: bool,
    ) -> (StatusCode, Vec<u8>, String, Option<String>, String, String) {
        let mut request = Request::builder()
            .method(method)
            .uri("/api/fetch-url")
            .header(header::HOST, "127.0.0.1");
        if token {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        if let Some(bytes) = body {
            request = request
                .header(header::CONTENT_TYPE, "application/json")
                .header(header::CONTENT_LENGTH, bytes.len().to_string());
        }
        let response = self
            .app
            .clone()
            .oneshot(
                request
                    .body(Body::from(body.unwrap_or_default().to_vec()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let status = response.status();
        let allow = response
            .headers()
            .get(header::ALLOW)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        let header_value = |name: axum::http::HeaderName| {
            response
                .headers()
                .get(name)
                .and_then(|value| value.to_str().ok())
                .unwrap_or("")
                .to_string()
        };
        let content_type = header_value(header::CONTENT_TYPE);
        let access_control_methods = header_value(header::ACCESS_CONTROL_ALLOW_METHODS);
        let access_control_headers = header_value(header::ACCESS_CONTROL_ALLOW_HEADERS);
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (
            status,
            bytes.to_vec(),
            content_type,
            allow,
            access_control_methods,
            access_control_headers,
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

struct Case {
    name: &'static str,
    method: &'static str,
    body: Option<&'static str>,
    token: bool,
    compare: Compare,
}

#[derive(Clone, Copy)]
enum Compare {
    Body,
    StatusAndCode,
    StatusOnly,
}

const OVERSIZED_PADDING: usize = 2_000_001;

fn cases() -> Vec<Case> {
    vec![
        Case {
            name: "cached-success",
            method: "POST",
            body: Some(r#"{"url":"https://example.com/cached-page"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "loopback",
            method: "POST",
            body: Some(r#"{"url":"http://127.0.0.1:9/"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "localhost",
            method: "POST",
            body: Some(r#"{"url":"http://localhost:8000/"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "local-suffix",
            method: "POST",
            body: Some(r#"{"url":"http://printer.local/"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "ftp",
            method: "POST",
            body: Some(r#"{"url":"ftp://example.com"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "credentials",
            method: "POST",
            body: Some(r#"{"url":"https://user:pass@example.com"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "bad-port",
            method: "POST",
            body: Some(r#"{"url":"https://example.com:abc/"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "metadata-ip",
            method: "POST",
            body: Some(r#"{"url":"http://169.254.169.254/"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "empty-url",
            method: "POST",
            body: Some(r#"{"url":""}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "numeric-url",
            method: "POST",
            body: Some(r#"{"url":42}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "not-object",
            method: "POST",
            body: Some("[]"),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "invalid-json",
            method: "POST",
            body: Some("{"),
            token: true,
            compare: Compare::StatusAndCode,
        },
        Case {
            name: "empty-body",
            method: "POST",
            body: None,
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "method-get",
            method: "GET",
            body: None,
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "method-put",
            method: "PUT",
            body: Some("{}"),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "method-options",
            method: "OPTIONS",
            body: None,
            token: true,
            compare: Compare::StatusOnly,
        },
        Case {
            name: "unauthorized-post",
            method: "POST",
            body: Some(r#"{"url":"https://example.com/"}"#),
            token: false,
            compare: Compare::Body,
        },
    ]
}

struct OracleCase<'a> {
    name: &'static str,
    method: &'static str,
    body: Option<&'a str>,
    token: bool,
}

fn oracle(root: &Path, cases: &[OracleCase<'_>]) -> Vec<Value> {
    let script = r#"
import json, sys
from fastapi.testclient import TestClient
from deepseek_infra.web.server import create_app
data = json.load(sys.stdin)
client = TestClient(create_app(), base_url="http://127.0.0.1", raise_server_exceptions=False)
results = []
for case in data["cases"]:
    headers = {"Host": "127.0.0.1"}
    if case["token"]:
        headers["Authorization"] = "Bearer " + data["token"]
    content = case["body"]
    if content is not None:
        headers["Content-Type"] = "application/json"
    try:
        response = client.request(case["method"], "/api/fetch-url", headers=headers, content=content)
        results.append({"name": case["name"], "status": response.status_code, "body": response.text,
                        "allow": response.headers.get("allow", ""),
                        "contentType": response.headers.get("content-type", ""),
                        "acam": response.headers.get("access-control-allow-methods", ""),
                        "acah": response.headers.get("access-control-allow-headers", "")})
    except Exception as error:
        results.append({"name": case["name"], "status": 599, "body": type(error).__name__, "allow": "", "contentType": "", "acam": "", "acah": ""})
client.close()
print(json.dumps(results))
"#;
    let payload = json!({
        "token": TEST_TOKEN,
        "cases": cases.iter().map(|case| json!({
            "name": case.name,
            "method": case.method,
            "body": case.body,
            "token": case.token,
        })).collect::<Vec<_>>(),
    });
    let mut child = Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env("AUTH_TOKEN", TEST_TOKEN)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("python");
    child
        .stdin
        .as_mut()
        .expect("stdin")
        .write_all(payload.to_string().as_bytes())
        .expect("write oracle input");
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

#[tokio::test]
async fn fetch_url_route_matches_the_oracle_without_network_access() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    fixture.seed_cache();
    let expected_cache = fixture.expected_cache_relative();
    let root = fixture.root.path().to_path_buf();
    let all_cases = cases();
    let oversized_body = format!(
        r#"{{"url":"https://example.com/","pad":"{}"}}"#,
        "x".repeat(OVERSIZED_PADDING)
    );
    let oracle_cases: Vec<OracleCase<'_>> = all_cases
        .iter()
        .map(|case| OracleCase {
            name: case.name,
            method: case.method,
            body: case.body,
            token: case.token,
        })
        .collect();
    let expected = oracle(&root, &oracle_cases);

    for (case, want) in all_cases.iter().zip(expected.iter()) {
        assert_eq!(want["name"].as_str(), Some(case.name), "oracle order");
        let body = case.body.map(|text| text.as_bytes());
        let (status, native_body, content_type, allow, acam, acah) =
            fixture.request(case.method, body, case.token).await;
        let want_status = want["status"].as_u64().expect("oracle status") as u16;
        assert_eq!(status.as_u16(), want_status, "{}", case.name);
        match case.compare {
            Compare::StatusOnly => {
                if case.method == "OPTIONS" {
                    assert_eq!(
                        acam,
                        want["acam"].as_str().unwrap_or(""),
                        "{}: access-control-allow-methods",
                        case.name
                    );
                    assert_eq!(
                        acah,
                        want["acah"].as_str().unwrap_or(""),
                        "{}: access-control-allow-headers",
                        case.name
                    );
                }
            }
            Compare::StatusAndCode => {
                let want_body: Value =
                    serde_json::from_str(want["body"].as_str().unwrap_or_default())
                        .unwrap_or(Value::Null);
                let native = parse_body(&native_body);
                assert_eq!(native["code"], want_body["code"], "{}", case.name);
            }
            Compare::Body => {
                let want_body: Value =
                    serde_json::from_str(want["body"].as_str().unwrap_or_default())
                        .unwrap_or(Value::Null);
                assert_eq!(parse_body(&native_body), want_body, "{}", case.name);
                assert!(
                    content_type.contains("application/json"),
                    "{}: content type {content_type}",
                    case.name
                );
                let want_allow = want["allow"].as_str().unwrap_or("");
                assert_eq!(
                    allow.as_deref().unwrap_or(""),
                    want_allow,
                    "{}: allow",
                    case.name
                );
            }
        }
    }

    // Oversized bodies are refused from the declared Content-Length exactly like
    // the oracle, before any parsing.
    let oversized = oracle(
        &root,
        &[OracleCase {
            name: "oversized",
            method: "POST",
            body: Some(oversized_body.as_str()),
            token: true,
        }],
    );
    assert_eq!(oversized[0]["status"], json!(413));
    let (status, body, _, _, _, _) = fixture
        .request("POST", Some(oversized_body.as_bytes()), true)
        .await;
    assert_eq!(status, StatusCode::PAYLOAD_TOO_LARGE);
    assert_eq!(parse_body(&body)["code"], "upload_too_large");

    assert_eq!(
        tree(&root),
        vec![expected_cache],
        "the probe created or removed files"
    );
}
