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
//! # Where the tool and taint orders come from
//!
//! Two parts of the documents are not fixed shapes — the order lives in the data — and
//! the by-name tables cannot carry them:
//!
//! - **Tool schemas.** `parameters.properties` names each tool's properties in its own
//!   declaration order (25 distinct property orders across the catalog, reusing the same
//!   property names) and every property object orders `type` before `description`. The
//!   catalog owns those trees ([`tool_catalog::ordered_tool_definition`]), and
//!   [`PreparedDeepSeekRequest::render_body`] substitutes them for the definitions the
//!   catalog owns byte for byte; a definition it does not own keeps the generic rendering,
//!   where drift stays visible.
//! - **The taint block.** `sources` accumulates in first-appearance order over the
//!   **untruncated** segment scan, which the capped `segments` list cannot re-derive, so
//!   the builder hands the renderer its tree
//!   ([`deepseek_policy::context_taint::build_taint_report_ordered`], carried on
//!   [`PreparedDeepSeekRequest::taint_ordered`]) and
//!   [`PreparedDeepSeekRequest::render_diagnostics`] substitutes it while the two views
//!   still agree.
//!
//! The cross-language check is the probe pair
//! (`tasks/native-runtime/request_assembly_parity_probe.py` +
//! `examples/request_assembly_parity_probe.rs`): 406 rows over every branch the corpus can
//! reach — the forced `tool_choice` object, the memory-state axis, the ledger
//! short-circuit, both temperature clamps, the search-on and narrowed tool lists — rendered
//! the way the wire renders them, and matching byte for byte.

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
use deepseek_policy::context_taint::{ContextTaintSettings, build_taint_report_ordered};
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
use deepseek_policy::tool_catalog;

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
pub const NESTED_ORDERS: [(&str, &[&str]); 15] = [
    // Array elements are ordered by the array's name, so messages and tools differ.
    ("messages", &["role", "content"]),
    ("tools", &["type", "function"]),
    // A forced artifact tool replaces the `"auto"` string with this object.
    ("tool_choice", &["type", "function"]),
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
    // `delta` entries are built as `{"type": ..., <one payload key>}` — the type tag
    // leads and the single remaining key sorts behind it.
    ("delta", &["type"]),
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
    // Every router reason is built as `{"router": …, "decision": …}`.
    ("reasons", &["router", "decision"]),
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
    /// The taint block's ordered tree, built once beside the `Value` the diagnostics hold.
    ///
    /// It exists because `sources` accumulates in first-appearance order over the
    /// **untruncated** segment scan: once the visible `segments` are capped, that order is
    /// not re-derivable from anything the `Value` keeps, so whoever renders the block must
    /// be handed the tree ([`deepseek_policy::context_taint::build_taint_report_ordered`]).
    pub taint_ordered: Option<OrderedJson>,
}

impl PreparedDeepSeekRequest {
    /// `json.dumps(body)` — default separators, insertion order: what `requests` sends.
    pub fn render_body(&self) -> String {
        let mut ordered =
            OrderedJson::from_value_with_orders(&self.body, &BODY_KEYS, &NESTED_ORDERS);
        substitute_catalog_tool_orders(&mut ordered, &self.body);
        ordered.render_default_separators()
    }

    /// `json.dumps(diagnostics, ensure_ascii=False, separators=(",", ":"))`: what the SSE
    /// writer emits.
    pub fn render_diagnostics(&self) -> String {
        let mut ordered = OrderedJson::from_value_with_orders(
            &self.diagnostics,
            &DIAGNOSTIC_KEYS,
            &NESTED_ORDERS,
        );
        substitute_taint_order(&mut ordered, &self.diagnostics, self.taint_ordered.as_ref());
        ordered.render_compact()
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

/// Tool definitions carry the catalog's own key order, which the by-name tables cannot
/// express: `parameters.properties` names each tool's properties in its own declaration
/// order (25 distinct orders across the catalog) and the property objects order `type`
/// before `description`. The catalog owns those trees
/// ([`tool_catalog::ordered_tool_definition`]); a definition it does not own — drifted or
/// unknown — keeps the generic rendering, so the difference stays visible instead of
/// being silently served the catalog's bytes.
fn substitute_catalog_tool_orders(ordered: &mut OrderedJson, body: &Value) {
    let Some(Value::Array(tools)) = body.get("tools") else {
        return;
    };
    let OrderedJson::Object(pairs) = ordered else {
        return;
    };
    let Some((_, OrderedJson::List(elements))) =
        pairs.iter_mut().find(|(key, _)| key.as_str() == "tools")
    else {
        return;
    };
    for (element, tool) in elements.iter_mut().zip(tools.iter()) {
        if let Some(tree) = tool_catalog::ordered_tool_definition(tool) {
            *element = tree;
        }
    }
}

/// The taint block's builder-owned tree replaces the generic rendering — while the two
/// views still agree, the same guard the tool substitution uses. A caller that edits
/// `diagnostics.contextTaint` without clearing [`PreparedDeepSeekRequest::taint_ordered`]
/// keeps the edited bytes visible rather than being served the builder's tree.
fn substitute_taint_order(
    ordered: &mut OrderedJson,
    diagnostics: &Value,
    report: Option<&OrderedJson>,
) {
    let Some(report) = report else {
        return;
    };
    if diagnostics.get("contextTaint") != Some(&report.to_value()) {
        return;
    }
    let OrderedJson::Object(pairs) = ordered else {
        return;
    };
    for (key, node) in pairs.iter_mut() {
        if key == "contextTaint" {
            *node = report.clone();
        }
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

    // `memory_state or empty_memory_state(payload)`: Python's `or` treats a falsy state
    // — `{}` included — as absent, so the payload-derived default applies.
    let memory_state = match memory_state {
        Some(state) if python_truthy(state) => state.clone(),
        _ => empty_memory_state(payload),
    };
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
        request_body.insert("temperature".to_string(), clamped_temperature(payload));
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
    // The taint block is built in tree form: two of its orders (`sources`
    // first-appearance over the untruncated scan, segment key order) are facts of
    // construction and cannot be re-derived from the `Value` the diagnostics carry.
    let taint_ordered = build_taint_report_ordered(&body, env.taint);
    if let Some(report) = &taint_ordered {
        if let Some(fields) = diagnostics.as_object_mut() {
            fields.insert("contextTaint".to_string(), report.to_value());
        }
    }

    Ok(PreparedDeepSeekRequest {
        api_key,
        body,
        diagnostics,
        taint_ordered,
    })
}

/// `max(0, min(float(temperature), 2))` with Python's type behaviour, for the flash tier.
///
/// `isinstance(temperature, (int, float))` lets a bool through (`float(True)` is `1.0`),
/// anything else is the `1.0` default. The clamp is the interesting part: `min(t, 2)`
/// keeps **the integer 2** when `t > 2` and `max(0, …)` keeps **the integer 0** when the
/// inner value is not above zero, so `json.dumps` writes `2` / `0` — not `2.0` / `0.0` —
/// at the bounds. Measured on the oracle: 3.5→`2`, 2.0→`2.0`, 0.5→`0.5`, 0→`0`,
/// -1→`0`, True→`1.0`, False→`0`; NaN falls through both comparisons to `0`, as there.
fn clamped_temperature(payload: &Value) -> Value {
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
    if temperature > 2.0 {
        json!(2)
    } else if temperature > 0.0 {
        json!(temperature)
    } else {
        json!(0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use deepseek_policy::tool_catalog::available_tool_definitions;

    fn catalog_tool(name: &str) -> Value {
        available_tool_definitions()
            .iter()
            .find(|tool| tool["function"]["name"] == name)
            .unwrap_or_else(|| panic!("{name} is in the catalog"))
            .clone()
    }

    fn prepared(body: Value, diagnostics: Value) -> PreparedDeepSeekRequest {
        PreparedDeepSeekRequest {
            api_key: "k".to_string(),
            body,
            diagnostics,
            taint_ordered: None,
        }
    }

    /// The clamp's type is part of the bytes: a clamped value is the integer the oracle's
    /// `min`/`max` kept, a float boundary keeps its `.0`, and a bool is an `int`.
    #[test]
    fn a_clamped_temperature_keeps_pythons_type_at_the_bounds() {
        for (temperature, expected) in [
            (json!(3.5), "2"),
            (json!(2.0), "2.0"),
            (json!(2), "2.0"),
            (json!(0.5), "0.5"),
            (json!(0), "0"),
            (json!(0.0), "0"),
            (json!(-1), "0"),
            (json!(true), "1.0"),
            (json!(false), "0"),
            (json!("x"), "1.0"),
        ] {
            let bounded = clamped_temperature(&json!({"temperature": temperature}));
            let rendered =
                OrderedJson::from_value_with_order(&bounded, &[]).render_default_separators();
            assert_eq!(rendered, expected, "{temperature}");
        }
    }

    /// The body's key order is the oracle's assignment order, not the alphabet:
    /// `model, messages, stream, tools, tool_choice, …`. The tables are the contract,
    /// and this pins them without a cross-language run.
    #[test]
    fn the_body_keeps_the_order_the_oracle_assigns() {
        let rendered = prepared(
            json!({
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": false,
                "tools": [catalog_tool("web_search")],
                "tool_choice": {"type": "function", "function": {"name": "create_pptx"}},
            }),
            json!({}),
        )
        .render_body();
        let position = |needle: &str| rendered.find(needle).unwrap_or_else(|| panic!("{needle}"));
        assert!(position("\"model\"") < position("\"messages\""));
        assert!(position("\"messages\"") < position("\"stream\""));
        assert!(position("\"stream\"") < position("\"tools\""));
        assert!(position("\"tools\"") < position("\"tool_choice\""));
        // A forced tool choice keeps `type` before `function`.
        assert!(
            rendered.contains(
                r#""tool_choice": {"type": "function", "function": {"name": "create_pptx"}}"#
            ),
            "{rendered}"
        );
    }

    /// Tool schemas render with the **catalog's** order: `query` is declared before
    /// `intent`, and the property object puts `type` before `description`. Neither is
    /// alphabetical, so a sorted rendering would visibly differ.
    #[test]
    fn tool_schemas_render_with_the_catalogs_declaration_order() {
        let rendered =
            prepared(json!({"tools": [catalog_tool("web_search")]}), json!({})).render_body();
        assert!(
            rendered.contains(r#""properties": {"query": {"type": "string""#),
            "{rendered}"
        );
    }

    /// A definition the catalog does not own keeps the generic rendering: its edit is
    /// visible in the output and the catalog's orders are **not** borrowed for it.
    #[test]
    fn a_drifted_definition_renders_visibly_instead_of_borrowing_the_catalogs_order() {
        let mut drifted = catalog_tool("web_search");
        drifted["function"]["description"] = json!("edited elsewhere");
        let rendered = prepared(json!({"tools": [drifted]}), json!({})).render_body();
        assert!(rendered.contains("edited elsewhere"), "{rendered}");
        // The generic path sorts the properties (`intent` before `query`), which is the
        // visible difference a substitution would have hidden.
        assert!(
            rendered.contains(r#""properties": {"intent""#),
            "{rendered}"
        );
    }

    /// `contextDiff.delta` entries lead with their type tag — all four shapes the oracle
    /// can emit, including the `trim` entry the parity corpus cannot reach today.
    #[test]
    fn delta_entries_lead_with_their_type() {
        let rendered = prepared(
            json!({}),
            json!({
                "contextEngine": {
                    "enabled": true,
                    "model": "m",
                    "tokenBudget": null,
                    "contextDiff": {
                        "baseContextId": "ce_x",
                        "delta": [
                            {"type": "history", "messages": 1},
                            {"type": "dynamic_context", "chars": 187},
                            {"type": "tools", "count": 26},
                            {"type": "trim", "droppedMessages": 2},
                        ],
                    },
                },
            }),
        )
        .render_diagnostics();
        for expected in [
            r#"{"type":"history","messages":1}"#,
            r#"{"type":"dynamic_context","chars":187}"#,
            r#"{"type":"tools","count":26}"#,
            r#"{"type":"trim","droppedMessages":2}"#,
        ] {
            assert!(
                rendered.contains(expected),
                "{expected} missing from {rendered}"
            );
        }
    }

    /// The taint block renders from the builder's tree — where `sources` keeps its
    /// first-appearance order and each segment its declared key order — and a block the
    /// tree no longer matches keeps its own bytes instead.
    #[test]
    fn the_taint_block_renders_from_the_builders_tree_while_it_agrees() {
        let body = json!({"messages": [
            {"role": "user", "content": "中文提问[用户上传文件上下文]文件内容"},
            {"role": "system", "content": "[Per-turn context]\nsearch snippets"},
        ]});
        let report = build_taint_report_ordered(&body, &ContextTaintSettings::default())
            .expect("taint is enabled");
        let mut request = prepared(json!({}), json!({"contextTaint": report.to_value()}));
        request.taint_ordered = Some(report);
        let rendered = request.render_diagnostics();
        // The user turn is scanned before the system turn, so `trusted_user` leads —
        // not the sorted spelling, which would open with `trusted_system`.
        assert!(
            rendered.contains(r#""sources":{"trusted_user""#),
            "{rendered}"
        );
        assert!(
            rendered
                .contains(r#""segments":[{"source":"trusted_user","trust":"trusted","chars":4,"#,),
            "{rendered}"
        );

        // A drifted block does not get borrowed orders: the edit stays visible.
        let drifted = prepared(json!({}), json!({"contextTaint": {"tainted": false}}));
        let drifted_rendered = drifted.render_diagnostics();
        assert!(
            drifted_rendered.contains(r#""contextTaint":{"tainted":false}"#),
            "{drifted_rendered}"
        );
    }
}
