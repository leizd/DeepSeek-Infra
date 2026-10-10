//! `POST /api/conversations/search`.
//!
//! The search runs on the conversation list the client already holds. It does not
//! read a server store. The match rules are `conversation_search` and
//! `conversation_search_matches` in `deepseek_infra/web/server.py`.

use axum::Json;
use axum::body::Bytes;
use axum::extract::rejection::BytesRejection;
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use serde_json::{Value, json};

const MAX_CONVERSATIONS: usize = 200;
const MAX_RESULTS: usize = 50;
const MAX_MATCHES: usize = 5;

pub async fn api_conversation_search(
    headers: HeaderMap,
    body: Result<Bytes, BytesRejection>,
) -> Response {
    match read_object(&headers, body) {
        Ok(payload) => match search(&payload) {
            Ok(value) => Json(value).into_response(),
            Err(error) => error_response(error),
        },
        Err(error) => error_response(error),
    }
}

pub(crate) fn search(payload: &Value) -> Result<Value, AppError> {
    let query = payload
        .get("query")
        .filter(|value| python_truthy(value))
        .map(value_str)
        .unwrap_or_default();
    let query = query.trim().to_lowercase();
    if query.is_empty() {
        return Ok(json!({"results": []}));
    }
    let Some(conversations) = payload.get("conversations").and_then(Value::as_array) else {
        return Err(AppError::invalid_payload("conversations must be a list"));
    };
    let mut results = Vec::new();
    for conversation in conversations.iter().take(MAX_CONVERSATIONS) {
        let Some(conversation) = conversation.as_object() else {
            continue;
        };
        let matches = search_matches(conversation, &query);
        if matches.is_empty() {
            continue;
        }
        let title = payload_text(conversation.get("title"), "New conversation");
        results.push(json!({
            "id": payload_text(conversation.get("id"), ""),
            "title": chars_at_most(&title, 160),
            "updatedAt": conversation.get("updatedAt").cloned().unwrap_or(Value::Null),
            "favorite": conversation.get("favorite").is_some_and(python_truthy),
            "tags": conversation_tags(conversation),
            "matches": matches.into_iter().take(MAX_MATCHES).collect::<Vec<_>>(),
        }));
        if results.len() == MAX_RESULTS {
            break;
        }
    }
    Ok(json!({"results": results}))
}

fn search_matches(conversation: &serde_json::Map<String, Value>, query: &str) -> Vec<Value> {
    let mut haystacks = vec![
        (
            "title".to_string(),
            String::new(),
            payload_text(conversation.get("title"), ""),
        ),
        (
            "tag".to_string(),
            String::new(),
            conversation_tags(conversation).join(" "),
        ),
    ];
    if let Some(messages) = conversation.get("messages").and_then(Value::as_array) {
        for message in messages {
            let Some(message) = message.as_object() else {
                continue;
            };
            let text = format!(
                "{} {} {}",
                payload_text(message.get("role"), ""),
                payload_text(message.get("content"), ""),
                payload_text(message.get("reasoning"), "")
            );
            haystacks.push((
                "message".to_string(),
                payload_text(message.get("id"), ""),
                text,
            ));
        }
    }
    let mut matches = Vec::new();
    for (kind, message_id, text) in haystacks {
        let Some(snippet) = snippet(&text, query) else {
            continue;
        };
        matches.push(json!({"kind": kind, "messageId": message_id, "snippet": snippet}));
    }
    matches
}

/// `str(value or default)`, then the caller truncates where the oracle does.
fn payload_text(value: Option<&Value>, default: &str) -> String {
    value
        .filter(|item| python_truthy(item))
        .map(value_str)
        .filter(|text| !text.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn conversation_tags(conversation: &serde_json::Map<String, Value>) -> Vec<String> {
    let Some(tags) = conversation.get("tags").and_then(Value::as_array) else {
        return Vec::new();
    };
    tags.iter()
        .filter_map(|tag| {
            let text = if python_truthy(tag) {
                value_str(tag)
            } else {
                String::new()
            };
            let text = text.trim();
            if text.is_empty() {
                None
            } else {
                Some(chars_at_most(text, 32))
            }
        })
        .take(12)
        .collect()
}

fn snippet(text: &str, query: &str) -> Option<String> {
    let lowered = text.to_lowercase();
    let byte = lowered.find(query)?;
    let index = lowered[..byte].chars().count();
    let chars: Vec<char> = text.chars().collect();
    let start = index.saturating_sub(48);
    let end = (index + query.chars().count() + 96).min(chars.len());
    if start >= end {
        return Some(String::new());
    }
    Some(
        chars[start..end]
            .iter()
            .collect::<String>()
            .trim()
            .to_string(),
    )
}

fn chars_at_most(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

fn read_object(
    headers: &HeaderMap,
    body: Result<Bytes, BytesRejection>,
) -> Result<Value, AppError> {
    let length = content_length(headers)?;
    if length == 0 {
        return Err(AppError::invalid_payload("Request body is empty"));
    }
    if length > 2_000_000 {
        return Err(AppError {
            status: 413,
            code: codes::UPLOAD_TOO_LARGE,
            message: "Request body is too large".into(),
        });
    }
    let bytes = body.map_err(|_| AppError {
        status: 413,
        code: codes::UPLOAD_TOO_LARGE,
        message: "Request body is too large".into(),
    })?;
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|error| AppError::invalid_payload(format!("Invalid JSON: {error}")))?;
    if !value.is_object() {
        return Err(AppError::invalid_payload(
            "Request body must be a JSON object",
        ));
    }
    Ok(value)
}

fn content_length(headers: &HeaderMap) -> Result<usize, AppError> {
    let raw = headers
        .get("content-length")
        .and_then(|value| value.to_str().ok())
        .unwrap_or("0");
    let length = raw
        .parse::<i64>()
        .map_err(|_| AppError::invalid_payload("Invalid Content-Length"))?;
    if length < 0 {
        return Err(AppError::invalid_payload("Invalid Content-Length"));
    }
    Ok(length as usize)
}

fn error_response(error: AppError) -> Response {
    (
        StatusCode::from_u16(error.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error": error.message, "code": error.code})),
    )
        .into_response()
}
