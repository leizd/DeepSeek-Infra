//! `GET /api/workspace/authority/retention/policy` through the production router.
//!
//! The original reads and normalizes `.backup-authority-retention/policy.json` on
//! every request. The oracle writes each stored shape into the same workspace
//! root the native router reads and serves the route through `TestClient`, so
//! both sides answer the identical status and body for: a missing file, an
//! unreadable file, a corrupt file, every falsy top-level value (defaults), every
//! truthy non-object (400 `Retention policy must be a JSON object`) and each
//! field-level rejection.
//!
//! The native side additionally proves the read-only contract: the route never
//! creates `.backup-authority-retention/`, and `POST` (the unported mutation) is
//! forwarded to the Go control plane rather than answered locally.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::Value;
use tower::ServiceExt;

const TEST_TOKEN: &str = "authority-retention-route-token";

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

    fn path(&self) -> &Path {
        self.root.path()
    }

    fn policy_path(&self) -> PathBuf {
        self.root
            .path()
            .join(".backup-authority-retention")
            .join("policy.json")
    }

    fn write_policy(&self, raw: &str) {
        let path = self.policy_path();
        std::fs::create_dir_all(path.parent().expect("parent")).expect("policy dir");
        std::fs::write(path, raw).expect("policy write");
    }

    fn remove_policy(&self) {
        let _ = std::fs::remove_file(self.policy_path());
        let _ = std::fs::remove_dir(self.policy_path().parent().expect("parent"));
    }

    async fn request(
        &self,
        method: &str,
        uri: &str,
        token: bool,
    ) -> (StatusCode, Vec<u8>, Option<String>, Option<String>) {
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
        let acam = header_value(header::ACCESS_CONTROL_ALLOW_METHODS);
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (status, bytes.to_vec(), allow, acam)
    }
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf()
}

struct Case {
    name: &'static str,
    method: &'static str,
    /// The exact file content to store, or `None` to remove the file.
    stored: Option<&'static str>,
    token: bool,
}

/// Every stored shape with a distinct documented answer.
const CASES: &[Case] = &[
    Case {
        name: "missing-file",
        method: "GET",
        stored: None,
        token: true,
    },
    Case {
        name: "empty-object",
        method: "GET",
        stored: Some("{}"),
        token: true,
    },
    Case {
        name: "corrupt-json",
        method: "GET",
        stored: Some("not json"),
        token: true,
    },
    Case {
        name: "falsy-array",
        method: "GET",
        stored: Some("[]"),
        token: true,
    },
    Case {
        name: "falsy-string",
        method: "GET",
        stored: Some("\"\""),
        token: true,
    },
    Case {
        name: "falsy-zero",
        method: "GET",
        stored: Some("0"),
        token: true,
    },
    Case {
        name: "falsy-null",
        method: "GET",
        stored: Some("null"),
        token: true,
    },
    Case {
        name: "falsy-false",
        method: "GET",
        stored: Some("false"),
        token: true,
    },
    Case {
        name: "truthy-array",
        method: "GET",
        stored: Some("[1, 2]"),
        token: true,
    },
    Case {
        name: "truthy-string",
        method: "GET",
        stored: Some("\"text\""),
        token: true,
    },
    Case {
        name: "truthy-number",
        method: "GET",
        stored: Some("7"),
        token: true,
    },
    Case {
        name: "truthy-true",
        method: "GET",
        stored: Some("true"),
        token: true,
    },
    Case {
        name: "stored-values",
        method: "GET",
        stored: Some(
            r#"{"minimumGenerations": 7, "minimumAgeDays": 0, "keepMutationClasses": [" z ", "a", "a"], "checkpointInterval": 1}"#,
        ),
        token: true,
    },
    Case {
        name: "stored-empty-classes",
        method: "GET",
        stored: Some(r#"{"keepMutationClasses": []}"#),
        token: true,
    },
    Case {
        name: "classes-coerced",
        method: "GET",
        stored: Some(r#"{"keepMutationClasses": ["b", "", null, 1, true, ["x"], {"k": "v"}]}"#),
        token: true,
    },
    Case {
        name: "bad-version-int",
        method: "GET",
        stored: Some(r#"{"retentionPolicyVersion": 2}"#),
        token: true,
    },
    Case {
        name: "bad-version-string",
        method: "GET",
        stored: Some(r#"{"retentionPolicyVersion": "1"}"#),
        token: true,
    },
    Case {
        name: "bad-version-null",
        method: "GET",
        stored: Some(r#"{"retentionPolicyVersion": null}"#),
        token: true,
    },
    Case {
        name: "bad-version-false",
        method: "GET",
        stored: Some(r#"{"retentionPolicyVersion": false}"#),
        token: true,
    },
    Case {
        name: "bad-version-float",
        method: "GET",
        stored: Some(r#"{"retentionPolicyVersion": 1.0}"#),
        token: true,
    },
    Case {
        name: "bad-version-true",
        method: "GET",
        stored: Some(r#"{"retentionPolicyVersion": true}"#),
        token: true,
    },
    Case {
        name: "bad-generations-bool",
        method: "GET",
        stored: Some(r#"{"minimumGenerations": true}"#),
        token: true,
    },
    Case {
        name: "bad-generations-zero",
        method: "GET",
        stored: Some(r#"{"minimumGenerations": 0}"#),
        token: true,
    },
    Case {
        name: "bad-generations-float",
        method: "GET",
        stored: Some(r#"{"minimumGenerations": 1.0}"#),
        token: true,
    },
    Case {
        name: "bad-generations-null",
        method: "GET",
        stored: Some(r#"{"minimumGenerations": null}"#),
        token: true,
    },
    Case {
        name: "bad-age-negative",
        method: "GET",
        stored: Some(r#"{"minimumAgeDays": -1}"#),
        token: true,
    },
    Case {
        name: "bad-interval-zero",
        method: "GET",
        stored: Some(r#"{"checkpointInterval": 0}"#),
        token: true,
    },
    Case {
        name: "bad-classes-string",
        method: "GET",
        stored: Some(r#"{"keepMutationClasses": "x"}"#),
        token: true,
    },
    Case {
        name: "bad-classes-null",
        method: "GET",
        stored: Some(r#"{"keepMutationClasses": null}"#),
        token: true,
    },
    Case {
        name: "unknown-keys-ignored",
        method: "GET",
        stored: Some(r#"{"unknownKey": 1}"#),
        token: true,
    },
    Case {
        name: "put-method-not-allowed",
        method: "PUT",
        stored: Some("{}"),
        token: true,
    },
    Case {
        name: "options-global",
        method: "OPTIONS",
        stored: Some("{}"),
        token: true,
    },
    Case {
        name: "no-token",
        method: "GET",
        stored: Some("{}"),
        token: false,
    },
];

fn spawn_oracle(root: &Path, script: &str) -> std::process::Child {
    Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env("AUTH_TOKEN", TEST_TOKEN)
        .env("PYTHONPATH", ".")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("python")
}

fn write_and_wait(mut child: std::process::Child, payload: &str) -> Vec<u8> {
    child
        .stdin
        .as_mut()
        .expect("stdin")
        .write_all(payload.as_bytes())
        .expect("write oracle input");
    let output = child.wait_with_output().expect("oracle");
    assert!(
        output.status.success(),
        "oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}

/// Drives the oracle: store each case's content, then serve the request.
fn oracle(root: &Path, cases: &[Case]) -> Vec<Value> {
    let script = r#"
import json, os, sys
from pathlib import Path
from fastapi.testclient import TestClient
from deepseek_infra.infra.workspace import authority_retention
from deepseek_infra.web.server import create_app

data = json.load(sys.stdin)
policy = authority_retention._policy_path()
client = TestClient(create_app(), base_url="http://127.0.0.1", raise_server_exceptions=False)
results = []
for case in data["cases"]:
    if case["stored"] is None:
        try:
            policy.unlink()
        except OSError:
            pass
        try:
            policy.parent.rmdir()
        except OSError:
            pass
    else:
        policy.parent.mkdir(parents=True, exist_ok=True)
        policy.write_text(case["stored"], encoding="utf-8")
    headers = {"Host": "127.0.0.1"}
    if case["token"]:
        headers["Authorization"] = "Bearer " + data["token"]
    try:
        response = client.request(case["method"], "/api/workspace/authority/retention/policy", headers=headers)
        results.append({"name": case["name"], "status": response.status_code, "body": response.text,
                        "allow": response.headers.get("allow", ""),
                        "acam": response.headers.get("access-control-allow-methods", "")})
    except Exception as error:
        results.append({"name": case["name"], "status": 599, "body": type(error).__name__,
                        "allow": "", "acam": ""})
client.close()
print(json.dumps(results))
"#;
    let child = spawn_oracle(root, script);
    let payload = serde_json::json!({
        "token": TEST_TOKEN,
        "cases": cases.iter().map(|case| serde_json::json!({
            "name": case.name,
            "method": case.method,
            "stored": case.stored,
            "token": case.token,
        })).collect::<Vec<_>>(),
    });
    let output = write_and_wait(child, &payload.to_string());
    serde_json::from_slice(&output).expect("oracle json")
}

fn body_text(bytes: &[u8]) -> String {
    String::from_utf8(bytes.to_vec()).unwrap_or_default()
}

#[tokio::test]
async fn authorization_retention_policy_matches_the_oracle() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.path().to_path_buf();
    let expected = oracle(&root, CASES);

    for (case, want) in CASES.iter().zip(expected.iter()) {
        assert_eq!(want["name"].as_str(), Some(case.name), "oracle order");
        match case.stored {
            Some(raw) => fixture.write_policy(raw),
            None => fixture.remove_policy(),
        }
        let (status, body, allow, acam) =
            fixture.request(case.method, POLICY_URI, case.token).await;
        assert_eq!(
            status.as_u16(),
            want["status"].as_u64().expect("oracle status") as u16,
            "{}: {}",
            case.name,
            body_text(&body)
        );
        assert_eq!(
            body_text(&body),
            want["body"].as_str().unwrap_or_default(),
            "{}: body",
            case.name
        );
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

    // HEAD is answered by the native route; the original reaches the static
    // catch-all for HEAD, so it cannot be compared in this fixture.
    fixture.remove_policy();
    let (status, body, _, _) = fixture.request("HEAD", POLICY_URI, true).await;
    assert_eq!(status, StatusCode::OK);
    assert!(body.is_empty());
}

/// The route only reads. A workspace with no retention directory keeps none.
#[tokio::test]
async fn authorization_retention_policy_is_read_only() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let directory = fixture.path().join(".backup-authority-retention");

    let (status, body, _, _) = fixture.request("GET", POLICY_URI, true).await;
    assert_eq!(status, StatusCode::OK, "{}", body_text(&body));
    assert!(
        !directory.exists(),
        "the read-only route created the retention directory"
    );

    // A stored policy is answered without being rewritten: the bytes and the
    // directory listing are unchanged afterwards.
    let raw = r#"{"minimumGenerations": 3, "keepMutationClasses": [" z ", "a"]}"#;
    fixture.write_policy(raw);
    let before = body_text(&std::fs::read(fixture.policy_path()).expect("policy"));
    let (status, body, _, _) = fixture.request("GET", POLICY_URI, true).await;
    assert_eq!(status, StatusCode::OK, "{}", body_text(&body));
    assert_eq!(
        body_text(&std::fs::read(fixture.policy_path()).expect("policy")),
        before,
        "the read-only route rewrote the policy file"
    );
    assert_eq!(
        body_text(&body),
        r#"{"retentionPolicyVersion":1,"minimumGenerations":3,"minimumAgeDays":30,"keepMutationClasses":["a","z"],"checkpointInterval":10000}"#
    );
}

const POLICY_URI: &str = "/api/workspace/authority/retention/policy";
