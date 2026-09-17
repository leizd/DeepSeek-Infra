//! The message layer of request validation — `normalize_chat_messages` and the two checks
//! that run before a request is assembled.
//!
//! **Fail-closed by design.** An earlier revision of the oracle silently `continue`-ed past
//! every turn it could not represent: non-dict entries, blank or `None` content, `tool` turns
//! missing `tool_call_id`, and every `system` turn. The caller's instruction then reached the
//! model as if it had never been written — the request succeeded and the answer quietly
//! ignored what the user had said. Each unrepresentable turn now raises instead, with the
//! same codes the gateway's own preparation layer returns, so both paths fail identically.
//!
//! Dropping the `system` role defended nothing either: this layer's callers build the
//! authoritative system prefix themselves from `payload["systemPrompt"]`, so a `system` turn
//! arriving in `messages` is not a duplicate to prune — it is caller intent being discarded.
//!
//! **The content expander is injected.** `expanded_message_content` in the oracle expands
//! attachments through `build_attachment_context`, which reads the file index — that is I/O,
//! and it belongs to the file-store slice. Everything in this module is pure, so the expander
//! arrives as a parameter, exactly as the clock and the transport do elsewhere in this crate.
//! A caller that has no file store can pass a plain-content expander; one that does passes the
//! real thing, and this layer does not change.

use serde_json::{Map, Value};

use crate::app_error::{AppError, codes};
use crate::core_utils::normalize_model_name;
use crate::model_router::{ModelRouterSettings, is_auto_request, route_request};
use crate::python_json::{dumps_compact, dumps_default_separators};

/// Mirrors `MESSAGE_HARD_LIMIT`: above this many messages, a context summary is required.
pub const MESSAGE_HARD_LIMIT: usize = 40;

/// Mirrors `ValidatedPayload`: the three values every later layer needs.
#[derive(Debug, Clone, PartialEq)]
pub struct ValidatedPayload {
    pub api_key: String,
    pub model: String,
    pub messages: Vec<Value>,
}

/// Mirrors `stable_tool_call_id`.
///
/// The name is lower-cased, every non-alphanumeric character becomes an underscore, the
/// result is trimmed of underscores (falling back to `"tool"` if nothing is left) and cut to
/// **48 characters**.
pub fn stable_tool_call_id(index: usize, name: &str) -> String {
    let mapped: String = name
        .to_lowercase()
        .chars()
        .map(|character| {
            if character.is_alphanumeric() {
                character
            } else {
                '_'
            }
        })
        .collect();
    let safe_name = mapped.trim_matches('_');
    let safe_name = if safe_name.is_empty() {
        "tool"
    } else {
        safe_name
    };
    let capped: String = safe_name.chars().take(48).collect();
    format!("call_{}_{}", index + 1, capped)
}

/// Mirrors `canonical_tool_arguments`.
///
/// A string is parsed and re-serialized canonically; a string that is not JSON comes back
/// trimmed; anything else is serialized as-is. The canonical form is the compact dumper,
/// which is also what keeps key order stable.
pub fn canonical_tool_arguments(arguments: &Value) -> String {
    match arguments {
        Value::String(text) => match serde_json::from_str::<Value>(text) {
            Ok(parsed) => dumps_compact(&parsed),
            Err(_) => text.trim().to_string(),
        },
        other => dumps_compact(other),
    }
}

/// Mirrors `normalize_tool_calls`.
///
/// Note the asymmetry with [`normalize_chat_messages`]: **here** an unrepresentable entry is
/// skipped rather than raised, because a tool call is metadata the model already answered
/// with — the oracle's own choice, kept as-is. `stable_ids` and `canonical_arguments` are the
/// oracle's two flags; the message layer calls it with both off.
///
/// # Measured divergence, kept on purpose
///
/// When `arguments` is a JSON **object** rather than a string, the oracle re-serializes it
/// with `json.dumps`, which preserves the caller's key order. This port emits **sorted** keys,
/// because `serde_json::Map` is a `BTreeMap` here and this workspace deliberately does not
/// enable `preserve_order` — that switch would silently re-order every other response in the
/// crate. The order information is gone at parse time, so it cannot be recovered at this
/// layer. Unreachable from the wired path (the wire format carries tool arguments as a
/// string), and pinned by a unit test so the tolerance stays a decision rather than an
/// accident. The canonical path is unaffected: it sorts on both sides.
pub fn normalize_tool_calls(
    value: Option<&Value>,
    stable_ids: bool,
    canonical_arguments: bool,
) -> Vec<Value> {
    let Some(Value::Array(items)) = value else {
        return Vec::new();
    };
    let mut tool_calls: Vec<Value> = Vec::new();
    for (index, item) in items.iter().enumerate() {
        let Some(object) = item.as_object() else {
            continue;
        };
        let function = object.get("function").filter(|found| found.is_object());
        // `str(function.get("name") or item.get("name") or "").strip()`: the first *truthy*
        // raw value wins, and only then is it stringified and trimmed — so a name of blanks
        // falls through to the item's own name rather than stopping here.
        let raw_name = match function.and_then(|found| found.get("name")) {
            Some(value) if crate::core_utils::python_truthy(value) => value.clone(),
            _ => match object.get("name") {
                Some(value) if crate::core_utils::python_truthy(value) => value.clone(),
                _ => Value::Null,
            },
        };
        let name = text_or_empty(Some(&raw_name)).trim().to_string();
        if name.is_empty() {
            continue;
        }
        let arguments = function
            .and_then(|found| found.get("arguments"))
            .cloned()
            .unwrap_or(Value::String(String::new()));
        let normalized_arguments = if canonical_arguments {
            canonical_tool_arguments(&arguments)
        } else {
            match &arguments {
                Value::String(text) => text.clone(),
                other => dumps_default_separators(other),
            }
        };
        let tool_call_id = if stable_ids {
            stable_tool_call_id(index, &name)
        } else {
            match object.get("id") {
                Some(id) if crate::core_utils::python_truthy(id) => {
                    crate::python_json::value_str(id)
                }
                _ => format!("call_{}", index + 1),
            }
        };
        let call_type = match object.get("type") {
            Some(found) if crate::core_utils::python_truthy(found) => {
                crate::python_json::value_str(found)
            }
            _ => "function".to_string(),
        };
        let mut function_object = Map::new();
        function_object.insert("name".to_string(), Value::String(name));
        function_object.insert("arguments".to_string(), Value::String(normalized_arguments));
        let mut call = Map::new();
        call.insert("id".to_string(), Value::String(tool_call_id));
        call.insert("type".to_string(), Value::String(call_type));
        call.insert("function".to_string(), Value::Object(function_object));
        tool_calls.push(Value::Object(call));
    }
    tool_calls
}

/// Mirrors `_image_content_parts`: an attachment whose `imageData` is an inline `data:image/`
/// payload **longer than 32 characters** becomes an `image_url` part. The length guard is the
/// oracle's — a one-character payload is not an image.
pub fn image_content_parts(message: &Value) -> Vec<Value> {
    let Some(Value::Array(attachments)) = message.get("attachments") else {
        return Vec::new();
    };
    let mut parts: Vec<Value> = Vec::new();
    for attachment in attachments {
        if !attachment.is_object() {
            continue;
        }
        let image_data = text_or_empty(attachment.get("imageData"))
            .trim()
            .to_string();
        if image_data.starts_with("data:image/") && image_data.chars().count() > 32 {
            parts.push(serde_json::json!({"type": "image_url", "image_url": {"url": image_data}}));
        }
    }
    parts
}

/// Mirrors `normalize_chat_messages`.
///
/// `expand` is the injected content expander (see the module docs). Every rejection carries
/// the index of the turn it rejected, because the point of fail-closed here is that the
/// caller is *told* which turn could not be represented.
pub fn normalize_chat_messages(
    messages: &Value,
    expand: &dyn Fn(&Value) -> String,
) -> Result<Vec<Value>, AppError> {
    let Value::Array(items) = messages else {
        return Err(AppError {
            message: "messages must be an array".to_string(),
            code: codes::INVALID_MESSAGES,
            status: 400,
        });
    };
    let mut api_messages: Vec<Value> = Vec::new();
    for (index, message) in items.iter().enumerate() {
        if !message.is_object() {
            return Err(AppError {
                message: format!("message at index {index} must be an object"),
                code: codes::INVALID_MESSAGES,
                status: 400,
            });
        }
        let role = message.get("role").cloned().unwrap_or(Value::Null);
        let role_name = role.as_str().unwrap_or("");
        let content = expand(message);

        if role_name == "tool" {
            let tool_call_id = text_or_empty(message.get("tool_call_id"))
                .trim()
                .to_string();
            if content.trim().is_empty() {
                return Err(AppError {
                    message: format!("tool message at index {index} requires non-empty content"),
                    code: codes::INVALID_MESSAGE_CONTENT,
                    status: 400,
                });
            }
            if tool_call_id.is_empty() {
                return Err(AppError {
                    message: format!("tool message at index {index} requires tool_call_id"),
                    code: codes::INVALID_MESSAGE_CONTENT,
                    status: 400,
                });
            }
            let mut entry = Map::new();
            entry.insert("role".to_string(), Value::String("tool".to_string()));
            entry.insert("tool_call_id".to_string(), Value::String(tool_call_id));
            entry.insert(
                "content".to_string(),
                Value::String(content.trim().to_string()),
            );
            api_messages.push(Value::Object(entry));
            continue;
        }

        if role_name == "user" {
            let image_parts = image_content_parts(message);
            if !image_parts.is_empty() {
                let text = content.trim();
                let mut parts: Vec<Value> = Vec::new();
                if !text.is_empty() {
                    parts.push(serde_json::json!({"type": "text", "text": text}));
                }
                parts.extend(image_parts);
                let mut entry = Map::new();
                entry.insert("role".to_string(), Value::String("user".to_string()));
                entry.insert("content".to_string(), Value::Array(parts));
                api_messages.push(Value::Object(entry));
                continue;
            }
        }

        // The oracle also tests `isinstance(content, str)` here; the injected expander
        // returns a `String` by type, so only the role check can fail in this port.
        if !matches!(role_name, "system" | "user" | "assistant") {
            return Err(AppError {
                message: format!("message at index {index} has an unsupported role or content"),
                code: codes::INVALID_MESSAGE_CONTENT,
                status: 400,
            });
        }

        let tool_calls = if role_name == "assistant" {
            normalize_tool_calls(message.get("tool_calls"), false, false)
        } else {
            Vec::new()
        };
        if role_name == "assistant" && !tool_calls.is_empty() {
            let mut entry = Map::new();
            entry.insert("role".to_string(), Value::String(role_name.to_string()));
            entry.insert(
                "content".to_string(),
                Value::String(content.trim().to_string()),
            );
            entry.insert("tool_calls".to_string(), Value::Array(tool_calls));
            api_messages.push(Value::Object(entry));
            continue;
        }

        if content.trim().is_empty() {
            return Err(AppError {
                message: format!("message at index {index} has empty content"),
                code: codes::INVALID_MESSAGE_CONTENT,
                status: 400,
            });
        }
        let mut entry = Map::new();
        entry.insert("role".to_string(), Value::String(role_name.to_string()));
        entry.insert(
            "content".to_string(),
            Value::String(content.trim().to_string()),
        );
        api_messages.push(Value::Object(entry));
    }
    Ok(api_messages)
}

/// Mirrors `validate_deepseek_payload`.
///
/// `api_key_fallback` is the oracle's `settings.deepseek_api_key`; the port takes it as a
/// parameter so this crate never reaches for the environment itself. Auto-routing is resolved
/// here, so every later layer sees a concrete supported model.
pub fn validate_deepseek_payload(
    payload: &Value,
    api_key_fallback: &str,
    router: &ModelRouterSettings,
) -> Result<ValidatedPayload, AppError> {
    let api_key = match payload.get("apiKey") {
        Some(found) if crate::core_utils::python_truthy(found) => {
            crate::python_json::value_str(found)
        }
        _ => api_key_fallback.to_string(),
    };
    let api_key = api_key.trim().to_string();
    if api_key.is_empty() {
        return Err(AppError {
            message: "Missing DeepSeek API Key. Set DEEPSEEK_API_KEY or enter a key in settings."
                .to_string(),
            code: codes::MISSING_API_KEY,
            status: 400,
        });
    }

    let raw_model = match payload.get("model") {
        Some(found) if crate::core_utils::python_truthy(found) => Some(found.clone()),
        _ => Some(Value::String(router.default_model.clone())),
    };
    let mut model = normalize_model_name(raw_model.as_ref(), &router.model_aliases);
    if is_auto_request(payload, router) {
        model = route_request(payload, 0, router).model;
    }
    if !router.supported_models.contains(&model) {
        return Err(AppError {
            message: "Unsupported model".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }

    let messages = match payload.get("messages") {
        Some(Value::Array(items)) if !items.is_empty() => items.clone(),
        _ => {
            return Err(AppError {
                message: "At least one message is required".to_string(),
                code: codes::INVALID_PAYLOAD,
                status: 400,
            });
        }
    };

    Ok(ValidatedPayload {
        api_key,
        model,
        messages,
    })
}

/// Mirrors `_validate_request_messages`.
pub fn validate_request_messages(
    payload: &Value,
    messages: &[Value],
    expand: &dyn Fn(&Value) -> String,
) -> Result<(), AppError> {
    let context_summary = text_or_empty(payload.get("contextSummary"))
        .trim()
        .to_string();
    let normalized = normalize_chat_messages(&Value::Array(messages.to_vec()), expand)?;
    if normalized.len() > MESSAGE_HARD_LIMIT && context_summary.is_empty() {
        return Err(AppError {
            message: "Context compression required before sending more than 40 messages."
                .to_string(),
            code: codes::CONTEXT_COMPRESSION_REQUIRED,
            status: 409,
        });
    }
    let has_user = normalized
        .iter()
        .any(|item| item.get("role") == Some(&Value::String("user".to_string())));
    if !has_user {
        return Err(AppError {
            message: "A user message is required".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    Ok(())
}

/// Mirrors `preflight_deepseek_payload`.
pub fn preflight_deepseek_payload(
    payload: &Value,
    api_key_fallback: &str,
    router: &ModelRouterSettings,
    expand: &dyn Fn(&Value) -> String,
) -> Result<ValidatedPayload, AppError> {
    let validated = validate_deepseek_payload(payload, api_key_fallback, router)?;
    validate_request_messages(payload, &validated.messages, expand)?;
    Ok(validated)
}

/// `str(value or "")` without the strip.
fn text_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(found) if crate::core_utils::python_truthy(found) => {
            crate::python_json::value_str(found)
        }
        _ => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn expander(message: &Value) -> String {
        text_or_empty(message.get("content")).trim().to_string()
    }

    #[test]
    fn an_unrepresentable_turn_is_rejected_with_its_index() {
        let cases: Vec<(Value, &str, &str)> = vec![
            (
                json!([5]),
                "invalid_messages",
                "message at index 0 must be an object",
            ),
            (
                json!([{"role": "tool", "content": "r"}]),
                "invalid_message_content",
                "tool message at index 0 requires tool_call_id",
            ),
            (
                json!([{"role": "tool", "content": "  ", "tool_call_id": "c"}]),
                "invalid_message_content",
                "tool message at index 0 requires non-empty content",
            ),
            (
                json!([{"role": "ghost", "content": "x"}]),
                "invalid_message_content",
                "message at index 0 has an unsupported role or content",
            ),
            (
                json!([{"role": "user", "content": "   "}]),
                "invalid_message_content",
                "message at index 0 has empty content",
            ),
        ];
        for (messages, code, message) in cases {
            let error = normalize_chat_messages(&messages, &expander).expect_err("must reject");
            assert_eq!(error.code, code);
            assert_eq!(error.message, message);
        }
        assert_eq!(
            normalize_chat_messages(&json!("not-a-list"), &expander)
                .expect_err("must reject")
                .code,
            "invalid_messages"
        );
    }

    #[test]
    fn the_message_limit_is_exclusive_and_a_summary_lifts_it() {
        let many: Value = Value::Array(
            (0..=MESSAGE_HARD_LIMIT)
                .map(|index| json!({"role": "user", "content": format!("m{index}")}))
                .collect(),
        );
        // One more than the limit, with no summary, needs compression first — and the status
        // is 409, not the default 400.
        let error = validate_request_messages(&json!({}), many.as_array().unwrap(), &expander)
            .expect_err("must reject");
        assert_eq!(error.code, "context_compression_required");
        assert_eq!(error.status, 409);
        // A summary lifts it, and exactly the limit is fine either way.
        assert!(
            validate_request_messages(
                &json!({"contextSummary": "s"}),
                many.as_array().unwrap(),
                &expander
            )
            .is_ok()
        );
        let at_limit: Vec<Value> = (0..MESSAGE_HARD_LIMIT)
            .map(|index| json!({"role": "user", "content": format!("m{index}")}))
            .collect();
        assert!(validate_request_messages(&json!({}), &at_limit, &expander).is_ok());
        // A conversation with no user turn is rejected whatever else it holds.
        let error = validate_request_messages(
            &json!({}),
            &[json!({"role": "system", "content": "x"})],
            &expander,
        )
        .expect_err("must reject");
        assert_eq!(error.code, "invalid_payload");
    }

    #[test]
    fn an_image_attachment_becomes_a_part_and_a_short_one_does_not() {
        let long = format!("data:image/png;base64,{}", "A".repeat(40));
        let normalized = normalize_chat_messages(
            &json!([{"role": "user", "content": "look", "attachments": [{"imageData": long}]}]),
            &expander,
        )
        .expect("valid");
        assert_eq!(
            normalized[0]["content"],
            json!([
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": long}},
            ])
        );
        // Below the length guard the payload is not an image, and the turn stays plain text.
        let short = normalize_chat_messages(
            &json!([{"role": "user", "content": "look", "attachments": [{"imageData": "data:image/png;base64,AA"}]}]),
            &expander,
        )
        .expect("valid");
        assert_eq!(short[0]["content"], json!("look"));
    }

    #[test]
    fn an_assistant_turn_keeps_its_normalised_tool_calls() {
        let normalized = normalize_chat_messages(
            &json!([{"role": "assistant", "content": "calling", "tool_calls": [
                {"id": "c1", "function": {"name": "web_search", "arguments": "{\"q\": 1}"}},
            ]}]),
            &expander,
        )
        .expect("valid");
        assert_eq!(
            normalized[0],
            json!({
                "role": "assistant",
                "content": "calling",
                "tool_calls": [{
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{\"q\": 1}"},
                }],
            })
        );
    }

    #[test]
    fn an_object_arguments_is_re_serialized_in_sorted_order() {
        // The measured divergence: the oracle keeps the caller's insertion order here and this
        // port cannot, because `serde_json::Map` is a `BTreeMap` and `preserve_order` is off
        // workspace-wide on purpose. Unreachable from the wire format, which sends a string.
        let calls = normalize_tool_calls(
            Some(&json!([{"function": {"name": "s", "arguments": {"b": 1, "a": 2}}}])),
            false,
            false,
        );
        assert_eq!(
            calls[0]["function"]["arguments"],
            json!("{\"a\": 2, \"b\": 1}")
        );
        // The canonical path agrees with the oracle because it sorts on both sides.
        let canonical = canonical_tool_arguments(&json!({"b": 1, "a": 2}));
        assert_eq!(canonical, "{\"a\":2,\"b\":1}");
    }

    #[test]
    fn tool_call_ids_are_stabilised_from_the_name() {
        assert_eq!(stable_tool_call_id(0, "web_search"), "call_1_web_search");
        assert_eq!(
            stable_tool_call_id(1, "  Mixed-Case.Name  "),
            "call_2_mixed_case_name"
        );
        // Nothing alphanumeric survives, so the fallback name is used.
        assert_eq!(stable_tool_call_id(2, "  "), "call_3_tool");
        // 48 characters of the sanitised name survive, and a CJK name keeps its characters.
        let long = stable_tool_call_id(3, &format!("long_name_{}", "x".repeat(60)));
        assert_eq!(long.chars().count(), "call_4_".len() + 48);
        let cjk = stable_tool_call_id(4, &"中".repeat(60));
        assert_eq!(cjk.chars().count(), "call_5_".len() + 48);
    }

    #[test]
    fn a_canonical_argument_string_is_reparsed_and_a_bad_one_is_only_trimmed() {
        assert_eq!(
            canonical_tool_arguments(&json!("{\"b\":2,\"a\":1}")),
            "{\"a\":1,\"b\":2}"
        );
        assert_eq!(canonical_tool_arguments(&json!("not json")), "not json");
        assert_eq!(canonical_tool_arguments(&json!("  padded  ")), "padded");
        assert_eq!(canonical_tool_arguments(&json!(5)), "5");
    }

    #[test]
    fn validation_resolves_auto_and_rejects_missing_keys_and_models() {
        let router = ModelRouterSettings::default();
        let empty = validate_deepseek_payload(&json!({}), "", &router).expect_err("no key");
        assert_eq!(empty.code, "missing_api_key");

        // `apiKey` wins over the fallback, and both are trimmed.
        let validated = validate_deepseek_payload(
            &json!({"apiKey": "  k  ", "messages": [{"role": "user", "content": "x"}]}),
            "fallback",
            &router,
        )
        .expect("ok");
        assert_eq!(validated.api_key, "k");
        let validated = validate_deepseek_payload(
            &json!({"messages": [{"role": "user", "content": "x"}]}),
            "fallback",
            &router,
        )
        .expect("ok");
        assert_eq!(validated.api_key, "fallback");

        // Aliases normalise, an unsupported name is rejected, and auto resolves through the
        // router rather than staying the sentinel.
        let aliased = validate_deepseek_payload(
            &json!({"apiKey": "k", "model": "flash", "messages": [{"role": "user", "content": "x"}]}),
            "",
            &router,
        )
        .expect("ok");
        assert_eq!(aliased.model, "deepseek-v4-flash");
        let rejected = validate_deepseek_payload(
            &json!({"apiKey": "k", "model": "unknown", "messages": [{"role": "user", "content": "x"}]}),
            "",
            &router,
        )
        .expect_err("unsupported");
        assert_eq!(rejected.code, "invalid_payload");
        let auto = validate_deepseek_payload(
            &json!({"apiKey": "k", "model": "auto", "messages": [{"role": "user", "content": "帮我写代码"}]}),
            "",
            &router,
        )
        .expect("ok");
        assert_eq!(auto.model, "deepseek-v4-pro");

        // No messages at all is a payload error, not a message-shape error.
        let no_messages =
            validate_deepseek_payload(&json!({"apiKey": "k"}), "", &router).expect_err("empty");
        assert_eq!(no_messages.code, "invalid_payload");
    }
}
