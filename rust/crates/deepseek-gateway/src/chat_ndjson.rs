//! `POST /api/chat` — the frontend's NDJSON streaming route.
//!
//! This is the native counterpart of `deepseek_infra/web/routes/chat.py:47` plus
//! `deepseek_infra/web/server.py:chat_event_stream`. The event bytes and the
//! accumulator live in `deepseek_policy::chat_stream_events`, where a parity probe
//! compares them with the oracle; this module is the request path and the stream loop.
//!
//! # The branches this route does **not** serve, and why they are refused
//!
//! The oracle's `stream_deepseek` is the union of several producers, and two of them
//! have no native implementation yet. A request that needs one is refused with a
//! structured error **before** any upstream call, rather than being served by a thinner
//! path that would look like success:
//!
//! | branch | predicate | why it is refused |
//! |---|---|---|
//! | agent mode | `payload.agentMode is true` | `stream_multi_agent` is a separate producer with its own event vocabulary (`agent`, `agent_delta`, `run_status`, `agent_plan`, …) |
//! | edge inference | `edge_route_for_payload(payload).use_edge` | the edge **routing** half of `edge_inference` is not ported (only its query-shape patterns are) |
//!
//! Forced search uses the retained native prefetch algorithm, emits live progress,
//! hardens external context and seeds the same request's tool-search state.
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
use deepseek_policy::request_messages::{
    ValidatedPayload, validate_deepseek_payload, validate_request_messages,
};
use futures_util::StreamExt;
use serde_json::{Value, json};

use crate::assembly_env::NativeAssembly;
use crate::chat_execution::{self, UpstreamConfig};
use crate::chat_stream::{UpstreamDelta, decode_event};
use crate::chat_tool_loop::{SuggestionCallback, ToolRoundExecutor};
use crate::may_write_native_store;
use crate::request_assembly::build_deepseek_request;
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

    let router = ModelRouterSettings::from_env();
    let validated = match validate_deepseek_payload(&payload, &state.api_key_fallback, &router) {
        Ok(validated) => validated,
        Err(error) => return app_error(error),
    };
    let streaming =
        deepseek_policy::core_utils::python_truthy(payload.get("stream").unwrap_or(&Value::Null));
    let prefetch = payload.get("searchEnabled") == Some(&Value::Bool(true))
        && deepseek_policy::search::forced_search_mode(&payload);
    let query = deepseek_policy::core_utils::latest_user_query(&payload);
    if prefetch && query.is_empty() {
        return app_error(AppError {
            message: "Search query is empty".into(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }

    let assembly = match NativeAssembly::from_env() {
        Ok(assembly) => assembly.with_router_settings(router.clone()),
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
    let (memory_state, consulted_file_index) =
        assembly.with_env(|env| -> Result<Value, AppError> {
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
            Ok(memory_state)
        });
    if consulted_file_index {
        return app_error(deepseek_policy::file_store::vector_index_not_ready());
    }
    let memory_state = match memory_state {
        Ok(memory_state) => memory_state,
        Err(error) => return app_error(error),
    };
    let mut config = UpstreamConfig::from_env();
    let executor = ToolRoundExecutor::from_env();
    if let Some(provider) = executor.search_provider() {
        provider.set_turn_query(query.clone());
    }
    if streaming && payload.get("agentMode") == Some(&Value::Bool(true)) {
        return crate::chat_multi_agent::response(
            SearchChatTurn {
                payload,
                validated,
                memory_state,
                assembly,
                streaming,
            },
            executor,
            config,
            query,
            router,
        )
        .await;
    }
    if !streaming || deepseek_policy::model_router::cascade_requested(&payload, &router) {
        let turn = SearchChatTurn {
            payload,
            validated,
            memory_state,
            assembly,
            streaming,
        };
        if streaming {
            return crate::chat_cascade::stream_response(
                turn, executor, query, config, prefetch, router,
            );
        }
        return match crate::chat_cascade::complete(
            turn, &executor, query, config, prefetch, &router,
        )
        .await
        {
            Ok(answer) => Json(answer).into_response(),
            Err(error) => app_error(error),
        };
    }
    if prefetch {
        return prefetched_ndjson_response(
            SearchChatTurn {
                payload,
                validated,
                memory_state,
                assembly,
                streaming,
            },
            executor,
            query,
            config,
        );
    }
    let (prepared, consulted_file_index) = assembly.with_env(|env| {
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
    ndjson_response(config, prepared.body, executor, upstream, &model, None)
}

/// Captured request context shared by complete cloud stages and stream producers.
#[derive(Clone)]
pub(crate) struct SearchChatTurn {
    pub payload: Value,
    pub validated: ValidatedPayload,
    pub memory_state: Value,
    pub assembly: NativeAssembly,
    pub(crate) streaming: bool,
}

pub(crate) async fn complete_nonstream(
    mut turn: SearchChatTurn,
    executor: &ToolRoundExecutor,
    query: String,
    mut config: UpstreamConfig,
    prefetch: bool,
) -> Result<Value, AppError> {
    if prefetch {
        let Some(provider) = executor.search_provider() else {
            return Err(AppError {
                message: "Search workspace is unavailable".into(),
                code: codes::INTERNAL,
                status: 500,
            });
        };
        let search =
            match tokio::task::spawn_blocking(move || provider.prefetch(&query, &mut |_| {})).await
            {
                Ok(Ok(search)) => search,
                Ok(Err(error)) => return Err(error),
                Err(_) => {
                    return Err(AppError {
                        message: "Search execution failed".into(),
                        code: codes::INTERNAL,
                        status: 500,
                    });
                }
            };
        set_search_context(&mut turn.payload, &search);
    }
    let (prepared, consulted) = turn.assembly.with_env(|env| {
        build_deepseek_request(
            &turn.payload,
            false,
            Some(&turn.memory_state),
            Some(&turn.validated),
            env,
        )
    });
    if consulted {
        return Err(deepseek_policy::file_store::vector_index_not_ready());
    }
    let prepared = prepared?;
    if !prepared.api_key.is_empty() {
        config.api_key = prepared.api_key.clone();
    }
    let completed =
        match crate::chat_tool_loop::execute_chat_turn(&config, &prepared.body, executor).await {
            Ok(completed) => completed,
            Err(error) => return Err(execution_app_error(error)),
        };
    let search = executor
        .search_provider()
        .and_then(|provider| provider.latest());
    let diagnostics = deepseek_policy::chat_diagnostics::diagnostics_with_usage(
        &deepseek_policy::chat_diagnostics::diagnostics_with_search(
            &deepseek_policy::chat_diagnostics::diagnostics_with_tools(
                &prepared.diagnostics,
                completed.tool_call_count,
                &completed.tool_names,
            ),
            search.as_ref(),
        ),
        &completed.usage,
    );
    let mut answer = json!({
        "id": completed.turn.id,
        "model": completed.turn.model,
        "content": completed.turn.content(),
        "reasoning": completed.turn.reasoning_content(),
        "usage": completed.usage,
        "diagnostics": diagnostics,
    });
    if search.is_some() {
        answer["search"] = deepseek_policy::search::search_for_client(search.as_ref());
    }
    if !completed.memory_suggestions.is_empty() {
        answer["memorySuggestions"] = json!(completed.memory_suggestions);
    }
    Ok(answer)
}

fn set_search_context(payload: &mut Value, search: &Value) {
    if search
        .get("results")
        .and_then(Value::as_array)
        .is_some_and(|results| !results.is_empty())
    {
        payload["searchContext"] = json!(deepseek_policy::context_taint::harden_search_context(
            &deepseek_policy::search::format_search_context(search),
            &ContextTaintSettings::from_env()
        ));
    } else if search.get("status").and_then(Value::as_str) == Some("error") {
        payload["searchContext"] = json!(deepseek_policy::search::format_search_failure_context(
            search
        ));
    }
}

fn prefetched_ndjson_response(
    mut turn: SearchChatTurn,
    executor: ToolRoundExecutor,
    query: String,
    mut config: UpstreamConfig,
) -> Response {
    let body_stream = async_stream::stream! {
        if let Some(first) = deepseek_policy::search::search_queries_for(&query).first() {
            yield Ok::<Bytes, std::io::Error>(Bytes::from(encode_stream_event(&ChatEvent::SystemNote {
                text: format!("已为本轮预取搜索结果。第一轮方向：{first}\n后续如需补充，会通过搜索工具继续查询。\n\n"),
            })));
        }
        let Some(provider) = executor.search_provider() else {
            yield Ok(Bytes::from(encode_stream_event(&ChatEvent::Error {
                error: "Search workspace is unavailable".into(), code: codes::INTERNAL.into(),
            })));
            return;
        };
        let (sender, mut progress) = tokio::sync::mpsc::channel::<Value>(8);
        let job = tokio::task::spawn_blocking(move || {
            provider.prefetch(&query, &mut |event| { let _ = sender.blocking_send(event.clone()); })
        });
        while let Some(data) = progress.recv().await {
            yield Ok(Bytes::from(encode_stream_event(&ChatEvent::Search {
                search: deepseek_policy::search::search_for_client(Some(&data)),
            })));
        }
        let search = match job.await {
            Ok(Ok(data)) => data,
            failure => {
                let (error, code) = match failure {
                    Ok(Err(error)) => (error.message, error.code),
                    _ => ("Search execution failed".into(), codes::INTERNAL),
                };
                yield Ok(Bytes::from(encode_stream_event(&ChatEvent::Error { error, code: code.into() })));
                return;
            }
        };
        let has_results = search.get("results").and_then(Value::as_array).is_some_and(|results| !results.is_empty());
        if has_results {
            let cached = deepseek_policy::core_utils::python_truthy(search.get("cached").unwrap_or(&Value::Null));
            yield Ok(Bytes::from(encode_stream_event(&ChatEvent::SystemNote {
                text: if cached { "已命中本地搜索缓存。我会复用已缓存的搜索来源继续回答。\n\n" }
                    else { "搜索预取已完成。我会结合来源继续推理；如信息不足，可再通过搜索工具补充。\n\n" }.into(),
            })));
            set_search_context(&mut turn.payload, &search);
        } else if search.get("status").and_then(Value::as_str) == Some("error") {
            let detail = search.get("rounds").and_then(Value::as_array).into_iter().flatten()
                .filter_map(|round| round.get("error").and_then(Value::as_str)).find(|error| !error.is_empty())
                .unwrap_or("未知错误");
            yield Ok(Bytes::from(encode_stream_event(&ChatEvent::SystemNote {
                text: format!("预取搜索失败：{detail}\n我会继续基于已有上下文回答；如有需要可再次通过搜索工具尝试。\n\n"),
            })));
            set_search_context(&mut turn.payload, &search);
        }
        let (prepared, consulted) = turn.assembly.with_env(|env| {
            build_deepseek_request(&turn.payload, turn.streaming, Some(&turn.memory_state), Some(&turn.validated), env)
        });
        let prepared = if consulted { Err(deepseek_policy::file_store::vector_index_not_ready()) } else { prepared };
        let prepared = match prepared {
            Ok(prepared) => prepared,
            Err(error) => {
                yield Ok(Bytes::from(encode_stream_event(&ChatEvent::Error { error: error.message, code: error.code.into() })));
                return;
            }
        };
        if !prepared.api_key.is_empty() { config.api_key = prepared.api_key.clone(); }
        let upstream = match chat_execution::open_chat_stream(&config, &prepared.body).await {
            Ok(upstream) => upstream,
            Err(error) => {
                let message = match &error {
                    chat_execution::ChatExecutionError::UpstreamStatus { status } =>
                        format!("upstream completion failed with status {status}"),
                    other => other.code().to_ascii_lowercase().replace('_', " "),
                };
                yield Ok(Bytes::from(encode_stream_event(&ChatEvent::Error { error: message, code: error.code().into() })));
                return;
            }
        };
        let model = prepared.body.get("model").and_then(Value::as_str).unwrap_or_default().to_string();
        let response = ndjson_response(config, prepared.body, executor, upstream, &model, Some(search));
        let mut stream = response.into_body().into_data_stream();
        while let Some(chunk) = stream.next().await {
            yield chunk.map_err(std::io::Error::other);
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

pub(crate) async fn stream_prepared_turn(
    turn: SearchChatTurn,
    executor: ToolRoundExecutor,
    mut config: UpstreamConfig,
    max_tool_rounds: usize,
) -> Result<Response, AppError> {
    let (prepared, consulted) = turn.assembly.with_env(|env| {
        build_deepseek_request(
            &turn.payload,
            true,
            Some(&turn.memory_state),
            Some(&turn.validated),
            env,
        )
    });
    if consulted {
        return Err(deepseek_policy::file_store::vector_index_not_ready());
    }
    let prepared = prepared?;
    config.api_key = prepared.api_key;
    let upstream = chat_execution::open_chat_stream(&config, &prepared.body)
        .await
        .map_err(execution_app_error)?;
    let model = prepared
        .body
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string();
    Ok(ndjson_response_with_limit(
        config,
        prepared.body,
        executor,
        upstream,
        &model,
        None,
        max_tool_rounds,
    ))
}

/// The NDJSON response: `application/x-ndjson`, one event per line.
fn ndjson_response(
    config: UpstreamConfig,
    prepared: Value,
    executor: ToolRoundExecutor,
    upstream: reqwest::Response,
    model: &str,
    initial_search: Option<Value>,
) -> Response {
    ndjson_response_with_limit(
        config,
        prepared,
        executor,
        upstream,
        model,
        initial_search,
        MAX_TOOL_ROUNDS,
    )
}

fn ndjson_response_with_limit(
    config: UpstreamConfig,
    prepared: Value,
    executor: ToolRoundExecutor,
    upstream: reqwest::Response,
    model: &str,
    initial_search: Option<Value>,
    max_tool_rounds: usize,
) -> Response {
    let model = model.to_string();
    let body_stream = async_stream::stream! {
        let mut accumulator = ChatStreamAccumulator::new();
        accumulator.observe_model(Some(&json!(model)));
        let mut latest_search = initial_search;
        if latest_search.is_some() {
            accumulator.observe_search(deepseek_policy::search::search_for_client(latest_search.as_ref()));
        }
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
        let on_suggestion: SuggestionCallback = Arc::new({
            let suggestions = suggestions.clone();
            move |suggestion: &Value| {
                suggestions
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner)
                    .push(suggestion.clone());
            }
        });

        for tool_round in 0..(max_tool_rounds + 2) {
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
            match decide_round(finalized.len(), tool_round, max_tool_rounds) {
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
                        .run_round_with_suggestions(finalized.clone(), Some(on_suggestion.clone()))
                        .await;
                    let current_search = executor.search_provider().and_then(|provider| provider.latest());
                    if current_search.is_some() && current_search != latest_search {
                        latest_search = current_search;
                        let search = deepseek_policy::search::search_for_client(latest_search.as_ref());
                        accumulator.observe_search(search.clone());
                        yield Ok(Bytes::from(encode_stream_event(&ChatEvent::Search { search })));
                    }
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
                    latest_search.as_ref(),
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
    app_error(execution_app_error(error))
}

pub(crate) fn execution_app_error(error: chat_execution::ChatExecutionError) -> AppError {
    let message = match &error {
        chat_execution::ChatExecutionError::UpstreamStatus { status } => {
            format!("upstream completion failed with status {status}")
        }
        other => other.code().to_ascii_lowercase().replace('_', " "),
    };
    AppError {
        message,
        code: error.code(),
        status: error.status(),
    }
}
