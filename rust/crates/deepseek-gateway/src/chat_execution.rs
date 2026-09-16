//! Native non-streaming chat execution.
//!
//! Scope: the `POST /v1/chat/completions` fast path, which the Python oracle
//! serves through `deepseek_client.call_deepseek` → `openai_api.openai_chat_completion`.
//! This module owns the upstream single-round exchange only.
//!
//! Deliberately NOT implemented here (each stays on the Python path until its
//! own slice lands, and each is reported as an explicit blocker rather than
//! silently dropped):
//! - tool-call rounds (`append_tool_exchange`, web search, `create_pptx`)
//! - semantic cache, memory retrieval, context compression, model router
//! - scheduler leases, resiliency retries, trace/span emission, budget ledger
//! - SSE streaming (`stream_deepseek`); streaming requests are refused upstream
//!   by `request_preparation` before reaching this module
//!
//! Because of that, an upstream response carrying `tool_calls` is refused with
//! `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` instead of returning a flattened answer:
//! returning the tool-call round's prose as if it were the final answer would be
//! a silent behavior change against the oracle.

use serde_json::{Value, json};
use std::time::Duration;

/// Mirrors `deepseek_infra.core.config.DEFAULT_DEEPSEEK_API_URL`.
pub const DEFAULT_UPSTREAM_URL: &str = "https://api.deepseek.com/chat/completions";
/// Mirrors `settings.deepseek_timeout_seconds`' default.
pub const DEFAULT_UPSTREAM_TIMEOUT_SECONDS: u64 = 120;

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
    /// Upstream answered with tool_calls, which this slice cannot continue.
    ToolRoundsUnwired,
    /// Upstream answered without a usable choices[0].message.
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
            Self::ToolRoundsUnwired => "NATIVE_CHAT_TOOL_ROUNDS_NOT_READY",
            Self::NoAnswer => "NATIVE_CHAT_NO_ANSWER",
        }
    }

    /// Upstream-originated failures surface as 502 so the client can retry or
    /// fail over, matching the oracle's `status=502` on `AppError(...)`.
    pub fn status(&self) -> u16 {
        match self {
            Self::MissingUpstreamCredential => 503,
            Self::InvalidUpstreamUrl => 503,
            Self::ToolRoundsUnwired => 501,
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

fn usage_int(usage: &Value, names: &[&str]) -> i64 {
    for name in names {
        if let Some(value) = usage.get(*name).and_then(Value::as_i64) {
            return value.max(0);
        }
    }
    0
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

/// Send one non-streaming completion and translate the upstream body.
///
/// `prepared` is the output of `request_preparation::prepare_request`, so the
/// sanitization and validation rules have already been applied exactly once.
pub async fn execute_chat_completion(
    config: &UpstreamConfig,
    prepared: &Value,
) -> Result<ChatCompletionResult, ChatExecutionError> {
    let url = config.validate()?;
    let body = serde_json::to_vec(prepared).map_err(|_| ChatExecutionError::UpstreamMalformed)?;
    let client = upstream_client(config, true)?;
    let response = client
        .post(url)
        .header("Authorization", format!("Bearer {}", config.api_key))
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
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
    // `reqwest::Response::json` needs the crate's `json` feature, which this
    // crate does not enable; decode the bytes explicitly instead.
    let bytes = response
        .bytes()
        .await
        .map_err(|_| ChatExecutionError::UpstreamUnreachable)?;
    let payload: Value =
        serde_json::from_slice(&bytes).map_err(|_| ChatExecutionError::UpstreamMalformed)?;
    translate_completion(&payload)
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

/// Pure translation from an upstream chat-completions body. Split out so the
/// contract is unit-testable without a live upstream.
pub fn translate_completion(payload: &Value) -> Result<ChatCompletionResult, ChatExecutionError> {
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
    // A tool-calling round is not a final answer. Refuse instead of returning
    // the round's prose as the completion.
    if message
        .get("tool_calls")
        .and_then(Value::as_array)
        .is_some_and(|calls| !calls.is_empty())
    {
        return Err(ChatExecutionError::ToolRoundsUnwired);
    }
    let content = message
        .get("content")
        .and_then(Value::as_str)
        .ok_or(ChatExecutionError::NoAnswer)?
        .to_string();
    if content.is_empty() {
        return Err(ChatExecutionError::NoAnswer);
    }
    let usage = object.get("usage").cloned().unwrap_or(Value::Null);
    let prompt_tokens = usage_int(&usage, &["prompt_tokens", "promptTokens"]);
    let completion_tokens = usage_int(&usage, &["completion_tokens", "completionTokens"]);
    let total_tokens = usage_int(&usage, &["total_tokens", "totalTokens"]);
    let total_tokens = if total_tokens == 0 {
        prompt_tokens + completion_tokens
    } else {
        total_tokens
    };
    let model = object
        .get("model")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToString::to_string)
        .ok_or(ChatExecutionError::UpstreamMalformed)?;
    Ok(ChatCompletionResult {
        id: object.get("id").cloned().unwrap_or(Value::Null),
        model,
        content,
        prompt_tokens,
        completion_tokens,
        total_tokens,
    })
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
    fn tool_calls_are_refused_rather_than_flattened_into_an_answer() {
        // Returning this round's prose would silently drop the oracle's tool
        // loop, so the slice must refuse.
        let mut body = upstream_body();
        body["choices"][0]["message"]["tool_calls"] = json!([{"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}}]);
        assert_eq!(
            translate_completion(&body).unwrap_err(),
            ChatExecutionError::ToolRoundsUnwired
        );
        assert_eq!(
            ChatExecutionError::ToolRoundsUnwired.status(),
            501,
            "unwired capability must not report success"
        );
    }

    #[test]
    fn empty_choices_and_empty_content_are_refused() {
        let mut body = upstream_body();
        body["choices"] = json!([]);
        assert_eq!(
            translate_completion(&body).unwrap_err(),
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
            ChatExecutionError::ToolRoundsUnwired.code(),
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
            translate_completion(&body).unwrap_err(),
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
