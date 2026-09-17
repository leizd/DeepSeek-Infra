//! Context-engine parity probe, Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/context_engine_parity_probe.py` through
//! `deepseek_policy::context_engine`.
//!
//! Usage::
//!
//!     python tasks/native-runtime/context_engine_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example context_engine_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use deepseek_policy::context_engine::{
    ContextEngineSettings, available_input_tokens, base_context_id, build_context_diff,
    build_engine_diagnostics, context_window_for_model, estimate_body_breakdown,
    estimate_message_tokens, estimate_tokens, estimate_tools_tokens, plan_token_budget, token_trim,
};
use serde_json::{Map, Value, json};

/// The oracle's text corpus, in order — the two generated entries included.
fn corpus_texts() -> Vec<String> {
    vec![
        String::new(),
        "a".to_string(),
        "hello world".to_string(),
        "中文".to_string(),
        "中文 with latin".to_string(),
        "你好，世界！".to_string(),
        "ひらがなカタカナ".to_string(),
        "한국어".to_string(),
        "漢字ＡＢＣ".to_string(),
        "🙂🙂".to_string(),
        "a".repeat(400),
        "中".repeat(7),
        "mixed 中 and a".to_string(),
        "ｆｕｌｌ\u{3000}width".to_string(),
    ]
}

fn message_cases() -> Vec<Value> {
    vec![
        json!({}),
        json!({"role": "user", "content": "text"}),
        json!({"role": "user", "content": "中文内容"}),
        json!({"role": "user", "content": [{"type": "text", "text": "中文"}]}),
        json!({"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u"}}]}),
        json!({"role": "user", "content": [{"type": "text"}, {"type": "image_url"}]}),
        json!({"role": "assistant", "content": "ok", "tool_calls": [{"function": {"name": "web_search", "arguments": "{\"q\": \"中文\"}"}}]}),
        json!({"role": "assistant", "tool_calls": ["bad", {"function": "not-a-dict"}]}),
        json!({"role": "user", "content": 5}),
    ]
}

fn tool_arrays() -> Vec<Value> {
    vec![
        Value::Null,
        json!([]),
        json!("not-a-list"),
        json!([{"type": "function", "function": {"name": "web_search", "description": "搜索"}}]),
        json!([
            {"type": "function", "function": {"name": "tool0", "description": "描述"}},
            {"type": "function", "function": {"name": "tool1", "description": "描述"}},
            {"type": "function", "function": {"name": "tool2", "description": "描述"}},
            {"type": "function", "function": {"name": "tool3", "description": "描述"}},
        ]),
    ]
}

fn bodies() -> Vec<Value> {
    vec![
        json!({}),
        json!({"messages": []}),
        json!({"messages": [{"role": "system", "content": "role prompt"}]}),
        json!({"messages": [{"role": "system", "content": "role prompt"}, {"role": "system", "content": "dynamic"}]}),
        json!({"messages": [{"role": "system", "content": "a"}, {"role": "user", "content": "b"}, {"role": "system", "content": "c"}]}),
        json!({"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "中文"}], "tools": [{"function": {"name": "t"}}]}),
    ]
}

fn models() -> Vec<Option<&'static str>> {
    vec![
        None,
        Some(""),
        Some("deepseek-v4-pro"),
        Some("deepseek-v4-flash"),
        Some("unknown-model"),
        Some(" deepseek-v4-pro "),
    ]
}

fn identity_bodies() -> Vec<Value> {
    let mut all = bodies();
    all.extend(vec![
        json!({"messages": [{"role": "system", "content": "中文前缀"}]}),
        json!({"tools": [{"function": {"name": "a"}}, {"function": {"name": ""}}, {"function": "x"}, "not-a-dict"]}),
        json!({"tools": [{"function": {"name": 5}}]}),
        json!({"messages": [{"role": "user", "content": "x"}], "tools": [{"function": {"name": "b"}}, {"function": {"name": "a"}}]}),
        json!({"messages": [{"role": "user", "content": "x"}], "tools": [{"function": {"name": "a"}}, {"function": {"name": "b"}}]}),
    ]);
    all
}

fn trim_messages() -> Vec<Value> {
    vec![
        json!({"role": "system", "content": "stable prefix"}),
        json!({"role": "user", "content": "中".repeat(20)}),
        json!({"role": "assistant", "content": "a".repeat(40)}),
        json!({"role": "user", "content": "b".repeat(40)}),
        json!({"role": "system", "content": "dynamic tail"}),
    ]
}

fn settings_cases() -> Vec<ContextEngineSettings> {
    let windows = |entries: Vec<(&str, i64)>| {
        entries
            .into_iter()
            .map(|(name, window)| (name.to_string(), window))
            .collect::<Vec<_>>()
    };
    let base = ContextEngineSettings::default();
    vec![
        base.clone(),
        ContextEngineSettings {
            reserve_output_tokens: 0,
            safety_margin_ratio: 0.0,
            default_context_window: 40,
            min_keep_messages: 2,
            model_context_windows: windows(vec![("deepseek-v4-pro", 100)]),
            ..base.clone()
        },
        ContextEngineSettings {
            min_keep_messages: 4,
            reserve_output_tokens: 0,
            safety_margin_ratio: 0.0,
            default_context_window: 40,
            model_context_windows: windows(vec![("deepseek-v4-pro", 100)]),
            ..base.clone()
        },
        ContextEngineSettings {
            compress_threshold_pct: 1.0,
            model_context_windows: windows(vec![("deepseek-v4-pro", 131_072)]),
            ..base.clone()
        },
        ContextEngineSettings {
            reserve_output_tokens: 100_000,
            model_context_windows: windows(vec![("deepseek-v4-pro", 131_072)]),
            ..base.clone()
        },
        ContextEngineSettings {
            enabled: false,
            model_context_windows: windows(vec![("deepseek-v4-pro", 131_072)]),
            ..base
        },
    ]
}

fn main() {
    let mut out: Map<String, Value> = Map::new();
    let default = ContextEngineSettings::default();

    for (index, text) in corpus_texts().iter().enumerate() {
        out.insert(format!("tokens::{index}"), json!(estimate_tokens(text)));
    }

    for (index, message) in message_cases().iter().enumerate() {
        out.insert(
            format!("message::{index}"),
            json!(estimate_message_tokens(message)),
        );
    }

    for (index, tools) in tool_arrays().iter().enumerate() {
        let tools = if tools.is_null() { None } else { Some(tools) };
        out.insert(
            format!("tools::{index}"),
            json!(estimate_tools_tokens(tools)),
        );
    }

    for (index, body) in bodies().iter().enumerate() {
        out.insert(
            format!("breakdown::{index}"),
            estimate_body_breakdown(body).to_value(),
        );
    }

    for (index, model) in models().iter().enumerate() {
        out.insert(
            format!("window::{index}"),
            json!(context_window_for_model(*model, &default)),
        );
        out.insert(
            format!("available::{index}"),
            json!(available_input_tokens(*model, &default)),
        );
    }

    for (index, body) in bodies().iter().enumerate() {
        out.insert(
            format!("plan::{index}"),
            plan_token_budget(body, None, &default).to_value(),
        );
    }

    for (settings_index, settings) in settings_cases().iter().enumerate() {
        for (model_index, model) in models().iter().enumerate() {
            out.insert(
                format!("window-s{settings_index}::{model_index}"),
                json!(context_window_for_model(*model, settings)),
            );
            out.insert(
                format!("available-s{settings_index}::{model_index}"),
                json!(available_input_tokens(*model, settings)),
            );
        }
        out.insert(
            format!("plan-s{settings_index}"),
            plan_token_budget(&bodies()[5], None, settings).to_value(),
        );
        let messages = trim_messages();
        let (trimmed, dropped) = token_trim(&messages, None, 0, settings);
        let roles: Vec<Value> = trimmed
            .iter()
            .map(|item| item.get("role").cloned().unwrap_or(Value::Null))
            .collect();
        out.insert(format!("trim-s{settings_index}::dropped"), json!(dropped));
        out.insert(
            format!("trim-s{settings_index}::kept"),
            json!(trimmed.len()),
        );
        out.insert(format!("trim-s{settings_index}::roles"), json!(roles));
        out.insert(
            format!("trim-s{settings_index}::first"),
            trimmed
                .first()
                .and_then(|item| item.get("content"))
                .cloned()
                .unwrap_or(Value::Null),
        );
        out.insert(
            format!("trim-s{settings_index}::last"),
            trimmed
                .last()
                .and_then(|item| item.get("content"))
                .cloned()
                .unwrap_or(Value::Null),
        );
        out.insert(
            format!("trim-s{settings_index}::overhead"),
            json!(token_trim(&messages, None, 6, settings).1),
        );
    }

    let id_bodies = identity_bodies();
    for (index, body) in id_bodies.iter().enumerate() {
        out.insert(format!("base::{index}"), json!(base_context_id(body)));
        out.insert(format!("diff::{index}"), build_context_diff(body, 0));
        out.insert(
            format!("engine::{index}"),
            build_engine_diagnostics(body, None, 0, &default),
        );
    }
    // Tool order is part of the prefix identity, and the last two bodies differ only there.
    out.insert(
        "base::order-swapped".to_string(),
        json!(base_context_id(&id_bodies[3]) != base_context_id(&id_bodies[4])),
    );
    out.insert(
        "diff::dropped".to_string(),
        build_context_diff(&bodies()[4], 3),
    );
    out.insert(
        "engine-s1".to_string(),
        build_engine_diagnostics(&bodies()[5], None, 0, &settings_cases()[1]),
    );

    let (empty, empty_dropped) = token_trim(&[], None, 0, &default);
    out.insert("trim::empty".to_string(), json!(empty));
    out.insert("trim::empty-dropped".to_string(), json!(empty_dropped));

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
