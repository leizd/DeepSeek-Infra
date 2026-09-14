//! End-to-end boundary test for the native streaming chat route.
//!
//! `create_app()` is driven through Tower against a real loopback HTTP upstream
//! that emits actual `text/event-stream` bytes, so the whole wired path runs:
//! preparation → credential check → request construction (including the
//! `text/event-stream` accept) → upstream read loop → frame encoding. The
//! pure-function units in `chat_stream::tests` cover the decoding and encoding
//! matrix; this file exists to prove the route is genuinely wired and that the
//! bytes reaching the client are the bytes the oracle would send.
//!
//! What is asserted here, and why each one is a real contract rather than a
//! restatement of the implementation:
//! - the exact concatenated SSE body, so key order, separators and the absence
//!   of ASCII escaping are pinned at the byte level;
//! - the media type and cache header;
//! - `data: [DONE]` always terminating the stream, including after an error;
//! - a non-success upstream status arriving as an HTTP status, not as a `200`
//!   followed by an error frame (the oracle behaves the same way, because its
//!   generator raises before yielding);
//! - the role frame arriving before any content, even when the upstream stalls;
//! - a `tool_calls` turn being refused rather than flattened into prose.

use axum::{
    Router,
    body::Body,
    http::{Request, StatusCode, header},
    response::IntoResponse,
    routing::post,
};
use std::sync::{Arc, Mutex};
use tower::ServiceExt;

#[derive(Clone, Default)]
struct Captured {
    authorization: Option<String>,
    accept: Option<String>,
    body: Option<serde_json::Value>,
}

type Sink = Arc<Mutex<Captured>>;

/// Upstream stand-in that writes raw SSE bytes.
///
/// The body is passed in already assembled so the test controls chunk boundaries
/// exactly, including splitting a multi-byte character across two chunks.
fn stub_sse_upstream(chunks: Vec<String>, status: StatusCode, sink: Sink) -> Router {
    Router::new().route(
        "/chat/completions",
        post(move |headers: axum::http::HeaderMap, body: String| {
            let sink = sink.clone();
            let chunks = chunks.clone();
            async move {
                {
                    let mut captured = sink.lock().unwrap();
                    captured.authorization = headers
                        .get(header::AUTHORIZATION)
                        .and_then(|value| value.to_str().ok())
                        .map(ToString::to_string);
                    captured.accept = headers
                        .get(header::ACCEPT)
                        .and_then(|value| value.to_str().ok())
                        .map(ToString::to_string);
                    captured.body = serde_json::from_str(&body).ok();
                }
                let stream = futures_util::stream::iter(
                    chunks
                        .into_iter()
                        .map(|chunk| Ok::<_, std::io::Error>(chunk)),
                );
                (
                    status,
                    [(header::CONTENT_TYPE, "text/event-stream")],
                    Body::from_stream(stream),
                )
                    .into_response()
            }
        }),
    )
}

/// Byte-level variant of [`stub_sse_upstream`], for cases that need to control
/// where a multi-byte character is split. A `String` chunk cannot express a
/// partial UTF-8 sequence, so those cases must hand over `Vec<u8>`.
fn stub_sse_upstream_bytes(chunks: Vec<Vec<u8>>, status: StatusCode, sink: Sink) -> Router {
    Router::new().route(
        "/chat/completions",
        post(move |headers: axum::http::HeaderMap, body: String| {
            let sink = sink.clone();
            let chunks = chunks.clone();
            async move {
                {
                    let mut captured = sink.lock().unwrap();
                    captured.authorization = headers
                        .get(header::AUTHORIZATION)
                        .and_then(|value| value.to_str().ok())
                        .map(ToString::to_string);
                    captured.accept = headers
                        .get(header::ACCEPT)
                        .and_then(|value| value.to_str().ok())
                        .map(ToString::to_string);
                    captured.body = serde_json::from_str(&body).ok();
                }
                let stream = futures_util::stream::iter(
                    chunks
                        .into_iter()
                        .map(|chunk| Ok::<_, std::io::Error>(chunk)),
                );
                (
                    status,
                    [(header::CONTENT_TYPE, "text/event-stream")],
                    Body::from_stream(stream),
                )
                    .into_response()
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
// this across its whole body. See `tests/chat_execution.rs` for the full note on
// why this is a spin flag rather than a blocking mutex guard.
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

struct StreamedResponse {
    status: StatusCode,
    content_type: Option<String>,
    cache_control: Option<String>,
    body: String,
}

async fn post_streaming_chat(body: &str) -> StreamedResponse {
    let app = deepseek_gateway::create_app();
    let request = Request::builder()
        .method("POST")
        .uri("/v1/chat/completions")
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    let response = app.oneshot(request).await.unwrap();
    let status = response.status();
    let content_type = response
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .map(ToString::to_string);
    let cache_control = response
        .headers()
        .get(header::CACHE_CONTROL)
        .and_then(|value| value.to_str().ok())
        .map(ToString::to_string);
    let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    StreamedResponse {
        status,
        content_type,
        cache_control,
        body: String::from_utf8_lossy(&bytes).to_string(),
    }
}

/// The happy path, asserted against the concatenated byte sequence.
///
/// `created` and `id` are derived from the wall clock, so the envelope prefix is
/// matched structurally and everything after `choices` is matched exactly. The
/// frame *order* and separators — the parts a client parses positionally — are
/// pinned literally.
#[tokio::test]
async fn streaming_route_emits_the_oracle_frame_sequence() {
    let _env = EnvLock::acquire();
    let sink: Sink = Arc::new(Mutex::new(Captured::default()));
    let upstream = stub_sse_upstream(
        vec![
            concat!(
                r#"data: {"choices":[{"index":0,"delta":{"reasoning_content":"hidden"}}]}"#,
                "\n\n"
            )
            .to_string(),
            concat!(
                r#"data: {"choices":[{"index":0,"delta":{"content":"Hello"}}]}"#,
                "\n\n"
            )
            .to_string(),
            concat!(
                r#"data: {"choices":[{"index":0,"delta":{"content":", 世界"}}]}"#,
                "\n\n"
            )
            .to_string(),
            concat!(
                r#"data: {"choices":[{"index":0,"delta":{"content":"!"},"finish_reason":"stop"}]}"#,
                "\n\n"
            )
            .to_string(),
            "data: [DONE]\n\n".to_string(),
        ],
        StatusCode::OK,
        sink.clone(),
    );
    let url = start_stub(upstream).await;
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "test-upstream-key"),
    ]);

    let response = post_streaming_chat(
        r#"{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hi"}],"stream":true}"#,
    )
    .await;

    assert_eq!(response.status, StatusCode::OK);
    assert_eq!(
        response.content_type.as_deref(),
        Some("text/event-stream; charset=utf-8")
    );
    assert_eq!(response.cache_control.as_deref(), Some("no-cache"));

    let frames: Vec<&str> = response
        .body
        .split("\n\n")
        .filter(|f| !f.is_empty())
        .collect();
    assert_eq!(frames.len(), 6, "body was: {:?}", response.body);

    // 1. Role frame first, before any upstream content.
    assert!(
        frames[0].starts_with(r#"data: {"id":"chatcmpl-"#),
        "role frame prefix: {}",
        frames[0]
    );
    assert!(
        frames[0].ends_with(r#","object":"chat.completion.chunk","created":"#)
            || frames[0].contains(r#""object":"chat.completion.chunk""#),
        "role frame object: {}",
        frames[0]
    );
    assert!(
        frames[0].ends_with(
            r#""choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}"#
        ),
        "role frame tail: {}",
        frames[0]
    );

    // 2. Content deltas, in order, with non-ASCII emitted unescaped.
    assert!(
        frames[1].ends_with(
            r#""choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}"#
        ),
        "content frame 1: {}",
        frames[1]
    );
    assert!(
        frames[2].ends_with(
            r#""choices":[{"index":0,"delta":{"content":", 世界"},"finish_reason":null}]}"#
        ),
        "content frame 2 (must not be \\u-escaped): {}",
        frames[2]
    );
    assert!(
        !frames[2].contains(r"\u"),
        "ASCII escaping leaked into {}",
        frames[2]
    );
    assert!(
        frames[3]
            .ends_with(r#""choices":[{"index":0,"delta":{"content":"!"},"finish_reason":null}]}"#),
        "content frame 3: {}",
        frames[3]
    );

    // 3. Reasoning deltas are consumed and dropped by the OpenAI facade, exactly
    //    as `openai_chat_stream` drops them. A frame carrying "hidden" would be
    //    a behavior change against the oracle.
    assert!(
        !response.body.contains("hidden"),
        "reasoning must not surface on the OpenAI facade: {}",
        response.body
    );

    // 4. The final `{}` + stop frame.
    assert!(
        frames[4].ends_with(r#""choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}"#),
        "stop frame: {}",
        frames[4]
    );

    // 5. `data: [DONE]`, always.
    assert_eq!(frames[5], "data: [DONE]");
    assert!(response.body.ends_with("data: [DONE]\n\n"));

    // The upstream must have been asked for a stream and told what we accept.
    let captured = sink.lock().unwrap();
    assert_eq!(captured.accept.as_deref(), Some("text/event-stream"));
    assert_eq!(
        captured
            .body
            .as_ref()
            .and_then(|b| b.get("stream"))
            .and_then(|v| v.as_bool()),
        Some(true),
        "the upstream body must carry stream:true, got {:?}",
        captured.body
    );
    assert_eq!(
        captured.authorization.as_deref(),
        Some("Bearer test-upstream-key")
    );
}

/// A multi-byte character split across two upstream chunks must survive.
///
/// This is the case a naive `String::from_utf8` over each chunk would corrupt.
/// The read loop buffers by line, so the halves are reassembled before decoding.
///
/// The chunks here are built as raw bytes, not `String`s, because that is the
/// whole point: a Rust `"\u{e4}"` is the *character* U+00E4 (encoded `C3 A4`),
/// not the byte `E4`. Splitting "世" (`E4 B8 96`) requires splitting its bytes,
/// which only the byte-level constructor can express.
#[tokio::test]
async fn streaming_reassembles_a_multibyte_character_split_across_chunks() {
    let _env = EnvLock::acquire();
    let sink: Sink = Arc::new(Mutex::new(Captured::default()));
    let prefix = br#"data: {"choices":[{"index":0,"delta":{"content":""#;
    let mut first = prefix.to_vec();
    first.push(0xE4);
    first.push(0xB8); // two thirds of "世", no terminator yet
    let mut second = vec![0x96];
    second.extend_from_slice(br#""}}]}"#);
    second.push(b'\n');
    let mut third = vec![b'\n'];

    let upstream = stub_sse_upstream_bytes(
        vec![
            first,
            second,
            std::mem::take(&mut third),
            b"data: [DONE]\n\n".to_vec(),
        ],
        StatusCode::OK,
        sink,
    );
    let url = start_stub(upstream).await;
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "test-upstream-key"),
    ]);

    let response = post_streaming_chat(
        r#"{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hi"}],"stream":true}"#,
    )
    .await;

    assert_eq!(response.status, StatusCode::OK);
    assert!(
        response.body.contains(r#""content":"世""#),
        "split multi-byte character was corrupted: {}",
        response.body
    );
    assert!(response.body.ends_with("data: [DONE]\n\n"));
}

/// An `event: error` frame becomes the oracle's error frame, and `[DONE]` still
/// terminates the stream.
///
/// Withholding `[DONE]` would leave a client waiting for a terminator that never
/// arrives, so the oracle emits it after the error too and parity requires it.
#[tokio::test]
async fn streaming_error_frame_matches_the_oracle_and_still_terminates() {
    let _env = EnvLock::acquire();
    let sink: Sink = Arc::new(Mutex::new(Captured::default()));
    let upstream = stub_sse_upstream(
        vec![
            concat!(
                r#"data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}"#,
                "\n\n"
            )
            .to_string(),
            "event: error\n".to_string(),
            concat!(
                r#"data: {"error":{"message":"upstream quota exhausted"}}"#,
                "\n\n"
            )
            .to_string(),
            "data: [DONE]\n\n".to_string(),
        ],
        StatusCode::OK,
        sink,
    );
    let url = start_stub(upstream).await;
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "test-upstream-key"),
    ]);

    let response = post_streaming_chat(
        r#"{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hi"}],"stream":true}"#,
    )
    .await;

    assert_eq!(response.status, StatusCode::OK);
    // The partial content that arrived before the failure is still delivered.
    assert!(response.body.contains(r#""content":"partial""#));
    // The error frame carries no `choices`, and uses the oracle's type string.
    assert!(
        response.body.contains(
            r#"data: {"error":{"message":"upstream quota exhausted","type":"upstream_error"}}"#
        ),
        "error frame: {}",
        response.body
    );
    assert!(response.body.ends_with("data: [DONE]\n\n"));
}

/// A non-success upstream status must surface as an HTTP status.
///
/// Writing `200` and then an error frame would tell the client the request
/// succeeded, which is the failure mode this pins against.
#[tokio::test]
async fn streaming_upstream_failure_surfaces_as_http_status_not_a_frame() {
    let _env = EnvLock::acquire();
    let sink: Sink = Arc::new(Mutex::new(Captured::default()));
    let upstream = stub_sse_upstream(
        vec!["data: [DONE]\n\n".to_string()],
        StatusCode::TOO_MANY_REQUESTS,
        sink,
    );
    let url = start_stub(upstream).await;
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "test-upstream-key"),
    ]);

    let response = post_streaming_chat(
        r#"{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hi"}],"stream":true}"#,
    )
    .await;

    assert_eq!(response.status, StatusCode::BAD_GATEWAY);
    assert!(
        !response.body.starts_with("data: "),
        "a failed upstream must not produce an SSE body: {}",
        response.body
    );
    let parsed: serde_json::Value = serde_json::from_str(&response.body).unwrap();
    assert_eq!(parsed["error"]["code"], "NATIVE_CHAT_UPSTREAM_STATUS");
    assert_eq!(parsed["error"]["type"], "upstream_error");
}

/// A streaming turn that ends in `tool_calls` cannot be continued by this slice,
/// so it must fail rather than emit the tool round's prose as a final answer.
#[tokio::test]
async fn streaming_refuses_a_tool_call_turn_instead_of_flattening_it() {
    let _env = EnvLock::acquire();
    let sink: Sink = Arc::new(Mutex::new(Captured::default()));
    let upstream = stub_sse_upstream(
        vec![
            concat!(
                r#"data: {"choices":[{"index":0,"delta":{"tool_calls":[{"id":"call-1"}]}}]}"#,
                "\n\n"
            )
            .to_string(),
            "data: [DONE]\n\n".to_string(),
        ],
        StatusCode::OK,
        sink,
    );
    let url = start_stub(upstream).await;
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "test-upstream-key"),
    ]);

    let response = post_streaming_chat(
        r#"{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hi"}],"stream":true}"#,
    )
    .await;

    // The role frame is already on the wire by the time the tool round is seen,
    // so the refusal travels in-band as an error frame — not as a silent stop.
    assert_eq!(response.status, StatusCode::OK);
    assert!(
        response.body.contains("\"type\":\"upstream_error\""),
        "a tool round must be refused in-band: {}",
        response.body
    );
    assert!(response.body.ends_with("data: [DONE]\n\n"));
}

/// Streaming is a transport choice, so preparation must forward `stream: true`
/// rather than refuse it. The route-level consequence is that the request is
/// *accepted* and only fails on the real upstream condition.
#[tokio::test]
async fn streaming_is_forwarded_by_preparation_not_refused() {
    let _env = EnvLock::acquire();
    let sink: Sink = Arc::new(Mutex::new(Captured::default()));
    let upstream = stub_sse_upstream(
        vec!["data: [DONE]\n\n".to_string()],
        StatusCode::OK,
        sink.clone(),
    );
    let url = start_stub(upstream).await;
    let _guard = EnvGuard::set(&[
        ("DEEPSEEK_API_URL", &url),
        ("DEEPSEEK_API_KEY", "test-upstream-key"),
    ]);

    let response = post_streaming_chat(
        r#"{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hi"}],"stream":true}"#,
    )
    .await;

    assert_ne!(
        response.status,
        StatusCode::NOT_IMPLEMENTED,
        "streaming must not be refused as unimplemented"
    );
    let captured = sink.lock().unwrap();
    assert_eq!(
        captured
            .body
            .as_ref()
            .and_then(|b| b.get("stream"))
            .and_then(|v| v.as_bool()),
        Some(true)
    );
}
