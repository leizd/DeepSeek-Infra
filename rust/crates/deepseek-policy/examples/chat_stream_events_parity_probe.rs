//! chat NDJSON event parity probe, Rust side.
//!
//! Pins `encode_stream_event` — the `POST /api/chat` line format — and the stream
//! accumulator's totals against the oracle. The route's transport is pinned by
//! `rust/crates/deepseek-gateway/tests/chat_ndjson_route.rs`; what this probe compares
//! is the bytes of each event and the arithmetic of a multi-round turn.
//!
//! Run against `tasks/native-runtime/chat_stream_events_parity_probe.py`; the two
//! outputs must be byte-identical.
//!
//! ```text
//! python tasks/native-runtime/chat_stream_events_parity_probe.py > python.json
//! cd rust && cargo run -p deepseek-policy --example chat_stream_events_parity_probe > ../rust.json
//! ```
//!
//! Keys come from a `BTreeMap` so the order is sorted whatever `serde_json` was built
//! with; the Python side uses `sort_keys=True` for the same bytes.

use std::collections::BTreeMap;

use deepseek_policy::chat_diagnostics::{
    diagnostics_with_search, diagnostics_with_tools, diagnostics_with_usage, search_round_count,
};
use deepseek_policy::chat_stream_events::{
    ChatEvent, ChatStreamAccumulator, STREAM_MEDIA_TYPE, StreamToolCalls, compact_event_json,
    encode_stream_event, finalized_stream_tool_calls, merge_stream_tool_call_deltas,
    merge_usage_totals,
};
use serde_json::{Value, json};

/// The event corpus: one entry per vocabulary member, plus the shapes that only a
/// careful reading of the oracle predicts.
fn events() -> Vec<(&'static str, ChatEvent)> {
    vec![
        (
            "system_note_plain",
            ChatEvent::SystemNote {
                text: "hello".to_string(),
            },
        ),
        (
            "system_note_newlines",
            ChatEvent::SystemNote {
                text: "line\n\n".to_string(),
            },
        ),
        (
            "system_note_tool",
            ChatEvent::SystemNote {
                text: "正在调用本地工具：create_document\n\n".to_string(),
            },
        ),
        (
            "system_note_limit",
            ChatEvent::SystemNote {
                text: "工具调用次数已达上限，改为直接整理最终回答。\n\n".to_string(),
            },
        ),
        (
            "content_ascii",
            ChatEvent::Content {
                text: "hi".to_string(),
            },
        ),
        (
            "content_cjk",
            ChatEvent::Content {
                text: "你好世界".to_string(),
            },
        ),
        (
            "content_quote",
            ChatEvent::Content {
                text: "a\"b\\c".to_string(),
            },
        ),
        (
            "content_tab",
            ChatEvent::Content {
                text: "a\tb".to_string(),
            },
        ),
        (
            "content_empty",
            ChatEvent::Content {
                text: String::new(),
            },
        ),
        (
            "reasoning_plain",
            ChatEvent::Reasoning {
                text: "think".to_string(),
            },
        ),
        (
            "reasoning_emoji",
            ChatEvent::Reasoning {
                text: "思考 🎉".to_string(),
            },
        ),
        (
            "search_null",
            ChatEvent::Search {
                search: Value::Null,
            },
        ),
        ("search_scalar", ChatEvent::Search { search: json!(1) }),
        (
            "error_plain",
            ChatEvent::Error {
                error: "boom".to_string(),
                code: "internal".to_string(),
            },
        ),
        (
            "error_cjk",
            ChatEvent::Error {
                error: "上游流式连接中断（ConnectionResetError）".to_string(),
                code: "upstream_failure".to_string(),
            },
        ),
        (
            "memory_suggestion_no_type",
            ChatEvent::MemorySuggestion {
                suggestion: json!({"content": "x"}),
            },
        ),
        (
            "memory_suggestion_own_type",
            ChatEvent::MemorySuggestion {
                suggestion: json!({"content": "记住我喜欢喝咖啡", "type": "instruction"}),
            },
        ),
        (
            "memory_suggestion_not_object",
            ChatEvent::MemorySuggestion {
                suggestion: json!("scalar"),
            },
        ),
        ("done_empty", ChatStreamAccumulator::new().done(json!({}))),
        (
            "done_full",
            ChatEvent::Done(Box::new(deepseek_policy::chat_stream_events::ChatDone {
                id: json!("resp-1"),
                model: "deepseek-v4-flash".to_string(),
                content: "答案".to_string(),
                reasoning: "推理".to_string(),
                usage: deepseek_policy::chat_stream_events::RawJson::object(&[
                    ("prompt_tokens", json!(10)),
                    ("completion_tokens", json!(4)),
                    ("total_tokens", json!(14)),
                ]),
                search: Value::Null,
                memory_suggestions: json!([{"content": "x"}]),
                finish_reason: "stop".to_string(),
                diagnostics: json!({"tools": {"count": 0}}),
            })),
        ),
        (
            "done_null_id",
            ChatEvent::Done(Box::new(deepseek_policy::chat_stream_events::ChatDone {
                id: Value::Null,
                model: "m".to_string(),
                content: String::new(),
                reasoning: String::new(),
                usage: deepseek_policy::chat_stream_events::RawJson::default(),
                search: json!({"status": "done"}),
                memory_suggestions: json!([]),
                finish_reason: "length".to_string(),
                diagnostics: json!({}),
            })),
        ),
    ]
}

/// The streamed tool-call sequences the merge is compared over.
fn tool_call_cases() -> Vec<(&'static str, Vec<Value>)> {
    vec![
        ("empty", vec![]),
        (
            "single_split_arguments",
            vec![
                json!([{"index": 0, "id": "call_abc", "type": "function",
                        "function": {"name": "create_document", "arguments": "{\"title\":"}}]),
                json!([{"index": 0, "function": {"arguments": "\"x\"}"}}]),
            ],
        ),
        (
            "out_of_order_indices",
            vec![json!([
                {"index": 2, "id": "call_2", "function": {"name": "b", "arguments": "{}"}},
                {"index": 1, "id": "call_1", "function": {"name": "a", "arguments": "{}"}},
            ])],
        ),
        (
            "missing_index",
            vec![
                json!([{"function": {"name": "first", "arguments": "{}"}}]),
                json!([{"index": "1", "function": {"name": "second", "arguments": "{}"}}]),
                json!([{"index": "nope", "function": {"name": "third", "arguments": "{}"}}]),
            ],
        ),
        (
            "empty_fragments_do_not_blank",
            vec![
                json!([{"index": 0, "id": "call_keep", "type": "function",
                        "function": {"name": "keep", "arguments": "{}"}}]),
                json!([{"index": 0, "id": "", "type": "",
                        "function": {"name": "", "arguments": ""}}]),
            ],
        ),
        (
            "no_name_is_dropped",
            vec![json!([{"index": 0, "function": {"arguments": "{}"}}])],
        ),
        (
            "non_list_and_non_objects",
            vec![json!("not a list"), json!(["x", 5]), Value::Null],
        ),
        (
            "null_index_and_negative",
            vec![json!([
                {"index": null, "function": {"name": "n", "arguments": "{}"}},
                {"index": -4, "function": {"name": "neg", "arguments": "{}"}},
            ])],
        ),
    ]
}

/// The tool-name lists `diagnostics_with_tools` is compared over.
fn tool_diagnostic_cases() -> Vec<(&'static str, usize, Vec<String>)> {
    vec![
        ("empty", 0, vec![]),
        (
            "sorted_and_deduplicated",
            3,
            vec![
                "search_files".to_string(),
                "create_document".to_string(),
                "search_files".to_string(),
            ],
        ),
        (
            "case_sensitive_sort",
            2,
            vec!["Zebra".to_string(), "apple".to_string()],
        ),
        ("cjk_names", 1, vec!["创建文档".to_string()]),
    ]
}

/// The usage objects `diagnostics_with_usage` is compared over. The tie cases are the
/// point: `round(x, 1)` is half-to-even on the *decimal* value, which a naive
/// multiply-then-round gets wrong.
fn usage_diagnostic_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("no_cache_tokens", json!({"prompt_tokens": 10})),
        (
            "three_quarters",
            json!({"prompt_cache_hit_tokens": 75, "prompt_cache_miss_tokens": 25}),
        ),
        (
            "aliases",
            json!({"promptCacheHitTokens": 1, "promptCacheMissTokens": 3}),
        ),
        ("all_hits", json!({"prompt_cache_hit_tokens": 10})),
        ("all_misses", json!({"prompt_cache_miss_tokens": 10})),
        // The tie corpus: every one of these is a `round(x, 1)` that differs between
        // half-up, half-even-on-the-double, and Python's decimal rule.
        (
            "tie_1_of_19",
            json!({"prompt_cache_hit_tokens": 1, "prompt_cache_miss_tokens": 18}),
        ),
        (
            "tie_2_of_19",
            json!({"prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 17}),
        ),
        (
            "tie_3_of_19",
            json!({"prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 16}),
        ),
        (
            "tie_7_of_19",
            json!({"prompt_cache_hit_tokens": 7, "prompt_cache_miss_tokens": 12}),
        ),
        (
            "tie_11_of_19",
            json!({"prompt_cache_hit_tokens": 11, "prompt_cache_miss_tokens": 8}),
        ),
        (
            "tie_1_of_8",
            json!({"prompt_cache_hit_tokens": 1, "prompt_cache_miss_tokens": 7}),
        ),
        (
            "tie_3_of_8",
            json!({"prompt_cache_hit_tokens": 3, "prompt_cache_miss_tokens": 5}),
        ),
        (
            "tie_5_of_8",
            json!({"prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 3}),
        ),
        (
            "tie_1_of_40",
            json!({"prompt_cache_hit_tokens": 1, "prompt_cache_miss_tokens": 39}),
        ),
        (
            "tie_17_of_40",
            json!({"prompt_cache_hit_tokens": 17, "prompt_cache_miss_tokens": 23}),
        ),
        // A negative counter is floored at zero by `usage_int`, so the rate is 0.
        (
            "negative_floored",
            json!({"prompt_cache_hit_tokens": -5, "prompt_cache_miss_tokens": 5}),
        ),
        // A non-numeric value falls through to the alias, then to zero.
        (
            "non_numeric",
            json!({"prompt_cache_hit_tokens": "x", "prompt_cache_miss_tokens": 4}),
        ),
    ]
}

fn usage_cases() -> Vec<(&'static str, Value, Value)> {
    vec![
        ("both_empty", json!({}), json!({})),
        (
            "disjoint",
            json!({"prompt_tokens": 1}),
            json!({"completion_tokens": 2}),
        ),
        (
            "summed",
            json!({"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}),
            json!({"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}),
        ),
        (
            "non_numeric_replaced",
            json!({"prompt_tokens": 10, "cache": "miss"}),
            json!({"completion_tokens": 2, "cache": "hit"}),
        ),
        (
            "float_sum",
            json!({"cost": 0.0001}),
            json!({"cost": 0.0002}),
        ),
        ("round_not_object", json!({"a": 1}), json!("scalar")),
        ("alias", json!({}), json!({"promptTokens": 7})),
        (
            "negative_floored",
            json!({"prompt_tokens": 5}),
            json!({"prompt_tokens": -3}),
        ),
        (
            "fractional_truncated",
            json!({}),
            json!({"prompt_tokens": 2.7}),
        ),
        (
            "empty_string_skipped",
            json!({}),
            json!({"prompt_tokens": ""}),
        ),
        ("round_empty", json!({"a": 1}), json!({})),
    ]
}

fn main() {
    let mut out: BTreeMap<String, Value> = BTreeMap::new();
    out.insert("media_type".to_string(), json!(STREAM_MEDIA_TYPE));

    let encoded: BTreeMap<String, Value> = events()
        .into_iter()
        .map(|(label, event)| {
            let bytes = encode_stream_event(&event);
            (
                label.to_string(),
                json!({
                    "text": String::from_utf8_lossy(&bytes).to_string(),
                    "len": bytes.len(),
                    // The last byte is the newline `encode_stream_event` appends.
                    "ends_with_newline": bytes.last() == Some(&b'\n'),
                }),
            )
        })
        .collect();
    out.insert("events".to_string(), json!(encoded));

    let usage: BTreeMap<String, Value> = usage_cases()
        .into_iter()
        .map(|(label, total, round)| (label.to_string(), merge_usage_totals(&total, &round)))
        .collect();
    out.insert("usage_totals".to_string(), json!(usage));

    let tool_calls: BTreeMap<String, Value> = tool_call_cases()
        .into_iter()
        .map(|(label, chunks)| {
            let mut accumulator = StreamToolCalls::new();
            for chunk in &chunks {
                merge_stream_tool_call_deltas(&mut accumulator, Some(chunk));
            }
            let finalized = finalized_stream_tool_calls(&accumulator);
            (
                label.to_string(),
                json!({
                    // The raw accumulator, so a difference in the placeholder id or in
                    // the appended arguments shows up even when the finalizer would
                    // drop the entry.
                    "accumulated": accumulator
                        .values()
                        .cloned()
                        .collect::<Vec<Value>>(),
                    "finalized": finalized,
                }),
            )
        })
        .collect();
    out.insert("tool_calls".to_string(), json!(tool_calls));

    let diagnostics: BTreeMap<String, Value> = tool_diagnostic_cases()
        .into_iter()
        .map(|(label, count, names)| {
            (
                label.to_string(),
                diagnostics_with_tools(&json!({"base": true}), count, &names),
            )
        })
        .collect();
    out.insert("diagnostics_tools".to_string(), json!(diagnostics));

    let diagnostics: BTreeMap<String, Value> = usage_diagnostic_cases()
        .into_iter()
        .map(|(label, usage)| {
            (
                label.to_string(),
                diagnostics_with_usage(&json!({"base": true}), &usage),
            )
        })
        .collect();
    out.insert("diagnostics_usage".to_string(), json!(diagnostics));

    let rounds: BTreeMap<String, Value> = [
        ("three", json!({"rounds": [1, 2, 3]})),
        ("empty", json!({"rounds": []})),
        ("not_a_list", json!({"rounds": "no"})),
        ("absent", json!({})),
        ("null", Value::Null),
    ]
    .into_iter()
    .map(|(label, search)| (label.to_string(), json!(search_round_count(&search))))
    .collect();
    out.insert("search_round_counts".to_string(), json!(rounds));

    let search_diagnostics: BTreeMap<String, Value> = [
        ("absent", None),
        ("null", Some(json!(Value::Null))),
        ("empty_object", Some(json!({}))),
        (
            "rounds_and_results",
            Some(json!({"rounds": [1, 2], "results": [{"url": "x"}]})),
        ),
        ("rounds_only", Some(json!({"rounds": [1]}))),
        ("not_a_list", Some(json!({"rounds": "no", "results": "no"}))),
    ]
    .into_iter()
    .map(|(label, search)| {
        (
            label.to_string(),
            diagnostics_with_search(&json!({"base": true}), search.as_ref()),
        )
    })
    .collect();
    out.insert("diagnostics_search".to_string(), json!(search_diagnostics));

    // The accumulator over a scripted delta sequence: the totals the terminal event
    // repeats must equal the concatenation of what was emitted.
    let mut accumulator = ChatStreamAccumulator::new();
    let mut emitted: Vec<Value> = Vec::new();
    accumulator.observe_id(Some(&json!("resp-9")));
    accumulator.observe_id(Some(&json!("")));
    accumulator.observe_model(Some(&json!("deepseek-v4-flash")));
    for text in ["a", "bc", ""] {
        emitted.push(
            serde_json::from_str(&compact_event_json(&accumulator.content_delta(text))).unwrap(),
        );
    }
    for text in ["t1", "t2"] {
        emitted.push(
            serde_json::from_str(&compact_event_json(&accumulator.reasoning_delta(text))).unwrap(),
        );
    }
    accumulator.observe_finish_reason(Some(&json!("stop")));
    accumulator.observe_usage(Some(&json!({"prompt_tokens": 5, "completion_tokens": 1})));
    accumulator.observe_usage(Some(&json!({"prompt_tokens": 2})));
    accumulator.push_memory_suggestion(json!({"content": "x"}));
    let done = accumulator.done(json!({"d": true}));
    out.insert(
        "accumulator".to_string(),
        json!({
            "emitted": emitted,
            "content": accumulator.content(),
            "reasoning": accumulator.reasoning(),
            "finish_reason": accumulator.finish_reason(),
            "done": serde_json::from_str::<Value>(&compact_event_json(&done)).unwrap(),
        }),
    );

    let mut encoded = serde_json::to_string_pretty(&out).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}
