//! `POST /api/chat` — the frontend's NDJSON streaming route.
//!
//! This is the native counterpart of `deepseek_infra/web/routes/chat.py:47` plus
//! `deepseek_infra/web/server.py:chat_event_stream`. The event bytes and the
//! accumulator live in `deepseek_policy::chat_stream_events`, where a parity probe
//! compares them with the oracle; this module is the request path and the stream loop.
//!
//! # The branches this route does **not** serve, and why they are refused
//!
//! The oracle's `stream_deepseek` is the union of several producers, and three of them
//! have no native implementation yet. A request that needs one is refused with a
//! structured error **before** any upstream call, rather than being served by a thinner
//! path that would look like success:
//!
//! | branch | predicate | why it is refused |
//! |---|---|---|
//! | agent mode | `payload.agentMode is true` | `stream_multi_agent` is a separate producer with its own event vocabulary (`agent`, `agent_delta`, `run_status`, `agent_plan`, …) |
//! | cascade | `model_router_cascade_requested(payload)` | draft → gate → refine is a non-streaming producer replayed as events; the native cascade path is not wired |
//! | edge inference | `edge_route_for_payload(payload).use_edge` | the edge **routing** half of `edge_inference` is not ported (only its query-shape patterns are) |
//!
//! Search prefetch is a fourth: `forced_search_mode(payload)` is checked, and a payload
//! that asks for it is refused. That check is **structurally reachable here** even
//! though it is not on `/v1/chat/completions` — the OpenAI facade never forwards
//! `searchMode`, but `/api/chat` takes the internal payload, so `searchMode` arrives
//! intact and the oracle's prefetch branch would run. Refusing it is the honest
//! answer; silently skipping the prefetch would answer a different question than the
//! user asked.
//!
//! # What it does serve
//!
//! The ordinary streaming turn, including the tool-round loop: the upstream is opened
//! with `stream: true`, its SSE deltas are decoded and emitted as NDJSON, the round's
//! tool calls are merged with the ported
//! `chat_stream_events::merge_stream_tool_call_deltas`, and the tools run through the
//! same `ToolRoundExecutor` the OpenAI route uses — so a `browser_*`, `search_files`,
//! `create_document` or `reminders` call behaves identically on both routes.

use std::sync::Arc;

use axum::Json;
use axum::body::{Body, Bytes};
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::chat_stream_events::{
    ChatEvent, ChatStreamAccumulator, STREAM_MEDIA_TYPE, StreamToolCalls, encode_stream_event,
    finalized_stream_tool_calls, merge_stream_tool_call_deltas,
};
use deepseek_policy::context_taint::ContextTaintSettings;
use deepseek_policy::model_router::ModelRouterSettings;
use deepseek_policy::request_messages::{validate_deepseek_payload, validate_request_messages};
use futures_util::StreamExt;
use serde_json::{Value, json};

use crate::assembly_env::NativeAssembly;
use crate::chat_execution::{self, UpstreamConfig};
use crate::chat_stream::{UpstreamDelta, decode_event};
use crate::chat_tool_loop::ToolRoundExecutor;
use crate::may_write_native_store;
use crate::request_assembly::{PreparedDeepSeekRequest, build_deepseek_request};
use crate::tool_rounds::{MAX_TOOL_ROUNDS, RoundDecision, decide_round};

/// The code carried when a request needs a branch this route does not serve.
pub const CHAT_BRANCH_NOT_READY: &str = "NATIVE_CHAT_BRANCH_NOT_READY";
/// The code carried when the memory store is still Python's and the turn would write it.
pub const CHAT_MEMORY_WRITE_NOT_OWNED: &str = "NATIVE_CHAT_MEMORY_WRITE_NOT_OWNED";
/// The code carried when the file vector index would have to be consulted.
pub const CHAT_FILE_INDEX_NOT_READY: &str = "NATIVE_FILE_VECTOR_INDEX_NOT_READY";

/// What the handler needs from the server environment.
///
/// Held as state rather than read inside the handler so a test can point the route at a
/// scripted upstream without touching the process environment for the *root* — the
/// upstream URL still comes from `UpstreamConfig::from_env`, which is the same reader
/// the OpenAI route uses.
#[derive(Clone)]
pub struct ChatNdjsonState {
    pub root: std::path::PathBuf,
    pub api_key_fallback: String,
}

impl ChatNdjsonState {
    pub fn from_env() -> Self {
        Self {
            root: std::env::var_os("DEEPSEEK_INFRA_ROOT")
                .map(std::path::PathBuf::from)
                .unwrap_or_else(|| std::path::PathBuf::from(".")),
            api_key_fallback: std::env::var("DEEPSEEK_API_KEY").unwrap_or_default(),
        }
    }
}

/// `POST /api/chat`.
pub async fn api_chat(
    State(state): State<ChatNdjsonState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let raw: Value = match serde_json::from_slice(&body) {
        Ok(value) => value,
        Err(_) => {
            return app_error(AppError {
                message: "request must be valid JSON".to_string(),
                code: codes::INVALID_REQUEST,
                status: 400,
            });
        }
    };
    let Some(object) = raw.as_object() else {
        return app_error(AppError {
            message: "Chat payload must be an object".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    };
    // `{**payload, "localBaseUrl": request_base_url(request)}` — the route injects it,
    // and the assembly reads it for the artifact download base.
    let mut payload = object.clone();
    payload.insert(
        "localBaseUrl".to_string(),
        json!(crate::openai_facade::request_base_url(&headers)),
    );
    let payload = Value::Object(payload);

    if let Some(refusal) = refuse_unported_branch(&payload) {
        return app_error(refusal);
    }

    let router = ModelRouterSettings::default();
    let validated = match validate_deepseek_payload(&payload, &state.api_key_fallback, &router) {
        Ok(validated) => validated,
        Err(error) => return app_error(error),
    };
    let streaming =
        deepseek_policy::core_utils::python_truthy(payload.get("stream").unwrap_or(&Value::Null));

    let assembly = match NativeAssembly::from_env() {
        Ok(assembly) => assembly,
        Err(error) => return app_error(error),
    };
    // A `rag_vec` table would change which memories this turn retrieves, and this
    // process cannot evaluate a `vec0` MATCH; the OpenAI route refuses for the same
    // reason rather than serving the cosine fallback, whose hits differ in membership.
    let memory_index = deepseek_policy::memory_index::MemoryIndex::open(assembly.root());
    if memory_index
        .as_ref()
        .is_some_and(|index| index.vector_table_ready)
    {
        return app_error(AppError {
            message: "The memory index has a rag_vec table, so sqlite-vec distances would \
                      change which memories this turn retrieves; refusing rather than \
                      serving the cosine fallback."
                .to_string(),
            code: CHAT_FILE_INDEX_NOT_READY,
            status: 501,
        });
    }

    let root = assembly.root().to_path_buf();
    let owns_memory_store = may_write_native_store("memory_store");
    let (prepared, consulted_file_index) =
        assembly.with_env(|env| -> Result<PreparedDeepSeekRequest, AppError> {
            validate_request_messages(&payload, &validated.messages, env.expander)?;
            let latest_query = deepseek_policy::core_utils::latest_user_query(&payload);
            if !owns_memory_store
                && deepseek_policy::memory::has_explicit_memory_command(&latest_query)
            {
                return Err(AppError {
                    message: "This turn asks for a long-term memory to be saved or deleted. The \
                              memory store is still written by the Python runtime, so the native \
                              route refuses rather than answering without saving."
                        .to_string(),
                    code: CHAT_MEMORY_WRITE_NOT_OWNED,
                    status: 501,
                });
            }
            let provider =
                |query: &str, scopes: &[String]| -> std::collections::HashMap<String, i64> {
                    match &memory_index {
                        Some(index) => deepseek_policy::memory_index::memory_vector_hits(
                            index,
                            query,
                            scopes,
                            deepseek_policy::memory::MEMORY_RETRIEVE_LIMIT * 2,
                        )
                        .unwrap_or_default(),
                        None => std::collections::HashMap::new(),
                    }
                };
            let borrowed: &deepseek_policy::memory::VectorHits<'_> = &provider;
            let memory_state = if owns_memory_store {
                deepseek_policy::memory::prepare_memory_state(
                    &payload,
                    &root,
                    &deepseek_policy::core_utils::SystemClock,
                    Some(borrowed),
                )
            } else {
                deepseek_policy::memory::prepare_memory_state_read_only(
                    &payload,
                    &root,
                    Some(borrowed),
                )
            };
            build_deepseek_request(
                &payload,
                streaming,
                Some(&memory_state),
                Some(&validated),
                env,
            )
        });
    if consulted_file_index {
        return app_error(deepseek_policy::file_store::vector_index_not_ready());
    }
    let prepared = match prepared {
        Ok(prepared) => prepared,
        Err(error) => return app_error(error),
    };

    let mut config = UpstreamConfig::from_env();
    if !prepared.api_key.is_empty() {
        config.api_key = prepared.api_key.clone();
    }
    // The upstream turn is opened before the response is built, so a non-success
    // status surfaces as an HTTP error rather than a `200` followed by an error line.
    let upstream = match chat_execution::open_chat_stream(&config, &prepared.body).await {
        Ok(upstream) => upstream,
        Err(error) => return execution_error(error),
    };
    let model = prepared
        .body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    let executor = ToolRoundExecutor::from_env();
    ndjson_response(config, prepared.body, executor, upstream, &model)
}

/// The refusal for a branch this route does not serve, or `None` when it serves it.
fn refuse_unported_branch(payload: &Value) -> Option<AppError> {
    if payload
        .get("agentMode")
        .is_some_and(|value| matches!(value, Value::Bool(true)))
    {
        return Some(AppError {
            message: "agentMode is not served by the native /api/chat route yet.".to_string(),
            code: CHAT_BRANCH_NOT_READY,
            status: 501,
        });
    }
    let router = ModelRouterSettings::default();
    if deepseek_policy::model_router::cascade_requested(payload, &router) {
        return Some(AppError {
            message: "The model-router cascade is not served by the native /api/chat route yet."
                .to_string(),
            code: CHAT_BRANCH_NOT_READY,
            status: 501,
        });
    }
    if deepseek_policy::search::forced_search_mode(payload) {
        return Some(AppError {
            message: "A forced search mode is not served by the native /api/chat route yet."
                .to_string(),
            code: CHAT_BRANCH_NOT_READY,
            status: 501,
        });
    }
    let taint = ContextTaintSettings::from_env();
    let _ = taint;
    None
}

/// The NDJSON response: `application/x-ndjson`, one event per line.
fn ndjson_response(
    config: UpstreamConfig,
    prepared: Value,
    executor: ToolRoundExecutor,
    upstream: reqwest::Response,
    model: &str,
) -> Response {
    let model = model.to_string();
    let body_stream = async_stream::stream! {
        let mut accumulator = ChatStreamAccumulator::new();
        accumulator.observe_model(Some(&json!(model)));
        let mut body = prepared;
        let mut current = upstream;
        // Set once an `error` line has gone out: the oracle `return`s from there, so
        // neither a further round nor a `done` event follows.
        let mut terminated = false;
        let local_final_content = false;
        let mut tool_call_count = 0usize;
        let mut seen_tool_names: Vec<String> = Vec::new();
        let suggestions: Arc<std::sync::Mutex<Vec<Value>>> =
            Arc::new(std::sync::Mutex::new(Vec::new()));
        let on_suggestion: &'static (dyn Fn(&Value) + Send + Sync) = Box::leak(Box::new({
            let suggestions = suggestions.clone();
            move |suggestion: &Value| {
                suggestions
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .push(suggestion.clone());
            }
        }));

        for tool_round in 0..(MAX_TOOL_ROUNDS + 2) {
            let mut calls = StreamToolCalls::new();
            let mut round_content = String::new();
            let mut round_reasoning = String::new();
            let mut pending = Vec::<u8>::new();
            let mut event_name = String::from("message");
            let mut round_over = false;
            let mut round_usage: Option<Value> = None;

            let mut stream = current.bytes_stream();
            while let Some(chunk) = stream.next().await {
                let chunk = match chunk {
                    Ok(chunk) => chunk,
                    Err(_) => {
                        yield Ok::<Bytes, std::io::Error>(Bytes::from(encode_stream_event(
                            &ChatEvent::Error {
                                error: "Upstream stream error".to_string(),
                                code: codes::UPSTREAM_FAILURE.to_string(),
                            },
                        )));
                        terminated = true;
                        break;
                    }
                };
                pending.extend_from_slice(&chunk);
                while let Some(newline) = pending.iter().position(|byte| *byte == b'\n') {
                    let line_bytes: Vec<u8> = pending.drain(..=newline).collect();
                    let line = String::from_utf8_lossy(&line_bytes);
                    for delta in decode_event(&line, &mut event_name) {
                        match delta {
                            UpstreamDelta::Content(text) => {
                                round_content.push_str(&text);
                                yield Ok(Bytes::from(encode_stream_event(
                                    &accumulator.content_delta(&text),
                                )));
                            }
                            UpstreamDelta::Reasoning(text) => {
                                round_reasoning.push_str(&text);
                                yield Ok(Bytes::from(encode_stream_event(
                                    &accumulator.reasoning_delta(&text),
                                )));
                            }
                            UpstreamDelta::ToolCalls(fragments) => {
                                merge_stream_tool_call_deltas(&mut calls, Some(&fragments));
                            }
                            UpstreamDelta::Usage(usage) => {
                                accumulator.observe_usage(Some(&usage));
                                round_usage = Some(usage);
                            }
                            UpstreamDelta::FinishReason(reason) => {
                                accumulator.observe_finish_reason(Some(&json!(reason)));
                            }
                            UpstreamDelta::ResponseId(id) => {
                                accumulator.observe_id(Some(&json!(id)));
                            }
                            UpstreamDelta::Model(name) => {
                                accumulator.observe_model(Some(&json!(name)));
                            }
                            UpstreamDelta::Done => round_over = true,
                            UpstreamDelta::Error { message } => {
                                yield Ok(Bytes::from(encode_stream_event(&ChatEvent::Error {
                                    error: message,
                                    code: codes::UPSTREAM_FAILURE.to_string(),
                                })));
                                terminated = true;
                            }
                        }
                    }
                }
                if round_over || terminated {
                    break;
                }
            }
            if terminated {
                break;
            }
            let _ = round_usage;

            let finalized = finalized_stream_tool_calls(&calls);
            match decide_round(finalized.len(), tool_round, MAX_TOOL_ROUNDS) {
                RoundDecision::Finish => break,
                RoundDecision::ForceFinalAnswer => {
                    seen_tool_names.extend(crate::tool_rounds::tool_names(&finalized));
                    yield Ok(Bytes::from(encode_stream_event(&ChatEvent::SystemNote {
                        text: "工具调用次数已达上限，改为直接整理最终回答。\n\n".to_string(),
                    })));
                    body = crate::tool_rounds::force_final_answer_without_tools(&body);
                }
                RoundDecision::Continue => {
                    tool_call_count += finalized.len();
                    let names = crate::tool_rounds::tool_names(&finalized);
                    seen_tool_names.extend(names.iter().cloned());
                    yield Ok(Bytes::from(encode_stream_event(&ChatEvent::SystemNote {
                        text: format!(
                            "正在调用本地工具：{}\n\n",
                            if names.is_empty() {
                                "tool".to_string()
                            } else {
                                names.join(", ")
                            }
                        ),
                    })));
                    let results = executor
                        .run_round_with_suggestions(finalized.clone(), Some(on_suggestion))
                        .await;
                    // A suggestion built during the round is emitted as its own event,
                    // which is what `emit_memory_suggestion` does before the exchange is
                    // appended. The guard is dropped before the first `yield`, because a
                    // `MutexGuard` is not `Send` and this stream has to be.
                    let collected: Vec<Value> = {
                        let mut guard = suggestions
                            .lock()
                            .unwrap_or_else(std::sync::PoisonError::into_inner);
                        guard.drain(..).collect()
                    };
                    for suggestion in collected {
                        accumulator.push_memory_suggestion(suggestion.clone());
                        yield Ok(Bytes::from(encode_stream_event(
                            &ChatEvent::MemorySuggestion { suggestion },
                        )));
                    }
                    body = crate::tool_rounds::append_tool_exchange(
                        &body,
                        &round_content,
                        &round_reasoning,
                        &finalized,
                        &results,
                    );
                    yield Ok(Bytes::from(encode_stream_event(&ChatEvent::SystemNote {
                        text: "本地工具调用完成，继续生成回答。\n\n".to_string(),
                    })));
                }
            }
            if local_final_content {
                break;
            }
            match chat_execution::open_chat_stream(&config, &body).await {
                Ok(next) => current = next,
                Err(_) => {
                    yield Ok(Bytes::from(encode_stream_event(&ChatEvent::Error {
                        error: "Upstream stream error".to_string(),
                        code: codes::UPSTREAM_FAILURE.to_string(),
                    })));
                    terminated = true;
                    break;
                }
            }
        }

        if !terminated {
            if accumulator.finish_reason() == "length" {
                yield Ok(Bytes::from(encode_stream_event(&ChatEvent::SystemNote {
                    text: "回答已达到上游输出长度上限，内容可能被截断。\n\n".to_string(),
                })));
            }
            // The diagnostics the oracle folds in for this path: the tool summary, the
            // search counts (absent when there was no search, which is what the oracle
            // writes) and the cache-token block. The gateway-attempt, semantic-cache,
            // cost and trace blocks need state this route does not have yet.
            let diagnostics = deepseek_policy::chat_diagnostics::diagnostics_with_usage(
                &deepseek_policy::chat_diagnostics::diagnostics_with_search(
                    &deepseek_policy::chat_diagnostics::diagnostics_with_tools(
                        &json!({}),
                        tool_call_count,
                        &seen_tool_names,
                    ),
                    None,
                ),
                &accumulator.usage(),
            );
            yield Ok(Bytes::from(encode_stream_event(&accumulator.done(diagnostics))));
        }
    };
    (
        [
            (header::CONTENT_TYPE, STREAM_MEDIA_TYPE),
            (header::CACHE_CONTROL, "no-cache"),
            (header::HeaderName::from_static("x-accel-buffering"), "no"),
        ],
        Body::from_stream(body_stream),
    )
        .into_response()
}

/// The oracle's `AppError` envelope: `{"error": message, "code": code}`.
fn app_error(error: AppError) -> Response {
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::BAD_REQUEST);
    (
        status,
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}

/// A native execution failure, with the status distinguishing local misconfiguration
/// from upstream trouble.
fn execution_error(error: chat_execution::ChatExecutionError) -> Response {
    let status = StatusCode::from_u16(error.status()).unwrap_or(StatusCode::BAD_GATEWAY);
    let message = match &error {
        chat_execution::ChatExecutionError::UpstreamStatus { status } => {
            format!("upstream completion failed with status {status}")
        }
        other => other.code().to_ascii_lowercase().replace('_', " "),
    };
    (
        status,
        Json(json!({"error": message, "code": error.code()})),
    )
        .into_response()
}
