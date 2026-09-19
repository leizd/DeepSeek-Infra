//! Native OpenAI-chat composition parity probe, Rust side.
//!
//! Replays `tasks/native-runtime/native_chat_composition_parity_probe.py` through
//! [`deepseek_gateway::native_chat`] and prints canonical JSON.
//!
//! Usage::
//!
//! ```text
//! python tasks/native-runtime/native_chat_composition_parity_probe.py > python.json
//! cd rust && cargo run -p deepseek-gateway --example native_chat_composition_parity_probe > ../rust.json
//! diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)
//! ```
//!
//! The expander, the clock and the downgrade decision are stubbed with the same anchors
//! the Python probe injects (`request_assembly_parity_probe.rs` uses the same three), so
//! the comparison covers the composition rather than the environment. `api_key_fallback`
//! is `probe-key`, which is what the Python probe pins `DEEPSEEK_API_KEY` to.

use std::collections::BTreeMap;

use deepseek_gateway::native_chat::{assemble_openai_chat, prepare_openai_chat};
use deepseek_gateway::request_assembly::AssemblyEnv;
use deepseek_policy::budget_ledger::LedgerDeps;
use deepseek_policy::budget_manager::BudgetSettings;
use deepseek_policy::context_engine::ContextEngineSettings;
use deepseek_policy::context_manager::ContextManagerSettings;
use deepseek_policy::context_taint::ContextTaintSettings;
use deepseek_policy::dynamic_context::{DynamicContextEnv, LocalNow};
use deepseek_policy::model_router::ModelRouterSettings;
use deepseek_policy::python_json::OrderedJson;
use serde_json::{Map, Value, json};

const EPOCH_SECONDS: i64 = 1_789_696_800;
const OFFSET_SECONDS: i32 = 28_800;
const TIMEZONE_NAME: &str = "CST";
const EXPANDED: &str = "[expanded]";
const KEY: &str = "probe-key";
const BASE_URL: &str = "http://127.0.0.1:8000";

fn turns() -> Value {
    json!([{"role": "user", "content": "你好"}])
}

fn cases() -> Vec<(&'static str, Value)> {
    vec![
        (
            "minimal",
            json!({"model": "deepseek-v4-pro", "messages": turns()}),
        ),
        ("alias-model", json!({"model": "fast", "messages": turns()})),
        (
            "temperature",
            json!({"messages": turns(), "temperature": 0.25}),
        ),
        (
            "multi-turn",
            json!({
                "model": "deepseek-v4-pro",
                "messages": [
                    {"role": "system", "content": "你是助手"},
                    {"role": "user", "content": "第一问"},
                    {"role": "assistant", "content": "第一答"},
                    {"role": "user", "content": "第二问"},
                ],
            }),
        ),
        (
            "blank-content-turn",
            json!({
                "messages": [
                    {"role": "user", "content": "   "},
                    {"role": "user", "content": "真正的问题"},
                ]
            }),
        ),
        (
            "tool-turn",
            json!({
                "messages": [
                    {"role": "user", "content": "问题"},
                    {"role": "assistant", "content": "", "tool_calls": [
                        {"id": "call-1", "type": "function",
                         "function": {"name": "generate_chart", "arguments": "{}"}}
                    ]},
                    {"role": "tool", "tool_call_id": "call-1", "content": "结果"},
                ]
            }),
        ),
        (
            "client-tools-dropped",
            json!({
                "model": "deepseek-v4-pro",
                "messages": turns(),
                "tools": [{"type": "function", "function": {"name": "client_tool", "parameters": {}}}],
                "tool_choice": "auto",
            }),
        ),
        (
            "search-fields-dropped",
            json!({
                "messages": turns(),
                "searchEnabled": true,
                "searchMode": "force",
                "allowedTools": ["generate_chart"],
            }),
        ),
        (
            "system-prompt",
            json!({"messages": turns(), "systemPrompt": "系统提示"}),
        ),
        (
            "context-summary",
            json!({"messages": turns(), "contextSummary": "早前摘要"}),
        ),
        (
            "too-many-turns",
            json!({"messages": (0..41).map(|i| json!({"role": "user", "content": format!("t{i}")})).collect::<Vec<_>>()}),
        ),
        ("refused-non-object", json!([])),
        ("refused-no-messages", json!({"model": "deepseek-v4-pro"})),
        ("refused-empty-messages", json!({"messages": []})),
    ]
}

fn main() {
    let router = ModelRouterSettings::default();
    let budget = BudgetSettings::default();
    let taint = ContextTaintSettings::default();
    let context_manager = ContextManagerSettings::default();
    let engine = ContextEngineSettings::default();
    let dynamic = DynamicContextEnv::new(LocalNow {
        epoch_seconds: EPOCH_SECONDS,
        offset_seconds: OFFSET_SECONDS,
        timezone_name: TIMEZONE_NAME.to_string(),
    });
    let expander = |_message: &Value| EXPANDED.to_string();
    let read = |_scope: &str, _day: &str| -> Result<Option<Value>, String> { Ok(None) };
    let write = |_row: &Value| -> Result<(), String> { Ok(()) };
    let ledger = LedgerDeps {
        database_present: false,
        database_path: "probe-budget.db".to_string(),
        day: "2026-09-18".to_string(),
        now_iso: "2026-09-18T02:00:00Z".to_string(),
        read_spend_row: &read,
        write_spend_row: &write,
    };
    let env = AssemblyEnv {
        api_key_fallback: KEY,
        expander: &expander,
        router: &router,
        budget: &budget,
        ledger: &ledger,
        taint: &taint,
        context_manager: &context_manager,
        engine: &engine,
        dynamic: &dynamic,
    };

    let memory_state = json!({"enabled": false});
    let mut out: BTreeMap<String, Value> = BTreeMap::new();
    out.insert("probe::base-url".to_string(), json!(BASE_URL));

    for (label, body) in cases() {
        let view = match prepare_openai_chat(&body, BASE_URL, env.router, env.api_key_fallback) {
            Ok(prepared) => match assemble_openai_chat(prepared, false, &memory_state, &env) {
                Ok(assembled) => {
                    let tool_names: Vec<String> = assembled
                        .body
                        .get("tools")
                        .and_then(Value::as_array)
                        .map(|tools| {
                            tools
                                .iter()
                                .filter_map(|tool| {
                                    tool.get("function")?
                                        .get("name")?
                                        .as_str()
                                        .map(str::to_string)
                                })
                                .collect()
                        })
                        .unwrap_or_default();
                    let mut sorted = tool_names;
                    sorted.sort();
                    let mut view = Map::new();
                    view.insert("api_key".to_string(), json!(assembled.api_key));
                    view.insert("body".to_string(), json!(assembled.render_body()));
                    view.insert(
                        "diagnostics".to_string(),
                        json!(assembled.render_diagnostics()),
                    );
                    view.insert("tool_names".to_string(), json!(sorted));
                    Value::Object(view)
                }
                Err(error) => error_view(&error),
            },
            Err(error) => error_view(&error),
        };
        out.insert(format!("case::{label}"), view);
    }

    let value = Value::Object(out.into_iter().collect::<Map<String, Value>>());
    let rendered = OrderedJson::from_value_with_order(&value, &[]).render_indent_2();
    println!("{rendered}");
}

fn error_view(error: &deepseek_policy::app_error::AppError) -> Value {
    let mut view = Map::new();
    view.insert("error".to_string(), json!(error.message));
    view.insert("code".to_string(), json!(error.code));
    view.insert("status".to_string(), json!(error.status));
    Value::Object(view)
}
