//! Tool-round control parity probe, Rust side.
//!
//! Replays the same fixed scripts as
//! `tasks/native-runtime/tool_round_parity_probe.py` through
//! `deepseek_gateway::tool_rounds` and prints canonical JSON, so the two outputs
//! can be diffed byte-for-byte. Key names, inputs, and ordering mirror the Python
//! probe exactly; the only intended difference is that Python's `json.dumps`
//! preserves insertion order while this workspace's `serde_json` sorts map keys,
//! which is why the probe passes `sort_keys=True` on the Python side.
//!
//! Usage::
//!
//!     python tasks/native-runtime/tool_round_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-gateway --example tool_round_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use deepseek_gateway::tool_rounds::{
    MAX_TOOL_CALLS_PER_RESPONSE, MAX_TOOL_ROUNDS, TOOL_BUDGET_EXHAUSTED_PROMPT, TOOL_BUDGET_NOTE,
    ToolCallAccumulator, append_tool_exchange, decide_round, force_final_answer_without_tools,
    normalize_tool_calls_lenient, tool_call_note, tool_names,
};
use serde_json::{Map, Value, json};

/// `(name, deltas)` — mirrors the Python probe's `DELTA_SCRIPTS` verbatim.
///
/// The inputs must be identical on both sides or the diff measures the inputs
/// rather than the implementations. This list was initially written from memory
/// and drifted (`zero`/`one`/`two` instead of `first`/`second`); the diff caught
/// it, which is the whole point of comparing outputs rather than reasoning.
fn delta_scripts() -> Vec<(&'static str, Vec<Value>)> {
    vec![
        (
            "single-call-fragmented-arguments",
            vec![
                json!([{"index": 0, "id": "call_a", "type": "function",
                        "function": {"name": "search_files", "arguments": "{\"qu"}}]),
                json!([{"index": 0, "function": {"arguments": "ery\":\"x\"}"}}]),
            ],
        ),
        (
            "later-id-overwrites-earlier",
            vec![
                json!([{"index": 0, "id": "call_1", "function": {"name": "a", "arguments": "{}"}}]),
                json!([{"index": 0, "id": "call_real"}]),
            ],
        ),
        (
            "indexless-delta-lands-last",
            vec![
                json!([{"function": {"name": "first", "arguments": "{}"}}]),
                json!([{"index": 5, "function": {"name": "five", "arguments": "{}"}}]),
                json!([{"function": {"name": "third", "arguments": "{}"}}]),
            ],
        ),
        (
            "out-of-order-indexes-sort",
            vec![
                json!([{"index": 1, "function": {"name": "second", "arguments": "{}"}}]),
                json!([{"index": 0, "function": {"name": "first", "arguments": "{}"}}]),
            ],
        ),
        (
            "placeholder-id-when-absent",
            vec![json!([{"index": 2, "function": {"name": "x", "arguments": "{}"}}])],
        ),
        (
            "non-array-and-non-object-deltas",
            vec![json!("nope"), json!([1, "two", null])],
        ),
        (
            "empty-arguments-fragment-is-ignored",
            vec![json!([{"index": 0, "id": "c", "function": {"name": "n", "arguments": ""}}])],
        ),
    ]
}

/// `(name, value)` — mirrors the Python probe's `NORMALIZE_INPUTS` verbatim.
fn normalize_inputs() -> Vec<(&'static str, Value)> {
    vec![
        ("not-a-list", json!({"not": "a list"})),
        (
            "nameless-dropped",
            json!([
                {"id": "a", "function": {"name": "  ", "arguments": "{}"}},
                {"id": "b", "function": {"name": "keep", "arguments": "{}"}},
                "not an object",
            ]),
        ),
        (
            "top-level-name-fallback",
            json!([{"name": "flat", "arguments": "{}"}]),
        ),
        (
            "object-arguments-json-encoded",
            json!([{"function": {"name": "x", "arguments": {"a": 1}}}]),
        ),
        (
            "missing-id-uses-positional",
            json!([{"function": {"name": "n", "arguments": "{}"}}]),
        ),
        (
            "missing-type-defaults-to-function",
            json!([{"id": "z", "function": {"name": "n", "arguments": "{}"}}]),
        ),
        (
            "numeric-id-is-stringified",
            json!([{"id": 123, "type": 7, "function": {"name": "n", "arguments": "{}"}}]),
        ),
        (
            "zero-id-falls-back",
            json!([{"id": 0, "function": {"name": "n", "arguments": "{}"}}]),
        ),
        (
            "empty-string-id-falls-back",
            json!([{"id": "", "function": {"name": "n", "arguments": "{}"}}]),
        ),
        (
            "nested-arguments-keep-python-separators",
            json!([{"function": {"name": "n", "arguments": {"a": 1, "b": [1, 2, {"c": "x"}]}}}]),
        ),
        (
            "non-ascii-arguments-stay-unescaped",
            json!([{"function": {"name": "n", "arguments": {"n": null, "t": true, "名": "值"}}}]),
        ),
    ]
}

/// Mirrors the Python probe's `_decide`: the oracle's inline branch, in its own
/// order.
fn decide_label(tool_call_count: usize, tool_round: usize) -> &'static str {
    match decide_round(tool_call_count, tool_round, MAX_TOOL_ROUNDS) {
        deepseek_gateway::tool_rounds::RoundDecision::Finish => "finish",
        deepseek_gateway::tool_rounds::RoundDecision::Continue => "continue",
        deepseek_gateway::tool_rounds::RoundDecision::ForceFinalAnswer => "force_final_answer",
    }
}

fn main() {
    let mut out = Map::new();

    // 1. The merge loop, run through the accumulator.
    for (name, deltas) in delta_scripts() {
        let mut accumulator = ToolCallAccumulator::new();
        for delta in &deltas {
            accumulator.merge(delta);
        }
        let finalized = accumulator.finalize();
        let names: Vec<Value> = tool_names(&finalized)
            .into_iter()
            .map(Value::String)
            .collect();
        out.insert(
            format!("merge::{name}"),
            json!({"finalized": finalized, "names": names}),
        );
    }

    // 2. Lenient normalization fed directly. The probe's "not-a-list" case still
    //    goes through the slice-taking function with an empty slice, matching
    //    Python's `normalize(value)` on a dict returning `[]`.
    for (name, value) in normalize_inputs() {
        let items = value.as_array().cloned().unwrap_or_default();
        out.insert(
            format!("normalize::{name}"),
            Value::Array(normalize_tool_calls_lenient(&items)),
        );
    }

    // 3. Round-budget decisions, replayed with the oracle's branch order.
    for (calls, tool_round) in [(0usize, 99usize), (1, 0), (1, 2), (1, 3), (3, 1)] {
        out.insert(
            format!("decide::{calls}@{tool_round}"),
            Value::String(decide_label(calls, tool_round).to_string()),
        );
    }

    // 4. Message assembly and the forced final answer.
    let body = json!({
        "model": "deepseek-v4-pro",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "tool_choice": {"type": "function", "function": {"name": "pinned"}},
    });
    let calls =
        vec![json!({"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}})];
    let results = vec![json!({"role": "tool", "tool_call_id": "c1", "content": "ok"})];

    out.insert(
        "append::object-tool-choice-released".to_string(),
        append_tool_exchange(&body, "thinking out loud", "why", &calls, &results),
    );
    out.insert(
        "append::string-tool-choice-kept".to_string(),
        append_tool_exchange(
            &json!({"messages": [], "tool_choice": "auto"}),
            "a",
            "",
            &[],
            &[],
        ),
    );
    out.insert(
        "append::empty-reasoning-omitted".to_string(),
        append_tool_exchange(&json!({"messages": []}), "answer", "", &[], &[]),
    );
    // The oracle reads `reasoning_content or reasoning`; `append_tool_exchange`
    // here takes the already-resolved reasoning string, so the alias case feeds
    // the same value the oracle would have resolved to.
    out.insert(
        "append::reasoning-alias-field".to_string(),
        append_tool_exchange(&json!({"messages": []}), "a", "alias", &[], &[]),
    );

    out.insert(
        "force::keeps-tools".to_string(),
        force_final_answer_without_tools(&body),
    );
    out.insert(
        "force::drops-choice-without-tools".to_string(),
        force_final_answer_without_tools(&json!({"messages": [], "tool_choice": "auto"})),
    );
    out.insert(
        "force::empty-tools-counts-as-none".to_string(),
        force_final_answer_without_tools(
            &json!({"messages": [], "tools": [], "tool_choice": "auto"}),
        ),
    );

    // 5. Notes and limits.
    let names = tool_names(&calls);
    out.insert(
        "note::names".to_string(),
        Value::String(tool_call_note(&names)),
    );
    out.insert(
        "note::empty".to_string(),
        Value::String(tool_call_note(&[])),
    );
    out.insert(
        "note::round-budget".to_string(),
        Value::String(TOOL_BUDGET_NOTE.to_string()),
    );
    out.insert(
        "note::budget".to_string(),
        Value::String(TOOL_BUDGET_EXHAUSTED_PROMPT.to_string()),
    );
    out.insert(
        "limits::per-response".to_string(),
        json!(MAX_TOOL_CALLS_PER_RESPONSE),
    );
    out.insert("limits::rounds".to_string(), json!(MAX_TOOL_ROUNDS));

    let document = Value::Object(out);
    let mut encoded = serde_json::to_string_pretty(&document).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
