//! `POST /api/title` — the native conversation-title route.
//!
//! The pure half of this route (the prompt, the request body, the sanitiser, the rate
//! limiter) lives in `deepseek_policy::title`, where a parity probe can compare it with
//! `title_generator.py` without a socket. This module is the transport and the
//! envelope, and it is deliberately the only part that touches the network.
//!
//! # Why this route existed as a 503
//!
//! `/api/title` is what the frontend calls to name a conversation, and it fell through
//! to the Go `/api/*` catch-all, which answers `503 GO_CONTROL_PROXY_NOT_READY` when no
//! Go control plane is configured. Registering it ahead of that catch-all is the
//! difference between "the endpoint exists" and "the endpoint works".
//!
//! # The upstream call is the oracle's
//!
//! `urlopen(request, timeout=min(DEEPSEEK_TIMEOUT_SECONDS, 20))`: the timeout is the
//! *smaller* of the configured upstream timeout and 20 seconds, because a title is
//! decoration and must not hold a turn open. Errors map the way the oracle maps them —
//! an HTTP error becomes the provider's own message with the status capped at 502, and
//! a transport failure becomes `upstream_timeout` when the reason mentions a timeout
//! and `upstream_failure` otherwise.

use std::sync::OnceLock;
use std::time::Duration;

use axum::Json;
use axum::extract::State;
use axum::http::StatusCode;
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::title::{
    TitleRateLimiter, format_upstream_error, title_from_response, title_request_body,
};
use serde_json::{Value, json};

use crate::chat_execution::{DEFAULT_UPSTREAM_TIMEOUT_SECONDS, DEFAULT_UPSTREAM_URL};

/// The oracle's cap: `min(DEEPSEEK_TIMEOUT_SECONDS, 20)`.
const TITLE_UPSTREAM_TIMEOUT_CAP_SECONDS: u64 = 20;

/// The process-wide window, mirroring the oracle's module-level `_TITLE_RATE_LIMITS`.
///
/// Per process, not per request, on purpose: the oracle's dict is module-level too, so
/// a deployment with one worker enforces one window and a deployment with several
/// enforces one per worker on both sides.
fn rate_limiter() -> &'static TitleRateLimiter {
    static LIMITER: OnceLock<TitleRateLimiter> = OnceLock::new();
    LIMITER.get_or_init(TitleRateLimiter::default)
}

/// The upstream URL, read the same way `chat_execution` reads it.
fn upstream_url() -> String {
    std::env::var("DEEPSEEK_API_URL")
        .map(|value| value.trim().to_string())
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_UPSTREAM_URL.to_string())
}

fn upstream_timeout() -> Duration {
    let configured = std::env::var("DEEPSEEK_TIMEOUT_SECONDS")
        .ok()
        .and_then(|value| value.trim().parse::<u64>().ok())
        .filter(|seconds| *seconds > 0)
        .unwrap_or(DEFAULT_UPSTREAM_TIMEOUT_SECONDS);
    Duration::from_secs(configured.min(TITLE_UPSTREAM_TIMEOUT_CAP_SECONDS))
}

/// `POST /api/title`.
pub async fn api_title(
    State(state): State<TitleRouteState>,
    Json(payload): Json<Value>,
) -> Result<Json<Value>, (StatusCode, Json<Value>)> {
    let api_key = payload
        .get("apiKey")
        .map(|value| deepseek_policy::core_utils::text_or_empty(Some(value)))
        .unwrap_or_default()
        .trim()
        .to_string();
    let api_key = if api_key.is_empty() {
        state.api_key_fallback.trim().to_string()
    } else {
        api_key
    };
    if api_key.is_empty() {
        return Err(app_error(AppError {
            message: "Missing DeepSeek API Key.".to_string(),
            code: codes::MISSING_API_KEY,
            status: 400,
        }));
    }

    // An empty user message is the oracle's early `{"title": ""}` — no rate-limit
    // charge and no upstream call, which is why the check precedes both.
    let Some(body) = title_request_body(&payload) else {
        return Ok(Json(json!({"title": ""})));
    };

    if rate_limiter()
        .check(&api_key, std::time::Instant::now())
        .is_err()
    {
        return Err(app_error(AppError {
            message: "Title generation is temporarily rate limited.".to_string(),
            code: codes::RATE_LIMITED,
            status: 429,
        }));
    }

    let client = reqwest::Client::builder()
        .no_proxy()
        .redirect(reqwest::redirect::Policy::none())
        .retry(reqwest::retry::never())
        .connect_timeout(Duration::from_secs(10))
        .timeout(upstream_timeout())
        .build()
        .map_err(|_| {
            app_error(AppError {
                message: "Cannot reach DeepSeek API".to_string(),
                code: codes::UPSTREAM_FAILURE,
                status: 502,
            })
        })?;
    let response = client
        .post(upstream_url())
        .header("Authorization", format!("Bearer {api_key}"))
        .header("Content-Type", "application/json")
        .header("Accept", "application/json")
        .body(serde_json::to_vec(&body).map_err(|_| {
            app_error(AppError {
                message: "Cannot reach DeepSeek API".to_string(),
                code: codes::UPSTREAM_FAILURE,
                status: 502,
            })
        })?)
        .send()
        .await
        .map_err(|error| {
            // `urllib.error.URLError`: a timeout is its own code, everything else is a
            // generic upstream failure. `reqwest` reports both as one error type, so
            // the distinction is made on the same signal the oracle uses — whether the
            // failure is a timeout.
            let timeout = error.is_timeout();
            app_error(AppError {
                message: format!("Cannot reach DeepSeek API: {error}"),
                code: if timeout {
                    codes::UPSTREAM_TIMEOUT
                } else {
                    codes::UPSTREAM_FAILURE
                },
                status: 502,
            })
        })?;

    let status = response.status();
    let bytes = response.bytes().await.map_err(|_| {
        app_error(AppError {
            message: "Cannot reach DeepSeek API".to_string(),
            code: codes::UPSTREAM_FAILURE,
            status: 502,
        })
    })?;
    let text = String::from_utf8_lossy(&bytes).to_string();
    if !status.is_success() {
        // `HTTPError`: the provider's own message, with `min(status, 502)`.
        return Err(app_error(AppError {
            message: format_upstream_error(&text),
            code: codes::UPSTREAM_FAILURE,
            status: status.as_u16().min(502),
        }));
    }

    let parsed: Value = serde_json::from_str(&text).map_err(|_| {
        app_error(AppError {
            message: "DeepSeek API returned invalid JSON".to_string(),
            code: codes::UPSTREAM_FAILURE,
            status: 502,
        })
    })?;
    Ok(Json(json!({"title": title_from_response(&parsed)})))
}

/// The parts of the server environment this route needs.
///
/// Passed as state rather than read inside the handler so a test can bind a scripted
/// upstream and a known key without touching the process environment.
#[derive(Clone)]
pub struct TitleRouteState {
    pub api_key_fallback: String,
}

impl TitleRouteState {
    pub fn from_env() -> Self {
        Self {
            api_key_fallback: std::env::var("DEEPSEEK_API_KEY").unwrap_or_default(),
        }
    }
}

impl Default for TitleRouteState {
    fn default() -> Self {
        Self::from_env()
    }
}

/// The oracle's `AppError` envelope: `{"error": message, "code": code}`.
fn app_error(error: AppError) -> (StatusCode, Json<Value>) {
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::BAD_REQUEST);
    (
        status,
        Json(json!({"error": error.message, "code": error.code})),
    )
}
