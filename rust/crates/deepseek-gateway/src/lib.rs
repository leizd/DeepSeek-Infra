use axum::{
    Json, Router,
    body::Bytes,
    extract::DefaultBodyLimit,
    http::StatusCode,
    routing::{get, post},
};
use serde::{Deserialize, Serialize};
use serde_json::json;

pub mod policy_routes;
pub mod request_preparation;

pub fn gateway_version() -> &'static str {
    deepseek_core::version_info().version
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HealthzResponse {
    pub ok: bool,
    pub service: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChatCompletionRequest {
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub messages: Vec<ChatMessage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stream: Option<bool>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChatMessage {
    pub role: String,
    pub content: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChatCompletionResponse {
    pub id: String,
    pub object: String,
    pub created: i64,
    pub model: String,
    pub choices: Vec<ChatChoice>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ChatChoice {
    pub index: u32,
    pub message: ChatMessage,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub finish_reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModelListResponse {
    pub object: String,
    pub data: Vec<ModelDescriptor>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ModelDescriptor {
    pub id: String,
    pub object: String,
    pub created: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub owned_by: Option<String>,
}

pub fn create_app() -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/v1/models", get(models))
        .route("/v1/chat/completions", post(chat_completions))
        .route("/gateway/request/prepare", post(gateway_request_prepare))
        .route("/mcp", post(mcp_protocol_prepare))
        .route("/mcp/request/prepare", post(mcp_protocol_prepare))
        .route("/rag/query/normalize", post(rag_query_normalize))
        .route("/rag/chunks/score", post(rag_chunks_score))
        .route("/rag/vectors/rank", post(rag_vectors_rank))
        .route("/rag/citation/format", post(rag_citation_format))
        .route("/rag/index/validate", post(rag_index_validate))
        .route("/rag/documents/prepare", post(rag_document_prepare))
        .merge(policy_routes::router())
        .layer(DefaultBodyLimit::max(
            deepseek_rag::document_preparation::MAX_REQUEST_BYTES + 1_000_000,
        ))
        .layer(tower_http::trace::TraceLayer::new_for_http())
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

async fn models() -> Json<ModelListResponse> {
    Json(ModelListResponse {
        object: "list".to_string(),
        data: vec![ModelDescriptor {
            id: "deepseek-v4-pro".to_string(),
            object: "model".to_string(),
            created: 1_700_000_000,
            owned_by: Some("deepseek".to_string()),
        }],
    })
}

async fn chat_completions(
    Json(req): Json<ChatCompletionRequest>,
) -> Result<Json<ChatCompletionResponse>, (StatusCode, Json<serde_json::Value>)> {
    validate_chat_request(&req)?;

    Ok(Json(ChatCompletionResponse {
        id: "chatcmpl-stub".to_string(),
        object: "chat.completion".to_string(),
        created: 1_700_000_000,
        model: req.model,
        choices: vec![ChatChoice {
            index: 0,
            message: ChatMessage {
                role: "assistant".to_string(),
                content: "This is a deterministic stub response from deepseek-gateway-rs."
                    .to_string(),
            },
            finish_reason: Some("stop".to_string()),
        }],
    }))
}

fn validate_chat_request(
    req: &ChatCompletionRequest,
) -> Result<(), (StatusCode, Json<serde_json::Value>)> {
    if req.model.trim().is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": {
                    "message": "model is required",
                    "type": "invalid_request_error"
                }
            })),
        ));
    }

    if req.messages.is_empty() {
        return Err((
            StatusCode::BAD_REQUEST,
            Json(json!({
                "error": {
                    "message": "messages must not be empty",
                    "type": "invalid_request_error"
                }
            })),
        ));
    }

    if req.stream == Some(true) {
        return Err((
            StatusCode::NOT_IMPLEMENTED,
            Json(json!({
                "error": {
                    "message": "streaming is not supported in this MVP",
                    "type": "not_supported"
                }
            })),
        ));
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::{Body, to_bytes};
    use axum::http::Request;
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
    async fn models_returns_openai_compatible_shape() {
        let app = create_app();
        let (status, body) = send_request(app, "GET", "/v1/models", None).await;
        assert_eq!(status, StatusCode::OK);
        let list: ModelListResponse = serde_json::from_str(&body).unwrap();
        assert_eq!(list.object, "list");
        assert!(!list.data.is_empty());
        let model = &list.data[0];
        assert_eq!(model.object, "model");
        assert!(!model.id.is_empty());
    }

    #[tokio::test]
    async fn chat_rejects_missing_model() {
        let app = create_app();
        let body = r#"{"messages":[{"role":"user","content":"hello"}]}"#;
        let (status, _body) =
            send_request(app, "POST", "/v1/chat/completions", Some(body.to_string())).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn chat_rejects_empty_messages() {
        let app = create_app();
        let body = r#"{"model":"deepseek-v4-pro","messages":[]}"#;
        let (status, _body) =
            send_request(app, "POST", "/v1/chat/completions", Some(body.to_string())).await;
        assert_eq!(status, StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn chat_accepts_minimal_non_stream_request() {
        let app = create_app();
        let body = r#"{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hello"}]}"#;
        let (status, response_body) =
            send_request(app, "POST", "/v1/chat/completions", Some(body.to_string())).await;
        assert_eq!(status, StatusCode::OK);
        let response: ChatCompletionResponse = serde_json::from_str(&response_body).unwrap();
        assert_eq!(response.object, "chat.completion");
        assert_eq!(response.model, "deepseek-v4-pro");
        assert_eq!(response.choices.len(), 1);
        assert_eq!(response.choices[0].message.role, "assistant");
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
        assert_eq!(
            serde_json::from_str::<serde_json::Value>(&alias_response).unwrap()["routing"]["owner"],
            "python"
        );
    }

    #[tokio::test]
    async fn chat_rejects_streaming_for_mvp() {
        let app = create_app();
        let body = r#"{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hello"}],"stream":true}"#;
        let (status, _body) =
            send_request(app, "POST", "/v1/chat/completions", Some(body.to_string())).await;
        assert_eq!(status, StatusCode::NOT_IMPLEMENTED);
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
