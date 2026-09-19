use axum::{
    Json, Router,
    body::Bytes,
    extract::DefaultBodyLimit,
    http::{HeaderMap, StatusCode, header::CONTENT_TYPE},
    middleware,
    response::{IntoResponse, Response},
    routing::{any, get, post},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{collections::HashMap, io, path::Path as FsPath};

use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::{SystemClock, latest_user_query, python_truthy};
use deepseek_policy::memory;
use deepseek_policy::model_router::ModelRouterSettings;
use deepseek_policy::request_messages::validate_request_messages;

pub mod assembly_env;
mod auth;
pub mod chat_execution;
pub mod chat_stream;
pub mod chat_tool_loop;
mod control_proxy;
pub mod local_clock;
pub mod native_chat;
pub mod observability;
pub mod openai_facade;
pub mod policy_routes;
pub mod request_assembly;
pub mod request_preparation;
pub mod search_provider;
pub mod static_files;
pub mod tool_rounds;

pub fn gateway_version() -> &'static str {
    deepseek_core::version_info().version
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HealthzResponse {
    pub ok: bool,
    pub service: String,
}

pub fn create_app() -> Router {
    apply_gateway_layers(create_routes())
}

fn create_routes() -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/metrics", get(observability::metrics))
        .route("/v1/models", get(models))
        .route("/v1/chat/completions", post(chat_completions))
        .route("/gateway/request/prepare", post(gateway_request_prepare))
        .route("/mcp", post(mcp_rpc))
        .route("/mcp/request/prepare", post(mcp_protocol_prepare))
        .route("/.well-known/agent-card.json", get(agent_card))
        .route("/a2a", post(a2a_rpc))
        .route("/api/*path", any(control_proxy::proxy_api_to_go))
        // Private control handlers must not fall through to either proxy or SPA.
        .route("/internal", any(|| async { StatusCode::NOT_FOUND }))
        .route("/internal/", any(|| async { StatusCode::NOT_FOUND }))
        .route("/internal/*path", any(|| async { StatusCode::NOT_FOUND }))
        .route("/rag/query/normalize", post(rag_query_normalize))
        .route("/rag/chunks/score", post(rag_chunks_score))
        .route("/rag/vectors/rank", post(rag_vectors_rank))
        .route("/rag/vectors/rank-binary", post(rag_vectors_rank_binary))
        .route("/rag/citation/format", post(rag_citation_format))
        .route("/rag/index/validate", post(rag_index_validate))
        .route("/rag/documents/prepare", post(rag_document_prepare))
        .merge(policy_routes::router())
}

fn apply_gateway_layers(router: Router) -> Router {
    router
        .layer(DefaultBodyLimit::max(
            deepseek_rag::document_preparation::MAX_REQUEST_BYTES + 1_000_000,
        ))
        .layer(middleware::from_fn(observability::observe_sidecar_request))
}

pub fn create_production_app(static_root: impl AsRef<FsPath>) -> io::Result<Router> {
    create_production_app_with_auth(static_root, auth::ProductionAuth::from_env())
}

fn create_production_app_with_auth(
    static_root: impl AsRef<FsPath>,
    auth_config: auth::ProductionAuth,
) -> io::Result<Router> {
    let static_files = static_files::StaticFiles::load(static_root)?;
    Ok(apply_gateway_layers(
        create_routes().fallback(move |request: axum::extract::Request| {
            let static_files = static_files.clone();
            async move { static_files.serve(request).await }
        }),
    )
    .layer(middleware::from_fn_with_state(
        auth_config,
        auth::require_production_auth,
    )))
}

fn unavailable(code: &'static str, message: &'static str) -> (StatusCode, Json<serde_json::Value>) {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        Json(json!({
            "error": {
                "code": code,
                "message": message,
            }
        })),
    )
}

async fn agent_card() -> (StatusCode, Json<serde_json::Value>) {
    (
        StatusCode::OK,
        Json(json!({
            "protocolVersion": "0.3.0",
            "name": "DeepSeek Infra Orchestrator",
            "description": "Native agent discovery; A2A execution is not wired",
            "url": "/a2a",
            "preferredTransport": "JSONRPC",
            "version": gateway_version(),
            "capabilities": {
                "streaming": false,
                "pushNotifications": false,
                "stateTransitionHistory": false
            },
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": []
        })),
    )
}

fn jsonrpc_not_ready(body: &Bytes, message: &'static str) -> Json<serde_json::Value> {
    if body.is_empty() {
        return Json(json!({
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request: empty body"},
            "id": null
        }));
    }
    let val: serde_json::Value = match serde_json::from_slice(body) {
        Ok(v) => v,
        Err(_) => {
            return Json(json!({
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error"},
                "id": null
            }));
        }
    };
    if !val.is_object() {
        return Json(json!({
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request"},
            "id": null
        }));
    }
    let id = val.get("id").cloned().unwrap_or(serde_json::Value::Null);
    Json(json!({
        "jsonrpc": "2.0",
        "error": {"code": -32601, "message": message},
        "id": id
    }))
}

const MCP_PROTOCOL_VERSION: &str = "2025-06-18";

fn jsonrpc_result(id: serde_json::Value, result: serde_json::Value) -> Json<serde_json::Value> {
    Json(json!({
        "jsonrpc": "2.0",
        "id": id,
        "result": result,
    }))
}

async fn mcp_rpc(body: Bytes) -> Response {
    if body.is_empty() {
        return jsonrpc_not_ready(&body, "native MCP execution is not wired").into_response();
    }
    let val: serde_json::Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(_) => {
            return jsonrpc_not_ready(&body, "native MCP execution is not wired").into_response();
        }
    };
    if !val.is_object() {
        return jsonrpc_not_ready(&body, "native MCP execution is not wired").into_response();
    }
    let method = val
        .get("method")
        .and_then(serde_json::Value::as_str)
        .unwrap_or("");
    let notification = val.get("id").is_none();
    if method == "notifications/initialized" && notification {
        return StatusCode::ACCEPTED.into_response();
    }
    let id = val.get("id").cloned().unwrap_or(serde_json::Value::Null);
    match method {
        "initialize" => jsonrpc_result(
            id,
            json!({
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": false}},
                "serverInfo": {
                    "name": "deepseek-infra",
                    "title": "DeepSeek Infra MCP Tool Hub",
                    "version": gateway_version(),
                },
            }),
        )
        .into_response(),
        "ping" => jsonrpc_result(id, json!({})).into_response(),
        "tools/list" | "tools/call" | "resources/list" | "resources/read" | "prompts/list"
        | "prompts/get" => {
            jsonrpc_not_ready(&body, "native MCP tool execution is not wired").into_response()
        }
        _ => jsonrpc_not_ready(&body, "native MCP execution is not wired").into_response(),
    }
}

async fn a2a_rpc(body: Bytes) -> Json<serde_json::Value> {
    jsonrpc_not_ready(&body, "native A2A execution is not wired")
}

async fn gateway_request_prepare(body: Bytes) -> Json<serde_json::Value> {
    if body.len() > request_preparation::MAX_REQUEST_BYTES {
        return Json(
            request_preparation::PreparationError {
                code: "request_too_large",
                message: "request exceeds the preparation budget",
            }
            .response(),
        );
    }
    let value: serde_json::Value = match serde_json::from_slice(&body) {
        Ok(value) => value,
        Err(_) => {
            return Json(
                request_preparation::PreparationError {
                    code: "invalid_request",
                    message: "request must be valid JSON",
                }
                .response(),
            );
        }
    };
    match request_preparation::prepare_request(&value) {
        Ok(request) => Json(json!({
            "ok": true,
            "request": request,
            "diagnostics": {"runtime": "rust", "normalized": true}
        })),
        Err(error) => Json(error.response()),
    }
}

async fn mcp_protocol_prepare(body: Bytes) -> Json<serde_json::Value> {
    Json(deepseek_mcp::prepare_protocol_bytes(&body))
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagQueryNormalizeRequest {
    pub query: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagQueryNormalizeResponse {
    pub normalized: String,
    pub tokens: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagChunkScoreRequest {
    pub query: String,
    pub chunks: Vec<deepseek_rag::chunk::RagChunk>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagChunkScoreResponse {
    pub ranked: Vec<RagChunkScoreEntry>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagChunkScoreEntry {
    pub id: String,
    pub score: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagVectorRankRequest {
    pub query: Vec<f64>,
    pub candidates: Vec<Vec<f64>>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagVectorRankResponse {
    pub index: Option<usize>,
    pub similarity: f64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagCitationFormatRequest {
    pub source: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub start_line: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub end_line: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagCitationFormatResponse {
    pub citation: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagIndexValidateRequest {
    pub chunks: Vec<deepseek_rag::chunk::RagChunk>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RagIndexValidateResponse {
    pub valid: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

async fn rag_query_normalize(
    Json(req): Json<RagQueryNormalizeRequest>,
) -> Result<Json<RagQueryNormalizeResponse>, (StatusCode, Json<serde_json::Value>)> {
    match deepseek_rag::query::parse_query(&req.query) {
        Ok(query) => Ok(Json(RagQueryNormalizeResponse {
            normalized: query.normalized,
            tokens: query.tokens,
        })),
        Err(err) => Err((
            StatusCode::BAD_REQUEST,
            Json(json!({"error": err.to_string()})),
        )),
    }
}

async fn rag_chunks_score(
    Json(req): Json<RagChunkScoreRequest>,
) -> Result<Json<RagChunkScoreResponse>, (StatusCode, Json<serde_json::Value>)> {
    let query = match deepseek_rag::query::parse_query(&req.query) {
        Ok(q) => q,
        Err(err) => {
            return Err((
                StatusCode::BAD_REQUEST,
                Json(json!({"error": err.to_string()})),
            ));
        }
    };
    let ranked = deepseek_rag::score::rank_chunks(&query, &req.chunks);
    Ok(Json(RagChunkScoreResponse {
        ranked: ranked
            .into_iter()
            .map(|(id, score)| RagChunkScoreEntry { id, score })
            .collect(),
    }))
}

async fn rag_vectors_rank(Json(req): Json<RagVectorRankRequest>) -> Json<RagVectorRankResponse> {
    let best = deepseek_rag::vector::best_match(&req.query, &req.candidates);
    Json(RagVectorRankResponse {
        index: best.map(|(index, _)| index),
        similarity: best.map_or(0.0, |(_, similarity)| similarity),
    })
}

fn vector_binary_error(
    status: StatusCode,
    error: deepseek_rag::vector_binary::BinaryError,
) -> Response {
    (
        status,
        [(CONTENT_TYPE, "application/json")],
        Json(json!({
            "ok": false,
            "code": error.code(),
            "message": error.message(),
        })),
    )
        .into_response()
}

async fn rag_vectors_rank_binary(headers: HeaderMap, body: Bytes) -> Response {
    let expected = deepseek_rag::vector_binary::CONTENT_TYPE;
    let valid_content_type = headers
        .get(CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .is_some_and(|value| value.eq_ignore_ascii_case(expected));
    if !valid_content_type {
        return (
            StatusCode::UNSUPPORTED_MEDIA_TYPE,
            [(CONTENT_TYPE, "application/json")],
            Json(json!({
                "ok": false,
                "code": "invalid_content_type",
                "message": "binary vector ranking requires the versioned binary content type",
            })),
        )
            .into_response();
    }
    let request = match deepseek_rag::vector_binary::decode_request(&body) {
        Ok(request) => request,
        Err(error) => {
            let status = if error == deepseek_rag::vector_binary::BinaryError::PayloadTooLarge {
                StatusCode::PAYLOAD_TOO_LARGE
            } else {
                StatusCode::BAD_REQUEST
            };
            return vector_binary_error(status, error);
        }
    };
    let best = match request.best_match() {
        Ok(best) => best,
        Err(error) => return vector_binary_error(StatusCode::UNPROCESSABLE_ENTITY, error),
    };
    let response = match deepseek_rag::vector_binary::encode_response(best) {
        Ok(response) => response,
        Err(error) => return vector_binary_error(StatusCode::UNPROCESSABLE_ENTITY, error),
    };
    (
        StatusCode::OK,
        [(CONTENT_TYPE, deepseek_rag::vector_binary::CONTENT_TYPE)],
        response,
    )
        .into_response()
}

async fn rag_citation_format(
    Json(req): Json<RagCitationFormatRequest>,
) -> Result<Json<RagCitationFormatResponse>, (StatusCode, Json<serde_json::Value>)> {
    match deepseek_rag::citation::format_citation(&req.source, req.start_line, req.end_line) {
        Ok(citation) => Ok(Json(RagCitationFormatResponse { citation })),
        Err(err) => Err((
            StatusCode::BAD_REQUEST,
            Json(json!({"error": err.to_string()})),
        )),
    }
}

async fn rag_index_validate(
    Json(req): Json<RagIndexValidateRequest>,
) -> Result<Json<RagIndexValidateResponse>, (StatusCode, Json<serde_json::Value>)> {
    let index = deepseek_rag::index::IndexMetadata { chunks: req.chunks };
    match deepseek_rag::index::validate_index_metadata(&index) {
        Ok(()) => Ok(Json(RagIndexValidateResponse {
            valid: true,
            error: None,
        })),
        Err(err) => Ok(Json(RagIndexValidateResponse {
            valid: false,
            error: Some(err.to_string()),
        })),
    }
}

async fn rag_document_prepare(body: Bytes) -> Json<serde_json::Value> {
    Json(deepseek_rag::document_preparation::prepare_document_bytes(
        &body,
    ))
}

async fn healthz() -> Json<HealthzResponse> {
    Json(HealthzResponse {
        ok: true,
        service: "deepseek-gateway-rs".to_string(),
    })
}

async fn models() -> (StatusCode, Json<serde_json::Value>) {
    let created = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0);
    (
        StatusCode::OK,
        Json(request_preparation::native_model_catalog(created)),
    )
}

/// What the composition produced for one `/v1/chat/completions` request.
struct NativeChat {
    /// The assembled upstream body — `build_deepseek_request`'s output, not a thin
    /// OpenAI→upstream mapping.
    body: serde_json::Value,
    /// The credential the assembly validated. The oracle sends *this*, not a fresh
    /// environment read, so an explicit `apiKey` in the payload is honoured and a
    /// missing one has already failed closed.
    api_key: String,
    streaming: bool,
    model: String,
}

/// `NATIVE_MEMORY_WRITE_NOT_OWNED` — a turn that would write the memory store, which Python still
/// owns. Raised in two places, because the store has two reachable write paths on this route: the
/// turn's own explicit command (`apply_explicit_memory_command`, checked here) and the model
/// calling `forget_memory` (checked in [`chat_tool_loop`]).
pub(crate) const MEMORY_WRITE_NOT_OWNED: &str = "NATIVE_MEMORY_WRITE_NOT_OWNED";
/// `NATIVE_MEMORY_VECTOR_INDEX_NOT_REPRODUCIBLE` — the deployment has a `rag_vec` table.
const MEMORY_VECTOR_INDEX_NOT_REPRODUCIBLE: &str = "NATIVE_MEMORY_VECTOR_INDEX_NOT_REPRODUCIBLE";

/// The data-plane stores this process is allowed to write.
///
/// Two separate questions decide a write, and this list is the second one:
///
/// 1. **Has the deployment de-authorised Python?** — [`python_is_de_authorised`], the same signal
///    `deepseek_infra/infra/native_runtime/authority.py:assert_python_writer_allowed` reads
///    (`DEEPSEEK_RUNTIME_MODE=python_disabled`, ADR-0049's 4.9.4). Deliberately **not**
///    `DEEPSEEK_GO_CONTROL=1`: that is the *control* plane's mode, and the ADR hands the control plane
///    over one domain at a time while the data plane can still be Python's — reading it as data-plane
///    ownership would put two writers on one store.
/// 2. **Has this store been declared and cut over?** — `release/native_runtime_ownership_v1.json`
///    lists it as python -> rust with a cutover. `memory_store` is declared. `reminders_store` is
///    **not**: `remind` appears nowhere in the contract, in `GO_CONTROL_DOMAINS` or in the command
///    codes, so the route refuses it whatever the mode says — setting the mode alone must not be able
///    to enable a store nobody has declared. A test pins this list against the contract.
pub(crate) const DECLARED_NATIVE_DATA_DOMAINS: [&str; 1] = ["memory_store"];

/// Whether this process may write the store `domain` names.
///
/// Both conditions above, and the order matters only for the reader: an undeclared store is refused
/// even in a fully native deployment.
pub(crate) fn may_write_native_store(domain: &str) -> bool {
    DECLARED_NATIVE_DATA_DOMAINS.contains(&domain) && python_is_de_authorised()
}

/// Whether the deployment has de-authorised Python — ADR-0049's 4.9.4 mode.
pub(crate) fn python_is_de_authorised() -> bool {
    python_is_de_authorised_in(&std::env::var("DEEPSEEK_RUNTIME_MODE").unwrap_or_default())
}

/// [`python_is_de_authorised`] over an explicit mode string, so the rule is testable without env.
pub(crate) fn python_is_de_authorised_in(mode: &str) -> bool {
    mode.trim().eq_ignore_ascii_case("python_disabled")
}

/// `NATIVE_REMINDERS_WRITE_NOT_OWNED` — a turn that would write the reminders store, which is
/// undeclared and still Python's. A sibling of [`MEMORY_WRITE_NOT_OWNED`] rather than a shared code:
/// the two stores have different cutovers, and a caller that sees this one knows which store it was.
pub(crate) const REMINDERS_WRITE_NOT_OWNED: &str = "NATIVE_REMINDERS_WRITE_NOT_OWNED";

async fn chat_completions(
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, (StatusCode, Json<serde_json::Value>)> {
    let raw: serde_json::Value = serde_json::from_slice(&body).map_err(|_| {
        (
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": {
                    "message": "request must be valid JSON",
                    "type": "invalid_request_error"
                }
            })),
        )
    })?;

    // The body is built the way the oracle builds it: the OpenAI facade translates the
    // request into the internal payload (dropping everything it does not forward), the
    // payload is validated, and only then is the upstream body assembled. Building it
    // straight from the OpenAI request — what this route used to do — forwards `tools`,
    // `tool_choice`, `max_tokens`, `top_p` and `reasoning_effort`, none of which the
    // oracle forwards, and omits `thinkingEnabled`/`localBaseUrl`.
    //
    // Facade and validation run **before** the workspace is bound: the oracle validates
    // inside `call_deepseek`, before anything reads a store, so a request with no user turn
    // must be a `400` whether or not this process has a workspace root.
    let router = ModelRouterSettings::default();
    let api_key_fallback = std::env::var("DEEPSEEK_API_KEY").unwrap_or_default();
    let base_url = openai_facade::request_base_url(&headers);
    let prepared = native_chat::prepare_openai_chat(&raw, &base_url, &router, &api_key_fallback)
        .map_err(openai_app_error)?;

    let assembly = assembly_env::NativeAssembly::from_env().map_err(openai_app_error)?;

    // A deployment that installed the optional `sqlite-vec` extra has a `rag_vec` table, and
    // the oracle would blend its distances into every memory score. This process cannot
    // evaluate a `vec0` MATCH, so it refuses rather than serving the cosine fallback, whose
    // hits differ in *membership*, not just order.
    let memory_index = deepseek_policy::memory_index::MemoryIndex::open(assembly.root());
    if memory_index
        .as_ref()
        .is_some_and(|index| index.vector_table_ready)
    {
        return Err(openai_app_error(AppError {
            message: "The memory index has a rag_vec table, so sqlite-vec distances would \
                      change which memories this turn retrieves; refusing rather than \
                      serving the cosine fallback."
                .to_string(),
            code: MEMORY_VECTOR_INDEX_NOT_REPRODUCIBLE,
            status: 501,
        }));
    }

    let root = assembly.root().to_path_buf();
    // The facade translation and the validation already ran, deliberately before the workspace
    // was bound, so this closure starts from that `prepared` rather than repeating the pair.
    let (outcome, consulted_file_index) =
        assembly.with_env(|env| -> Result<NativeChat, AppError> {
            let streaming = python_truthy(prepared.payload.get("stream").unwrap_or(&Value::Null));
            let model = prepared.validated.model.clone();

            // The message rules run here rather than with the validation above, and that placement is
            // forced: they are defined over `normalize_chat_messages`, whose first act is
            // `expanded_message_content(message)` (`deepseek_client.py:492`), so a turn that is blank
            // *before* expansion is not blank to them. They therefore need the expander, and the
            // expander needs the file cache. Running them before the memory read — instead of leaving
            // them to `build_deepseek_request` — is what keeps the oracle's order inside
            // `call_deepseek`: validate, message rules, memory, build.
            validate_request_messages(
                &prepared.payload,
                &prepared.validated.messages,
                env.expander,
            )?;

            // `.memory/memories.json` is a declared domain (`memory_store`, python -> rust, cutover
            // 4.9.4) and `one_table_one_authoritative_writer` is an invariant, so exactly one side
            // writes it. Which side is this turn's business, not a constant:
            //
            // - while Python is authoritative (`native_owns_memory_store()` false) this route must
            //   not write it, and answering a "记住: X" turn *without* saving would silently discard
            //   the instruction — so the turn is refused;
            // - once the mode says Python is de-authorised, the ported write half runs and this is
            //   the writer, which is what makes the handover a flip rather than a rewrite.
            let owns_memory_store = may_write_native_store("memory_store");
            let latest_query = latest_user_query(&prepared.payload);
            if !owns_memory_store && memory::has_explicit_memory_command(&latest_query) {
                return Err(AppError {
                    message: "This turn asks for a long-term memory to be saved or deleted. The \
                          memory store is still written by the Python runtime, so the native \
                          route refuses rather than answering without saving."
                        .to_string(),
                    code: MEMORY_WRITE_NOT_OWNED,
                    status: 501,
                });
            }

            let provider = |query: &str, scopes: &[String]| -> HashMap<String, i64> {
                match &memory_index {
                    // `vector_table_ready` was already refused above, so this read cannot be
                    // the un-reproducible branch; the fallback is unreachable rather than
                    // swallowed, which is why it is safe here.
                    Some(index) => deepseek_policy::memory_index::memory_vector_hits(
                        index,
                        query,
                        scopes,
                        memory::MEMORY_RETRIEVE_LIMIT * 2,
                    )
                    .unwrap_or_default(),
                    None => HashMap::new(),
                }
            };
            let borrowed: &memory::VectorHits<'_> = &provider;
            let memory_state = if owns_memory_store {
                // The writer path, and the oracle's own function: the explicit command runs *before*
                // the retrieval so a memory saved this turn is retrievable in it, and its notice
                // reaches the prompt.
                memory::prepare_memory_state(&prepared.payload, &root, &SystemClock, Some(borrowed))
            } else {
                memory::prepare_memory_state_read_only(&prepared.payload, &root, Some(borrowed))
            };
            let assembled =
                native_chat::assemble_openai_chat(prepared, streaming, &memory_state, env)?;
            Ok(NativeChat {
                body: assembled.body,
                api_key: assembled.api_key,
                streaming,
                model,
            })
        });

    // The flag is set by the very call the oracle would have served, so it cannot drift
    // from `expanded_message_content`'s own trigger the way a duplicated predicate would.
    if consulted_file_index {
        return Err(openai_app_error(
            deepseek_policy::file_store::vector_index_not_ready(),
        ));
    }
    let outcome = outcome.map_err(openai_app_error)?;

    // The upstream credential comes from the assembled request, which is where the oracle
    // takes it; the client never supplies one (the facade does not forward credentials).
    let mut config = chat_execution::UpstreamConfig::from_env();
    if !outcome.api_key.is_empty() {
        config.api_key = outcome.api_key.clone();
    }
    let model = outcome.model.clone();
    if outcome.streaming {
        // The upstream turn is opened *before* the response is built so a
        // non-success upstream status can surface as an HTTP error. Once the
        // stream is open the status line is already on the wire, so any later
        // failure has to travel as an SSE error frame instead.
        let upstream = chat_execution::open_chat_stream(&config, &outcome.body)
            .await
            .map_err(chat_execution_error)?;
        let created = now_unix_seconds();
        // Per request, exactly as on the non-streaming path: the executor's file
        // cache has to persist across this request's tool rounds, and the
        // workspace root and policy profile come from the server environment.
        let executor = chat_tool_loop::ToolRoundExecutor::from_env();
        return Ok(chat_stream::streaming_response(
            config,
            outcome.body,
            executor,
            upstream,
            &model,
            created,
        ));
    }
    // The tool executor is per request: its file cache persists across this
    // request's calls, which is the WorkspaceContext contract. The workspace
    // root and the policy profile come from the server environment.
    let executor = chat_tool_loop::ToolRoundExecutor::from_env();
    match chat_tool_loop::execute_chat_with_tool_rounds(&config, &outcome.body, &executor).await {
        Ok(result) => Ok(Json(chat_execution::openai_completion_response(
            &result,
            &model,
            now_unix_seconds(),
        ))
        .into_response()),
        Err(error) => Err(chat_execution_error(error)),
    }
}

/// Report an assembly failure in the oracle's envelope: `{"error": <message>, "code":
/// <code>}` with `AppError.status`.
///
/// This is the shape `AppError.to_response()` produces and the FastAPI handler returns, so
/// a client that reads `error` as a string and `code` as the machine-readable field sees
/// what the Python runtime already sends. The frozen REST inventory records this route but
/// no error envelope, so the shape is a compatibility choice rather than a frozen byte —
/// and it is the oracle's, which is the behaviour being preserved.
fn openai_app_error(error: AppError) -> (StatusCode, Json<serde_json::Value>) {
    let status = StatusCode::from_u16(error.status).unwrap_or(StatusCode::BAD_REQUEST);
    (
        status,
        Json(json!({"error": error.message, "code": error.code})),
    )
}

fn now_unix_seconds() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0)
}

/// Report a native execution failure with the OpenAI-compatible envelope and a
/// status that distinguishes local misconfiguration from upstream trouble.
fn chat_execution_error(
    error: chat_execution::ChatExecutionError,
) -> (StatusCode, Json<serde_json::Value>) {
    let status = StatusCode::from_u16(error.status()).unwrap_or(StatusCode::BAD_GATEWAY);
    let message = match &error {
        chat_execution::ChatExecutionError::UpstreamStatus { status } => {
            format!("upstream completion failed with status {status}")
        }
        other => other.code().to_ascii_lowercase().replace('_', " "),
    };
    (
        status,
        Json(json!({
            "error": {
                "code": error.code(),
                "message": message,
                "type": "upstream_error"
            }
        })),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::{Body, to_bytes};
    use axum::http::{Request, header};
    use std::fs;
    use std::path::Path as FsPath;
    use tower::ServiceExt;

    async fn send_request(
        app: Router,
        method: &str,
        uri: &str,
        body: Option<String>,
    ) -> (StatusCode, String) {
        let body = body.map(Body::from).unwrap_or(Body::empty());
        let request = Request::builder()
            .method(method)
            .uri(uri)
            .header("Content-Type", "application/json")
            .body(body)
            .unwrap();
        let response = app.oneshot(request).await.unwrap();
        let status = response.status();
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        (status, String::from_utf8(bytes.to_vec()).unwrap())
    }

    // The route reads its server-side credential fallback from the process environment, so a
    // test that needs a credential has to set one, and a test that needs *no* credential has to
    // clear whatever the developer's shell exported — otherwise these cases pass or fail by
    // accident. A blocking `std::sync::MutexGuard` cannot be held across `.await` (clippy's
    // `await_holding_lock`), so this is the hand-rolled spin flag `tests/chat_execution.rs` and
    // `tests/chat_stream.rs` already use, released by `EnvGuard::drop`.
    static ENV_BUSY: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

    struct EnvLock;

    impl EnvLock {
        fn acquire() -> Self {
            use std::sync::atomic::Ordering;
            while ENV_BUSY
                .compare_exchange(false, true, Ordering::Acquire, Ordering::Relaxed)
                .is_err()
            {
                std::thread::yield_now();
            }
            Self
        }
    }

    impl Drop for EnvLock {
        fn drop(&mut self) {
            ENV_BUSY.store(false, std::sync::atomic::Ordering::Release);
        }
    }

    /// `DEEPSEEK_API_KEY` and a workspace root, both forced for the lifetime of the guard.
    ///
    /// The root is needed by any case that gets past the credential, model and `messages` checks:
    /// `/v1/chat/completions` binds the assembly from there, and the message rules expand each
    /// turn's content, so they read the file cache the same way the memory store and the budget
    /// ledger do. Without a root such a case answers `500 DEEPSEEK_INFRA_ROOT is not set`.
    struct EnvGuard {
        saved: Vec<(&'static str, Option<String>)>,
        _root: tempfile::TempDir,
    }

    impl EnvGuard {
        fn api_key(value: &str) -> Self {
            Self::with(&[("DEEPSEEK_API_KEY", value)])
        }

        /// `pairs` forced for the guard's lifetime, with a workspace root installed alongside them —
        /// the route binds the assembly, so any case that gets past validation needs one.
        fn with(pairs: &[(&'static str, &str)]) -> Self {
            let _root = tempfile::tempdir().expect("a temp workspace root");
            let root_path = _root
                .path()
                .to_str()
                .expect("a utf-8 temp path")
                .to_string();
            let mut saved = Vec::new();
            for (name, value) in std::iter::once(("DEEPSEEK_INFRA_ROOT", root_path.as_str()))
                .chain(pairs.iter().copied())
            {
                saved.push((name, std::env::var(name).ok()));
                // `set_var` is `unsafe` in edition 2024; `EnvLock` is what makes it sound here, by
                // keeping these mutations away from other tests' threads for its lifetime.
                unsafe { std::env::set_var(name, value) };
            }
            Self { saved, _root }
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            for (name, value) in &self.saved {
                match value {
                    Some(value) => unsafe { std::env::set_var(name, value) },
                    None => unsafe { std::env::remove_var(name) },
                }
            }
        }
    }

    async fn send_bytes(
        app: Router,
        uri: &str,
        content_type: &str,
        body: Vec<u8>,
    ) -> (StatusCode, String, Vec<u8>) {
        let request = Request::builder()
            .method("POST")
            .uri(uri)
            .header(CONTENT_TYPE, content_type)
            .body(Body::from(body))
            .unwrap();
        let response = app.oneshot(request).await.unwrap();
        let status = response.status();
        let content_type = response
            .headers()
            .get(CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .unwrap_or_default()
            .to_string();
        let bytes = to_bytes(response.into_body(), usize::MAX).await.unwrap();
        (status, content_type, bytes.to_vec())
    }

    fn write_frontend_fixture(root: &FsPath) {
        fs::create_dir_all(root.join("ui/assets")).unwrap();
        fs::create_dir_all(root.join("icons")).unwrap();
        fs::write(
            root.join("ui/index.html"),
            "<!doctype html><main>native ui</main>",
        )
        .unwrap();
        fs::write(
            root.join("ui/assets/app-0123456789abcdef.js"),
            "globalThis.__nativeUi = true;",
        )
        .unwrap();
        fs::write(root.join("icons/app.svg"), "<svg></svg>").unwrap();
        fs::write(
            root.join("ui/manifest-root.webmanifest"),
            r#"{"name":"DeepSeek Infra"}"#,
        )
        .unwrap();
        fs::write(
            root.join("ui/sw-root-0123456789abcdef.js"),
            "self.__nativeWorker = true;",
        )
        .unwrap();
    }

    async fn get_response(app: Router, uri: &str) -> Response {
        app.oneshot(
            Request::builder()
                .method("GET")
                .uri(uri)
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap()
    }

    async fn response_body(response: Response) -> String {
        String::from_utf8(
            to_bytes(response.into_body(), usize::MAX)
                .await
                .unwrap()
                .to_vec(),
        )
        .unwrap()
    }

    #[test]
    fn production_app_rejects_a_missing_frontend_build() {
        let temp = tempfile::tempdir().unwrap();
        let error = create_production_app(temp.path()).unwrap_err();
        assert_eq!(error.kind(), std::io::ErrorKind::NotFound);
        assert!(error.to_string().contains("static/ui/index.html"));
    }

    #[tokio::test]
    async fn production_app_serves_index_and_spa_routes_with_security_headers() {
        let temp = tempfile::tempdir().unwrap();
        write_frontend_fixture(temp.path());
        let app = create_production_app(temp.path()).unwrap();

        for uri in ["/", "/ui", "/conversations/conv-1"] {
            let response = get_response(app.clone(), uri).await;
            assert_eq!(response.status(), StatusCode::OK, "{uri}");
            assert_eq!(response.headers()[header::CACHE_CONTROL], "no-store");
            assert_eq!(
                response.headers()[header::X_CONTENT_TYPE_OPTIONS],
                "nosniff"
            );
            assert_eq!(response.headers()[header::X_FRAME_OPTIONS], "DENY");
            assert!(response.headers().contains_key("x-deepseek-request-id"));
            assert!(
                response.headers()[header::CONTENT_TYPE]
                    .to_str()
                    .unwrap()
                    .starts_with("text/html")
            );
            assert!(response_body(response).await.contains("native ui"));
        }
    }

    #[tokio::test]
    async fn production_app_serves_assets_with_the_frozen_cache_contract() {
        let temp = tempfile::tempdir().unwrap();
        write_frontend_fixture(temp.path());
        let app = create_production_app(temp.path()).unwrap();

        let hashed = get_response(app.clone(), "/ui/assets/app-0123456789abcdef.js").await;
        assert_eq!(hashed.status(), StatusCode::OK);
        assert_eq!(
            hashed.headers()[header::CACHE_CONTROL],
            "public, max-age=31536000, immutable"
        );
        assert!(response_body(hashed).await.contains("__nativeUi"));

        let ordinary = get_response(app, "/icons/app.svg").await;
        assert_eq!(ordinary.status(), StatusCode::OK);
        assert_eq!(ordinary.headers()[header::CACHE_CONTROL], "no-cache");
        assert_eq!(ordinary.headers()[header::CONTENT_TYPE], "image/svg+xml");
    }

    #[tokio::test]
    async fn production_app_preserves_root_pwa_aliases() {
        let temp = tempfile::tempdir().unwrap();
        write_frontend_fixture(temp.path());
        let app = create_production_app(temp.path()).unwrap();

        let manifest = get_response(app.clone(), "/manifest.webmanifest").await;
        assert_eq!(manifest.status(), StatusCode::OK);
        assert_eq!(manifest.headers()[header::CACHE_CONTROL], "no-cache");
        assert_eq!(
            manifest.headers()[header::CONTENT_TYPE],
            "application/manifest+json"
        );

        let worker = get_response(app, "/sw-0123456789abcdef.js").await;
        assert_eq!(worker.status(), StatusCode::OK);
        assert_eq!(
            worker.headers()[header::CACHE_CONTROL],
            "public, max-age=31536000, immutable"
        );
        assert!(response_body(worker).await.contains("__nativeWorker"));
    }

    #[tokio::test]
    async fn production_app_never_turns_missing_assets_or_traversal_into_spa_success() {
        let temp = tempfile::tempdir().unwrap();
        write_frontend_fixture(temp.path());
        fs::write(
            temp.path().parent().unwrap().join("outside-secret"),
            "secret",
        )
        .unwrap();
        let app = create_production_app(temp.path()).unwrap();

        for uri in [
            "/ui/assets/missing.js",
            "/icons",
            "/%2e%2e/outside-secret",
            "/ui/%2e%2e/%2e%2e/outside-secret",
            "/%5coutside-secret",
            "/legacy",
        ] {
            let response = get_response(app.clone(), uri).await;
            assert_eq!(response.status(), StatusCode::NOT_FOUND, "{uri}");
            assert!(
                !response_body(response).await.contains("native ui"),
                "{uri}"
            );
        }
    }

    #[tokio::test]
    async fn production_routes_take_precedence_and_static_post_is_rejected() {
        let temp = tempfile::tempdir().unwrap();
        write_frontend_fixture(temp.path());
        let app = create_production_app_with_auth(
            temp.path(),
            auth::ProductionAuth {
                enabled: true,
                token: String::new(),
            },
        )
        .unwrap();

        let api = get_response(app.clone(), "/api/policies").await;
        assert_eq!(api.status(), StatusCode::UNAUTHORIZED);
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&response_body(api).await).unwrap()["error"]
                ["code"],
            "UNAUTHORIZED"
        );

        let post = app
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/conversations/conv-1")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(post.status(), StatusCode::METHOD_NOT_ALLOWED);
    }

    #[tokio::test]
    async fn production_models_and_api_accept_bearer_then_enforce_proxy_readiness() {
        let temp = tempfile::tempdir().unwrap();
        write_frontend_fixture(temp.path());
        let app = create_production_app_with_auth(
            temp.path(),
            auth::ProductionAuth {
                enabled: true,
                token: "gateway-test-token".to_string(),
            },
        )
        .unwrap();

        let denied = Request::builder()
            .method("GET")
            .uri("/v1/models")
            .body(Body::empty())
            .unwrap();
        let denied = app.clone().oneshot(denied).await.unwrap();
        assert_eq!(denied.status(), StatusCode::UNAUTHORIZED);

        let allowed = Request::builder()
            .method("GET")
            .uri("/v1/models")
            .header(header::AUTHORIZATION, "Bearer gateway-test-token")
            .body(Body::empty())
            .unwrap();
        let allowed = app.clone().oneshot(allowed).await.unwrap();
        assert_eq!(allowed.status(), StatusCode::OK);
        let catalog: serde_json::Value =
            serde_json::from_str(&response_body(allowed).await).unwrap();
        assert_eq!(catalog["object"], "list");

        let proxy = Request::builder()
            .method("GET")
            .uri("/api/policies")
            .header(header::AUTHORIZATION, "Bearer gateway-test-token")
            .body(Body::empty())
            .unwrap();
        let proxy = app.oneshot(proxy).await.unwrap();
        assert_eq!(proxy.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&response_body(proxy).await).unwrap()["error"]
                ["code"],
            "GO_CONTROL_PROXY_NOT_READY"
        );
    }

    #[test]
    fn gateway_version_matches_core() {
        assert_eq!(gateway_version(), deepseek_core::version_info().version);
    }

    #[tokio::test]
    async fn healthz_returns_ok() {
        let app = create_app();
        let (status, body) = send_request(app, "GET", "/healthz", None).await;
        assert_eq!(status, StatusCode::OK);
        let health: HealthzResponse = serde_json::from_str(&body).unwrap();
        assert!(health.ok);
        assert_eq!(health.service, "deepseek-gateway-rs");
    }

    #[tokio::test]
    async fn models_returns_native_catalog_matching_prepare_request() {
        let app = create_app();
        let (status, body) = send_request(app, "GET", "/v1/models", None).await;
        assert_eq!(status, StatusCode::OK);
        let response: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(response["object"], "list");
        let data = response["data"].as_array().expect("catalog data");
        let ids: Vec<&str> = data
            .iter()
            .map(|entry| entry["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, request_preparation::CATALOG_MODEL_IDS);
        for entry in data {
            assert_eq!(entry["object"], "model");
            assert_eq!(entry["owned_by"], "deepseek-infra");
            assert!(entry["created"].as_i64().unwrap() > 0);
            assert!(
                request_preparation::prepare_request(&json!({
                    "model": entry["id"],
                    "messages": [{"role": "user", "content": "catalog"}]
                }))
                .is_ok(),
                "{}",
                entry["id"]
            );
        }
        assert!(response.get("error").is_none());
    }

    /// A missing `model` is **accepted**, not rejected.
    ///
    /// `openai_to_internal_payload` substitutes `settings.default_model` for a falsy `model`
    /// (`openai_api.py:36`), so the oracle answers a request without one using the default.
    /// This route used to reject it with `400`; that was its own divergence, not the oracle's
    /// rule, and `openai_facade_parity_probe`'s `default-model-when-absent` case is what pins
    /// the fallback. The assertion is made at the composition boundary rather than over HTTP
    /// because the route's next step needs a bound workspace, and what this case is about is
    /// the translation, not the workspace.
    #[test]
    fn a_missing_model_falls_back_to_the_default_rather_than_being_rejected() {
        let router = deepseek_policy::model_router::ModelRouterSettings::default();
        let prepared = native_chat::prepare_openai_chat(
            &json!({"messages": [{"role": "user", "content": "hello"}]}),
            "http://127.0.0.1:8000",
            &router,
            "test-key",
        )
        .expect("a missing model is not an error");
        assert_eq!(prepared.validated.model, "deepseek-v4-pro");
    }

    #[tokio::test]
    async fn chat_rejects_empty_messages() {
        let app = create_app();
        let body = r#"{"model":"deepseek-v4-pro","messages":[]}"#;
        let (status, _body) =
            send_request(app, "POST", "/v1/chat/completions", Some(body.to_string())).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }

    /// The ownership signal is the mode, and only the mode.
    ///
    /// `DEEPSEEK_GO_CONTROL=1` means the Go *control* plane is authoritative; the data plane can
    /// still be Python's during that window, so reading it as ownership of the memory store would put
    /// two writers on one file — the thing ADR-0049 forbids. The table below is the counterpart of
    /// `authority.py`'s rule for data domains, and it deliberately ignores the Go flag.
    #[test]
    fn the_memory_store_owner_is_the_mode_and_not_go_control() {
        let _env = EnvLock::acquire();
        assert!(!python_is_de_authorised_in(""));
        assert!(!python_is_de_authorised_in("python_authoritative"));
        assert!(!python_is_de_authorised_in("shadow"));
        assert!(!python_is_de_authorised_in("go_authoritative"));
        assert!(python_is_de_authorised_in("python_disabled"));
        // Case and surrounding whitespace are the reader's own tolerance, not a second spelling.
        assert!(python_is_de_authorised_in(" Python_Disabled "));

        let _go = EnvGuard::with(&[("DEEPSEEK_GO_CONTROL", "1"), ("DEEPSEEK_RUNTIME_MODE", "")]);
        assert!(
            !python_is_de_authorised(),
            "DEEPSEEK_GO_CONTROL is the control plane and must not grant the data plane"
        );
        // With the Go flag still set, the mode alone flips it: the store's owner is a mode question.
        let _mode = EnvGuard::with(&[("DEEPSEEK_RUNTIME_MODE", "python_disabled")]);
        assert!(python_is_de_authorised());
        // ...and the mode alone is not enough: the store has to be declared as well.
        assert!(may_write_native_store("memory_store"));
        assert!(!may_write_native_store("reminders_store"));
    }

    /// The writable-stores list must be the contract's list, not a second opinion.
    ///
    /// `release/native_runtime_ownership_v1.json` is the authority for which stores have cut over, so
    /// this pins the two together: declaring a domain without adding it here would leave the route
    /// refusing a store it owns, and adding one here without declaring it would put a second writer on
    /// a store the contract still gives to Python.
    #[test]
    fn the_writable_stores_are_the_ones_the_contract_declares() {
        let contract: serde_json::Value = serde_json::from_str(
            &std::fs::read_to_string(
                std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                    .join("../../../release/native_runtime_ownership_v1.json"),
            )
            .expect("the ownership contract is readable from the crate root"),
        )
        .expect("the ownership contract is JSON");
        let mut declared: Vec<&str> = contract["domains"]
            .as_array()
            .expect("domains[] is an array")
            .iter()
            .filter(|item| {
                item["current_owner"] == json!("python")
                    && item["target_owner"] == json!("rust")
                    && item["durable_store"] == json!("rust_data")
            })
            .filter_map(|item| item["id"].as_str())
            .collect();
        declared.sort_unstable();
        // A **subset**, not equality: the contract also declares stores this gateway has nothing to
        // do with (`s3_minio_streaming`, `transfer_jobs`, the crypto/verification domains — nine of
        // them carry `durable_store: rust_data`). What must never happen is this list inventing
        // ownership the contract has not declared, because that is the second writer it forbids.
        for domain in DECLARED_NATIVE_DATA_DOMAINS {
            assert!(
                declared.contains(&domain),
                "{domain} is writable here but the contract does not declare it python -> rust"
            );
        }
        // And the one this route actually predicates on is in it, so the list is not decorative.
        assert!(DECLARED_NATIVE_DATA_DOMAINS.contains(&"memory_store"));
    }

    /// With no credential the route fails closed, and it answers with the oracle's own error.
    ///
    /// Wiring the assembly moved this case from `503 NATIVE_CHAT_UPSTREAM_CREDENTIAL_MISSING` to
    /// `400 missing_api_key`: the route now validates through the facade's
    /// `validate_deepseek_payload` (`deepseek_client.py:198-201`), which checks the credential
    /// **first** and raises `AppError`'s default status, 400. 400 is what the oracle answers —
    /// the 503 was this route's own divergence — and the credential check further down can no
    /// longer fire here, because validation has already required a non-empty key.
    ///
    /// `DEEPSEEK_API_KEY` is forced empty so a developer shell that exports one cannot silently
    /// turn this case into a different request.
    #[tokio::test]
    async fn chat_reports_missing_upstream_credential_instead_of_a_stub() {
        let _env = EnvLock::acquire();
        let _key = EnvGuard::api_key("");
        let app = create_app();
        let body = r#"{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hello"}]}"#;
        let (status, response_body) =
            send_request(app, "POST", "/v1/chat/completions", Some(body.to_string())).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        let response: serde_json::Value = serde_json::from_str(&response_body).unwrap();
        assert_eq!(response["code"], "missing_api_key", "{response_body}");
        assert!(!response_body.contains("chatcmpl-stub"));
        assert!(
            response.get("choices").is_none(),
            "a failed request must not carry a completion: {response_body}"
        );
    }

    /// `/v1/chat/completions` runs the oracle's own preparation layer, in the oracle's order.
    ///
    /// `validate_deepseek_payload` checks the credential **first** (`deepseek_client.py:198`), so
    /// a credential has to be present for these cases to reach the model allowlist, the user-turn
    /// requirement and the compression conflict at all. That ordering is itself part of the
    /// contract: with no key the only answer this route gives is `400 missing_api_key`.
    #[tokio::test]
    async fn chat_enforces_the_shared_preparation_rules() {
        let _env = EnvLock::acquire();
        let _key = EnvGuard::api_key("test-key");
        let app = create_app();

        // No user turn -> the oracle's user-message requirement.
        let body =
            r#"{"model":"deepseek-v4-pro","messages":[{"role":"assistant","content":"hi"}]}"#;
        let (status, response_body) = send_request(
            app.clone(),
            "POST",
            "/v1/chat/completions",
            Some(body.to_string()),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
        assert!(
            // The oracle's own wording and casing (`deepseek_client.py:245`), not the thinner
            // validator's `a user message is required` that this route used to raise.
            response_body.contains("A user message is required"),
            "unexpected body: {response_body}"
        );

        // Unsupported model -> the shared model allowlist, not a free-form string.
        let body = r#"{"model":"gpt-9","messages":[{"role":"user","content":"hi"}]}"#;
        let (status, _) = send_request(
            app.clone(),
            "POST",
            "/v1/chat/completions",
            Some(body.to_string()),
        )
        .await;
        assert_eq!(status, StatusCode::BAD_REQUEST);

        // More than MESSAGE_HARD_LIMIT turns without a context summary -> 409,
        // matching the oracle's CONTEXT_COMPRESSION_REQUIRED conflict.
        let mut messages = Vec::new();
        for index in 0..=request_preparation::MESSAGE_HARD_LIMIT {
            messages.push(json!({"role": "user", "content": format!("turn {index}")}));
        }
        let body = json!({"model": "deepseek-v4-pro", "messages": messages}).to_string();
        let (status, response_body) =
            send_request(app, "POST", "/v1/chat/completions", Some(body)).await;
        assert_eq!(status, StatusCode::CONFLICT);
        assert!(
            // The oracle's sentence (`deepseek_client.py:231`), not the thinner validator's
            // `context compression is required`.
            response_body
                .contains("Context compression required before sending more than 40 messages."),
            "unexpected body: {response_body}"
        );
    }

    #[tokio::test]
    async fn gateway_request_prepare_endpoint_returns_normalized_contract() {
        let app = create_app();
        let body = r#"{"model":"fast","messages":[{"role":"user","content":" 你好 🚀 "}],"tools":[],"tool_choice":"auto","temperature":1}"#;
        let (status, response_body) = send_request(
            app,
            "POST",
            "/gateway/request/prepare",
            Some(body.to_string()),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let response: serde_json::Value = serde_json::from_str(&response_body).unwrap();
        assert_eq!(response["ok"], true);
        assert_eq!(response["request"]["model"], "deepseek-v4-flash");
        assert_eq!(response["request"]["messages"][0]["content"], "你好 🚀");
        assert!(response["request"].get("tools").is_none());
    }

    #[tokio::test]
    async fn gateway_request_prepare_endpoint_returns_stable_error_code() {
        let app = create_app();
        let body = r#"{"model":"deepseek-v4-pro","messages":[{"role":"owner","content":"hello"}]}"#;
        let (status, response_body) = send_request(
            app,
            "POST",
            "/gateway/request/prepare",
            Some(body.to_string()),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let response: serde_json::Value = serde_json::from_str(&response_body).unwrap();
        assert_eq!(response["ok"], false);
        assert_eq!(response["code"], "invalid_message_role");
    }

    #[tokio::test]
    async fn gateway_request_prepare_endpoint_rejects_malformed_json() {
        let app = create_app();
        let (status, response_body) = send_request(
            app,
            "POST",
            "/gateway/request/prepare",
            Some("{".to_string()),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        let response: serde_json::Value = serde_json::from_str(&response_body).unwrap();
        assert_eq!(response["ok"], false);
        assert_eq!(response["code"], "invalid_request");
    }

    #[tokio::test]
    async fn mcp_protocol_prepare_endpoint_returns_python_owned_descriptor() {
        let app = create_app();
        let body = json!({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": " search ", "arguments": {"query": "Rust MCP 中文🚀"}}
        })
        .to_string();
        let (status, response_body) =
            send_request(app, "POST", "/mcp/request/prepare", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let response: serde_json::Value = serde_json::from_str(&response_body).unwrap();
        assert_eq!(response["ok"], true);
        assert_eq!(response["messageType"], "request");
        assert_eq!(response["request"]["params"]["name"], "search");
        assert_eq!(
            response["request"]["params"]["arguments"]["query"],
            "Rust MCP 中文🚀"
        );
        assert_eq!(response["routing"]["owner"], "python");
        assert!(response.get("result").is_none());
    }

    #[tokio::test]
    async fn mcp_protocol_prepare_endpoint_returns_stable_error_mapping() {
        let app = create_app();
        let body = json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{}}).to_string();
        let (status, response_body) =
            send_request(app, "POST", "/mcp/request/prepare", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let response: serde_json::Value = serde_json::from_str(&response_body).unwrap();
        assert_eq!(response["ok"], false);
        assert_eq!(response["code"], "invalid_params");
        assert_eq!(response["jsonRpcCode"], -32602);
    }

    #[tokio::test]
    async fn mcp_protocol_prepare_endpoint_rejects_malformed_json() {
        let app = create_app();
        let (status, response_body) =
            send_request(app, "POST", "/mcp/request/prepare", Some("{".to_string())).await;
        assert_eq!(status, StatusCode::OK);
        let response: serde_json::Value = serde_json::from_str(&response_body).unwrap();
        assert_eq!(response["code"], "parse_error");

        let app = create_app();
        let alias_body = json!({"jsonrpc":"2.0","id":1,"method":"ping"}).to_string();
        let (alias_status, alias_response) =
            send_request(app, "POST", "/mcp", Some(alias_body)).await;
        assert_eq!(alias_status, StatusCode::OK);
        let alias_response = serde_json::from_str::<serde_json::Value>(&alias_response).unwrap();
        assert_eq!(alias_response["id"], 1);
        assert_eq!(alias_response["result"], json!({}));
        assert!(alias_response.get("error").is_none());
    }

    #[tokio::test]
    async fn mcp_initialize_and_ping_are_native_jsonrpc_while_tools_stay_unwired() {
        let app = create_app();
        let init = json!({
            "jsonrpc": "2.0",
            "id": 7,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "native-test", "version": "1"}
            }
        })
        .to_string();
        let (status, body) = send_request(app.clone(), "POST", "/mcp", Some(init)).await;
        assert_eq!(status, StatusCode::OK);
        let response: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(response["result"]["protocolVersion"], "2025-06-18");
        assert_eq!(response["result"]["serverInfo"]["name"], "deepseek-infra");
        assert!(response.get("error").is_none());

        let tools = json!({"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"echo"}})
            .to_string();
        let (tool_status, tool_body) = send_request(app, "POST", "/mcp", Some(tools)).await;
        assert_eq!(tool_status, StatusCode::OK);
        let tool_response: serde_json::Value = serde_json::from_str(&tool_body).unwrap();
        assert_eq!(tool_response["error"]["code"], -32601);
        assert!(
            tool_response["error"]["message"]
                .as_str()
                .unwrap()
                .contains("tool execution is not wired")
        );
        assert!(tool_response.get("result").is_none());
    }

    #[tokio::test]
    async fn unimplemented_public_routes_never_claim_native_success() {
        let app = create_app();
        let (card_status, card_body) =
            send_request(app.clone(), "GET", "/.well-known/agent-card.json", None).await;
        assert_eq!(card_status, StatusCode::OK);
        let card: serde_json::Value = serde_json::from_str(&card_body).unwrap();
        assert_eq!(card["protocolVersion"], "0.3.0");
        assert_eq!(card["capabilities"]["streaming"], false);
        assert!(
            card.get("description")
                .and_then(|value| value.as_str())
                .unwrap()
                .contains("A2A execution is not wired")
        );

        let a2a_body = json!({"jsonrpc":"2.0","id":"a2a-1","method":"message/send"});
        let (a2a_status, a2a_response) =
            send_request(app.clone(), "POST", "/a2a", Some(a2a_body.to_string())).await;
        assert_eq!(a2a_status, StatusCode::OK);
        let a2a_response = serde_json::from_str::<serde_json::Value>(&a2a_response).unwrap();
        assert_eq!(a2a_response["error"]["code"], -32601);
        assert_eq!(a2a_response["id"], "a2a-1");
        assert!(a2a_response.get("result").is_none());

        let (proxy_status, proxy_body) = send_request(app, "GET", "/api/policies", None).await;
        assert_eq!(proxy_status, StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&proxy_body).unwrap()["error"]["code"],
            "GO_CONTROL_PROXY_NOT_READY"
        );
    }

    /// Streaming is a first-class transport, so `stream: true` must not be refused as
    /// unimplemented — and whatever refuses it has to refuse with ordinary JSON, before any SSE
    /// frame reaches the client.
    ///
    /// With no credential the refusal is `400 missing_api_key` from validation, which the ordered
    /// validation makes the first thing this route can say. The name therefore says what the case
    /// proves — the refusal arrives before any frame — rather than naming a layer.
    #[tokio::test]
    async fn chat_accepts_streaming_and_refuses_before_any_frame() {
        let _env = EnvLock::acquire();
        let _key = EnvGuard::api_key("");
        let app = create_app();
        let body = r#"{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hello"}],"stream":true}"#;
        let (status, response) =
            send_request(app, "POST", "/v1/chat/completions", Some(body.to_string())).await;
        assert_ne!(
            status,
            StatusCode::NOT_IMPLEMENTED,
            "streaming must not be refused as unimplemented: {response}"
        );
        assert_eq!(status, StatusCode::BAD_REQUEST);
        let parsed: serde_json::Value = serde_json::from_str(&response).unwrap();
        assert_eq!(parsed["code"], "missing_api_key", "{response}");
        // The refusal happened before the body, so it is ordinary JSON, not SSE.
        assert!(!response.starts_with("data: "));
    }

    #[tokio::test]
    async fn policy_url_endpoint_denies_localhost() {
        let app = create_app();
        let body = serde_json::json!({
            "url": "http://localhost:8080/admin",
            "trace_id": "trace-policy-url",
            "capability": "NetworkFetch",
            "risk_level": "High"
        })
        .to_string();
        let (status, body) = send_request(app, "POST", "/policy/url", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let decision: deepseek_policy::PolicyDecision = serde_json::from_str(&body).unwrap();
        assert!(!decision.is_allowed());
        assert_eq!(decision.code, deepseek_policy::codes::LOCALHOST_BLOCKED);
        assert!(decision.decision_id.starts_with("pd_"));
        assert_eq!(decision.trace_id.unwrap().0, "trace-policy-url");
    }

    #[tokio::test]
    async fn policy_path_endpoint_denies_parent_traversal() {
        let app = create_app();
        let body =
            serde_json::json!({"root": "/workspace", "requested": "../secret.txt"}).to_string();
        let (status, body) = send_request(app, "POST", "/policy/path", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let decision: deepseek_policy::PolicyDecision = serde_json::from_str(&body).unwrap();
        assert!(!decision.is_allowed());
        assert_eq!(decision.code, deepseek_policy::codes::PATH_TRAVERSAL);
    }

    #[tokio::test]
    async fn policy_capability_endpoint_denies_missing_capability() {
        let app = create_app();
        let body = serde_json::json!({
            "requested": "ShellExec",
            "granted": ["ReadFile"],
            "max_risk": "Critical"
        })
        .to_string();
        let (status, body) = send_request(app, "POST", "/policy/capability", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let decision: deepseek_policy::PolicyDecision = serde_json::from_str(&body).unwrap();
        assert!(!decision.is_allowed());
        assert_eq!(decision.code, deepseek_policy::codes::MISSING_CAPABILITY);
    }

    #[tokio::test]
    async fn policy_endpoint_returns_structured_decision() {
        let app = create_app();
        let body = serde_json::json!({"url": "https://example.com"}).to_string();
        let (status, body) = send_request(app, "POST", "/policy/url", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let decision: deepseek_policy::PolicyDecision = serde_json::from_str(&body).unwrap();
        assert!(decision.is_allowed());
        assert_eq!(decision.code, deepseek_policy::codes::ALLOWED);
        assert!(!decision.decision_id.is_empty());
    }

    #[tokio::test]
    async fn policy_endpoint_rejects_missing_fields_with_stable_code() {
        let app = create_app();
        let (status, body) = send_request(app, "POST", "/policy/url", Some("{}".to_string())).await;
        assert_eq!(status, StatusCode::OK);
        let decision: deepseek_policy::PolicyDecision = serde_json::from_str(&body).unwrap();
        assert!(!decision.is_allowed());
        assert_eq!(
            decision.code,
            deepseek_policy::codes::INVALID_POLICY_REQUEST
        );
    }

    #[tokio::test]
    async fn rag_query_normalize_endpoint_preserves_cjk() {
        let app = create_app();
        let body = serde_json::json!({"query": "  Rust 语言  "}).to_string();
        let (status, body) = send_request(app, "POST", "/rag/query/normalize", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let response: RagQueryNormalizeResponse = serde_json::from_str(&body).unwrap();
        assert_eq!(response.normalized, "rust 语言");
        assert_eq!(response.tokens, vec!["rust", "语言"]);
    }

    #[tokio::test]
    async fn rag_chunk_score_endpoint_ranks_exact_match() {
        let app = create_app();
        let chunks = vec![
            deepseek_rag::chunk::RagChunk {
                id: "partial".to_string(),
                source: "docs/example.md".to_string(),
                text: "deepseek is a company".to_string(),
                start_line: None,
                end_line: None,
                metadata: deepseek_rag::chunk::ChunkMetadata::default(),
            },
            deepseek_rag::chunk::RagChunk {
                id: "exact".to_string(),
                source: "docs/example.md".to_string(),
                text: "deepseek infra is a project".to_string(),
                start_line: None,
                end_line: None,
                metadata: deepseek_rag::chunk::ChunkMetadata::default(),
            },
        ];
        let body = serde_json::json!({"query": "deepseek infra", "chunks": chunks}).to_string();
        let (status, body) = send_request(app, "POST", "/rag/chunks/score", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let response: RagChunkScoreResponse = serde_json::from_str(&body).unwrap();
        assert_eq!(response.ranked[0].id, "exact");
        assert!(response.ranked[0].score > response.ranked[1].score);
    }

    #[tokio::test]
    async fn rag_vector_rank_endpoint_returns_stable_best_match() {
        let app = create_app();
        let body = serde_json::json!({
            "query": [1.0, 0.0],
            "candidates": [[0.5, 0.0], [1.0, 0.0], [1.0, 0.0]]
        })
        .to_string();
        let (status, body) = send_request(app, "POST", "/rag/vectors/rank", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let response: RagVectorRankResponse = serde_json::from_str(&body).unwrap();
        assert_eq!(response.index, Some(1));
        assert_eq!(response.similarity, 1.0);
    }

    #[tokio::test]
    async fn rag_vector_binary_endpoint_matches_json_endpoint() {
        let mut body = Vec::new();
        body.extend_from_slice(deepseek_rag::vector_binary::REQUEST_MAGIC);
        body.extend_from_slice(&2_u32.to_le_bytes());
        body.extend_from_slice(&3_u32.to_le_bytes());
        for value in [1.0_f64, 0.0, 0.5, 0.0, 1.0, 0.0, 1.0, 0.0] {
            body.extend_from_slice(&value.to_le_bytes());
        }
        let (status, content_type, response) = send_bytes(
            create_app(),
            "/rag/vectors/rank-binary",
            deepseek_rag::vector_binary::CONTENT_TYPE,
            body,
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(content_type, deepseek_rag::vector_binary::CONTENT_TYPE);
        assert_eq!(response.len(), deepseek_rag::vector_binary::RESPONSE_BYTES);
        assert_eq!(&response[..8], deepseek_rag::vector_binary::RESPONSE_MAGIC);
        assert_eq!(u32::from_le_bytes(response[8..12].try_into().unwrap()), 1);
        assert_eq!(
            f64::from_le_bytes(response[16..24].try_into().unwrap()),
            1.0
        );
    }

    #[tokio::test]
    async fn rag_vector_binary_endpoint_returns_stable_json_errors() {
        let (status, content_type, response) = send_bytes(
            create_app(),
            "/rag/vectors/rank-binary",
            "application/octet-stream",
            Vec::new(),
        )
        .await;
        assert_eq!(status, StatusCode::UNSUPPORTED_MEDIA_TYPE);
        assert_eq!(content_type, "application/json");
        let error: serde_json::Value = serde_json::from_slice(&response).unwrap();
        assert_eq!(error["code"], "invalid_content_type");
    }

    #[tokio::test]
    async fn rag_citation_endpoint_formats_line_range() {
        let app = create_app();
        let body =
            serde_json::json!({"source": "docs/example.md", "start_line": 10, "end_line": 20})
                .to_string();
        let (status, body) = send_request(app, "POST", "/rag/citation/format", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let response: RagCitationFormatResponse = serde_json::from_str(&body).unwrap();
        assert_eq!(response.citation, "docs/example.md:L10-L20");
    }

    #[tokio::test]
    async fn rag_index_validate_endpoint_rejects_duplicate_ids() {
        let app = create_app();
        let chunks = vec![
            deepseek_rag::chunk::RagChunk {
                id: "a".to_string(),
                source: "docs/example.md".to_string(),
                text: "first".to_string(),
                start_line: None,
                end_line: None,
                metadata: deepseek_rag::chunk::ChunkMetadata::default(),
            },
            deepseek_rag::chunk::RagChunk {
                id: "a".to_string(),
                source: "docs/other.md".to_string(),
                text: "second".to_string(),
                start_line: None,
                end_line: None,
                metadata: deepseek_rag::chunk::ChunkMetadata::default(),
            },
        ];
        let body = serde_json::json!({"chunks": chunks}).to_string();
        let (status, body) = send_request(app, "POST", "/rag/index/validate", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let response: RagIndexValidateResponse = serde_json::from_str(&body).unwrap();
        assert!(!response.valid);
        assert!(response.error.unwrap().contains("duplicate"));
    }

    #[tokio::test]
    async fn rag_document_prepare_endpoint_uses_character_offsets() {
        let app = create_app();
        let body = serde_json::json!({
            "documentId":"doc-1",
            "text":"中文🚀abc",
            "metadata":{"displayName":"notes.txt","sourceType":"text/plain"},
            "chunking":{"chunkChars":3,"chunkOverlap":1}
        })
        .to_string();
        let (status, body) = send_request(app, "POST", "/rag/documents/prepare", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let response: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(response["ok"], true);
        assert_eq!(response["chunks"][0]["text"], "中文🚀");
        assert_eq!(response["chunks"][1]["start"], 2);
        assert_eq!(response["document"]["characterCount"], 6);
    }

    #[tokio::test]
    async fn rag_document_prepare_endpoint_rejects_sensitive_metadata() {
        let app = create_app();
        let body = serde_json::json!({
            "documentId":"doc-1",
            "text":"already parsed",
            "metadata":{"absolutePath":"/tmp/secret"},
            "chunking":{"chunkChars":6000,"chunkOverlap":400}
        })
        .to_string();
        let (status, body) = send_request(app, "POST", "/rag/documents/prepare", Some(body)).await;
        assert_eq!(status, StatusCode::OK);
        let response: serde_json::Value = serde_json::from_str(&body).unwrap();
        assert_eq!(response["code"], "invalid_metadata");
    }
}
