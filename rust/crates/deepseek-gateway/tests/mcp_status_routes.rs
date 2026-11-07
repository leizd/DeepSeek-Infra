//! `GET /api/mcp` through the production router.
//!
//! The body is `mcp_status()` from the original MCP hub. Every value comes from
//! the same environment knobs the oracle reads, so each case compares the native
//! response with a fresh Python oracle run under the identical environment.

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "mcp-status-route-token";

const MCP_KEYS: &[&str] = &[
    "MCP_ENABLED",
    "MCP_CAPABILITY",
    "MCP_EXPOSE_RESOURCES",
    "MCP_EXPOSE_PROMPTS",
    "MCP_CLIENT_ENABLED",
    "MCP_CLIENT_SERVERS",
    "MCP_CLIENT_TIMEOUT_SECONDS",
    "MCP_CLIENT_MAX_RETRIES",
    "MCP_CLIENT_RETRY_BACKOFF_SECONDS",
    "MCP_CLIENT_CIRCUIT_BREAKER_FAILURES",
    "MCP_CLIENT_CIRCUIT_BREAKER_RESET_SECONDS",
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
    app: axum::Router,
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
            ("DEEPSEEK_RUNTIME_MODE", String::new()),
            ("GO_CONTROL_ADDR", String::new()),
            ("DEEPSEEK_GO_CONTROL_URL", String::new()),
        ];
        for key in MCP_KEYS {
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
        auth: bool,
    ) -> (StatusCode, Value, Vec<u8>, Option<String>) {
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
        let allow = response
            .headers()
            .get(header::ALLOW)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        let body = if bytes.is_empty() {
            Value::Null
        } else {
            serde_json::from_slice(&bytes).unwrap_or(Value::Null)
        };
        (status, body, bytes.to_vec(), allow)
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

fn apply_mcp(overrides: &[(&str, &str)]) {
    for key in MCP_KEYS {
        unsafe { std::env::set_var(key, "") }
    }
    for (name, value) in overrides {
        unsafe { std::env::set_var(name, value) }
    }
}

fn oracle(root: &Path) -> Value {
    let script = r#"
import json
from deepseek_infra.infra.mcp.server import mcp_status
print(json.dumps({"status": 200, "body": {"ok": True, "mcp": mcp_status()}}))
"#;
    let output = Command::new("python")
        .arg("-c")
        .arg(script)
        .current_dir(repo_root())
        .env("DEEPSEEK_INFRA_ROOT", root)
        .env("AUTH_TOKEN", TEST_TOKEN)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .output()
        .expect("python");
    assert!(
        output.status.success(),
        "oracle failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).expect("oracle json")
}

async fn assert_oracle(fixture: &Fixture, label: &str) -> Value {
    let request = tokio::time::timeout(
        Duration::from_secs(30),
        fixture.request("GET", "/api/mcp", true),
    )
    .await
    .unwrap_or_else(|_| panic!("timed out: {label}"));
    let (status, body, _, _) = request;
    let root = fixture.root.path().to_path_buf();
    let expected = tokio::time::timeout(
        Duration::from_secs(30),
        tokio::task::spawn_blocking(move || oracle(&root)),
    )
    .await
    .unwrap_or_else(|_| panic!("oracle timed out: {label}"))
    .expect("oracle thread");
    assert_eq!(
        status.as_u16(),
        expected["status"].as_u64().unwrap() as u16,
        "{label}"
    );
    assert_eq!(&body, &expected["body"], "{label}");
    assert!(
        tree(fixture.root.path()).is_empty(),
        "{label} created {}",
        tree(fixture.root.path()).join(", ")
    );
    body
}

#[tokio::test]
async fn mcp_status_matches_the_oracle_without_creating_a_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.root.path();

    let (status, unauthenticated, _, _) = tokio::time::timeout(
        Duration::from_secs(30),
        fixture.request("GET", "/api/mcp", false),
    )
    .await
    .expect("timed out: unauth");
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"], "Auth required");
    assert_eq!(unauthenticated["code"], "unauthorized");
    assert_eq!(unauthenticated["code"], "unauthorized");
    assert!(
        tree(root).is_empty(),
        "unauthenticated request created a store"
    );

    apply_mcp(&[]);
    let defaults = assert_oracle(&fixture, "defaults").await;
    assert_eq!(defaults["mcp"]["enabled"], json!(true));
    assert_eq!(defaults["mcp"]["protocolVersion"], "2025-06-18");
    assert_eq!(defaults["mcp"]["endpoint"], "/mcp");
    assert_eq!(defaults["mcp"]["capability"], "full");
    assert_eq!(defaults["mcp"]["exposeResources"], json!(true));
    assert_eq!(defaults["mcp"]["exposePrompts"], json!(true));
    let full_count = defaults["mcp"]["toolCount"]
        .as_u64()
        .expect("default tool count");
    assert!(full_count > 0, "default capability exposes no tools");
    assert_eq!(defaults["mcp"]["client"]["enabled"], json!(false));
    assert_eq!(defaults["mcp"]["client"]["servers"], json!([]));
    assert_eq!(defaults["mcp"]["client"]["timeoutSeconds"], json!(30));
    assert_eq!(defaults["mcp"]["client"]["maxRetries"], json!(1));
    assert_eq!(
        defaults["mcp"]["client"]["retryBackoffSeconds"],
        json!(0.25)
    );
    assert_eq!(
        defaults["mcp"]["client"]["circuitBreakerFailures"],
        json!(3)
    );
    assert_eq!(
        defaults["mcp"]["client"]["circuitBreakerResetSeconds"],
        json!(60)
    );

    let (query_status, query_body, _, _) = fixture
        .request("GET", "/api/mcp?limit=7&enabled=0", true)
        .await;
    assert_eq!(query_status, StatusCode::OK);
    assert_eq!(query_body, defaults, "query string changed the body");

    let (status, denied, _, allow) = fixture.request("POST", "/api/mcp", true).await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{denied}");
    assert_eq!(denied, json!({"detail": "Method Not Allowed"}));
    assert_eq!(allow.as_deref(), Some("GET, HEAD"));
    let (status, head, head_bytes, _) = fixture.request("HEAD", "/api/mcp", true).await;
    assert_eq!(status, StatusCode::OK);
    assert!(head_bytes.is_empty(), "{head}");

    apply_mcp(&[("MCP_ENABLED", "0")]);
    let disabled = assert_oracle(&fixture, "disabled hub").await;
    assert_eq!(disabled["mcp"]["enabled"], json!(false));
    assert_eq!(disabled["mcp"]["toolCount"], json!(0));

    apply_mcp(&[("MCP_ENABLED", "1"), ("MCP_CAPABILITY", " coder ")]);
    let coder = assert_oracle(&fixture, "coder capability").await;
    assert_eq!(coder["mcp"]["capability"], "coder");
    let coder_count = coder["mcp"]["toolCount"]
        .as_u64()
        .expect("coder tool count");
    assert!(coder_count > 0 && coder_count < full_count, "{coder}");

    apply_mcp(&[("MCP_CAPABILITY", " not-a-profile ")]);
    let unknown = assert_oracle(&fixture, "unknown capability").await;
    assert_eq!(unknown["mcp"]["capability"], "full");
    assert_eq!(unknown["mcp"]["toolCount"], json!(full_count));

    apply_mcp(&[
        ("MCP_ENABLED", "   "),
        ("MCP_EXPOSE_RESOURCES", "\u{1c}"),
        ("MCP_EXPOSE_PROMPTS", "off"),
    ]);
    let blank = assert_oracle(&fixture, "blank and control whitespace").await;
    assert_eq!(blank["mcp"]["enabled"], json!(true));
    assert_eq!(blank["mcp"]["exposeResources"], json!(true));
    assert_eq!(blank["mcp"]["exposePrompts"], json!(false));

    apply_mcp(&[("MCP_EXPOSE_RESOURCES", "no"), ("MCP_EXPOSE_PROMPTS", "0")]);
    let flags = assert_oracle(&fixture, "expose flags off").await;
    assert_eq!(flags["mcp"]["exposeResources"], json!(false));
    assert_eq!(flags["mcp"]["exposePrompts"], json!(false));

    let servers = r#"[
        {"name": "docs", "url": "http://127.0.0.1:3000/mcp", "timeoutSeconds": 10},
        {"name": "other", "url": "https://example.invalid/mcp"},
        {"name": "", "url": "http://ignored.invalid/mcp"},
        {"name": "bad-scheme", "url": "ftp://127.0.0.1/mcp"},
        {"name": "float-timeout", "url": "http://example.invalid/mcp", "timeoutSeconds": 1.5},
        {"nope": 1}
    ]"#;
    apply_mcp(&[
        ("MCP_CLIENT_ENABLED", "yes"),
        ("MCP_CLIENT_SERVERS", servers),
        ("MCP_CLIENT_TIMEOUT_SECONDS", "-5"),
        ("MCP_CLIENT_MAX_RETRIES", "99"),
        ("MCP_CLIENT_RETRY_BACKOFF_SECONDS", "-1.5"),
        ("MCP_CLIENT_CIRCUIT_BREAKER_FAILURES", "0"),
        ("MCP_CLIENT_CIRCUIT_BREAKER_RESET_SECONDS", "99999"),
    ]);
    let client = assert_oracle(&fixture, "client overrides and clamps").await;
    assert_eq!(client["mcp"]["client"]["enabled"], json!(true));
    // The original keeps every valid name/url in the ordered list even when its
    // timeout override is unusable; that entry falls back to the request timeout.
    assert_eq!(
        client["mcp"]["client"]["servers"],
        json!([
            {"name": "docs", "url": "http://127.0.0.1:3000/mcp", "timeoutSeconds": 10},
            {"name": "other", "url": "https://example.invalid/mcp", "timeoutSeconds": 1},
            {"name": "float-timeout", "url": "http://example.invalid/mcp", "timeoutSeconds": 1},
        ])
    );
    assert_eq!(client["mcp"]["client"]["timeoutSeconds"], json!(1));
    assert_eq!(client["mcp"]["client"]["maxRetries"], json!(5));
    assert_eq!(client["mcp"]["client"]["retryBackoffSeconds"], json!(0.0));
    assert_eq!(client["mcp"]["client"]["circuitBreakerFailures"], json!(1));
    assert_eq!(
        client["mcp"]["client"]["circuitBreakerResetSeconds"],
        json!(3600)
    );

    apply_mcp(&[("MCP_CLIENT_SERVERS", "[{not-json")]);
    let invalid = assert_oracle(&fixture, "invalid client servers").await;
    assert_eq!(invalid["mcp"]["client"]["servers"], json!([]));

    apply_mcp(&[("MCP_CLIENT_ENABLED", "yes")]);
    let enabled_without_servers = assert_oracle(&fixture, "client enabled without servers").await;
    assert_eq!(
        enabled_without_servers["mcp"]["client"]["enabled"],
        json!(true)
    );
    assert_eq!(
        enabled_without_servers["mcp"]["client"]["servers"],
        json!([])
    );

    assert!(tree(root).is_empty(), "mcp status created a store");
}
