//! The OpenAI facade's payload translation — `openai_to_internal_payload`.
//!
//! Mirrors `deepseek_infra/infra/gateway/openai_api.py:29`. `POST /v1/chat/completions`
//! is a *facade*: it accepts an OpenAI body, translates it into the internal chat
//! payload, and hands that to the provider — `resolve_provider(model).chat(payload)` →
//! `call_deepseek` → `prepare_deepseek_call` → `build_deepseek_request`, which is what
//! actually builds the upstream body. The translation is therefore part of the public
//! contract, and it is deliberately narrow.
//!
//! # What the translation forwards, and what it drops
//!
//! It forwards exactly: `model` (normalized through `MODEL_ALIASES`), `messages`
//! (verbatim and unvalidated — `build_deepseek_request` is what validates them),
//! `stream` (Python truthiness, so the string `"false"` is **true**), `thinkingEnabled:
//! false`, `localBaseUrl`, and `temperature` when it is a real number.
//!
//! Everything else an OpenAI client may send — `tools`, `tool_choice`, `max_tokens`,
//! `top_p`, `reasoning_effort`, `thinking` — is **dropped**, and that is the oracle's
//! behaviour rather than an omission: the facade's tools come from the internal catalog
//! (`tools_for_payload`) and reasoning is off by construction, so the standard endpoint
//! returns deterministic content only.
//!
//! # Why this module exists
//!
//! The native route built its upstream body straight from the OpenAI request
//! (`request_preparation::prepare_chat_request`), which **forwards** `tools`,
//! `tool_choice`, `max_tokens`, `top_p` and `reasoning_effort` — all of which the
//! oracle drops — and never sets `thinkingEnabled` or `localBaseUrl`. That is a visible
//! divergence on a public route, so this is the front half of the fix:
//! `examples/openai_facade_parity_probe.rs` pins every accept and refuse case against
//! the real Python function before the route is moved onto it.

use serde_json::{Map, Value};

use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::core_utils::{normalize_model_name, python_truthy};
use deepseek_policy::model_router::ModelRouterSettings;
use deepseek_policy::python_json::OrderedJson;

/// `settings.default_model` — the fallback when the body carries no truthy `model`.
pub const DEFAULT_MODEL: &str = "deepseek-v4-pro";

/// Mirrors `openai_to_internal_payload`.
///
/// `local_base_url` is `request_base_url(request)` at the route layer; it travels in the
/// payload because the internal path uses it for the local-provider arm.
///
/// The two refusals are `AppError(..., INVALID_PAYLOAD)`, 400 — the same shape the route
/// layer has always returned for a malformed body, so a client that only reads the
/// status code sees no change.
pub fn openai_to_internal_payload(
    body: &Value,
    local_base_url: &str,
    router: &ModelRouterSettings,
) -> Result<Value, AppError> {
    let Some(object) = body.as_object() else {
        return Err(invalid_payload("Request body must be a JSON object"));
    };
    let messages = match object.get("messages") {
        Some(Value::Array(turns)) if !turns.is_empty() => Value::Array(turns.clone()),
        _ => return Err(invalid_payload("messages must be a non-empty array")),
    };

    // `body.get("model") or settings.default_model`, then `normalize_model_name`. The
    // `or` is Python's: a falsy model (`""`, `null`, `0`, `false`, `[]`, `{}`) falls back
    // to the default, so `normalize_model_name` is never handed a falsy value.
    let model = match object.get("model") {
        Some(value) if python_truthy(value) => {
            normalize_model_name(Some(value), &router.model_aliases)
        }
        _ => normalize_model_name(
            Some(&Value::String(router.default_model.clone())),
            &router.model_aliases,
        ),
    };

    let mut payload = Map::new();
    payload.insert("model".to_string(), Value::String(model));
    payload.insert("messages".to_string(), messages);
    payload.insert(
        "stream".to_string(),
        Value::Bool(python_truthy(object.get("stream").unwrap_or(&Value::Null))),
    );
    // "Standard OpenAI endpoint: deterministic content only, no reasoning tokens."
    payload.insert("thinkingEnabled".to_string(), Value::Bool(false));
    payload.insert(
        "localBaseUrl".to_string(),
        Value::String(local_base_url.to_string()),
    );
    // `isinstance(temperature, (int, float)) and not isinstance(temperature, bool)`:
    // a bool is an `int` in Python, so `true` is excluded explicitly — and it is *not*
    // an error, it is simply not forwarded.
    if let Some(Value::Number(number)) = object.get("temperature") {
        if let Some(value) = number.as_f64() {
            if let Some(converted) = serde_json::Number::from_f64(value) {
                payload.insert("temperature".to_string(), Value::Number(converted));
            }
        }
    }
    Ok(Value::Object(payload))
}

fn invalid_payload(message: &str) -> AppError {
    AppError {
        message: message.to_string(),
        code: codes::INVALID_PAYLOAD,
        status: 400,
    }
}

/// The payload as the internal path renders it, for probes and diagnostics:
/// `json.dumps(value, ensure_ascii=False, sort_keys=True)` — Python's **default**
/// separators, so the rendering is comparable with the oracle's rather than with
/// `serde_json`'s compact default.
///
/// Keys are sorted because this crate builds plain `serde_json::Map`s (ordered by key)
/// while Python's dict keeps insertion order; the field *set* and every value are what
/// the contract is, so the comparison must not depend on the map implementation.
pub fn payload_canonical_json(payload: &Value) -> String {
    OrderedJson::from_value_with_order(&sort_value(payload), &[]).render_default_separators()
}

fn sort_value(value: &Value) -> Value {
    match value {
        Value::Object(fields) => {
            let mut sorted: Vec<(&String, Value)> = fields
                .iter()
                .map(|(key, item)| (key, sort_value(item)))
                .collect();
            sorted.sort_by(|left, right| left.0.cmp(right.0));
            let mut object = Map::new();
            for (key, item) in sorted {
                object.insert(key.clone(), item);
            }
            Value::Object(object)
        }
        Value::Array(items) => Value::Array(items.iter().map(sort_value).collect()),
        other => other.clone(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn router() -> ModelRouterSettings {
        ModelRouterSettings::default()
    }

    fn body(value: Value) -> Result<Value, AppError> {
        openai_to_internal_payload(&value, "http://127.0.0.1:8000", &router())
    }

    #[test]
    fn a_minimal_body_translates_to_the_six_forwarded_fields() {
        let payload = body(json!({
            "model": "deepseek-v4-pro",
            "messages": [{"role": "user", "content": " hi "}],
        }))
        .expect("a minimal body is accepted");
        assert_eq!(payload["model"], json!("deepseek-v4-pro"));
        assert_eq!(
            payload["messages"],
            json!([{"role": "user", "content": " hi "}])
        );
        assert_eq!(payload["stream"], json!(false));
        assert_eq!(payload["thinkingEnabled"], json!(false));
        assert_eq!(payload["localBaseUrl"], json!("http://127.0.0.1:8000"));
        assert!(payload.get("temperature").is_none());
        assert_eq!(payload.as_object().map(Map::len), Some(5));
    }

    #[test]
    fn everything_the_facade_does_not_forward_is_dropped() {
        let payload = body(json!({
            "model": "fast",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "t"}}],
            "tool_choice": "auto",
            "max_tokens": 16,
            "top_p": 0.5,
            "reasoning_effort": "high",
            "thinking": {"type": "enabled"},
        }))
        .expect("the body is accepted");
        for dropped in [
            "tools",
            "tool_choice",
            "max_tokens",
            "top_p",
            "reasoning_effort",
            "thinking",
        ] {
            assert!(payload.get(dropped).is_none(), "{dropped} was forwarded");
        }
        // The alias still resolves, because `model` *is* forwarded.
        assert_eq!(payload["model"], json!("deepseek-v4-flash"));
    }

    #[test]
    fn a_falsy_model_falls_back_to_the_default_before_normalization() {
        for falsy in [
            Value::Null,
            json!(""),
            json!(0),
            json!(false),
            json!([]),
            json!({}),
        ] {
            let payload = body(json!({
                "model": falsy,
                "messages": [{"role": "user", "content": "hi"}],
            }))
            .expect("a falsy model is not an error");
            assert_eq!(payload["model"], json!(DEFAULT_MODEL), "for {falsy:?}");
        }
        // A truthy non-string is stringified the way `str()` does, then looked up.
        let payload = body(json!({"model": true, "messages": [{"role": "user", "content": "hi"}]}))
            .expect("a truthy bool is accepted");
        assert_eq!(payload["model"], json!("True"));
    }

    #[test]
    fn stream_uses_python_truthiness_so_the_string_false_is_true() {
        for (value, expected) in [
            (json!(true), true),
            (json!(false), false),
            (json!("false"), true),
            (json!(""), false),
            (json!(0), false),
            (json!(1), true),
            (json!([]), false),
            (Value::Null, false),
        ] {
            let payload = body(json!({
                "messages": [{"role": "user", "content": "hi"}],
                "stream": value,
            }))
            .expect("the body is accepted");
            assert_eq!(payload["stream"], json!(expected), "for {value:?}");
        }
        // Absent is `None`, which is falsy.
        let payload =
            body(json!({"messages": [{"role": "user", "content": "hi"}]})).expect("accepted");
        assert_eq!(payload["stream"], json!(false));
    }

    #[test]
    fn temperature_is_forwarded_only_for_real_numbers() {
        assert_eq!(
            body(json!({"messages": [{"role": "user", "content": "hi"}], "temperature": 0.5}))
                .unwrap()["temperature"],
            json!(0.5)
        );
        // `bool` is an `int` in Python, hence the explicit exclusion; a string, an
        // array and null are all simply not forwarded rather than refused.
        for ignored in [
            json!(true),
            json!(false),
            json!("0.5"),
            json!([0.5]),
            Value::Null,
        ] {
            let payload = body(json!({
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": ignored,
            }))
            .unwrap();
            assert!(
                payload.get("temperature").is_none(),
                "temperature was forwarded for {ignored:?}"
            );
        }
        // `0` is a real number and is forwarded.
        assert_eq!(
            body(json!({"messages": [{"role": "user", "content": "hi"}], "temperature": 0}))
                .unwrap()["temperature"],
            json!(0.0)
        );
    }

    #[test]
    fn the_two_refusals_carry_invalid_payload_at_400() {
        for (value, message) in [
            (json!([]), "Request body must be a JSON object"),
            (json!("x"), "Request body must be a JSON object"),
            (json!(1), "Request body must be a JSON object"),
            (Value::Null, "Request body must be a JSON object"),
            (json!({}), "messages must be a non-empty array"),
            (
                json!({"messages": []}),
                "messages must be a non-empty array",
            ),
            (
                json!({"messages": "hi"}),
                "messages must be a non-empty array",
            ),
            (
                json!({"messages": {"role": "user"}}),
                "messages must be a non-empty array",
            ),
        ] {
            let error =
                openai_to_internal_payload(&value, "", &router()).expect_err("the body is refused");
            assert_eq!(error.message, message, "for {value:?}");
            assert_eq!(error.code, codes::INVALID_PAYLOAD);
            assert_eq!(error.status, 400);
        }
    }

    #[test]
    fn messages_are_forwarded_verbatim_rather_than_normalized() {
        // The facade does not validate or normalize turns; `build_deepseek_request`
        // does. A blank-content turn, a `tool` turn and a non-object entry all survive
        // this layer, which is what makes the validation single-sourced there.
        let messages = json!([
            {"role": "user", "content": "  "},
            {"role": "tool", "content": "x"},
            "not-an-object",
            {"role": "assistant", "content": null},
        ]);
        let payload = body(json!({"messages": messages})).expect("accepted");
        assert_eq!(payload["messages"], messages);
    }
}
