//! `GET /api/rust/status` through the production router.
//!
//! The body is `rust_status()`. A disabled gateway does not open a socket.
//! An enabled gateway is healthy only when `GET /healthz` returns HTTP 200.

use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tower::ServiceExt;

const TEST_TOKEN: &str = "rust-status-route-token";

const RUST_KEYS: &[&str] = &[
    "DEEPSEEK_RUST_GATEWAY",
    "DEEPSEEK_RUST_MCP",
    "DEEPSEEK_RUST_POLICY",
    "DEEPSEEK_RUST_RAG",
    "DEEPSEEK_RUST_GATEWAY_URL",
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
        for key in RUST_KEYS {
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

struct HealthServer {
    port: u16,
    stop: Arc<AtomicBool>,
    _listener: TcpListener,
}
impl HealthServer {
    fn start(status_code: u16) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind");
        listener.set_nonblocking(true).expect("nonblocking");
        let port = listener.local_addr().expect("port").port();
        let accept_listener = listener.try_clone().expect("clone");
        let stop = Arc::new(AtomicBool::new(false));
        let thread_stop = Arc::clone(&stop);
        std::thread::spawn(move || {
            while !thread_stop.load(Ordering::Acquire) {
                match accept_listener.accept() {
                    Ok((mut stream, _)) => {
                        let _ = stream.set_nonblocking(true);
                        let started = std::time::Instant::now();
                        let mut buf = [0_u8; 2048];
                        loop {
                            if thread_stop.load(Ordering::Acquire)
                                || started.elapsed() > Duration::from_secs(2)
                            {
                                break;
                            }
                            match stream.read(&mut buf) {
                                Ok(0) | Ok(_) => break,
                                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                                    std::thread::sleep(Duration::from_millis(10));
                                }
                                Err(_) => break,
                            }
                        }
                        let raw = format!(
                            "HTTP/1.1 {status_code} OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
                        );
                        let _ = stream.write_all(raw.as_bytes());
                        let _ = stream.flush();
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(20));
                    }
                    Err(_) => break,
                }
            }
        });
        Self {
            port,
            stop,
            _listener: listener,
        }
    }
}
impl Drop for HealthServer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
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

fn apply_rust(overrides: &[(&str, &str)]) {
    for key in RUST_KEYS {
        unsafe { std::env::set_var(key, "") }
    }
    for (name, value) in overrides {
        unsafe { std::env::set_var(name, value) }
    }
}

fn oracle(root: &Path) -> Value {
    let script = r#"
import json
from deepseek_infra.infra.rust_core.registry import rust_status
print(json.dumps({"status": 200, "body": {"ok": True, "rust": rust_status()}}))
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
        Duration::from_secs(8),
        fixture.request("GET", "/api/rust/status", true),
    )
    .await
    .unwrap_or_else(|_| panic!("timed out: {label}"));
    let (status, body, _, _) = request;
    let root = fixture.root.path().to_path_buf();
    let expected = tokio::time::timeout(
        Duration::from_secs(8),
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

fn closed_port() -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").expect("closed bind");
    let port = listener.local_addr().expect("port").port();
    drop(listener);
    port
}

#[tokio::test]
async fn rust_status_matches_the_oracle_without_creating_a_store() {
    let _lock = EnvLock::acquire();
    let fixture = Fixture::new();
    let root = fixture.root.path();
    let ok_server = HealthServer::start(200);
    let denied_server = HealthServer::start(503);
    let ok_url = format!("http://127.0.0.1:{}/ignored?x=1", ok_server.port);
    let denied_url = format!("http://127.0.0.1:{}", denied_server.port);
    let refused_url = format!("http://127.0.0.1:{}", closed_port());

    let (status, unauthenticated, _, _) = tokio::time::timeout(
        Duration::from_secs(8),
        fixture.request("GET", "/api/rust/status", false),
    )
    .await
    .expect("timed out: unauth");
    assert_eq!(status, StatusCode::UNAUTHORIZED, "{unauthenticated}");
    assert_eq!(unauthenticated["error"]["code"], "UNAUTHORIZED");
    assert_eq!(unauthenticated["error"]["message"], "Auth required");
    assert!(
        tree(root).is_empty(),
        "unauthenticated request created a store"
    );

    apply_rust(&[]);
    let defaults = assert_oracle(&fixture, "defaults").await;
    assert_eq!(defaults["rust"]["enabled"]["gateway"], json!(false));
    assert_eq!(defaults["rust"]["enabled"]["mcp"], json!(false));
    assert_eq!(defaults["rust"]["enabled"]["policy"], json!(false));
    assert_eq!(defaults["rust"]["enabled"]["rag"], json!(false));
    assert_eq!(
        defaults["rust"]["components"]["gateway"]["enabled"],
        json!(false)
    );
    assert_eq!(defaults["rust"]["components"]["gateway"]["url"], "");
    assert_eq!(
        defaults["rust"]["components"]["gateway"]["healthy"],
        json!(false)
    );
    let (query_status, query_body, _, _) = fixture
        .request("GET", "/api/rust/status?gateway=1", true)
        .await;
    assert_eq!(query_status, StatusCode::OK);
    assert_eq!(query_body, defaults, "query string changed the body");

    let (status, denied, _, allow) = fixture.request("POST", "/api/rust/status", true).await;
    assert_eq!(status, StatusCode::METHOD_NOT_ALLOWED, "{denied}");
    assert_eq!(denied, json!({"detail": "Method Not Allowed"}));
    assert_eq!(allow.as_deref(), Some("GET, HEAD"));
    let (status, head, head_bytes, _) = fixture.request("HEAD", "/api/rust/status", true).await;
    assert_eq!(status, StatusCode::OK);
    assert!(head_bytes.is_empty(), "{head}");

    apply_rust(&[
        ("DEEPSEEK_RUST_MCP", "yes"),
        ("DEEPSEEK_RUST_POLICY", "TRUE"),
        ("DEEPSEEK_RUST_RAG", "on"),
        ("DEEPSEEK_RUST_GATEWAY_URL", &ok_url),
    ]);
    let flags = assert_oracle(&fixture, "flags without gateway").await;
    assert_eq!(flags["rust"]["enabled"]["gateway"], json!(false));
    assert_eq!(flags["rust"]["enabled"]["mcp"], json!(true));
    assert_eq!(flags["rust"]["enabled"]["policy"], json!(true));
    assert_eq!(flags["rust"]["enabled"]["rag"], json!(true));
    assert_eq!(flags["rust"]["components"]["gateway"]["url"], "");
    assert_eq!(
        flags["rust"]["components"]["gateway"]["healthy"],
        json!(false)
    );

    apply_rust(&[
        ("DEEPSEEK_RUST_GATEWAY", "   "),
        ("DEEPSEEK_RUST_GATEWAY_URL", &ok_url),
    ]);
    let whitespace = assert_oracle(&fixture, "whitespace gateway flag").await;
    assert_eq!(whitespace["rust"]["enabled"]["gateway"], json!(false));
    assert_eq!(
        whitespace["rust"]["components"]["gateway"]["healthy"],
        json!(false)
    );
    assert_eq!(whitespace["rust"]["components"]["gateway"]["url"], "");

    apply_rust(&[
        ("DEEPSEEK_RUST_GATEWAY", "1"),
        ("DEEPSEEK_RUST_GATEWAY_URL", "ftp://127.0.0.1:9/healthz"),
    ]);
    let scheme = assert_oracle(&fixture, "non-http scheme").await;
    assert_eq!(
        scheme["rust"]["components"]["gateway"]["enabled"],
        json!(true)
    );
    assert_eq!(
        scheme["rust"]["components"]["gateway"]["url"],
        "ftp://127.0.0.1:9/healthz"
    );
    assert_eq!(
        scheme["rust"]["components"]["gateway"]["healthy"],
        json!(false)
    );

    apply_rust(&[
        ("DEEPSEEK_RUST_GATEWAY", "1"),
        ("DEEPSEEK_RUST_GATEWAY_URL", &refused_url),
    ]);
    let refused = assert_oracle(&fixture, "connection refused").await;
    assert_eq!(refused["rust"]["components"]["gateway"]["url"], refused_url);
    assert_eq!(
        refused["rust"]["components"]["gateway"]["healthy"],
        json!(false)
    );

    apply_rust(&[
        ("DEEPSEEK_RUST_GATEWAY", "1"),
        ("DEEPSEEK_RUST_GATEWAY_URL", &ok_url),
    ]);
    let healthy = assert_oracle(&fixture, "healthz 200").await;
    assert_eq!(healthy["rust"]["components"]["gateway"]["url"], ok_url);
    assert_eq!(
        healthy["rust"]["components"]["gateway"]["healthy"],
        json!(true)
    );
    assert_eq!(healthy["rust"]["enabled"]["gateway"], json!(true));

    apply_rust(&[
        ("DEEPSEEK_RUST_GATEWAY", "1"),
        ("DEEPSEEK_RUST_GATEWAY_URL", &denied_url),
    ]);
    let unavailable = assert_oracle(&fixture, "healthz 503").await;
    assert_eq!(
        unavailable["rust"]["components"]["gateway"]["healthy"],
        json!(false)
    );
    assert_eq!(
        unavailable["rust"]["components"]["gateway"]["url"],
        denied_url
    );

    apply_rust(&[
        ("DEEPSEEK_RUST_GATEWAY", "yes"),
        ("DEEPSEEK_RUST_MCP", "0"),
        ("DEEPSEEK_RUST_POLICY", "no"),
        ("DEEPSEEK_RUST_RAG", "1"),
        ("DEEPSEEK_RUST_GATEWAY_URL", "  ftp://127.0.0.1:9/healthz  "),
    ]);
    let trimmed = assert_oracle(&fixture, "padded url is stripped").await;
    assert_eq!(trimmed["rust"]["enabled"]["gateway"], json!(true));
    assert_eq!(trimmed["rust"]["enabled"]["mcp"], json!(false));
    assert_eq!(trimmed["rust"]["enabled"]["policy"], json!(false));
    assert_eq!(trimmed["rust"]["enabled"]["rag"], json!(true));
    assert_eq!(
        trimmed["rust"]["components"]["gateway"]["url"],
        "ftp://127.0.0.1:9/healthz"
    );
    assert_eq!(
        trimmed["rust"]["components"]["gateway"]["healthy"],
        json!(false)
    );

    assert!(tree(root).is_empty(), "rust status created a store");
}
