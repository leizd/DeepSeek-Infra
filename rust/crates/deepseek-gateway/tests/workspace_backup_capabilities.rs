//! `GET /api/workspace/backup-target-capabilities` through the production router.
//!
//! The original route reports the serving runtime's target capabilities. To pin
//! the byte-exact body when the optional S3 SDK is present, the Python oracle
//! runs with a stub `boto3` module on `PYTHONPATH`; that is the deployment shape
//! where Python also advertises S3. The native runtime always advertises S3
//! because it ships a real S3 client.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "backup-target-capabilities-token";

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

    async fn request(
        &self,
        method: &str,
        token: bool,
    ) -> (StatusCode, Vec<u8>, Option<String>, Option<String>) {
        let mut request = Request::builder()
            .method(method)
            .uri("/api/workspace/backup-target-capabilities")
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
    token: bool,
}

const CASES: &[Case] = &[
    Case {
        name: "get",
        method: "GET",
        token: true,
    },
    Case {
        name: "put-method-not-allowed",
        method: "PUT",
        token: true,
    },
    Case {
        name: "options-global",
        method: "OPTIONS",
        token: true,
    },
    Case {
        name: "no-token",
        method: "GET",
        token: false,
    },
];

fn oracle(root: &Path, cases: &[Case]) -> Vec<Value> {
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
    try:
        response = client.request(case["method"], "/api/workspace/backup-target-capabilities", headers=headers)
        results.append({"name": case["name"], "status": response.status_code, "body": response.text,
                        "allow": response.headers.get("allow", ""),
                        "acam": response.headers.get("access-control-allow-methods", "")})
    except Exception as error:
        results.append({"name": case["name"], "status": 599, "body": type(error).__name__, "allow": "", "acam": ""})
client.close()
print(json.dumps(results))
"#;
    // Stub the optional S3 SDK for the oracle only: `s3_sdk_available()` then
    // reports true, matching the native runtime's real S3 client.
    let stub_dir = tempfile::tempdir().expect("stub dir");
    std::fs::write(stub_dir.path().join("boto3.py"), b"").expect("stub module");
    let payload = json!({
        "token": TEST_TOKEN,
        "cases": cases.iter().map(|case| json!({
            "name": case.name,
            "method": case.method,
            "token": case.token,
        })).collect::<Vec<_>>(),
    });
    let mut child = Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env("AUTH_TOKEN", TEST_TOKEN)
        .env(
            "PYTHONPATH",
            std::env::var("PYTHONPATH")
                .map(|existing| {
                    let mut paths = vec![stub_dir.path().to_path_buf()];
                    paths.extend(std::env::split_paths(&existing));
                    std::env::join_paths(paths).expect("joined paths")
                })
                .unwrap_or_else(|_| stub_dir.path().to_path_buf().into_os_string()),
        )
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
async fn backup_target_capabilities_match_the_s3_enabled_oracle() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.root.path().to_path_buf();
    let expected = oracle(&root, CASES);
    let baseline = tree(&root);

    for (case, want) in CASES.iter().zip(expected.iter()) {
        assert_eq!(want["name"].as_str(), Some(case.name), "oracle order");
        let (status, body, allow, acam) = fixture.request(case.method, case.token).await;
        assert_eq!(
            status.as_u16(),
            want["status"].as_u64().expect("oracle status") as u16,
            "{}",
            case.name
        );
        let want_body: Value =
            serde_json::from_str(want["body"].as_str().unwrap_or_default()).unwrap_or(Value::Null);
        assert_eq!(parse_body(&body), want_body, "{}", case.name);
        assert_eq!(
            allow.as_deref().unwrap_or(""),
            want["allow"].as_str().unwrap_or(""),
            "{}: allow",
            case.name
        );
        if case.method == "OPTIONS" {
            assert_eq!(
                acam.as_deref().unwrap_or(""),
                want["acam"].as_str().unwrap_or(""),
                "{}: access-control-allow-methods",
                case.name
            );
        }
    }

    // HEAD is answered by the native route without a body; the Python TestClient
    // route reaches the static catch-all and cannot be compared in this fixture.
    let (status, body, _, _) = fixture.request("HEAD", true).await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.is_empty());

    assert_eq!(
        tree(&root),
        baseline,
        "the capability probe created or removed files"
    );
}
