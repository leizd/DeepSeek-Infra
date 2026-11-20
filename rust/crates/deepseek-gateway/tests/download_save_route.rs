//! `POST /api/download-save` through the production router.
//!
//! The original copies an already-generated file into the user's Downloads
//! folder. Oracle and native requests run in separate home directories under the
//! same workspace root, so every response and every downloaded byte can be
//! compared without one side seeing the other's collisions.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "download-save-route-token";
const PPTX_ID: &str = "0123456789abcdef0123456789abcdef";
const MD_ID: &str = "fedcba9876543210fedcba9876543210";

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
    home: tempfile::TempDir,
    _static_root: tempfile::TempDir,
    _env: EnvGuard,
    app: axum::Router,
}
impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().expect("workspace");
        let home = tempfile::tempdir().expect("home");
        let static_root = tempfile::tempdir().expect("static");
        std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
        std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
        let env = EnvGuard::set(&[
            (
                "DEEPSEEK_INFRA_ROOT",
                root.path().to_str().expect("utf-8").to_string(),
            ),
            ("HOME", home.path().to_str().expect("utf-8").to_string()),
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
            home,
            _static_root: static_root,
            _env: env,
            app,
        }
    }

    fn seed_generated(&self) {
        let generated = self.root.path().join(".generated");
        std::fs::create_dir_all(&generated).unwrap();
        std::fs::write(generated.join(format!("{PPTX_ID}.pptx")), b"pptx-bytes").unwrap();
        std::fs::write(generated.join(format!("{MD_ID}.md")), b"md-bytes").unwrap();
    }

    async fn request(
        &self,
        method: &str,
        body: Option<&[u8]>,
        token: bool,
    ) -> (StatusCode, Vec<u8>, Option<String>, Option<String>) {
        let mut request = Request::builder()
            .method(method)
            .uri("/api/download-save")
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
        let header_value = |name: axum::http::HeaderName| {
            response
                .headers()
                .get(name)
                .and_then(|value| value.to_str().ok())
                .map(str::to_string)
        };
        let allow = header_value(header::ALLOW);
        let access_control_methods = header_value(header::ACCESS_CONTROL_ALLOW_METHODS);
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (status, bytes.to_vec(), allow, access_control_methods)
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

#[derive(Clone, Copy, PartialEq)]
enum Compare {
    Body,
    StatusAndCode,
    StatusOnly,
}

fn cases() -> Vec<Case> {
    vec![
        Case {
            name: "empty-name",
            method: "POST",
            body: Some(r#"{"id":"0123456789abcdef0123456789abcdef","filename":""}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "chinese-name",
            method: "POST",
            body: Some(r#"{"id":"0123456789abcdef0123456789abcdef","filename":"报告"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "doc-extension",
            method: "POST",
            body: Some(r#"{"id":"0123456789abcdef0123456789abcdef","filename":"报告.pptx"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "download-prefix",
            method: "POST",
            body: Some(r#"{"id":"0123456789abcdef0123456789abcdef","filename":"点击下载 幻灯片"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "cleaned-characters",
            method: "POST",
            body: Some(r#"{"id":"0123456789abcdef0123456789abcdef","filename":"a/b:c*d?"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "markdown-file",
            method: "POST",
            body: Some(r#"{"id":"fedcba9876543210fedcba9876543210","filename":"notes"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "unknown-id",
            method: "POST",
            body: Some(r#"{"id":"ffffffffffffffffffffffffffffffff","filename":"x"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "invalid-id",
            method: "POST",
            body: Some(r#"{"id":"not-an-id","filename":"x"}"#),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "empty-body",
            method: "POST",
            body: None,
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
            name: "get-not-found",
            method: "GET",
            body: None,
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "put-method-not-allowed",
            method: "PUT",
            body: Some("{}"),
            token: true,
            compare: Compare::Body,
        },
        Case {
            name: "options-global",
            method: "OPTIONS",
            body: None,
            token: true,
            compare: Compare::StatusOnly,
        },
        Case {
            name: "no-token",
            method: "POST",
            body: Some(r#"{"id":"0123456789abcdef0123456789abcdef","filename":"x"}"#),
            token: false,
            compare: Compare::Body,
        },
    ]
}

fn oracle(root: &Path, home: &Path, cases: &[Case]) -> Vec<Value> {
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
        response = client.request(case["method"], "/api/download-save", headers=headers, content=content)
        results.append({"name": case["name"], "status": response.status_code, "body": response.text,
                        "allow": response.headers.get("allow", ""),
                        "acam": response.headers.get("access-control-allow-methods", "")})
    except Exception as error:
        results.append({"name": case["name"], "status": 599, "body": type(error).__name__, "allow": "", "acam": ""})
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
        .env("HOME", home)
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

fn relative_download_path(body: &Value, home: &Path) -> Option<String> {
    let raw = body["path"].as_str()?;
    let path = Path::new(raw).strip_prefix(home).ok()?;
    Some(path.to_string_lossy().replace('\\', "/"))
}

#[tokio::test]
async fn download_save_matches_the_oracle_and_writes_only_the_downloads_folder() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    fixture.seed_generated();
    let root = fixture.root.path().to_path_buf();
    let oracle_home = tempfile::tempdir().expect("oracle home");
    let all_cases = cases();
    let expected = oracle(&root, oracle_home.path(), &all_cases);
    // The Python oracle's own startup initializes stores under the shared root;
    // that is the baseline the native requests must not change.
    let root_baseline = tree(&root);

    let mut expected_downloads: Vec<String> = Vec::new();
    for (case, want) in all_cases.iter().zip(expected.iter()) {
        assert_eq!(want["name"].as_str(), Some(case.name), "oracle order");
        let body = case.body.map(|text| text.as_bytes());
        let (status, native_body, allow, acam) =
            fixture.request(case.method, body, case.token).await;
        assert_eq!(
            status.as_u16(),
            want["status"].as_u64().expect("oracle status") as u16,
            "{}",
            case.name
        );
        let want_body: Value =
            serde_json::from_str(want["body"].as_str().unwrap_or_default()).unwrap_or(Value::Null);
        match case.compare {
            Compare::StatusOnly => {
                if case.method == "OPTIONS" {
                    assert_eq!(
                        acam.as_deref().unwrap_or(""),
                        want["acam"].as_str().unwrap_or(""),
                        "{}: access-control-allow-methods",
                        case.name
                    );
                }
            }
            Compare::StatusAndCode => {
                let native = parse_body(&native_body);
                assert_eq!(native["code"], want_body["code"], "{}", case.name);
            }
            Compare::Body => {
                let native = parse_body(&native_body);
                if want["status"].as_u64() == Some(200) {
                    assert_eq!(native["filename"], want_body["filename"], "{}", case.name);
                    assert_eq!(
                        native["downloadUrl"], want_body["downloadUrl"],
                        "{}",
                        case.name
                    );
                    assert_eq!(
                        relative_download_path(&native, fixture.home.path()),
                        relative_download_path(&want_body, oracle_home.path()),
                        "{}: path",
                        case.name
                    );
                    expected_downloads
                        .push(relative_download_path(&native, fixture.home.path()).expect("path"));
                    let downloaded = fixture
                        .home
                        .path()
                        .join(relative_download_path(&native, fixture.home.path()).expect("path"));
                    let source = root
                        .join(".generated")
                        .join(if case.name == "markdown-file" {
                            format!("{MD_ID}.md")
                        } else {
                            format!("{PPTX_ID}.pptx")
                        });
                    assert_eq!(
                        std::fs::read(&downloaded).expect("downloaded bytes"),
                        std::fs::read(&source).expect("source bytes"),
                        "{}: bytes",
                        case.name
                    );
                } else {
                    assert_eq!(native, want_body, "{}", case.name);
                }
                assert_eq!(
                    allow.as_deref().unwrap_or(""),
                    want["allow"].as_str().unwrap_or(""),
                    "{}: allow",
                    case.name
                );
            }
        }
    }

    let mut native_tree = vec!["Downloads".to_string()];
    let mut sorted = expected_downloads.clone();
    sorted.sort();
    let mut unique = Vec::new();
    for path in sorted {
        if unique.last() != Some(&path) {
            unique.push(path);
        }
    }
    for path in unique {
        native_tree.push(path);
    }
    native_tree.sort();
    assert_eq!(tree(fixture.home.path()), native_tree, "Downloads content");
    assert_eq!(
        tree(&root),
        root_baseline,
        "native requests changed the workspace"
    );
}
