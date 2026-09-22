//! The `diagnostics` object the streaming routes put in their terminal event.
//!
//! `stream_deepseek` builds `result_diagnostics` by folding a chain of
//! `diagnostics_with_*` helpers over a base object, and `/api/chat`'s `done` event
//! carries the result. This module ports the three that are pure:
//!
//! | helper | keys it adds |
//! |---|---|
//! | [`diagnostics_with_tools`] | `toolCallCount`, `toolNames` (sorted, deduplicated) |
//! | [`diagnostics_with_usage`] | `cacheHitTokens`, `cacheMissTokens`, `cacheHitRate` |
//! | [`search_round_count`] | the `rounds` length a caller passes to the cost helper |
//!
//! The rest of the chain needs state this crate does not have: `gateway_attempts` is
//! the retry loop's, `semantic_cache` is the cache's, `with_trace_diagnostics` is the
//! trace store's, and `edge_diagnostics` is the edge router's. Those are the caller's
//! to fold in; what is here is what can be computed from the turn itself.
//!
//! # `round(x, 1)` is Python's, not Rust's
//!
//! `cacheHitRate` is `round((hit / total) * 100, 1)`. Python's `round` is
//! **half-to-even** on the *decimal* value, so `round(1.05, 1)` is `1.1` while
//! `round(2.675, 1)` is `2.7` — the second because `2.675` is not exactly
//! representable and the stored double is below the tie. `f64::round_ties_even` gives
//! the same answer for both, which the parity probe checks over a token corpus rather
//! than over one example.

use serde_json::{Value, json};

use crate::chat_stream_events::usage_int;

/// `diagnostics_with_tools`: the count and the sorted, deduplicated names.
///
/// `sorted(set(names))` — a set, so a tool called twice appears once, and the sort is
/// lexicographic over the names.
pub fn diagnostics_with_tools(diagnostics: &Value, count: usize, names: &[String]) -> Value {
    let mut unique: Vec<&str> = names.iter().map(String::as_str).collect();
    unique.sort_unstable();
    unique.dedup();
    let mut result = diagnostics.as_object().cloned().unwrap_or_default();
    result.insert("toolCallCount".to_string(), json!(count));
    result.insert(
        "toolNames".to_string(),
        Value::Array(unique.into_iter().map(|name| json!(name)).collect()),
    );
    Value::Object(result)
}

/// `diagnostics_with_usage`: the cache-token counters and their hit rate.
///
/// The rate is `round((hit / total) * 100, 1)` when there are any cache tokens at all,
/// and `0.0` when there are none — a *float* zero, not an integer, which is why the
/// value is written through `json!` on an `f64`.
pub fn diagnostics_with_usage(diagnostics: &Value, usage: &Value) -> Value {
    let hit_tokens = usage_int(usage, "prompt_cache_hit_tokens", "promptCacheHitTokens");
    let miss_tokens = usage_int(usage, "prompt_cache_miss_tokens", "promptCacheMissTokens");
    let mut result = diagnostics.as_object().cloned().unwrap_or_default();
    result.insert("cacheHitTokens".to_string(), json!(hit_tokens));
    result.insert("cacheMissTokens".to_string(), json!(miss_tokens));
    let total = hit_tokens + miss_tokens;
    let rate = if total == 0 {
        0.0
    } else {
        round_one_decimal((hit_tokens as f64 / total as f64) * 100.0)
    };
    result.insert("cacheHitRate".to_string(), json!(rate));
    Value::Object(result)
}

/// `round(value, 1)`.
///
/// The direct translation — `(value * 10).round_ties_even() / 10.0` — is **wrong for
/// this**, and the unit test below caught it: `1.05 * 10` is exactly `10.5`, whose
/// half-to-even rounding is `10`, so that version returns `1.0` where Python returns
/// `1.1`. Python's `round` works on the *decimal* value of the stored double, and
/// `1.05` is stored slightly **above** its decimal tie.
///
/// `format!("{value:.1}")` is the same algorithm: Rust's float formatting is
/// correctly rounded to the shortest decimal that round-trips, and its tie rule is
/// half-to-even on that decimal. Measured over the tie corpus in the test, and over a
/// token corpus by the parity probe, it agrees with Python's `round(value, 1)`
/// everywhere this reaches.
///
/// The parse cannot fail for a finite value formatted with one decimal place; the
/// fallback returns the input rather than panicking, so a non-finite input (which the
/// caller's `total == 0` branch already excludes) degrades to the unrounded value.
fn round_one_decimal(value: f64) -> f64 {
    if !value.is_finite() {
        return value;
    }
    format!("{value:.1}").parse::<f64>().unwrap_or(value)
}

/// `diagnostics_with_search`: the search round and result counts, when there was a
/// search.
///
/// `if search_data:` — a **falsy** search adds nothing at all, not zeroes. So a turn
/// with no search has no `searchRoundCount` key, and the parity probe covers both.
pub fn diagnostics_with_search(diagnostics: &Value, search_data: Option<&Value>) -> Value {
    let mut result = diagnostics.as_object().cloned().unwrap_or_default();
    let Some(search_data) = search_data.filter(|value| crate::core_utils::python_truthy(value))
    else {
        return Value::Object(result);
    };
    let rounds = search_data
        .get("rounds")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    let results = search_data
        .get("results")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    result.insert("searchRoundCount".to_string(), json!(rounds));
    result.insert("searchResultCount".to_string(), json!(results));
    Value::Object(result)
}

/// `_search_round_count`: the number of search rounds, or zero.
pub fn search_round_count(search_data: &Value) -> usize {
    search_data
        .get("rounds")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tools_are_sorted_and_deduplicated() {
        let names = vec![
            "search_files".to_string(),
            "create_document".to_string(),
            "search_files".to_string(),
        ];
        let result = diagnostics_with_tools(&json!({"existing": true}), 3, &names);
        assert_eq!(result["toolCallCount"], 3);
        assert_eq!(
            result["toolNames"],
            json!(["create_document", "search_files"])
        );
        // The base object's own keys survive, which is what `dict(diagnostics)` does.
        assert_eq!(result["existing"], true);
        // An empty turn still writes both keys.
        let result = diagnostics_with_tools(&json!({}), 0, &[]);
        assert_eq!(result["toolCallCount"], 0);
        assert_eq!(result["toolNames"], json!([]));
    }

    #[test]
    fn usage_writes_the_cache_counters_and_the_rate() {
        let result = diagnostics_with_usage(
            &json!({}),
            &json!({"prompt_cache_hit_tokens": 75, "prompt_cache_miss_tokens": 25}),
        );
        assert_eq!(result["cacheHitTokens"], 75);
        assert_eq!(result["cacheMissTokens"], 25);
        assert_eq!(result["cacheHitRate"], 75.0);
        // No cache tokens at all is a *float* zero.
        let result = diagnostics_with_usage(&json!({}), &json!({"prompt_tokens": 10}));
        assert_eq!(result["cacheHitTokens"], 0);
        assert_eq!(result["cacheMissTokens"], 0);
        assert_eq!(result["cacheHitRate"], 0.0);
        assert!(
            result["cacheHitRate"].is_f64(),
            "0.0 must not be an integer"
        );
        // The camelCase aliases are read the same way the merge reads them.
        let result = diagnostics_with_usage(
            &json!({}),
            &json!({"promptCacheHitTokens": 1, "promptCacheMissTokens": 3}),
        );
        assert_eq!(result["cacheHitRate"], 25.0);
    }

    #[test]
    fn the_rate_is_pythons_decimal_rounding() {
        // `round(1.05, 1)` is `1.1` — the stored double is *above* the decimal tie, so
        // multiplying by ten first (which gives exactly `10.5`) and rounding half-to-even
        // would wrongly return `1.0`. `round(2.675, 1)` is `2.7` for the same reason in
        // the other direction, and the `0.25`/`0.35` pair is a genuine half-to-even tie
        // resolved downward.
        assert_eq!(round_one_decimal(1.05), 1.1);
        assert_eq!(round_one_decimal(2.675), 2.7);
        assert_eq!(round_one_decimal(0.25), 0.2);
        assert_eq!(round_one_decimal(0.35), 0.3);
        assert_eq!(round_one_decimal(1.15), 1.1);
        assert_eq!(round_one_decimal(0.05), 0.1);
        assert_eq!(round_one_decimal(0.45), 0.5);
        assert_eq!(round_one_decimal(0.85), 0.8);
        assert_eq!(round_one_decimal(0.0), 0.0);
        assert_eq!(round_one_decimal(75.0), 75.0);
    }

    #[test]
    fn search_rounds_are_counted_or_zero() {
        assert_eq!(search_round_count(&json!({"rounds": [1, 2, 3]})), 3);
        assert_eq!(search_round_count(&json!({"rounds": []})), 0);
        assert_eq!(search_round_count(&json!({"rounds": "not a list"})), 0);
        assert_eq!(search_round_count(&json!({})), 0);
        assert_eq!(search_round_count(&Value::Null), 0);
    }

    #[test]
    fn a_falsy_search_adds_no_keys_at_all() {
        // `if search_data:` — absent, null and an empty object all add nothing.
        for search in [None, Some(json!(Value::Null)), Some(json!({}))] {
            let result = diagnostics_with_search(&json!({"base": true}), search.as_ref());
            assert_eq!(result, json!({"base": true}), "{search:?}");
        }
        // A search with rounds and results writes both counts, and a missing list is
        // zero rather than absent.
        let result = diagnostics_with_search(
            &json!({}),
            Some(&json!({"rounds": [1, 2], "results": [{"url": "x"}]})),
        );
        assert_eq!(result["searchRoundCount"], 2);
        assert_eq!(result["searchResultCount"], 1);
        let result = diagnostics_with_search(&json!({}), Some(&json!({"rounds": [1]})));
        assert_eq!(result["searchRoundCount"], 1);
        assert_eq!(result["searchResultCount"], 0);
    }
}
