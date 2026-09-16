//! Native streaming chat execution, served as OpenAI `chat.completion.chunk` SSE.
//!
//! Scope: the streaming branch of `POST /v1/chat/completions`, which the Python
//! oracle serves through `openai_api.openai_chat_stream` over the internal event
//! stream produced by `deepseek_client.stream_deepseek`.
//!
//! This module owns two things, and the split matters:
//!
//! 1. **Upstream SSE decoding** ([`decode_event`]) — parse the provider's
//!    `data:`/`event:` frames into the delta vocabulary the oracle forwards.
//! 2. **Downstream SSE encoding** ([`StreamChunkEncoder`]) — re-emit those
//!    deltas in the byte-exact `chat.completion.chunk` envelope
//!    `openai_api._sse` produces.
//!
//! The oracle's *event vocabulary* is wider than what this module reproduces.
//! `stream_deepseek` also emits `system_note`, `search`, `memory_suggestion`,
//! agent-plan events, and runs tool rounds, semantic cache lookups, memory
//! retrieval and context compression. Those stay on the Python path and are
//! reported as explicit blockers; this module forwards only the two delta kinds
//! the OpenAI facade actually consumes (`reasoning`, `content`) plus termination
//! and errors.
//!
//! Deliberately NOT implemented (each remains a reported blocker, never a silent
//! behavior change):
//! - tool-call rounds (`append_tool_exchange`), web search, `create_pptx`
//! - semantic cache, memory retrieval, context compression, model router
//! - scheduler leases, resiliency retries, trace/span emission, budget ledger
//! - the `system_note`/`search`/`memory_suggestion` event kinds, which the
//!   OpenAI facade drops anyway but `/api/chat` (NDJSON) does surface
//!
//! A streaming response whose upstream turn contains `tool_calls` is refused
//! with `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` rather than emitting the tool-call
//! round's prose as if it were the final answer, which would be a silent
//! behavior change against the oracle. The *non-streaming* path runs the same
//! rounds through `chat_tool_loop` now; continuing them mid-stream (where the
//! round's frames interleave with the SSE emission the oracle interleaves them
//! with) is the streaming slice's own seam.

use axum::body::{Body, Bytes};
use axum::http::header;
use axum::response::{IntoResponse, Response};
use futures_util::StreamExt;
use serde_json::{Value, json};

/// The refusal code for a streaming tool round: the one shape this transport
/// still cannot continue. Kept as a literal so the wire contract is visible at
/// the emission site.
const STREAM_TOOL_ROUNDS_NOT_READY: &str = "NATIVE_CHAT_TOOL_ROUNDS_NOT_READY";

/// One decoded upstream delta, in the vocabulary the OpenAI facade forwards.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UpstreamDelta {
    /// `delta.reasoning_content` / `reasoning` / `thinking_content` / `thinking`.
    ///
    /// The oracle reads all four spellings in that precedence order; mirroring
    /// the aliases matters because a provider that renames the field would
    /// otherwise lose reasoning text silently.
    Reasoning(String),
    /// `delta.content`.
    Content(String),
    /// `id` carried by any chunk; the oracle keeps the last one seen.
    ResponseId(String),
    /// `model` carried by any chunk.
    Model(String),
    /// Terminal marker `data: [DONE]`.
    Done,
    /// An upstream `event: error` frame, already humanized into a message.
    Error { message: String },
    /// The chunk announced `delta.tool_calls`.
    ///
    /// Surfaced as its own delta rather than swallowed because a turn that ends
    /// in tool calls is a *round* the oracle continues with `append_tool_exchange`,
    /// and this slice cannot continue it. Detecting it here is what lets the
    /// stream fail loudly instead of emitting the round's prose as the answer.
    ToolCalls,
}

/// Decode one upstream SSE line into a delta, or `None` when the line carries
/// no delta (blank separator, comment, unknown field, malformed JSON).
///
/// Mirrors `stream_deepseek`'s loop: blank lines reset the event name, `event:`
/// sets it, anything that is not `data:` is skipped, and malformed JSON is
/// logged and skipped rather than aborting the stream.
///
/// `event_name` is threaded through as `(&mut String)` because the SSE spec
/// makes it sticky across `data:` lines until the next blank line.
pub fn decode_event(line: &str, event_name: &mut String) -> Option<UpstreamDelta> {
    let line = line.trim_end_matches(['\r', '\n']);
    if line.is_empty() {
        *event_name = "message".to_string();
        return None;
    }
    if let Some(rest) = line.strip_prefix("event:") {
        let name = rest.trim();
        *event_name = if name.is_empty() {
            "message".to_string()
        } else {
            name.to_string()
        };
        return None;
    }
    let payload = line.strip_prefix("data:")?;
    let payload = payload.trim();
    if payload == "[DONE]" {
        return Some(UpstreamDelta::Done);
    }
    // The oracle routes `event: error` frames through `sse_error_message` +
    // `humanize_upstream_error`; extracting the provider's own message is the
    // part that matters for parity, since the humanization text is not a
    // parity surface.
    if *event_name == "error" {
        return Some(UpstreamDelta::Error {
            message: upstream_error_message(payload),
        });
    }
    let Ok(chunk) = serde_json::from_str::<Value>(payload) else {
        return None;
    };
    decode_chunk(&chunk)
}

/// Extract the provider's error text from an SSE error frame.
///
/// Mirrors `sse_error_message`: prefer `error.message`, then `error.type`, then
/// a top-level `message`/`type`, then the raw frame.
pub fn upstream_error_message(payload: &str) -> String {
    let Ok(parsed) = serde_json::from_str::<Value>(payload) else {
        return if payload.is_empty() {
            "Upstream stream error".to_string()
        } else {
            payload.to_string()
        };
    };
    let error = parsed.get("error");
    if let Some(message) = error
        .and_then(|value| value.get("message"))
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
    {
        return message.to_string();
    }
    if let Some(kind) = error
        .and_then(|value| value.get("type"))
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
    {
        return kind.to_string();
    }
    for key in ["message", "type"] {
        if let Some(text) = parsed
            .get(key)
            .and_then(Value::as_str)
            .filter(|text| !text.is_empty())
        {
            return text.to_string();
        }
    }
    parsed.to_string()
}

/// Translate one decoded upstream chunk into zero or more deltas.
///
/// Returns `None` for a chunk carrying no forwarded delta (e.g. a
/// usage-only final frame).
fn decode_chunk(chunk: &Value) -> Option<UpstreamDelta> {
    let object = chunk.as_object()?;
    // A tool-call round is checked first: the round may also carry a `content`
    // or `reasoning` field, and forwarding any of it would emit a partial answer
    // that the model never meant as final.
    if chunk_has_tool_calls(chunk) {
        return Some(UpstreamDelta::ToolCalls);
    }
    if let Some(id) = object.get("id").and_then(Value::as_str) {
        if !id.is_empty() {
            return Some(UpstreamDelta::ResponseId(id.to_string()));
        }
    }
    if let Some(model) = object.get("model").and_then(Value::as_str) {
        if !model.is_empty() {
            return Some(UpstreamDelta::Model(model.to_string()));
        }
    }
    let delta = object
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|choices| choices.first())
        .and_then(|choice| choice.get("delta"))?;
    if let Some(text) = delta
        .get("reasoning_content")
        .or_else(|| delta.get("reasoning"))
        .or_else(|| delta.get("thinking_content"))
        .or_else(|| delta.get("thinking"))
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
    {
        return Some(UpstreamDelta::Reasoning(text.to_string()));
    }
    if let Some(text) = delta
        .get("content")
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
    {
        return Some(UpstreamDelta::Content(text.to_string()));
    }
    None
}

/// Does this upstream chunk announce tool calls?
///
/// Checked before forwarding anything from the chunk, because a turn that ends
/// in `tool_calls` is a round the oracle continues with `append_tool_exchange`,
/// and this slice cannot. Detecting it lets the stream fail loudly instead of
/// emitting a partial answer.
pub fn chunk_has_tool_calls(chunk: &Value) -> bool {
    chunk
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|choices| choices.first())
        .and_then(|choice| choice.get("delta"))
        .and_then(|delta| delta.get("tool_calls"))
        .and_then(Value::as_array)
        .is_some_and(|calls| !calls.is_empty())
}

/// `finish_reason` on this upstream chunk, if any.
pub fn chunk_finish_reason(chunk: &Value) -> Option<String> {
    chunk
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|choices| choices.first())
        .and_then(|choice| choice.get("finish_reason"))
        .and_then(Value::as_str)
        .filter(|reason| !reason.is_empty())
        .map(str::to_string)
}

/// Byte-exact re-implementation of `openai_api.openai_chat_stream`'s envelope.
///
/// The oracle writes `b"data: " + compact_json + b"\n\n"` with
/// `ensure_ascii=False`. `serde_json` already emits non-ASCII unescaped, so the
/// only thing to preserve is the compact separator form — `serde_json::to_string`
/// is compact by default, unlike `to_string_pretty`.
#[derive(Debug)]
pub struct StreamChunkEncoder {
    completion_id: String,
    created: i64,
    model: String,
}

impl StreamChunkEncoder {
    pub fn new(completion_id: String, created: i64, model: String) -> Self {
        Self {
            completion_id,
            created,
            model,
        }
    }

    pub fn model(&self) -> &str {
        &self.model
    }

    /// Wrap one `delta` object in the `chat.completion.chunk` envelope.
    ///
    /// The key order is built explicitly rather than via `json!`, because the
    /// parity target is the *bytes* `openai_api._sse` writes, and Python's
    /// `json.dumps` emits keys in insertion order (`id`, `object`, `created`,
    /// `model`, `choices`) while this crate compiles `serde_json` **without**
    /// `preserve_order`, so its maps are key-sorted. Enabling `preserve_order`
    /// workspace-wide would silently reorder every other Rust response, so the
    /// ordering is pinned here, at the one place a byte-exact frame is required.
    pub fn chunk(&self, delta: Value, finish_reason: Option<&str>) -> Vec<u8> {
        let mut envelope = String::with_capacity(160);
        envelope.push('{');
        push_json_field(
            &mut envelope,
            "id",
            &Value::String(self.completion_id.clone()),
            true,
        );
        push_json_field(
            &mut envelope,
            "object",
            &Value::String("chat.completion.chunk".to_string()),
            false,
        );
        push_json_field(&mut envelope, "created", &Value::from(self.created), false);
        push_json_field(
            &mut envelope,
            "model",
            &Value::String(self.model.clone()),
            false,
        );
        envelope.push_str(",\"choices\":[{\"index\":0,\"delta\":");
        envelope.push_str(&serde_json::to_string(&delta).expect("serializing a Value cannot fail"));
        envelope.push_str(",\"finish_reason\":");
        match finish_reason {
            Some(reason) => {
                envelope.push_str(
                    &serde_json::to_string(reason).expect("serializing a &str cannot fail"),
                );
            }
            None => envelope.push_str("null"),
        }
        envelope.push_str("}]}");
        sse_frame_json(&envelope)
    }

    /// The opening frame. The oracle always emits this first, before reading any
    /// upstream delta, so a client can rely on the assistant role arriving
    /// before any content.
    pub fn role_frame(&self) -> Vec<u8> {
        self.chunk(json!({"role": "assistant"}), None)
    }

    /// The terminating `data: [DONE]` frame.
    pub fn done_frame(&self) -> Vec<u8> {
        b"data: [DONE]\n\n".to_vec()
    }

    /// Content delta frame.
    pub fn content_frame(&self, text: &str) -> Vec<u8> {
        self.chunk(json!({"content": text}), None)
    }

    /// The final `{}` + `finish_reason: "stop"` frame.
    pub fn stop_frame(&self) -> Vec<u8> {
        self.chunk(json!({}), Some("stop"))
    }

    /// Error frame, matching `openai_api`'s shape: no `choices`, a top-level
    /// `error` object with `message` and `type`.
    pub fn error_frame(message: &str) -> Vec<u8> {
        sse_frame(&json!({
            "error": {
                "message": message,
                "type": "upstream_error",
            }
        }))
    }
}

/// `b"data: " + compact_json + b"\n\n"`.
fn sse_frame(value: &Value) -> Vec<u8> {
    let mut out = Vec::with_capacity(64);
    out.extend_from_slice(b"data: ");
    // `serde_json::to_writer` is compact and never escapes non-ASCII, matching
    // `json.dumps(..., ensure_ascii=False, separators=(",", ":"))`.
    serde_json::to_writer(&mut out, value).expect("serializing a Value cannot fail");
    out.extend_from_slice(b"\n\n");
    out
}

/// `b"data: " + pre-rendered_json + b"\n\n"`, for frames whose key order is
/// pinned by hand.
fn sse_frame_json(compact_json: &str) -> Vec<u8> {
    let mut out = Vec::with_capacity(compact_json.len() + 8);
    out.extend_from_slice(b"data: ");
    out.extend_from_slice(compact_json.as_bytes());
    out.extend_from_slice(b"\n\n");
    out
}

/// Append `"key":value` to a hand-built JSON object, inserting a comma when the
/// object is not empty.
fn push_json_field(out: &mut String, key: &str, value: &Value, first: bool) {
    if !first {
        out.push(',');
    }
    out.push_str(&serde_json::to_string(key).expect("serializing a &str cannot fail"));
    out.push(':');
    out.push_str(&serde_json::to_string(value).expect("serializing a Value cannot fail"));
}

/// Build the downstream SSE response for an already-opened upstream stream.
///
/// The frame sequence is exactly `openai_api.openai_chat_stream`'s:
///
/// 1. `{"role":"assistant"}` with `finish_reason: null`
/// 2. zero or more `{"content": ...}` deltas
/// 3. `{}` with `finish_reason: "stop"`
/// 4. `data: [DONE]`
///
/// with the error frame replacing 3–4 when the upstream fails mid-stream. The
/// role frame is emitted *before* the first upstream read, matching the oracle,
/// so a client can rely on the assistant role arriving first even if the
/// provider stalls.
///
/// Backpressure is real, not nominal: the body is an `async_stream` generator
/// that only advances when the consumer polls it, so an upstream chunk is read
/// only when the previous frames have been handed downstream. Memory therefore
/// stays O(one line), not O(response).
///
/// The `[DONE]` frame is *always* emitted — including after an error — because
/// the oracle's generator reaches its `yield b"data: [DONE]\n\n"` statement
/// after `break`-ing out of the error path too. Withholding it would make a
/// client wait for a terminator that never arrives.
pub fn streaming_response(upstream: reqwest::Response, model: &str, created: i64) -> Response {
    let encoder =
        StreamChunkEncoder::new(format!("chatcmpl-{created}"), created, model.to_string());
    let body_stream = async_stream::stream! {
        yield Ok::<Bytes, std::io::Error>(Bytes::from(encoder.role_frame()));
        let mut pending = Vec::<u8>::new();
        let mut event_name = String::from("message");
        let mut finished = false;
        let mut stream = upstream.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let chunk = match chunk {
                Ok(chunk) => chunk,
                Err(_) => {
                    // A transport failure after the headers is not an upstream
                    // status; report it in-band and stop, as the oracle does.
                    yield Ok(Bytes::from(StreamChunkEncoder::error_frame(
                        "Upstream stream error",
                    )));
                    finished = true;
                    break;
                }
            };
            pending.extend_from_slice(&chunk);
            // SSE frames are newline-delimited. Decode with `from_utf8_lossy`
            // over whole lines; a multi-byte character split across two chunks
            // is reassembled by the buffering, since only complete lines are
            // consumed here.
            while let Some(newline) = pending.iter().position(|byte| *byte == b'\n') {
                let line_bytes: Vec<u8> = pending.drain(..=newline).collect();
                let line = String::from_utf8_lossy(&line_bytes);
                if let Some(frame) = forward_line(&line, &encoder, &mut event_name, &mut finished) {
                    yield Ok(Bytes::from(frame));
                }
            }
            if finished {
                break;
            }
        }
        if !finished {
            yield Ok(Bytes::from(encoder.stop_frame()));
        }
        yield Ok(Bytes::from(encoder.done_frame()));
    };
    sse_headers(Body::from_stream(body_stream))
}

/// Decode one upstream line and, when it carries a forwardable delta, return the
/// downstream frame for it.
///
/// Returns `None` for lines that produce no frame (heartbeats, unknown events,
/// `response_id`/`model` bookkeeping, malformed JSON). Sets `*finished` when the
/// upstream signalled completion or failure, so the caller stops reading.
fn forward_line(
    line: &str,
    encoder: &StreamChunkEncoder,
    event_name: &mut String,
    finished: &mut bool,
) -> Option<Vec<u8>> {
    match decode_event(line, event_name)? {
        UpstreamDelta::Reasoning(_) => {
            // The OpenAI facade does not surface reasoning as a separate channel
            // — `openai_chat_stream` only maps `content`. Reasoning deltas are
            // therefore consumed and dropped here, exactly as the facade drops
            // them. `/api/chat` (NDJSON) is what surfaces them, and that route
            // is out of scope for this slice.
            None
        }
        UpstreamDelta::Content(text) => Some(encoder.content_frame(&text)),
        UpstreamDelta::ResponseId(_) | UpstreamDelta::Model(_) => {
            // Bookkeeping the envelope does not carry through: the oracle emits
            // its own id/model in every chunk. Consumed, not forwarded.
            None
        }
        UpstreamDelta::Done => {
            *finished = true;
            Some(encoder.stop_frame())
        }
        UpstreamDelta::Error { message } => {
            *finished = true;
            Some(StreamChunkEncoder::error_frame(&message))
        }
        UpstreamDelta::ToolCalls => {
            // This slice cannot run the tool round the oracle would run next, and
            // emitting the round's prose as the final answer would be a silent
            // behavior change. Fail loudly; the non-streaming loop in
            // `chat_tool_loop` *can* continue the round, so the code says which
            // transport is not ready rather than that the capability is missing.
            *finished = true;
            Some(StreamChunkEncoder::error_frame(
                STREAM_TOOL_ROUNDS_NOT_READY,
            ))
        }
    }
}

/// SSE headers matching the oracle's streaming response.
fn sse_headers(body: Body) -> Response {
    (
        [
            (header::CONTENT_TYPE, "text/event-stream; charset=utf-8"),
            (header::CACHE_CONTROL, "no-cache"),
        ],
        body,
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn envelope_is_byte_identical_to_the_python_frame_shape() {
        let encoder =
            StreamChunkEncoder::new("chatcmpl-test".to_string(), 1_700_000_000, "m".into());
        let frame = encoder.role_frame();
        assert_eq!(
            String::from_utf8(frame).unwrap(),
            "data: {\"id\":\"chatcmpl-test\",\"object\":\"chat.completion.chunk\",\"created\":1700000000,\
\"model\":\"m\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\"},\"finish_reason\":null}]}\n\n"
        );
    }

    #[test]
    fn done_frame_is_literal_data_done() {
        let encoder = StreamChunkEncoder::new("id".into(), 0, "m".into());
        assert_eq!(encoder.done_frame(), b"data: [DONE]\n\n");
    }

    #[test]
    fn non_ascii_content_is_not_escaped() {
        let encoder = StreamChunkEncoder::new("id".into(), 0, "m".into());
        let frame = String::from_utf8(encoder.content_frame("你好")).unwrap();
        assert!(
            frame.contains("你好"),
            "content must be emitted as UTF-8, got {frame}"
        );
        assert!(
            !frame.contains("\\u"),
            "ensure_ascii=False parity violated: {frame}"
        );
    }

    #[test]
    fn error_frame_carries_no_choices() {
        let frame = String::from_utf8(StreamChunkEncoder::error_frame("boom")).unwrap();
        assert!(frame.starts_with("data: {"), "got {frame}");
        assert!(frame.contains("\"type\":\"upstream_error\""));
        assert!(
            !frame.contains("choices"),
            "error frames have no choices: {frame}"
        );
    }

    #[test]
    fn parses_content_and_reasoning_deltas() {
        let mut name = "message".to_string();
        assert_eq!(
            decode_event(
                "data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}",
                &mut name
            ),
            Some(UpstreamDelta::Content("hi".to_string()))
        );
        assert_eq!(
            decode_event(
                "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"why\"}}]}",
                &mut name
            ),
            Some(UpstreamDelta::Reasoning("why".to_string()))
        );
    }

    #[test]
    fn reasoning_aliases_are_all_read() {
        for key in [
            "reasoning_content",
            "reasoning",
            "thinking_content",
            "thinking",
        ] {
            let mut name = "message".to_string();
            let line = format!("data: {{\"choices\":[{{\"delta\":{{\"{key}\":\"x\"}}}}]}}");
            assert_eq!(
                decode_event(&line, &mut name),
                Some(UpstreamDelta::Reasoning("x".to_string())),
                "alias {key} must be read"
            );
        }
    }

    #[test]
    fn done_marker_and_blank_line_event_reset() {
        let mut name = "error".to_string();
        assert_eq!(decode_event("", &mut name), None);
        assert_eq!(name, "message", "a blank line resets the event name");
        assert_eq!(
            decode_event("data: [DONE]", &mut name),
            Some(UpstreamDelta::Done)
        );
    }

    #[test]
    fn malformed_json_is_skipped_not_aborted() {
        let mut name = "message".to_string();
        assert_eq!(decode_event("data: {not json", &mut name), None);
    }

    #[test]
    fn non_data_lines_are_ignored() {
        let mut name = "message".to_string();
        assert_eq!(decode_event(": keep-alive comment", &mut name), None);
        assert_eq!(decode_event("id: 42", &mut name), None);
    }

    #[test]
    fn error_event_extracts_provider_message_with_precedence() {
        assert_eq!(
            upstream_error_message(r#"{"error":{"message":"quota exhausted"}}"#),
            "quota exhausted"
        );
        assert_eq!(
            upstream_error_message(r#"{"error":{"type":"content_filter"}}"#),
            "content_filter"
        );
        assert_eq!(
            upstream_error_message(r#"{"message":"try later"}"#),
            "try later"
        );
        assert_eq!(
            upstream_error_message(r#"{"type":"overloaded"}"#),
            "overloaded"
        );
        assert_eq!(
            upstream_error_message(r#"{"other":true}"#),
            r#"{"other":true}"#
        );
        assert_eq!(upstream_error_message(""), "Upstream stream error");
    }

    #[test]
    fn error_event_is_only_read_under_the_error_event_name() {
        let mut name = "message".to_string();
        let line = r#"data: {"error":{"message":"boom"}}"#;
        // Under `message`, an `error` field is not the SSE error channel.
        assert_eq!(decode_event(line, &mut name), None);
        let mut name = "error".to_string();
        assert_eq!(
            decode_event(line, &mut name),
            Some(UpstreamDelta::Error {
                message: "boom".to_string()
            })
        );
    }

    #[test]
    fn tool_calls_are_detected_so_the_stream_can_refuse() {
        let chunk: Value =
            serde_json::from_str(r#"{"choices":[{"delta":{"tool_calls":[{"id":"c1"}]}}]}"#)
                .unwrap();
        assert!(chunk_has_tool_calls(&chunk));
        let plain: Value =
            serde_json::from_str(r#"{"choices":[{"delta":{"content":"hi"}}]}"#).unwrap();
        assert!(!chunk_has_tool_calls(&plain));
    }

    /// A tool round that also carries content must still be classified as a tool
    /// round, not as content: forwarding the prose is the silent-flattening
    /// failure the refusal exists to prevent.
    #[test]
    fn a_tool_call_chunk_is_typed_as_tool_calls_even_with_content() {
        let mut name = "message".to_string();
        assert_eq!(
            decode_event(
                r#"data: {"choices":[{"delta":{"content":"let me check","tool_calls":[{"id":"c1"}]}}]}"#,
                &mut name
            ),
            Some(UpstreamDelta::ToolCalls)
        );
    }

    #[test]
    fn finish_reason_is_extracted_only_when_present() {
        let chunk: Value =
            serde_json::from_str(r#"{"choices":[{"delta":{},"finish_reason":"length"}]}"#).unwrap();
        assert_eq!(chunk_finish_reason(&chunk).as_deref(), Some("length"));
        let plain: Value =
            serde_json::from_str(r#"{"choices":[{"delta":{},"finish_reason":null}]}"#).unwrap();
        assert_eq!(chunk_finish_reason(&plain), None);
    }

    #[test]
    fn response_id_and_model_are_surfaced_from_the_wire() {
        let mut name = "message".to_string();
        assert_eq!(
            decode_event(r#"data: {"id":"chatcmpl-1"}"#, &mut name),
            Some(UpstreamDelta::ResponseId("chatcmpl-1".to_string()))
        );
        assert_eq!(
            decode_event(r#"data: {"model":"deepseek-v4-pro"}"#, &mut name),
            Some(UpstreamDelta::Model("deepseek-v4-pro".to_string()))
        );
    }
}
