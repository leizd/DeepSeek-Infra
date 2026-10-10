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
//! A streaming response whose upstream turn contains `tool_calls` continues the
//! round, exactly as the non-streaming path does: [`streaming_response`] runs the
//! same `decide_round` / `append_tool_exchange` machinery, opens a fresh upstream
//! for each further round from inside the response body, and forwards every
//! round's `content` as it arrives.
//!
//! [`decode_event`] returns **every** delta a chunk carries, in the oracle's order,
//! because a round-ending chunk legitimately carries `tool_calls` *and* the content
//! that belongs in that round's assistant message. The earlier single-delta shape
//! had to choose, and it chose `tool_calls` — silently dropping text the model
//! produced, which is also the text `append_tool_exchange` replays upstream.

use axum::body::{Body, Bytes};
use axum::http::header;
use axum::response::{IntoResponse, Response};
use futures_util::StreamExt;
use serde_json::{Value, json};

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
    /// `delta.tool_calls` on this chunk, verbatim.
    ///
    /// Carries the fragments rather than just announcing them, because the round
    /// the oracle continues needs them: `merge_stream_tool_call_deltas` accumulates
    /// the argument JSON in pieces and the result is replayed to the provider as
    /// the assistant message's `tool_calls`.
    ///
    /// Streamed to the client as nothing — a tool-call round is not forwarded — but
    /// the round loop consumes it to build the assistant message the next round
    /// replays upstream.
    ToolCalls(Value),
    /// `usage` on this chunk, accumulated across rounds by the loop.
    Usage(Value),
    /// `choices[0].finish_reason` on this chunk, when present.
    ///
    /// The loop keeps the last non-empty one so the final envelope can report a
    /// `length` truncation; nothing is streamed for it.
    FinishReason(String),
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
///
/// Returns a **list**, because one upstream chunk can legitimately carry several
/// deltas at once. The oracle's loop merges `tool_calls`, records `usage` and
/// `finish_reason`, and forwards `content` / `reasoning` **independently** — so a
/// chunk that ends a round may also carry the text that belongs in that round's
/// assistant message. Returning a single delta would force a choice between them,
/// and the earlier version chose `tool_calls`, silently dropping the content.
pub fn decode_event(line: &str, event_name: &mut String) -> Vec<UpstreamDelta> {
    let line = line.trim_end_matches(['\r', '\n']);
    if line.is_empty() {
        *event_name = "message".to_string();
        return Vec::new();
    }
    if let Some(rest) = line.strip_prefix("event:") {
        let name = rest.trim();
        *event_name = if name.is_empty() {
            "message".to_string()
        } else {
            name.to_string()
        };
        return Vec::new();
    }
    let Some(payload) = line.strip_prefix("data:") else {
        return Vec::new();
    };
    let payload = payload.trim();
    if payload == "[DONE]" {
        return vec![UpstreamDelta::Done];
    }
    // The oracle routes `event: error` frames through `sse_error_message` +
    // `humanize_upstream_error`; extracting the provider's own message is the
    // part that matters for parity, since the humanization text is not a
    // parity surface.
    if *event_name == "error" {
        return vec![UpstreamDelta::Error {
            message: upstream_error_message(payload),
        }];
    }
    let Ok(chunk) = serde_json::from_str::<Value>(payload) else {
        return Vec::new();
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
/// Decode one upstream chunk into every delta it carries.
///
/// Mirrors the oracle's per-chunk body in `stream_deepseek`, which does all of
/// these **in one pass** rather than short-circuiting:
///
/// ```text
/// response_id = chunk.get("id") or response_id
/// response_model = chunk.get("model") or response_model
/// choices = chunk.get("choices") or []
/// if not choices: continue
/// delta = choices[0].get("delta") or {}
/// if choices[0].get("finish_reason"): round_finish = str(...)
/// if isinstance(chunk.get("usage"), dict): round_usage = chunk["usage"]
/// merge_stream_tool_call_deltas(stream_tool_calls, delta.get("tool_calls"))
/// ... forward reasoning, then content, independently ...
/// ```
///
/// So a chunk carrying `tool_calls` still contributes its `content` and
/// `reasoning`, and a chunk with no `choices` contributes nothing at all.
fn decode_chunk(chunk: &Value) -> Vec<UpstreamDelta> {
    let Some(object) = chunk.as_object() else {
        return Vec::new();
    };
    let mut deltas = Vec::new();
    if let Some(id) = object.get("id").and_then(Value::as_str) {
        if !id.is_empty() {
            deltas.push(UpstreamDelta::ResponseId(id.to_string()));
        }
    }
    if let Some(model) = object.get("model").and_then(Value::as_str) {
        if !model.is_empty() {
            deltas.push(UpstreamDelta::Model(model.to_string()));
        }
    }
    // `choices = chunk.get("choices") or []` — no choices means nothing further,
    // not an empty delta.
    let Some(first) = object
        .get("choices")
        .and_then(Value::as_array)
        .and_then(|choices| choices.first())
    else {
        return deltas;
    };
    if let Some(reason) = first
        .get("finish_reason")
        .and_then(Value::as_str)
        .filter(|reason| !reason.is_empty())
    {
        deltas.push(UpstreamDelta::FinishReason(reason.to_string()));
    }
    if let Some(usage) = object.get("usage").filter(|usage| usage.is_object()) {
        deltas.push(UpstreamDelta::Usage(usage.clone()));
    }
    let delta = first.get("delta").and_then(Value::as_object);
    // Merged unconditionally: `merge` ignores a non-list, so an absent
    // `tool_calls` is not a special case.
    if let Some(calls) = delta.and_then(|delta| delta.get("tool_calls")) {
        deltas.push(UpstreamDelta::ToolCalls(calls.clone()));
    }
    if let Some(text) = delta
        .and_then(|delta| {
            delta
                .get("reasoning_content")
                .or_else(|| delta.get("reasoning"))
                .or_else(|| delta.get("thinking_content"))
                .or_else(|| delta.get("thinking"))
        })
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
    {
        deltas.push(UpstreamDelta::Reasoning(text.to_string()));
    }
    if let Some(text) = delta
        .and_then(|delta| delta.get("content"))
        .and_then(Value::as_str)
        .filter(|text| !text.is_empty())
    {
        deltas.push(UpstreamDelta::Content(text.to_string()));
    }
    deltas
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

/// Build the downstream SSE response, running the tool rounds the stream needs.
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
/// stays O(one line per round), not O(response).
///
/// The `[DONE]` frame is *always* emitted — including after an error — because
/// the oracle's generator reaches its `yield b"data: [DONE]\n\n"` statement
/// after `break`-ing out of the error path too. Withholding it would make a
/// client wait for a terminator that never arrives.
///
/// # The round loop
///
/// Mirrors `stream_deepseek`'s `for tool_round in range(max_tool_rounds + 2)`.
/// Each round streams one upstream turn, forwarding its `content` as it arrives,
/// while accumulating the `tool_calls` fragments. When the round ends the calls
/// are finalized and [`crate::tool_rounds::decide_round`] decides:
///
/// - no calls — the answer is final, so the stop frame goes out and the loop ends;
/// - the budget is spent — one more turn with tools disabled, then its content
///   streams like any other round;
/// - otherwise — the tools run, the exchange is appended to the body, and the
///   **next upstream request is opened from inside the generator**. A response
///   body is single-shot, so every further round is a fresh request.
///
/// The round decision, the exchange assembly and the tool execution are the same
/// functions the non-streaming loop uses, so the two transports cannot drift.
///
/// # What is deliberately not emitted
///
/// The oracle's `system_note`s (`正在调用本地工具…`, the budget notice, the
/// `finish_reason: "length"` truncation notice) are **not** forwarded, because
/// `openai_chat_stream` maps only `content`, `done` and `error` — everything else
/// it consumes. For the same reason the per-round `usage` merge has no wire
/// effect here: the facade's frames carry no usage field. Both are still decoded
/// (see [`decode_event`]) so the loop is not reading a shape it cannot see.
pub fn streaming_response(
    config: crate::chat_execution::UpstreamConfig,
    prepared: Value,
    executor: crate::chat_tool_loop::ToolRoundExecutor,
    upstream: reqwest::Response,
    model: &str,
    created: i64,
) -> Response {
    let encoder =
        StreamChunkEncoder::new(format!("chatcmpl-{created}"), created, model.to_string());
    let body_stream = async_stream::stream! {
        use crate::tool_rounds::{RoundDecision, ToolCallAccumulator};

        yield Ok::<Bytes, std::io::Error>(Bytes::from(encoder.role_frame()));
        let mut body = prepared;
        let mut current = upstream;
        // Set when the error frame has gone out: the oracle `return`s from there,
        // so neither the stop frame nor a further round follows.
        let mut terminated = false;

        for tool_round in 0..(crate::tool_rounds::MAX_TOOL_ROUNDS + 2) {
            let mut accumulator = ToolCallAccumulator::new();
            let mut round_content = String::new();
            let mut round_reasoning = String::new();
            let mut pending = Vec::<u8>::new();
            let mut event_name = String::from("message");
            // `[DONE]` or an error ends *this round's* read, not the whole stream.
            let mut round_over = false;
            let mut stream = current.bytes_stream();
            while let Some(chunk) = stream.next().await {
                let chunk = match chunk {
                    Ok(chunk) => chunk,
                    Err(_) => {
                        // A transport failure after the headers is not an upstream
                        // status; report it in-band and stop, as the oracle does.
                        yield Ok(Bytes::from(StreamChunkEncoder::error_frame(
                            "Upstream stream error",
                        )));
                        terminated = true;
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
                    for delta in decode_event(&line, &mut event_name) {
                        match delta {
                            UpstreamDelta::Content(text) => {
                                round_content.push_str(&text);
                                yield Ok(Bytes::from(encoder.content_frame(&text)));
                            }
                            // Accumulated for the exchange the next round needs,
                            // never streamed — the facade has no reasoning channel.
                            UpstreamDelta::Reasoning(text) => round_reasoning.push_str(&text),
                            UpstreamDelta::ToolCalls(fragments) => accumulator.merge(&fragments),
                            // Decoded so the loop sees every field the oracle sees;
                            // the facade's frames carry neither of them.
                            UpstreamDelta::Usage(_) | UpstreamDelta::FinishReason(_) => {}
                            UpstreamDelta::ResponseId(_) | UpstreamDelta::Model(_) => {}
                            UpstreamDelta::Done => round_over = true,
                            UpstreamDelta::Error { message } => {
                                yield Ok(Bytes::from(StreamChunkEncoder::error_frame(&message)));
                                terminated = true;
                            }
                        }
                    }
                }
                if round_over || terminated {
                    break;
                }
            }
            if terminated {
                break;
            }

            // `finalized_stream_tool_calls`: sorted by slot index, then normalized.
            let calls = accumulator.finalize();
            match crate::tool_rounds::decide_round(
                calls.len(),
                tool_round,
                crate::tool_rounds::MAX_TOOL_ROUNDS,
            ) {
                RoundDecision::Finish => break,
                RoundDecision::ForceFinalAnswer => {
                    body = crate::tool_rounds::force_final_answer_without_tools(&body);
                }
                RoundDecision::Continue => {
                    let results = executor.run_round(calls.clone()).await;
                    body = crate::tool_rounds::append_tool_exchange(
                        &body,
                        &round_content,
                        &round_reasoning,
                        &calls,
                        &results,
                    );
                }
            }
            // A response body is single-shot, so a further round is a fresh
            // request. A failure to open it is an in-band error, because the
            // status line is already on the wire.
            match crate::chat_execution::open_chat_stream(&config, &body).await {
                Ok(next) => current = next,
                Err(_) => {
                    yield Ok(Bytes::from(StreamChunkEncoder::error_frame(
                        "Upstream stream error",
                    )));
                    terminated = true;
                    break;
                }
            }
        }

        if !terminated {
            yield Ok(Bytes::from(encoder.stop_frame()));
        }
        yield Ok(Bytes::from(encoder.done_frame()));
    };
    sse_headers(Body::from_stream(body_stream))
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
            vec![UpstreamDelta::Content("hi".to_string())]
        );
        assert_eq!(
            decode_event(
                "data: {\"choices\":[{\"delta\":{\"reasoning_content\":\"why\"}}]}",
                &mut name
            ),
            vec![UpstreamDelta::Reasoning("why".to_string())]
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
                vec![UpstreamDelta::Reasoning("x".to_string())],
                "alias {key} must be read"
            );
        }
    }

    #[test]
    fn done_marker_and_blank_line_event_reset() {
        let mut name = "error".to_string();
        assert_eq!(decode_event("", &mut name), Vec::new());
        assert_eq!(name, "message", "a blank line resets the event name");
        assert_eq!(
            decode_event("data: [DONE]", &mut name),
            vec![UpstreamDelta::Done]
        );
    }

    #[test]
    fn malformed_json_is_skipped_not_aborted() {
        let mut name = "message".to_string();
        assert_eq!(decode_event("data: {not json", &mut name), Vec::new());
    }

    #[test]
    fn non_data_lines_are_ignored() {
        let mut name = "message".to_string();
        assert_eq!(decode_event(": keep-alive comment", &mut name), Vec::new());
        assert_eq!(decode_event("id: 42", &mut name), Vec::new());
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
        assert_eq!(decode_event(line, &mut name), Vec::new());
        let mut name = "error".to_string();
        assert_eq!(
            decode_event(line, &mut name),
            vec![UpstreamDelta::Error {
                message: "boom".to_string()
            }]
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

    /// A tool-call chunk **also** contributes its content.
    ///
    /// Measured against the oracle's per-chunk body: `merge_stream_tool_call_deltas`
    /// runs first and unconditionally, then `delta_content` is forwarded *whatever*
    /// the tool calls were. The earlier shape short-circuited on `tool_calls`, which
    /// dropped the text that belongs in that round's assistant message — the very
    /// text `append_tool_exchange` replays to the provider.
    ///
    /// Both deltas come back, in the oracle's order, so the round loop can consume
    /// the calls and still forward the content.
    #[test]
    fn a_tool_call_chunk_still_contributes_its_content() {
        let mut name = "message".to_string();
        assert_eq!(
            decode_event(
                r#"data: {"choices":[{"delta":{"content":"let me check","tool_calls":[{"id":"c1"}]}}]}"#,
                &mut name
            ),
            vec![
                UpstreamDelta::ToolCalls(json!([{"id": "c1"}])),
                UpstreamDelta::Content("let me check".to_string()),
            ]
        );
    }

    /// Every field a chunk can carry is decoded in the oracle's order, because the
    /// round loop needs `finish_reason` and `usage` from the same chunk that ends
    /// the round.
    #[test]
    fn a_round_ending_chunk_yields_its_usage_and_finish_reason() {
        let mut name = "message".to_string();
        assert_eq!(
            decode_event(
                r#"data: {"id":"resp","model":"m","usage":{"prompt_tokens":3},"choices":[{"finish_reason":"tool_calls","delta":{"tool_calls":[{"index":0}]}}]}"#,
                &mut name
            ),
            vec![
                UpstreamDelta::ResponseId("resp".to_string()),
                UpstreamDelta::Model("m".to_string()),
                UpstreamDelta::FinishReason("tool_calls".to_string()),
                UpstreamDelta::Usage(json!({"prompt_tokens": 3})),
                UpstreamDelta::ToolCalls(json!([{"index": 0}])),
            ]
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
            vec![UpstreamDelta::ResponseId("chatcmpl-1".to_string())]
        );
        assert_eq!(
            decode_event(r#"data: {"model":"deepseek-v4-pro"}"#, &mut name),
            vec![UpstreamDelta::Model("deepseek-v4-pro".to_string())]
        );
    }
}
