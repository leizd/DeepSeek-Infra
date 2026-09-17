//! Context management for cache-friendly requests — the oracle's `gateway/context_manager.py`.
//!
//! Two things happen here, both of them about *stability* rather than content: the tool array
//! is sorted so its order cannot churn between turns, and the message list is trimmed — first
//! by message count, then by token estimate — while both ends are pinned. A single optional
//! leading system message (the stable prefix) and a single optional trailing system message
//! (this turn's dynamic context) are never dropped, because dropping either would invalidate
//! the prompt cache behind it or lose the turn's own context.
//!
//! The counters it returns are diagnostics, and they carry the same serialization caveat as
//! everywhere else in this crate: the oracle builds them in insertion order, `serde_json`'s
//! map sorts, so a serializer that wants byte parity owns the order rather than inheriting it.

use serde_json::{Map, Value};

use crate::context_engine::{
    ContextEngineSettings, build_engine_diagnostics, estimate_tools_tokens, token_trim,
};
use crate::python_json::dumps_compact;

/// The oracle's `GATEWAY_CONTEXT_MANAGER_ENABLED` / `GATEWAY_CONTEXT_WINDOW_MESSAGES`, plus the
/// two context-engine switches this module reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContextManagerSettings {
    pub enabled: bool,
    pub window_messages: usize,
    pub engine_enabled: bool,
    pub token_aware_trim: bool,
}

impl Default for ContextManagerSettings {
    fn default() -> Self {
        Self {
            enabled: true,
            window_messages: 36,
            engine_enabled: true,
            token_aware_trim: true,
        }
    }
}

/// Mirrors `stable_json_dumps`: the deterministic serialization the gateway uses for
/// idempotency keys.
pub fn stable_json_dumps(value: &Value) -> String {
    dumps_compact(value)
}

/// Mirrors `tool_name`.
pub fn tool_name(tool: &Value) -> String {
    let Some(function) = tool.get("function").filter(|found| found.is_object()) else {
        return String::new();
    };
    text_or_empty(function.get("name"))
}

/// Mirrors `tool_sort_key`: `(name, type)`, with a non-dict tool sorting *after* named ones
/// because the oracle prefixes it with `~`, which is greater than every ASCII letter.
pub fn tool_sort_key(tool: &Value) -> (String, String) {
    if !tool.is_object() {
        return ("~".to_string(), String::new());
    }
    (tool_name(tool), text_or_empty(tool.get("type")))
}

/// Mirrors `sliding_window_messages`.
///
/// The count window is applied to the *variable middle* only: the budget is whatever is left
/// after the pinned ends, floored at one so a tiny window still keeps the latest message.
pub fn sliding_window_messages(messages: &[Value], window_messages: usize) -> (Vec<Value>, usize) {
    if messages.len() <= window_messages {
        return (messages.to_vec(), 0);
    }
    let mut first: Vec<Value> = Vec::new();
    let mut tail: Vec<Value> = Vec::new();
    let mut start_index = 0usize;
    let mut end_index = messages.len();
    if messages.first().map(is_system).unwrap_or(false) {
        first.push(messages[0].clone());
        start_index = 1;
    }
    if end_index > start_index
        && messages[end_index - 1].is_object()
        && is_system(&messages[end_index - 1])
    {
        tail.push(messages[end_index - 1].clone());
        end_index -= 1;
    }
    let variable = &messages[start_index..end_index];
    let remaining_budget = window_messages
        .saturating_sub(first.len() + tail.len())
        .max(1);
    let kept_from = variable.len().saturating_sub(remaining_budget);
    let kept = &variable[kept_from..];
    let dropped = variable.len() - kept.len();
    let mut result = first;
    result.extend_from_slice(kept);
    result.extend(tail);
    (result, dropped)
}

/// Mirrors `manage_request_body`.
///
/// Returns the managed body and its diagnostics. When the manager is disabled the oracle
/// returns a shallow copy of the body and a one-key diagnostics block — the copy is
/// observable only through identity, which a JSON value does not expose, so a clone is
/// equivalent here.
pub fn manage_request_body(
    body: &Value,
    allow_sliding_window: bool,
    settings: &ContextManagerSettings,
    engine: &ContextEngineSettings,
) -> (Value, Value) {
    if !settings.enabled {
        let mut diagnostics: Map<String, Value> = Map::new();
        diagnostics.insert("enabled".to_string(), Value::Bool(false));
        return (body.clone(), Value::Object(diagnostics));
    }

    let mut managed = body.clone();
    let mut diagnostics: Map<String, Value> = Map::new();
    for (key, value) in [
        ("enabled", Value::Bool(true)),
        ("stableJson", Value::Bool(true)),
        ("stableSystemPosition", Value::String("front".to_string())),
        (
            "dynamicContextPosition",
            Value::String("tail-system".to_string()),
        ),
        ("toolOrderStable", Value::Bool(true)),
        ("slidingWindowApplied", Value::Bool(false)),
        ("slidingWindowAllowed", Value::Bool(allow_sliding_window)),
        ("droppedMessages", Value::from(0)),
    ] {
        diagnostics.insert(key.to_string(), value);
    }

    if let Some(Value::Array(tools)) = body.get("tools") {
        let mut ordered = tools.clone();
        // Stable, like Python's `sorted`: tools that compare equal keep their order.
        ordered.sort_by_key(tool_sort_key);
        let names: Vec<Value> = ordered
            .iter()
            .map(tool_name)
            .filter(|name| !name.is_empty())
            .map(Value::String)
            .collect();
        diagnostics.insert("toolOrder".to_string(), Value::Array(names));
        diagnostics.insert("toolCount".to_string(), Value::from(ordered.len()));
        managed["tools"] = Value::Array(ordered);
    }

    if let Some(Value::Array(messages)) = body.get("messages") {
        let (trimmed, dropped) = if allow_sliding_window {
            let (mut trimmed, mut dropped) =
                sliding_window_messages(messages, settings.window_messages);
            if settings.engine_enabled && settings.token_aware_trim {
                // The overhead is the tool-schema cost, which lives in the body but not in
                // `messages`, so the budget check stays accurate without trimming reaching
                // outside the message list.
                let overhead = estimate_tools_tokens(managed.get("tools"));
                let (again, extra) = token_trim(
                    &trimmed,
                    managed.get("model").and_then(Value::as_str),
                    overhead,
                    engine,
                );
                trimmed = again;
                dropped += extra;
                if extra > 0 {
                    diagnostics.insert("tokenAwareTrimApplied".to_string(), Value::Bool(true));
                }
            }
            (trimmed, dropped)
        } else {
            (messages.clone(), 0)
        };
        if dropped > 0 {
            managed["messages"] = Value::Array(trimmed);
            diagnostics.insert("slidingWindowApplied".to_string(), Value::Bool(true));
            diagnostics.insert("droppedMessages".to_string(), Value::from(dropped));
        }

        let managed_messages: Vec<Value> = match managed.get("messages") {
            Some(Value::Array(items)) => items.clone(),
            _ => Vec::new(),
        };
        diagnostics.insert(
            "messageCount".to_string(),
            Value::from(managed_messages.len()),
        );
        let request_message_count = managed_messages
            .iter()
            .filter(|item| {
                item.is_object()
                    && matches!(
                        item.get("role").and_then(Value::as_str),
                        Some("user") | Some("assistant")
                    )
            })
            .count();
        diagnostics.insert(
            "requestMessageCount".to_string(),
            Value::from(request_message_count),
        );
        diagnostics.insert(
            "hasFrontSystemPrompt".to_string(),
            Value::Bool(managed_messages.first().map(is_system).unwrap_or(false)),
        );
        diagnostics.insert(
            "hasTrailingDynamicContext".to_string(),
            Value::Bool(
                managed_messages.len() > 1
                    && managed_messages.last().map(is_system).unwrap_or(false),
            ),
        );
    }

    if settings.engine_enabled {
        let dropped = diagnostics
            .get("droppedMessages")
            .and_then(Value::as_u64)
            .unwrap_or(0) as usize;
        diagnostics.insert(
            "contextEngine".to_string(),
            build_engine_diagnostics(
                &managed,
                managed.get("model").and_then(Value::as_str),
                dropped,
                engine,
            ),
        );
    }

    (managed, Value::Object(diagnostics))
}

/// Mirrors `merge_context_manager_diagnostics`.
///
/// Note this **mutates** its second argument in the oracle (`pop`), which is why it takes the
/// block by value here: the engine block moves out of `context_manager` and lands at the top
/// level so callers can read `diagnostics["contextEngine"]` without reaching inside the
/// manager's block.
pub fn merge_context_manager_diagnostics(diagnostics: Value, mut context_manager: Value) -> Value {
    let mut result: Map<String, Value> = match diagnostics {
        Value::Object(fields) => fields,
        _ => Map::new(),
    };
    let block = context_manager
        .as_object_mut()
        .and_then(|fields| fields.remove("contextEngine"));
    let request_message_count = context_manager
        .get("requestMessageCount")
        .filter(|value| !value.is_null())
        .cloned();
    result.insert("contextManager".to_string(), context_manager);
    if let Some(block) = block {
        result.insert("contextEngine".to_string(), block);
    }
    if let Some(count) = request_message_count {
        result.insert("requestMessageCount".to_string(), count);
    }
    Value::Object(result)
}

fn is_system(message: &Value) -> bool {
    message.is_object() && message.get("role") == Some(&Value::String("system".to_string()))
}

/// `str(value or "")` without the strip.
fn text_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(found) if crate::core_utils::python_truthy(found) => {
            crate::python_json::value_str(found)
        }
        _ => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn engine() -> ContextEngineSettings {
        ContextEngineSettings::default()
    }

    fn forty_messages() -> Vec<Value> {
        (0..40)
            .map(|index| json!({"role": "user", "content": format!("m{index}")}))
            .collect()
    }

    #[test]
    fn the_tool_array_is_sorted_stably_and_only_named_tools_are_listed() {
        let body = json!({"tools": [
            {"type": "function", "function": {"name": "web_search"}},
            {"type": "other", "function": {"name": "create_pptx"}},
            {"type": "function", "function": {"name": "create_pptx"}},
            {"type": "function", "function": {"name": ""}},
            {"type": "function"},
        ]});
        let (managed, diagnostics) =
            manage_request_body(&body, false, &ContextManagerSettings::default(), &engine());
        let names: Vec<String> = managed["tools"]
            .as_array()
            .expect("tools")
            .iter()
            .map(tool_name)
            .collect();
        // Sorted by (name, type): the two create_pptx entries keep their relative order, and
        // the unnamed/invalid ones sort last.
        assert_eq!(
            names,
            vec!["", "", "create_pptx", "create_pptx", "web_search"]
        );
        // `toolOrder` filters the unnamed ones out; `toolCount` counts every entry.
        assert_eq!(
            diagnostics["toolOrder"],
            json!(["create_pptx", "create_pptx", "web_search"])
        );
        assert_eq!(diagnostics["toolCount"], json!(5));
    }

    #[test]
    fn a_tool_object_that_is_not_a_dict_renders_as_an_empty_name() {
        // The oracle's `tool_name` calls `.get` on it and raises AttributeError, so this
        // tolerance is a **measured divergence**: unreachable from the wired path (the tool
        // catalogue only ever yields objects) and pinned here so it stays a decision rather
        // than becoming an accident.
        assert_eq!(tool_name(&json!("not-a-dict")), "");
        assert_eq!(tool_name(&json!({"function": "not-a-dict"})), "");
        // `tool_sort_key` does guard in the oracle, and a non-dict sorts after every name.
        assert_eq!(
            tool_sort_key(&json!("not-a-dict")),
            ("~".to_string(), String::new())
        );
    }

    #[test]
    fn the_count_window_keeps_both_ends_and_never_drops_below_the_floor() {
        let messages = forty_messages();
        let (trimmed, dropped) = sliding_window_messages(&messages, 36);
        // Measured from the oracle: 40 messages into a 36-message window drops 4.
        assert_eq!(dropped, 4);
        assert_eq!(trimmed.len(), 36);

        let pinned = json!([
            {"role": "system", "content": "prefix"},
            {"role": "user", "content": "u"},
            {"role": "system", "content": "dynamic"},
        ]);
        let (trimmed, dropped) = sliding_window_messages(pinned.as_array().unwrap(), 1);
        // A budget of one still keeps the newest variable message, and both ends survive.
        assert_eq!(dropped, 0);
        assert_eq!(trimmed.len(), 3);
    }

    #[test]
    fn a_disabled_manager_returns_the_body_untouched_and_a_one_key_block() {
        let body = json!({"messages": [{"role": "user", "content": "hi"}]});
        let settings = ContextManagerSettings {
            enabled: false,
            ..ContextManagerSettings::default()
        };
        let (managed, diagnostics) = manage_request_body(&body, true, &settings, &engine());
        assert_eq!(managed, body);
        assert_eq!(diagnostics, json!({"enabled": false}));
    }

    #[test]
    fn without_permission_the_window_never_trims_but_the_counters_still_report() {
        let body = json!({"messages": [
            {"role": "system", "content": "prefix"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "a"},
            {"role": "system", "content": "dynamic"},
        ]});
        let settings = ContextManagerSettings {
            window_messages: 1,
            ..ContextManagerSettings::default()
        };
        let (managed, diagnostics) = manage_request_body(&body, false, &settings, &engine());
        assert_eq!(managed["messages"], body["messages"]);
        assert_eq!(diagnostics["slidingWindowApplied"], json!(false));
        assert_eq!(diagnostics["droppedMessages"], json!(0));
        assert_eq!(diagnostics["slidingWindowAllowed"], json!(false));
        assert_eq!(diagnostics["messageCount"], json!(4));
        assert_eq!(diagnostics["requestMessageCount"], json!(2));
        assert_eq!(diagnostics["hasFrontSystemPrompt"], json!(true));
        assert_eq!(diagnostics["hasTrailingDynamicContext"], json!(true));
    }

    #[test]
    fn the_token_aware_pass_only_runs_when_both_switches_allow_it() {
        let body = json!({"model": "deepseek-v4-pro", "messages": [
            {"role": "system", "content": "prefix"},
            {"role": "user", "content": "中".repeat(200)},
            {"role": "assistant", "content": "a".repeat(200)},
            {"role": "user", "content": "b".repeat(200)},
            {"role": "system", "content": "dynamic"},
        ]});
        // The window table would win for a known model, so the table is emptied: this test is
        // about the trim path, not about which window a model resolves to.
        let narrow = ContextEngineSettings {
            reserve_output_tokens: 0,
            safety_margin_ratio: 0.0,
            default_context_window: 60,
            model_context_windows: Vec::new(),
            ..engine()
        };
        let settings = ContextManagerSettings {
            window_messages: 36,
            ..ContextManagerSettings::default()
        };
        let (_, diagnostics) = manage_request_body(&body, true, &settings, &narrow);
        assert_eq!(diagnostics["tokenAwareTrimApplied"], json!(true));
        assert!(diagnostics["droppedMessages"].as_u64().unwrap() > 0);
        // The engine block is only present while the engine is on.
        assert!(diagnostics.get("contextEngine").is_some());

        let manual = ContextManagerSettings {
            token_aware_trim: false,
            ..settings
        };
        let (_, diagnostics) = manage_request_body(&body, true, &manual, &narrow);
        assert!(diagnostics.get("tokenAwareTrimApplied").is_none());
        assert_eq!(diagnostics["slidingWindowApplied"], json!(false));

        let no_engine = ContextManagerSettings {
            engine_enabled: false,
            ..settings
        };
        let (_, diagnostics) = manage_request_body(&body, true, &no_engine, &narrow);
        assert!(diagnostics.get("contextEngine").is_none());
    }

    #[test]
    fn merging_hoists_the_engine_block_and_copies_a_zero_count_too() {
        let merged = merge_context_manager_diagnostics(
            json!({"existing": 1}),
            json!({"enabled": true, "contextEngine": {"x": 1}, "requestMessageCount": 0}),
        );
        assert_eq!(merged["existing"], json!(1));
        assert_eq!(merged["contextEngine"], json!({"x": 1}));
        // A zero count is copied: the oracle tests `is not None`, not truthiness.
        assert_eq!(merged["requestMessageCount"], json!(0));
        // The block is *moved*, so it is no longer inside the manager's own block.
        assert!(merged["contextManager"].get("contextEngine").is_none());

        let without = merge_context_manager_diagnostics(json!({}), json!({"enabled": true}));
        assert!(without.get("contextEngine").is_none());
        assert_eq!(without["contextManager"], json!({"enabled": true}));
    }
}
