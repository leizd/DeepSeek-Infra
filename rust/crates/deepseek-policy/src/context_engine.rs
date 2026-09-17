//! The token engine: estimation, budget planning, and trimming.
//!
//! Mirrors `gateway/context_engine.py` minus its identity half. `base_context_id`,
//! `build_context_diff` and `build_engine_diagnostics` are **not** here: the first needs
//! SHA-1, which this crate does not depend on and which is a decision rather than a
//! detail, and the other two are built on it. Everything that decides *how big a prompt
//! is* and *what to drop when it is too big* is here.
//!
//! The heuristics are deliberately tokenizer-free: DeepSeek's BPE packs CJK denser per
//! character than Latin text, so the two classes are weighted separately and the estimate
//! rounds **up** — the planner must err toward leaving headroom, never toward
//! under-counting a prompt that would actually overflow.
//!
//! One trap that is invisible in the numbers: `estimate_tools_tokens` measures a
//! serialized tool array, and `serde_json`'s map ordering differs from Python's insertion
//! order, so the *string* can differ. The estimate cannot: key order changes neither the
//! length nor the CJK count of the serialization. Anything that starts comparing that
//! string byte-for-byte would break here, so it is deliberately not exposed.

use serde_json::Value;

use crate::core_utils::python_truthy;
use crate::python_json::{dumps_default_separators, value_str};

// --- Heuristics -----------------------------------------------------------------------

/// CJK characters per token in the wire format.
const CJK_CHARS_PER_TOKEN: f64 = 1.6;
/// Latin characters per token.
const LATIN_CHARS_PER_TOKEN: f64 = 4.0;
/// Per-message structural overhead (role, delimiters).
const MESSAGE_OVERHEAD_TOKENS: i64 = 4;
/// Flat cost for an inline image part.
const IMAGE_TOKENS: i64 = 1_024;

/// Mirrors `_is_cjk`. The ranges are the oracle's, including Fullwidth forms — a
/// "simplification" to a single CJK block would change every estimate that contains
/// punctuation from a CJK keyboard.
pub fn is_cjk(character: char) -> bool {
    let code = character as u32;
    (0x4E00..=0x9FFF).contains(&code)      // CJK Unified Ideographs
        || (0x3400..=0x4DBF).contains(&code) // CJK Extension A
        || (0x3040..=0x30FF).contains(&code) // Hiragana + Katakana
        || (0xAC00..=0xD7A3).contains(&code) // Hangul syllables
        || (0xF900..=0xFAFF).contains(&code) // CJK Compatibility Ideographs
        || (0xFF00..=0xFFEF).contains(&code) // Fullwidth forms
}

/// Mirrors `estimate_tokens`.
///
/// Kept in `f64` on purpose: both sides must round the same IEEE double with `ceil`, and
/// a rational rewrite would disagree on values where `cjk / 1.6` is already inexact.
pub fn estimate_tokens(text: &str) -> i64 {
    if text.is_empty() {
        return 0;
    }
    let characters = text.chars().count() as f64;
    let cjk = text.chars().filter(|character| is_cjk(*character)).count() as f64;
    let other = characters - cjk;
    (cjk / CJK_CHARS_PER_TOKEN + other / LATIN_CHARS_PER_TOKEN).ceil() as i64
}

/// Mirrors `estimate_message_tokens`: structural overhead, text parts, images, and the
/// name plus arguments of every tool call.
pub fn estimate_message_tokens(message: &Value) -> i64 {
    let Some(object) = message.as_object() else {
        return 0;
    };
    let mut total = MESSAGE_OVERHEAD_TOKENS;
    match object.get("content") {
        Some(Value::String(text)) => total += estimate_tokens(text),
        Some(Value::Array(parts)) => {
            for part in parts {
                let Some(part) = part.as_object() else {
                    continue;
                };
                match part.get("type").and_then(Value::as_str) {
                    Some("text") => {
                        total += estimate_tokens(&text_or_empty(part.get("text")));
                    }
                    Some("image_url") => total += IMAGE_TOKENS,
                    _ => {}
                }
            }
        }
        _ => {}
    }
    if let Some(Value::Array(calls)) = object.get("tool_calls") {
        for call in calls {
            let Some(function) = call.get("function").and_then(Value::as_object) else {
                continue;
            };
            total += estimate_tokens(&text_or_empty(function.get("name")));
            total += estimate_tokens(&text_or_empty(function.get("arguments")));
        }
    }
    total
}

/// Mirrors `estimate_tools_tokens`. A non-list or an empty list costs nothing, and so does
/// a serialization failure — the oracle swallows `TypeError`/`ValueError` the same way.
pub fn estimate_tools_tokens(tools: Option<&Value>) -> i64 {
    match tools {
        Some(value @ Value::Array(items)) if !items.is_empty() => {
            estimate_tokens(&dumps_default_separators(value))
        }
        _ => 0,
    }
}

/// The four categories `estimate_body_breakdown` reports.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct BodyBreakdown {
    pub system: i64,
    pub tools: i64,
    pub history: i64,
    pub dynamic: i64,
}

impl BodyBreakdown {
    pub fn total(&self) -> i64 {
        self.system + self.tools + self.history + self.dynamic
    }

    pub fn to_value(&self) -> Value {
        serde_json::json!({
            "system": self.system,
            "tools": self.tools,
            "history": self.history,
            "dynamic": self.dynamic,
        })
    }
}

/// Mirrors `estimate_body_breakdown`.
///
/// The trailing-system rule is `index == last_index and last_index > 0`, so a body whose
/// *only* message is a system message counts as `system`, not as `dynamic`.
pub fn estimate_body_breakdown(body: &Value) -> BodyBreakdown {
    let empty: Vec<Value> = Vec::new();
    let messages = match body.get("messages") {
        Some(Value::Array(items)) => items,
        _ => &empty,
    };
    let last_index = messages.len().saturating_sub(1);
    let mut breakdown = BodyBreakdown::default();
    for (index, message) in messages.iter().enumerate() {
        if !message.is_object() {
            continue;
        }
        let tokens = estimate_message_tokens(message);
        if message.get("role") == Some(&Value::String("system".to_string())) {
            if index == last_index && last_index > 0 {
                breakdown.dynamic += tokens;
            } else {
                breakdown.system += tokens;
            }
        } else {
            breakdown.history += tokens;
        }
    }
    breakdown.tools = estimate_tools_tokens(body.get("tools"));
    breakdown
}

// --- Settings -------------------------------------------------------------------------

/// Mirrors `ContextEngineSettings`, defaults included. The window table is spelled out
/// because it is a table: both current models happen to share 131 072, and collapsing them
/// into "the default" would hide which models are actually pinned.
#[derive(Debug, Clone, PartialEq)]
pub struct ContextEngineSettings {
    pub enabled: bool,
    pub token_aware_trim: bool,
    pub reserve_output_tokens: i64,
    pub safety_margin_ratio: f64,
    pub compress_threshold_pct: f64,
    pub default_context_window: i64,
    pub min_keep_messages: usize,
    /// Mirrors the oracle's `model_context_windows` mapping. A field rather than a
    /// constant because the oracle reads it from settings, and a port that cannot be
    /// configured the same way would quietly disagree with any deployment that overrides
    /// it through the environment.
    pub model_context_windows: Vec<(String, i64)>,
}

impl Default for ContextEngineSettings {
    fn default() -> Self {
        Self {
            enabled: true,
            token_aware_trim: true,
            reserve_output_tokens: 8_192,
            safety_margin_ratio: 0.05,
            compress_threshold_pct: 75.0,
            default_context_window: 65_536,
            min_keep_messages: 2,
            model_context_windows: vec![
                ("deepseek-v4-pro".to_string(), 131_072),
                ("deepseek-v4-flash".to_string(), 131_072),
            ],
        }
    }
}

impl ContextEngineSettings {
    /// The window for one model name, mirroring the oracle's dict lookup: the first
    /// positive entry wins, and anything else falls back to the default window.
    pub fn window_for(&self, name: &str) -> Option<i64> {
        self.model_context_windows
            .iter()
            .find(|(candidate, window)| candidate == name && *window > 0)
            .map(|(_, window)| *window)
    }
}

pub fn context_engine_enabled(settings: &ContextEngineSettings) -> bool {
    settings.enabled
}

/// Mirrors `context_window_for_model`: a positive entry from the table, else the default.
pub fn context_window_for_model(model: Option<&str>, settings: &ContextEngineSettings) -> i64 {
    let name = model.unwrap_or("").trim();
    settings
        .window_for(name)
        .unwrap_or(settings.default_context_window)
}

/// Mirrors `available_input_tokens`. `int(window * max(0.0, ratio))` truncates toward zero,
/// which is what `as i64` does here.
pub fn available_input_tokens(model: Option<&str>, settings: &ContextEngineSettings) -> i64 {
    let window = context_window_for_model(model, settings);
    let margin = (window as f64 * settings.safety_margin_ratio.max(0.0)) as i64;
    (window - settings.reserve_output_tokens - margin).max(0)
}

// --- Planning -------------------------------------------------------------------------

/// Mirrors `TokenBudgetPlan`.
#[derive(Debug, Clone, PartialEq)]
pub struct TokenBudgetPlan {
    pub model: String,
    pub context_window: i64,
    pub reserved_output_tokens: i64,
    pub available_input_tokens: i64,
    pub estimated_prompt_tokens: i64,
    pub breakdown: BodyBreakdown,
    pub headroom_tokens: i64,
    pub utilization_pct: f64,
    pub within_budget: bool,
    pub recommendation: &'static str,
}

impl TokenBudgetPlan {
    /// Mirrors `TokenBudgetPlan.to_dict`. As elsewhere, the key order is the serializer's
    /// business: `json!` sorts, and diagnostics byte-parity is a problem for the writer of
    /// that serializer rather than something to fake here.
    pub fn to_value(&self) -> Value {
        serde_json::json!({
            "model": self.model,
            "contextWindow": self.context_window,
            "reservedOutputTokens": self.reserved_output_tokens,
            "availableInputTokens": self.available_input_tokens,
            "estimatedPromptTokens": self.estimated_prompt_tokens,
            "breakdown": self.breakdown.to_value(),
            "headroomTokens": self.headroom_tokens,
            "utilizationPct": self.utilization_pct,
            "withinBudget": self.within_budget,
            "recommendation": self.recommendation,
        })
    }
}

/// Mirrors `round(x, 1)`.
///
/// Rust's `{:.1}` and CPython's `round` both perform correct rounding of the binary value
/// to one decimal with ties to even, so formatting and parsing back is the closest
/// available mirror; a `* 10 → round → / 10` rewrite is not, because it re-rounds an
/// already-rounded value.
pub fn round_one_decimal(value: f64) -> f64 {
    format!("{value:.1}").parse().unwrap_or(value)
}

/// Mirrors `plan_token_budget`.
pub fn plan_token_budget(
    body: &Value,
    model: Option<&str>,
    settings: &ContextEngineSettings,
) -> TokenBudgetPlan {
    // `str(model or body.get("model") or "")`: the argument wins when it is *truthy*, so an
    // empty string falls through to the body's model while whitespace does not.
    let raw_model = match model {
        Some(found) if !found.is_empty() => found.to_string(),
        _ => body
            .get("model")
            .filter(|value| python_truthy(value))
            .map(value_str)
            .unwrap_or_default(),
    };
    let resolved_model = raw_model.trim().to_string();
    let breakdown = estimate_body_breakdown(body);
    let prompt_tokens = breakdown.total();
    let window = context_window_for_model(Some(&resolved_model), settings);
    let available = available_input_tokens(Some(&resolved_model), settings);
    let headroom = available - prompt_tokens;
    let utilization = if available > 0 {
        round_one_decimal(prompt_tokens as f64 / available as f64 * 100.0)
    } else {
        100.0
    };
    let within_budget = prompt_tokens <= available;
    let recommendation = if !within_budget {
        "trim"
    } else if utilization >= settings.compress_threshold_pct {
        "compress"
    } else {
        "ok"
    };
    TokenBudgetPlan {
        model: resolved_model,
        context_window: window,
        reserved_output_tokens: settings.reserve_output_tokens,
        available_input_tokens: available,
        estimated_prompt_tokens: prompt_tokens,
        breakdown,
        headroom_tokens: headroom,
        utilization_pct: utilization,
        within_budget,
        recommendation,
    }
}

// --- Trimming -------------------------------------------------------------------------

/// Mirrors `_split_front_tail`: leading system, variable middle, trailing system.
fn split_front_tail(messages: &[Value]) -> (Vec<Value>, Vec<Value>, Vec<Value>) {
    let mut front: Vec<Value> = Vec::new();
    let mut tail: Vec<Value> = Vec::new();
    let mut start = 0usize;
    let mut end = messages.len();
    let is_system =
        |message: &Value| message.get("role") == Some(&Value::String("system".to_string()));
    if messages.first().map(is_system).unwrap_or(false) {
        front.push(messages[0].clone());
        start = 1;
    }
    if end > start && messages.last().map(is_system).unwrap_or(false) {
        tail.push(messages[end - 1].clone());
        end -= 1;
    }
    (front, messages[start..end].to_vec(), tail)
}

/// Mirrors `token_trim`.
///
/// Layered on top of the message-count window: the caller passes already-count-capped
/// messages and this drops *more* only when the token estimate still overflows. It never
/// drops the leading or trailing system message, and always keeps at least
/// `min_keep_messages` of the most recent variable messages — so for a normal turn it is a
/// no-op returning `(messages, 0)`.
pub fn token_trim(
    messages: &[Value],
    model: Option<&str>,
    fixed_overhead_tokens: i64,
    settings: &ContextEngineSettings,
) -> (Vec<Value>, usize) {
    if messages.is_empty() {
        return (messages.to_vec(), 0);
    }
    let available = available_input_tokens(model, settings);
    if available <= 0 {
        return (messages.to_vec(), 0);
    }
    let (front, variable, tail) = split_front_tail(messages);
    let mut total: i64 = fixed_overhead_tokens.max(0);
    for message in front.iter().chain(tail.iter()) {
        total += estimate_message_tokens(message);
    }
    let variable_tokens: Vec<i64> = variable.iter().map(estimate_message_tokens).collect();
    total += variable_tokens.iter().sum::<i64>();

    let min_keep = settings.min_keep_messages.max(1);
    let mut drop = 0usize;
    while total > available && (variable.len() - drop) > min_keep {
        total -= variable_tokens[drop];
        drop += 1;
    }
    if drop == 0 {
        return (messages.to_vec(), 0);
    }
    let mut result = front;
    result.extend(variable[drop..].iter().cloned());
    result.extend(tail);
    (result, drop)
}

/// `str(value or "")` without the strip.
fn text_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn settings() -> ContextEngineSettings {
        ContextEngineSettings::default()
    }

    #[test]
    fn the_estimate_weights_cjk_denser_and_rounds_up() {
        assert_eq!(estimate_tokens(""), 0);
        // 400 Latin characters at 4.0 per token.
        assert_eq!(estimate_tokens(&"a".repeat(400)), 100);
        // 7 CJK characters at 1.6 per token is 4.375, and the estimate must not round down.
        assert_eq!(estimate_tokens(&"中".repeat(7)), 5);
        // Fullwidth punctuation is inside the oracle's ranges, so a CJK keyboard's
        // punctuation is not miscounted as Latin.
        assert_eq!(estimate_tokens("，"), 1);
        assert_eq!(estimate_tokens("a"), 1);
    }

    #[test]
    fn a_message_carries_its_structural_overhead_even_with_no_text() {
        // Only a non-object message costs nothing: an empty *object* still pays the
        // structural overhead, which is what the oracle does too.
        assert_eq!(estimate_message_tokens(&json!("not-a-message")), 0);
        assert_eq!(estimate_message_tokens(&json!({})), MESSAGE_OVERHEAD_TOKENS);
        assert_eq!(
            estimate_message_tokens(&json!({"content": ""})),
            MESSAGE_OVERHEAD_TOKENS
        );
        assert_eq!(
            estimate_message_tokens(&json!({"content": [{"type": "image_url"}]})),
            MESSAGE_OVERHEAD_TOKENS + IMAGE_TOKENS
        );
        // Text parts are counted, images are flat, and unknown part types are ignored.
        assert_eq!(
            estimate_message_tokens(&json!({"content": [{"type": "text", "text": "a"}]})),
            MESSAGE_OVERHEAD_TOKENS + 1
        );
        assert_eq!(
            estimate_message_tokens(&json!({"content": [{"type": "audio"}]})),
            MESSAGE_OVERHEAD_TOKENS
        );
    }

    #[test]
    fn tool_calls_cost_their_name_and_arguments() {
        let message = json!({"tool_calls": [
            {"function": {"name": "web_search", "arguments": "{\"q\": \"中\"}"}},
            "not-a-dict",
        ]});
        let expected = MESSAGE_OVERHEAD_TOKENS
            + estimate_tokens("web_search")
            + estimate_tokens("{\"q\": \"中\"}");
        assert_eq!(estimate_message_tokens(&message), expected);
    }

    #[test]
    fn a_tool_array_costs_nothing_when_it_is_absent_empty_or_not_a_list() {
        assert_eq!(estimate_tools_tokens(None), 0);
        assert_eq!(estimate_tools_tokens(Some(&json!([]))), 0);
        assert_eq!(estimate_tools_tokens(Some(&json!("nope"))), 0);
        assert!(estimate_tools_tokens(Some(&json!([{"function": {"name": "t"}}]))) > 0);
    }

    #[test]
    fn only_a_trailing_system_message_after_another_message_is_dynamic() {
        // A body whose only message is a system message counts as `system`: the oracle's
        // rule is `index == last_index and last_index > 0`.
        let single =
            estimate_body_breakdown(&json!({"messages": [{"role": "system", "content": "a"}]}));
        assert_eq!((single.system, single.dynamic), (5, 0));

        let pair = estimate_body_breakdown(&json!({"messages": [
            {"role": "system", "content": "a"},
            {"role": "system", "content": "c"},
        ]}));
        assert_eq!((pair.system, pair.dynamic), (5, 5));

        // Middle system messages stay `system`, and non-system roles are history.
        let middle = estimate_body_breakdown(&json!({"messages": [
            {"role": "system", "content": "a"},
            {"role": "user", "content": "b"},
            {"role": "system", "content": "c"},
        ]}));
        assert_eq!((middle.system, middle.history, middle.dynamic), (5, 5, 5));
    }

    #[test]
    fn the_window_lookup_strips_and_falls_back() {
        let settings = settings();
        assert_eq!(
            context_window_for_model(Some(" deepseek-v4-pro "), &settings),
            131_072
        );
        assert_eq!(
            context_window_for_model(Some("unknown"), &settings),
            settings.default_context_window
        );
        assert_eq!(
            context_window_for_model(None, &settings),
            settings.default_context_window
        );
        // A non-positive window in the table is ignored rather than returned.
        let odd = ContextEngineSettings {
            model_context_windows: vec![("m".to_string(), 0)],
            default_context_window: 7,
            ..settings
        };
        assert_eq!(context_window_for_model(Some("m"), &odd), 7);
    }

    #[test]
    fn the_reserve_and_margin_come_off_the_window_and_never_go_negative() {
        // 65 536 * 0.05 truncates to 3 276, so the available budget is 54 068.
        assert_eq!(available_input_tokens(None, &settings()), 54_068);

        let starved = ContextEngineSettings {
            reserve_output_tokens: 100_000,
            ..settings()
        };
        assert_eq!(available_input_tokens(None, &starved), 0);
    }

    #[test]
    fn the_recommendation_ladder_runs_trim_then_compress_then_ok() {
        let settings = settings();
        let small = plan_token_budget(
            &json!({"messages": [{"role": "user", "content": "a"}]}),
            None,
            &settings,
        );
        assert_eq!(small.recommendation, "ok");
        assert!(small.within_budget);

        // With a 1 000-token window and no reserve, a 3 000-character prompt is 754
        // tokens: over the 75% compress line but still inside the budget.
        let narrow = ContextEngineSettings {
            reserve_output_tokens: 0,
            safety_margin_ratio: 0.0,
            default_context_window: 1_000,
            ..settings.clone()
        };
        let compressible = plan_token_budget(
            &json!({"messages": [{"role": "user", "content": "a".repeat(3_000)}]}),
            None,
            &narrow,
        );
        assert_eq!(compressible.recommendation, "compress");
        assert!(compressible.within_budget);

        // A window too small for its reserve leaves no budget, so the plan trims.
        let tight = ContextEngineSettings {
            reserve_output_tokens: 0,
            safety_margin_ratio: 0.0,
            default_context_window: 10,
            ..settings
        };
        let over = plan_token_budget(
            &json!({"messages": [{"role": "user", "content": "a".repeat(400)}]}),
            None,
            &tight,
        );
        assert_eq!(over.recommendation, "trim");
        assert!(!over.within_budget);
        assert!(over.headroom_tokens < 0);
    }

    #[test]
    fn utilization_is_rounded_to_one_decimal() {
        // 100 / 3 = 33.333... and the oracle keeps one decimal.
        assert_eq!(round_one_decimal(100.0 / 3.0), 33.3);
        // Ties go to even on both sides, and 2.25 is exactly representable.
        assert_eq!(round_one_decimal(2.25), 2.2);
        assert_eq!(round_one_decimal(2.35), 2.4);
    }

    #[test]
    fn trimming_keeps_both_system_anchors_and_drops_the_oldest_history() {
        let small = ContextEngineSettings {
            reserve_output_tokens: 0,
            safety_margin_ratio: 0.0,
            default_context_window: 40,
            ..settings()
        };
        let messages = vec![
            json!({"role": "system", "content": "stable prefix"}),
            json!({"role": "user", "content": "中".repeat(20)}),
            json!({"role": "assistant", "content": "a".repeat(40)}),
            json!({"role": "user", "content": "b".repeat(40)}),
            json!({"role": "system", "content": "dynamic tail"}),
        ];
        let (trimmed, dropped) = token_trim(&messages, None, 0, &small);
        assert_eq!(dropped, 1);
        assert_eq!(trimmed.first().unwrap()["content"], json!("stable prefix"));
        assert_eq!(trimmed.last().unwrap()["content"], json!("dynamic tail"));
        assert_eq!(trimmed.len(), messages.len() - 1);
    }

    #[test]
    fn the_minimum_keep_floor_stops_the_drop_early() {
        let guarded = ContextEngineSettings {
            reserve_output_tokens: 0,
            safety_margin_ratio: 0.0,
            default_context_window: 40,
            min_keep_messages: 3,
            ..settings()
        };
        let messages = vec![
            json!({"role": "system", "content": "stable prefix"}),
            json!({"role": "user", "content": "中".repeat(20)}),
            json!({"role": "assistant", "content": "a".repeat(40)}),
            json!({"role": "user", "content": "b".repeat(40)}),
            json!({"role": "system", "content": "dynamic tail"}),
        ];
        // Three variable messages, so the floor of 3 makes this a no-op even though the
        // estimate is over budget.
        assert_eq!(
            token_trim(&messages, None, 0, &guarded),
            (messages.clone(), 0)
        );
    }

    #[test]
    fn a_starved_budget_trims_nothing_at_all() {
        // `available <= 0` returns early: dropping messages could not help, and the oracle
        // leaves the caller's list untouched rather than emptying it.
        let starved = ContextEngineSettings {
            reserve_output_tokens: 100_000,
            ..settings()
        };
        let messages = vec![json!({"role": "user", "content": "a".repeat(4_000)})];
        assert_eq!(token_trim(&messages, None, 0, &starved), (messages, 0));
        assert_eq!(token_trim(&[], None, 0, &settings()), (Vec::new(), 0));
    }
}
