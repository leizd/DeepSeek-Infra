//! Model-router parity probe, Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/model_router_parity_probe.py` through
//! `deepseek_policy::model_router` and the `deepseek_policy::edge_inference` surface it uses.
//!
//! Usage::
//!
//!     python tasks/native-runtime/model_router_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example model_router_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use deepseek_policy::edge_inference::{
    ARTIFACT_QUERY_PATTERN, COMPLEX_QUERY_PATTERN, SIMPLE_TASK_PATTERN, chat_messages_from_payload,
    has_image_attachment,
};
use deepseek_policy::model_router::{
    CITATION_PATTERN, ModelRouterSettings, REFUSAL_MARKERS, UNCERTAINTY_MARKERS, cascade_plan,
    cascade_requested, is_auto_request, quality_gate, query_complexity, route_request,
    router_status,
};
use serde_json::{Map, Value, json};

fn queries() -> Vec<String> {
    vec![
        String::new(),
        "   ".to_string(),
        "帮我写代码".to_string(),
        "```python\nprint(1)".to_string(),
        "how to fix this traceback".to_string(),
        "做一份 PPT".to_string(),
        "看看 mind map".to_string(),
        "你好".to_string(),
        "please explain".to_string(),
        "short question?".to_string(),
        "a".repeat(1300),
        "解释".to_string() + &"啊".repeat(500),
        "啊".repeat(200),
    ]
}

fn image_payloads() -> Vec<Value> {
    vec![
        json!({}),
        json!({"messages": "not-a-list"}),
        json!({"messages": [{"role": "user"}]}),
        json!({"messages": [{"role": "user", "attachments": "x"}]}),
        json!({"messages": [{"role": "user", "attachments": [{"imageData": "data:image/png;base64,AAAA"}]}]}),
        json!({"messages": [{"role": "user", "attachments": [{"imageData": "http://x/y.png"}]}]}),
        json!({"messages": [{"role": "user", "attachments": [{"imageData": ""}, {"imageData": "data:image/jpeg;base64,B"}]}]}),
        json!({"messages": ["not-a-dict", {"role": "user", "attachments": [{"imageData": "data:image/"}]}]}),
        json!({"messages": [{"role": "user", "attachments": [{"imageData": 5}]}]}),
    ]
}

fn auto_payloads() -> Vec<Value> {
    vec![
        json!({}),
        json!({"model": "auto"}),
        json!({"model": "AUTO"}),
        json!({"model": " auto "}),
        json!({"model": "auto", "autoRoute": true}),
        json!({"autoRoute": true}),
        json!({"autoRoute": 1}),
        json!({"autoRoute": "true"}),
        json!({"model": "deepseek-v4-pro", "cascade": true}),
        json!({"cascade": 1}),
        json!({"cascade": true, "agentMode": true}),
        json!({"cascade": true, "judge": true}),
    ]
}

fn route_payloads() -> Vec<Value> {
    let image = "data:image/png;base64,AA";
    vec![
        json!({}),
        json!({"model": "flash"}),
        json!({"model": "v4pro"}),
        json!({"model": "deepseek_v4_flash"}),
        json!({"model": "unknown-model"}),
        json!({"model": ""}),
        json!({"model": 0}),
        json!({"model": null}),
        json!({"model": "auto", "messages": [{"role": "user", "content": "你好"}]}),
        json!({"model": "auto", "messages": [{"role": "user", "content": "帮我写代码"}]}),
        json!({"model": "auto", "messages": [{"role": "user", "content": "啊".repeat(200)}]}),
        json!({"model": "auto", "messages": [{"role": "user", "content": "帮我写代码"}], "attachments": []}),
        json!({"model": "auto", "messages": [{"role": "user", "content": "x", "attachments": [{"imageData": image}]}]}),
        json!({"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "x", "attachments": [{"imageData": image}]}]}),
        json!({"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "x", "attachments": [{"imageData": image}]}]}),
    ]
}

fn quality_cases() -> Vec<(String, i64, bool)> {
    let pad = "a".repeat(200);
    vec![
        (String::new(), 80, false),
        ("   ".to_string(), 80, false),
        ("太短了".to_string(), 80, false),
        (pad.clone(), 80, false),
        (format!("很抱歉，我无法回答这个问题。{pad}"), 80, false),
        (
            format!("我不确定这一点，而且无法确定答案。{pad}"),
            80,
            false,
        ),
        (format!("我不确定这一点。{pad}"), 80, false),
        (format!("结论见 [^W1]，细节略。{pad}"), 80, true),
        (format!("结论见文档，细节略。{pad}"), 80, true),
        (format!("The answer is [^f2].{pad}"), 80, true),
        (format!("I cannot help with that.{pad}"), 80, false),
        (
            format!("As an AI language model, I am unable to help.{pad}"),
            80,
            false,
        ),
        (pad, 500, false),
    ]
}

/// (enabled, cascade_enabled, judge_enabled, judge_model, judge_threshold, draft_model,
/// refine_model, cascade_min_chars, cost_budget_tokens)
type SettingsCase = (
    bool,
    bool,
    bool,
    &'static str,
    f64,
    &'static str,
    &'static str,
    i64,
    i64,
);

const SETTINGS_CASES: [SettingsCase; 3] = [
    (
        true,
        true,
        false,
        "deepseek-v4-flash",
        0.6,
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        80,
        0,
    ),
    (
        false,
        true,
        false,
        "deepseek-v4-flash",
        0.6,
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        80,
        0,
    ),
    (
        true,
        false,
        true,
        "deepseek-v4-pro",
        0.9,
        "ollama/qwen",
        "deepseek-v4-pro",
        10,
        50,
    ),
];

fn settings(case: SettingsCase) -> ModelRouterSettings {
    ModelRouterSettings {
        enabled: case.0,
        cascade_enabled: case.1,
        judge_enabled: case.2,
        judge_model: case.3.to_string(),
        judge_threshold: case.4,
        draft_model: case.5.to_string(),
        refine_model: case.6.to_string(),
        cascade_min_chars: case.7,
        cost_budget_tokens: case.8,
        ..ModelRouterSettings::default()
    }
}

fn main() {
    let mut out: Map<String, Value> = Map::new();

    for (key, pattern) in [
        ("pattern::complex", COMPLEX_QUERY_PATTERN),
        ("pattern::artifact", ARTIFACT_QUERY_PATTERN),
        ("pattern::simple", SIMPLE_TASK_PATTERN),
        ("pattern::citation", CITATION_PATTERN),
    ] {
        out.insert(key.to_string(), json!(pattern));
    }
    out.insert(
        "markers::uncertainty".to_string(),
        json!(UNCERTAINTY_MARKERS),
    );
    out.insert("markers::refusal".to_string(), json!(REFUSAL_MARKERS));

    for (index, query) in queries().iter().enumerate() {
        out.insert(
            format!("complexity::{index}"),
            json!(query_complexity(query)),
        );
    }

    for (index, payload) in image_payloads().iter().enumerate() {
        out.insert(
            format!("messages::{index}"),
            json!(chat_messages_from_payload(payload)),
        );
        out.insert(
            format!("has-image::{index}"),
            json!(has_image_attachment(payload)),
        );
    }

    for (settings_index, case) in SETTINGS_CASES.iter().enumerate() {
        let router = settings(*case);
        out.insert(format!("status-s{settings_index}"), router_status(&router));
        for (payload_index, payload) in auto_payloads().iter().enumerate() {
            out.insert(
                format!("auto-s{settings_index}::{payload_index}"),
                json!(is_auto_request(payload, &router)),
            );
            out.insert(
                format!("cascade-req-s{settings_index}::{payload_index}"),
                json!(cascade_requested(payload, &router)),
            );
            out.insert(
                format!("cascade-plan-s{settings_index}::{payload_index}"),
                cascade_plan(payload, &router).to_value(),
            );
        }
        for (payload_index, payload) in route_payloads().iter().enumerate() {
            for budget_used in [0i64, 40] {
                out.insert(
                    format!("route-s{settings_index}::{payload_index}::b{budget_used}"),
                    route_request(payload, budget_used, &router).to_value(),
                );
            }
        }
    }

    for (index, (content, min_chars, require_citations)) in quality_cases().iter().enumerate() {
        out.insert(
            format!("gate::{index}"),
            quality_gate(content, *min_chars, *require_citations).to_value(),
        );
    }

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
