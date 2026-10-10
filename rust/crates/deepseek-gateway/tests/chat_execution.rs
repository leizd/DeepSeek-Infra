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
    /// The workspace root the route binds, kept alive for as long as the variables are set.
    ///
    /// `/v1/chat/completions` composes through `build_deepseek_request` now, so it reads the
    /// memory store, file cache and budget ledger from `DEEPSEEK_INFRA_ROOT`; a case that posts
    /// to it without one gets `500 DEEPSEEK_INFRA_ROOT is not set`. Every case in this file posts
    /// to it, so the guard installs one rather than each test repeating three lines.
    _root: tempfile::TempDir,
}

impl EnvGuard {
    fn set(pairs: &[(&'static str, &str)]) -> Self {
        let _root = tempfile::tempdir().expect("a temp workspace root");
        let root_path = _root
            .path()
            .to_str()
            .expect("a utf-8 temp path")
            .to_string();
        let mut saved = Vec::new();
        for (name, value) in std::iter::once(("DEEPSEEK_INFRA_ROOT", root_path.as_str()))
            .chain(pairs.iter().copied())
        {
            saved.push((name, std::env::var(name).ok()));
            unsafe {
                std::env::set_var(name, value);
            }
        }
        Self { saved, _root }
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
    // The assembled body, not the client's: the catalog's tools (which the client never sent)
    // bring their parallel-call hint as the leading system turn, the client's own turn follows,
    // and the per-turn context arrives as the trailing system message. The thin body this route
    // used to send had none of the three.
    assert!(
        sent["tools"]
            .as_array()
            .is_some_and(|tools| !tools.is_empty()),
        "the catalog tools must be in the body: {sent}"
    );
    assert!(
        sent["messages"][0]["content"]
            .as_str()
            .is_some_and(|content| content.starts_with("当需要多个独立信息时")),
        "expected the parallel-call hint first: {sent}"
    );
    assert_eq!(sent["messages"][1]["role"], "user");
    assert_eq!(sent["messages"][1]["content"], "question");
    assert_eq!(sent["messages"][2]["role"], "system");
    assert!(
        sent["messages"][2]["content"]
            .as_str()
            .is_some_and(|content| content.starts_with("[Per-turn context]")),
        "expected the per-turn context last: {sent}"
    );
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
    // Five turns, not the three the thin body produced: the catalog tools' hint and the per-turn
    // context are the two the assembled body adds ahead of the round's own pair.
    assert_eq!(messages.len(), 5);
    assert_eq!(messages[0]["role"], "system");
    assert_eq!(messages[1]["role"], "user");
    assert_eq!(messages[2]["role"], "system");
    // The assistant turn that requested tools, replayed with its content and
    // reasoning (thinking mode rejects the follow-up without reasoning_content).
    assert_eq!(messages[3]["role"], "assistant");
    assert_eq!(messages[3]["content"], "let me chart that");
    assert_eq!(messages[3]["reasoning_content"], "chart reasoning");
    assert_eq!(messages[3]["tool_calls"][0]["id"], "call-1");
    // The tool result the model reads back: the dispatch envelope, compact JSON.
    assert_eq!(messages[4]["role"], "tool");
    assert_eq!(messages[4]["tool_call_id"], "call-1");
    assert_eq!(messages[4]["name"], "generate_chart");
    let content = messages[4]["content"].as_str().unwrap();
    assert!(content.starts_with("{\"ok\":true"), "content: {content}");
    assert!(content.contains("\"tool\":\"generate_chart\""));
    assert!(content.contains("markdownTable"), "content: {content}");
}

/// The route must not write the memory store, and the **tool loop is a write path too**.
///
/// `forget_memory` deletes through `delete_memories_by_query`, so a model that calls it writes
/// `.memory/memories.json` — a store Python still owns until the `memory` domain is declared and
/// cut over. The turn-level refusal cannot see it: it inspects the *user's* text
/// (`has_explicit_memory_command`), and "forget the dentist thing" is not the command grammar.
#[tokio::test]
async fn chat_route_refuses_the_memory_deleting_tool_instead_of_writing_the_store() {
    let _env = EnvLock::acquire();
    let workspace = tempfile::tempdir().unwrap();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let upstream = stub_upstream_sequence(
        vec![
            json!({
                "id": "chat-mem-1",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call-forget-1",
                            "type": "function",
                            "function": {"name": "forget_memory", "arguments": "{\"query\":\"dentist\"}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }),
            json!({
                "id": "chat-mem-2",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "asked"},
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

    // Seed the store the way Python would, so a delete has something to delete.
    let memory_dir = workspace.path().join(".memory");
    std::fs::create_dir_all(&memory_dir).unwrap();
    let memory_file = memory_dir.join("memories.json");
    std::fs::write(
        &memory_file,
        serde_json::to_string(&json!([{
            "id": "m-dentist",
            "memoryId": "m-dentist",
            "content": "dentist appointment on Thursday",
            "category": "fact",
            "type": "fact",
            "scope": "global",
            "source": "manual",
            "confidence": 0.9,
            "pinned": false,
            "createdAt": "2026-09-18T00:00:00Z",
            "updatedAt": "2026-09-18T00:00:00Z",
        }]))
        .unwrap(),
    )
    .unwrap();

    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "forget the dentist thing"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;
    assert_eq!(status, StatusCode::OK, "response: {response}");
    assert_eq!(response["choices"][0]["message"]["content"], "asked");

    // The tool result the model reads back is a refusal, not a deletion it can claim.
    let requests = requests.lock().unwrap().clone();
    let messages = requests[1]["messages"].as_array().unwrap();
    let tool_turn = messages
        .iter()
        .find(|message| message["role"] == "tool")
        .expect("the round appended a tool result");
    let content = tool_turn["content"].as_str().unwrap();
    assert!(
        content.contains("NATIVE_MEMORY_WRITE_NOT_OWNED"),
        "the tool result must be the ownership refusal: {content}"
    );

    // And the store Python owns is untouched.
    let after = std::fs::read_to_string(&memory_file).unwrap();
    assert!(
        after.contains("dentist appointment on Thursday"),
        "the native route wrote a store Python owns: {after}"
    );
}

/// The mirror of the turn-level refusal: once the mode says Python is de-authorised, the ported write
/// half runs and this route **is** the writer.
///
/// Without this the handover would be half a mechanism — Python mechanically stopped, nothing
/// writing — and the cutover would go from *refusing* to *broken* instead of from refusing to
/// serving.
#[tokio::test]
async fn chat_route_saves_the_memory_once_the_mode_de_authorises_python() {
    let _env = EnvLock::acquire();
    let workspace = tempfile::tempdir().unwrap();
    let sink: Sink = Arc::new(Mutex::new(Captured::default()));
    let upstream = stub_upstream(
        json!({
            "id": "chat-mem-owned-1",
            "model": "deepseek-v4-pro",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "记住了"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }),
        StatusCode::OK,
        sink,
    );
    let url = start_stub(upstream).await;
    let root = workspace.path().to_string_lossy().to_string();
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "unit-upstream-key"),
        ("DEEPSEEK_INFRA_ROOT", &root),
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
    ]);

    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "请帮我记住: 也写进去了"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;
    assert_eq!(status, StatusCode::OK, "response: {response}");

    let memory_file = workspace.path().join(".memory").join("memories.json");
    let stored = std::fs::read_to_string(&memory_file).expect("the route wrote the store");
    assert!(
        stored.contains("也写进去了"),
        "the turn's memory was not saved: {stored}"
    );
}

/// The mirror of the tool-level refusal: same seed, same scripted `forget_memory` call, one mode
/// different — and the tool now deletes instead of answering `NATIVE_MEMORY_WRITE_NOT_OWNED`.
#[tokio::test]
async fn chat_route_runs_the_memory_deleting_tool_once_the_mode_de_authorises_python() {
    let _env = EnvLock::acquire();
    let workspace = tempfile::tempdir().unwrap();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let upstream = stub_upstream_sequence(
        vec![
            json!({
                "id": "chat-mem-owned-2",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call-forget-owned",
                            "type": "function",
                            "function": {"name": "forget_memory", "arguments": "{\"query\":\"dentist\"}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }),
            json!({
                "id": "chat-mem-owned-3",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "forgotten"},
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
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
    ]);

    let memory_dir = workspace.path().join(".memory");
    std::fs::create_dir_all(&memory_dir).unwrap();
    let memory_file = memory_dir.join("memories.json");
    std::fs::write(
        &memory_file,
        serde_json::to_string(&json!([{
            "id": "m-dentist-owned",
            "memoryId": "m-dentist-owned",
            "content": "dentist appointment on Thursday",
            "category": "fact",
            "type": "fact",
            "scope": "global",
            "source": "manual",
            "confidence": 0.9,
            "pinned": false,
            "createdAt": "2026-09-18T00:00:00Z",
            "updatedAt": "2026-09-18T00:00:00Z",
        }]))
        .unwrap(),
    )
    .unwrap();

    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "forget the dentist thing"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;
    assert_eq!(status, StatusCode::OK, "response: {response}");

    let requests = requests.lock().unwrap().clone();
    let messages = requests[1]["messages"].as_array().unwrap();
    let tool_turn = messages
        .iter()
        .find(|message| message["role"] == "tool")
        .expect("the round appended a tool result");
    let content = tool_turn["content"].as_str().unwrap();
    assert!(
        content.contains("\"deleted\":1"),
        "the tool should have deleted the memory: {content}"
    );
    let stored = std::fs::read_to_string(&memory_file).unwrap();
    assert!(
        !stored.contains("dentist appointment on Thursday"),
        "the memory survived the delete: {stored}"
    );
}

/// The reminder tool is refused while Python owns the store, and runs once the mode flips — the same
/// two-sided shape the memory tests use, for the store that needed it more.
///
/// Why this one needed a refusal at all: `reminders_store` was the store the route wrote while it was
/// undeclared. Python writes it from three paths — one of them the *delivery* poll `due_reminders`,
/// which marks `notified` and rewrites the whole file — and both sides reproduce the same temp path
/// (`reminders.json` -> `reminders.tmp`), so two writers could interleave before either replaced it.
/// The declaration it now has is `python -> rust` at 4.9.4, which is the same cutover as
/// `memory_store`, so this case and the memory mirrors flip together and on the same signal.
#[tokio::test]
async fn chat_route_refuses_the_reminder_tool_until_the_store_is_declared_and_the_mode_flips() {
    let _env = EnvLock::acquire();
    let workspace = tempfile::tempdir().unwrap();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let tool_round = || {
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
        })
    };
    let final_turn = || {
        json!({
            "id": "chat-data-2",
            "model": "deepseek-v4-pro",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "reminder set"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14},
        })
    };
    // Two requests, two rounds each: the default mode, then the mode that de-authorises Python.
    let upstream = stub_upstream_sequence(
        vec![tool_round(), final_turn(), tool_round(), final_turn()],
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
    let tool_result = |index: usize| -> String {
        requests.lock().unwrap()[index]["messages"]
            .as_array()
            .unwrap()
            .iter()
            .find(|message| message["role"] == "tool")
            .expect("the round appended a tool result")
            .to_string()
    };
    let refused = tool_result(1);
    assert!(
        refused.contains("NATIVE_REMINDERS_WRITE_NOT_OWNED"),
        "the tool result must be the ownership refusal: {refused}"
    );
    assert!(!refused.contains("\"ok\":true"), "content: {refused}");
    let store = workspace.path().join(".reminders").join("reminders.json");
    assert!(!store.exists(), "a refused create wrote the store");

    // Same body, one mode different: the store is declared, so this is the flip and the tool runs.
    let _mode = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "unit-upstream-key"),
        ("DEEPSEEK_INFRA_ROOT", &root),
        ("DEEPSEEK_RUNTIME_MODE", "python_disabled"),
    ]);
    let (status, response) = post_chat(&body).await;
    assert_eq!(status, StatusCode::OK, "response: {response}");
    let ran = tool_result(3);
    assert!(
        !ran.contains("NATIVE_REMINDERS_WRITE_NOT_OWNED"),
        "a declared store must flip with the mode: {ran}"
    );
    assert!(
        std::fs::read_to_string(&store)
            .unwrap()
            .contains("buy milk"),
        "the created reminder was not stored"
    );
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
async fn chat_route_refuses_a_private_fetch_url_target() {
    let _env = EnvLock::acquire();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let upstream = stub_upstream_sequence(
        vec![
            json!({
                "id": "chat-fetch-ssrf-1",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "fetching that page",
                        "tool_calls": [{
                            "id": "call-fetch-ssrf",
                            "type": "function",
                            "function": {"name": "fetch_url", "arguments": "{\"url\":\"http://127.0.0.1/admin\"}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }),
            json!({
                "id": "chat-fetch-ssrf-2",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "that url is not allowed"},
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
        "messages": [{"role": "user", "content": "fetch http://127.0.0.1/admin"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;
    assert_eq!(status, StatusCode::OK, "response: {response}");
    let requests = requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 2);
    let content = requests[1]["messages"].as_array().unwrap()[4]["content"]
        .as_str()
        .unwrap();
    assert!(
        !content.contains("Tool did not run"),
        "a ported branch must not hide behind did-not-run: {content}"
    );
    assert!(
        content.contains("forbidden") || content.contains("not allowed"),
        "the private target must be refused: {content}"
    );
}

#[tokio::test]
async fn chat_route_searches_cached_files() {
    let _env = EnvLock::acquire();
    let workspace = tempfile::tempdir().expect("workspace");
    let cache = workspace.path().join(".file-cache");
    std::fs::create_dir_all(&cache).unwrap();
    std::fs::write(
        cache.join("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"),
        r#"{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"notes.txt","kind":"text","chunks":[{"index":0,"text":"useMemo caches computed values","lineStart":1,"lineEnd":1}]}"#,
    )
    .unwrap();
    let root = workspace.path().to_str().expect("utf-8 path").to_string();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let upstream = stub_upstream_sequence(
        vec![
            json!({
                "id": "chat-search-1",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "searching notes",
                        "tool_calls": [{
                            "id": "call-search-1",
                            "type": "function",
                            "function": {"name": "search_files", "arguments": "{\"query\":\"useMemo\"}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }),
            json!({
                "id": "chat-search-2",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "found the memo notes"},
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
        ("DEEPSEEK_INFRA_ROOT", &root),
    ]);
    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "find useMemo in my notes"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;
    assert_eq!(status, StatusCode::OK, "response: {response}");
    let requests = requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 2);
    let content = requests[1]["messages"].as_array().unwrap()[4]["content"]
        .as_str()
        .unwrap();
    assert!(
        !content.contains("Tool did not run"),
        "search_files must run: {content}"
    );
    assert!(
        content.contains("useMemo") && content.contains("notes.txt"),
        "the cached file must be retrieved: {content}"
    );
}

#[tokio::test]
async fn chat_route_creates_a_mindmap_svg() {
    let _env = EnvLock::acquire();
    let workspace = tempfile::tempdir().expect("workspace");
    let root = workspace.path().to_str().expect("utf-8 path").to_string();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let upstream = stub_upstream_sequence(
        vec![
            json!({
                "id": "chat-mindmap-1",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "drawing a map",
                        "tool_calls": [{
                            "id": "call-map-1",
                            "type": "function",
                            "function": {"name": "create_mindmap", "arguments": "{\"title\":\"Growth plan\",\"nodes\":[{\"label\":\"Market\",\"children\":[]}]}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }),
            json!({
                "id": "chat-mindmap-2",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "here is the map"},
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
        ("DEEPSEEK_INFRA_ROOT", &root),
    ]);
    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "make a mind map"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;
    assert_eq!(status, StatusCode::OK, "response: {response}");
    let requests = requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 2);
    let content = requests[1]["messages"].as_array().unwrap()[4]["content"]
        .as_str()
        .unwrap();
    assert!(
        !content.contains("Tool did not run"),
        "create_mindmap must run: {content}"
    );
    assert!(
        content.contains("Growth plan") && content.contains("/api/download?id="),
        "the mind map must be stored and linked: {content}"
    );
    let generated = std::fs::read_dir(workspace.path().join(".generated"))
        .unwrap()
        .filter_map(Result::ok)
        .any(|entry| entry.path().extension().and_then(|ext| ext.to_str()) == Some("svg"));
    assert!(generated, "no svg was written under .generated");
}

#[tokio::test]
async fn chat_route_creates_a_docx_document() {
    let _env = EnvLock::acquire();
    let workspace = tempfile::tempdir().expect("workspace");
    let root = workspace.path().to_str().expect("utf-8 path").to_string();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let upstream = stub_upstream_sequence(
        vec![
            json!({
                "id": "chat-doc-1",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "writing a report",
                        "tool_calls": [{
                            "id": "call-doc-1",
                            "type": "function",
                            "function": {"name": "create_document", "arguments": "{\"format\":\"docx\",\"title\":\"季度产品报告\",\"sections\":[{\"heading\":\"概述\",\"body\":[\"背景\"],\"bullets\":[],\"table\":null}]}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }),
            json!({
                "id": "chat-doc-2",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "here is the document"},
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
        ("DEEPSEEK_INFRA_ROOT", &root),
    ]);
    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "write a report"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;
    assert_eq!(status, StatusCode::OK, "response: {response}");
    let requests = requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 2);
    let content = requests[1]["messages"].as_array().unwrap()[4]["content"]
        .as_str()
        .unwrap();
    assert!(
        !content.contains("Tool did not run"),
        "create_document must run: {content}"
    );
    assert!(
        content.contains("季度产品报告") && content.contains("/api/download?id="),
        "the document must be stored and linked: {content}"
    );
    let generated = std::fs::read_dir(workspace.path().join(".generated"))
        .unwrap()
        .filter_map(Result::ok)
        .any(|entry| entry.path().extension().and_then(|ext| ext.to_str()) == Some("docx"));
    assert!(generated, "no docx was written under .generated");
}

#[tokio::test]
async fn chat_route_creates_a_pptx_deck() {
    let _env = EnvLock::acquire();
    let workspace = tempfile::tempdir().expect("workspace");
    let root = workspace.path().to_str().expect("utf-8 path").to_string();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let upstream = stub_upstream_sequence(
        vec![
            json!({
                "id": "chat-ppt-1",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "building a deck",
                        "tool_calls": [{
                            "id": "call-ppt-1",
                            "type": "function",
                            "function": {"name": "create_pptx", "arguments": "{\"title\":\"测试标题\",\"slides\":[{\"title\":\"第一页\",\"bullets\":[\"要点 A\"]}]}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }),
            json!({
                "id": "chat-ppt-2",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "here is the deck"},
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
        ("DEEPSEEK_INFRA_ROOT", &root),
    ]);
    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "make a ppt"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;
    assert_eq!(status, StatusCode::OK, "response: {response}");
    let requests = requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 2);
    let content = requests[1]["messages"].as_array().unwrap()[4]["content"]
        .as_str()
        .unwrap();
    assert!(
        !content.contains("Tool did not run"),
        "create_pptx must run: {content}"
    );
    assert!(
        content.contains("测试标题") && content.contains("/api/download?id="),
        "the deck must be stored and linked: {content}"
    );
    let generated = std::fs::read_dir(workspace.path().join(".generated"))
        .unwrap()
        .filter_map(Result::ok)
        .any(|entry| entry.path().extension().and_then(|ext| ext.to_str()) == Some("pptx"));
    assert!(generated, "no pptx was written under .generated");
}

#[tokio::test]
async fn chat_route_evals_a_python_expression() {
    let _env = EnvLock::acquire();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let upstream = stub_upstream_sequence(
        vec![
            json!({
                "id": "chat-eval-1",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "computing",
                        "tool_calls": [{
                            "id": "call-eval-1",
                            "type": "function",
                            "function": {"name": "python_eval", "arguments": "{\"expression\":\"factorial(6)\"}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }),
            json!({
                "id": "chat-eval-2",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "720"},
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
        "messages": [{"role": "user", "content": "what is 6!"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;
    assert_eq!(status, StatusCode::OK, "response: {response}");
    let requests = requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 2);
    let content = requests[1]["messages"].as_array().unwrap()[4]["content"]
        .as_str()
        .unwrap();
    assert!(
        !content.contains("Tool did not run"),
        "python_eval must run: {content}"
    );
    assert!(
        content.contains("720"),
        "factorial(6) must evaluate: {content}"
    );
}

#[tokio::test]
async fn chat_route_blocks_a_private_browser_url() {
    let _env = EnvLock::acquire();
    let requests: Arc<Mutex<Vec<serde_json::Value>>> = Arc::new(Mutex::new(Vec::new()));
    let upstream = stub_upstream_sequence(
        vec![
            json!({
                "id": "chat-browser-1",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "opening",
                        "tool_calls": [{
                            "id": "call-browser-1",
                            "type": "function",
                            "function": {"name": "browser_open_url", "arguments": "{\"url\":\"http://127.0.0.1:8000/admin\"}"},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }),
            json!({
                "id": "chat-browser-2",
                "model": "deepseek-v4-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "blocked"},
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
        ("BROWSER_CONTROL_ENABLED", "1"),
    ]);
    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "open that admin page"}],
    })
    .to_string();
    let (status, response) = post_chat(&body).await;
    assert_eq!(status, StatusCode::OK, "response: {response}");
    let requests = requests.lock().unwrap().clone();
    assert_eq!(requests.len(), 2);
    let content = requests[1]["messages"].as_array().unwrap()[4]["content"]
        .as_str()
        .unwrap();
    assert!(
        !content.contains("Tool did not run"),
        "browser_open_url must run: {content}"
    );
    assert!(
        content.contains("forbidden") || content.contains("unsafe_url"),
        "private hosts must be blocked: {content}"
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
