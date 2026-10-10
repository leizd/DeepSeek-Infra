use serde_json::{Map, Value, json};

pub const MAX_PROTOCOL_BYTES: usize = 2_000_000;
pub const MAX_PROTOCOL_DEPTH: usize = 32;

const SUPPORTED_PROTOCOL_VERSIONS: [&str; 2] = ["2024-11-05", "2025-06-18"];
const SUPPORTED_REQUEST_METHODS: [&str; 8] = [
    "initialize",
    "ping",
    "tools/list",
    "tools/call",
    "resources/list",
    "resources/read",
    "prompts/list",
    "prompts/get",
];
const SUPPORTED_NOTIFICATION_METHODS: [&str; 1] = ["notifications/initialized"];

fn json_rpc_code(code: &str) -> i64 {
    match code {
        "parse_error" => -32700,
        "method_not_supported" => -32601,
        "invalid_params"
        | "invalid_initialize_request"
        | "invalid_capabilities"
        | "invalid_protocol_version" => -32602,
        _ => -32600,
    }
}

fn error(
    code: &'static str,
    message: &'static str,
    notification: bool,
    message_type: Option<&str>,
) -> Value {
    let mut response = json!({
        "ok": false,
        "code": code,
        "jsonRpcCode": json_rpc_code(code),
        "message": message,
        "notification": notification,
    });
    if let (Some(kind), Some(object)) = (message_type, response.as_object_mut()) {
        object.insert("messageType".to_string(), Value::String(kind.to_string()));
    }
    response
}

fn notification_hint(object: &Map<String, Value>) -> bool {
    if !object.contains_key("id") {
        return true;
    }
    object
        .get("method")
        .and_then(Value::as_str)
        .is_some_and(|method| method.starts_with("notifications/"))
}

fn valid_request_id(value: &Value) -> bool {
    value.is_null() || value.is_string() || value.as_i64().is_some()
}

fn max_depth(value: &Value) -> usize {
    let mut maximum = 1;
    let mut stack = vec![(value, 1_usize)];
    while let Some((current, depth)) = stack.pop() {
        maximum = maximum.max(depth);
        if depth > MAX_PROTOCOL_DEPTH {
            return depth;
        }
        match current {
            Value::Array(values) => {
                for child in values {
                    if child.is_array() || child.is_object() {
                        stack.push((child, depth + 1));
                    }
                }
            }
            Value::Object(values) => {
                for child in values.values() {
                    if child.is_array() || child.is_object() {
                        stack.push((child, depth + 1));
                    }
                }
            }
            _ => {}
        }
    }
    maximum
}

fn category(method: &str) -> &str {
    if method == "initialize" || method.starts_with("notifications/") {
        "lifecycle"
    } else if method == "ping" {
        "control"
    } else {
        method.split('/').next().unwrap_or(method)
    }
}

fn success(message_type: &str, request: Value, route_category: &str) -> Value {
    json!({
        "ok": true,
        "messageType": message_type,
        "request": request,
        "routing": {"owner": "python", "category": route_category},
    })
}

fn prepare_response(object: &Map<String, Value>, normalized: Value) -> Value {
    if object
        .get("id")
        .is_none_or(|identifier| !valid_request_id(identifier))
    {
        return error(
            "invalid_request_id",
            "response id is invalid",
            false,
            Some("response"),
        );
    }
    let has_result = object.contains_key("result");
    let has_error = object.contains_key("error");
    if has_result == has_error {
        return error(
            "invalid_request",
            "response must contain exactly one of result or error",
            false,
            Some("response"),
        );
    }
    if has_error && object.get("error").is_none_or(|value| !value.is_object()) {
        return error(
            "invalid_request",
            "response error must be an object",
            false,
            Some("response"),
        );
    }
    success("response", normalized, "response")
}

fn validate_initialize(
    params: &Map<String, Value>,
    notification: bool,
    message_type: &str,
) -> Option<Value> {
    if let Some(version) = params.get("protocolVersion") {
        let valid = version
            .as_str()
            .is_some_and(|candidate| SUPPORTED_PROTOCOL_VERSIONS.contains(&candidate));
        if !valid {
            return Some(error(
                "invalid_protocol_version",
                "initialize protocolVersion is not supported",
                notification,
                Some(message_type),
            ));
        }
    }
    if params
        .get("capabilities")
        .is_some_and(|capabilities| !capabilities.is_object())
    {
        return Some(error(
            "invalid_capabilities",
            "initialize capabilities must be an object",
            notification,
            Some(message_type),
        ));
    }
    if let Some(client_info) = params.get("clientInfo") {
        let Some(client_info) = client_info.as_object() else {
            return Some(error(
                "invalid_initialize_request",
                "initialize clientInfo must be an object",
                notification,
                Some(message_type),
            ));
        };
        let valid_name = client_info
            .get("name")
            .and_then(Value::as_str)
            .is_some_and(|name| !name.trim().is_empty());
        if !valid_name {
            return Some(error(
                "invalid_initialize_request",
                "initialize clientInfo.name is required",
                notification,
                Some(message_type),
            ));
        }
        if client_info
            .get("version")
            .is_some_and(|version| !version.is_string() && !version.is_null())
        {
            return Some(error(
                "invalid_initialize_request",
                "initialize clientInfo.version must be a string",
                notification,
                Some(message_type),
            ));
        }
    }
    None
}

fn invalid_params(message: &'static str, notification: bool, message_type: &str) -> Value {
    error("invalid_params", message, notification, Some(message_type))
}

fn validate_method_params(
    method: &str,
    params: &Map<String, Value>,
    normalized: &mut Map<String, Value>,
    notification: bool,
    message_type: &str,
) -> Option<Value> {
    if method == "initialize" {
        return validate_initialize(params, notification, message_type);
    }
    if method == "tools/call" {
        let Some(name) = params.get("name").and_then(Value::as_str) else {
            return Some(invalid_params(
                "tools/call name must be a non-empty string",
                notification,
                message_type,
            ));
        };
        if name.trim().is_empty() {
            return Some(invalid_params(
                "tools/call name must be a non-empty string",
                notification,
                message_type,
            ));
        }
        if let Some(normalized_params) = normalized.get_mut("params").and_then(Value::as_object_mut)
        {
            normalized_params.insert("name".to_string(), Value::String(name.trim().to_string()));
        }
        if params
            .get("arguments")
            .is_some_and(|arguments| !arguments.is_null() && !arguments.is_object())
        {
            return Some(invalid_params(
                "tools/call arguments must be an object",
                notification,
                message_type,
            ));
        }
    } else if method == "resources/read" {
        let Some(uri) = params.get("uri").and_then(Value::as_str) else {
            return Some(invalid_params(
                "resources/read uri must be a non-empty string",
                notification,
                message_type,
            ));
        };
        if uri.trim().is_empty() {
            return Some(invalid_params(
                "resources/read uri must be a non-empty string",
                notification,
                message_type,
            ));
        }
        if let Some(normalized_params) = normalized.get_mut("params").and_then(Value::as_object_mut)
        {
            normalized_params.insert("uri".to_string(), Value::String(uri.trim().to_string()));
        }
    } else if method == "prompts/get" {
        let Some(name) = params.get("name").and_then(Value::as_str) else {
            return Some(invalid_params(
                "prompts/get name must be a non-empty string",
                notification,
                message_type,
            ));
        };
        if name.trim().is_empty() {
            return Some(invalid_params(
                "prompts/get name must be a non-empty string",
                notification,
                message_type,
            ));
        }
        if let Some(normalized_params) = normalized.get_mut("params").and_then(Value::as_object_mut)
        {
            normalized_params.insert("name".to_string(), Value::String(name.trim().to_string()));
        }
        if params
            .get("arguments")
            .is_some_and(|arguments| !arguments.is_null() && !arguments.is_object())
        {
            return Some(invalid_params(
                "prompts/get arguments must be an object",
                notification,
                message_type,
            ));
        }
    }
    None
}

pub fn prepare_protocol_value(value: &Value, payload_size: usize) -> Value {
    if payload_size > MAX_PROTOCOL_BYTES {
        return error(
            "request_too_large",
            "MCP request exceeds the preparation budget",
            false,
            None,
        );
    }
    if max_depth(value) > MAX_PROTOCOL_DEPTH {
        return error(
            "nesting_limit_exceeded",
            "MCP request nesting is too deep",
            false,
            None,
        );
    }
    let Some(object) = value.as_object() else {
        return error(
            "invalid_request",
            "MCP message must be a JSON object",
            false,
            None,
        );
    };

    let notification = notification_hint(object);
    let hinted_type = if notification {
        "notification"
    } else {
        "request"
    };
    if object.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return error(
            "invalid_jsonrpc_version",
            "jsonrpc must be '2.0'",
            notification,
            Some(hinted_type),
        );
    }

    let mut normalized = object.clone();
    let raw_method = object.get("method");
    if raw_method.is_none_or(Value::is_null)
        && (object.contains_key("result") || object.contains_key("error"))
    {
        return prepare_response(object, Value::Object(normalized));
    }
    let Some(method) = raw_method.and_then(Value::as_str) else {
        return error(
            "invalid_method",
            "method must be a non-empty normalized string",
            notification,
            Some(hinted_type),
        );
    };
    if method.is_empty() || method != method.trim() {
        return error(
            "invalid_method",
            "method must be a non-empty normalized string",
            notification,
            Some(hinted_type),
        );
    }
    if object
        .get("id")
        .is_some_and(|identifier| !valid_request_id(identifier))
    {
        return error(
            "invalid_request_id",
            "request id must be a string, signed 64-bit integer, or null",
            notification,
            Some(hinted_type),
        );
    }

    let supported = if method.starts_with("notifications/") {
        SUPPORTED_NOTIFICATION_METHODS.contains(&method)
    } else {
        SUPPORTED_REQUEST_METHODS.contains(&method)
    };
    if !supported {
        return error(
            "method_not_supported",
            "method is not supported by the Python MCP server",
            notification,
            Some(hinted_type),
        );
    }

    let params = match object.get("params") {
        None => Map::new(),
        Some(Value::Object(params)) => params.clone(),
        Some(_) => {
            normalized.insert("params".to_string(), Value::Object(Map::new()));
            Map::new()
        }
    };
    if let Some(validation_error) =
        validate_method_params(method, &params, &mut normalized, notification, hinted_type)
    {
        return validation_error;
    }
    success(hinted_type, Value::Object(normalized), category(method))
}

pub fn prepare_protocol_bytes(raw: &[u8]) -> Value {
    if raw.len() > MAX_PROTOCOL_BYTES {
        return error(
            "request_too_large",
            "MCP request exceeds the preparation budget",
            false,
            None,
        );
    }
    let value: Value = match serde_json::from_slice(raw) {
        Ok(value) => value,
        Err(_) => {
            return error(
                "parse_error",
                "MCP request must contain valid JSON",
                false,
                None,
            );
        }
    };
    prepare_protocol_value(&value, raw.len())
}

pub fn diagnostic_summary(preparation: &Value, payload_size: usize) -> Value {
    let request = preparation.get("request");
    let method = request
        .and_then(|value| value.get("method"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let id_type = match request.and_then(|value| value.get("id")) {
        None => "absent",
        Some(Value::Null) => "null",
        Some(Value::String(_)) => "string",
        Some(Value::Number(number)) if number.as_i64().is_some() => "integer",
        Some(_) => "invalid",
    };
    json!({
        "method": method,
        "messageType": preparation.get("messageType").and_then(Value::as_str).unwrap_or("invalid"),
        "requestIdType": id_type,
        "payloadSize": payload_size,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn prepare(value: Value) -> Value {
        let raw = serde_json::to_vec(&value).unwrap();
        prepare_protocol_bytes(&raw)
    }

    #[test]
    fn parses_valid_request() {
        let result = prepare(json!({"jsonrpc":"2.0","id":7,"method":"ping"}));
        assert_eq!(result["ok"], true);
        assert_eq!(result["messageType"], "request");
    }

    #[test]
    fn parses_valid_notification() {
        let result = prepare(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
        assert_eq!(result["messageType"], "notification");
    }

    #[test]
    fn rejects_invalid_jsonrpc_version() {
        let result = prepare(json!({"jsonrpc":"1.0","id":1,"method":"ping"}));
        assert_eq!(result["code"], "invalid_jsonrpc_version");
    }

    #[test]
    fn validates_request_id() {
        let result = prepare(json!({"jsonrpc":"2.0","id":true,"method":"ping"}));
        assert_eq!(result["code"], "invalid_request_id");
    }

    #[test]
    fn validates_method() {
        let result = prepare(json!({"jsonrpc":"2.0","id":1,"method":"unknown"}));
        assert_eq!(result["code"], "method_not_supported");
    }

    #[test]
    fn normalizes_initialize() {
        let result = prepare(json!({
            "jsonrpc":"2.0","id":1,"method":"initialize",
            "params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"client","version":"1"}}
        }));
        assert_eq!(result["routing"]["owner"], "python");
        assert_eq!(result["routing"]["category"], "lifecycle");
    }

    #[test]
    fn validates_capabilities() {
        let result = prepare(
            json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":[]}}),
        );
        assert_eq!(result["code"], "invalid_capabilities");
    }

    #[test]
    fn prepares_tools_list() {
        let result = prepare(json!({"jsonrpc":"2.0","id":"list","method":"tools/list"}));
        assert_eq!(result["routing"]["category"], "tools");
        assert!(result.get("result").is_none());
    }

    #[test]
    fn prepares_tools_call() {
        let result = prepare(
            json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"echo","arguments":{}}}),
        );
        assert_eq!(result["ok"], true);
        assert_eq!(result["routing"]["owner"], "python");
    }

    #[test]
    fn preserves_tool_arguments() {
        let arguments = json!({"query":"rust mcp","nested":{"中文":"🚀"}});
        let result = prepare(
            json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search","arguments":arguments}}),
        );
        assert_eq!(result["request"]["params"]["arguments"], arguments);
    }

    #[test]
    fn rejects_invalid_tool_name() {
        let result =
            prepare(json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":" "}}));
        assert_eq!(result["code"], "invalid_params");
    }

    #[test]
    fn rejects_invalid_params() {
        let result = prepare(
            json!({"jsonrpc":"2.0","id":1,"method":"prompts/get","params":{"name":"x","arguments":[]}}),
        );
        assert_eq!(result["code"], "invalid_params");
    }

    #[test]
    fn rejects_oversized_request() {
        let raw = vec![b'x'; MAX_PROTOCOL_BYTES + 1];
        assert_eq!(prepare_protocol_bytes(&raw)["code"], "request_too_large");
    }

    #[test]
    fn rejects_excessive_nesting() {
        let mut nested = json!("leaf");
        for _ in 0..MAX_PROTOCOL_DEPTH + 2 {
            nested = json!({"next": nested});
        }
        let value = json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x","arguments":nested}});
        assert_eq!(prepare(value)["code"], "nesting_limit_exceeded");
    }

    #[test]
    fn preserves_cjk_and_emoji() {
        let result = prepare(
            json!({"jsonrpc":"2.0","id":"中文🚀","method":"ping","params":{"note":"协议准备✅"}}),
        );
        assert_eq!(result["request"]["params"]["note"], "协议准备✅");
    }

    #[test]
    fn response_roundtrips_json() {
        let result = prepare(json!({"jsonrpc":"2.0","id":1,"result":{"ok":true}}));
        let encoded = serde_json::to_string(&result).unwrap();
        let decoded: Value = serde_json::from_str(&encoded).unwrap();
        assert_eq!(decoded, result);
    }

    #[test]
    fn never_executes_tools() {
        let result = prepare(
            json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"echo","arguments":{"message":"do not execute"}}}),
        );
        assert_eq!(result["routing"]["owner"], "python");
        assert!(result.get("result").is_none());
    }

    #[test]
    fn never_logs_tool_arguments() {
        let result = prepare(
            json!({"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"echo","arguments":{"secret":"do-not-log"}}}),
        );
        let diagnostics = diagnostic_summary(&result, 123);
        assert!(!diagnostics.to_string().contains("do-not-log"));
        assert_eq!(diagnostics["method"], "tools/call");
    }
}
