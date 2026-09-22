//! Real HTTP coverage for `POST /api/title`.
//!
//! The pure half of the route (prompt, request body, sanitiser, rate limiter) is
//! pinned in `deepseek-policy` and compared with the oracle by
//! `tasks/native-runtime/title_parity_probe.py`. What this suite adds is the part a
//! unit test cannot see: that the **production router** serves the route, that the
//! request the upstream actually receives is the oracle's body, and that the failure
//! envelopes are the oracle's.
//!
//! # Why the upstream is a real socket
//!
//! `/api/title` was a `503 GO_CONTROL_PROXY_NOT_READY` before this slice, and the
//! only way to prove it is not one now is to call it through `create_production_app`
//! with an upstream that answers. A handler called directly would not have shown the
//! registration order or the auth layer either.

use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

use axum::body::Body;
use axum::http::{Request, StatusCode, header};
use deepseek_gateway::create_production_app;
use serde_json::{Value, json};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tower::ServiceExt;

const TEST_TOKEN: &str = "unit-title-route-token";

/// `DEEPSEEK_API_URL` and `DEEPSEEK_API_KEY` are process-wide, so the cases that set
/// them must not overlap. A blocking guard cannot be held across `.await` (and clippy's
/// `await_holding_lock` is right to object), so this is the same spin flag
/// `chat_execution.rs` and `data_routes.rs` use, released by `EnvLock::drop`.
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

/// What the scripted upstream should answer with.
#[derive(Clone)]
enum Upstream {
    /// A well-formed completion whose first choice carries `content`.
    Completion(&'static str),
    /// A non-success status with the given body.
    Error { status: u16, body: &'static str },
}

/// A loopback upstream that records the request it received.
///
/// Recording on the server side is what makes the body assertion real: the test
/// checks the bytes that arrived on the socket, not a value the client believed it
/// sent. The route discards everything but `choices[0].message.content`, so the echo
/// cannot ride back in the response.
struct ScriptedUpstream {
    address: std::net::SocketAddr,
    requests: Arc<AtomicUsize>,
    received: Arc<std::sync::Mutex<Vec<Value>>>,
    handle: tokio::task::JoinHandle<()>,
}

impl Drop for ScriptedUpstream {
    fn drop(&mut self) {
        self.handle.abort();
    }
}

impl ScriptedUpstream {
    async fn start(upstream: Upstream) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind loopback");
        let address = listener.local_addr().expect("local address");
        let requests = Arc::new(AtomicUsize::new(0));
        let received = Arc::new(std::sync::Mutex::new(Vec::new()));
        let counter = requests.clone();
        let recorder = received.clone();
        let handle = tokio::spawn(async move {
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    return;
                };
                let upstream = upstream.clone();
                let counter = counter.clone();
                let recorder = recorder.clone();
                tokio::spawn(async move {
                    let mut buffer = Vec::new();
                    let mut chunk = [0u8; 4096];
                    // Read headers, then exactly `Content-Length` bytes of body.
                    let (head, body_start) = loop {
                        let Ok(read) = stream.read(&mut chunk).await else {
                            return;
                        };
                        if read == 0 {
                            return;
                        }
                        buffer.extend_from_slice(&chunk[..read]);
                        if let Some(index) = find_headers_end(&buffer) {
                            break (buffer[..index].to_vec(), index);
                        }
                    };
                    let head_text = String::from_utf8_lossy(&head).to_string();
                    let length = head_text
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().ok())?
                        })
                        .unwrap_or(0);
                    let mut body = buffer[body_start..].to_vec();
                    while body.len() < length {
                        let Ok(read) = stream.read(&mut chunk).await else {
                            return;
                        };
                        if read == 0 {
                            break;
                        }
                        body.extend_from_slice(&chunk[..read]);
                    }
                    body.truncate(length);
                    counter.fetch_add(1, Ordering::SeqCst);

                    let request_line = head_text.lines().next().unwrap_or_default().to_string();
                    let path = request_line
                        .split_whitespace()
                        .nth(1)
                        .unwrap_or("/")
                        .to_string();
                    let header_of = |wanted: &str| {
                        head_text
                            .lines()
                            .find_map(|line| {
                                let (name, value) = line.split_once(':')?;
                                name.eq_ignore_ascii_case(wanted)
                                    .then(|| value.trim().to_string())
                            })
                            .unwrap_or_default()
                    };
                    let sent: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
                    recorder
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner)
                        .push(json!({
                            "path": path,
                            "authorization": header_of("authorization"),
                            "accept": header_of("accept"),
                            "body": sent,
                        }));

                    let (status, payload) = match &upstream {
                        Upstream::Completion(content) => (
                            200u16,
                            json!({
                                "id": "title-1",
                                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
                            }),
                        ),
                        Upstream::Error { status, body } => (
                            *status,
                            serde_json::from_str(body).unwrap_or(json!({"raw": body})),
                        ),
                    };
                    let payload = payload.to_string();
                    let response = format!(
                        "HTTP/1.1 {status} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
                        if status == 200 { "OK" } else { "Error" },
                        payload.len()
                    );
                    let _ = stream.write_all(response.as_bytes()).await;
                    let _ = stream.flush().await;
                });
            }
        });
        Self {
            address,
            requests,
            received,
            handle,
        }
    }

    fn url(&self) -> String {
        format!("http://{}/chat/completions", self.address)
    }

    fn request_count(&self) -> usize {
        self.requests.load(Ordering::SeqCst)
    }

    /// The last request that arrived, or `Null` when none did.
    fn last_request(&self) -> Value {
        self.received
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .last()
            .cloned()
            .unwrap_or(Value::Null)
    }
}

/// The index just past the `\r\n\r\n` (or `\n\n`) that ends the headers.
fn find_headers_end(buffer: &[u8]) -> Option<usize> {
    buffer
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|index| index + 4)
        .or_else(|| {
            buffer
                .windows(2)
                .position(|window| window == b"\n\n")
                .map(|index| index + 2)
        })
}

/// Bind a scripted upstream plus whatever environment the case needs.
///
/// `DEEPSEEK_API_URL` and `DEEPSEEK_API_KEY` are process-wide, so every case that
/// sets them holds this flag; the rate limiter is process-wide too, which is why
/// each case uses its own API key.
struct EnvGuard {
    saved: Vec<(&'static str, Option<String>)>,
}

impl EnvGuard {
    fn set(pairs: &[(&'static str, String)]) -> Self {
        let mut saved = Vec::new();
        for (name, value) in pairs {
            saved.push((*name, std::env::var(name).ok()));
            unsafe {
                std::env::set_var(name, value);
            }
        }
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

/// A distinct API key per case, so the process-wide window cannot couple two cases.
fn unique_key(label: &str) -> String {
    static COUNTER: AtomicUsize = AtomicUsize::new(0);
    format!(
        "test-key-{label}-{}",
        COUNTER.fetch_add(1, Ordering::SeqCst)
    )
}

async fn post_title(payload: Value) -> (StatusCode, Value) {
    post_title_with_token(payload, Some(TEST_TOKEN)).await
}

async fn post_title_with_token(payload: Value, token: Option<&str>) -> (StatusCode, Value) {
    let mut builder = Request::builder()
        .method("POST")
        .uri("/api/title")
        .header(header::CONTENT_TYPE, "application/json");
    if let Some(token) = token {
        builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
    }
    let request = builder.body(Body::from(payload.to_string())).unwrap();
    let response = production_app().oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(Value::Null),
    )
}

/// The production router, with the static layer a real deployment has.
///
/// `create_production_app` refuses to start without `static/ui/index.html`, so the
/// fixture writes one: this suite measures the router the browser reaches, and
/// stubbing the static layer out would mean measuring a different one.
fn production_app() -> axum::Router {
    let static_root = tempfile::tempdir().expect("a static root");
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(
        static_root.path().join("ui/index.html"),
        "<!doctype html><main>native ui</main>",
    )
    .unwrap();
    create_production_app(static_root.path()).expect("production app")
}

#[tokio::test]
async fn the_title_route_serves_the_oracles_request_and_sanitises_the_answer() {
    let _env_lock = EnvLock::acquire();
    let upstream = ScriptedUpstream::start(Upstream::Completion("「标题： 解释 FastCDC」")).await;
    let key = unique_key("happy");
    let _guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("DEEPSEEK_API_URL", upstream.url()),
        ("DEEPSEEK_API_KEY", key.clone()),
    ]);

    let (status, body) = post_title(json!({
        "userMessage": "解释一下 FastCDC",
        "assistantMessage": "FastCDC 是一种内容定义分块算法。",
    }))
    .await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    // The sanitiser peeled the quotes, the label, and the closing quote.
    assert_eq!(body["title"], "解释 FastCDC");

    // The upstream received the oracle's body, on the oracle's path, with the
    // caller's key — read back off the socket rather than assumed.
    assert_eq!(upstream.request_count(), 1);
    let received = upstream.last_request();
    assert_eq!(received["path"], "/chat/completions");
    assert_eq!(received["authorization"], format!("Bearer {key}"));
    assert_eq!(received["accept"], "application/json");
    let sent = &received["body"];
    assert_eq!(sent["stream"], false);
    assert_eq!(sent["thinking"], json!({"type": "disabled"}));
    assert_eq!(sent["temperature"], 0.3);
    assert_eq!(sent["max_tokens"], 60);
    // The default model, because `titleModel` was not sent.
    assert_eq!(sent["model"], "deepseek-v4-flash");
    assert!(
        sent["messages"][0]["content"]
            .as_str()
            .unwrap_or_default()
            .contains("标题生成器")
    );
    assert!(
        sent["messages"][1]["content"]
            .as_str()
            .unwrap_or_default()
            .contains("解释一下 FastCDC")
    );
}

#[tokio::test]
async fn a_blank_user_message_returns_an_empty_title_without_calling_upstream() {
    let _env_lock = EnvLock::acquire();
    let upstream = ScriptedUpstream::start(Upstream::Completion("unused")).await;
    let _guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("DEEPSEEK_API_URL", upstream.url()),
        ("DEEPSEEK_API_KEY", unique_key("blank")),
    ]);

    let (status, body) = post_title(json!({"userMessage": "   "})).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["title"], "");
    // The early return is the point: a blank turn must not spend a request or a
    // rate-limit slot.
    assert_eq!(upstream.request_count(), 0);

    let (status, body) = post_title(json!({})).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["title"], "");
    assert_eq!(upstream.request_count(), 0);
}

#[tokio::test]
async fn a_missing_api_key_is_the_oracles_400() {
    let _env_lock = EnvLock::acquire();
    let upstream = ScriptedUpstream::start(Upstream::Completion("unused")).await;
    let _guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("DEEPSEEK_API_URL", upstream.url()),
        ("DEEPSEEK_API_KEY", String::new()),
    ]);

    let (status, body) = post_title(json!({"userMessage": "hi"})).await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "body: {body}");
    assert_eq!(body["code"], "missing_api_key");
    assert_eq!(body["error"], "Missing DeepSeek API Key.");
    assert_eq!(upstream.request_count(), 0);
}

#[tokio::test]
async fn an_upstream_error_is_the_providers_message_with_the_status_capped_at_502() {
    let _env_lock = EnvLock::acquire();
    let upstream = ScriptedUpstream::start(Upstream::Error {
        status: 503,
        body: r#"{"error": {"message": "upstream is busy"}}"#,
    })
    .await;
    let _guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("DEEPSEEK_API_URL", upstream.url()),
        ("DEEPSEEK_API_KEY", unique_key("error")),
    ]);

    let (status, body) = post_title(json!({"userMessage": "hi"})).await;
    // `min(exc.code, 502)`.
    assert_eq!(status, StatusCode::BAD_GATEWAY, "body: {body}");
    assert_eq!(body["error"], "upstream is busy");
    assert_eq!(body["code"], "upstream_failure");
}

#[tokio::test]
async fn a_non_json_upstream_body_is_reported_as_a_failure() {
    let _env_lock = EnvLock::acquire();
    let upstream = ScriptedUpstream::start(Upstream::Completion("")).await;
    let _guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("DEEPSEEK_API_URL", upstream.url()),
        ("DEEPSEEK_API_KEY", unique_key("empty")),
    ]);

    // A completion with empty content is a successful call with an empty title —
    // the oracle sanitises whatever it got rather than inventing one.
    let (status, body) = post_title(json!({"userMessage": "hi"})).await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(body["title"], "");
}

#[tokio::test]
async fn the_thirteenth_title_request_in_the_window_is_rate_limited() {
    let _env_lock = EnvLock::acquire();
    let upstream = ScriptedUpstream::start(Upstream::Completion("ok")).await;
    let key = unique_key("limit");
    let _guard = EnvGuard::set(&[
        ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ("DEEPSEEK_API_URL", upstream.url()),
        ("DEEPSEEK_API_KEY", key),
    ]);

    for index in 0..12 {
        let (status, body) = post_title(json!({"userMessage": format!("turn {index}")})).await;
        assert_eq!(status, StatusCode::OK, "call {index} body: {body}");
    }
    let (status, body) = post_title(json!({"userMessage": "turn 13"})).await;
    assert_eq!(status, StatusCode::TOO_MANY_REQUESTS, "body: {body}");
    assert_eq!(body["code"], "rate_limited");
    // Refused means the upstream was not called a thirteenth time.
    assert_eq!(upstream.request_count(), 12);
}

#[tokio::test]
async fn the_title_route_is_behind_the_production_auth_layer() {
    let _env_lock = EnvLock::acquire();
    let static_root = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
    std::fs::write(static_root.path().join("ui/index.html"), "<main>ui</main>").unwrap();
    let _guard = EnvGuard::set(&[("AUTH_TOKEN", TEST_TOKEN.to_string())]);
    let app = create_production_app(static_root.path()).unwrap();
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/title")
                .header(header::CONTENT_TYPE, "application/json")
                .body(Body::from(json!({"userMessage": "hi"}).to_string()))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
}
