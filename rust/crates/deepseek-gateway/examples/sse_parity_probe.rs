//! Byte-level parity probe for the OpenAI SSE envelope.
//!
//! The downstream wire format is a *byte* contract: `openai_api._sse` writes
//! `b"data: " + compact_json + b"\n\n"` and clients parse it positionally. A
//! semantic match is not a pass, so this probe prints the exact bytes each side
//! produces for the same fixed upstream script.
//!
//! Run both sides and diff:
//!
//! ```text
//! cargo run -p deepseek-gateway --example sse_parity_probe > rust.frames
//! python ../tasks/native-runtime/sse_parity_probe.py > python.frames
//! diff rust.frames python.frames
//! ```
//!
//! The companion Python probe extracts `_sse` and `openai_chat_stream` straight
//! out of `deepseek_infra/infra/gateway/openai_api.py` with `ast`, and feeds the
//! *same* upstream script through the real `deepseek_client.stream_deepseek`
//! decoding loop. It explicitly does NOT reimplement either side, because a
//! probe that reimplements the thing it measures can only confirm its own
//! assumptions.
//!
//! Format of each line: `<case-name>\t<hex-of-one-frame>` per frame, prefixed by
//! `CASE <name>`. Hex rather than raw text so trailing-newline and non-ASCII
//! differences cannot be hidden by a terminal.

use deepseek_gateway::chat_stream::{StreamChunkEncoder, UpstreamDelta, decode_event};

/// The fixed upstream script both sides replay. Each entry is one raw SSE line
/// as the provider would emit it, including the blank separators.
const UPSTREAM_SCRIPT: &[&str] = &[
    r#"data: {"id":"chat-1","model":"deepseek-v4-pro","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}"#,
    "",
    r#"data: {"choices":[{"index":0,"delta":{"reasoning_content":"think 1"}}]}"#,
    "",
    r#"data: {"choices":[{"index":0,"delta":{"content":"Hello"}}]}"#,
    "",
    r#"data: {"choices":[{"index":0,"delta":{"content":", 世界"}}]}"#,
    "",
    r#"data: {"choices":[{"index":0,"delta":{"content":"!"},"finish_reason":"stop"}]}"#,
    "",
    "data: [DONE]",
    "",
];

/// An error script: the provider signals failure after partial output.
const ERROR_SCRIPT: &[&str] = &[
    r#"data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}"#,
    "",
    "event: error",
    r#"data: {"error":{"message":"upstream quota exhausted"}}"#,
    "",
];

fn main() {
    let encoder = StreamChunkEncoder::new(
        "chatcmpl-fixture".to_string(),
        1_700_000_000,
        "deepseek-v4-pro".to_string(),
    );
    emit_case("happy", &encoder, UPSTREAM_SCRIPT);
    emit_case("error", &encoder, ERROR_SCRIPT);
}

/// Replay one script through the decoding + encoding pair and print frames.
///
/// This mirrors the read loop in `streaming_response`, minus the HTTP plumbing:
/// the role frame first, then one frame per forwarded delta, then `[DONE]`.
fn emit_case(name: &str, encoder: &StreamChunkEncoder, script: &[&str]) {
    println!("CASE {name}");
    print_frame("role", &encoder.role_frame());
    let mut event_name = String::from("message");
    let mut finished = false;
    for line in script {
        if finished {
            break;
        }
        // `decode_event` returns every delta the chunk carries, in the oracle's order.
        for delta in decode_event(line, &mut event_name) {
            match delta {
                UpstreamDelta::Content(text) => {
                    print_frame("content", &encoder.content_frame(&text));
                }
                UpstreamDelta::Reasoning(_)
                | UpstreamDelta::ResponseId(_)
                | UpstreamDelta::Model(_) => {
                    // Consumed and dropped, exactly as `forward_line` does.
                }
                UpstreamDelta::Done => {
                    finished = true;
                    print_frame("stop", &encoder.stop_frame());
                }
                UpstreamDelta::Error { message } => {
                    finished = true;
                    print_frame("error", &StreamChunkEncoder::error_frame(&message));
                }
                UpstreamDelta::ToolCalls(_) => {
                    // Not part of the byte-parity scripts: a tool round is refused
                    // rather than encoded, and the refusal is covered by the
                    // integration test. Counting it here would make the probe
                    // report a frame the oracle never emits for the same input.
                    finished = true;
                }
                // Accumulated by the round loop, never encoded. A frame would make
                // the probe report output the oracle does not emit here.
                UpstreamDelta::Usage(_) | UpstreamDelta::FinishReason(_) => {}
            }
        }
    }
    if !finished {
        print_frame("stop", &encoder.stop_frame());
    }
    print_frame("done", &encoder.done_frame());
}

fn print_frame(label: &str, frame: &[u8]) {
    let mut hex = String::with_capacity(frame.len() * 2);
    for byte in frame {
        hex.push_str(&format!("{byte:02x}"));
    }
    println!("{label}\t{hex}");
}
