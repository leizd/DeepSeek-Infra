//! OpenAI facade payload-parity probe, Rust side.
//!
//! Replays `tasks/native-runtime/openai_facade_parity_probe.py` through
//! [`deepseek_gateway::openai_facade`] and prints canonical JSON.
//!
//! Usage::
//!
//! ```text
//! python tasks/native-runtime/openai_facade_parity_probe.py > python.json
//! cd rust && cargo run -p deepseek-gateway --example openai_facade_parity_probe > ../rust.json
//! diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)
//! ```
//!
//! The payload is compared as **sorted-key JSON** on both sides. Python's dict keeps
//! insertion order and this crate's `serde_json::Map` is keyed, so comparing renderings
//! directly would test the map implementation rather than the translation; the field
//! *set* and every value are what the contract is.

use std::collections::BTreeMap;

use deepseek_gateway::openai_facade::{openai_to_internal_payload, payload_canonical_json};
use deepseek_policy::model_router::ModelRouterSettings;
use deepseek_policy::python_json::OrderedJson;
use serde_json::{Map, Value, json};

const BASE_URL: &str = "http://127.0.0.1:8000";

fn turns() -> Value {
    json!([{"role": "user", "content": " hi "}])
}

/// `(label, body)` pairs, in the same order and with the same bodies as the Python
/// probe. Kept as a literal list rather than generated, so a case that is added on one
/// side and not the other shows up as a key diff.
fn cases() -> Vec<(&'static str, Value)> {
    vec![
        (
            "minimal",
            json!({"model": "deepseek-v4-pro", "messages": turns()}),
        ),
        ("default-model-when-absent", json!({"messages": turns()})),
        (
            "falsy-model-empty-string",
            json!({"model": "", "messages": turns()}),
        ),
        (
            "falsy-model-null",
            json!({"model": null, "messages": turns()}),
        ),
        ("falsy-model-zero", json!({"model": 0, "messages": turns()})),
        (
            "falsy-model-false",
            json!({"model": false, "messages": turns()}),
        ),
        (
            "falsy-model-empty-list",
            json!({"model": [], "messages": turns()}),
        ),
        (
            "falsy-model-empty-dict",
            json!({"model": {}, "messages": turns()}),
        ),
        (
            "truthy-model-bool",
            json!({"model": true, "messages": turns()}),
        ),
        (
            "truthy-model-number",
            json!({"model": 7, "messages": turns()}),
        ),
        (
            "alias-exact",
            json!({"model": "deepseek-v4-pro", "messages": turns()}),
        ),
        (
            "alias-uppercase",
            json!({"model": "DEEPSEEK-V4-PRO", "messages": turns()}),
        ),
        ("alias-short", json!({"model": "fast", "messages": turns()})),
        (
            "alias-short-expert",
            json!({"model": "expert", "messages": turns()}),
        ),
        (
            "alias-underscores",
            json!({"model": "v4_pro", "messages": turns()}),
        ),
        (
            "alias-spaces-stripped",
            json!({"model": "deep seek v4 flash", "messages": turns()}),
        ),
        (
            "alias-surrounding-space",
            json!({"model": "  flash  ", "messages": turns()}),
        ),
        (
            "alias-unknown-passthrough",
            json!({"model": "my-model", "messages": turns()}),
        ),
        (
            "alias-unknown-keeps-case",
            json!({"model": "My Model", "messages": turns()}),
        ),
        ("stream-absent", json!({"messages": turns()})),
        ("stream-true", json!({"messages": turns(), "stream": true})),
        (
            "stream-false",
            json!({"messages": turns(), "stream": false}),
        ),
        (
            "stream-string-false-is-true",
            json!({"messages": turns(), "stream": "false"}),
        ),
        (
            "stream-empty-string-is-false",
            json!({"messages": turns(), "stream": ""}),
        ),
        ("stream-zero", json!({"messages": turns(), "stream": 0})),
        ("stream-one", json!({"messages": turns(), "stream": 1})),
        (
            "stream-empty-list",
            json!({"messages": turns(), "stream": []}),
        ),
        ("stream-null", json!({"messages": turns(), "stream": null})),
        (
            "temperature-float",
            json!({"messages": turns(), "temperature": 0.5}),
        ),
        (
            "temperature-zero",
            json!({"messages": turns(), "temperature": 0}),
        ),
        (
            "temperature-negative",
            json!({"messages": turns(), "temperature": -1}),
        ),
        (
            "temperature-large-int",
            json!({"messages": turns(), "temperature": 3}),
        ),
        (
            "temperature-bool-true-ignored",
            json!({"messages": turns(), "temperature": true}),
        ),
        (
            "temperature-bool-false-ignored",
            json!({"messages": turns(), "temperature": false}),
        ),
        (
            "temperature-string-ignored",
            json!({"messages": turns(), "temperature": "0.5"}),
        ),
        (
            "temperature-list-ignored",
            json!({"messages": turns(), "temperature": [0.5]}),
        ),
        (
            "temperature-null-ignored",
            json!({"messages": turns(), "temperature": null}),
        ),
        (
            "messages-forwarded-verbatim",
            json!({
                "messages": [
                    {"role": "user", "content": "  "},
                    {"role": "tool", "content": "x"},
                    "not-an-object",
                    {"role": "assistant", "content": null},
                    {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                ]
            }),
        ),
        (
            "messages-extra-turn-fields-kept",
            json!({
                "messages": [{"role": "user", "content": "hi", "name": "n", "tool_call_id": "t"}]
            }),
        ),
        (
            "drops-tools",
            json!({
                "model": "fast",
                "messages": turns(),
                "tools": [{"type": "function", "function": {"name": "t", "parameters": {}}}],
                "tool_choice": "auto",
            }),
        ),
        (
            "drops-sampling-and-reasoning",
            json!({
                "messages": turns(),
                "max_tokens": 16,
                "top_p": 0.5,
                "reasoning_effort": "high",
                "thinking": {"type": "enabled"},
            }),
        ),
        (
            "drops-everything-at-once",
            json!({
                "model": "expert",
                "messages": turns(),
                "tools": [],
                "tool_choice": "none",
                "max_tokens": 1,
                "top_p": 1,
                "reasoning_effort": "minimal",
                "thinking": {"type": "enabled"},
                "temperature": 0.25,
            }),
        ),
        ("refuse-empty-object", json!({})),
        ("refuse-no-messages", json!({"model": "deepseek-v4-pro"})),
        ("refuse-empty-messages", json!({"messages": []})),
        ("refuse-messages-string", json!({"messages": "hi"})),
        (
            "refuse-messages-object",
            json!({"messages": {"role": "user"}}),
        ),
        ("refuse-messages-null", json!({"messages": null})),
        ("refuse-messages-number", json!({"messages": 3})),
        ("refuse-body-list", json!([])),
        ("refuse-body-string", json!("x")),
        ("refuse-body-number", json!(1)),
        ("refuse-body-null", Value::Null),
    ]
}

fn main() {
    let router = ModelRouterSettings::default();
    let mut out: BTreeMap<String, Value> = BTreeMap::new();

    out.insert("probe::base-url".to_string(), json!(BASE_URL));
    out.insert(
        "probe::default-model".to_string(),
        json!(router.default_model),
    );
    let aliases: BTreeMap<String, String> = router
        .model_aliases
        .iter()
        .map(|(from, to)| (from.clone(), to.clone()))
        .collect();
    out.insert(
        "probe::aliases".to_string(),
        serde_json::to_value(aliases).unwrap_or(Value::Null),
    );

    for (label, body) in cases() {
        let view = match openai_to_internal_payload(&body, BASE_URL, &router) {
            Ok(payload) => {
                let mut view = Map::new();
                view.insert("ok".to_string(), json!(payload_canonical_json(&payload)));
                Value::Object(view)
            }
            Err(error) => {
                let mut view = Map::new();
                view.insert("error".to_string(), json!(error.message));
                view.insert("code".to_string(), json!(error.code));
                view.insert("status".to_string(), json!(error.status));
                Value::Object(view)
            }
        };
        out.insert(format!("case::{label}"), view);
    }

    let value = Value::Object(out.into_iter().collect::<Map<String, Value>>());
    let rendered = OrderedJson::from_value_with_order(&value, &[]).render_indent_2();
    println!("{rendered}");
}
