//! End-to-end boundary test for the native non-streaming chat route.
//!
//! `create_app()` is driven through Tower against a real loopback HTTP upstream
//! stand-in, so the whole wired path runs: preparation → credential check →
//! HTTP request construction and headers → upstream exchange → response
//! translation → OpenAI envelope. The pure-function units in
//! `chat_execution::tests` cover the translation matrix; this file exists to
//! prove the route is genuinely wired, which a unit test cannot show.
//!
//! Environment variables are process-global, so the cases that mutate them run
//! serially inside one test function (`#[tokio::test]` bodies in a single test
//! share a process with the other tests in this binary, but those do not read
//! these variables concurrently because the whole body is one await chain).

use axum::{
    Router,
    body::Body,
    http::{Request, StatusCode, header},
    routing::post,
};
use serde_json::json;
use std::sync::{Arc, Mutex};
use tower::ServiceExt;

#[derive(Clone, Default)]
struct Captured {
    authorization: Option<String>,
    content_type: Option<String>,
    accept: Option<String>,
    body: Option<serde_json::Value>,
}

type Sink = Arc<Mutex<Captured>>;

/// Upstream stand-in. Records what the gateway actually sent so the test can
/// assert on the wire contract rather than only the response.
fn stub_upstream(response_body: serde_json::Value, status: StatusCode, sink: Sink) -> Router {
    Router::new().route(
        "/chat/completions",
        post(move |headers: axum::http::HeaderMap, body: String| {
            let sink = sink.clone();
            let response_body = response_body.clone();
            async move {
                let parsed = serde_json::from_str(&body).ok();
                {
                    let mut captured = sink.lock().unwrap();
                    captured.authorization = headers
                        .get(header::AUTHORIZATION)
                        .and_then(|value| value.to_str().ok())
                        .map(ToString::to_string);
                    captured.content_type = headers
                        .get(header::CONTENT_TYPE)
                        .and_then(|value| value.to_str().ok())
                        .map(ToString::to_string);
                    captured.accept = headers
                        .get(header::ACCEPT)
                        .and_then(|value| value.to_str().ok())
                        .map(ToString::to_string);
                    captured.body = parsed;
                }
                (
                    status,
                    [(header::CONTENT_TYPE, "application/json")],
                    axum::Json(response_body).into_response(),
                )
            }
        }),
    )
}

use axum::response::IntoResponse;

async fn start_stub(router: Router) -> String {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let _ = axum::serve(listener, router).await;
    });
    format!("http://{addr}/chat/completions")
}

// Environment variables are process-global and Rust tests in one binary run on
// multiple threads, so every test that points the gateway at a stub must hold
// this across its whole body. Without it a sibling test resets
// `DEEPSEEK_API_URL` mid-request and the failure is a flaky 429, not a real
// defect.
//
// A blocking `std::sync::MutexGuard` cannot be held across `.await` (clippy's
// `await_holding_lock`, and it risks starving the runtime), so this is a
// hand-rolled spin flag released by `EnvGuard::drop`. Poisoning is irrelevant:
// the flag carries no data, and a failed test's guard still clears it.
static ENV_BUSY: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

struct EnvLock;

impl EnvLock {
    fn acquire() -> Self {
        use std::sync::atomic::Ordering;
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
        ENV_BUSY.store(false, std::sync::atomic::Ordering::Release);
    }
}

async fn post_chat(uri: &str) -> (StatusCode, serde_json::Value) {
    let app = deepseek_gateway::create_app();
    let request = Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(uri.to_string()))
        .unwrap();
    let response = app.oneshot(request).await.unwrap();
    let status = response.status();
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    (
        status,
        serde_json::from_slice(&bytes).unwrap_or(serde_json::Value::Null),
    )
}

struct EnvGuard {
    saved: Vec<(&'static str, Option<String>)>,
}

impl EnvGuard {
    fn set(pairs: &[(&'static str, &str)]) -> Self {
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

#[tokio::test]
async fn chat_route_executes_against_a_real_upstream() {
    let _env = EnvLock::acquire();
    let sink: Sink = Arc::new(Mutex::new(Captured::default()));
    let upstream = stub_upstream(
        json!({
            "id": "chat-upstream-1",
            "model": "deepseek-v4-pro",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "native answer"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
        }),
        StatusCode::OK,
        sink.clone(),
    );
    let url = start_stub(upstream).await;

    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "unit-upstream-key"),
    ]);

    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "  question  "}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;

    assert_eq!(status, StatusCode::OK, "response: {response}");
    assert_eq!(response["object"], "chat.completion");
    assert_eq!(response["id"], "chat-upstream-1");
    assert_eq!(response["model"], "deepseek-v4-pro");
    assert_eq!(response["choices"][0]["message"]["role"], "assistant");
    assert_eq!(
        response["choices"][0]["message"]["content"],
        "native answer"
    );
    assert_eq!(response["choices"][0]["finish_reason"], "stop");
    assert_eq!(response["usage"]["prompt_tokens"], 11);
    assert_eq!(response["usage"]["completion_tokens"], 4);
    assert_eq!(response["usage"]["total_tokens"], 15);
    assert!(response["created"].as_i64().unwrap() > 0);

    // The wire contract the oracle also sends, and the prepared body — not the
    // raw client body. The credential travels in the header only.
    let captured = sink.lock().unwrap().clone();
    assert_eq!(
        captured.authorization.as_deref(),
        Some("Bearer unit-upstream-key")
    );
    assert_eq!(captured.content_type.as_deref(), Some("application/json"));
    assert_eq!(captured.accept.as_deref(), Some("application/json"));
    let sent = captured.body.expect("upstream received a JSON body");
    assert_eq!(sent["model"], "deepseek-v4-pro");
    assert_eq!(sent["messages"][0]["content"], "question");
    assert!(
        sent.get("api_key").is_none() && sent.get("apiKey").is_none(),
        "credential must never be placed in the body: {sent}"
    );
}

#[tokio::test]
async fn chat_route_surfaces_upstream_failure_without_a_completion() {
    let _env = EnvLock::acquire();
    let sink: Sink = Arc::new(Mutex::new(Captured::default()));
    let upstream = stub_upstream(
        json!({"error": {"message": "rate limited"}}),
        StatusCode::TOO_MANY_REQUESTS,
        sink,
    );
    let url = start_stub(upstream).await;
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "unit-upstream-key"),
    ]);

    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "hello"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;

    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert_eq!(response["error"]["code"], "NATIVE_CHAT_UPSTREAM_STATUS");
    assert!(
        response["error"]["message"]
            .as_str()
            .unwrap()
            .contains("429")
    );
    assert!(response.get("choices").is_none());
}

#[tokio::test]
async fn chat_route_refuses_tool_rounds_instead_of_flattening_them() {
    let _env = EnvLock::acquire();
    let sink: Sink = Arc::new(Mutex::new(Captured::default()));
    let upstream = stub_upstream(
        json!({
            "id": "chat-tools-1",
            "model": "deepseek-v4-pro",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "let me search",
                    "tool_calls": [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": "{\"q\":\"x\"}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }),
        StatusCode::OK,
        sink,
    );
    let url = start_stub(upstream).await;
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "unit-upstream-key"),
    ]);

    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "search for x"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;

    assert_eq!(status, StatusCode::NOT_IMPLEMENTED);
    assert_eq!(
        response["error"]["code"],
        "NATIVE_CHAT_TOOL_ROUNDS_NOT_READY"
    );
    assert!(
        response.get("choices").is_none(),
        "a refused tool round must not look like a completed answer: {response}"
    );
}

#[tokio::test]
async fn chat_route_fails_closed_when_the_upstream_is_unreachable() {
    let _env = EnvLock::acquire();
    // Bind then drop the listener so the port is closed: a real transport
    // failure, not a mocked error.
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    drop(listener);

    let _guard = EnvGuard::set(&[
        (
            "DEEPSEEK_API_URL",
            &format!("http://{addr}/chat/completions"),
        ),
        ("DEEPSEEK_API_KEY", "unit-upstream-key"),
        ("DEEPSEEK_TIMEOUT_SECONDS", "5"),
    ]);

    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "hello"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;

    assert_eq!(status, StatusCode::BAD_GATEWAY);
    assert_eq!(
        response["error"]["code"],
        "NATIVE_CHAT_UPSTREAM_UNREACHABLE"
    );
    assert!(response.get("choices").is_none());
}
