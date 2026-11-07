//! Public-route authentication semantics through the production router.
//!
//! The original routes call `require_api_auth`: a caller's Host is checked
//! against the allow-list *before* the token, and both refusals are flat
//! `{"error": ..., "code": ...}` documents. Every case below is compared with a
//! fresh Python oracle run under the identical environment.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::Router;
use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "auth-semantics-route-token";

const ENV_KEYS: &[&str] = &[
    "AUTH_DISABLED",
    "AUTH_ALLOWED_HOSTS",
    "HOST",
    "DEEPSEEK_RUNTIME_MODE",
    "GO_CONTROL_ADDR",
    "DEEPSEEK_GO_CONTROL_URL",
];

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
    app: Router,
}
impl Fixture {
    fn new() -> Self {
        let root = tempfile::tempdir().expect("workspace");
        let static_root = tempfile::tempdir().expect("static");
        std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
        std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
        let mut pairs = vec![
            (
                "DEEPSEEK_INFRA_ROOT",
                root.path().to_str().expect("utf-8").to_string(),
            ),
            ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ];
        for key in ENV_KEYS {
            pairs.push((*key, String::new()));
        }
        let env = EnvGuard::set(&pairs);
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
        host: &str,
        token: bool,
    ) -> (StatusCode, Vec<u8>, String) {
        let mut request = Request::builder().method(method).uri(uri);
        if !host.is_empty() {
            request = request.header(header::HOST, host);
        }
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
        let content_type = response
            .headers()
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .unwrap_or("")
            .to_string();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (status, bytes.to_vec(), content_type)
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
    path: &'static str,
    host: &'static str,
    token: bool,
}

const CASES: &[Case] = &[
    Case {
        name: "status-no-token",
        method: "GET",
        path: "/api/rust/status",
        host: "127.0.0.1",
        token: false,
    },
    Case {
        name: "media-no-token",
        method: "GET",
        path: "/api/media",
        host: "127.0.0.1",
        token: false,
    },
    Case {
        name: "skills-no-token",
        method: "GET",
        path: "/api/skills",
        host: "127.0.0.1",
        token: false,
    },
    Case {
        name: "config-no-token",
        method: "GET",
        path: "/api/config",
        host: "127.0.0.1",
        token: false,
    },
    Case {
        name: "backup-policies-no-token",
        method: "GET",
        path: "/api/workspace/backup-policies",
        host: "127.0.0.1",
        token: false,
    },
    Case {
        name: "traces-no-token",
        method: "GET",
        path: "/api/traces",
        host: "127.0.0.1",
        token: false,
    },
    Case {
        name: "semantic-cache-no-token",
        method: "GET",
        path: "/api/semantic-cache/status",
        host: "127.0.0.1",
        token: false,
    },
    Case {
        name: "wrong-method-no-token",
        method: "POST",
        path: "/api/rust/status",
        host: "127.0.0.1",
        token: false,
    },
    Case {
        name: "mcp-no-token",
        method: "POST",
        path: "/mcp",
        host: "127.0.0.1",
        token: false,
    },
    Case {
        name: "a2a-no-token",
        method: "POST",
        path: "/a2a",
        host: "127.0.0.1",
        token: false,
    },
    Case {
        name: "v1-no-token",
        method: "POST",
        path: "/v1/chat/completions",
        host: "127.0.0.1",
        token: false,
    },
    Case {
        name: "localhost-host",
        method: "GET",
        path: "/api/rust/status",
        host: "localhost",
        token: false,
    },
    Case {
        name: "host-with-port",
        method: "GET",
        path: "/api/rust/status",
        host: "127.0.0.1:9999",
        token: false,
    },
    Case {
        name: "foreign-host-no-token",
        method: "GET",
        path: "/api/rust/status",
        host: "evil.invalid",
        token: false,
    },
    Case {
        name: "foreign-host-valid-token",
        method: "GET",
        path: "/api/rust/status",
        host: "evil.invalid",
        token: true,
    },
    Case {
        name: "foreign-host-media",
        method: "GET",
        path: "/api/media",
        host: "evil.invalid",
        token: true,
    },
    Case {
        name: "foreign-host-mcp",
        method: "POST",
        path: "/mcp",
        host: "evil.invalid",
        token: true,
    },
    Case {
        name: "local-ip-host",
        method: "GET",
        path: "/api/rust/status",
        host: "__LOCAL_IP__",
        token: false,
    },
    Case {
        name: "valid-token-status",
        method: "GET",
        path: "/api/rust/status",
        host: "127.0.0.1",
        token: true,
    },
    Case {
        name: "valid-token-config",
        method: "GET",
        path: "/api/config",
        host: "127.0.0.1",
        token: true,
    },
];

struct OracleResult {
    local_ip: String,
    results: Vec<Value>,
}

fn oracle(root: &Path, cases: &[Case]) -> OracleResult {
    let script = r#"
import json, sys
from fastapi.testclient import TestClient
from deepseek_infra.web.server import create_app
from deepseek_infra.core.utils import local_ip
data = json.load(sys.stdin)
client = TestClient(create_app(), base_url="http://127.0.0.1", raise_server_exceptions=False)
local = local_ip()
results = []
for case in data["cases"]:
    host = local if case["host"] == "__LOCAL_IP__" else case["host"]
    headers = {"Host": host}
    if case["token"]:
        headers["Authorization"] = "Bearer " + data["token"]
    try:
        response = client.request(case["method"], case["path"], headers=headers)
        results.append({"name": case["name"], "status": response.status_code, "body": response.text,
                        "contentType": response.headers.get("content-type", "")})
    except Exception as error:
        results.append({"name": case["name"], "status": 599, "body": type(error).__name__, "contentType": ""})
client.close()
print(json.dumps({"localIp": local, "results": results}))
"#;
    let payload = json!({
        "token": TEST_TOKEN,
        "cases": cases
            .iter()
            .map(|case| json!({
                "name": case.name,
                "method": case.method,
                "path": case.path,
                "host": case.host,
                "token": case.token,
            }))
            .collect::<Vec<_>>(),
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
    let parsed: Value = serde_json::from_slice(&output.stdout).expect("oracle json");
    OracleResult {
        local_ip: parsed["localIp"].as_str().unwrap_or_default().to_string(),
        results: parsed["results"]
            .as_array()
            .expect("oracle results")
            .clone(),
    }
}

fn parse_body(bytes: &[u8]) -> Value {
    if bytes.is_empty() {
        Value::Null
    } else {
        serde_json::from_slice(bytes).unwrap_or(Value::Null)
    }
}

#[tokio::test]
async fn public_auth_refusals_match_the_oracle_without_creating_a_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.root.path().to_path_buf();

    let expected = oracle(&root, CASES);
    assert!(
        !expected.local_ip.is_empty(),
        "oracle reported no LAN address"
    );
    // The Python application initializes request-queue, semantic-cache, trace
    // and workspace-fence stores under the shared root; that is the oracle's
    // startup shape, not a side effect of the native requests below.
    let baseline = tree(&root);

    for (case, want) in CASES.iter().zip(expected.results.iter()) {
        assert_eq!(want["name"].as_str(), Some(case.name), "oracle order");
        // `__LOCAL_IP__` is substituted by the oracle with its own detected LAN
        // address; the native edge must accept the same address.
        let host = if case.host == "__LOCAL_IP__" {
            expected.local_ip.as_str()
        } else {
            case.host
        };
        let (status, native_body, content_type) = fixture
            .request(case.method, case.path, host, case.token)
            .await;
        let want_status = want["status"].as_u64().expect("oracle status") as u16;
        assert_eq!(
            status.as_u16(),
            want_status,
            "{}: native {} vs oracle {}",
            case.name,
            status,
            want_status
        );
        if want_status == 200 {
            assert!(
                content_type.contains("application/json"),
                "{}: success content type",
                case.name
            );
            continue;
        }
        let want_body: Value =
            serde_json::from_str(want["body"].as_str().unwrap_or_default()).unwrap_or(Value::Null);
        assert_eq!(
            parse_body(&native_body),
            want_body,
            "{}: refusal body",
            case.name
        );
        assert!(
            content_type.contains("application/json"),
            "{}: refusal content type {content_type}",
            case.name
        );
    }

    assert_eq!(tree(&root), baseline, "native auth requests created files");
}
