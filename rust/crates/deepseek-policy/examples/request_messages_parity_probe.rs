//! Message-layer parity probe, Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/request_messages_parity_probe.py` through
//! `deepseek_policy::request_messages`. The content expander is injected here for the same
//! reason the Python probe stubs it: the real one expands attachments through the file index,
//! which is I/O and belongs to the file-store slice. The stub mirrors the oracle's
//! non-attachment path (`str(content or "").strip()`).
//!
//! Usage::
//!
//!     python tasks/native-runtime/request_messages_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example request_messages_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use deepseek_policy::app_error::AppError;
use deepseek_policy::model_router::ModelRouterSettings;
use deepseek_policy::request_messages::{
    canonical_tool_arguments, image_content_parts, normalize_chat_messages, normalize_tool_calls,
    preflight_deepseek_payload, stable_tool_call_id, validate_deepseek_payload,
    validate_request_messages,
};
use serde_json::{Map, Value, json};

const LONG_IMAGE: &str = "data:image/png;base64,AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA";
const SHORT_IMAGE: &str = "data:image/png;base64,AA";

fn message_sets() -> Vec<Value> {
    vec![
        json!([]),
        json!([{"role": "user", "content": "hi"}]),
        json!([{"role": "system", "content": "sys"}, {"role": "assistant", "content": "ok"}]),
        json!([{"role": "user", "content": "  padded  "}]),
        json!([{"role": "user", "content": ""}]),
        json!([{"role": "user", "content": "   "}]),
        json!([{"role": "user", "content": null}]),
        json!([{"role": "user", "content": 5}]),
        json!([{"role": "tool", "content": "result", "tool_call_id": "c1"}]),
        json!([{"role": "tool", "content": "result"}]),
        json!([{"role": "tool", "content": "  ", "tool_call_id": "c1"}]),
        json!([{"role": "unknown", "content": "x"}]),
        json!([{"role": 5, "content": "x"}]),
        json!([5]),
        json!(["not-a-dict"]),
        json!([{"role": "user", "content": "look", "attachments": [{"imageData": LONG_IMAGE}]}]),
        json!([{"role": "user", "content": "", "attachments": [{"imageData": LONG_IMAGE}]}]),
        json!([{"role": "user", "content": "look", "attachments": [{"imageData": SHORT_IMAGE}]}]),
        json!([{"role": "user", "content": "look", "attachments": [{"imageData": "http://x"}]}]),
        json!([{"role": "user", "content": "x", "attachments": [{"imageData": LONG_IMAGE}, "not-a-dict"]}]),
        json!([{"role": "assistant", "content": "calling", "tool_calls": [{"id": "c1", "function": {"name": "web_search", "arguments": "{\"q\": \"中\"}"}}]}]),
        json!([{"role": "assistant", "content": "calling", "tool_calls": []}]),
        json!([{"role": "assistant", "content": "calling", "tool_calls": [{"function": {"name": ""}}]}]),
    ]
}

fn tool_call_values() -> Vec<Value> {
    vec![
        Value::Null,
        json!("not-a-list"),
        json!([]),
        json!([{"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{\"q\": 1}"}}]),
        // Keys already in sorted order: the oracle emits insertion order for an object
        // `arguments` and this port emits sorted order, so an unsorted object here would
        // compare a divergence the port cannot fix (see the module docs).
        json!([{"function": {"name": "search", "arguments": {"a": 2, "b": 1}}}]),
        json!([{"function": {"name": "search"}}]),
        json!([{"name": "top_level", "arguments": "raw"}]),
        json!([{"function": {"name": "   ", "arguments": "x"}}]),
        json!([{"function": "not-a-dict", "name": "fallback", "arguments": "{}"}]),
        json!([{"id": "", "function": {"name": "blank-id"}}]),
        json!([{"id": 7, "type": "", "function": {"name": "numeric-id"}}]),
        json!(["not-a-dict", {"function": {"name": "kept"}}]),
        json!([{"function": {"name": "canon", "arguments": "{\"b\":2,\"a\":1}"}}]),
        json!([{"function": {"name": "bad-json", "arguments": "not json"}}]),
        json!([{"function": {"name": format!("long_name_{}", "x".repeat(60)), "arguments": "{}"}}]),
    ]
}

fn validate_payloads() -> Vec<Value> {
    vec![
        json!({}),
        json!({"apiKey": "k", "messages": [{"role": "user", "content": "x"}]}),
        json!({"apiKey": "  k  ", "messages": [{"role": "user", "content": "x"}]}),
        json!({"apiKey": "", "messages": [{"role": "user", "content": "x"}]}),
        json!({"apiKey": "k", "model": "flash", "messages": [{"role": "user", "content": "x"}]}),
        json!({"apiKey": "k", "model": "unknown", "messages": [{"role": "user", "content": "x"}]}),
        json!({"apiKey": "k", "model": "auto", "messages": [{"role": "user", "content": "帮我写代码"}]}),
        json!({"apiKey": 0, "messages": [{"role": "user", "content": "x"}]}),
        json!({"apiKey": "k"}),
        json!({"apiKey": "k", "messages": []}),
        json!({"apiKey": "k", "messages": "not-a-list"}),
    ]
}

/// (messages, unused) — the second slot mirrors the Python corpus' shape.
fn validate_message_cases() -> Vec<Value> {
    let run = |count: usize| -> Value {
        Value::Array(
            (0..count)
                .map(|index| json!({"role": "user", "content": format!("m{index}")}))
                .collect(),
        )
    };
    vec![
        json!([{"role": "user", "content": "x"}]),
        json!([{"role": "system", "content": "x"}]),
        json!([{"role": "user", "content": "x"}]),
        run(41),
        run(41),
        run(40),
        json!([{"role": "user", "content": "  "}]),
    ]
}

fn stable_id_cases() -> Vec<(usize, String)> {
    vec![
        (0, "web_search".to_string()),
        (1, String::new()),
        (2, "  ".to_string()),
        (3, "  Mixed-Case.Name  ".to_string()),
        (4, "中".repeat(60)),
    ]
}

fn canonical_cases() -> Vec<Value> {
    vec![
        json!("{\"b\":2,\"a\":1}"),
        json!("not json"),
        json!("  padded  "),
        json!(5),
        json!({"z": 1, "a": [2, 3]}),
        json!([1, "中"]),
    ]
}

fn image_part_cases() -> Vec<Value> {
    vec![
        json!({}),
        json!({"attachments": "x"}),
        json!({"attachments": [{"imageData": LONG_IMAGE}]}),
        json!({"attachments": [{"imageData": SHORT_IMAGE}]}),
        json!({"attachments": [{"imageData": format!(" data:image/png;base64,{} ", "B".repeat(30))}]}),
        json!({"attachments": [{"imageData": 5}, "x"]}),
    ]
}

/// The oracle's non-attachment path, verbatim.
fn expander(message: &Value) -> String {
    match message.get("content") {
        Some(value) if deepseek_policy::core_utils::python_truthy(value) => {
            deepseek_policy::python_json::value_str(value)
        }
        _ => String::new(),
    }
    .trim()
    .to_string()
}

fn rejection(error: &AppError) -> Value {
    json!({"error": error.code, "status": error.status, "message": error.message})
}

fn main() {
    let mut out: Map<String, Value> = Map::new();
    let router = ModelRouterSettings::default();

    for (index, messages) in message_sets().iter().enumerate() {
        let rendered = match normalize_chat_messages(messages, &expander) {
            Ok(normalized) => json!(normalized),
            Err(error) => rejection(&error),
        };
        out.insert(format!("normalize::{index}"), rendered);
    }

    for (index, messages) in validate_message_cases().iter().enumerate() {
        for (summary_index, summary) in [None, Some("s")].iter().enumerate() {
            let payload = match summary {
                Some(text) => json!({"contextSummary": text}),
                None => json!({}),
            };
            let list = messages.as_array().cloned().unwrap_or_default();
            let rendered = match validate_request_messages(&payload, &list, &expander) {
                Ok(()) => json!("ok"),
                Err(error) => rejection(&error),
            };
            out.insert(format!("check::{index}::{summary_index}"), rendered);
        }
    }

    for (index, value) in tool_call_values().iter().enumerate() {
        let value = if value.is_null() { None } else { Some(value) };
        out.insert(
            format!("tool-calls::{index}"),
            json!(normalize_tool_calls(value, false, false)),
        );
        out.insert(
            format!("tool-calls::stable::{index}"),
            json!(normalize_tool_calls(value, true, false)),
        );
        out.insert(
            format!("tool-calls::canonical::{index}"),
            json!(normalize_tool_calls(value, false, true)),
        );
    }

    for (index, (position, name)) in stable_id_cases().iter().enumerate() {
        out.insert(
            format!("stable-id::{index}"),
            json!(stable_tool_call_id(*position, name)),
        );
    }

    for (index, value) in canonical_cases().iter().enumerate() {
        out.insert(
            format!("canonical::{index}"),
            json!(canonical_tool_arguments(value)),
        );
    }

    for (index, message) in image_part_cases().iter().enumerate() {
        out.insert(
            format!("image-parts::{index}"),
            json!(image_content_parts(message)),
        );
    }

    for (index, payload) in validate_payloads().iter().enumerate() {
        let rendered = match validate_deepseek_payload(payload, "", &router) {
            Ok(validated) => json!({
                "apiKey": validated.api_key,
                "model": validated.model,
                "messages": validated.messages,
            }),
            Err(error) => rejection(&error),
        };
        out.insert(format!("validate::{index}"), rendered);

        let preflight = match preflight_deepseek_payload(payload, "", &router, &expander) {
            Ok(validated) => json!({
                "apiKey": validated.api_key,
                "model": validated.model,
                "count": validated.messages.len(),
            }),
            Err(error) => rejection(&error),
        };
        out.insert(format!("preflight::{index}"), preflight);
    }

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
