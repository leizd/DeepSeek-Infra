use serde_json::{Map, Value, json};
use std::collections::HashSet;

pub const MAX_REQUEST_BYTES: usize = 16_000_000;
pub const CATALOG_MODEL_IDS: [&str; 2] = ["deepseek-v4-pro", "deepseek-v4-flash"];
/// Mirrors `deepseek_client.MESSAGE_HARD_LIMIT`: above this many normalized
/// messages the Python oracle demands prior context compression.
pub const MESSAGE_HARD_LIMIT: usize = 40;
const MAX_REQUEST_DEPTH: usize = 32;
const MAX_TOKENS: i64 = 131_072;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PreparationError {
    pub code: &'static str,
    pub message: &'static str,
}

impl PreparationError {
    fn new(code: &'static str, message: &'static str) -> Self {
        Self { code, message }
    }

    pub fn response(&self) -> Value {
        json!({"ok": false, "code": self.code, "message": self.message})
    }
}

fn depth(value: &Value, current: usize) -> usize {
    if current > MAX_REQUEST_DEPTH {
        return current;
    }
    match value {
        Value::Array(items) => items
            .iter()
            .map(|item| depth(item, current + 1))
            .max()
            .unwrap_or(current),
        Value::Object(items) => items
            .values()
            .map(|item| depth(item, current + 1))
            .max()
            .unwrap_or(current),
        _ => current,
    }
}

fn normalized_model(value: Option<&Value>) -> Result<String, PreparationError> {
    let raw = value.and_then(Value::as_str).map(str::trim).unwrap_or("");
    if raw.is_empty() {
        return Err(PreparationError::new(
            "unsupported_model",
            "model must be a non-empty string",
        ));
    }
    let key = raw.to_lowercase().replace('_', "-").replace(' ', "");
    let normalized = match key.as_str() {
        "deepseek-v4-pro" | "deepseekv4pro" | "v4pro" | "expert" => CATALOG_MODEL_IDS[0],
        "deepseek-v4-flash" | "deepseekv4flash" | "v4flash" | "flash" | "fast" => {
            CATALOG_MODEL_IDS[1]
        }
        _ => {
            return Err(PreparationError::new(
                "unsupported_model",
                "unsupported model",
            ));
        }
    };
    Ok(normalized.to_string())
}

/// Mirrors the oracle's `contextSummary` escape hatch: a non-blank summary
/// lifts the message-count ceiling. Both accept a non-string value as absent,
/// matching `str(payload.get("contextSummary") or "").strip()`.
fn context_summary_present(object: &Map<String, Value>) -> bool {
    object
        .get("contextSummary")
        .and_then(Value::as_str)
        .map(str::trim)
        .is_some_and(|summary| !summary.is_empty())
}

pub fn native_model_catalog(created: i64) -> Value {
    json!({
        "object": "list",
        "data": CATALOG_MODEL_IDS.iter().map(|id| json!({
            "id": id,
            "object": "model",
            "created": created,
            "owned_by": "deepseek-infra",
        })).collect::<Vec<_>>(),
    })
}

fn normalize_content(value: Option<&Value>, allow_empty: bool) -> Result<Value, PreparationError> {
    if let Some(text) = value.and_then(Value::as_str) {
        let trimmed = text.trim();
        if trimmed.is_empty() && !allow_empty {
            return Err(PreparationError::new(
                "invalid_message_content",
                "message content must not be empty",
            ));
        }
        return Ok(Value::String(trimmed.to_string()));
    }
    let parts = value.and_then(Value::as_array).ok_or_else(|| {
        PreparationError::new(
            "invalid_message_content",
            "message content must be a string or non-empty content array",
        )
    })?;
    if parts.is_empty() {
        return Err(PreparationError::new(
            "invalid_message_content",
            "message content must be a string or non-empty content array",
        ));
    }
    let mut normalized = Vec::with_capacity(parts.len());
    for part in parts {
        let object = part.as_object().ok_or_else(|| {
            PreparationError::new(
                "invalid_message_content",
                "message content parts must be objects",
            )
        })?;
        match object.get("type").and_then(Value::as_str) {
            Some("text") => {
                let text = object
                    .get("text")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .unwrap_or("");
                if text.is_empty() {
                    return Err(PreparationError::new(
                        "invalid_message_content",
                        "unsupported message content part",
                    ));
                }
                normalized.push(json!({"type": "text", "text": text}));
            }
            Some("image_url") => {
                let image = object
                    .get("image_url")
                    .and_then(Value::as_object)
                    .ok_or_else(|| {
                        PreparationError::new(
                            "invalid_message_content",
                            "unsupported message content part",
                        )
                    })?;
                let url = image
                    .get("url")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .unwrap_or("");
                if url.is_empty() {
                    return Err(PreparationError::new(
                        "invalid_message_content",
                        "unsupported message content part",
                    ));
                }
                let mut normalized_image = Map::new();
                normalized_image.insert("url".to_string(), Value::String(url.to_string()));
                if let Some(detail @ ("auto" | "low" | "high")) =
                    image.get("detail").and_then(Value::as_str)
                {
                    normalized_image
                        .insert("detail".to_string(), Value::String(detail.to_string()));
                }
                normalized.push(json!({"type": "image_url", "image_url": normalized_image}));
            }
            _ => {
                return Err(PreparationError::new(
                    "invalid_message_content",
                    "unsupported message content part",
                ));
            }
        }
    }
    Ok(Value::Array(normalized))
}

fn normalize_tool_calls(value: Option<&Value>) -> Result<Value, PreparationError> {
    let calls = value
        .and_then(Value::as_array)
        .filter(|items| !items.is_empty())
        .ok_or_else(|| {
            PreparationError::new(
                "invalid_message_content",
                "assistant tool_calls must be a non-empty array",
            )
        })?;
    let mut normalized = Vec::with_capacity(calls.len());
    for call in calls {
        let object = call.as_object().ok_or_else(|| {
            PreparationError::new(
                "invalid_message_content",
                "assistant tool calls must be objects",
            )
        })?;
        let function = object
            .get("function")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                PreparationError::new(
                    "invalid_message_content",
                    "assistant tool call function must be an object",
                )
            })?;
        let name = function
            .get("name")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        let arguments = function
            .get("arguments")
            .and_then(Value::as_str)
            .unwrap_or("");
        if name.is_empty() || !function.get("arguments").is_none_or(Value::is_string) {
            return Err(PreparationError::new(
                "invalid_message_content",
                "assistant tool call name and arguments are invalid",
            ));
        }
        let mut normalized_call = Map::new();
        normalized_call.insert("type".to_string(), Value::String("function".to_string()));
        normalized_call.insert(
            "function".to_string(),
            json!({"name": name, "arguments": arguments}),
        );
        if let Some(id) = object.get("id").and_then(Value::as_str).map(str::trim) {
            if !id.is_empty() {
                normalized_call.insert("id".to_string(), Value::String(id.to_string()));
            }
        }
        normalized.push(Value::Object(normalized_call));
    }
    Ok(Value::Array(normalized))
}

fn normalize_messages(value: Option<&Value>) -> Result<Value, PreparationError> {
    let messages = value
        .and_then(Value::as_array)
        .filter(|items| !items.is_empty())
        .ok_or_else(|| {
            PreparationError::new("invalid_messages", "messages must be a non-empty array")
        })?;
    let mut normalized = Vec::with_capacity(messages.len());
    for message in messages {
        let object = message.as_object().ok_or_else(|| {
            PreparationError::new("invalid_messages", "each message must be an object")
        })?;
        let role = object.get("role").and_then(Value::as_str).unwrap_or("");
        if !matches!(role, "system" | "user" | "assistant" | "tool") {
            return Err(PreparationError::new(
                "invalid_message_role",
                "unsupported message role",
            ));
        }
        let has_tool_calls = role == "assistant" && object.contains_key("tool_calls");
        let mut item = Map::new();
        item.insert("role".to_string(), Value::String(role.to_string()));
        item.insert(
            "content".to_string(),
            normalize_content(object.get("content"), has_tool_calls)?,
        );
        if has_tool_calls {
            item.insert(
                "tool_calls".to_string(),
                normalize_tool_calls(object.get("tool_calls"))?,
            );
        }
        if role == "tool" {
            let tool_call_id = object
                .get("tool_call_id")
                .and_then(Value::as_str)
                .map(str::trim)
                .unwrap_or("");
            if tool_call_id.is_empty() {
                return Err(PreparationError::new(
                    "invalid_message_content",
                    "tool messages require tool_call_id",
                ));
            }
            item.insert(
                "tool_call_id".to_string(),
                Value::String(tool_call_id.to_string()),
            );
        }
        normalized.push(Value::Object(item));
    }
    Ok(Value::Array(normalized))
}

fn normalize_tools(value: Option<&Value>) -> Result<Vec<Value>, PreparationError> {
    let tools = value
        .and_then(Value::as_array)
        .ok_or_else(|| PreparationError::new("invalid_tools", "tools must be an array"))?;
    let mut normalized = Vec::with_capacity(tools.len());
    for tool in tools {
        let object = tool.as_object().ok_or_else(|| {
            PreparationError::new("invalid_tools", "tools must be function definitions")
        })?;
        if object.get("type").and_then(Value::as_str) != Some("function") {
            return Err(PreparationError::new(
                "invalid_tools",
                "tools must be function definitions",
            ));
        }
        let function = object
            .get("function")
            .and_then(Value::as_object)
            .ok_or_else(|| {
                PreparationError::new("invalid_tools", "tool function must be an object")
            })?;
        let name = function
            .get("name")
            .and_then(Value::as_str)
            .map(str::trim)
            .unwrap_or("");
        if name.is_empty() {
            return Err(PreparationError::new(
                "invalid_tools",
                "tool function name is required",
            ));
        }
        let parameters = function
            .get("parameters")
            .unwrap_or(&Value::Object(Map::new()))
            .clone();
        if !parameters.is_object() {
            return Err(PreparationError::new(
                "invalid_tools",
                "tool parameters must be an object",
            ));
        }
        let mut normalized_function = Map::new();
        normalized_function.insert("name".to_string(), Value::String(name.to_string()));
        normalized_function.insert("parameters".to_string(), parameters);
        if let Some(description) = function
            .get("description")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            normalized_function.insert(
                "description".to_string(),
                Value::String(description.to_string()),
            );
        }
        if let Some(strict) = function.get("strict").and_then(Value::as_bool) {
            normalized_function.insert("strict".to_string(), Value::Bool(strict));
        }
        normalized.push(json!({"type": "function", "function": normalized_function}));
    }
    Ok(normalized)
}

fn normalize_tool_choice(
    value: Option<&Value>,
    tool_names: &HashSet<String>,
) -> Result<Value, PreparationError> {
    if let Some(choice @ ("auto" | "none" | "required")) = value.and_then(Value::as_str) {
        return Ok(Value::String(choice.to_string()));
    }
    if let Some(object) = value.and_then(Value::as_object) {
        let name = object
            .get("function")
            .and_then(Value::as_object)
            .and_then(|function| function.get("name"))
            .and_then(Value::as_str)
            .map(str::trim);
        if object.get("type").and_then(Value::as_str) == Some("function") {
            if let Some(name) = name {
                if !name.is_empty() && tool_names.contains(name) {
                    return Ok(json!({"type": "function", "function": {"name": name}}));
                }
            }
        }
    }
    Err(PreparationError::new(
        "invalid_tool_choice",
        "invalid tool_choice",
    ))
}

fn finite_number(
    value: Option<&Value>,
    code: &'static str,
    invalid_message: &'static str,
    range_message: &'static str,
    minimum: f64,
    maximum: f64,
) -> Result<f64, PreparationError> {
    let number = value
        .and_then(Value::as_f64)
        .ok_or_else(|| PreparationError::new(code, invalid_message))?;
    if !number.is_finite() || number < minimum || number > maximum {
        return Err(PreparationError::new(code, range_message));
    }
    Ok(number)
}

pub fn prepare_request(value: &Value) -> Result<Value, PreparationError> {
    let object = value
        .as_object()
        .ok_or_else(|| PreparationError::new("invalid_request", "request must be a JSON object"))?;
    if serde_json::to_vec(value).map_or(true, |bytes| bytes.len() > MAX_REQUEST_BYTES)
        || depth(value, 0) > MAX_REQUEST_DEPTH
    {
        return Err(PreparationError::new(
            "request_too_large",
            "request exceeds the preparation budget",
        ));
    }
    if object.keys().any(|key| {
        matches!(
            key.to_lowercase().as_str(),
            "authorization"
                | "api_key"
                | "apikey"
                | "deepseek_api_key"
                | "localbaseurl"
                | "local_base_url"
                | "file_path"
                | "filepath"
        )
    }) {
        return Err(PreparationError::new(
            "invalid_request",
            "credentials and local paths are not accepted",
        ));
    }

    let mut request = Map::new();
    request.insert(
        "model".to_string(),
        Value::String(normalized_model(object.get("model"))?),
    );
    let messages = normalize_messages(object.get("messages"))?;
    // Parity with `deepseek_client._validate_request_messages`: both rules are
    // evaluated against *normalized* messages, because normalization drops
    // empty-content turns. A request whose only user turn has blank content
    // therefore still fails the user-message requirement.
    let normalized_turns = messages.as_array().map(Vec::as_slice).unwrap_or_default();
    if normalized_turns.len() > MESSAGE_HARD_LIMIT && !context_summary_present(object) {
        return Err(PreparationError::new(
            "context_compression_required",
            "context compression is required before sending more than 40 messages",
        ));
    }
    if !normalized_turns
        .iter()
        .any(|turn| turn.get("role").and_then(Value::as_str) == Some("user"))
    {
        return Err(PreparationError::new(
            "missing_user_message",
            "a user message is required",
        ));
    }
    request.insert("messages".to_string(), messages);
    if object.get("stream") == Some(&Value::Bool(true)) {
        return Err(PreparationError::new(
            "invalid_request",
            "streaming requests stay on the Python path",
        ));
    }
    if object.contains_key("stream") {
        if object.get("stream") != Some(&Value::Bool(false)) {
            return Err(PreparationError::new(
                "invalid_request",
                "stream must be a boolean",
            ));
        }
        request.insert("stream".to_string(), Value::Bool(false));
    }

    let mut tools = Vec::new();
    if object.contains_key("tools") {
        tools = normalize_tools(object.get("tools"))?;
        if !tools.is_empty() {
            request.insert("tools".to_string(), Value::Array(tools.clone()));
        }
    }
    if object.contains_key("tool_choice") {
        let tool_names = tools
            .iter()
            .filter_map(|tool| {
                tool.get("function")
                    .and_then(Value::as_object)
                    .and_then(|function| function.get("name"))
                    .and_then(Value::as_str)
                    .map(ToString::to_string)
            })
            .collect();
        let choice = normalize_tool_choice(object.get("tool_choice"), &tool_names)?;
        if !tools.is_empty() || choice == Value::String("none".to_string()) {
            request.insert("tool_choice".to_string(), choice);
        }
    }

    if object.contains_key("temperature") {
        request.insert(
            "temperature".to_string(),
            json!(finite_number(
                object.get("temperature"),
                "invalid_temperature",
                "temperature must be a finite number",
                "temperature is outside the supported range",
                0.0,
                2.0,
            )?),
        );
    }
    if object.contains_key("top_p") {
        request.insert(
            "top_p".to_string(),
            json!(finite_number(
                object.get("top_p"),
                "invalid_request",
                "top_p must be a finite number",
                "top_p is outside the supported range",
                0.0,
                1.0,
            )?),
        );
    }
    if object.contains_key("max_tokens") {
        let max_tokens = object
            .get("max_tokens")
            .and_then(Value::as_i64)
            .filter(|value| (1..=MAX_TOKENS).contains(value))
            .ok_or_else(|| {
                PreparationError::new(
                    "invalid_max_tokens",
                    "max_tokens is outside the supported range",
                )
            })?;
        request.insert("max_tokens".to_string(), json!(max_tokens));
    }
    if object.contains_key("reasoning_effort") {
        let effort = object.get("reasoning_effort").and_then(Value::as_str);
        if !matches!(effort, Some("minimal" | "low" | "medium" | "high" | "max")) {
            return Err(PreparationError::new(
                "invalid_request",
                "invalid reasoning_effort",
            ));
        }
        request.insert(
            "reasoning_effort".to_string(),
            Value::String(effort.unwrap_or_default().to_string()),
        );
    }
    if object.contains_key("thinking") {
        if object.get("thinking") != Some(&json!({"type": "enabled"})) {
            return Err(PreparationError::new(
                "invalid_request",
                "invalid thinking configuration",
            ));
        }
        request.insert("thinking".to_string(), json!({"type": "enabled"}));
    }
    Ok(Value::Object(request))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn minimal() -> Value {
        json!({"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": " hello "}]})
    }

    #[test]
    fn native_catalog_ids_are_the_models_prepare_request_accepts() {
        let catalog = native_model_catalog(1);
        assert_eq!(catalog["object"], "list");
        let ids: Vec<&str> = catalog["data"]
            .as_array()
            .unwrap()
            .iter()
            .map(|entry| entry["id"].as_str().unwrap())
            .collect();
        assert_eq!(ids, CATALOG_MODEL_IDS);
        for id in CATALOG_MODEL_IDS {
            assert_eq!(
                normalized_model(Some(&Value::String(id.to_string()))).unwrap(),
                id
            );
        }
    }

    #[test]
    fn valid_minimal_request() {
        let prepared = prepare_request(&minimal()).unwrap();
        assert_eq!(prepared["messages"][0]["content"], "hello");
    }

    #[test]
    fn rejects_request_without_a_user_turn() {
        // Oracle: `_validate_request_messages` requires at least one user turn.
        let request = json!({"model": "deepseek-v4-pro", "messages": [{"role": "assistant", "content": "hi"}]});
        let error = prepare_request(&request).unwrap_err();
        assert_eq!(error.code, "missing_user_message");
    }

    #[test]
    fn rejects_a_blank_turn_instead_of_dropping_it() {
        // Measured against the real oracle (`normalize_chat_messages`): Python
        // silently drops a blank-content turn and accepts the request, so
        // `[{user:"  "},{user:"real"}]` becomes a single user turn. Dropping a
        // turn the caller sent is silent data loss - the caller gets no signal
        // that part of its input never reached the model - so this crate
        // rejects instead. Rejecting is the strict direction; relaxing it to
        // match the oracle would turn a rejection into an acceptance.
        let request = json!({
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "user", "content": "   "},
                {"role": "user", "content": "real"},
            ],
        });
        let error = prepare_request(&request).unwrap_err();
        assert_eq!(error.code, "invalid_message_content");
    }

    #[test]
    fn rejects_a_blank_assistant_turn_instead_of_dropping_it() {
        // Same divergence, assistant side. Python drops it; this crate refuses
        // the whole request rather than building a prompt that differs from
        // what the caller sent.
        let request = json!({
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "assistant", "content": ""},
                {"role": "user", "content": "real"},
            ],
        });
        let error = prepare_request(&request).unwrap_err();
        assert_eq!(error.code, "invalid_message_content");
    }

    #[test]
    fn rejects_non_object_messages_instead_of_ignoring_them() {
        // Second measured divergence: Python's `isinstance(message, dict)`
        // guard skips a non-object entry and still accepts the request. That is
        // the same silent-loss class as the blank-turn case, so this crate
        // reports it instead of quietly discarding it.
        let request = json!({
            "model": "deepseek-v4-pro",
            "messages": ["nope", {"role": "user", "content": "real"}],
        });
        let error = prepare_request(&request).unwrap_err();
        assert_eq!(error.code, "invalid_messages");
    }

    #[test]
    fn rejects_a_tool_message_without_tool_call_id_instead_of_dropping_it() {
        // Third measured divergence: Python drops a `tool` turn whose
        // `tool_call_id` is missing, which silently breaks the
        // assistant.tool_calls <-> tool-result pairing the upstream API
        // requires. Refusing surfaces the malformed request at the edge.
        let request = json!({
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "tool", "content": "x"},
                {"role": "user", "content": "real"},
            ],
        });
        let error = prepare_request(&request).unwrap_err();
        assert_eq!(error.code, "invalid_message_content");
    }

    #[test]
    fn keeps_system_turns_where_the_oracle_drops_them() {
        // Fourth measured divergence, and the only one where this crate is the
        // *looser* side. Python accepts only `user`/`assistant` during
        // normalization and therefore drops every `system` turn the caller
        // sends. `build_deepseek_request` constructs system turns itself, so
        // system is a first-class role on this protocol; dropping caller-
        // supplied ones would be a capability removal, not a strictness
        // alignment. This test pins the retained behavior so the difference
        // cannot disappear unnoticed.
        let request = json!({
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "real"},
            ],
        });
        let prepared = prepare_request(&request).unwrap();
        let roles: Vec<&str> = prepared["messages"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|message| message["role"].as_str())
            .collect();
        assert_eq!(roles, ["system", "user"]);
        assert_eq!(prepared["messages"][0]["content"], "be terse");
    }

    #[test]
    fn blank_only_turns_agree_with_the_oracle_by_both_refusing() {
        // Not a divergence: Python drops the blank turn and then fails its
        // user-turn requirement; this crate rejects the blank content. Both
        // refuse, so the observable contract agrees. Only the code differs.
        let request =
            json!({"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": "   "}]});
        let error = prepare_request(&request).unwrap_err();
        assert!(
            matches!(
                error.code,
                "missing_user_message" | "invalid_message_content"
            ),
            "unexpected code: {}",
            error.code
        );
    }

    #[test]
    fn rejects_more_than_message_hard_limit_without_context_summary() {
        let mut messages = Vec::new();
        for index in 0..=MESSAGE_HARD_LIMIT {
            messages.push(json!({"role": "user", "content": format!("turn {index}")}));
        }
        let request = json!({"model": "deepseek-v4-pro", "messages": messages});
        let error = prepare_request(&request).unwrap_err();
        assert_eq!(error.code, "context_compression_required");
    }

    #[test]
    fn context_summary_lifts_the_message_ceiling() {
        let mut messages = Vec::new();
        for index in 0..=MESSAGE_HARD_LIMIT {
            messages.push(json!({"role": "user", "content": format!("turn {index}")}));
        }
        let request = json!({
            "model": "deepseek-v4-pro",
            "messages": messages,
            "contextSummary": "earlier turns were compressed",
        });
        let prepared = prepare_request(&request).unwrap();
        assert_eq!(
            prepared["messages"].as_array().unwrap().len(),
            MESSAGE_HARD_LIMIT + 1
        );
    }

    #[test]
    fn blank_context_summary_does_not_lift_the_message_ceiling() {
        let mut messages = Vec::new();
        for index in 0..=MESSAGE_HARD_LIMIT {
            messages.push(json!({"role": "user", "content": format!("turn {index}")}));
        }
        let request = json!({
            "model": "deepseek-v4-pro",
            "messages": messages,
            "contextSummary": "   ",
        });
        let error = prepare_request(&request).unwrap_err();
        assert_eq!(error.code, "context_compression_required");
    }

    #[test]
    fn normalizes_messages() {
        let prepared = prepare_request(&json!({"model":"fast","messages":[{"role":"system","content":" rules "},{"role":"user","content":" hi "}]})).unwrap();
        assert_eq!(prepared["model"], "deepseek-v4-flash");
        assert_eq!(prepared["messages"][0]["content"], "rules");
    }

    #[test]
    fn validates_model() {
        let error = prepare_request(
            &json!({"model":"unknown","messages":[{"role":"user","content":"hi"}]}),
        )
        .unwrap_err();
        assert_eq!(error.code, "unsupported_model");
    }

    #[test]
    fn filters_tools() {
        let prepared = prepare_request(&json!({"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hi"}],"tools":[{"type":"function","ignored":true,"function":{"name":" echo ","description":" echo text ","parameters":{"type":"object"},"ignored":true}}],"tool_choice":"auto"})).unwrap();
        assert!(prepared["tools"][0].get("ignored").is_none());
        assert_eq!(prepared["tools"][0]["function"]["name"], "echo");
    }

    #[test]
    fn validates_tool_choice() {
        let error = prepare_request(&json!({"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hi"}],"tools":[],"tool_choice":{"type":"function","function":{"name":"missing"}}})).unwrap_err();
        assert_eq!(error.code, "invalid_tool_choice");
    }

    #[test]
    fn rejects_invalid_roles() {
        let error = prepare_request(
            &json!({"model":"deepseek-v4-pro","messages":[{"role":"owner","content":"hi"}]}),
        )
        .unwrap_err();
        assert_eq!(error.code, "invalid_message_role");
    }

    #[test]
    fn rejects_invalid_content() {
        let error = prepare_request(
            &json!({"model":"deepseek-v4-pro","messages":[{"role":"user","content":3}]}),
        )
        .unwrap_err();
        assert_eq!(error.code, "invalid_message_content");
    }

    #[test]
    fn rejects_non_finite_numbers() {
        let mut request = minimal();
        request["temperature"] =
            Value::Number(serde_json::Number::from_f64(f64::INFINITY).unwrap_or_else(|| 0.into()));
        request["temperature"] = Value::String("NaN".to_string());
        assert_eq!(
            prepare_request(&request).unwrap_err().code,
            "invalid_temperature"
        );
    }

    #[test]
    fn rejects_oversized_requests() {
        let mut request = minimal();
        request["extra"] = Value::String("x".repeat(MAX_REQUEST_BYTES));
        assert_eq!(
            prepare_request(&request).unwrap_err().code,
            "request_too_large"
        );
    }

    #[test]
    fn preserves_cjk_and_emoji() {
        let prepared = prepare_request(&json!({"model":"deepseek-v4-pro","messages":[{"role":"user","content":"你好 Rust 🚀"}]})).unwrap();
        assert_eq!(prepared["messages"][0]["content"], "你好 Rust 🚀");
    }

    #[test]
    fn response_roundtrips_json() {
        let prepared = prepare_request(&minimal()).unwrap();
        let encoded = serde_json::to_string(&prepared).unwrap();
        assert_eq!(serde_json::from_str::<Value>(&encoded).unwrap(), prepared);
    }
}
