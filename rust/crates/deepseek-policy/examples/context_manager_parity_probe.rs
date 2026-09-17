//! Context-manager parity probe, Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/context_manager_parity_probe.py` through
//! `deepseek_policy::context_manager`.
//!
//! Usage::
//!
//!     python tasks/native-runtime/context_manager_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example context_manager_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use deepseek_policy::context_engine::ContextEngineSettings;
use deepseek_policy::context_manager::{
    ContextManagerSettings, manage_request_body, merge_context_manager_diagnostics,
    sliding_window_messages, stable_json_dumps, tool_name, tool_sort_key,
};
use serde_json::{Map, Value, json};

fn stable_values() -> Vec<Value> {
    vec![
        json!({"b": 1, "a": [1, 2, {"z": null}]}),
        json!({"text": "中文", "n": 1.5}),
        json!([]),
        json!("plain"),
    ]
}

/// Tool *objects* only: a bare non-dict entry makes the oracle's `tool_name` raise, so it is
/// not part of a compared corpus. `tool_key::non-dict` below covers the guarded path.
fn tools() -> Vec<Value> {
    vec![
        json!({"type": "function", "function": {"name": "web_search"}}),
        json!({"type": "function", "function": {"name": "create_pptx"}}),
        json!({"type": "other", "function": {"name": "create_pptx"}}),
        json!({"function": {"name": "a_tool"}}),
        json!({"type": "function", "function": {}}),
        json!({"type": "function", "function": "not-a-dict"}),
        json!({"type": "function"}),
    ]
}

fn message_sets() -> Vec<Value> {
    let forty: Vec<Value> = (0..40)
        .map(|index| json!({"role": "user", "content": format!("m{index}")}))
        .collect();
    vec![
        json!([]),
        json!([{"role": "system", "content": "prefix"}, {"role": "user", "content": "hi"}]),
        json!([
            {"role": "system", "content": "prefix"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
            {"role": "assistant", "content": "a3"},
            {"role": "system", "content": "dynamic"},
        ]),
        json!([{"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "u2"}]),
        json!([{"role": "system", "content": "prefix"}, "not-a-dict", {"role": "user", "content": "u"}, 5]),
        Value::Array(forty),
    ]
}

/// (manager_enabled, window, engine_enabled, token_aware_trim,
///  engine_reserve, engine_safety, engine_default_window, engine_min_keep)
/// (manager_enabled, window, engine_enabled, token_aware_trim, engine_reserve,
///  engine_safety, engine_default_window, engine_min_keep)
type SettingsCase = (bool, usize, bool, bool, i64, f64, i64, usize);

const SETTINGS_CASES: [SettingsCase; 5] = [
    (true, 36, true, true, 8_192, 0.05, 65_536, 2),
    (true, 8, true, true, 0, 0.0, 40, 2),
    (true, 8, true, false, 0, 0.0, 40, 2),
    (true, 8, false, true, 0, 0.0, 40, 2),
    (false, 36, true, true, 8_192, 0.05, 65_536, 2),
];

fn manager_settings(case: SettingsCase) -> ContextManagerSettings {
    ContextManagerSettings {
        enabled: case.0,
        window_messages: case.1,
        engine_enabled: case.2,
        token_aware_trim: case.3,
    }
}

fn engine_settings(case: SettingsCase) -> ContextEngineSettings {
    ContextEngineSettings {
        reserve_output_tokens: case.4,
        safety_margin_ratio: case.5,
        default_context_window: case.6,
        min_keep_messages: case.7,
        ..ContextEngineSettings::default()
    }
}

fn merge_cases() -> Vec<(Value, Value)> {
    vec![
        (
            json!({"a": 1}),
            json!({"enabled": true, "requestMessageCount": 3}),
        ),
        (
            json!({"a": 1}),
            json!({"enabled": true, "requestMessageCount": 0}),
        ),
        (
            json!({"a": 1}),
            json!({"enabled": true, "contextEngine": {"x": 1}, "requestMessageCount": 2}),
        ),
        (
            json!({"a": 1, "contextEngine": {"old": true}}),
            json!({"enabled": true}),
        ),
    ]
}

fn role_of(message: &Value) -> Value {
    message.get("role").cloned().unwrap_or(Value::Null)
}

fn main() {
    let mut out: Map<String, Value> = Map::new();
    let list_argument = |body: &Value| -> Vec<Value> {
        match body.get("messages") {
            Some(Value::Array(items)) => items.clone(),
            _ => Vec::new(),
        }
    };

    for (index, value) in stable_values().iter().enumerate() {
        out.insert(format!("stable::{index}"), json!(stable_json_dumps(value)));
    }

    for (index, tool) in tools().iter().enumerate() {
        out.insert(format!("tool-name::{index}"), json!(tool_name(tool)));
        let key = tool_sort_key(tool);
        out.insert(format!("tool-key::{index}"), json!([key.0, key.1]));
    }
    // `tool_sort_key` tolerates a non-dict where `tool_name` raises; only the former compares.
    let key = tool_sort_key(&json!("not-a-dict"));
    out.insert("tool-key::non-dict".to_string(), json!([key.0, key.1]));

    let default_manager = ContextManagerSettings::default();
    for (index, messages) in message_sets().iter().enumerate() {
        // `messages` *is* the array here, not a body that contains one.
        let list = messages.as_array().cloned().unwrap_or_default();
        let (trimmed, dropped) = sliding_window_messages(&list, default_manager.window_messages);
        let roles: Vec<Value> = trimmed
            .iter()
            .map(|item| {
                if item.is_object() {
                    role_of(item)
                } else {
                    json!("?")
                }
            })
            .collect();
        out.insert(format!("window::dropped::{index}"), json!(dropped));
        out.insert(format!("window::kept::{index}"), json!(trimmed.len()));
        out.insert(format!("window::roles::{index}"), json!(roles));
    }

    for (settings_index, case) in SETTINGS_CASES.iter().enumerate() {
        let manager = manager_settings(*case);
        let engine = engine_settings(*case);
        for (messages_index, messages) in message_sets().iter().enumerate() {
            for allow in [false, true] {
                let body = json!({
                    "model": "unknown-model",
                    "messages": messages,
                    "tools": tools(),
                });
                let (managed, diagnostics) = manage_request_body(&body, allow, &manager, &engine);
                let key = format!(
                    "manage::s{settings_index}::m{messages_index}::a{}",
                    u8::from(allow)
                );
                let names: Vec<String> = managed
                    .get("tools")
                    .and_then(Value::as_array)
                    .map(|items| items.iter().map(tool_name).collect())
                    .unwrap_or_default();
                out.insert(format!("{key}::tools"), json!(names));
                let kept = list_argument(&managed);
                out.insert(format!("{key}::messages"), json!(kept.len()));
                out.insert(
                    format!("{key}::first"),
                    kept.first().map(role_of).unwrap_or(Value::Null),
                );
                out.insert(
                    format!("{key}::last"),
                    kept.last().map(role_of).unwrap_or(Value::Null),
                );
                out.insert(format!("{key}::diagnostics"), diagnostics);
            }
        }
    }

    for (index, (diagnostics, manager)) in merge_cases().iter().enumerate() {
        out.insert(
            format!("merge::{index}"),
            merge_context_manager_diagnostics(diagnostics.clone(), manager.clone()),
        );
    }

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
