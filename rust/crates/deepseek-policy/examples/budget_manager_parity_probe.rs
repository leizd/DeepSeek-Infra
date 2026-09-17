//! Budget-manager parity probe, Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/budget_manager_parity_probe.py` through
//! `deepseek_policy::budget_manager`. The oracle reads its limits and prices from module
//! globals, so the Python probe swaps seven attributes around each call; here the settings
//! are a parameter and the three cases are values.
//!
//! Two injection points line up with the oracle's probe. `today()` reads the clock there and
//! takes the epoch here — the pinned instant is 2025-09-17T04:05:06Z. And the output goes
//! through [`OrderedJson`] rather than `serde_json::to_string_pretty`, because most of this
//! corpus's costs sit below `1e-4`, where Python writes `4.9e-05` and `serde_json` writes
//! `0.000049`.
//!
//! Usage::
//!
//!     python tasks/native-runtime/budget_manager_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example budget_manager_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use serde_json::{Map, Value, json};

use deepseek_policy::budget_manager::{
    BudgetSettings, DOWNGRADE_POLICY, SPEND_TABLE, ToolBudget, budget_policy_from_payload,
    budget_scope, cost_from_usage, default_budget_policy, diagnostics_with_cost, estimate_cost,
    model_pricing, today, usage_int, valid_policies,
};
use deepseek_policy::python_json::OrderedJson;

/// 2025-09-17T04:05:06Z — the instant the Python probe pins its clock to.
const FIXED_EPOCH_SECONDS: i64 = 1_758_081_906;

const PROMPT_NAMES: [&str; 2] = ["prompt_tokens", "promptTokens"];
const COMPLETION_NAMES: [&str; 2] = ["completion_tokens", "completionTokens"];

fn usage_cases() -> Vec<Value> {
    vec![
        json!({}),
        json!({"prompt_tokens": 10, "completion_tokens": 20}),
        json!({"prompt_tokens": "12", "completion_tokens": "3.9"}),
        json!({"prompt_tokens": 3.7, "completion_tokens": 0.2}),
        json!({"prompt_tokens": true, "completion_tokens": false}),
        json!({"prompt_tokens": -5, "completion_tokens": -1}),
        json!({"prompt_tokens": "abc", "promptTokens": 7}),
        json!({"prompt_tokens": "", "promptTokens": 7}),
        json!({"prompt_tokens": null, "promptTokens": 7}),
        json!({"prompt_tokens": [], "promptTokens": 7}),
        json!({"promptTokens": 9}),
        json!({"prompt_tokens": " 5 "}),
    ]
}

fn models() -> [Option<&'static str>; 7] {
    [
        None,
        Some(""),
        Some("deepseek-v4-pro"),
        Some("deepseek-v4-flash"),
        Some("unknown"),
        Some(" deepseek-v4-pro "),
        Some("DeepSeek-V4-Pro"),
    ]
}

fn policy_payloads() -> Vec<Value> {
    vec![
        json!({}),
        json!({"budget": "not-a-dict"}),
        json!({"budget": {}}),
        json!({"budget": {"max_total_tokens": 100, "max_agent_tokens": "50"}}),
        json!({"budget": {"max_total_tokens": -5, "max_estimated_cost_usd": "1.25"}}),
        json!({"budget": {"max_total_tokens": "abc", "max_tool_calls": 3.9}}),
        json!({"budget": {"max_search_calls": null, "max_tool_calls": []}}),
        json!({"budget": {"policy": "downgrade_to_flash_when_exceeded"}}),
        json!({"budgetPolicy": "downgrade_to_flash_when_exceeded"}),
        json!({"budgetPolicy": "unknown-policy", "budget": {"policy": "none"}}),
        json!({"budgetPolicy": "", "budget": {"policy": "downgrade_to_flash_when_exceeded"}}),
    ]
}

fn scope_payloads() -> Vec<Value> {
    vec![
        json!({}),
        json!({"memoryScope": "  project:abc  "}),
        json!({"memoryScope": "中".repeat(200)}),
        json!({"memoryScope": ""}),
        json!({"memoryScope": null}),
        json!({"projectId": "p1"}),
        json!({"projectId": "p1", "activeProjectId": "p2"}),
        json!({"projectId": ""}),
        json!({"projectId": "", "activeProjectId": "p2"}),
        json!({"activeProjectId": 7}),
        json!({"projectId": 0, "activeProjectId": "p2"}),
        json!({"memoryScope": 0, "projectId": "p1"}),
    ]
}

fn diagnostics_cases() -> Vec<Value> {
    vec![
        json!({}),
        json!({"a": 1}),
        json!({"costUsd": 9.9, "other": null}),
        json!({"nested": {"x": 1}}),
    ]
}

/// The three settings sets the Python probe swaps in, in order. The first is the module
/// default; the second turns the downgrade policy on with a single priced model; the third
/// prices nothing.
fn settings_cases() -> Vec<BudgetSettings> {
    vec![
        BudgetSettings::default(),
        BudgetSettings {
            max_total_tokens: 1_000,
            max_agent_tokens: 500,
            max_search_calls: 3,
            max_tool_calls: 4,
            max_estimated_cost_usd: 1.5,
            policy: DOWNGRADE_POLICY.to_string(),
            pricing: vec![("deepseek-v4-pro".to_string(), (1.0, 2.0))],
            ..BudgetSettings::default()
        },
        BudgetSettings {
            pricing: Vec::new(),
            ..BudgetSettings::default()
        },
    ]
}

fn tool_budget_runs() -> [(i64, Vec<&'static str>); 4] {
    [
        (0, vec!["", "a", "a", "b"]),
        (2, vec!["", "a", "b"]),
        (1, vec!["x"]),
        (-3, vec!["a"]),
    ]
}

fn main() {
    let mut out: Map<String, Value> = Map::new();
    let default = BudgetSettings::default();

    let mut policies = valid_policies().to_vec();
    policies.sort_unstable();
    out.insert(
        "constants".to_string(),
        json!([SPEND_TABLE, DOWNGRADE_POLICY, policies]),
    );

    let usage_cases = usage_cases();
    for (index, usage) in usage_cases.iter().enumerate() {
        out.insert(
            format!("usage-int::{index}"),
            json!(usage_int(usage, &PROMPT_NAMES)),
        );
        out.insert(
            format!("completion-int::{index}"),
            json!(usage_int(usage, &COMPLETION_NAMES)),
        );
    }

    let models = models();
    for (index, model) in models.iter().enumerate() {
        let (input, output) = model_pricing(*model, &default);
        out.insert(format!("pricing::{index}"), json!([input, output]));
        out.insert(
            format!("cost::{index}"),
            json!(estimate_cost(1_234_567, 765_432, *model, &default)),
        );
    }

    for (index, usage) in usage_cases.iter().enumerate() {
        out.insert(
            format!("cost-from-usage::{index}"),
            json!(cost_from_usage(usage, Some("deepseek-v4-pro"), &default)),
        );
        out.insert(
            format!("cost-from-usage-bad::{index}"),
            json!(cost_from_usage(
                &json!("not-a-dict"),
                Some("deepseek-v4-pro"),
                &default
            )),
        );
    }

    let base = default_budget_policy(&default);
    out.insert("default-policy".to_string(), base.to_value());
    out.insert(
        "default-policy::downgrade".to_string(),
        json!(base.downgrade()),
    );

    for (settings_index, settings) in settings_cases().iter().enumerate() {
        out.insert(
            format!("default-policy-s{settings_index}"),
            default_budget_policy(settings).to_value(),
        );
        for (payload_index, payload) in policy_payloads().iter().enumerate() {
            let policy = budget_policy_from_payload(payload, settings);
            out.insert(
                format!("policy-s{settings_index}::{payload_index}"),
                policy.to_value(),
            );
            out.insert(
                format!("policy-downgrade-s{settings_index}::{payload_index}"),
                json!(policy.downgrade()),
            );
        }
        for (model_index, model) in models.iter().enumerate() {
            let (input, output) = model_pricing(*model, settings);
            out.insert(
                format!("pricing-s{settings_index}::{model_index}"),
                json!([input, output]),
            );
            out.insert(
                format!("cost-s{settings_index}::{model_index}"),
                json!(estimate_cost(1_000_000, 1_000_000, *model, settings)),
            );
        }
    }

    for (index, (limit, keys)) in tool_budget_runs().iter().enumerate() {
        let budget = ToolBudget::new(*limit);
        let attempts: Vec<bool> = keys.iter().map(|key| budget.try_consume(key)).collect();
        let mut used_by_key = Map::new();
        for key in keys {
            let normalized: &str = if key.is_empty() { "default" } else { key };
            let count = budget.used_for(normalized);
            if count > 0 {
                used_by_key.insert(normalized.to_string(), json!(count));
            }
        }
        out.insert(
            format!("tool-budget::{index}"),
            json!({
                "totalLimit": budget.total_limit(),
                "attempts": attempts,
                "used": budget.used(),
                "usedByKey": Value::Object(used_by_key),
            }),
        );
    }

    out.insert("today".to_string(), json!(today(FIXED_EPOCH_SECONDS)));

    for (index, payload) in scope_payloads().iter().enumerate() {
        out.insert(format!("scope::{index}"), json!(budget_scope(payload)));
    }

    for (diagnostics_index, diagnostics) in diagnostics_cases().iter().enumerate() {
        for (usage_index, usage) in usage_cases.iter().take(4).enumerate() {
            out.insert(
                format!("diagnostics::{diagnostics_index}::{usage_index}"),
                diagnostics_with_cost(diagnostics, usage, Some("deepseek-v4-flash"), &default),
            );
        }
    }

    let rendered = OrderedJson::from_value_with_order(&Value::Object(out), &[]).render_indent_2();
    println!("{rendered}");
}
