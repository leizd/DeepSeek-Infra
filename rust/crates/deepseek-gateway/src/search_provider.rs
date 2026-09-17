//! The web-search provider: a concrete Tavily transport, and the `web_search`
//! callback the tool executor injects.
//!
//! This is the wiring layer. The logic it drives lives in `deepseek_policy::search`
//! and is byte-verified against the oracle; what is here is the HTTP client, the
//! per-request state, and the environment config — the parts that cannot be pure.
//!
//! Mirrors `_perform_web_search` / `search_single_round` from
//! `infra/gateway/deepseek_client.py` and `infra/tool_runtime/search.py`.
//!
//! # Why the transport is blocking
//!
//! `ExecutorContext.web_search` is a **synchronous** callback, and the round loop
//! calls it from `tokio::task::spawn_blocking` — so a blocking client is the right
//! shape, not a compromise. `reqwest::blocking` must not be entered from an async
//! context; a `spawn_blocking` thread has none, so it is safe here.
//!
//! # `search_budget` and `progress_callback` are parity, not gaps
//!
//! An earlier version of this doc called them "deliberate omissions with owners".
//! Measuring the call path says otherwise, so the claim is corrected here rather than
//! left standing:
//!
//! - **`search_budget` is `None` on this route.** `SearchBudget` is constructed only in
//!   `agent_runtime/agent_runs.py` and `agent_runtime/multi_agent.py`; the OpenAI route
//!   calls `provider.stream_chat(payload, emit, cancel_event=…)` with no budget, so the
//!   parameter takes its `None` default and the `try_consume` refusal **never fires**.
//!   Adding it here would introduce a refusal the oracle does not perform.
//! - **`progress_callback` is `None` too**, and its state mutation has no destination
//!   here. `record_progress` still records the round into `rounds_by_index` and refreshes
//!   `latest_search_data` when the callback is `None`, and that value flows to
//!   `search_for_response` — but neither `openai_chat_stream` nor
//!   `openai_completion_response` carries a `search` field, so nothing is observable on
//!   `/v1/chat/completions`. The internal `/api/chat` route is what surfaces it, and that
//!   route is out of scope for the Rust gateway.
//!
//! Both belong to the agent runtime and the internal route, which the Rust gateway does
//! not own. Recording them as "not ported" would have implied a missing refusal where the
//! reference implementation is silent.

use std::cell::RefCell;
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use deepseek_policy::search::{
    SEARCH_CACHE_MAX_AGE_SECONDS, Transport, TransportOutcome, compact_search_tool_result,
    load_search_cache, normalize_search_query_text, save_search_cache, search_round_status,
    search_tavily_with_retry,
};
use deepseek_policy::tool_dispatch::ToolFailure;
use serde_json::{Value, json};

/// `WEB_SEARCH_TURN_LIMIT`.
pub const WEB_SEARCH_TURN_LIMIT: i64 = 15;
/// `WEB_SEARCH_LIMIT_ERROR`.
pub const WEB_SEARCH_LIMIT_ERROR: &str = "本轮搜索次数已达上限，请基于已有搜索结果回答。";
/// `TAVILY_TIMEOUT_SECONDS`, from the policy crate's constant.
pub const TAVILY_TIMEOUT_SECONDS: u64 = deepseek_policy::search::TAVILY_TIMEOUT_SECONDS;

/// Upstream endpoint and credential, both from the server environment.
///
/// The key is never read from the request: `request_preparation` rejects
/// client-supplied credential fields, and this follows that contract.
pub struct TavilyConfig {
    pub url: String,
    pub api_key: String,
    pub timeout: Duration,
}

impl TavilyConfig {
    /// `TAVILY_API_URL` (defaulting to the pinned endpoint), `TAVILY_API_KEY`, and
    /// `TAVILY_TIMEOUT_SECONDS`.
    pub fn from_env() -> Self {
        let url = std::env::var("TAVILY_API_URL")
            .map(|value| value.trim().to_string())
            .ok()
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| deepseek_policy::search::TAVILY_URL.to_string());
        let api_key = std::env::var("TAVILY_API_KEY")
            .map(|value| value.trim().to_string())
            .unwrap_or_default();
        let timeout = std::env::var("TAVILY_TIMEOUT_SECONDS")
            .ok()
            .and_then(|value| value.trim().parse::<u64>().ok())
            .filter(|seconds| *seconds > 0)
            .unwrap_or(TAVILY_TIMEOUT_SECONDS);
        Self {
            url,
            api_key,
            timeout: Duration::from_secs(timeout),
        }
    }

    pub fn is_configured(&self) -> bool {
        !self.api_key.is_empty()
    }
}

/// A transport that performs the POST with `reqwest::blocking`.
///
/// The outcome mapping is the transport's whole job: a response — **any** status —
/// becomes `Response`, and everything that stops the request from completing becomes
/// `Failure`, with `timed_out` distinguishing the oracle's `"timed out"` check so
/// `search_tavily` can pick `upstream_timeout` over `upstream_failure`.
/// Takes the timeout **by value, not a `&TavilyConfig`**.
///
/// In edition 2024 a returned `impl Trait` captures every input lifetime by default, so
/// a reference parameter would tie the closure to the config's borrow — and
/// `SearchProvider::callback` cannot hand out a closure borrowed from `&self`. Owning
/// the single field it needs removes the lifetime question entirely.
pub fn tavily_transport(
    timeout: Duration,
) -> impl Fn(&str, &[u8], &[(&str, &str)]) -> TransportOutcome {
    move |url: &str, body: &[u8], headers: &[(&str, &str)]| {
        let Ok(client) = reqwest::blocking::Client::builder()
            .timeout(timeout)
            .build()
        else {
            return TransportOutcome::Failure {
                reason: "could not build the HTTP client".to_string(),
                timed_out: false,
            };
        };
        let mut request = client.post(url).body(body.to_vec());
        for (name, value) in headers {
            request = request.header(*name, *value);
        }
        match request.send() {
            Ok(response) => {
                let status = response.status().as_u16();
                match response.bytes() {
                    Ok(bytes) => TransportOutcome::Response {
                        status,
                        body: bytes.to_vec(),
                    },
                    // The status arrived but the body did not; the oracle reads the
                    // body inside the same `try`, so this is a transport failure.
                    Err(error) => TransportOutcome::Failure {
                        reason: error.to_string(),
                        timed_out: error.is_timeout(),
                    },
                }
            }
            Err(error) => TransportOutcome::Failure {
                // The oracle classifies on the *reason text* containing "timed out";
                // `reqwest` knows this properly, so the flag is set from the error.
                reason: error.to_string(),
                timed_out: error.is_timeout(),
            },
        }
    }
}

/// Per-request search state: the oracle's `counter`, `citation_counter` and
/// `cached_tool_results` closure variables.
#[derive(Default)]
struct SearchState {
    counter: i64,
    citation_offset: i64,
    tool_results: BTreeMap<String, Value>,
}

/// The web-search provider for one request.
pub struct SearchProvider {
    config: TavilyConfig,
    root: PathBuf,
    turn_limit: i64,
}

impl SearchProvider {
    /// Reads the config from the environment and takes the workspace root, which is
    /// also where the search cache lives (`<root>/.search-cache`).
    pub fn from_env(root: PathBuf) -> Self {
        let turn_limit = std::env::var("WEB_SEARCH_TURN_LIMIT")
            .ok()
            .and_then(|value| value.trim().parse::<i64>().ok())
            .filter(|limit| *limit > 0)
            .unwrap_or(WEB_SEARCH_TURN_LIMIT);
        Self {
            config: TavilyConfig::from_env(),
            root,
            turn_limit: turn_limit.max(1),
        }
    }

    pub fn is_configured(&self) -> bool {
        self.config.is_configured()
    }

    /// The `ExecutorContext.web_search` callback.
    ///
    /// Mirrors `_perform_web_search`, including the order: the per-request memo is
    /// consulted **before** the turn limit, so a repeated query does not consume a
    /// turn; and `limit_result` itself increments the counter, so hitting the limit
    /// still advances the round numbering.
    pub fn callback(self) -> impl Fn(&str, &str) -> Result<Value, ToolFailure> {
        let transport = tavily_transport(self.config.timeout);
        let TavilyConfig { api_key, .. } = self.config;
        let root = self.root;
        let turn_limit = self.turn_limit;
        let state = RefCell::new(SearchState::default());

        move |query: &str, intent: &str| {
            let cleaned = normalize_search_query_text(query);
            let key = cleaned.to_lowercase();
            let mut state = state.borrow_mut();

            if !key.is_empty() {
                if let Some(memo) = state.tool_results.get(&key) {
                    let mut hit = memo.clone();
                    hit["cached"] = json!(true);
                    return Ok(hit);
                }
            }

            if state.counter >= turn_limit {
                return Ok(limit_result(&cleaned, intent, &mut state));
            }

            state.counter += 1;
            let round_index = state.counter;
            let citation_offset = state.citation_offset;

            let result = match search_single_round(
                &cleaned,
                intent,
                round_index,
                citation_offset,
                &api_key,
                &root,
                &transport,
            ) {
                Ok(value) => value,
                Err(error) => {
                    let failed = compact_search_tool_result(
                        &search_round_status(&cleaned, round_index, "error", &error.message),
                        intent,
                        citation_offset,
                    );
                    return Ok(failed);
                }
            };

            let count = result
                .get("results")
                .and_then(Value::as_array)
                .map(|items| items.len() as i64)
                .unwrap_or(0);
            state.citation_offset += count;
            if !key.is_empty() {
                state.tool_results.insert(key, result.clone());
            }
            Ok(result)
        }
    }
}

/// Mirrors `limit_result`: a search that was refused for budget, which is reported as
/// an **error round** rather than an error envelope, so the model still sees it.
fn limit_result(query: &str, intent: &str, state: &mut SearchState) -> Value {
    state.counter += 1;
    let round = json!({
        "query": query,
        "round": state.counter,
        "intent": if intent.trim().is_empty() { "general" } else { intent },
        "answer": "",
        "results": [],
        "status": "error",
        "error": WEB_SEARCH_LIMIT_ERROR,
        "retried": false,
        "retryQuery": "",
        "retryError": "",
        "cached": false,
    });
    compact_search_tool_result(&round, intent, state.citation_offset)
}

/// Mirrors `search_single_round`: the cache, then the retrying search, then the cache
/// write, then the projection the model sees.
#[allow(clippy::too_many_arguments)]
fn search_single_round(
    cleaned: &str,
    intent: &str,
    round_index: i64,
    citation_offset: i64,
    api_key: &str,
    root: &std::path::Path,
    transport: &Transport,
) -> Result<Value, deepseek_policy::app_error::AppError> {
    if cleaned.is_empty() {
        return Ok(compact_search_tool_result(
            &json!({"ok": false, "error": "Empty query", "query": "", "round": round_index,
                    "intent": intent, "results": []}),
            intent,
            citation_offset,
        ));
    }

    let now = now_epoch();
    // `use_cache=True` on this path, so a fresh cache entry short-circuits the search.
    if let Some(cached) = load_search_cache(root, cleaned, now) {
        if cached
            .get("results")
            .and_then(Value::as_array)
            .is_some_and(|items| !items.is_empty())
        {
            let mut round_data = cached;
            round_data["round"] = json!(round_index);
            round_data["status"] = json!("done");
            return Ok(compact_search_tool_result(
                &round_data,
                intent,
                citation_offset,
            ));
        }
    }

    let round_data = search_tavily_with_retry(cleaned, api_key, transport)?;
    let mut round_data = round_data;
    round_data["round"] = json!(round_index);
    if !round_data.get("status").is_some_and(Value::is_string) {
        round_data["status"] = json!("done");
    }

    if round_data
        .get("results")
        .and_then(Value::as_array)
        .is_some_and(|items| !items.is_empty())
    {
        cache_result(root, cleaned, &round_data, now);
    }

    Ok(compact_search_tool_result(
        &round_data,
        intent,
        citation_offset,
    ))
}

/// `save_search_cache`, with the failure swallowed: the oracle's cache write is
/// best-effort and must never fail a search that already succeeded.
fn cache_result(root: &std::path::Path, query: &str, round_data: &Value, now: i64) {
    // The oracle aggregates before saving; with one round that is the round plus the
    // derived envelope, so `save_search_cache` gets the round's own projection here.
    let _ = save_search_cache(root, query, round_data, now);
}

fn now_epoch() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or(0)
}

/// `SEARCH_CACHE_MAX_AGE_SECONDS` is re-exported so the gateway can document the
/// cache's lifetime without reaching into the policy crate's constants.
pub const CACHE_MAX_AGE_SECONDS: i64 = SEARCH_CACHE_MAX_AGE_SECONDS;

/// A callback with no provider configured reports "not enabled for this request"
/// rather than failing at call time — the same answer the oracle's `None` callback
/// produces, and the reason `ExecutorContext` treats `None` as an error rather than
/// a skip.
pub fn disabled_failure() -> ToolFailure {
    ToolFailure::app("web_search", "web_search is not enabled for this request")
}
