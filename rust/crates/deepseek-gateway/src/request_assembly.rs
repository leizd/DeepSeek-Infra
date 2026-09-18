//! The request assembly — `build_deepseek_request`, the last pure piece before the gateway
//! hands a body to the upstream client.
//!
//! Everything it calls is already ported; what this module owns is the **order** of the two
//! documents it produces, and the injection points for the three things that are not pure:
//! the content expander (the attachment path), the ledger read, and the clock (through
//! [`DynamicContextEnv`]).
//!
//! # Why the key order is spelled out
//!
//! Neither document is sorted. The body is what `requests` sends as `json.dumps(body)` —
//! Python's **default separators** and **insertion order** — and the diagnostics go out
//! through the SSE writer as `json.dumps(obj, ensure_ascii=False, separators=(",", ":"))`,
//! which is compact but still insertion-ordered. A `serde_json::Map` is a `BTreeMap`, so both
//! orders have to be carried explicitly: [`BODY_KEYS`] and [`DIAGNOSTIC_KEYS`] for the two
//! envelopes, [`NESTED_ORDERS`] for the blocks that arrive as a `Value` from the module that
//! built them.
//!
//! The orders are **measured**, not inferred: the Python probe publishes the key order of
//! every envelope and every nested block it sees, and these tables are what it reported.
//!
//! # The remaining gap, as the probe measures it
//!
//! With the tables below, the probe reports **95 of 292 rows differing**, and every one of
//! them is inside a **tool schema**: `parameters.properties` names its properties in
//! declaration order (not sorted) and each property object orders `type` before
//! `description`. Both belong to the tool catalog — [`crate::request_preparation`] and the
//! catalog slice own that shape — so extending these tables from here would put one slice's
//! contract in another slice's file. The tool entries are therefore the known gap, named
//! rather than papered over; everything else the envelope carries matches byte for byte.

use serde_json::{Map, Value, json};

use deepseek_policy::app_error::AppError;
use deepseek_policy::budget_ledger::{
    LedgerDeps, daily_spend, over_daily_budget, should_downgrade,
};
use deepseek_policy::budget_manager::{BudgetSettings, budget_policy_from_payload, budget_scope};
use deepseek_policy::context_engine::ContextEngineSettings;
use deepseek_policy::context_manager::{
    ContextManagerSettings, manage_request_body, merge_context_manager_diagnostics,
};
use deepseek_policy::context_taint::{ContextTaintSettings, build_taint_report};
use deepseek_policy::core_utils::{python_int_opt, python_truthy, text_or_empty};
use deepseek_policy::dynamic_context::{
    DynamicContextEnv, append_context_to_latest_user, build_dynamic_turn_context,
};
use deepseek_policy::memory::empty_memory_state;
use deepseek_policy::model_router::{ModelRouterSettings, is_auto_request, route_request};
use deepseek_policy::python_json::OrderedJson;
use deepseek_policy::request_messages::{
    ValidatedPayload, normalize_chat_messages, validate_deepseek_payload, validate_request_messages,
};
use deepseek_policy::request_shaping::{
    TOOL_PARALLEL_SYSTEM_HINT, count_payload_attachments, forced_artifact_tool_name,
    has_image_content, normalize_reasoning_effort, tools_for_payload,
};

/// The upstream request body's key order: the three required keys, then each conditional
/// block in the order the oracle assigns them.
pub const BODY_KEYS: [&str; 9] = [
    "model",
    "messages",
    "stream",
    "tools",
    "tool_choice",
    "temperature",
    "top_p",
    "reasoning_effort",
    "thinking",
];

/// The diagnostics envelope's key order, as measured from the oracle.
pub const DIAGNOSTIC_KEYS: [&str; 19] = [
    "requestMessageCount",
    "contextSummaryChars",
    "dynamicContextChars",
    "contextSummaryGeneration",
    "contextSummaryMessageCount",
    "contextCompressionDeltaCount",
    "memoryEnabled",
    "memoryHitCount",
    "attachmentCount",
    "searchRoundCount",
    "searchResultCount",
    "toolCallCount",
    "toolNames",
    "modelRouter",
    "budgetPolicy",
    "budgetDowngraded",
    "contextManager",
    "contextEngine",
    "contextTaint",
];

/// Key orders for the blocks that arrive as a `Value`, matched by block name.
pub const NESTED_ORDERS: [(&str, &[&str]); 12] = [
    // Array elements are ordered by the array's name, so messages and tools differ.
    ("messages", &["role", "content"]),
    ("tools", &["type", "function"]),
    ("function", &["name", "strict", "description", "parameters"]),
    (
        "parameters",
        &["type", "properties", "required", "additionalProperties"],
    ),
    (
        "tokenBudget",
        &[
            "model",
            "contextWindow",
            "reservedOutputTokens",
            "availableInputTokens",
            "estimatedPromptTokens",
            "breakdown",
            "headroomTokens",
            "utilizationPct",
            "withinBudget",
            "recommendation",
        ],
    ),
    ("breakdown", &["system", "tools", "history", "dynamic"]),
    ("contextDiff", &["baseContextId", "delta"]),
    (
        "modelRouter",
        &[
            "model",
            "tier",
            "auto",
            "capability",
            "fallbackModel",
            "estimatedPromptTokens",
            "reasons",
        ],
    ),
    (
        "budgetPolicy",
        &[
            "maxTotalTokens",
            "maxAgentTokens",
            "maxSearchCalls",
            "maxToolCalls",
            "maxEstimatedCostUsd",
            "policy",
        ],
    ),
    (
        "contextManager",
        &[
            "enabled",
            "stableJson",
            "stableSystemPosition",
            "dynamicContextPosition",
            "toolOrderStable",
            "slidingWindowApplied",
            "slidingWindowAllowed",
            "droppedMessages",
            "toolOrder",
            "toolCount",
            "messageCount",
            "requestMessageCount",
            "hasFrontSystemPrompt",
            "hasTrailingDynamicContext",
        ],
    ),
    (
        "contextEngine",
        &["enabled", "model", "tokenBudget", "contextDiff"],
    ),
    (
        "contextTaint",
        &[
            "enabled",
            "tainted",
            "riskLevel",
            "untrustedChars",
            "untrustedSegments",
            "injectionHits",
            "exfiltrationHits",
            "toolDirectiveHits",
            "escalatedTools",
            "recommendedAction",
            "sources",
            "segments",
        ],
    ),
];

/// The reads and settings the assembly needs, injected.
pub struct AssemblyEnv<'a> {
    /// `validate_deepseek_payload`'s fallback key.
    pub api_key_fallback: &'a str,
    /// `expanded_message_content` — the attachment path, already ported.
    pub expander: &'a dyn Fn(&Value) -> String,
    pub router: &'a ModelRouterSettings,
    pub budget: &'a BudgetSettings,
    pub ledger: &'a LedgerDeps<'a>,
    pub taint: &'a ContextTaintSettings,
    pub context_manager: &'a ContextManagerSettings,
    pub engine: &'a ContextEngineSettings,
    /// The clock, anchored by the caller for the same reason the other clock readers are.
    pub dynamic: &'a DynamicContextEnv,
}

/// Mirrors `PreparedDeepSeekRequest`.
#[derive(Debug, Clone, PartialEq)]
pub struct PreparedDeepSeekRequest {
    pub api_key: String,
    pub body: Value,
    pub diagnostics: Value,
}

impl PreparedDeepSeekRequest {
    /// `json.dumps(body)` — default separators, insertion order: what `requests` sends.
    pub fn render_body(&self) -> String {
        OrderedJson::from_value_with_orders(&self.body, &BODY_KEYS, &NESTED_ORDERS)
            .render_default_separators()
    }

    /// `json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":"))`: what the SSE
    /// writer emits.
    pub fn render_diagnostics(&self) -> String {
        OrderedJson::from_value_with_orders(&self.diagnostics, &DIAGNOSTIC_KEYS, &NESTED_ORDERS)
            .render_compact()
    }

    /// The nested blocks' key order, as this side sees it — the probe compares it with the
    /// oracle's, which is how the tables above were built and are kept honest.
    pub fn nested_key_orders(&self) -> Value {
        let Some(fields) = self.diagnostics.as_object() else {
            return json!({});
        };
        let mut blocks = Map::new();
        let ordered = OrderedJson::from_value_with_orders(
            &self.diagnostics,
            &DIAGNOSTIC_KEYS,
            &NESTED_ORDERS,
        );
        if let OrderedJson::Object(pairs) = &ordered {
            for (name, node) in pairs {
                if !fields.get(name).is_some_and(Value::is_object) {
                    continue;
                }
                if let OrderedJson::Object(inner) = node {
                    blocks.insert(
                        name.clone(),
                        Value::Array(
                            inner
                                .iter()
                                .map(|(key, _)| Value::String(key.clone()))
                                .collect(),
                        ),
                    );
                }
            }
        }
        Value::Object(blocks)
    }
}

/// Mirrors `build_deepseek_request`.
///
/// The order the oracle applies, and which this keeps:
/// validate → route → budget downgrade → system parts → normalize → validate messages →
/// memory state → dynamic context → image override → body blocks → diagnostics blocks →
/// context manager → taint report.
pub fn build_deepseek_request(
    payload: &Value,
    stream: bool,
    memory_state: Option<&Value>,
    validated: Option<&ValidatedPayload>,
    env: &AssemblyEnv<'_>,
) -> Result<PreparedDeepSeekRequest, AppError> {
    let owned;
    let (api_key, model, messages) = match validated {
        Some(found) => (
            found.api_key.clone(),
            found.model.clone(),
            found.messages.clone(),
        ),
        None => {
            owned = validate_deepseek_payload(payload, env.api_key_fallback, env.router)?;
            (
                owned.api_key.clone(),
                owned.model.clone(),
                owned.messages.clone(),
            )
        }
    };
    let mut model = model;
    let messages_value = Value::Array(messages.clone());

    // Model Router: auto routing picks the tier; an explicit pick is left alone.
    let route_decision = if is_auto_request(payload, env.router) {
        Some(route_request(payload, 0, env.router))
    } else {
        None
    };
    if let Some(decision) = &route_decision {
        model = decision.model.clone();
    }

    // Budget governance: the ledger is read only when a downgrade policy is active **and**
    // the request is on the pro tier, which is the oracle's short-circuit order.
    let budget_policy = budget_policy_from_payload(payload, env.budget);
    let mut budget_downgraded = false;
    if budget_policy.downgrade() && model == "deepseek-v4-pro" {
        let scope = budget_scope(payload);
        let (spend, _ledger_error) = daily_spend(&scope, None, env.ledger);
        let over = over_daily_budget(&spend, &budget_policy);
        if should_downgrade(&budget_policy, &over) {
            model = "deepseek-v4-flash".to_string();
            budget_downgraded = true;
        }
    }

    // `toolsEnabled is not False`: only the literal `false` disables tools.
    let tools_enabled = !matches!(payload.get("toolsEnabled"), Some(Value::Bool(false)));

    let mut stable_system_parts: Vec<String> = Vec::new();
    let system_prompt = text_or_empty(payload.get("systemPrompt"))
        .trim()
        .to_string();
    if !system_prompt.is_empty() {
        stable_system_parts.push(system_prompt);
    }
    if tools_enabled {
        stable_system_parts.push(TOOL_PARALLEL_SYSTEM_HINT.to_string());
    }
    let context_summary = text_or_empty(payload.get("contextSummary"))
        .trim()
        .to_string();

    let mut api_messages: Vec<Value> = Vec::new();
    if !stable_system_parts.is_empty() {
        api_messages.push(json!({"role": "system", "content": stable_system_parts.join("\n\n")}));
    }

    let normalized_messages = normalize_chat_messages(&messages_value, env.expander)?;
    validate_request_messages(payload, &messages, env.expander)?;

    let memory_state = memory_state
        .cloned()
        .unwrap_or_else(|| empty_memory_state(payload));
    let memory_enabled = python_truthy(memory_state.get("enabled").unwrap_or(&Value::Null));
    let memory_hit_count = python_int_opt(memory_state.get("hitCount")).unwrap_or(0);
    let dynamic_context =
        build_dynamic_turn_context(payload, &memory_state, tools_enabled, env.dynamic);
    let normalized_messages = if dynamic_context.is_empty() {
        normalized_messages
    } else {
        append_context_to_latest_user(&normalized_messages, &dynamic_context)
    };

    api_messages.extend(normalized_messages);

    // A multimodal message forces the vision tier.
    if has_image_content(&api_messages) {
        model = "deepseek-v4-pro".to_string();
    }

    let mut request_body: Map<String, Value> = Map::new();
    request_body.insert("model".to_string(), Value::String(model.clone()));
    request_body.insert("messages".to_string(), Value::Array(api_messages.clone()));
    request_body.insert("stream".to_string(), Value::Bool(stream));

    if tools_enabled {
        let request_tools = tools_for_payload(payload);
        let forced_tool = forced_artifact_tool_name(payload, &request_tools);
        request_body.insert("tools".to_string(), Value::Array(request_tools));
        if forced_tool.is_empty() {
            request_body.insert("tool_choice".to_string(), Value::String("auto".to_string()));
        } else {
            request_body.insert(
                "tool_choice".to_string(),
                json!({"type": "function", "function": {"name": forced_tool}}),
            );
        }
    }

    if model == "deepseek-v4-flash" {
        // `isinstance(temperature, (int, float))` — a bool counts as an int here.
        let temperature = match payload.get("temperature") {
            Some(Value::Number(number)) => number.as_f64().unwrap_or(1.0),
            Some(Value::Bool(flag)) => {
                if *flag {
                    1.0
                } else {
                    0.0
                }
            }
            _ => 1.0,
        };
        // Python's `max(0, min(t, 2))`, written as comparisons so a NaN would keep the bound
        // the way Python's min/max do.
        let capped = if temperature < 2.0 { temperature } else { 2.0 };
        let bounded = if capped > 0.0 { capped } else { 0.0 };
        request_body.insert("temperature".to_string(), json!(bounded));
        request_body.insert("top_p".to_string(), json!(1.0));
    }

    // `is None` picks the default; `is True` is the only way to enable.
    let thinking_enabled = match payload.get("thinkingEnabled") {
        None | Some(Value::Null) => model == "deepseek-v4-pro",
        Some(Value::Bool(flag)) => *flag,
        Some(_) => false,
    };
    if thinking_enabled {
        request_body.insert(
            "reasoning_effort".to_string(),
            Value::String(normalize_reasoning_effort(payload.get("reasoningEffort")).to_string()),
        );
        request_body.insert("thinking".to_string(), json!({"type": "enabled"}));
    }

    let diagnostic_int = |name: &str| -> i64 {
        payload
            .get(name)
            .and_then(|value| python_int_opt(Some(value)))
            .map(|value| value.max(0))
            .unwrap_or(0)
    };
    let request_message_count = api_messages
        .iter()
        .filter(|item| {
            matches!(
                item.get("role").and_then(Value::as_str),
                Some("user" | "assistant")
            )
        })
        .count();

    let mut diagnostics: Map<String, Value> = Map::new();
    diagnostics.insert(
        "requestMessageCount".to_string(),
        json!(request_message_count),
    );
    diagnostics.insert(
        "contextSummaryChars".to_string(),
        json!(context_summary.chars().count()),
    );
    diagnostics.insert(
        "dynamicContextChars".to_string(),
        json!(dynamic_context.chars().count()),
    );
    diagnostics.insert(
        "contextSummaryGeneration".to_string(),
        json!(diagnostic_int("contextSummaryGeneration")),
    );
    diagnostics.insert(
        "contextSummaryMessageCount".to_string(),
        json!(diagnostic_int("contextSummaryMessageCount")),
    );
    diagnostics.insert(
        "contextCompressionDeltaCount".to_string(),
        json!(diagnostic_int("contextCompressionDeltaCount")),
    );
    diagnostics.insert("memoryEnabled".to_string(), Value::Bool(memory_enabled));
    diagnostics.insert("memoryHitCount".to_string(), json!(memory_hit_count));
    diagnostics.insert(
        "attachmentCount".to_string(),
        json!(count_payload_attachments(Some(&messages_value))),
    );
    diagnostics.insert("searchRoundCount".to_string(), json!(0));
    diagnostics.insert("searchResultCount".to_string(), json!(0));
    diagnostics.insert("toolCallCount".to_string(), json!(0));
    diagnostics.insert("toolNames".to_string(), json!([]));
    if let Some(decision) = &route_decision {
        diagnostics.insert("modelRouter".to_string(), decision.to_value());
    }
    if budget_policy.policy != "none" {
        diagnostics.insert("budgetPolicy".to_string(), budget_policy.to_value());
        diagnostics.insert(
            "budgetDowngraded".to_string(),
            Value::Bool(budget_downgraded),
        );
    }

    let (body, context_manager_diag) = manage_request_body(
        &Value::Object(request_body),
        !context_summary.is_empty(),
        env.context_manager,
        env.engine,
    );
    let mut diagnostics =
        merge_context_manager_diagnostics(Value::Object(diagnostics), context_manager_diag);
    let taint_report = build_taint_report(&body, env.taint);
    if let Some(report) = taint_report {
        if let Some(fields) = diagnostics.as_object_mut() {
            fields.insert("contextTaint".to_string(), report);
        }
    }

    Ok(PreparedDeepSeekRequest {
        api_key,
        body,
        diagnostics,
    })
}
