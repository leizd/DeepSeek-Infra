//! Batch tool execution, mirroring `execute_tool_calls` in
//! `deepseek_infra/infra/tool_runtime/tools.py`.
//!
//! This is the layer that turns a model's list of tool calls into the `role:
//! "tool"` messages fed back to it. Two things here are contracts rather than
//! mechanics:
//!
//! 1. **Batching.** Parallel-safe calls accumulate into a group; a serial call
//!    flushes the group first and then runs alone. The grouping is observable
//!    because outputs are always written back by *original index*, so the plan is
//!    exposed separately via [`plan_batches`] where it can be asserted directly.
//! 2. **The model-facing content.** [`tool_result_message`] serializes the result
//!    with Python's compact separators and truncates to
//!    [`MAX_TOOL_RESULT_CHARS`]. That string is literally what the model reads
//!    back, so its separators and truncation point are part of the contract.
//!
//! Concurrency is deliberately **not** reproduced: the oracle runs a group on a
//! thread pool, but every result is written back by its original index, so
//! running a group sequentially produces the same list. What *is* reproduced is
//! the cancellation post-condition — a group interrupted part-way leaves its
//! remaining slots empty, which the assembly then resolves to a cancelled
//! envelope rather than to a generic "did not run".

use serde_json::{Value, json};

use crate::python_json::{dumps_compact, value_str};
use crate::tool_dispatch::{
    DispatchOutcome, INTERNAL, MAX_TOOL_CALLS_PER_RESPONSE, is_parallel_safe_tool, tool_call_name,
};

/// `MAX_TOOL_RESULT_CHARS` — the truncation applied to the model-facing content.
pub const MAX_TOOL_RESULT_CHARS: usize = 12_000;

/// `Tool did not run` — the fallback envelope for a slot that produced nothing.
pub const DID_NOT_RUN: &str = "Tool did not run";

/// How the selected calls are grouped for execution.
///
/// Returned as data so the batching rule can be asserted without running anything.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BatchStep {
    /// One serial call, run on its own.
    Serial(usize),
    /// Consecutive parallel-safe calls, run as one group.
    Parallel(Vec<usize>),
}

/// Plan the batches for the selected calls, mirroring the oracle's loop.
pub fn plan_batches(selected: &[Value]) -> Vec<BatchStep> {
    let mut steps = Vec::new();
    let mut group: Vec<usize> = Vec::new();
    for (index, tool_call) in selected.iter().enumerate() {
        if is_parallel_safe_tool(tool_call) {
            group.push(index);
            continue;
        }
        if !group.is_empty() {
            steps.push(BatchStep::Parallel(std::mem::take(&mut group)));
        }
        steps.push(BatchStep::Serial(index));
    }
    if !group.is_empty() {
        steps.push(BatchStep::Parallel(group));
    }
    steps
}

/// The envelope for a call that was cancelled before or during execution.
pub fn cancelled_output(tool_call: &Value) -> Value {
    let name = tool_call_name(tool_call);
    json!({
        "ok": false,
        "tool": if name.is_empty() { "unknown" } else { &name },
        "error": "Request cancelled before tool execution completed",
        "code": INTERNAL,
    })
}

/// The envelope for a slot that produced nothing and was not cancelled.
pub fn did_not_run_output(tool_call: &Value) -> Value {
    let name = tool_call_name(tool_call);
    json!({
        "ok": false,
        "tool": if name.is_empty() { "unknown" } else { &name },
        "error": DID_NOT_RUN,
        "code": INTERNAL,
    })
}

/// Mirrors `strip_volatile_tool_fields`: drops a `cached` key at any depth.
///
/// `cached` reports whether the upstream answered from its own cache — noise the
/// model should not see, and something that would differ between identical calls.
pub fn strip_volatile_tool_fields(value: &Value) -> Value {
    match value {
        Value::Object(fields) => Value::Object(
            fields
                .iter()
                .filter(|(key, _)| key.as_str() != "cached")
                .map(|(key, item)| (key.clone(), strip_volatile_tool_fields(item)))
                .collect(),
        ),
        Value::Array(items) => Value::Array(items.iter().map(strip_volatile_tool_fields).collect()),
        other => other.clone(),
    }
}

/// Mirrors `stable_tool_output_for_model`.
///
/// **Deferred path:** the oracle compacts artifact output for `create_pptx`,
/// `create_document` and `create_mindmap`. That helper lands with those branches,
/// none of which is ported, so the path is unreachable today. Output currently
/// passes through unchanged for them, and
/// `artifact_output_passes_through_until_those_branches_land` pins that so the gap
/// stays visible instead of becoming a silent difference.
pub fn stable_tool_output_for_model(output: &Value) -> Value {
    match tool_name_of(output).as_str() {
        "web_search" | "compare_search_results" => strip_volatile_tool_fields(output),
        _ => output.clone(),
    }
}

/// Mirrors `tool_result_message`: the `role: "tool"` message the model reads.
pub fn tool_result_message(tool_call: &Value, output: &Value) -> Value {
    // `str(tool_call.get("id") or "")` — a falsy id becomes the empty string.
    let tool_call_id = match tool_call.get("id") {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(text)) if text.is_empty() => String::new(),
        Some(id) => value_str(id),
    };
    let content: String = dumps_compact(&stable_tool_output_for_model(output))
        .chars()
        .take(MAX_TOOL_RESULT_CHARS)
        .collect();
    json!({
        "role": "tool",
        "tool_call_id": tool_call_id,
        "name": tool_name_of(output),
        "content": content,
    })
}

fn tool_name_of(output: &Value) -> String {
    match output.get("tool").and_then(Value::as_str) {
        Some(name) if !name.is_empty() => name.to_string(),
        _ => String::new(),
    }
}

/// Run a list of tool calls and assemble the `role: "tool"` messages.
///
/// `run` executes one call (normally `tool_dispatch::dispatch`); `is_cancelled` is
/// polled at the same four points the oracle polls it.
pub fn execute_tool_calls(
    tool_calls: &[Value],
    is_cancelled: &dyn Fn() -> bool,
    run: &dyn Fn(&Value) -> DispatchOutcome,
) -> Vec<Value> {
    let selected: Vec<Value> = tool_calls
        .iter()
        .take(MAX_TOOL_CALLS_PER_RESPONSE)
        .cloned()
        .collect();
    let mut outputs: Vec<Option<Value>> = vec![None; selected.len()];
    let mut group: Vec<usize> = Vec::new();

    for (index, tool_call) in selected.iter().enumerate() {
        if is_cancelled() {
            outputs[index] = Some(cancelled_output(tool_call));
            continue;
        }
        if is_parallel_safe_tool(tool_call) {
            group.push(index);
            continue;
        }
        flush_group(&mut group, &selected, &mut outputs, is_cancelled, run);
        // The serial path goes through `run_call`, which re-checks cancellation.
        outputs[index] = Some(if is_cancelled() {
            cancelled_output(tool_call)
        } else {
            outcome_to_output(run(tool_call))
        });
    }
    flush_group(&mut group, &selected, &mut outputs, is_cancelled, run);

    let mut results = Vec::with_capacity(selected.len());
    for (tool_call, output) in selected.iter().zip(outputs.iter()) {
        let resolved = match output {
            None if is_cancelled() => cancelled_output(tool_call),
            None => did_not_run_output(tool_call),
            Some(value) => value.clone(),
        };
        results.push(tool_result_message(tool_call, &resolved));
    }
    results
}

/// Run one accumulated group. An interruption leaves the remaining slots empty,
/// which is what the assembly turns into cancelled envelopes.
fn flush_group(
    group: &mut Vec<usize>,
    selected: &[Value],
    outputs: &mut [Option<Value>],
    is_cancelled: &dyn Fn() -> bool,
    run: &dyn Fn(&Value) -> DispatchOutcome,
) {
    if group.is_empty() {
        return;
    }
    for index in std::mem::take(group) {
        if is_cancelled() {
            break;
        }
        outputs[index] = Some(outcome_to_output(run(&selected[index])));
    }
}

/// A dispatched branch that never ran has no envelope; the batch layer must not
/// invent a success, so it surfaces the "did not run" envelope carrying the name.
fn outcome_to_output(outcome: DispatchOutcome) -> Value {
    match outcome.to_output() {
        Some(value) => value.clone(),
        None => json!({
            "ok": false,
            "tool": match &outcome {
                DispatchOutcome::Unported { tool, .. } => tool.clone(),
                _ => "unknown".to_string(),
            },
            "error": DID_NOT_RUN,
            "code": INTERNAL,
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn call(name: &str, id: &str) -> Value {
        json!({"id": id, "function": {"name": name, "arguments": "{}"}})
    }

    /// A runner that always succeeds, recording which calls it saw.
    fn noop_outcome(_call: &Value) -> DispatchOutcome {
        DispatchOutcome::Executed(json!({"ok": true, "tool": "generate_chart", "result": {}}))
    }

    #[test]
    fn batching_groups_parallel_calls_and_isolates_serial_ones() {
        let selected = vec![
            call("generate_chart", "1"),
            call("generate_chart", "2"),
            call("web_search", "3"),
            call("generate_chart", "4"),
        ];
        assert_eq!(
            plan_batches(&selected),
            vec![
                BatchStep::Parallel(vec![0, 1]),
                BatchStep::Serial(2),
                BatchStep::Parallel(vec![3]),
            ]
        );
    }

    /// A serial call at index 0 flushes the (still empty) group and runs first;
    /// the group only accumulates afterwards. My first version of this test listed
    /// the group first — the oracle's loop is index-ordered, so the serial call
    /// comes first even though it is "serial".
    #[test]
    fn a_leading_serial_call_runs_before_the_group_that_follows() {
        let selected = vec![call("web_search", "1"), call("generate_chart", "2")];
        assert_eq!(
            plan_batches(&selected),
            vec![BatchStep::Serial(0), BatchStep::Parallel(vec![1])]
        );
    }

    #[test]
    fn selection_is_capped_at_six_calls() {
        let calls: Vec<Value> = (0..10)
            .map(|index| call("generate_chart", &index.to_string()))
            .collect();
        let results = execute_tool_calls(&calls, &|| false, &noop_outcome);
        assert_eq!(results.len(), 6);
    }

    #[test]
    fn results_are_ordered_by_original_index_and_carry_the_call_id() {
        let calls = vec![call("generate_chart", "a"), call("web_search", "b")];
        let results = execute_tool_calls(&calls, &|| false, &noop_outcome);
        assert_eq!(results[0]["tool_call_id"], "a");
        assert_eq!(results[1]["tool_call_id"], "b");
        assert_eq!(results[0]["role"], "tool");
    }

    #[test]
    fn a_cancelled_batch_marks_every_slot_cancelled() {
        let calls = vec![call("generate_chart", "a"), call("web_search", "b")];
        let results = execute_tool_calls(&calls, &|| true, &noop_outcome);
        for result in &results {
            let content = result["content"].as_str().unwrap();
            assert!(content.contains("Request cancelled before tool execution completed"));
            assert!(content.contains("\"ok\":false"));
        }
    }

    /// A group interrupted part-way leaves the remainder empty, and the assembly
    /// turns those into cancelled envelopes rather than "did not run".
    #[test]
    fn an_interrupted_group_becomes_cancelled_not_did_not_run() {
        let calls = vec![call("generate_chart", "a"), call("generate_chart", "b")];
        // `Cell` rather than a plain counter: `execute_tool_calls` takes `&dyn Fn`,
        // so the predicate must not need a mutable borrow.
        let remaining = std::cell::Cell::new(1);
        let should_cancel = || {
            let left = remaining.get();
            remaining.set(left - 1);
            left <= 0
        };
        let results = execute_tool_calls(&calls, &should_cancel, &noop_outcome);
        let last = results[1]["content"].as_str().unwrap();
        assert!(last.contains("Request cancelled before tool execution completed"));
        assert!(!last.contains(DID_NOT_RUN));
    }

    #[test]
    fn an_unported_branch_yields_did_not_run_rather_than_success() {
        let calls = vec![call("fetch_url", "a")];
        let unported = |_call: &Value| DispatchOutcome::Unported {
            tool: "fetch_url".to_string(),
            branch: crate::tool_dispatch::Branch::FetchUrl,
        };
        let results = execute_tool_calls(&calls, &|| false, &unported);
        let content = results[0]["content"].as_str().unwrap();
        assert!(content.contains(DID_NOT_RUN));
        assert!(content.contains("\"ok\":false"));
        assert!(!content.contains("\"ok\":true"));
    }

    #[test]
    fn a_nameless_call_is_labelled_unknown() {
        let calls = vec![json!({"id": ""})];
        let results = execute_tool_calls(&calls, &|| true, &noop_outcome);
        let content = results[0]["content"].as_str().unwrap();
        assert!(content.contains("\"tool\":\"unknown\""));
        // A blank id becomes an empty string, not "None".
        assert_eq!(results[0]["tool_call_id"], "");
    }

    #[test]
    fn volatile_fields_are_stripped_recursively() {
        let output = json!({
            "tool": "web_search",
            "cached": true,
            "result": {"items": [{"cached": false, "title": "a"}]},
        });
        let stable = stable_tool_output_for_model(&output);
        assert!(stable.get("cached").is_none());
        assert!(stable["result"]["items"][0].get("cached").is_none());
        assert_eq!(stable["result"]["items"][0]["title"], "a");
    }

    /// Pins the deferred artifact-compaction path so it cannot quietly become a
    /// difference once those branches land.
    #[test]
    fn artifact_output_passes_through_until_those_branches_land() {
        let output = json!({"tool": "create_pptx", "result": {"title": "t"}});
        assert_eq!(stable_tool_output_for_model(&output), output);
    }

    #[test]
    fn content_is_compact_json_truncated_to_the_character_cap() {
        let call = call("generate_chart", "a");
        let output = json!({"ok": true, "tool": "generate_chart", "result": {"a": 1}});
        let message = tool_result_message(&call, &output);
        assert_eq!(
            message["content"],
            "{\"ok\":true,\"result\":{\"a\":1},\"tool\":\"generate_chart\"}"
        );

        // Over-long content is cut at the cap, counted in code points.
        let big = json!({"ok": true, "tool": "generate_chart", "result": "x".repeat(20_000)});
        let truncated = tool_result_message(&call, &big);
        assert_eq!(
            truncated["content"].as_str().unwrap().chars().count(),
            MAX_TOOL_RESULT_CHARS
        );
    }
}
