//! title generation parity probe, Rust side.
//!
//! Pins the pure half of `POST /api/title`: the request body the upstream receives,
//! the sanitiser, and the upstream-error formatter. The route's transport and envelope
//! are pinned by `rust/crates/deepseek-gateway/tests/title_route.rs`; what this probe
//! compares is the part that decides *what the model is asked* and *what the title
//! becomes*, which is where a port drifts silently.
//!
//! Run against `tasks/native-runtime/title_parity_probe.py`; the two outputs must be
//! byte-identical.
//!
//! ```text
//! python tasks/native-runtime/title_parity_probe.py > python.json
//! cd rust && cargo run -p deepseek-policy --example title_parity_probe > ../rust.json
//! ```
//!
//! Keys come from a `BTreeMap` so the order is sorted whatever `serde_json` was built
//! with; the Python side uses `sort_keys=True` for the same bytes.

use std::collections::BTreeMap;

use deepseek_policy::title::{
    TITLE_SYSTEM_PROMPT, format_upstream_error, sanitize_title, title_from_response,
    title_request_body, truncate,
};
use serde_json::{Value, json};

/// The sanitiser's corpus: the shapes a real model returns, plus the ones that only a
/// careful reading of `_sanitize_title` predicts.
const SANITIZE_CASES: [&str; 26] = [
    "",
    "   ",
    "简单标题",
    "「标题： 你好世界」",
    "『标题: 带书名号』",
    "《标题》",
    "\"quoted\"",
    "'single'",
    "“curly”",
    "‘curly single’",
    "`backtick`",
    "Title: Hello World",
    "title: lower",
    "标题：中文标签",
    "标题:半角标签",
    "first\nsecond",
    "first\r\nsecond",
    "  spaced   out  ",
    "topic。",
    "topic...",
    "topic，！？；：",
    "a。，！",
    "emoji 🎉 title",
    "这是一个非常长的中文标题它超过了二十四个字符的上限",
    "a very long english title that exceeds the six word guidance",
    "标题：   前导空格   ",
];

/// The `titleModel` corpus: aliases, an unsupported name, an empty string, null, and
/// a non-string. Built at run time because `json!` is not `const`.
fn model_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("absent", Value::Null),
        ("empty", json!("")),
        ("flash", json!("flash")),
        ("v4pro", json!("v4pro")),
        ("expert", json!("expert")),
        ("DeepSeek_V4_Pro", json!("DeepSeek_V4_Pro")),
        ("unsupported", json!("gpt-9")),
        ("number", json!(5)),
    ]
}

/// The message pairs the request-body comparison runs.
const MESSAGE_CASES: [(&str, &str); 6] = [
    ("hello", ""),
    ("解释一下 FastCDC", "FastCDC 是一种内容定义分块算法。"),
    ("", ""),
    ("   ", "ignored"),
    ("a", ""),
    ("长文本", "短"),
];

fn main() {
    let mut out: BTreeMap<String, Value> = BTreeMap::new();

    out.insert("system_prompt".to_string(), json!(TITLE_SYSTEM_PROMPT));

    let sanitized: BTreeMap<String, Value> = SANITIZE_CASES
        .iter()
        .map(|case| ((*case).to_string(), json!(sanitize_title(case))))
        .collect();
    out.insert("sanitize".to_string(), json!(sanitized));

    let truncated: BTreeMap<String, Value> = [
        ("short_3", "hello", 3usize),
        ("exact_3", "abc", 3),
        ("over_3", "abcdef", 3),
        ("spaces_3", "  hello  ", 3),
        ("cjk_exact_4", "中文标题", 4),
        ("cjk_over_3", "中文标题", 3),
        ("long_10", "0123456789", 10),
        ("empty_5", "", 5),
        ("blank_5", "   ", 5),
    ]
    .iter()
    .map(|(label, value, limit)| ((*label).to_string(), json!(truncate(value, *limit))))
    .collect();
    out.insert("truncate".to_string(), json!(truncated));

    let bodies: BTreeMap<String, Value> = MESSAGE_CASES
        .iter()
        .map(|(user, assistant)| {
            let label = format!("{user}|{assistant}");
            let body = title_request_body(&json!({
                "userMessage": user,
                "assistantMessage": assistant,
            }));
            (label, body.unwrap_or(Value::Null))
        })
        .collect();
    out.insert("request_bodies".to_string(), json!(bodies));

    let models: BTreeMap<String, Value> = model_cases()
        .into_iter()
        .map(|(label, title_model)| {
            let payload = if title_model.is_null() {
                json!({"userMessage": "hi"})
            } else {
                json!({"userMessage": "hi", "titleModel": title_model})
            };
            let model = title_request_body(&payload)
                .and_then(|body| body.get("model").cloned())
                .unwrap_or(Value::Null);
            (label.to_string(), model)
        })
        .collect();
    out.insert("models".to_string(), json!(models));

    let responses: BTreeMap<String, Value> = [
        (
            "plain",
            json!({"choices": [{"message": {"content": "标题"}}]}),
        ),
        (
            "wrapped",
            json!({"choices": [{"message": {"content": "「标题： 你好」"}}]}),
        ),
        ("empty_choices", json!({"choices": []})),
        ("no_choices", json!({})),
        (
            "null_content",
            json!({"choices": [{"message": {"content": null}}]}),
        ),
        ("missing_message", json!({"choices": [{}]})),
        (
            "extra_choice",
            json!({"choices": [
                {"message": {"content": "first"}},
                {"message": {"content": "second"}},
            ]}),
        ),
    ]
    .iter()
    .map(|(label, response)| ((*label).to_string(), json!(title_from_response(response))))
    .collect();
    out.insert("responses".to_string(), json!(responses));

    let long_body = "x".repeat(600);
    let errors: BTreeMap<String, Value> = [
        (
            "message",
            r#"{"error": {"message": "Invalid API key"}}"#.to_string(),
        ),
        (
            "type_only",
            r#"{"error": {"type": "rate_limit"}}"#.to_string(),
        ),
        ("empty_error", r#"{"error": {}}"#.to_string()),
        ("not_json", "not json".to_string()),
        ("empty", String::new()),
        ("long", long_body),
        ("nested", r#"{"error": {"message": "quota"}}"#.to_string()),
    ]
    .into_iter()
    .map(|(label, raw)| (label.to_string(), json!(format_upstream_error(&raw))))
    .collect();
    out.insert("upstream_errors".to_string(), json!(errors));

    let mut encoded = serde_json::to_string_pretty(&out).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}
