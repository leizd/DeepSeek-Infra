//! Budget-ledger parity probe, Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/budget_ledger_parity_probe.py`.
//!
//! The Python probe runs the oracle's own SQL against an in-memory database. **This side has
//! no SQL** — the statements and the connection are the store's slice — so the upsert
//! semantics are simulated by a map whose accumulate/overwrite behaviour mirrors the
//! `ON CONFLICT ... DO UPDATE SET` clause. What the two sides therefore compare is the
//! shaping around the SQL (the `max(0, int(...))` floors, the six-decimal cost, the
//! timestamp) and the view read back, plus the threshold, downgrade and status logic.
//!
//! `last_error` is threaded as a local, mirroring the oracle's module-level `_last_error`:
//! `budget_status` reads it back out, so whichever read failed most recently wins.
//!
//! Usage::
//!
//!     python tasks/native-runtime/budget_ledger_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example budget_ledger_parity_probe > ../rust.json

use std::cell::RefCell;

use serde_json::{Map, Value, json};

use deepseek_policy::budget_ledger::{
    LedgerDeps, budget_status, daily_spend, over_daily_budget, record_request_spend, record_spend,
    should_downgrade,
};
use deepseek_policy::budget_manager::{BudgetPolicy, BudgetSettings};
use deepseek_policy::python_json::OrderedJson;

const DAY: &str = "2026-09-18";
const NOW_ISO: &str = "2026-09-18T00:00:00Z";

fn spend_cases() -> Vec<Value> {
    vec![
        json!({"totalTokens": 0, "costUsd": 0.0, "searchCalls": 0, "toolCalls": 0}),
        json!({"totalTokens": 100, "costUsd": 0.5, "searchCalls": 2, "toolCalls": 3}),
        json!({"totalTokens": 1_000, "costUsd": 1.5, "searchCalls": 3, "toolCalls": 4}),
        json!({"totalTokens": 999, "costUsd": 1.499_999, "searchCalls": 2, "toolCalls": 3}),
    ]
}

fn policy_cases() -> Vec<Value> {
    vec![
        json!({"max_total_tokens": 0, "max_agent_tokens": 0, "max_search_calls": 0, "max_tool_calls": 0, "max_estimated_cost_usd": 0.0, "policy": "none"}),
        json!({"max_total_tokens": 1_000, "max_search_calls": 3, "max_tool_calls": 4, "max_estimated_cost_usd": 1.5, "policy": "none"}),
        json!({"max_total_tokens": 100, "max_search_calls": 0, "max_tool_calls": 0, "max_estimated_cost_usd": 0.0, "policy": "none"}),
        json!({"max_total_tokens": 1, "max_search_calls": 1, "max_tool_calls": 1, "max_estimated_cost_usd": 0.01, "policy": "downgrade_to_flash_when_exceeded"}),
        json!({"max_total_tokens": -5, "max_search_calls": -1, "max_tool_calls": -1, "max_estimated_cost_usd": -1.0, "policy": "none"}),
        json!({"max_total_tokens": 0, "max_search_calls": 0, "max_tool_calls": 0, "max_estimated_cost_usd": 0.0, "policy": "downgrade_to_flash_when_exceeded"}),
    ]
}

fn record_cases() -> Vec<(&'static str, Value)> {
    vec![
        (
            "global",
            json!({"prompt_tokens": 10, "completion_tokens": 20, "cost_usd": 4.9e-05, "model_calls": 1}),
        ),
        (
            "global",
            json!({"prompt_tokens": 5, "completion_tokens": 0, "cost_usd": 0.000_075_5, "model_calls": 1, "tool_calls": 2}),
        ),
        (
            "项目:1",
            json!({"prompt_tokens": -5, "completion_tokens": 3.7, "cost_usd": -1.0, "model_calls": -2, "search_calls": "2"}),
        ),
        (
            "",
            json!({"prompt_tokens": " 7 ", "completion_tokens": "abc", "cost_usd": "0.5", "model_calls": 0}),
        ),
    ]
}

fn request_spend_cases() -> Vec<(Value, Option<&'static str>, Value, i64, i64)> {
    vec![
        (json!({}), None, json!({}), 0, 0),
        (
            json!({"memoryScope": "project:abc"}),
            Some("deepseek-v4-pro"),
            json!({"prompt_tokens": 100, "completion_tokens": 50}),
            2,
            1,
        ),
        (
            json!({}),
            Some("deepseek-v4-flash"),
            json!({"promptTokens": "30"}),
            -3,
            0,
        ),
        (
            json!({"projectId": "p1"}),
            Some("unknown"),
            json!("not-a-dict"),
            1,
            1,
        ),
    ]
}

fn policy(case: &Value) -> BudgetPolicy {
    BudgetPolicy {
        max_total_tokens: case["max_total_tokens"].as_i64().unwrap_or(0),
        max_agent_tokens: case["max_agent_tokens"].as_i64().unwrap_or(0),
        max_search_calls: case["max_search_calls"].as_i64().unwrap_or(0),
        max_tool_calls: case["max_tool_calls"].as_i64().unwrap_or(0),
        max_estimated_cost_usd: case["max_estimated_cost_usd"].as_f64().unwrap_or(0.0),
        policy: case["policy"].as_str().unwrap_or("none").to_string(),
    }
}

/// The row whose spend view equals the Python probe's stubbed view.
fn row_for(case: &Value) -> Value {
    json!({
        "scope": "global",
        "day": DAY,
        "prompt_tokens": case["totalTokens"],
        "completion_tokens": 0,
        "cost_usd": case["costUsd"],
        "model_calls": 1,
        "search_calls": case["searchCalls"],
        "tool_calls": case["toolCalls"],
    })
}

fn main() {
    let mut out: Map<String, Value> = Map::new();
    let settings = BudgetSettings::default();
    let mut last_error = String::new();

    // --- A: thresholds, the downgrade decision and the status envelope -----------------
    let spend_cases = spend_cases();
    let policy_cases = policy_cases();
    let current: RefCell<Value> = RefCell::new(row_for(&spend_cases[0]));
    let read_current = |_scope: &str, _day: &str| -> Result<Option<Value>, String> {
        Ok(Some(current.borrow().clone()))
    };
    let unreachable_write = |_row: &Value| -> Result<(), String> { Ok(()) };
    let reads: RefCell<i64> = RefCell::new(0);
    let counting = |scope: &str, day: &str| -> Result<Option<Value>, String> {
        *reads.borrow_mut() += 1;
        read_current(scope, day)
    };
    let deps = LedgerDeps {
        database_present: true,
        database_path: "probe-budget.db".to_string(),
        day: DAY.to_string(),
        now_iso: NOW_ISO.to_string(),
        read_spend_row: &counting,
        write_spend_row: &unreachable_write,
    };

    for (spend_index, spend_case) in spend_cases.iter().enumerate() {
        *current.borrow_mut() = row_for(spend_case);
        for (policy_index, policy_case) in policy_cases.iter().enumerate() {
            let policy = policy(policy_case);
            let (spend, error) = daily_spend("global", None, &deps);
            assert!(error.is_none());
            let over = over_daily_budget(&spend, &policy);
            out.insert(format!("over::{spend_index}::{policy_index}"), over.clone());
            out.insert(
                format!("downgrade::{spend_index}::{policy_index}"),
                json!(should_downgrade(&policy, &over)),
            );
        }
    }

    for (scope_index, scope) in ["global", "project:abc", ""].iter().enumerate() {
        *reads.borrow_mut() = 0;
        let status = budget_status(scope, &json!({}), &settings, &deps, &last_error);
        out.insert(format!("status::{scope_index}"), status);
        // `today` and `overBudget` each read the ledger: two reads, not one.
        out.insert(
            format!("status-reads::{scope_index}"),
            json!(*reads.borrow()),
        );
    }

    // --- B: the row, the upsert and the read-back -------------------------------------
    let table: RefCell<Map<String, Value>> = RefCell::new(Map::new());
    let write = |row: &Value| -> Result<(), String> {
        let key = format!(
            "{}|{}",
            row.get("scope").and_then(Value::as_str).unwrap_or_default(),
            row.get("day").and_then(Value::as_str).unwrap_or_default()
        );
        let mut table = table.borrow_mut();
        match table.get_mut(&key) {
            None => {
                table.insert(key, row.clone());
            }
            Some(existing) => {
                let existing = existing.as_object_mut().expect("a row is an object");
                for field in [
                    "prompt_tokens",
                    "completion_tokens",
                    "model_calls",
                    "search_calls",
                    "tool_calls",
                ] {
                    let current = existing.get(field).and_then(Value::as_i64).unwrap_or(0);
                    let incoming = row.get(field).and_then(Value::as_i64).unwrap_or(0);
                    existing.insert(field.to_string(), json!(current + incoming));
                }
                let current = existing
                    .get("cost_usd")
                    .and_then(Value::as_f64)
                    .unwrap_or(0.0);
                let incoming = row.get("cost_usd").and_then(Value::as_f64).unwrap_or(0.0);
                existing.insert("cost_usd".to_string(), json!(current + incoming));
                existing.insert(
                    "updated_at".to_string(),
                    row.get("updated_at").cloned().unwrap_or(Value::Null),
                );
            }
        }
        Ok(())
    };
    let read = |scope: &str, day: &str| -> Result<Option<Value>, String> {
        Ok(table.borrow().get(&format!("{scope}|{day}")).cloned())
    };
    let locked = |_scope: &str, _day: &str| -> Result<Option<Value>, String> {
        Err("database is locked".to_string())
    };

    let mut live = LedgerDeps {
        database_present: true,
        database_path: "probe-budget.db".to_string(),
        day: DAY.to_string(),
        now_iso: NOW_ISO.to_string(),
        read_spend_row: &read,
        write_spend_row: &write,
    };

    let (missing, error) = daily_spend("global", None, &live);
    assert!(error.is_none());
    out.insert("read::missing-row".to_string(), missing);
    let (missing_scope, _) = daily_spend("project:never", None, &live);
    out.insert("read::missing-scope".to_string(), missing_scope);

    for (index, (scope, fields)) in record_cases().iter().enumerate() {
        if let Some(error) = record_spend(&json!(scope), fields, &settings, &live) {
            last_error = error;
        }
        let (spend, read_error) = daily_spend(scope, None, &live);
        if let Some(error) = read_error {
            last_error = error;
        }
        out.insert(format!("record::{index}"), spend);
    }

    if let Some(error) = record_spend(
        &json!("global"),
        &json!({"prompt_tokens": 1, "completion_tokens": 1, "cost_usd": 1e-06, "model_calls": 1}),
        &settings,
        &live,
    ) {
        last_error = error;
    }
    let (accumulated, _) = daily_spend("global", None, &live);
    out.insert("record::accumulated".to_string(), accumulated);
    let (other_day, _) = daily_spend("global", Some("2026-01-01"), &live);
    out.insert("record::other-day".to_string(), other_day);

    let gated_settings = BudgetSettings {
        tracking_enabled: false,
        ..BudgetSettings::default()
    };
    if let Some(error) = record_spend(
        &json!("gated"),
        &json!({"prompt_tokens": 9, "completion_tokens": 9, "cost_usd": 1.0, "model_calls": 1}),
        &gated_settings,
        &live,
    ) {
        last_error = error;
    }
    let (gated, _) = daily_spend("gated", None, &live);
    out.insert("gated::write".to_string(), gated);

    live.database_present = false;
    let (absent, error) = daily_spend("global", None, &live);
    assert!(error.is_none());
    out.insert("absent::read".to_string(), absent);
    live.database_present = true;

    let failing = LedgerDeps {
        database_present: true,
        database_path: "probe-budget.db".to_string(),
        day: DAY.to_string(),
        now_iso: NOW_ISO.to_string(),
        read_spend_row: &locked,
        write_spend_row: &unreachable_write,
    };
    let (failed, read_error) = daily_spend("global", None, &failing);
    if let Some(error) = read_error {
        last_error = error;
    }
    out.insert("failed::read".to_string(), failed);
    out.insert(
        "failed::status".to_string(),
        budget_status("global", &json!({}), &settings, &failing, &last_error),
    );
    out.insert("failed::error".to_string(), json!(last_error));

    for (index, (payload, model, usage, tool_calls, search_calls)) in
        request_spend_cases().iter().enumerate()
    {
        // Nothing reads the ledger's last error after this point, on either side.
        let (view, _error) = record_request_spend(
            payload,
            *model,
            usage,
            *tool_calls,
            *search_calls,
            &settings,
            &live,
        );
        out.insert(format!("request-spend::{index}"), view);
    }
    for (key, scope, day) in [
        ("request-spend::scope-total", "global", None),
        ("request-spend::project-total", "project:abc", None),
        ("request-spend::p1-total", "project:p1", None),
        ("request-spend::free-total", "global", Some("2026-01-01")),
    ] {
        let (spend, _) = daily_spend(scope, day, &live);
        out.insert(key.to_string(), spend);
    }

    let rendered = OrderedJson::from_value_with_order(&Value::Object(out), &[]).render_indent_2();
    println!("{rendered}");
}
