//! `GET /api/workspace/resilience/federation` through the production router.
//!
//! The body is `build_federation_snapshot` with the route's fixed wire list.
//! The call does not create a store.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "resilience-federation-route-token";

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

    async fn request(&self, method: &str, uri: &str, auth: bool) -> (StatusCode, Value, Vec<u8>) {
        let mut request = Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, "127.0.0.1");
        if auth {
            request = request.header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"));
        }
        let response = self
            .app
            .clone()
            .oneshot(request.body(Body::empty()).unwrap())
            .await
            .unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        let body = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        (status, body, bytes.to_vec())
    }
}

fn repo_root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("repo root")
        .to_path_buf()
}

fn oracle(root: &std::path::Path, plan: &Value) -> Value {
    let script = r#"
import json, sys
from deepseek_infra.infra.workspace.resilience_federation_readiness import (
    build_federation_snapshot,
    _snapshot_digest,
)
plan = json.loads(sys.stdin.read())
builds = []
for fleet_id in plan["builds"]:
    try:
        body = build_federation_snapshot(
            fleet_id=fleet_id,
            wire_compatibility=["object-set-v1", "receipt-v4", "commit-v4", "fastcdc-v3"],
            available_failure_domains=[],
            forecast_headroom=None,
            cost_class="unknown",
            readiness="UNKNOWN",
        )
        builds.append({"status": 200, "body": body})
    except ValueError:
        builds.append({"status": 500, "body": {"error": "Server error", "code": "internal"}})
checks = [_snapshot_digest(body) == body.get("snapshotDigest") for body in plan["checks"]]
print(json.dumps({"builds": builds, "checks": checks}))
"#;
    let mut child = std::process::Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env("PYTHONUTF8", "1")
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
            .write_all(&serde_json::to_vec(plan).unwrap())
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

fn stable(value: &Value) -> Value {
    let mut copy = value.clone();
    if let Some(object) = copy.as_object_mut() {
        object.remove("generatedAt");
        object.remove("snapshotDigest");
    }
    copy
}

fn assert_timestamp(body: &Value) {
    let generated = body["generatedAt"].as_str().unwrap_or("");
    assert!(
        generated.len() == 20
            && generated.ends_with('Z')
            && generated.as_bytes().get(10) == Some(&b'T'),
        "generatedAt={generated}"
    );
    let digest = body["snapshotDigest"].as_str().unwrap_or("");
    assert!(
        digest.len() == 64 && digest.bytes().all(|byte| byte.is_ascii_hexdigit()),
        "digest={digest}"
    );
}

#[tokio::test]
async fn federation_snapshot_matches_the_pure_builder_without_creating_a_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.root.path();

    let (status, body, _) = fixture
        .request("GET", "/api/workspace/resilience/federation", false)
        .await;
    assert_eq!(status, StatusCode::UNAUTHORIZED);
    assert_eq!(
        body,
        json!({"error": {"code": "UNAUTHORIZED", "message": "Auth required"}})
    );

    let oracle_plan = json!({
        "builds": ["local", "east-1", "0", "Local ", "东区", "   "],
        "checks": []
    });
    let expected = oracle(root, &oracle_plan);
    let builds = expected["builds"].as_array().expect("builds");

    let cases = [
        ("/api/workspace/resilience/federation", 0usize),
        ("/api/workspace/resilience/federation?fleetId=", 0),
        ("/api/workspace/resilience/federation?other=1", 0),
        ("/api/workspace/resilience/federation?fleetid=other", 0),
        ("/api/workspace/resilience/federation?fleetId=east-1", 1),
        (
            "/api/workspace/resilience/federation?fleetId=first&fleetId=second",
            1,
        ),
        ("/api/workspace/resilience/federation?fleetId=0", 2),
        ("/api/workspace/resilience/federation?fleetId=Local%20", 3),
        (
            "/api/workspace/resilience/federation?fleetId=%E4%B8%9C%E5%8C%BA",
            4,
        ),
        ("/api/workspace/resilience/federation?fleetId=%20%20%20", 5),
    ];
    // The second east-1 request is "first", not the oracle's "east-1".
    // Rebuild that one against the oracle value the handler actually passes.
    let first_oracle = oracle(root, &json!({"builds": ["first"], "checks": []}));
    let mut http_bodies = Vec::new();
    for (uri, index) in cases {
        let (status, body, _) = fixture.request("GET", uri, true).await;
        let expected_body = if uri.contains("fleetId=first") {
            &first_oracle["builds"][0]
        } else {
            &builds[index]
        };
        let expected_status =
            StatusCode::from_u16(expected_body["status"].as_u64().unwrap() as u16).unwrap();
        assert_eq!(
            status, expected_status,
            "uri={uri} body={body} expected={expected_body}"
        );
        if status == StatusCode::OK {
            assert_eq!(stable(&body), stable(&expected_body["body"]), "uri={uri}");
            assert_timestamp(&body);
            http_bodies.push(body);
        } else {
            assert_eq!(body, expected_body["body"], "uri={uri}");
        }
    }

    let checked = oracle(root, &json!({"builds": [], "checks": http_bodies}));
    let checks = checked["checks"].as_array().expect("checks");
    assert!(
        checks.iter().all(|item| item.as_bool() == Some(true)),
        "checks={checks:?}"
    );

    let (status, _, raw) = fixture
        .request("HEAD", "/api/workspace/resilience/federation", true)
        .await;
    assert_eq!(status, StatusCode::OK);
    assert!(raw.is_empty());
    let (status, head_body, raw) = fixture
        .request(
            "HEAD",
            "/api/workspace/resilience/federation?fleetId=%20",
            true,
        )
        .await;
    assert_eq!(status, StatusCode::INTERNAL_SERVER_ERROR);
    assert!(raw.is_empty(), "HEAD error body={head_body}");

    let (status, body, _) = fixture
        .request("POST", "/api/workspace/resilience/federation", true)
        .await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED);
    assert_eq!(body, json!({"detail": "Method Not Allowed"}));

    let (status, body, _) = fixture
        .request("GET", "/api/workspace/resilience/journal", true)
        .await;
    assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(body["error"]["code"], "GO_CONTROL_PROXY_NOT_READY");
    assert!(!root.join(".resilience-journal").exists());
    assert_eq!(std::fs::read_dir(root).unwrap().count(), 0);
}
