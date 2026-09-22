//! Real HTTP coverage for `POST /api/chat`.
//!
//! The event bytes are pinned in `deepseek-policy` and compared with the oracle by
//! `tasks/native-runtime/chat_stream_events_parity_probe.py`. What this suite adds is
//! the part a unit test cannot see: that the **production router** serves the route,
//! that a scripted upstream's SSE deltas become the oracle's NDJSON lines in the
//! oracle's order, and that the branches this route does not serve are refused rather
//! than answered by a thinner path.
//!
//! # Why the upstream is a real socket
//!
//! `/api/chat` was a `503 GO_CONTROL_PROXY_NOT_READY` before this slice, and the only
//! way to prove it is not one now is to call it through `create_production_app` with an
//! upstream that streams. A handler called directly would not have shown the
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

const TEST_TOKEN: &str = "unit-chat-ndjson-token";

/// `DEEPSEEK_API_URL`, `DEEPSEEK_INFRA_ROOT` and `AUTH_TOKEN` are process-wide, so the
/// cases that set them must not overlap.
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

/// A loopback upstream that answers one request with a scripted SSE body.
struct ScriptedSseUpstream {
    address: std::net::SocketAddr,
    requests: Arc<AtomicUsize>,
    handle: tokio::task::JoinHandle<()>,
}

impl Drop for ScriptedSseUpstream {
    fn drop(&mut self) {
        self.handle.abort();
    }
}

impl ScriptedSseUpstream {
    /// `frames` are the SSE payloads to send, each as its own `data:` line.
    async fn start(frames: Vec<&'static str>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0")
            .await
            .expect("bind loopback");
        let address = listener.local_addr().expect("local address");
        let requests = Arc::new(AtomicUsize::new(0));
        let counter = requests.clone();
        let handle = tokio::spawn(async move {
            loop {
                let Ok((mut stream, _)) = listener.accept().await else {
                    return;
                };
                let counter = counter.clone();
                let frames = frames.clone();
                tokio::spawn(async move {
                    // Read the request head and body so the client's write completes.
                    let mut buffer = Vec::new();
                    let mut chunk = [0u8; 4096];
                    let mut head_end = None;
                    while head_end.is_none() {
                        let Ok(read) = stream.read(&mut chunk).await else {
                            return;
                        };
                        if read == 0 {
                            return;
                        }
                        buffer.extend_from_slice(&chunk[..read]);
                        head_end = find_headers_end(&buffer);
                    }
                    let head_end = head_end.expect("head end");
                    let head = String::from_utf8_lossy(&buffer[..head_end]).to_string();
                    let length = head
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().ok())?
                        })
                        .unwrap_or(0);
                    while buffer.len() < head_end + length {
                        let Ok(read) = stream.read(&mut chunk).await else {
                            return;
                        };
                        if read == 0 {
                            break;
                        }
                        buffer.extend_from_slice(&chunk[..read]);
                    }
                    counter.fetch_add(1, Ordering::SeqCst);

                    let mut body = String::new();
                    for frame in &frames {
                        body.push_str("data: ");
                        body.push_str(frame);
                        body.push_str("\n\n");
                    }
                    body.push_str("data: [DONE]\n\n");
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    let _ = stream.write_all(response.as_bytes()).await;
                    let _ = stream.flush().await;
                });
            }
        });
        Self {
            address,
            requests,
            handle,
        }
    }

    fn url(&self) -> String {
        format!("http://{}/chat/completions", self.address)
    }

    fn request_count(&self) -> usize {
        self.requests.load(Ordering::SeqCst)
    }
}

fn find_headers_end(buffer: &[u8]) -> Option<usize> {
    buffer
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|index| index + 4)
}

/// A workspace root, plus the production router bound to it.
struct Fixture {
    _root: tempfile::TempDir,
    _env: EnvGuard,
    app: axum::Router,
}

impl Fixture {
    fn new(upstream_url: &str) -> Self {
        let root = tempfile::tempdir().expect("a temp workspace root");
        let env = EnvGuard::set(&[
            (
                "DEEPSEEK_INFRA_ROOT",
                root.path().to_str().expect("utf-8 temp path").to_string(),
            ),
            ("DEEPSEEK_API_URL", upstream_url.to_string()),
            ("DEEPSEEK_API_KEY", "test-upstream-key".to_string()),
            ("AUTH_TOKEN", TEST_TOKEN.to_string()),
        ]);
        let static_root = tempfile::tempdir().expect("a static root");
        std::fs::create_dir_all(static_root.path().join("ui")).unwrap();
        std::fs::write(
            static_root.path().join("ui/index.html"),
            "<!doctype html><main>native ui</main>",
        )
        .unwrap();
        let app = create_production_app(static_root.path()).expect("production app");
        Self {
            _root: root,
            _env: env,
            app,
        }
    }

    async fn post(&self, payload: Value) -> (StatusCode, axum::http::HeaderMap, String) {
        let request = Request::builder()
            .method("POST")
            .uri("/api/chat")
            .header(header::CONTENT_TYPE, "application/json")
            .header(header::AUTHORIZATION, format!("Bearer {TEST_TOKEN}"))
            .body(Body::from(payload.to_string()))
            .unwrap();
        let response = self.app.clone().oneshot(request).await.unwrap();
        let status = response.status();
        let headers = response.headers().clone();
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        (status, headers, String::from_utf8_lossy(&bytes).to_string())
    }
}

/// The lines of an NDJSON body, with the empty trailing line dropped.
fn lines(body: &str) -> Vec<Value> {
    body.lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| serde_json::from_str(line).unwrap_or(Value::Null))
        .collect()
}

/// One upstream chunk carrying a content delta.
fn content_frame(text: &str) -> String {
    format!(
        r#"{{"id":"resp-1","model":"deepseek-v4-flash","choices":[{{"index":0,"delta":{{"content":{}}}}}]}}"#,
        serde_json::to_string(text).unwrap()
    )
}

/// One upstream chunk carrying a reasoning delta.
fn reasoning_frame(text: &str) -> String {
    format!(
        r#"{{"choices":[{{"index":0,"delta":{{"reasoning_content":{}}}}}]}}"#,
        serde_json::to_string(text).unwrap()
    )
}

/// One upstream chunk carrying usage and a finish reason.
fn final_frame() -> String {
    r#"{"id":"resp-1","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":4,"total_tokens":14}}"#.to_string()
}

#[tokio::test]
async fn the_route_streams_the_oracles_event_sequence() {
    let _env_lock = EnvLock::acquire();
    let content = content_frame("你好");
    let reasoning = reasoning_frame("思考");
    let final_chunk = final_frame();
    let upstream = ScriptedSseUpstream::start(vec![
        Box::leak(reasoning.into_boxed_str()),
        Box::leak(content.into_boxed_str()),
        Box::leak(content_frame("世界").into_boxed_str()),
        Box::leak(final_chunk.into_boxed_str()),
    ])
    .await;
    let fixture = Fixture::new(&upstream.url());

    let (status, headers, body) = fixture
        .post(json!({
            "model": "deepseek-v4-flash",
            "stream": true,
            "messages": [{"role": "user", "content": "你好"}],
            "apiKey": "client-key",
        }))
        .await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    assert_eq!(
        headers[header::CONTENT_TYPE],
        "application/x-ndjson; charset=utf-8"
    );
    assert_eq!(headers[header::CACHE_CONTROL], "no-cache");
    assert_eq!(headers["x-accel-buffering"], "no");

    let events = lines(&body);
    // The oracle's order: each delta as it arrives, then the terminal `done`.
    let types: Vec<&str> = events
        .iter()
        .map(|event| event["type"].as_str().unwrap_or_default())
        .collect();
    assert_eq!(types, vec!["reasoning", "content", "content", "done"]);
    assert_eq!(events[0]["text"], "思考");
    assert_eq!(events[1]["text"], "你好");
    assert_eq!(events[2]["text"], "世界");

    // The terminal event repeats the accumulated totals, which is what the frontend
    // renders when the stream ends.
    let done = &events[3];
    assert_eq!(done["id"], "resp-1");
    assert_eq!(done["model"], "deepseek-v4-flash");
    assert_eq!(done["content"], "你好世界");
    assert_eq!(done["reasoning"], "思考");
    assert_eq!(done["finishReason"], "stop");
    assert_eq!(done["usage"]["prompt_tokens"], 10);
    assert_eq!(done["usage"]["completion_tokens"], 4);
    assert_eq!(done["memorySuggestions"], json!([]));
    // The diagnostics block is the oracle's own helper chain over this path: the tool
    // summary, the search counts (absent, because there was no search) and the
    // cache-token block from the accumulated usage.
    let diagnostics = &done["diagnostics"];
    assert_eq!(diagnostics["toolCallCount"], 0);
    assert_eq!(diagnostics["toolNames"], json!([]));
    assert!(
        diagnostics.get("searchRoundCount").is_none(),
        "a turn with no search must not carry search counts: {diagnostics}"
    );
    assert_eq!(diagnostics["cacheHitTokens"], 0);
    assert_eq!(diagnostics["cacheMissTokens"], 0);
    assert_eq!(diagnostics["cacheHitRate"], 0.0);

    // Each line is one compact JSON object plus a newline, which is the contract.
    for line in body.lines().filter(|line| !line.trim().is_empty()) {
        assert!(!line.contains(": "), "not compact: {line}");
        assert!(line.starts_with('{') && line.ends_with('}'), "{line}");
    }
    assert_eq!(upstream.request_count(), 1);
}

#[tokio::test]
async fn a_truncated_answer_emits_the_length_note_before_done() {
    let _env_lock = EnvLock::acquire();
    let truncated =
        r#"{"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":"length"}]}"#;
    let upstream = ScriptedSseUpstream::start(vec![truncated]).await;
    let fixture = Fixture::new(&upstream.url());

    let (status, _, body) = fixture
        .post(json!({
            "model": "deepseek-v4-flash",
            "stream": true,
            "messages": [{"role": "user", "content": "hi"}],
            "apiKey": "client-key",
        }))
        .await;
    assert_eq!(status, StatusCode::OK, "body: {body}");
    let events = lines(&body);
    let types: Vec<&str> = events
        .iter()
        .map(|event| event["type"].as_str().unwrap_or_default())
        .collect();
    // `last_finish_reason == "length"` puts the note before the terminal event.
    assert_eq!(types, vec!["content", "system_note", "done"]);
    assert!(
        events[1]["text"]
            .as_str()
            .unwrap_or_default()
            .contains("输出长度上限")
    );
    assert_eq!(events[2]["finishReason"], "length");
}

#[tokio::test]
async fn agent_mode_is_refused_before_any_upstream_call() {
    let _env_lock = EnvLock::acquire();
    let upstream = ScriptedSseUpstream::start(vec![]).await;
    let fixture = Fixture::new(&upstream.url());

    let (status, _, body) = fixture
        .post(json!({
            "model": "deepseek-v4-flash",
            "stream": true,
            "agentMode": true,
            "messages": [{"role": "user", "content": "hi"}],
            "apiKey": "client-key",
        }))
        .await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "body: {body}");
    let parsed: Value = serde_json::from_str(&body).unwrap_or(Value::Null);
    assert_eq!(parsed["code"], "NATIVE_CHAT_BRANCH_NOT_READY");
    assert!(
        parsed["error"]
            .as_str()
            .unwrap_or_default()
            .contains("agentMode")
    );
    // Refused means refused: the upstream was never dialled.
    assert_eq!(upstream.request_count(), 0);
}

#[tokio::test]
async fn a_forced_search_mode_is_refused_before_any_upstream_call() {
    let _env_lock = EnvLock::acquire();
    let upstream = ScriptedSseUpstream::start(vec![]).await;
    let fixture = Fixture::new(&upstream.url());

    let (status, _, body) = fixture
        .post(json!({
            "model": "deepseek-v4-flash",
            "stream": true,
            "searchMode": "on",
            "messages": [{"role": "user", "content": "hi"}],
            "apiKey": "client-key",
        }))
        .await;
    assert_eq!(status, StatusCode::NOT_IMPLEMENTED, "body: {body}");
    let parsed: Value = serde_json::from_str(&body).unwrap_or(Value::Null);
    assert_eq!(parsed["code"], "NATIVE_CHAT_BRANCH_NOT_READY");
    assert_eq!(upstream.request_count(), 0);
}

#[tokio::test]
async fn a_payload_with_no_user_turn_is_the_oracles_400() {
    let _env_lock = EnvLock::acquire();
    let upstream = ScriptedSseUpstream::start(vec![]).await;
    let fixture = Fixture::new(&upstream.url());

    let (status, _, body) = fixture
        .post(json!({
            "model": "deepseek-v4-flash",
            "stream": true,
            "messages": [{"role": "assistant", "content": "no user turn"}],
            "apiKey": "client-key",
        }))
        .await;
    assert_eq!(status, StatusCode::BAD_REQUEST, "body: {body}");
    let parsed: Value = serde_json::from_str(&body).unwrap_or(Value::Null);
    assert!(
        parsed["code"].is_string(),
        "the envelope carries a machine-readable code: {body}"
    );
    assert_eq!(upstream.request_count(), 0);
}

#[tokio::test]
async fn an_upstream_failure_is_an_http_error_not_a_200_stream() {
    let _env_lock = EnvLock::acquire();
    // Nothing listens on this port, so opening the stream fails.
    let fixture = Fixture::new("http://127.0.0.1:1/chat/completions");
    let (status, _, body) = fixture
        .post(json!({
            "model": "deepseek-v4-flash",
            "stream": true,
            "messages": [{"role": "user", "content": "hi"}],
            "apiKey": "client-key",
        }))
        .await;
    assert!(
        status.is_client_error() || status.is_server_error(),
        "{status}"
    );
    let parsed: Value = serde_json::from_str(&body).unwrap_or(Value::Null);
    assert!(parsed["error"].is_string(), "{body}");
    assert!(parsed["code"].is_string(), "{body}");
}

#[tokio::test]
async fn the_chat_route_is_behind_the_production_auth_layer() {
    let _env_lock = EnvLock::acquire();
    let upstream = ScriptedSseUpstream::start(vec![]).await;
    let fixture = Fixture::new(&upstream.url());
    let request = Request::builder()
        .method("POST")
        .uri("/api/chat")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(
            json!({"stream": true, "messages": [{"role": "user", "content": "hi"}]}).to_string(),
        ))
        .unwrap();
    let response = fixture.app.clone().oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
    assert_eq!(upstream.request_count(), 0);
}
