//! Model routing and cascade inference — mirrors `gateway/model_router.py`.
//!
//! The router only *auto-selects* the cloud model tier (flash vs pro) when a request opts in
//! (`autoRoute` / `model="auto"`); an explicit model choice is always respected, and the
//! image override applies either way as a safety net. Cascade runs a cheap draft, scores it
//! with a heuristic gate, and escalates only when the draft is insufficient.
//!
//! Two pairs of lookalikes live here and must not be collapsed:
//!
//! - `is_auto_request` reads `payload["model"]` for the literal `"auto"` and then
//!   `autoRoute is True`; the second is an **identity** check, so `autoRoute: 1` opts out.
//! - capability uses [`has_image_attachment`] (an `imageData` field on some message's
//!   attachments) while the request-shaping layer uses `has_image_content` (an `image_url`
//!   part in an assembled message). Same question in prose, different evidence in the body.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::Value;

use crate::context_engine::estimate_tokens;
use crate::core_utils::{latest_user_query, normalize_model_name};
use crate::edge_inference::{
    artifact_query_regex, complex_query_regex, has_image_attachment, simple_task_regex,
};
use crate::python_json::value_str;

/// Strong uncertainty phrases (not bare `可能`/`也许`, which are common in good answers) that
/// signal a draft worth escalating in cascade mode. The oracle's comment, kept because the
/// distinction is the reason the list is short.
pub const UNCERTAINTY_MARKERS: [&str; 10] = [
    "我不确定",
    "无法确定",
    "不太确定",
    "可能不准确",
    "仅供参考",
    "i'm not sure",
    "i am not sure",
    "i am not certain",
    "not entirely sure",
    "i'm uncertain",
];

pub const REFUSAL_MARKERS: [&str; 8] = [
    "无法回答",
    "抱歉，我无法",
    "抱歉，我不能",
    "无法提供",
    "i cannot help",
    "i can't help",
    "i'm unable to",
    "as an ai language model",
];

/// Mirrors `_CITATION_RE`, without the `IGNORECASE` flag.
pub const CITATION_PATTERN: &str = r"\[\^[WF]\d";

/// Every knob `model_router.py` reads from settings, at the oracle's defaults.
#[derive(Debug, Clone, PartialEq)]
pub struct ModelRouterSettings {
    pub enabled: bool,
    pub cascade_enabled: bool,
    pub judge_enabled: bool,
    pub judge_model: String,
    pub judge_threshold: f64,
    pub draft_model: String,
    pub refine_model: String,
    pub cascade_min_chars: i64,
    pub cost_budget_tokens: i64,
    pub default_model: String,
    pub supported_models: Vec<String>,
    pub model_aliases: Vec<(String, String)>,
}

impl Default for ModelRouterSettings {
    fn default() -> Self {
        let aliases = [
            ("deepseek-v4-pro", "deepseek-v4-pro"),
            ("deepseekv4pro", "deepseek-v4-pro"),
            ("v4pro", "deepseek-v4-pro"),
            ("expert", "deepseek-v4-pro"),
            ("deepseek-v4-flash", "deepseek-v4-flash"),
            ("deepseekv4flash", "deepseek-v4-flash"),
            ("v4flash", "deepseek-v4-flash"),
            ("flash", "deepseek-v4-flash"),
            ("fast", "deepseek-v4-flash"),
        ];
        Self {
            enabled: true,
            cascade_enabled: true,
            judge_enabled: false,
            judge_model: "deepseek-v4-flash".to_string(),
            judge_threshold: 0.6,
            draft_model: "deepseek-v4-flash".to_string(),
            refine_model: "deepseek-v4-pro".to_string(),
            cascade_min_chars: 80,
            cost_budget_tokens: 0,
            default_model: "deepseek-v4-pro".to_string(),
            supported_models: vec![
                "deepseek-v4-pro".to_string(),
                "deepseek-v4-flash".to_string(),
            ],
            model_aliases: aliases
                .iter()
                .map(|(from, to)| (from.to_string(), to.to_string()))
                .collect(),
        }
    }
}

/// `"ollama"` when the draft model is served locally, else `"deepseek"`.
fn draft_provider(draft_model: &str) -> &'static str {
    if draft_model.starts_with("ollama/") {
        "ollama"
    } else {
        "deepseek"
    }
}

/// Mirrors `router_status`: the block `/api/config` serves.
pub fn router_status(settings: &ModelRouterSettings) -> Value {
    serde_json::json!({
        "enabled": settings.enabled,
        "cascadeEnabled": settings.enabled && settings.cascade_enabled,
        "judgeEnabled": settings.judge_enabled,
        "draftModel": settings.draft_model,
        "draftProvider": draft_provider(&settings.draft_model),
        "refineModel": settings.refine_model,
        "judgeModel": settings.judge_model,
        "costBudgetTokens": settings.cost_budget_tokens,
    })
}

/// Mirrors `is_auto_request`. Disabled routing means nothing auto-routes, whatever the body
/// asks for.
pub fn is_auto_request(payload: &Value, settings: &ModelRouterSettings) -> bool {
    if !settings.enabled {
        return false;
    }
    if text_or_empty(payload.get("model")).trim().to_lowercase() == "auto" {
        return true;
    }
    payload.get("autoRoute") == Some(&Value::Bool(true))
}

/// Mirrors `cascade_requested`: the body must ask *and* both switches must be on.
pub fn cascade_requested(payload: &Value, settings: &ModelRouterSettings) -> bool {
    settings.enabled
        && settings.cascade_enabled
        && payload.get("cascade") == Some(&Value::Bool(true))
}

/// Mirrors `query_complexity`.
///
/// The order of the tests is the contract: a short query that matches the complex pattern is
/// complex, and the simple pattern only counts while the text is short enough.
pub fn query_complexity(query: &str) -> &'static str {
    let text = query.trim();
    if text.is_empty() {
        return "neutral";
    }
    if complex_query_regex().is_match(text) || artifact_query_regex().is_match(text) {
        return "complex";
    }
    let characters = text.chars().count();
    if characters > 1200 {
        return "complex";
    }
    if simple_task_regex().is_match(text) && characters <= 400 {
        return "simple";
    }
    if characters <= 120 {
        return "simple";
    }
    "neutral"
}

/// Mirrors `_estimate_payload_tokens`: the string contents of the payload's messages, joined
/// by newlines and **cut to 200 000 characters** before estimating.
pub fn estimate_payload_tokens(payload: &Value) -> i64 {
    let empty: Vec<Value> = Vec::new();
    let messages = match payload.get("messages") {
        Some(Value::Array(items)) => items,
        _ => &empty,
    };
    let parts: Vec<String> = messages
        .iter()
        .filter_map(|message| match message.get("content") {
            Some(Value::String(text)) => Some(text.clone()),
            _ => None,
        })
        .collect();
    let joined: String = parts.join("\n");
    let capped: String = joined.chars().take(200_000).collect();
    estimate_tokens(&capped)
}

/// Mirrors `RouteDecision`.
#[derive(Debug, Clone, PartialEq)]
pub struct RouteDecision {
    pub model: String,
    pub tier: String,
    pub auto: bool,
    pub capability: &'static str,
    pub fallback_model: String,
    pub estimated_prompt_tokens: i64,
    pub reasons: Vec<(String, String)>,
}

impl RouteDecision {
    /// Mirrors `RouteDecision.to_dict`; each reason carries exactly the `router`/`decision`
    /// pair the oracle builds.
    pub fn to_value(&self) -> Value {
        let reasons: Vec<Value> = self
            .reasons
            .iter()
            .map(|(router, decision)| serde_json::json!({"router": router, "decision": decision}))
            .collect();
        serde_json::json!({
            "model": self.model,
            "tier": self.tier,
            "auto": self.auto,
            "capability": self.capability,
            "fallbackModel": self.fallback_model,
            "estimatedPromptTokens": self.estimated_prompt_tokens,
            "reasons": reasons,
        })
    }
}

/// Mirrors `route_request`.
pub fn route_request(
    payload: &Value,
    budget_used: i64,
    settings: &ModelRouterSettings,
) -> RouteDecision {
    let draft = settings.draft_model.as_str();
    let refine = settings.refine_model.as_str();
    let query = latest_user_query(payload);
    let image = has_image_attachment(payload);
    let capability = if image { "vision" } else { "text" };
    let prompt_tokens = estimate_payload_tokens(payload);
    let mut reasons: Vec<(String, String)> = Vec::new();

    let model = if !is_auto_request(payload, settings) {
        let mut base = normalize_model_name(payload.get("model"), &settings.model_aliases);
        if base.is_empty() {
            base = settings.default_model.clone();
        }
        if !settings.supported_models.contains(&base) {
            base = settings.default_model.clone();
        }
        reasons.push(("explicit".to_string(), base.clone()));
        if image && base != refine {
            base = refine.to_string();
            reasons.push(("capability".to_string(), format!("vision->{refine}")));
        }
        base
    } else {
        let complexity = query_complexity(&query);
        if image {
            reasons.push(("capability".to_string(), format!("vision->{refine}")));
            refine.to_string()
        } else if complexity == "complex" {
            reasons.push(("capability".to_string(), format!("complex->{refine}")));
            refine.to_string()
        } else if settings.cost_budget_tokens > 0
            && (budget_used + prompt_tokens) > settings.cost_budget_tokens
        {
            reasons.push(("cost".to_string(), format!("over_budget->{draft}")));
            draft.to_string()
        } else if complexity == "simple" {
            reasons.push(("latency".to_string(), format!("simple->{draft}")));
            draft.to_string()
        } else {
            reasons.push(("default".to_string(), settings.default_model.clone()));
            settings.default_model.clone()
        }
    };

    let fallback = if model == refine {
        draft.to_string()
    } else {
        refine.to_string()
    };
    // The tier falls back to the model name itself when the model is neither the draft nor
    // the refine model — so a custom explicit model reports as its own tier.
    let tier = if model == draft {
        "fast".to_string()
    } else if model == refine {
        "expert".to_string()
    } else {
        model.clone()
    };
    RouteDecision {
        model,
        tier,
        auto: is_auto_request(payload, settings),
        capability,
        fallback_model: fallback,
        estimated_prompt_tokens: prompt_tokens,
        reasons,
    }
}

/// Mirrors `CascadePlan`.
#[derive(Debug, Clone, PartialEq)]
pub struct CascadePlan {
    pub enabled: bool,
    pub draft_model: String,
    pub refine_model: String,
    pub draft_provider: &'static str,
    pub judge: bool,
    pub judge_model: String,
    pub judge_threshold: f64,
    pub min_chars: i64,
}

impl CascadePlan {
    pub fn to_value(&self) -> Value {
        serde_json::json!({
            "enabled": self.enabled,
            "draftModel": self.draft_model,
            "refineModel": self.refine_model,
            "draftProvider": self.draft_provider,
            "judge": self.judge,
            "judgeModel": self.judge_model,
            "judgeThreshold": self.judge_threshold,
            "minChars": self.min_chars,
        })
    }
}

/// Mirrors `cascade_plan`: disabled for vision and agent turns, which need the strong model
/// directly rather than a draft to escalate from.
pub fn cascade_plan(payload: &Value, settings: &ModelRouterSettings) -> CascadePlan {
    let enabled = cascade_requested(payload, settings)
        && payload.get("agentMode") != Some(&Value::Bool(true))
        && !has_image_attachment(payload);
    let judge =
        enabled && (settings.judge_enabled || payload.get("judge") == Some(&Value::Bool(true)));
    CascadePlan {
        enabled,
        draft_model: settings.draft_model.clone(),
        refine_model: settings.refine_model.clone(),
        draft_provider: draft_provider(&settings.draft_model),
        judge,
        judge_model: settings.judge_model.clone(),
        judge_threshold: settings.judge_threshold,
        min_chars: settings.cascade_min_chars,
    }
}

/// Mirrors `GateResult`.
#[derive(Debug, Clone, PartialEq)]
pub struct GateResult {
    pub passed: bool,
    pub score: f64,
    pub reasons: Vec<&'static str>,
}

impl GateResult {
    pub fn to_value(&self) -> Value {
        serde_json::json!({"passed": self.passed, "score": self.score, "reasons": self.reasons})
    }
}

/// Mirrors `gateway/model_router.py`'s `round(x, 3)`: correct rounding of the binary value to
/// three decimals with ties to even, which `format!("{:.3}")` reproduces and a
/// multiply-round-divide rewrite would not.
fn round_three_decimals(value: f64) -> f64 {
    format!("{value:.3}").parse().unwrap_or(value)
}

/// Mirrors `quality_gate`.
///
/// Fails — and therefore escalates — on an empty or too-short answer, a refusal, two or more
/// uncertainty markers, or a missing citation when the turn needed sources. The score is one
/// minus 0.34 per reason, floored at zero.
pub fn quality_gate(content: &str, min_chars: i64, require_citations: bool) -> GateResult {
    let text = content.trim();
    if text.is_empty() {
        return GateResult {
            passed: false,
            score: 0.0,
            reasons: vec!["empty"],
        };
    }
    let mut reasons: Vec<&'static str> = Vec::new();
    if (text.chars().count() as i64) < min_chars.max(1) {
        reasons.push("too_short");
    }
    let lowered = text.to_lowercase();
    if REFUSAL_MARKERS
        .iter()
        .any(|marker| lowered.contains(&marker.to_lowercase()))
    {
        reasons.push("refusal");
    }
    let uncertain = UNCERTAINTY_MARKERS
        .iter()
        .filter(|marker| lowered.contains(&marker.to_lowercase()))
        .count();
    if uncertain >= 2 {
        reasons.push("uncertain");
    }
    if require_citations && !citation_regex().is_match(text) {
        reasons.push("missing_citation");
    }
    GateResult {
        passed: reasons.is_empty(),
        score: round_three_decimals((1.0 - 0.34 * reasons.len() as f64).max(0.0)),
        reasons,
    }
}

/// The oracle compiles `_CITATION_RE` once with `IGNORECASE`; the inline flag is the Rust
/// spelling of the same thing.
fn citation_regex() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| Regex::new(&format!("(?i){CITATION_PATTERN}")).expect("static pattern"))
}

/// `str(value or "")` without the strip.
fn text_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(found) if crate::core_utils::python_truthy(found) => value_str(found),
        _ => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn settings() -> ModelRouterSettings {
        ModelRouterSettings::default()
    }

    #[test]
    fn complexity_tests_run_in_the_oracles_order() {
        assert_eq!(query_complexity(""), "neutral");
        assert_eq!(query_complexity("   "), "neutral");
        // The complex pattern beats a short length.
        assert_eq!(query_complexity("帮我写代码"), "complex");
        assert_eq!(query_complexity("做一份 PPT"), "complex");
        assert_eq!(query_complexity(&"a".repeat(1300)), "complex");
        // The simple pattern only counts while the text fits in 400 characters, so a long
        // "explain…" request falls through to neutral rather than reading as simple.
        assert_eq!(query_complexity("解释"), "simple");
        assert_eq!(
            query_complexity(&format!("解释{}", "啊".repeat(500))),
            "neutral"
        );
        // Short and unmatched is still simple; longer and unmatched is neutral.
        assert_eq!(query_complexity(&"啊".repeat(100)), "simple");
        assert_eq!(query_complexity(&"啊".repeat(200)), "neutral");
    }

    #[test]
    fn auto_routing_is_an_identity_check_on_autroute_and_a_case_fold_on_the_model() {
        let settings = settings();
        assert!(is_auto_request(&json!({"model": "auto"}), &settings));
        assert!(is_auto_request(&json!({"model": " AUTO "}), &settings));
        assert!(is_auto_request(&json!({"autoRoute": true}), &settings));
        // `autoRoute is True`: a truthy `1` or `"true"` does not opt in.
        assert!(!is_auto_request(&json!({"autoRoute": 1}), &settings));
        assert!(!is_auto_request(&json!({"autoRoute": "true"}), &settings));
        // Disabled routing means nothing auto-routes, whatever the body asks for.
        let off = ModelRouterSettings {
            enabled: false,
            ..settings
        };
        assert!(!is_auto_request(&json!({"model": "auto"}), &off));
        assert!(!is_auto_request(&json!({"autoRoute": true}), &off));
    }

    #[test]
    fn an_explicit_model_is_normalised_then_checked_against_the_supported_list() {
        let settings = settings();
        let flash = route_request(&json!({"model": "flash"}), 0, &settings);
        assert_eq!(flash.model, "deepseek-v4-flash");
        assert_eq!(flash.tier, "fast");
        assert_eq!(
            flash.reasons,
            vec![("explicit".to_string(), "deepseek-v4-flash".to_string())]
        );

        // An unknown name falls back to the default rather than being passed through.
        let unknown = route_request(&json!({"model": "unknown-model"}), 0, &settings);
        assert_eq!(unknown.model, "deepseek-v4-pro");
        assert_eq!(unknown.tier, "expert");

        // An absent model does too.
        let absent = route_request(&json!({}), 0, &settings);
        assert_eq!(absent.model, "deepseek-v4-pro");
        assert!(!absent.auto);
    }

    #[test]
    fn vision_overrides_an_explicit_non_refine_choice() {
        let settings = settings();
        let payload = json!({
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": "x", "attachments": [
                {"imageData": "data:image/png;base64,AA"},
            ]}],
        });
        let decision = route_request(&payload, 0, &settings);
        assert_eq!(decision.capability, "vision");
        assert_eq!(decision.model, "deepseek-v4-pro");
        assert_eq!(
            decision.reasons[1],
            (
                "capability".to_string(),
                "vision->deepseek-v4-pro".to_string()
            )
        );

        // The image evidence is the *attachment*, not a content part.
        let parts = json!({
            "model": "deepseek-v4-flash",
            "messages": [{"role": "user", "content": [{"type": "image_url"}]}],
        });
        assert_eq!(
            route_request(&parts, 0, &settings).capability,
            "text",
            "content parts are has_image_content's business, not this one's"
        );
    }

    #[test]
    fn auto_routing_follows_complexity_then_the_soft_cost_cap() {
        let settings = settings();
        let simple = route_request(
            &json!({"model": "auto", "messages": [{"role": "user", "content": "你好"}]}),
            0,
            &settings,
        );
        assert!(simple.auto);
        assert_eq!(simple.model, "deepseek-v4-flash");
        assert_eq!(simple.tier, "fast");

        let complex = route_request(
            &json!({"model": "auto", "messages": [{"role": "user", "content": "帮我写代码"}]}),
            0,
            &settings,
        );
        assert_eq!(complex.model, "deepseek-v4-pro");
        assert_eq!(complex.fallback_model, "deepseek-v4-flash");

        // A zero budget disables the cost arm; a positive one prefers the draft when the
        // estimate would exceed it.
        let capped = ModelRouterSettings {
            cost_budget_tokens: 1,
            ..settings
        };
        let over = route_request(
            &json!({"model": "auto", "messages": [{"role": "user", "content": "随便聊聊这件事"}]}),
            0,
            &capped,
        );
        assert_eq!(over.model, "deepseek-v4-flash");
        assert_eq!(over.reasons[0].0, "cost");
    }

    #[test]
    fn cascade_is_refused_for_agent_and_vision_turns() {
        let settings = settings();
        assert!(cascade_plan(&json!({"cascade": true}), &settings).enabled);
        assert!(!cascade_plan(&json!({"cascade": true, "agentMode": true}), &settings).enabled);
        let with_image = json!({"cascade": true, "messages": [{"role": "user", "attachments": [
            {"imageData": "data:image/png;base64,AA"},
        ]}]});
        assert!(!cascade_plan(&with_image, &settings).enabled);

        // The judge needs the switch or the payload, and the draft provider follows the name.
        assert!(!cascade_plan(&json!({"cascade": true}), &settings).judge);
        assert!(cascade_plan(&json!({"cascade": true, "judge": true}), &settings).judge);

        let local = ModelRouterSettings {
            draft_model: "ollama/qwen".to_string(),
            ..settings
        };
        assert_eq!(
            cascade_plan(&json!({"cascade": true}), &local).draft_provider,
            "ollama"
        );
        assert_eq!(router_status(&local)["draftProvider"], json!("ollama"));
    }

    #[test]
    fn the_quality_gate_scores_one_minus_a_third_per_reason() {
        assert_eq!(
            quality_gate("", 80, false).to_value(),
            json!({"passed": false, "score": 0.0, "reasons": ["empty"]})
        );
        assert_eq!(quality_gate("太短了", 80, false).reasons, vec!["too_short"]);
        // One reason costs 0.34, and the score keeps three decimals.
        let refusal = quality_gate(
            &format!("很抱歉，我无法回答。{}", "a".repeat(200)),
            80,
            false,
        );
        assert_eq!(refusal.reasons, vec!["refusal"]);
        assert_eq!(refusal.score, 0.66);
        // A single uncertainty marker is tolerated; two are not.
        let one = quality_gate(&format!("我不确定。{}", "a".repeat(200)), 80, false);
        assert!(one.passed);
        let two = quality_gate(
            &format!("我不确定，而且无法确定。{}", "a".repeat(200)),
            80,
            false,
        );
        assert_eq!(two.reasons, vec!["uncertain"]);
        // Citations are only required when asked for.
        let uncited = format!("结论见文档。{}", "a".repeat(200));
        assert!(quality_gate(&uncited, 80, false).passed);
        assert_eq!(
            quality_gate(&uncited, 80, true).reasons,
            vec!["missing_citation"]
        );
        assert!(quality_gate(&format!("见 [^W1]。{}", "a".repeat(200)), 80, true).passed);
    }
}
