use axum::{
    Json, Router,
    http::StatusCode,
    routing::{get, post},
};
use serde::{Deserialize, Serialize};
use serde_json::json;

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
        .layer(tower_http::trace::TraceLayer::new_for_http())
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
    async fn chat_rejects_streaming_for_mvp() {
        let app = create_app();
        let body = r#"{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hello"}],"stream":true}"#;
        let (status, _body) =
            send_request(app, "POST", "/v1/chat/completions", Some(body.to_string())).await;
        assert_eq!(status, StatusCode::NOT_IMPLEMENTED);
    }
}
