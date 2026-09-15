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

use deepseek_policy::tool_batch::execute_tool_calls;
use deepseek_policy::tool_dispatch::{
    DispatchOutcome, SERIAL_TOOL_NAMES, chart_markdown_table, dispatch, generate_chart,
    is_parallel_safe_tool, parse_tool_arguments, safe_limit, tool_call_name,
};
use deepseek_policy::tool_policy::ToolPolicy;
use deepseek_policy::tool_transform::data_transform;
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
            "transform-number-summary",
            json!({"function": {"name": "data_transform",
                "arguments": "{\"operation\": \"number_summary\", \"input\": \"1 2 3\"}"}}),
            false,
        ),
        (
            "transform-unknown-operation",
            json!({"function": {"name": "data_transform",
                "arguments": "{\"operation\": \"nope\", \"input\": \"x\"}"}}),
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

/// Mask the engine-specific suffix of a parse-error message, mirroring the
/// Python probe. The prefix is the oracle's own message and stays compared.
fn mask_engine_error(error: &str) -> String {
    for prefix in ["Invalid JSON", "Invalid regex"] {
        if error.starts_with(prefix) {
            return format!("{prefix}: <engine>");
        }
    }
    error.to_string()
}

/// `(label, operation, input, pattern, path, delimiter)`
fn transform_cases() -> Vec<(
    &'static str,
    &'static str,
    String,
    &'static str,
    &'static str,
    &'static str,
)> {
    let oversized = format!("{{\"a\": \"{}\"}}", "x".repeat(5000));
    vec![
        (
            "extract-simple",
            "extract_regex",
            "a1 b2 c3".to_string(),
            r"([a-z])(\d)",
            "",
            ",",
        ),
        (
            "extract-no-groups",
            "extract_regex",
            "xx yy".to_string(),
            "x+",
            "",
            ",",
        ),
        (
            "extract-optional-group",
            "extract_regex",
            "ab a".to_string(),
            r"a(b)?",
            "",
            ",",
        ),
        (
            "extract-missing-pattern",
            "extract_regex",
            "x".to_string(),
            "",
            "",
            ",",
        ),
        (
            "extract-unicode",
            "extract_regex",
            "中文 abc".to_string(),
            "[a-z]+",
            "",
            ",",
        ),
        (
            "json-whole",
            "json_path",
            "{\"a\": {\"b\": [10, 20]}}".to_string(),
            "",
            "$",
            ",",
        ),
        (
            "json-nested",
            "json_path",
            "{\"a\": {\"b\": [10, 20]}}".to_string(),
            "",
            "$.a.b[1]",
            ",",
        ),
        (
            "json-no-prefix",
            "json_path",
            "{\"a\": {\"b\": [10, 20]}}".to_string(),
            "",
            "a.b[0]",
            ",",
        ),
        (
            "json-missing",
            "json_path",
            "{\"a\": 1}".to_string(),
            "",
            "$.nope",
            ",",
        ),
        (
            "json-unsupported",
            "json_path",
            "{\"a\": 1}".to_string(),
            "",
            "$.a[*]",
            ",",
        ),
        (
            "json-out-of-range",
            "json_path",
            "{\"a\": [1]}".to_string(),
            "",
            "$.a[5]",
            ",",
        ),
        (
            "json-invalid",
            "json_path",
            "not json".to_string(),
            "",
            "$",
            ",",
        ),
        ("json-oversized", "json_path", oversized, "", "$.a", ","),
        (
            "csv-basic",
            "csv_summary",
            "name,value\nx,1\ny,2.5\n".to_string(),
            "",
            "",
            ",",
        ),
        (
            "csv-quoted-delimiter",
            "csv_summary",
            "a,b\n\"x,y\",2\n".to_string(),
            "",
            "",
            ",",
        ),
        (
            "csv-doubled-quotes",
            "csv_summary",
            "a\n\"say \"\"hi\"\"\"\n".to_string(),
            "",
            "",
            ",",
        ),
        (
            "csv-multiline-field",
            "csv_summary",
            "a\n\"one\ntwo\"\n".to_string(),
            "",
            "",
            ",",
        ),
        (
            "csv-thousands",
            "csv_summary",
            ",v\n,\"1,000\"\n".to_string(),
            "",
            "",
            ",",
        ),
        ("csv-empty", "csv_summary", String::new(), "", "", ","),
        (
            "csv-header-only",
            "csv_summary",
            "a,b\n".to_string(),
            "",
            "",
            ",",
        ),
        (
            "csv-semicolon",
            "csv_summary",
            "a;b\n1;2\n".to_string(),
            "",
            "",
            ";",
        ),
        (
            "numbers-simple",
            "number_summary",
            "1 2 3 4".to_string(),
            "",
            "",
            ",",
        ),
        (
            "numbers-signed-decimal",
            "number_summary",
            "-1.5 and +2 and .5".to_string(),
            "",
            "",
            ",",
        ),
        (
            "numbers-none",
            "number_summary",
            "no digits here".to_string(),
            "",
            "",
            ",",
        ),
        ("unknown-operation", "nope", "x".to_string(), "", "", ","),
    ]
}

/// `(label, tool_calls, cancelled)`
fn batch_cases() -> Vec<(&'static str, Vec<Value>, bool)> {
    let chart = |label: &str, id: &str| {
        json!({"id": id, "function": {"name": "generate_chart",
            "arguments": format!("{{\"data\": [{{\"label\": \"{label}\", \"value\": 1}}]}}")}})
    };
    vec![
        (
            "two-parallel",
            vec![
                chart("a", "a"),
                json!({"id": "b", "function": {"name": "data_transform",
                    "arguments": "{\"operation\": \"number_summary\", \"input\": \"1 2\"}"}}),
            ],
            false,
        ),
        (
            "mixed-with-unknown",
            vec![
                chart("a", "a"),
                json!({"id": "b", "function": {"name": "not_a_tool", "arguments": "{}"}}),
            ],
            false,
        ),
        (
            "cancelled-from-start",
            vec![chart("a", "a"), chart("b", "b")],
            true,
        ),
        (
            "capped-at-six",
            (0..9)
                .map(|index| chart("a", &format!("id{index}")))
                .collect(),
            false,
        ),
    ]
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

    // --- data_transform -------------------------------------------------------

    for (label, operation, input, pattern, path, delimiter) in transform_cases() {
        let result = data_transform(operation, &input, pattern, path, delimiter);
        out.insert(
            format!("transform::{label}"),
            match result {
                Ok(value) => json!({"ok": true, "result": value}),
                Err(failure) => json!({
                    "ok": false,
                    "error": mask_engine_error(&failure.error),
                    "code": failure.code,
                }),
            },
        );
    }

    // --- execute_tool_calls ---------------------------------------------------

    for (label, calls, cancelled) in batch_cases() {
        let calls: Vec<Value> = calls;
        let should_cancel = move || cancelled;
        let messages = execute_tool_calls(&calls, &should_cancel, &|call| {
            let name = tool_call_name(call);
            let arguments = call
                .get("function")
                .and_then(|function| function.get("arguments"))
                .cloned()
                .unwrap_or(Value::Null);
            dispatch(&name, &arguments, None, None)
        });
        out.insert(
            format!("batch::{label}"),
            json!({
                "messages": messages,
                // Every compared case uses ported branches or an unknown name, so
                // no recorder-visible branch is invoked — matching Python, where
                // those two branches are the real extracted functions.
                "branches": Vec::<String>::new(),
            }),
        );
    }

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
