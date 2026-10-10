//! Budget policy, pricing, and cost estimation — the pure half of `gateway/budget_manager.py`.
//!
//! The oracle module is 371 lines and its ledger is **SQLite** (`connect_db`, `record_spend`,
//! `daily_spend`, `over_daily_budget`, `should_downgrade`, `budget_status`). That is a store, and
//! it is its own slice — but it is also a precondition for the assembly, which calls
//! `should_downgrade` when a downgrade policy is active. What is here is everything that needs no
//! database: the pricing table, the cost arithmetic, the policy object and its payload override,
//! the in-memory tool budget, the scope key, and the cost diagnostic.
//!
//! `today()` reads the clock in the oracle; here the clock is a parameter, as it is for
//! `utc_now_iso` and `format_current_time_context`, so this module stays pure. The date itself
//! reuses [`crate::core_utils::isoformat_seconds`] rather than re-deriving the civil date.

use std::collections::HashMap;
use std::sync::Mutex;

use serde_json::{Map, Value};

use crate::core_utils::{
    isoformat_seconds, python_float_opt, python_int_opt, python_truthy, round_six, text_or_empty,
};
use crate::python_json::value_str;

/// Mirrors `SPEND_TABLE`.
pub const SPEND_TABLE: &str = "budget_daily";
/// Mirrors `DOWNGRADE_POLICY`.
pub const DOWNGRADE_POLICY: &str = "downgrade_to_flash_when_exceeded";

/// Mirrors `VALID_POLICIES`.
pub fn valid_policies() -> [&'static str; 2] {
    ["none", DOWNGRADE_POLICY]
}

/// Mirrors `BudgetSettings`: the limits default to **unlimited** (`0`), the policy to `none`, and
/// the two priced models to the values the oracle ships.
#[derive(Debug, Clone, PartialEq)]
pub struct BudgetSettings {
    pub tracking_enabled: bool,
    pub max_total_tokens: i64,
    pub max_agent_tokens: i64,
    pub max_search_calls: i64,
    pub max_tool_calls: i64,
    pub max_estimated_cost_usd: f64,
    pub policy: String,
    /// USD per 1M tokens, `(input, output)`.
    pub pricing: Vec<(String, (f64, f64))>,
}

impl Default for BudgetSettings {
    fn default() -> Self {
        Self {
            tracking_enabled: true,
            max_total_tokens: 0,
            max_agent_tokens: 0,
            max_search_calls: 0,
            max_tool_calls: 0,
            max_estimated_cost_usd: 0.0,
            policy: "none".to_string(),
            pricing: vec![
                ("deepseek-v4-pro".to_string(), (0.55, 2.19)),
                ("deepseek-v4-flash".to_string(), (0.27, 1.10)),
            ],
        }
    }
}

/// Mirrors `_usage_int`: the first name whose value is present, non-empty and convertible to an
/// integer, floored at zero. A name that looks convertible but is not (a word, a list) is
/// **skipped** rather than treated as zero, so a later spelling can still win.
pub fn usage_int(usage: &Value, names: &[&str]) -> i64 {
    for name in names {
        let Some(found) = usage.get(*name) else {
            continue;
        };
        if found.is_null() {
            continue;
        }
        if let Value::String(text) = found {
            if text.is_empty() {
                continue;
            }
        }
        if let Some(value) = python_int_opt(Some(found)) {
            return value.max(0);
        }
    }
    0
}

/// Mirrors `model_pricing`: USD per 1M tokens, or `(0.0, 0.0)` for a local or unknown model.
pub fn model_pricing(model: Option<&str>, settings: &BudgetSettings) -> (f64, f64) {
    let name = model.unwrap_or("").trim();
    for (candidate, price) in &settings.pricing {
        if candidate == name {
            return *price;
        }
    }
    (0.0, 0.0)
}

/// Mirrors `estimate_cost`, including the six-decimal rounding: a cost is reported to
/// microdollars, so binary-float noise never reaches the caller.
pub fn estimate_cost(
    prompt_tokens: i64,
    completion_tokens: i64,
    model: Option<&str>,
    settings: &BudgetSettings,
) -> f64 {
    let (input_price, output_price) = model_pricing(model, settings);
    let cost = (prompt_tokens.max(0) as f64 / 1_000_000.0) * input_price
        + (completion_tokens.max(0) as f64 / 1_000_000.0) * output_price;
    round_six(cost)
}

/// Mirrors `cost_from_usage`: a non-dict usage reads as `{}`, so it costs nothing.
pub fn cost_from_usage(usage: &Value, model: Option<&str>, settings: &BudgetSettings) -> f64 {
    let data = if usage.is_object() {
        usage.clone()
    } else {
        Value::Object(Map::new())
    };
    estimate_cost(
        usage_int(&data, &["prompt_tokens", "promptTokens"]),
        usage_int(&data, &["completion_tokens", "completionTokens"]),
        model,
        settings,
    )
}

/// Mirrors `BudgetPolicy`.
#[derive(Debug, Clone, PartialEq)]
pub struct BudgetPolicy {
    pub max_total_tokens: i64,
    pub max_agent_tokens: i64,
    pub max_search_calls: i64,
    pub max_tool_calls: i64,
    pub max_estimated_cost_usd: f64,
    pub policy: String,
}

impl BudgetPolicy {
    /// Mirrors the `downgrade` property.
    pub fn downgrade(&self) -> bool {
        self.policy == DOWNGRADE_POLICY
    }

    /// Mirrors `to_dict`.
    ///
    /// **Key order.** The oracle inserts `maxTotalTokens, maxAgentTokens, maxSearchCalls,
    /// maxToolCalls, maxEstimatedCostUsd, policy`, and this payload reaches the wire as
    /// `diagnostics["budgetPolicy"]`, where Python's response `json.dumps` does not sort. A
    /// `serde_json::Map` iterates alphabetically, so the serializer that serves this field
    /// must be handed the order explicitly — the same treatment the other byte-exact
    /// responses get — rather than relying on this `Value` alone.
    pub fn to_value(&self) -> Value {
        serde_json::json!({
            "maxTotalTokens": self.max_total_tokens,
            "maxAgentTokens": self.max_agent_tokens,
            "maxSearchCalls": self.max_search_calls,
            "maxToolCalls": self.max_tool_calls,
            "maxEstimatedCostUsd": self.max_estimated_cost_usd,
            "policy": self.policy,
        })
    }
}

/// Mirrors `default_budget_policy`.
pub fn default_budget_policy(settings: &BudgetSettings) -> BudgetPolicy {
    BudgetPolicy {
        max_total_tokens: settings.max_total_tokens,
        max_agent_tokens: settings.max_agent_tokens,
        max_search_calls: settings.max_search_calls,
        max_tool_calls: settings.max_tool_calls,
        max_estimated_cost_usd: settings.max_estimated_cost_usd,
        policy: settings.policy.clone(),
    }
}

/// Mirrors `budget_policy_from_payload`.
///
/// The payload's `budget` block overrides the defaults **per field**: a field that is present but
/// unusable (a word, a list, `null`) falls back rather than failing, and a limit is floored at
/// zero — so a negative limit reads as unlimited, not as an error. An unknown `policy` also falls
/// back, which is why the payload cannot turn on a policy the server does not know.
pub fn budget_policy_from_payload(payload: &Value, settings: &BudgetSettings) -> BudgetPolicy {
    let base = default_budget_policy(settings);
    let block: Map<String, Value> = match payload.get("budget") {
        Some(Value::Object(fields)) => fields.clone(),
        _ => Map::new(),
    };

    let int_field = |key: &str, fallback: i64| -> i64 {
        match block.get(key).and_then(|value| python_int_opt(Some(value))) {
            Some(value) => value.max(0),
            None => fallback,
        }
    };
    let float_field = |key: &str, fallback: f64| -> f64 {
        match block.get(key).and_then(python_float_opt) {
            Some(value) => value.max(0.0),
            None => fallback,
        }
    };

    let policy = match payload.get("budgetPolicy") {
        Some(found) if python_truthy(found) => value_str(found),
        _ => match block.get("policy") {
            Some(found) if python_truthy(found) => value_str(found),
            _ => base.policy.clone(),
        },
    };
    let policy = if valid_policies().contains(&policy.as_str()) {
        policy
    } else {
        base.policy.clone()
    };

    BudgetPolicy {
        max_total_tokens: int_field("max_total_tokens", base.max_total_tokens),
        max_agent_tokens: int_field("max_agent_tokens", base.max_agent_tokens),
        max_search_calls: int_field("max_search_calls", base.max_search_calls),
        max_tool_calls: int_field("max_tool_calls", base.max_tool_calls),
        max_estimated_cost_usd: float_field("max_estimated_cost_usd", base.max_estimated_cost_usd),
        policy,
    }
}

/// Mirrors `ToolBudget`: a thread-safe per-run limit, where `total_limit <= 0` means unlimited.
#[derive(Debug)]
pub struct ToolBudget {
    total_limit: i64,
    inner: Mutex<ToolBudgetInner>,
}

#[derive(Debug, Default)]
struct ToolBudgetInner {
    used: i64,
    used_by_key: HashMap<String, i64>,
}

impl ToolBudget {
    pub fn new(total_limit: i64) -> Self {
        Self {
            total_limit: total_limit.max(0),
            inner: Mutex::new(ToolBudgetInner::default()),
        }
    }

    /// The limit as the oracle exposes `total_limit` — already floored at zero.
    pub fn total_limit(&self) -> i64 {
        self.total_limit
    }

    /// Mirrors `try_consume`: a refusal leaves the counters untouched, and the key is normalised
    /// through `str(key or "default")` so an empty key lands in the default bucket.
    pub fn try_consume(&self, key: &str) -> bool {
        let normalized = if key.is_empty() {
            "default".to_string()
        } else {
            key.to_string()
        };
        let mut inner = self
            .inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if self.total_limit > 0 && inner.used >= self.total_limit {
            return false;
        }
        inner.used += 1;
        *inner.used_by_key.entry(normalized).or_insert(0) += 1;
        true
    }

    pub fn used(&self) -> i64 {
        self.inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .used
    }

    pub fn used_for(&self, key: &str) -> i64 {
        self.inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .used_by_key
            .get(key)
            .copied()
            .unwrap_or(0)
    }
}

/// Mirrors `today()`: the UTC date, from an injected clock.
pub fn today(epoch_seconds: i64) -> String {
    isoformat_seconds(epoch_seconds, 0)[..10].to_string()
}

/// Mirrors `budget_scope`.
///
/// The explicit `memoryScope` wins outright; otherwise a project id, prefixed; otherwise
/// `global`. Both branches cap at **120 characters** — which is what keeps a long id from
/// becoming an unbounded ledger key.
pub fn budget_scope(payload: &Value) -> String {
    let raw = text_or_empty(payload.get("memoryScope")).trim().to_string();
    if !raw.is_empty() {
        return raw.chars().take(120).collect();
    }
    let project_id = match payload.get("projectId") {
        Some(found) if python_truthy(found) => value_str(found),
        _ => text_or_empty(payload.get("activeProjectId")),
    };
    let project_id = project_id.trim().to_string();
    if !project_id.is_empty() {
        return format!("project:{project_id}").chars().take(120).collect();
    }
    "global".to_string()
}

/// Mirrors `diagnostics_with_cost`: a copy of the diagnostics with `costUsd` added.
///
/// One asymmetry, recorded because it is the only one in this module: the oracle calls
/// `dict(diagnostics)` with **no guard**, so a non-dict raises there; this reads a
/// non-object as `{}`. Diagnostics are assembled internally, so neither branch is
/// reachable from the wired path — and note that `cost_from_usage` right next to it *does*
/// guard its input in the oracle, so the two must not be "unified" later by reflex.
pub fn diagnostics_with_cost(
    diagnostics: &Value,
    usage: &Value,
    model: Option<&str>,
    settings: &BudgetSettings,
) -> Value {
    let mut result: Map<String, Value> = match diagnostics {
        Value::Object(fields) => fields.clone(),
        _ => Map::new(),
    };
    result.insert(
        "costUsd".to_string(),
        serde_json::json!(cost_from_usage(usage, model, settings)),
    );
    Value::Object(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_usage_value_that_cannot_convert_lets_a_later_spelling_win() {
        let names = ["prompt_tokens", "promptTokens"];
        // The first name is present but unusable, so the second one still counts.
        assert_eq!(
            usage_int(&json!({"prompt_tokens": "abc", "promptTokens": 7}), &names),
            7
        );
        assert_eq!(
            usage_int(&json!({"prompt_tokens": [], "promptTokens": 7}), &names),
            7
        );
        assert_eq!(
            usage_int(&json!({"prompt_tokens": "", "promptTokens": 7}), &names),
            7
        );
        assert_eq!(
            usage_int(&json!({"prompt_tokens": null, "promptTokens": 7}), &names),
            7
        );
        // Truncation, not rounding; a floor at zero; and a padded string parses.
        assert_eq!(usage_int(&json!({"prompt_tokens": 3.7}), &names), 3);
        assert_eq!(usage_int(&json!({"prompt_tokens": true}), &names), 1);
        assert_eq!(usage_int(&json!({"prompt_tokens": -5}), &names), 0);
        assert_eq!(usage_int(&json!({"prompt_tokens": " 5 "}), &names), 5);
    }

    #[test]
    fn pricing_trims_the_name_and_an_unknown_model_is_free() {
        let settings = BudgetSettings::default();
        assert_eq!(
            model_pricing(Some(" deepseek-v4-pro "), &settings),
            (0.55, 2.19)
        );
        assert_eq!(
            model_pricing(Some("DeepSeek-V4-Pro"), &settings),
            (0.0, 0.0)
        );
        assert_eq!(model_pricing(Some(""), &settings), (0.0, 0.0));
        assert_eq!(model_pricing(None, &settings), (0.0, 0.0));
    }

    #[test]
    fn a_cost_is_reported_in_microdollars() {
        let settings = BudgetSettings::default();
        assert_eq!(
            estimate_cost(1_234_567, 765_432, Some("deepseek-v4-pro"), &settings),
            2.355308
        );
        // A typical request costs a fraction of a cent; the six decimals are what keep
        // the binary float's noise out of the diagnostic.
        assert_eq!(
            cost_from_usage(
                &json!({"prompt_tokens": 10, "completion_tokens": 20}),
                Some("deepseek-v4-pro"),
                &settings
            ),
            4.9e-5
        );
        // A usage that is not an object costs nothing.
        assert_eq!(
            cost_from_usage(&json!("not-a-dict"), Some("deepseek-v4-pro"), &settings),
            0.0
        );
    }

    #[test]
    fn limits_in_a_payload_floor_and_fall_back() {
        let settings = BudgetSettings {
            max_total_tokens: 42,
            max_search_calls: 9,
            ..BudgetSettings::default()
        };
        let policy = budget_policy_from_payload(
            &json!({"budget": {
                "max_total_tokens": -5,
                "max_agent_tokens": "50",
                "max_tool_calls": 3.9,
                "max_search_calls": "abc",
            }}),
            &settings,
        );
        // Present and negative: floored at zero, *not* the fallback 42.
        assert_eq!(policy.max_total_tokens, 0);
        assert_eq!(policy.max_agent_tokens, 50);
        assert_eq!(policy.max_tool_calls, 3);
        // Unusable: falls back to the settings rather than flooring.
        assert_eq!(policy.max_search_calls, 9);

        let priced = budget_policy_from_payload(
            &json!({"budget": {"max_estimated_cost_usd": "1.25"}}),
            &settings,
        );
        assert_eq!(priced.max_estimated_cost_usd, 1.25);
        let floored = budget_policy_from_payload(
            &json!({"budget": {"max_estimated_cost_usd": -3.0}}),
            &settings,
        );
        assert_eq!(floored.max_estimated_cost_usd, 0.0);
    }

    #[test]
    fn a_payload_cannot_turn_on_a_policy_the_server_does_not_know() {
        let settings = BudgetSettings {
            policy: DOWNGRADE_POLICY.to_string(),
            ..BudgetSettings::default()
        };
        let unknown =
            budget_policy_from_payload(&json!({"budgetPolicy": "unknown-policy"}), &settings);
        assert_eq!(unknown.policy, DOWNGRADE_POLICY);
        assert!(unknown.downgrade());
        // An empty `budgetPolicy` falls through to the block's own choice.
        let from_block = budget_policy_from_payload(
            &json!({"budgetPolicy": "", "budget": {"policy": "none"}}),
            &settings,
        );
        assert_eq!(from_block.policy, "none");
        assert!(!from_block.downgrade());
    }

    #[test]
    fn a_refused_tool_call_leaves_the_counters_untouched() {
        let budget = ToolBudget::new(2);
        assert_eq!(budget.total_limit(), 2);
        assert!(budget.try_consume(""));
        assert!(budget.try_consume("a"));
        assert!(!budget.try_consume("a"));
        // The refusal consumed nothing: the counts stay where the successes left them.
        assert_eq!(budget.used(), 2);
        assert_eq!(budget.used_for("default"), 1);
        assert_eq!(budget.used_for("a"), 1);
        // A non-positive limit is unlimited, and its floor is observable.
        let unlimited = ToolBudget::new(-3);
        assert_eq!(unlimited.total_limit(), 0);
        assert!(unlimited.try_consume("x"));
    }

    #[test]
    fn a_long_scope_is_capped_where_the_ledger_key_starts() {
        // Capped by code point, so a 200-character Chinese scope keeps 120 characters.
        let scope = budget_scope(&json!({"memoryScope": "中".repeat(200)}));
        assert_eq!(scope.chars().count(), 120);
        assert_eq!(
            budget_scope(&json!({"memoryScope": "  project:abc  "})),
            "project:abc"
        );
        assert_eq!(
            budget_scope(&json!({"memoryScope": 0, "projectId": "p1"})),
            "project:p1"
        );
        assert_eq!(
            budget_scope(&json!({"projectId": 0, "activeProjectId": "p2"})),
            "project:p2"
        );
        assert_eq!(budget_scope(&json!({})), "global");
    }

    #[test]
    fn the_cost_diagnostic_overwrites_a_cost_it_finds() {
        let settings = BudgetSettings::default();
        let with_cost = diagnostics_with_cost(
            &json!({"costUsd": 9.9, "other": null}),
            &json!({"prompt_tokens": 10, "completion_tokens": 20}),
            Some("deepseek-v4-pro"),
            &settings,
        );
        assert_eq!(with_cost, json!({"costUsd": 4.9e-5, "other": null}));
        assert_eq!(
            diagnostics_with_cost(&json!({}), &json!({}), None, &settings),
            json!({"costUsd": 0.0})
        );
    }

    #[test]
    fn today_reads_the_injected_clock() {
        assert_eq!(today(1_758_081_906), "2025-09-17");
    }
}
