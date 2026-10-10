//! Native non-streaming chat execution.
//!
//! Scope: the `POST /v1/chat/completions` fast path, which the Python oracle
//! serves through `deepseek_client.call_deepseek` → `openai_api.openai_chat_completion`.
//! This module owns **one upstream exchange**: the request, the answer-turn
//! extraction, and the final-answer translation. The multi-round tool loop that
//! drives these exchanges lives in `chat_tool_loop`; the split mirrors how the
//! oracle separates `call_deepseek`'s loop body from the per-turn
//! `first_response_message` + `merge_usage_totals` pair.
//!
//! A turn that answers with `tool_calls` is **no longer refused here** — the
//! loop continues it (execute the calls, append the tool exchange, ask again),
//! mirroring the oracle. What this module still refuses is treating a turn as a
//! final answer when there is no answer to give: empty `choices`, a missing
//! `message`, or empty content all fail with `NATIVE_CHAT_NO_ANSWER`.
//!
//! Deliberately NOT implemented here (each stays on the Python path until its
//! own slice lands, and each is an explicit gap rather than a silent drop):
//! - semantic cache, memory retrieval, context compression, model router
//! - scheduler leases, resiliency retries, trace/span emission, budget ledger
//! - the web-search provider and `mcp__*` external bridging — no callback is
//!   injected, so `web_search` / `compare_search_results` report
//!   "not enabled for this request" and bridged names stay unsupported
//! - SSE streaming (`stream_deepseek`); streaming requests are opened by
//!   [`open_chat_stream`] and read by `chat_stream`

use serde_json::{Map, Value, json};
use std::time::Duration;

/// Mirrors `deepseek_infra.core.config.DEFAULT_DEEPSEEK_API_URL`.
pub const DEFAULT_UPSTREAM_URL: &str = "https://api.deepseek.com/chat/completions";
/// Mirrors `settings.deepseek_timeout_seconds`' default.
///
/// The oracle's default is **180** (`core/config.py`, `_env_int("DEEPSEEK_TIMEOUT_SECONDS", 180)`).
/// This constant read `120` and was measured against the oracle while porting the
/// title route, which caps its own call at `min(timeout, 20)` — a cap that hid the
/// difference. Nothing depended on the wrong value, so it is corrected here rather
/// than left as a divergence a longer request would eventually expose.
pub const DEFAULT_UPSTREAM_TIMEOUT_SECONDS: u64 = 180;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChatExecutionError {
    /// No server-side upstream credential is configured. The oracle raises the
    /// same class of failure (`cloud_api_key_available` false) rather than
    /// asking the client for a provider key.
    MissingUpstreamCredential,
    /// Configured origin or URL is not usable.
    InvalidUpstreamUrl,
    /// Could not reach, or TLS/transport failed against, the upstream.
    UpstreamUnreachable,
    /// Upstream returned a non-success status.
    UpstreamStatus { status: u16 },
    /// Upstream body was not the documented JSON object.
    UpstreamMalformed,
    /// Upstream answered without a usable choices[0].message, or with empty
    /// content.
    NoAnswer,
}

impl ChatExecutionError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::MissingUpstreamCredential => "NATIVE_CHAT_UPSTREAM_CREDENTIAL_MISSING",
            Self::InvalidUpstreamUrl => "NATIVE_CHAT_UPSTREAM_URL_INVALID",
            Self::UpstreamUnreachable => "NATIVE_CHAT_UPSTREAM_UNREACHABLE",
            Self::UpstreamStatus { .. } => "NATIVE_CHAT_UPSTREAM_STATUS",
            Self::UpstreamMalformed => "NATIVE_CHAT_UPSTREAM_MALFORMED",
            Self::NoAnswer => "NATIVE_CHAT_NO_ANSWER",
        }
    }

    /// Upstream-originated failures surface as 502 so the client can retry or
    /// fail over, matching the oracle's `status=502` on `AppError(...)`.
    pub fn status(&self) -> u16 {
        match self {
            Self::MissingUpstreamCredential | Self::InvalidUpstreamUrl => 503,
            Self::UpstreamUnreachable
            | Self::UpstreamStatus { .. }
            | Self::UpstreamMalformed
            | Self::NoAnswer => 502,
        }
    }
}

/// Upstream origin and credential. Both come from the server environment, never
/// from the client request: `request_preparation` rejects any client-supplied
/// `api_key`/`apikey`/`authorization` field outright.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UpstreamConfig {
    pub url: String,
    pub api_key: String,
    pub timeout: Duration,
}

impl UpstreamConfig {
    pub fn from_env() -> Self {
        let url = std::env::var("DEEPSEEK_API_URL")
            .map(|value| value.trim().to_string())
            .ok()
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| DEFAULT_UPSTREAM_URL.to_string());
        let api_key = std::env::var("DEEPSEEK_API_KEY")
            .map(|value| value.trim().to_string())
            .unwrap_or_default();
        let timeout = std::env::var("DEEPSEEK_TIMEOUT_SECONDS")
            .ok()
            .and_then(|value| value.trim().parse::<u64>().ok())
            .filter(|seconds| *seconds > 0)
            .unwrap_or(DEFAULT_UPSTREAM_TIMEOUT_SECONDS);
        Self {
            url,
            api_key,
            timeout: Duration::from_secs(timeout),
        }
    }

    fn validate(&self) -> Result<reqwest::Url, ChatExecutionError> {
        if self.api_key.is_empty() {
            return Err(ChatExecutionError::MissingUpstreamCredential);
        }
        let parsed =
            reqwest::Url::parse(&self.url).map_err(|_| ChatExecutionError::InvalidUpstreamUrl)?;
        // An operator may point this at a self-hosted compatible endpoint, but
        // never at a non-HTTP scheme or a credentialed URL.
        if !matches!(parsed.scheme(), "http" | "https")
            || parsed.host_str().is_none()
            || !parsed.username().is_empty()
            || parsed.password().is_some()
        {
            return Err(ChatExecutionError::InvalidUpstreamUrl);
        }
        Ok(parsed)
    }
}

/// Result of one upstream exchange, in the shape the OpenAI facade needs.
#[derive(Debug, Clone, PartialEq)]
pub struct ChatCompletionResult {
    pub id: Value,
    pub model: String,
    pub content: String,
    pub prompt_tokens: i64,
    pub completion_tokens: i64,
    pub total_tokens: i64,
}

/// The usage fields summed across rounds, mirroring `USAGE_SUM_FIELDS`.
///
/// The two cache fields are not rendered by the OpenAI envelope, but the merge
/// carries them anyway so the later budget-ledger slice inherits the oracle's
/// arithmetic instead of re-deriving it.
const USAGE_SUM_FIELDS: [(&str, &str); 5] = [
    ("prompt_tokens", "promptTokens"),
    ("completion_tokens", "completionTokens"),
    ("total_tokens", "totalTokens"),
    ("prompt_cache_hit_tokens", "promptCacheHitTokens"),
    ("prompt_cache_miss_tokens", "promptCacheMissTokens"),
];

/// Mirrors `deepseek_client.usage_int`: the first named field that is present,
/// non-`None` and non-blank, coerced the way Python's `int()` coerces — an
/// integer passes, a float truncates toward zero, a numeric string must be an
/// integer literal (after `int()`'s whitespace strip) — floored at zero.
///
/// Unparseable, blank and absent values fall through to the next name, then 0.
fn usage_int(usage: &Value, names: &[&str]) -> i64 {
    for name in names {
        let Some(raw) = usage.get(*name) else {
            continue;
        };
        if let Some(number) = raw.as_i64() {
            return number.max(0);
        }
        if let Some(flag) = raw.as_bool() {
            return i64::from(flag);
        }
        if let Some(number) = raw.as_f64() {
            return (number.trunc() as i64).max(0);
        }
        if let Some(text) = raw.as_str() {
            if !text.is_empty() {
                if let Ok(parsed) = text.trim().parse::<i64>() {
                    return parsed.max(0);
                }
            }
        }
    }
    0
}

/// Mirrors `merge_usage_totals`: add one round's usage into the running total.
///
/// A field is only written when the round's contribution is truthy — Python's
/// `if value:` — so a zero increment leaves the field absent, exactly as in the
/// oracle. A non-object or empty usage leaves the total untouched.
pub fn merge_usage_totals(total: &Value, usage: &Value) -> Value {
    let Some(fields) = usage.as_object() else {
        return total.clone();
    };
    if fields.is_empty() {
        return total.clone();
    }
    let mut merged: Map<String, Value> = total.as_object().cloned().unwrap_or_default();
    for (canonical, alias) in USAGE_SUM_FIELDS {
        let increment = usage_int(usage, &[canonical, alias]);
        if increment == 0 {
            continue;
        }
        // The running total is always one of this merger's own integer writes
        // under the canonical name, so a plain read suffices.
        let running = merged.get(canonical).and_then(Value::as_i64).unwrap_or(0);
        merged.insert(canonical.to_string(), json!(running + increment));
    }
    Value::Object(merged)
}

/// One upstream answer turn: `first_response_message` plus the fields the tool
/// loop needs from the surrounding response body.
///
/// Extraction is deliberately **not** the final-answer translation. A turn that
/// carries `tool_calls` extracts fine — the loop's job is to continue it, not
/// to refuse it. [`final_answer`] is what refuses to invent an answer.
#[derive(Debug, Clone, PartialEq)]
pub struct UpstreamTurn {
    /// The response `id`, passed through verbatim (`response_json.get("id")`).
    pub id: Value,
    /// The response `model`. Required: a body without one is refused rather
    /// than silently relabelled with the requested model.
    pub model: String,
    /// `choices[0].message` as an object.
    pub message: Value,
    /// The raw `usage` object (or `Null`), merged across rounds by the loop.
    pub usage: Value,
}

impl UpstreamTurn {
    /// The finalized tool calls, mirroring
    /// `normalize_tool_calls(answer.get("tool_calls"))`: the lenient round-layer
    /// normalization, which silently drops entries it cannot represent — this is
    /// the provider's own output, not a client's.
    pub fn tool_calls(&self) -> Vec<Value> {
        match self.message.get("tool_calls").and_then(Value::as_array) {
            Some(items) => crate::tool_rounds::normalize_tool_calls_lenient(items),
            None => Vec::new(),
        }
    }

    /// `str(answer.get("content") or "")`. A non-string content reads as empty —
    /// the shapes DeepSeek sends are string or null.
    pub fn content(&self) -> String {
        self.message
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string()
    }

    /// `answer.get("reasoning_content") or answer.get("reasoning")`,
    /// stringified when truthy — replayed into the follow-up request because
    /// thinking mode rejects a `tool_calls` assistant message without it.
    pub fn reasoning_content(&self) -> String {
        for candidate in [
            self.message.get("reasoning_content"),
            self.message.get("reasoning"),
        ] {
            if let Some(text) = candidate.and_then(stringify_truthy) {
                return text;
            }
        }
        String::new()
    }
}

/// Python truthiness plus `str()` for the reasoning fields: `null`, `""`, `0`,
/// `0.0`, `false`, `[]` and `{}` fall through; a non-empty string passes and a
/// number or `true` is stringified the way `str()` would.
fn stringify_truthy(value: &Value) -> Option<String> {
    match value {
        Value::Null => None,
        Value::String(text) => (!text.is_empty()).then(|| text.clone()),
        Value::Bool(flag) => (*flag).then(|| "True".to_string()),
        Value::Number(number) => (number.as_f64() != Some(0.0)).then(|| number.to_string()),
        Value::Array(items) => {
            (!items.is_empty()).then(|| serde_json::to_string(value).unwrap_or_default())
        }
        Value::Object(fields) => {
            (!fields.is_empty()).then(|| serde_json::to_string(value).unwrap_or_default())
        }
    }
}

/// Extract the answer turn from an upstream body. Split out so the contract is
/// unit-testable without a live upstream.
pub fn turn_from_payload(payload: &Value) -> Result<UpstreamTurn, ChatExecutionError> {
    let object = payload
        .as_object()
        .ok_or(ChatExecutionError::UpstreamMalformed)?;
    let choices = object
        .get("choices")
        .and_then(Value::as_array)
        .ok_or(ChatExecutionError::UpstreamMalformed)?;
    let first = choices
        .first()
        .and_then(Value::as_object)
        .ok_or(ChatExecutionError::NoAnswer)?;
    let message = first
        .get("message")
        .and_then(Value::as_object)
        .ok_or(ChatExecutionError::NoAnswer)?;
    let model = object
        .get("model")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string)
        .ok_or(ChatExecutionError::UpstreamMalformed)?;
    Ok(UpstreamTurn {
        id: object.get("id").cloned().unwrap_or(Value::Null),
        model,
        message: Value::Object(message.clone()),
        usage: object.get("usage").cloned().unwrap_or(Value::Null),
    })
}

/// Translate the loop's last turn into the facade result.
///
/// The gateway's pre-existing rule, kept and pinned by tests: an answer with
/// empty content is refused (`NATIVE_CHAT_NO_ANSWER`) rather than returned as a
/// silent success. (The oracle returns `""` with 200 here — a divergence this
/// facade has always had, not one the loop introduced.)
pub fn final_answer(
    turn: &UpstreamTurn,
    usage_totals: &Value,
) -> Result<ChatCompletionResult, ChatExecutionError> {
    let content = turn.content();
    if content.is_empty() {
        return Err(ChatExecutionError::NoAnswer);
    }
    let prompt_tokens = usage_int(usage_totals, &["prompt_tokens", "promptTokens"]);
    let completion_tokens = usage_int(usage_totals, &["completion_tokens", "completionTokens"]);
    let total_tokens = usage_int(usage_totals, &["total_tokens", "totalTokens"]);
    let total_tokens = if total_tokens == 0 {
        prompt_tokens + completion_tokens
    } else {
        total_tokens
    };
    Ok(ChatCompletionResult {
        id: turn.id.clone(),
        model: turn.model.clone(),
        content,
        prompt_tokens,
        completion_tokens,
        total_tokens,
    })
}

/// Pure translation from a single upstream body, for callers outside the loop.
///
/// A `tool_calls` turn extracts like any other here; a caller that treats one as
/// a final answer would be flattening a round the oracle continues. The loop in
/// `chat_tool_loop` never does — its `decide_round` keeps executing first.
pub fn translate_completion(payload: &Value) -> Result<ChatCompletionResult, ChatExecutionError> {
    let turn = turn_from_payload(payload)?;
    final_answer(&turn, &turn.usage)
}

/// Build the upstream HTTP client shared by both exchange shapes.
///
/// The same client options must apply whether the caller streams or not, so the
/// two paths cannot drift on timeout, redirect, proxy or retry policy. Only the
/// total timeout differs: a single non-streaming answer is bounded by the
/// request timeout, while a streaming body is read incrementally and must be
/// allowed to outlive the first byte — its bound is the read loop's own
/// cancellation, not a whole-response deadline.
fn upstream_client(
    config: &UpstreamConfig,
    whole_response_timeout: bool,
) -> Result<reqwest::Client, ChatExecutionError> {
    let mut builder = reqwest::Client::builder()
        // Upstream reachability must be the operator's decision, not whatever
        // proxy variables happen to be set in the server process.
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .connect_timeout(Duration::from_secs(10));
    if whole_response_timeout {
        builder = builder.timeout(config.timeout);
    }
    builder
        .build()
        .map_err(|_| ChatExecutionError::UpstreamUnreachable)
}

/// Send one non-streaming turn and extract it.
///
/// `body` is the loop's working body — the prepared request on the first round,
/// then each `append_tool_exchange` / `force_final_answer_without_tools`
/// result. Headers, statuses and error codes match the single-exchange contract
/// this route has always served.
pub async fn exchange_turn(
    config: &UpstreamConfig,
    body: &Value,
) -> Result<UpstreamTurn, ChatExecutionError> {
    let url = config.validate()?;
    let bytes = serde_json::to_vec(body).map_err(|_| ChatExecutionError::UpstreamMalformed)?;
    let client = upstream_client(config, true)?;
    let response = client
        .post(url)
        .header("Authorization", format!("Bearer {}", config.api_key))
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .body(bytes)
        .send()
        .await
        .map_err(|_| ChatExecutionError::UpstreamUnreachable)?;
    let status = response.status();
    if !status.is_success() {
        return Err(ChatExecutionError::UpstreamStatus {
            status: status.as_u16(),
        });
    }
    // `reqwest::Response::json` needs the crate's `json` feature, which this
    // crate does not enable; decode the bytes explicitly instead.
    let bytes = response
        .bytes()
        .await
        .map_err(|_| ChatExecutionError::UpstreamUnreachable)?;
    let payload: Value =
        serde_json::from_slice(&bytes).map_err(|_| ChatExecutionError::UpstreamMalformed)?;
    turn_from_payload(&payload)
}

/// Open one streaming upstream turn and hand back the live response.
///
/// Split from the decoding loop on purpose: the caller needs the *status* before
/// it writes the first downstream frame, because a non-success upstream status
/// must surface as an HTTP error rather than a `200` followed by an error frame.
/// Returning the open `reqwest::Response` lets the caller make that decision and
/// then own the read loop, which is where cancellation and backpressure live.
///
/// `prepared` must already carry `"stream": true`; `request_preparation` sets it
/// for streaming requests, mirroring the oracle's
/// `{"model": ..., "messages": ..., "stream": stream}` body (line 299 of
/// `deepseek_client.py`). This function does not re-inject it: negotiating the
/// body shape is preparation's job, and duplicating it here would make the two
/// layers able to disagree.
pub async fn open_chat_stream(
    config: &UpstreamConfig,
    prepared: &Value,
) -> Result<reqwest::Response, ChatExecutionError> {
    let url = config.validate()?;
    let body = serde_json::to_vec(prepared).map_err(|_| ChatExecutionError::UpstreamMalformed)?;
    let client = upstream_client(config, false)?;
    let response = client
        .post(url)
        .header("Authorization", format!("Bearer {}", config.api_key))
        .header("Content-Type", "application/json")
        // The oracle asks for `text/event-stream` on the streaming path
        // (`request_with_body(..., accept="text/event-stream")`).
        .header("Accept", "text/event-stream")
        .body(body)
        .send()
        .await
        .map_err(|_| ChatExecutionError::UpstreamUnreachable)?;
    let status = response.status();
    if !status.is_success() {
        return Err(ChatExecutionError::UpstreamStatus {
            status: status.as_u16(),
        });
    }
    Ok(response)
}

/// Render the OpenAI `chat.completion` envelope. `fallback_model` is the model
/// the client asked for, used when the upstream omits its own.
pub fn openai_completion_response(
    result: &ChatCompletionResult,
    fallback_model: &str,
    created: i64,
) -> Value {
    let id = match &result.id {
        Value::String(id) if !id.is_empty() => id.clone(),
        _ => format!("chatcmpl-{created}"),
    };
    let model = if result.model.is_empty() {
        fallback_model
    } else {
        &result.model
    };
    json!({
        "id": id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": result.content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.completion_tokens,
            "total_tokens": result.total_tokens,
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn upstream_body() -> Value {
        json!({
            "id": "chat-1",
            "model": "deepseek-v4-pro",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })
    }

    #[test]
    fn missing_credential_fails_closed_without_contacting_upstream() {
        let config = UpstreamConfig {
            url: DEFAULT_UPSTREAM_URL.to_string(),
            api_key: String::new(),
            timeout: Duration::from_secs(1),
        };
        assert_eq!(
            config.validate().unwrap_err(),
            ChatExecutionError::MissingUpstreamCredential
        );
    }

    #[test]
    fn credentialed_or_non_http_urls_are_refused() {
        for url in [
            "file:///tmp/upstream",
            "https://user:pass@api.example/chat/completions",
            "not a url",
        ] {
            let config = UpstreamConfig {
                url: url.to_string(),
                api_key: "k".to_string(),
                timeout: Duration::from_secs(1),
            };
            assert_eq!(
                config.validate().unwrap_err(),
                ChatExecutionError::InvalidUpstreamUrl,
                "url accepted: {url}"
            );
        }
    }

    #[test]
    fn translates_a_plain_completion() {
        let result = translate_completion(&upstream_body()).unwrap();
        assert_eq!(result.content, "hello");
        assert_eq!(result.model, "deepseek-v4-pro");
        assert_eq!(result.total_tokens, 5);
    }

    #[test]
    fn derives_total_tokens_when_upstream_omits_it() {
        let mut body = upstream_body();
        body["usage"] = json!({"prompt_tokens": 7, "completion_tokens": 4});
        let result = translate_completion(&body).unwrap();
        assert_eq!(result.total_tokens, 11);
    }

    #[test]
    fn a_tool_call_turn_extracts_so_the_loop_can_continue() {
        // The turn is not refused — it is *data* for the loop. The finalized
        // calls use the lenient round-layer normalization: id fallback,
        // name required, arguments string passed through verbatim.
        let mut body = upstream_body();
        body["choices"][0]["message"] = json!({
            "role": "assistant",
            "content": "let me chart that",
            "reasoning_content": "thinking about it",
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "generate_chart", "arguments": "{\"type\":\"bar\"}"}},
                {"function": {"name": "", "arguments": "{}"}},
                "not an object",
            ],
        });
        let turn = turn_from_payload(&body).unwrap();
        assert_eq!(turn.content(), "let me chart that");
        assert_eq!(turn.reasoning_content(), "thinking about it");
        let calls = turn.tool_calls();
        assert_eq!(calls.len(), 1, "blank-name and non-object entries drop");
        assert_eq!(calls[0]["id"], "call-1");
        assert_eq!(calls[0]["function"]["name"], "generate_chart");
        assert_eq!(calls[0]["function"]["arguments"], "{\"type\":\"bar\"}");
    }

    #[test]
    fn reasoning_falls_back_to_reasoning_and_requires_truthy() {
        let mut body = upstream_body();
        body["choices"][0]["message"] = json!({
            "role": "assistant",
            "content": "hi",
            "reasoning_content": "",
            "reasoning": "fallback",
        });
        let turn = turn_from_payload(&body).unwrap();
        assert_eq!(turn.reasoning_content(), "fallback");

        body["choices"][0]["message"]["reasoning"] = json!("");
        let turn = turn_from_payload(&body).unwrap();
        assert_eq!(turn.reasoning_content(), "");
    }

    #[test]
    fn empty_choices_and_empty_content_are_refused() {
        let mut body = upstream_body();
        body["choices"] = json!([]);
        assert_eq!(
            turn_from_payload(&body).unwrap_err(),
            ChatExecutionError::NoAnswer
        );

        let mut body = upstream_body();
        body["choices"][0]["message"]["content"] = json!("");
        assert_eq!(
            translate_completion(&body).unwrap_err(),
            ChatExecutionError::NoAnswer
        );
    }

    #[test]
    fn usage_totals_sum_across_rounds_with_aliases_and_zero_skips() {
        // Round one carries camelCase aliases, round two the canonical names.
        let totals = merge_usage_totals(
            &json!({}),
            &json!({
                "promptTokens": 3, "completionTokens": 2, "prompt_cache_hit_tokens": 4,
            }),
        );
        let totals = merge_usage_totals(
            &totals,
            &json!({
                "prompt_tokens": 7, "completion_tokens": 4, "total_tokens": 11,
            }),
        );
        assert_eq!(totals["prompt_tokens"], 10);
        assert_eq!(totals["completion_tokens"], 6);
        assert_eq!(totals["total_tokens"], 11);
        assert_eq!(totals["prompt_cache_hit_tokens"], 4);

        // A zero contribution is falsy in Python, so the field is left absent
        // rather than written as a sum that changed nothing.
        let totals = merge_usage_totals(&totals, &json!({"prompt_tokens": 0}));
        assert!(
            totals
                .get("prompt_tokens")
                .is_some_and(|value| value == &json!(10))
        );

        // Non-dict and empty usage leave the total untouched.
        let untouched = merge_usage_totals(&totals, &Value::Null);
        assert_eq!(untouched, totals);
        let untouched = merge_usage_totals(&totals, &json!({}));
        assert_eq!(untouched, totals);
    }

    #[test]
    fn usage_int_coerces_the_way_pythons_int_does() {
        let usage = json!({
            "a": 5, "b": 5.9, "c": "7", "d": " 8 ", "e": "7.9",
            "f": "", "g": true, "h": false,
        });
        assert_eq!(usage_int(&usage, &["a"]), 5);
        assert_eq!(usage_int(&usage, &["b"]), 5);
        assert_eq!(usage_int(&usage, &["c"]), 7);
        assert_eq!(usage_int(&usage, &["d"]), 8);
        // "7.9" raises in Python, so the next name is consulted.
        assert_eq!(usage_int(&usage, &["e", "a"]), 5);
        // Blank and unparseable values fall through to the next name.
        assert_eq!(usage_int(&usage, &["f", "a"]), 5);
        assert_eq!(usage_int(&usage, &["missing", "a"]), 5);
        assert_eq!(usage_int(&usage, &["missing"]), 0);
        // `int(True)` is 1 and `int(False)` is 0.
        assert_eq!(usage_int(&usage, &["g"]), 1);
        assert_eq!(usage_int(&usage, &["h"]), 0);
    }

    #[test]
    fn upstream_errors_are_502_while_local_misconfiguration_is_503() {
        assert_eq!(
            ChatExecutionError::UpstreamStatus { status: 429 }.status(),
            502
        );
        assert_eq!(ChatExecutionError::UpstreamUnreachable.status(), 502);
        assert_eq!(ChatExecutionError::MissingUpstreamCredential.status(), 503);
    }

    #[test]
    fn errors_carry_distinct_codes() {
        let codes = [
            ChatExecutionError::MissingUpstreamCredential.code(),
            ChatExecutionError::InvalidUpstreamUrl.code(),
            ChatExecutionError::UpstreamUnreachable.code(),
            ChatExecutionError::UpstreamStatus { status: 500 }.code(),
            ChatExecutionError::UpstreamMalformed.code(),
            ChatExecutionError::NoAnswer.code(),
        ];
        let unique: std::collections::HashSet<&str> = codes.iter().copied().collect();
        assert_eq!(
            unique.len(),
            codes.len(),
            "duplicate error codes: {codes:?}"
        );
        assert!(codes.iter().all(|code| code.starts_with("NATIVE_CHAT_")));
    }

    #[test]
    fn renders_the_openai_envelope() {
        let result = translate_completion(&upstream_body()).unwrap();
        let response = openai_completion_response(&result, "deepseek-v4-flash", 1_700_000_000);
        assert_eq!(response["object"], "chat.completion");
        assert_eq!(response["id"], "chat-1");
        assert_eq!(response["model"], "deepseek-v4-pro");
        assert_eq!(response["choices"][0]["message"]["role"], "assistant");
        assert_eq!(response["choices"][0]["message"]["content"], "hello");
        assert_eq!(response["choices"][0]["finish_reason"], "stop");
        assert_eq!(response["usage"]["total_tokens"], 5);
    }

    #[test]
    fn an_upstream_body_without_a_model_is_malformed() {
        // The facade's `model` fallback only covers the rendering step; a body
        // that omits `model` is refused instead of being silently labelled
        // with the requested model.
        let mut body = upstream_body();
        body["model"] = json!("");
        assert_eq!(
            turn_from_payload(&body).unwrap_err(),
            ChatExecutionError::UpstreamMalformed
        );
    }

    #[test]
    fn renders_a_generated_id_when_upstream_omits_one() {
        let mut body = upstream_body();
        body.as_object_mut().unwrap().remove("id");
        let result = translate_completion(&body).unwrap();
        let response = openai_completion_response(&result, "deepseek-v4-flash", 42);
        assert_eq!(response["id"], "chatcmpl-42");
        assert_eq!(response["model"], "deepseek-v4-pro");
    }

    #[test]
    fn falls_back_to_the_requested_model_when_the_result_carries_none() {
        // Defensive path in the renderer: `ChatCompletionResult` is public, so
        // a future caller could construct one without a model.
        let result = ChatCompletionResult {
            id: Value::Null,
            model: String::new(),
            content: "hi".to_string(),
            prompt_tokens: 1,
            completion_tokens: 1,
            total_tokens: 2,
        };
        let response = openai_completion_response(&result, "deepseek-v4-flash", 7);
        assert_eq!(response["model"], "deepseek-v4-flash");
        assert_eq!(response["id"], "chatcmpl-7");
    }
}
