//! Tool-dispatch parity probe, Rust side.
//!
//! Replays the same fixed corpus as
//! `tasks/native-runtime/tool_dispatch_parity_probe.py` through
//! `deepseek_policy::tool_dispatch` and prints canonical JSON, so the two outputs
//! can be diffed byte-for-byte.
//!
//! Only the branches that exist on both sides are compared. [DispatchOutcome
//! ::Unported] deliberately has no envelope, so a branch that has not been ported
//! cannot be made to look like one that ran — those are covered by unit tests
//! instead (see `exactly_one_branch_is_ported_and_every_other_names_its_blocker`
//! and `an_unported_branch_has_no_envelope_at_all`).
//!
//! Usage::
//!
//!     python tasks/native-runtime/tool_dispatch_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example tool_dispatch_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use deepseek_policy::tool_dispatch::{
    DispatchOutcome, SERIAL_TOOL_NAMES, chart_markdown_table, dispatch, generate_chart,
    is_parallel_safe_tool, parse_tool_arguments, safe_limit, tool_call_name,
};
use deepseek_policy::tool_policy::ToolPolicy;
use serde_json::{Map, Value, json};

fn parse_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("object", json!({"a": 1})),
        ("object-string", json!("{\"a\": 1}")),
        ("nested-string", json!("{\"a\": {\"b\": [1, 2]}}")),
        ("not-json", json!("not json")),
        ("json-array", json!("[1, 2]")),
        ("json-scalar", json!("42")),
        ("blank", json!("   ")),
        ("empty", json!("")),
        ("none", Value::Null),
        ("integer", json!(7)),
    ]
}

fn limit_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("in-range", json!(3)),
        ("zero", json!(0)),
        ("negative", json!(-9)),
        ("above-max", json!(99)),
        ("numeric-string", json!("7")),
        ("float-string", json!("7.9")),
        ("float", json!(3.9)),
        ("unparseable", json!("abc")),
        ("none", Value::Null),
        ("list", json!([1])),
        ("bool-true", json!(true)),
    ]
}

fn name_cases() -> Vec<(&'static str, Value)> {
    vec![
        (
            "function-name",
            json!({"function": {"name": " search_files "}}),
        ),
        (
            "blank-function-top-level",
            json!({"function": {"name": ""}, "name": "x"}),
        ),
        ("top-level-only", json!({"name": "y"})),
        ("missing", json!({})),
        (
            "function-not-a-dict",
            json!({"function": "nope", "name": "z"}),
        ),
    ]
}

fn parallel_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("serial-tool", json!({"function": {"name": "web_search"}})),
        (
            "parallel-tool",
            json!({"function": {"name": "generate_chart"}}),
        ),
        ("nameless", json!({})),
    ]
}

fn chart_cases() -> Vec<(&'static str, Value)> {
    vec![
        (
            "line-with-title",
            json!({"type": "line", "title": "Revenue", "data": [
                {"label": "Q1", "value": 1}, {"label": "Q2", "value": 2.5}]}),
        ),
        ("defaults", json!({"data": [{"label": "a", "value": 1}]})),
        (
            "unknown-type",
            json!({"type": "radar", "data": [{"label": "a", "value": 1}]}),
        ),
        (
            "string-numbers",
            json!({"data": [{"label": "a", "value": "3"}]}),
        ),
        ("drops-and-caps", {
            let mut data = vec![
                json!("nope"),
                json!({"value": 1}),
                json!({"label": "nv"}),
                json!({"label": "n", "value": null}),
                json!({"label": "bad", "value": "abc"}),
                json!({"label": "ok", "value": "3"}),
            ];
            for index in 0..20 {
                data.push(json!({"label": format!("p{index}"), "value": index}));
            }
            json!({"data": data})
        }),
        (
            "pipe-in-label",
            json!({"data": [{"label": "a|b", "value": 1}]}),
        ),
        ("empty-list", json!({"data": []})),
        ("not-a-list", json!({"data": "nope"})),
        ("no-valid-points", json!({"data": [{"value": 1}]})),
        (
            "float-rendering",
            json!({"data": [
            {"label": "x", "value": 100}, {"label": "y", "value": -0.0}]}),
        ),
    ]
}

/// `(label, tool_call, policy)` — `policy` is `None` or a permissive policy.
fn dispatch_cases() -> Vec<(&'static str, Value, bool)> {
    vec![
        (
            "unknown-no-policy",
            json!({"function": {"name": "not_a_tool", "arguments": "{}"}}),
            false,
        ),
        (
            "unknown-with-policy",
            json!({"function": {"name": "not_a_tool", "arguments": "{}"}}),
            true,
        ),
        (
            "chart-no-policy",
            json!({"function": {"name": "generate_chart",
                "arguments": "{\"data\": [{\"label\": \"a\", \"value\": 1}]}"}}),
            false,
        ),
        (
            "chart-with-policy",
            json!({"function": {"name": "generate_chart",
                "arguments": "{\"data\": [{\"label\": \"a\", \"value\": 1}]}"}}),
            true,
        ),
        (
            "chart-empty-args",
            json!({"function": {"name": "generate_chart", "arguments": "{}"}}),
            false,
        ),
        (
            "chart-arguments-as-object",
            json!({"function": {"name": "generate_chart",
                "arguments": {"data": [{"label": "a", "value": 2}]}}}),
            false,
        ),
        (
            "ssrf-denied-with-policy",
            json!({"function": {"name": "fetch_url",
                "arguments": "{\"url\": \"http://169.254.169.254/\"}"}}),
            true,
        ),
        (
            "path-denied-with-policy",
            json!({"function": {"name": "search_files",
                "arguments": "{\"path\": \"../../etc/passwd\"}"}}),
            true,
        ),
    ]
}

fn outcome_label(outcome: &DispatchOutcome) -> &'static str {
    match outcome {
        DispatchOutcome::Executed(_) => "executed",
        DispatchOutcome::Denied(_) => "denied",
        DispatchOutcome::Unsupported(_) => "unsupported",
        DispatchOutcome::Unported { .. } => "unported",
    }
}

fn main() {
    let mut out = Map::new();

    for (label, value) in parse_cases() {
        let parsed = parse_tool_arguments(Some(&value));
        out.insert(format!("parse::{label}"), Value::Object(parsed));
    }

    for (label, value) in limit_cases() {
        out.insert(
            format!("limit::{label}"),
            json!(safe_limit(Some(&value), 5, 10)),
        );
    }

    for (label, call) in name_cases() {
        out.insert(
            format!("name::{label}"),
            Value::String(tool_call_name(&call)),
        );
    }

    for (label, call) in parallel_cases() {
        out.insert(
            format!("parallel::{label}"),
            Value::Bool(is_parallel_safe_tool(&call)),
        );
    }

    for (label, arguments) in chart_cases() {
        // The oracle calls `generate_chart(type, title, data)` with
        // `str(arguments.get(key) or default)`; this port reads the same fields
        // off the argument map, so the defaults are applied inside.
        let result = generate_chart(&parse_tool_arguments(Some(&arguments)));
        out.insert(
            format!("chart::{label}"),
            match result {
                Ok(value) => json!({"ok": true, "result": value}),
                Err(failure) => {
                    json!({"ok": false, "error": failure.error, "code": failure.code})
                }
            },
        );
    }

    out.insert(
        "markdown::table".to_string(),
        Value::String(chart_markdown_table(&[
            json!({"label": "a|b", "value": 1.0}),
            json!({"label": "c", "value": 2.5}),
        ])),
    );

    let mut serial: Vec<Value> = SERIAL_TOOL_NAMES
        .iter()
        .map(|name| Value::String((*name).to_string()))
        .collect();
    serial.sort_by(|left, right| left.as_str().cmp(&right.as_str()));
    out.insert("serial::names".to_string(), Value::Array(serial));

    for (label, call, use_policy) in dispatch_cases() {
        let name = tool_call_name(&call);
        let arguments = call
            .get("function")
            .and_then(|function| function.get("arguments"))
            .cloned()
            .unwrap_or(Value::Null);

        // `no-policy` means literally no gate, as the oracle's `policy=None`
        // does — not a default-configured one. Passing a policy here would make
        // the unknown-tool case deny instead of reaching the `Unsupported tool:`
        // envelope, which is the difference between the two error paths.
        let mut permissive = ToolPolicy::permissive();
        let outcome = if use_policy {
            dispatch(&name, &arguments, Some(&mut permissive), None)
        } else {
            dispatch(&name, &arguments, None, None)
        };

        out.insert(
            format!("dispatch::{label}"),
            json!({
                "outcome": outcome_label(&outcome),
                "output": outcome.to_output().cloned().unwrap_or(Value::Null),
                // Every compared case either denies before routing or runs the
                // one ported branch, so no other branch can be invoked.
                "branches": Vec::<String>::new(),
            }),
        );
    }

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
