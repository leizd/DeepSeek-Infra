//! Request-shaping helpers parity probe, Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/request_shaping_parity_probe.py` through
//! `deepseek_policy::request_shaping` and `deepseek_policy::memory`.
//!
//! Usage::
//!
//!     python tasks/native-runtime/request_shaping_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example request_shaping_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use deepseek_policy::dynamic_context::presentation_intent_requested;
use deepseek_policy::memory::{empty_memory_state, memory_scope_from_payload};
use deepseek_policy::request_shaping::{
    TOOL_PARALLEL_SYSTEM_HINT, count_payload_attachments, forced_artifact_tool_name,
    has_create_pptx_tool, has_image_content, normalize_reasoning_effort, should_force_create_pptx,
    tools_for_payload,
};
use serde_json::{Map, Value, json};

fn effort_cases() -> Vec<Value> {
    vec![
        json!("low"),
        json!("max"),
        json!("minimal"),
        json!("MEDIUM"),
        json!(" high "),
        json!(""),
        Value::Null,
        json!(5),
        json!(true),
        json!("medium"),
    ]
}

fn message_lists() -> Vec<Value> {
    vec![
        json!([]),
        json!([{"role": "user", "content": "text"}]),
        json!([{"role": "user", "content": [{"type": "text", "text": "a"}]}]),
        json!([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}]),
        json!([{"role": "assistant", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}]),
        json!([{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {"url": "u"}}]}]),
        json!([{"role": "user", "content": "no parts"}, {"role": "user", "content": [{"type": "image_url"}]}]),
    ]
}

fn tool_payloads() -> Vec<Value> {
    vec![
        json!({}),
        json!({"searchEnabled": true, "searchMode": "on"}),
        json!({"searchEnabled": true, "searchMode": "off"}),
        json!({"searchEnabled": true, "allowedTools": ["create_pptx", "web_search"]}),
        json!({"allowedTools": ["create_pptx"]}),
        json!({"allowedTools": ["create_pptx"], "searchEnabled": true}),
        json!({"allowedTools": "not-a-list"}),
        json!({"allowedTools": [1, null, "create_pptx"]}),
    ]
}

fn ppt_query() -> Value {
    json!([{"role": "user", "content": "帮我做一份 PPT"}])
}

fn mindmap_query() -> Value {
    json!([{"role": "user", "content": "画一张思维导图"}])
}

fn keyword_only() -> Value {
    json!([{"role": "user", "content": "什么是 mindmap？"}])
}

/// (payload, use the payload's own tool list)
fn force_cases() -> Vec<(Value, bool)> {
    vec![
        (json!({"messages": ppt_query()}), false),
        (
            json!({"messages": ppt_query(), "toolsEnabled": false}),
            false,
        ),
        (
            json!({"messages": ppt_query(), "allowedTools": ["web_search"]}),
            false,
        ),
        (
            json!({"messages": ppt_query(), "allowedTools": ["create_pptx"]}),
            false,
        ),
        (json!({"messages": mindmap_query()}), false),
        (
            json!({"messages": mindmap_query(), "allowedTools": ["create_pptx"]}),
            false,
        ),
        (json!({"messages": keyword_only()}), false),
        (json!({"messages": ppt_query()}), true),
    ]
}

fn attachment_cases() -> Vec<Value> {
    vec![
        Value::Null,
        json!([]),
        json!([{"attachments": [{"a": 1}, {"b": 2}]}]),
        json!([{"attachments": "x"}, {"attachments": [1, "y", {"z": 3}]}]),
        json!([{"attachments": []}, {"role": "user"}]),
        json!(["not a dict", {"attachments": [{"a": 1}]}]),
    ]
}

fn memory_payloads() -> Vec<Value> {
    vec![
        json!({}),
        json!({"memoryEnabled": false}),
        json!({"memoryEnabled": 0}),
        json!({"memoryEnabled": ""}),
        json!({"memoryScope": "project:abc"}),
        json!({"memoryScope": "bogus"}),
        json!({"messages": [{"role": "user", "projectId": "p1"}]}),
        json!({"messages": [{"role": "user", "seekId": "s1"}]}),
        json!({"messages": [{"role": "user", "projectId": "p1"}, {"role": "user", "content": "x"}]}),
        json!({"messages": [{"role": "user", "content": "x"}, {"role": "user", "projectId": "p2"}]}),
        json!({"memoryScope": "global", "messages": [{"role": "user", "projectId": "p3"}]}),
        json!({"messages": [{"role": "assistant", "projectId": "p4"}]}),
        json!({"messages": [{"role": "user", "projectId": "bad id!"}]}),
    ]
}

/// The oracle compares tool *names*; the definitions themselves are the tool catalog's
/// subject and are covered by its own probe.
fn names(tools: &[Value]) -> Vec<String> {
    tools
        .iter()
        .map(|tool| {
            tool.get("function")
                .and_then(|found| found.get("name"))
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string()
        })
        .collect()
}

fn main() {
    let mut out: Map<String, Value> = Map::new();

    out.insert(
        "parallel-hint".to_string(),
        json!(TOOL_PARALLEL_SYSTEM_HINT),
    );

    for (index, value) in effort_cases().iter().enumerate() {
        out.insert(
            format!("effort::{index}"),
            json!(normalize_reasoning_effort(Some(value))),
        );
    }

    for (index, messages) in message_lists().iter().enumerate() {
        let list = messages.as_array().cloned().unwrap_or_default();
        out.insert(format!("image::{index}"), json!(has_image_content(&list)));
    }

    for (index, payload) in tool_payloads().iter().enumerate() {
        let tools = tools_for_payload(payload);
        out.insert(format!("tools::{index}"), json!(names(&tools)));
        out.insert(format!("tools-count::{index}"), json!(tools.len()));
    }

    for (index, (payload, use_own)) in force_cases().iter().enumerate() {
        let tools = if *use_own {
            tools_for_payload(payload)
        } else {
            tools_for_payload(&json!({}))
        };
        out.insert(
            format!("force::{index}"),
            json!(forced_artifact_tool_name(payload, &tools)),
        );
        out.insert(format!("force-own::{index}"), json!(use_own));
    }

    let catalog = tools_for_payload(&json!({}));
    out.insert(
        "has-pptx::catalog".to_string(),
        json!(has_create_pptx_tool(&catalog)),
    );
    out.insert(
        "has-pptx::empty".to_string(),
        json!(has_create_pptx_tool(&[])),
    );
    let ppt = json!({"messages": ppt_query()});
    out.insert(
        "force-alias::ppt".to_string(),
        json!(should_force_create_pptx(&ppt) == presentation_intent_requested(&ppt)),
    );
    out.insert(
        "force-alias::none".to_string(),
        json!(should_force_create_pptx(&json!({})) == presentation_intent_requested(&json!({}))),
    );

    for (index, messages) in attachment_cases().iter().enumerate() {
        out.insert(
            format!("attach::{index}"),
            json!(count_payload_attachments(Some(messages))),
        );
    }

    for (index, payload) in memory_payloads().iter().enumerate() {
        out.insert(
            format!("memory-state::{index}"),
            empty_memory_state(payload),
        );
        out.insert(
            format!("memory-scope::{index}"),
            json!(memory_scope_from_payload(payload)),
        );
    }

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
