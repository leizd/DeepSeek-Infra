//! The budget ledger's behaviour without its database — the pure half of
//! `budget_manager.py` lines 183-371.
//!
//! What is here is what the oracle's `connect_db`/`initialize_schema` surround rather than
//! what they do: the spend view a row becomes, the parameter shaping an upsert receives, the
//! four threshold checks, `should_downgrade`, `record_request_spend`, and `budget_status`.
//! The two SQL statements and the connection — including the `WAL` / `synchronous` pragmas —
//! are the store's slice, so they arrive as [`LedgerDeps`]; that is the same boundary drawn
//! for the file index in [`crate::attachment_context`].
//!
//! # Failure is data here, not an exception
//!
//! The oracle swallows every database error into a module-level `_last_error` (and logs it),
//! then returns the empty spend view. Nothing raises. So these functions **return** the
//! message that `set_last_error` would have stored, and the caller owns the state —
//! `budget_status` reads it back out, which is why it reports the *newest* failure.
//!
//! # A recorded key-order note
//!
//! `budget_status` answers a request, and its `pricing` map iterates `BUDGET_PRICING` in
//! Python's insertion order where a `serde_json::Map` iterates alphabetically — the same
//! caveat [`crate::budget_manager::BudgetPolicy::to_value`] carries. The serializer that
//! serves this response has to be handed the order.

use serde_json::{Map, Value, json};

use crate::budget_manager::{
    BudgetPolicy, BudgetSettings, budget_policy_from_payload, budget_scope, cost_from_usage,
    usage_int,
};
use crate::core_utils::{
    python_float_opt, python_int_opt, python_truthy, round_six, text_or_empty,
};
use crate::python_json::value_str;

/// `str(scope or "global")`.
fn scope_or_global(scope: &str) -> String {
    if scope.is_empty() {
        "global".to_string()
    } else {
        scope.to_string()
    }
}

/// `SELECT` one `(scope, day)` row, or `None` for no row.
type ReadSpendRow<'a> = &'a dyn Fn(&str, &str) -> Result<Option<Value>, String>;
/// The `INSERT ... ON CONFLICT DO UPDATE` for one row.
type WriteSpendRow<'a> = &'a dyn Fn(&Value) -> Result<(), String>;

/// The reads and writes the pure layer needs, injected.
///
/// `read_spend_row` runs the `SELECT` for one `(scope, day)` and returns the row, or `None`
/// for no row; `write_spend_row` runs the upsert. Both report a failure as `Err`, which
/// becomes the message `set_last_error` stores.
pub struct LedgerDeps<'a> {
    pub database_present: bool,
    pub database_path: String,
    /// `today()`.
    pub day: String,
    /// `datetime.now(timezone.utc).isoformat(timespec="seconds")` with `+00:00` as `Z`.
    pub now_iso: String,
    pub read_spend_row: ReadSpendRow<'a>,
    pub write_spend_row: WriteSpendRow<'a>,
}

/// Mirrors the `empty` view `daily_spend` returns, including for a scope it has never seen.
pub fn empty_spend(scope: &str, day: &str) -> Value {
    json!({
        "scope": scope_or_global(scope),
        "day": day,
        "promptTokens": 0,
        "completionTokens": 0,
        "totalTokens": 0,
        "costUsd": 0.0,
        "modelCalls": 0,
        "searchCalls": 0,
        "toolCalls": 0,
    })
}

/// Mirrors the tail of `daily_spend`: one row becomes the spend view.
///
/// `str(row["scope"])` is kept as `value_str`, so a non-text column would render the way
/// Python renders it rather than failing.
pub fn spend_from_row(row: &Value) -> Value {
    let prompt = python_int_opt(row.get("prompt_tokens")).unwrap_or(0);
    let completion = python_int_opt(row.get("completion_tokens")).unwrap_or(0);
    let cost = python_float_opt(row.get("cost_usd").unwrap_or(&Value::Null)).unwrap_or(0.0);
    json!({
        "scope": text_or_empty(row.get("scope")),
        "day": text_or_empty(row.get("day")),
        "promptTokens": prompt,
        "completionTokens": completion,
        "totalTokens": prompt + completion,
        "costUsd": round_six(cost),
        "modelCalls": python_int_opt(row.get("model_calls")).unwrap_or(0),
        "searchCalls": python_int_opt(row.get("search_calls")).unwrap_or(0),
        "toolCalls": python_int_opt(row.get("tool_calls")).unwrap_or(0),
    })
}

/// Mirrors `daily_spend`: the empty view when the database or the row is missing, the row's
/// own view otherwise, and — when the read fails — the empty view plus the message
/// `set_last_error` would have stored.
pub fn daily_spend(scope: &str, day: Option<&str>, deps: &LedgerDeps) -> (Value, Option<String>) {
    let resolved_day = match day {
        Some(value) if !value.is_empty() => value.to_string(),
        _ => deps.day.clone(),
    };
    let empty = empty_spend(scope, &resolved_day);
    if !deps.database_present {
        return (empty, None);
    }
    match (deps.read_spend_row)(&scope_or_global(scope), &resolved_day) {
        Ok(None) => (empty, None),
        Ok(Some(row)) => (spend_from_row(&row), None),
        Err(error) => (empty, Some(format!("budget read failed: {error}"))),
    }
}

/// Mirrors `over_daily_budget`: the four checks, in the oracle's order.
///
/// A limit of zero means "no limit" rather than "always exceeded", and the comparison is
/// `>=`, so a spend that exactly reaches a limit counts as over.
pub fn over_daily_budget(spend: &Value, policy: &BudgetPolicy) -> Value {
    let total_tokens = python_int_opt(spend.get("totalTokens")).unwrap_or(0);
    let cost = python_float_opt(spend.get("costUsd").unwrap_or(&Value::Null)).unwrap_or(0.0);
    let search_calls = python_int_opt(spend.get("searchCalls")).unwrap_or(0);
    let tool_calls = python_int_opt(spend.get("toolCalls")).unwrap_or(0);

    let mut reasons: Vec<Value> = Vec::new();
    if policy.max_total_tokens > 0 && total_tokens >= policy.max_total_tokens {
        reasons.push(json!("max_total_tokens"));
    }
    if policy.max_estimated_cost_usd > 0.0 && cost >= policy.max_estimated_cost_usd {
        reasons.push(json!("max_estimated_cost_usd"));
    }
    if policy.max_search_calls > 0 && search_calls >= policy.max_search_calls {
        reasons.push(json!("max_search_calls"));
    }
    if policy.max_tool_calls > 0 && tool_calls >= policy.max_tool_calls {
        reasons.push(json!("max_tool_calls"));
    }

    json!({
        "exceeded": !reasons.is_empty(),
        "reasons": reasons,
        "spend": spend.clone(),
    })
}

/// Mirrors `should_downgrade`: the policy has to ask for it first.
pub fn should_downgrade(policy: &BudgetPolicy, over: &Value) -> bool {
    if !policy.downgrade() {
        return false;
    }
    python_truthy(over.get("exceeded").unwrap_or(&Value::Null))
}

/// The row `record_spend` writes, shaped the way the oracle shapes it: `str(scope or
/// "global")`, `max(0, int(...))` for every counter, the cost rounded to microdollars, and
/// the caller's timestamp.
///
/// `Err` is the oracle's own outcome for a value `int()`/`float()` rejects: the exception is
/// caught by the same handler as a failed write, so nothing is inserted and the message goes
/// to the ledger's last error. This side does not reproduce CPython's `ValueError` text
/// (`invalid literal for int() with base 10: 'abc'`) — it says which field did not convert.
/// Only `lastError` would ever render that text, and a later successful read overwrites it.
fn shaped_row(scope: &Value, fields: &Value, day: &str, now_iso: &str) -> Result<Value, String> {
    let counter = |name: &str| -> Result<i64, String> {
        let value = fields.get(name).cloned().unwrap_or(json!(0));
        python_int_opt(Some(&value))
            .map(|parsed| parsed.max(0))
            .ok_or_else(|| format!("{name} is not an integer"))
    };
    let cost_value = fields.get("cost_usd").cloned().unwrap_or(json!(0.0));
    let cost =
        python_float_opt(&cost_value).ok_or_else(|| "cost_usd is not a number".to_string())?;

    Ok(json!({
        "scope": if python_truthy(scope) { value_str(scope) } else { "global".to_string() },
        "day": day,
        "prompt_tokens": counter("prompt_tokens")?,
        "completion_tokens": counter("completion_tokens")?,
        "cost_usd": round_six(cost.max(0.0)),
        "model_calls": counter("model_calls")?,
        "search_calls": counter("search_calls")?,
        "tool_calls": counter("tool_calls")?,
        "updated_at": now_iso,
    }))
}

/// Mirrors `record_spend(scope, **fields)`: recording is gated by the tracking switch, the
/// row is shaped here (so a bad field fails like a failed write), and the failure is
/// remembered rather than raised.
pub fn record_spend(
    scope: &Value,
    fields: &Value,
    settings: &BudgetSettings,
    deps: &LedgerDeps,
) -> Option<String> {
    if !settings.tracking_enabled {
        return None;
    }
    let row = match shaped_row(scope, fields, &deps.day, &deps.now_iso) {
        Ok(row) => row,
        Err(error) => return Some(format!("budget record failed: {error}")),
    };
    match (deps.write_spend_row)(&row) {
        Ok(()) => None,
        Err(error) => Some(format!("budget record failed: {error}")),
    }
}

/// Mirrors `record_request_spend`: one model call's cost, its scope, and the row it adds.
pub fn record_request_spend(
    payload: &Value,
    model: Option<&str>,
    usage: &Value,
    tool_calls: i64,
    search_calls: i64,
    settings: &BudgetSettings,
    deps: &LedgerDeps,
) -> (Value, Option<String>) {
    let data = if usage.is_object() {
        usage.clone()
    } else {
        Value::Object(Map::new())
    };
    let cost = cost_from_usage(&data, model, settings);
    let scope = budget_scope(payload);
    let fields = json!({
        "prompt_tokens": usage_int(&data, &["prompt_tokens", "promptTokens"]),
        "completion_tokens": usage_int(&data, &["completion_tokens", "completionTokens"]),
        "cost_usd": cost,
        "model_calls": 1,
        "tool_calls": tool_calls.max(0),
        "search_calls": search_calls.max(0),
    });
    let last_error = record_spend(&Value::String(scope.clone()), &fields, settings, deps);
    let view = json!({
        "costUsd": cost,
        "scope": scope,
        "model": model.unwrap_or(""),
    });
    (view, last_error)
}

/// Mirrors `budget_status`.
///
/// The oracle reads the ledger **twice** — once for `today` and once inside
/// `over_daily_budget` — so this does too, and `lastError` ends up holding whichever read
/// failed most recently. Collapsing the two reads would hide a concurrent write.
pub fn budget_status(
    scope: &str,
    payload: &Value,
    settings: &BudgetSettings,
    deps: &LedgerDeps,
    last_error: &str,
) -> Value {
    let policy = budget_policy_from_payload(payload, settings);
    let (today, first_error) = daily_spend(scope, None, deps);
    let (over_spend, second_error) = daily_spend(scope, None, deps);
    let over = over_daily_budget(&over_spend, &policy);

    let mut effective_error = last_error.to_string();
    if let Some(error) = first_error {
        effective_error = error;
    }
    if let Some(error) = second_error {
        effective_error = error;
    }

    let mut pricing = Map::new();
    for (model, (input, output)) in &settings.pricing {
        pricing.insert(
            model.clone(),
            json!({"inputPerMTok": input, "outputPerMTok": output}),
        );
    }

    json!({
        "enabled": settings.tracking_enabled,
        "databasePath": deps.database_path,
        "pricing": Value::Object(pricing),
        "policy": policy.to_value(),
        "scope": scope_or_global(scope),
        "today": today,
        "overBudget": over,
        "lastError": effective_error,
    })
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;

    use super::*;
    use crate::budget_manager::BudgetSettings;

    const DAY: &str = "2026-09-18";

    fn no_row(_scope: &str, _day: &str) -> Result<Option<Value>, String> {
        Ok(None)
    }

    fn refuses(_scope: &str, _day: &str) -> Result<Option<Value>, String> {
        Err("database is locked".to_string())
    }

    fn discards(_row: &Value) -> Result<(), String> {
        Ok(())
    }

    fn deps_with<'a>(read: ReadSpendRow<'a>) -> LedgerDeps<'a> {
        LedgerDeps {
            database_present: true,
            database_path: "probe-budget.db".to_string(),
            day: DAY.to_string(),
            now_iso: "2026-09-18T00:00:00Z".to_string(),
            read_spend_row: read,
            write_spend_row: &discards,
        }
    }

    fn policy(downgrade: bool, total: i64, cost: f64) -> BudgetPolicy {
        BudgetPolicy {
            max_total_tokens: total,
            max_agent_tokens: 0,
            max_search_calls: 0,
            max_tool_calls: 0,
            max_estimated_cost_usd: cost,
            policy: if downgrade {
                crate::budget_manager::DOWNGRADE_POLICY.to_string()
            } else {
                "none".to_string()
            },
        }
    }

    fn spend(total_tokens: i64, cost: f64) -> Value {
        json!({
            "scope": "global",
            "day": DAY,
            "totalTokens": total_tokens,
            "costUsd": cost,
            "searchCalls": 0,
            "toolCalls": 0,
        })
    }

    #[test]
    fn a_zero_limit_is_no_limit_and_a_reached_limit_counts() {
        assert!(!python_truthy(
            over_daily_budget(&spend(0, 0.0), &policy(false, 0, 0.0))
                .get("exceeded")
                .unwrap()
        ));
        // `>=`: spending exactly the limit is over it.
        let over = over_daily_budget(&spend(100, 0.0), &policy(false, 100, 0.0));
        assert_eq!(over["reasons"], json!(["max_total_tokens"]));
        // A negative limit is "unset", not "always over".
        assert_eq!(
            over_daily_budget(&spend(1, 1.0), &policy(false, -5, -1.0))["reasons"],
            json!([])
        );
    }

    #[test]
    fn the_reasons_keep_the_oracles_order() {
        let over = over_daily_budget(
            &json!({
                "totalTokens": 10, "costUsd": 2.0, "searchCalls": 3, "toolCalls": 4,
            }),
            &BudgetPolicy {
                max_total_tokens: 10,
                max_agent_tokens: 0,
                max_search_calls: 3,
                max_tool_calls: 4,
                max_estimated_cost_usd: 1.0,
                policy: "none".to_string(),
            },
        );
        assert_eq!(
            over["reasons"],
            json!([
                "max_total_tokens",
                "max_estimated_cost_usd",
                "max_search_calls",
                "max_tool_calls"
            ])
        );
    }

    #[test]
    fn the_downgrade_only_applies_when_the_policy_asks_for_it() {
        let exceeded = json!({"exceeded": true});
        assert!(should_downgrade(&policy(true, 1, 0.0), &exceeded));
        assert!(!should_downgrade(&policy(false, 1, 0.0), &exceeded));
        assert!(!should_downgrade(
            &policy(true, 1, 0.0),
            &json!({"exceeded": false})
        ));
    }

    #[test]
    fn a_missing_row_and_a_missing_database_both_read_as_empty() {
        let deps = deps_with(&no_row);
        let (spend, error) = daily_spend("", None, &deps);
        assert!(error.is_none());
        assert_eq!(spend["scope"], "global");
        assert_eq!(spend["day"], DAY);
        assert_eq!(spend["costUsd"], 0.0);

        let absent = LedgerDeps {
            database_present: false,
            ..deps_with(&refuses)
        };
        let (spend, error) = daily_spend("global", None, &absent);
        assert!(
            error.is_none(),
            "a database that is not there is not an error"
        );
        assert_eq!(spend["totalTokens"], 0);
    }

    #[test]
    fn a_failed_read_returns_the_empty_view_and_names_itself() {
        let deps = deps_with(&refuses);
        let (spend, error) = daily_spend("global", None, &deps);
        assert_eq!(spend["totalTokens"], 0);
        assert_eq!(
            error.as_deref(),
            Some("budget read failed: database is locked")
        );
    }

    #[test]
    fn a_row_becomes_the_spend_view_it_should() {
        let row = json!({
            "scope": "project:abc",
            "day": "2026-01-01",
            "prompt_tokens": "10",
            "completion_tokens": 5,
            "cost_usd": "0.0001234567",
            "model_calls": 2,
        });
        let spend = spend_from_row(&row);
        assert_eq!(spend["scope"], "project:abc");
        assert_eq!(spend["promptTokens"], 10);
        assert_eq!(spend["totalTokens"], 15);
        // Six decimals, as the oracle rounds it.
        assert_eq!(spend["costUsd"], 0.000123);
        assert_eq!(spend["searchCalls"], 0);
    }

    #[test]
    fn recording_floors_shapes_and_can_fail_before_it_writes() {
        let wrote: RefCell<Vec<Value>> = RefCell::new(Vec::new());
        let capture = |row: &Value| -> Result<(), String> {
            wrote.borrow_mut().push(row.clone());
            Ok(())
        };
        let deps = LedgerDeps {
            write_spend_row: &capture,
            ..deps_with(&no_row)
        };
        let settings = BudgetSettings::default();

        assert!(
            record_spend(
                &json!(""),
                &json!({
                    "prompt_tokens": -5, "completion_tokens": 3.7, "cost_usd": -1.0,
                    "model_calls": -2, "search_calls": "2",
                }),
                &settings,
                &deps,
            )
            .is_none()
        );
        let row = wrote.borrow().last().cloned().expect("one row was written");
        assert_eq!(row["scope"], "global", "the empty scope becomes global");
        assert_eq!(row["prompt_tokens"], 0);
        assert_eq!(row["completion_tokens"], 3, "int(3.7) truncates");
        assert_eq!(row["cost_usd"], 0.0);
        assert_eq!(row["model_calls"], 0);
        assert_eq!(row["search_calls"], 2, "a numeric string converts");
        assert_eq!(row["updated_at"], "2026-09-18T00:00:00Z");

        // A value `int()` rejects fails like a failed write: nothing lands.
        let written = wrote.borrow().len();
        let error = record_spend(
            &json!("global"),
            &json!({"completion_tokens": "abc"}),
            &settings,
            &deps,
        );
        assert!(error.is_some_and(|message| message.starts_with("budget record failed: ")));
        assert_eq!(wrote.borrow().len(), written);

        // The tracking switch short-circuits before any shaping.
        let off = BudgetSettings {
            tracking_enabled: false,
            ..BudgetSettings::default()
        };
        assert!(
            record_spend(
                &json!("global"),
                &json!({"completion_tokens": "abc"}),
                &off,
                &deps
            )
            .is_none()
        );
        assert_eq!(wrote.borrow().len(), written);
    }

    #[test]
    fn the_status_reads_the_ledger_twice_and_reports_the_newest_failure() {
        let rows: RefCell<Vec<Option<Value>>> = RefCell::new(vec![None]);
        let reads: RefCell<usize> = RefCell::new(0);
        let read = |_scope: &str, _day: &str| -> Result<Option<Value>, String> {
            *reads.borrow_mut() += 1;
            Ok(rows.borrow()[0].clone())
        };
        let deps = deps_with(&read);
        let status = budget_status("global", &json!({}), &BudgetSettings::default(), &deps, "");
        assert_eq!(*reads.borrow(), 2, "today and overBudget each read");
        assert_eq!(status["scope"], "global");
        assert_eq!(status["databasePath"], "probe-budget.db");
        assert_eq!(status["lastError"], "");
        assert_eq!(status["pricing"]["deepseek-v4-pro"]["inputPerMTok"], 0.55);
        assert_eq!(status["overBudget"]["exceeded"], false);

        // A failing read overwrites whatever the caller last held.
        let failing = deps_with(&refuses);
        let status = budget_status(
            "global",
            &json!({}),
            &BudgetSettings::default(),
            &failing,
            "older",
        );
        assert_eq!(
            status["lastError"],
            "budget read failed: database is locked"
        );
    }
}
