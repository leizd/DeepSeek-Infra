//! End-to-end boundary test for the native non-streaming chat route.
//!
//! `create_app()` is driven through Tower against a real loopback HTTP upstream
//! stand-in, so the whole wired path runs: preparation → credential check →
//! HTTP request construction and headers → upstream exchange → **the tool round
//! loop** (dispatch → tool results → follow-up request) → response translation →
//! OpenAI envelope. The pure-function units in `chat_execution::tests` and
//! `chat_tool_loop::tests` cover the matrices; this file exists to prove the
//! route is genuinely wired, which a unit test cannot show.
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
use std::collections::VecDeque;
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

/// Upstream stand-in for a tool loop: answers a scripted sequence, one body per
/// request, and records every request body it received so a test can assert on
/// the follow-up request the loop builds. Once the script runs out it answers a
/// plain completion, so an unexpectedly long loop fails on the request-count
/// assertion rather than on a hang.
fn stub_upstream_sequence(
    scripts: Vec<serde_json::Value>,
    requests: Arc<Mutex<Vec<serde_json::Value>>>,
) -> Router {
    let remaining: Arc<Mutex<VecDeque<serde_json::Value>>> = Arc::new(Mutex::new(scripts.into()));
    Router::new().route(
        "/chat/completions",
        post(move |body: String| {
            let requests = requests.clone();
            let remaining = remaining.clone();
            async move {
                if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&body) {
                    requests.lock().unwrap().push(parsed);
                }
                let response_body = remaining
                    .lock()
                    .unwrap()
                    .pop_front()
                    .unwrap_or_else(|| json!({
                        "id": "chat-unexpected",
                        "model": "deepseek-v4-pro",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "unexpected extra turn"}, "finish_reason": "stop"}],
                    }));
                (
                    StatusCode::OK,
                    [(header::CONTENT_TYPE, "application/json")],
                    axum::Json(response_body).into_response(),
                )
            }
        }),
    )
}

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
async fn chat_route_runs_tool_rounds_through_dispatch() {
    let _env = EnvLock::acquire();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let chart_call = json!({
        "id": "call-1",
        "type": "function",
        "function": {
            "name": "generate_chart",
            "arguments": "{\"type\":\"bar\",\"title\":\"t\",\"data\":[{\"label\":\"a\",\"value\":1},{\"label\":\"b\",\"value\":2}]}",
        },
    });
    let upstream = stub_upstream_sequence(
        vec![
            json!({
                "id": "chat-tools-1",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "let me chart that",
                        "reasoning_content": "chart reasoning",
                        "tool_calls": [chart_call.clone()],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            }),
            json!({
                "id": "chat-tools-2",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "the chart is drawn"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11},
            }),
        ],
        requests.clone(),
    );
    let url = start_stub(upstream).await;

    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "unit-upstream-key"),
    ]);

    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "chart a vs b"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "generate_chart",
                "description": "render a chart",
                "parameters": {"type": "object"},
            },
        }],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;

    assert_eq!(status, StatusCode::OK, "response: {response}");
    assert_eq!(
        response["choices"][0]["message"]["content"],
        "the chart is drawn"
    );
    // Usage is merged across both rounds, mirroring `merge_usage_totals`.
    assert_eq!(response["usage"]["prompt_tokens"], 10);
    assert_eq!(response["usage"]["completion_tokens"], 6);
    assert_eq!(response["usage"]["total_tokens"], 16);

    let requests = requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 2, "one executed round, then one final turn");
    // The tool definitions survived preparation — without that, the model could
    // never have called anything and the loop would be unreachable.
    assert!(
        requests[0]["tools"]
            .as_array()
            .is_some_and(|tools| !tools.is_empty()),
        "first request must carry the tools: {}",
        requests[0]
    );
    let messages = requests[1]["messages"].as_array().unwrap();
    assert_eq!(messages.len(), 3);
    // The assistant turn that requested tools, replayed with its content and
    // reasoning (thinking mode rejects the follow-up without reasoning_content).
    assert_eq!(messages[1]["role"], "assistant");
    assert_eq!(messages[1]["content"], "let me chart that");
    assert_eq!(messages[1]["reasoning_content"], "chart reasoning");
    assert_eq!(messages[1]["tool_calls"][0]["id"], "call-1");
    // The tool result the model reads back: the dispatch envelope, compact JSON.
    assert_eq!(messages[2]["role"], "tool");
    assert_eq!(messages[2]["tool_call_id"], "call-1");
    assert_eq!(messages[2]["name"], "generate_chart");
    let content = messages[2]["content"].as_str().unwrap();
    assert!(content.starts_with("{\"ok\":true"), "content: {content}");
    assert!(content.contains("\"tool\":\"generate_chart\""));
    assert!(content.contains("markdownTable"), "content: {content}");
}

#[tokio::test]
async fn chat_route_runs_a_data_branch_against_the_workspace() {
    let _env = EnvLock::acquire();
    let workspace = tempfile::tempdir().unwrap();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let upstream = stub_upstream_sequence(
        vec![
            json!({
                "id": "chat-data-1",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call-rem-1",
                            "type": "function",
                            "function": {"name": "create_reminder", "arguments": "{\"title\":\"buy milk\",\"content\":\"two litres\",\"dueAt\":\"2027-01-01T09:00:00Z\"}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }),
            json!({
                "id": "chat-data-2",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "reminder set"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14},
            }),
        ],
        requests.clone(),
    );
    let url = start_stub(upstream).await;
    let root = workspace.path().to_string_lossy().to_string();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "unit-upstream-key"),
        ("DEEPSEEK_INFRA_ROOT", &root),
    ]);

    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "remind me to buy milk"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "create_reminder",
                "description": "create a reminder",
                "parameters": {"type": "object"},
            },
        }],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;

    assert_eq!(status, StatusCode::OK, "response: {response}");
    assert_eq!(response["choices"][0]["message"]["content"], "reminder set");

    let requests = requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 2);
    let messages = requests[1]["messages"].as_array().unwrap();
    assert_eq!(messages[2]["role"], "tool");
    assert_eq!(messages[2]["name"], "create_reminder");
    let content = messages[2]["content"].as_str().unwrap();
    assert!(content.contains("\"ok\":true"), "content: {content}");
    assert!(content.contains("buy milk"), "content: {content}");

    // The write went through the injected workspace, fence and all: the store
    // and the mutation gate's durable files live under DEEPSEEK_INFRA_ROOT,
    // not under any process-global default.
    assert!(
        workspace
            .path()
            .join(".reminders")
            .join("reminders.json")
            .exists()
    );
    assert!(workspace.path().join(".workspace-generation").exists());
}

#[tokio::test]
async fn chat_route_exhausts_the_round_budget_and_forces_a_final_answer() {
    let _env = EnvLock::acquire();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    // The model never stops calling tools, so the loop must execute three tool
    // rounds, then disable tools for the remaining turns, mirroring
    // `force_final_answer_without_tools`.
    let tool_turn = |content: &str| {
        json!({
            "id": "chat-budget",
            "model": "deepseek-v4-pro",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [{
                        "id": "call-loop",
                        "type": "function",
                        "function": {"name": "generate_chart", "arguments": "{\"data\":[{\"label\":\"a\",\"value\":1}]}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })
    };
    let scripts = vec![
        tool_turn(""),
        tool_turn(""),
        tool_turn(""),
        tool_turn(""),
        tool_turn("partial answer"),
    ];
    let upstream = stub_upstream_sequence(scripts, requests.clone());
    let url = start_stub(upstream).await;
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "unit-upstream-key"),
    ]);

    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "chart forever"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "generate_chart",
                "description": "render a chart",
                "parameters": {"type": "object"},
            },
        }],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;

    // The oracle returns the last turn's partial content once the budget is
    // spent, rather than failing — the gateway keeps that shape.
    assert_eq!(status, StatusCode::OK, "response: {response}");
    assert_eq!(
        response["choices"][0]["message"]["content"],
        "partial answer"
    );

    let requests = requests.lock().unwrap().clone();
    assert_eq!(
        requests.len(),
        5,
        "max_tool_rounds + 2 turns: three executed rounds, two forced finals"
    );
    // The final request disables tools but keeps the definitions (prefix-cache
    // stability) and carries the budget-exhausted user turn.
    let last = &requests[4];
    assert_eq!(last["tool_choice"], "none");
    assert!(
        last["tools"]
            .as_array()
            .is_some_and(|tools| !tools.is_empty())
    );
    let budget_prompts = last["messages"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|message| {
            message["content"] == json!(deepseek_gateway::tool_rounds::TOOL_BUDGET_EXHAUSTED_PROMPT)
        })
        .count();
    assert_eq!(
        budget_prompts, 1,
        "the prompt is pushed once for the last sent request (the round-4 push never gets POSTed)"
    );
}

#[tokio::test]
async fn chat_route_reports_an_unported_branch_as_did_not_run() {
    let _env = EnvLock::acquire();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let upstream = stub_upstream_sequence(
        vec![
            json!({
                "id": "chat-unported-1",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "fetching that page",
                        "tool_calls": [{
                            "id": "call-fetch-1",
                            "type": "function",
                            "function": {"name": "fetch_url", "arguments": "{\"url\":\"https://example.com/\"}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }),
            json!({
                "id": "chat-unported-2",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "i could not fetch the page"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            }),
        ],
        requests.clone(),
    );
    let url = start_stub(upstream).await;
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "unit-upstream-key"),
    ]);

    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "fetch that page"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "fetch_url",
                "description": "fetch a url",
                "parameters": {"type": "object"},
            },
        }],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;

    assert_eq!(status, StatusCode::OK, "response: {response}");
    let requests = requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 2);
    let messages = requests[1]["messages"].as_array().unwrap();
    let content = messages[2]["content"].as_str().unwrap();
    // The unported branch reports "did not run" rather than a success or a
    // route-level failure — the degradation is visible to the model, and it
    // can still answer around it.
    assert!(content.contains("Tool did not run"), "content: {content}");
    assert!(content.contains("\"tool\":\"fetch_url\""));
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
