//! Per-turn dynamic context parity probe, Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/dynamic_context_parity_probe.py`
//! through `deepseek_policy::dynamic_context`.
//!
//! The Python probe stubs `format_current_time_context` with the anchored output for the
//! assembly cases, because the oracle's builder reads the machine clock; here the same
//! anchor is what the injected clock carries. The `time::` cases below exercise the real
//! function over the same eight instants.
//!
//! Usage::
//!
//!     python tasks/native-runtime/dynamic_context_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example dynamic_context_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use deepseek_policy::dynamic_context::{
    CONTEXT_SUMMARY_MAX_CHARS, CURRENT_TIME_CONTEXT_HEADER, DynamicContextEnv, LocalNow,
    SLIDES_RUNTIME_GUIDANCE, SLIDES_SKILL_NAME, SLIDES_SKILL_REFERENCE, WEB_SEARCH_SYSTEM_HINT,
    append_context_to_latest_user, build_dynamic_turn_context, format_context_summary_context,
    format_current_time_context, format_memory_notice, format_slides_skill_context,
};
use serde_json::{Map, Value, json};

/// (epoch_seconds, offset_seconds, tzname), in the oracle's order.
const TIME_CASES: [(i64, i32, &str); 8] = [
    (1758096268, 0, "UTC"),
    (1758096268, 28800, "China Standard Time"),
    (1758096268, -18000, "Eastern Standard Time"),
    (1758096268, 19800, "India Standard Time"),
    (1758096268, -34200, "Marquesas Time"),
    (0, 0, "UTC"),
    (1758067200, 28800, "China Standard Time"),
    (1758153600, 28800, "China Standard Time"),
];

/// Whose formatted output is the anchor the assembly cases pin the clock to.
const ANCHOR_INDEX: usize = 1;

fn local_now(case: (i64, i32, &str)) -> LocalNow {
    LocalNow {
        epoch_seconds: case.0,
        offset_seconds: case.1,
        timezone_name: case.2.to_string(),
    }
}

/// (payload, memory_state, tools_enabled), in the oracle's order.
fn corpus() -> Vec<(Value, Value, bool)> {
    vec![
        (json!({}), json!({}), true),
        (json!({"searchContext": "hits"}), json!({}), true),
        (
            json!({"searchContext": "  spaced  ", "continuationContext": "cont"}),
            json!({}),
            true,
        ),
        (
            json!({"contextSummary": "较早的摘要"}),
            json!({"context": "记忆内容", "notice": "已保存"}),
            true,
        ),
        (
            json!({"searchEnabled": true, "searchMode": "on"}),
            json!({}),
            true,
        ),
        (
            json!({"searchEnabled": true, "searchMode": "off"}),
            json!({}),
            true,
        ),
        (
            json!({"messages": [{"role": "user", "content": "帮我做一份 PPT"}]}),
            json!({}),
            true,
        ),
        (
            json!({"messages": [{"role": "user", "content": "什么是 presentation？"}]}),
            json!({}),
            true,
        ),
        (json!({"searchContext": 0}), json!({}), true),
        (json!({"searchContext": [1, 2]}), json!({}), true),
        (
            json!({"contextSummary": "x".repeat(12001)}),
            json!({}),
            true,
        ),
        (
            json!({
                "searchEnabled": true,
                "searchMode": "force",
                "contextSummary": "s",
                "continuationContext": "c",
                "searchContext": "ctx",
                "messages": [{"role": "user", "content": "生成一份幻灯片"}],
            }),
            json!({"context": "m", "notice": "n"}),
            true,
        ),
        (
            json!({"searchEnabled": true, "messages": [{"role": "user", "content": "做 PPT"}]}),
            json!({}),
            false,
        ),
        (
            json!({"searchEnabled": true, "searchMode": "on"}),
            json!({}),
            false,
        ),
    ]
}

fn message_cases() -> Vec<(Vec<Value>, &'static str)> {
    vec![
        (vec![], ""),
        (vec![json!({"role": "user", "content": "hi"})], ""),
        (
            vec![json!({"role": "user", "content": "hi"})],
            "[Per-turn context]\n\nx",
        ),
        (
            vec![
                json!({"role": "system", "content": "s"}),
                json!({"role": "user", "content": "u"}),
            ],
            "ctx",
        ),
    ]
}

fn main() {
    let mut out: Map<String, Value> = Map::new();

    out.insert("header".to_string(), json!(CURRENT_TIME_CONTEXT_HEADER));
    out.insert("web-search-hint".to_string(), json!(WEB_SEARCH_SYSTEM_HINT));
    out.insert("slides-name".to_string(), json!(SLIDES_SKILL_NAME));
    out.insert(
        "slides-reference".to_string(),
        json!(SLIDES_SKILL_REFERENCE),
    );
    out.insert(
        "slides-guidance".to_string(),
        json!(SLIDES_RUNTIME_GUIDANCE),
    );
    out.insert(
        "summary-max-chars".to_string(),
        json!(CONTEXT_SUMMARY_MAX_CHARS),
    );
    out.insert(
        "slides-context".to_string(),
        json!(format_slides_skill_context()),
    );

    for (index, case) in TIME_CASES.iter().enumerate() {
        out.insert(
            format!("time::{index}"),
            json!(format_current_time_context(&local_now(*case))),
        );
    }

    out.insert(
        "summary::empty".to_string(),
        json!(format_context_summary_context(
            "",
            CONTEXT_SUMMARY_MAX_CHARS
        )),
    );
    out.insert(
        "summary::short".to_string(),
        json!(format_context_summary_context(
            "abc",
            CONTEXT_SUMMARY_MAX_CHARS
        )),
    );
    out.insert(
        "summary::capped".to_string(),
        json!(format_context_summary_context(
            &"x".repeat(12001),
            CONTEXT_SUMMARY_MAX_CHARS
        )),
    );
    out.insert(
        "notice::plain".to_string(),
        json!(format_memory_notice("已保存")),
    );
    out.insert("notice::empty".to_string(), json!(format_memory_notice("")));

    let anchor = format_current_time_context(&local_now(TIME_CASES[ANCHOR_INDEX]));
    let env = DynamicContextEnv::new(local_now(TIME_CASES[ANCHOR_INDEX]));
    for (index, (payload, memory_state, tools_enabled)) in corpus().iter().enumerate() {
        out.insert(
            format!("build::{index}"),
            json!(build_dynamic_turn_context(
                payload,
                memory_state,
                *tools_enabled,
                &env
            )),
        );
    }

    for (index, (messages, context)) in message_cases().iter().enumerate() {
        out.insert(
            format!("append::{index}"),
            json!(append_context_to_latest_user(messages, context)),
        );
    }

    out.insert("anchor".to_string(), json!(anchor));

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
