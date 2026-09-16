//! Tool-catalog parity probe, Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/tool_catalog_parity_probe.py`
//! through `deepseek_policy::tool_catalog`.
//!
//! Usage::
//!
//!     python tasks/native-runtime/tool_catalog_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example tool_catalog_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use deepseek_policy::tool_catalog::{
    agent_tool_definitions, available_tool_definitions, catalog_json, schema_for_tool,
    tool_parameter_schemas,
};
use serde_json::{Map, Value, json};

fn schema_cases() -> Vec<&'static str> {
    vec![
        "web_search",
        "create_reminder",
        "generate_chart",
        "read_file_chunk",
        "browser_open_url",
        "  web_search  ",
        "no_such_tool",
        "",
    ]
}

fn index_sample() -> [&'static str; 4] {
    [
        "web_search",
        "create_reminder",
        "generate_chart",
        "read_file_chunk",
    ]
}

fn main() {
    let mut out = Map::new();

    // The asset's exact bytes, as the oracle renders them. Byte-identity here is what
    // proves the committed JSON is still `json.dumps(..., ensure_ascii=False, indent=2)`.
    out.insert("asset::json".to_string(), json!(catalog_json()));

    let names: Vec<Value> = available_tool_definitions()
        .iter()
        .map(|tool| tool["function"]["name"].clone())
        .collect();
    out.insert("definitions::names".to_string(), json!(names));
    out.insert(
        "definitions::count".to_string(),
        json!(available_tool_definitions().len()),
    );

    let index = tool_parameter_schemas();
    out.insert("schemas::count".to_string(), json!(index.len()));
    out.insert(
        "schemas::names".to_string(),
        json!(index.keys().cloned().collect::<Vec<String>>()),
    );
    for name in index_sample() {
        out.insert(
            format!("schema::{name}"),
            index.get(name).cloned().unwrap_or(Value::Null),
        );
    }

    for case in schema_cases() {
        // The key mirrors Python's `json.dumps(case, ensure_ascii=False)`.
        out.insert(
            format!("schema-for::{}", json!(case)),
            schema_for_tool(case, None).unwrap_or(Value::Null),
        );
    }

    // No external MCP profiles: the local catalog unchanged, which is the arm the
    // oracle takes when the bridge contributes nothing.
    let agent: Vec<Value> = agent_tool_definitions(None)
        .iter()
        .map(|tool| tool["function"]["name"].clone())
        .collect();
    out.insert("agent::names".to_string(), json!(agent));

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
