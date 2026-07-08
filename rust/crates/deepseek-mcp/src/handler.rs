use serde_json::{Value, json};

use crate::{
    JsonRpcError, JsonRpcNotification, JsonRpcRequest, JsonRpcResponse, JsonRpcVersion, RequestId,
    registry::{call_tool, tool_descriptors},
};

pub fn handle_mcp_message(value: Value) -> Value {
    match serde_json::from_value::<JsonRpcRequest>(value.clone()) {
        Ok(req) => serde_json::to_value(handle_request(req)).unwrap(),
        Err(_) => match serde_json::from_value::<JsonRpcNotification>(value.clone()) {
            Ok(_notif) => Value::Null,
            Err(_) => error_response_value(
                RequestId::Null,
                -32700,
                "Parse error",
                Some(json!({"reason": "invalid JSON-RPC 2.0 message"})),
            ),
        },
    }
}

fn handle_request(req: JsonRpcRequest) -> JsonRpcResponse {
    match req.method.as_str() {
        "initialize" => JsonRpcResponse {
            jsonrpc: JsonRpcVersion::V2_0,
            result: Some(handle_initialize(req.params)),
            error: None,
            id: req.id,
        },
        "tools/list" => JsonRpcResponse {
            jsonrpc: JsonRpcVersion::V2_0,
            result: Some(json!({"tools": tool_descriptors()})),
            error: None,
            id: req.id,
        },
        "tools/call" => handle_tools_call(req.id, req.params),
        _ => error_response(req.id, -32601, "Method not found", None),
    }
}

fn handle_initialize(_params: Option<Value>) -> Value {
    json!({
        "protocolVersion": "2024-11-05",
        "serverInfo": {
            "name": "deepseek-mcp-rs",
            "version": "0.1.0"
        },
        "capabilities": {}
    })
}

fn handle_tools_call(id: RequestId, params: Option<Value>) -> JsonRpcResponse {
    let params = match params {
        Some(p) => p,
        None => return error_response(id, -32602, "Invalid params: missing arguments", None),
    };

    let name = params
        .get("name")
        .and_then(|v| v.as_str())
        .unwrap_or_default();
    let arguments = params.get("arguments").cloned().unwrap_or(Value::Null);

    match call_tool(name, &arguments) {
        Some(result) => JsonRpcResponse {
            jsonrpc: JsonRpcVersion::V2_0,
            result: Some(result),
            error: None,
            id,
        },
        None => error_response(id, -32602, &format!("Unknown tool: {name}"), None),
    }
}

fn error_response(id: RequestId, code: i64, message: &str, data: Option<Value>) -> JsonRpcResponse {
    JsonRpcResponse {
        jsonrpc: JsonRpcVersion::V2_0,
        result: None,
        error: Some(JsonRpcError {
            code,
            message: message.to_string(),
            data,
        }),
        id,
    }
}

fn error_response_value(id: RequestId, code: i64, message: &str, data: Option<Value>) -> Value {
    serde_json::to_value(error_response(id, code, message, data)).unwrap()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn fixture_path(name: &str) -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../../fixtures/mcp")
            .join(name)
    }

    fn load_fixture(name: &str) -> Value {
        let json = std::fs::read_to_string(fixture_path(name)).unwrap();
        serde_json::from_str(&json).unwrap()
    }

    #[test]
    fn initialize_returns_capabilities() {
        let response = handle_mcp_message(load_fixture("initialize-request.json"));
        let resp: JsonRpcResponse = serde_json::from_value(response).unwrap();
        assert!(resp.error.is_none());
        let result = resp.result.unwrap();
        assert_eq!(result["protocolVersion"], "2024-11-05");
        assert_eq!(result["serverInfo"]["name"], "deepseek-mcp-rs");
    }

    #[test]
    fn tools_list_returns_stub_tools() {
        let response = handle_mcp_message(load_fixture("tools-list-request.json"));
        let resp: JsonRpcResponse = serde_json::from_value(response).unwrap();
        assert!(resp.error.is_none());
        let result = resp.result.unwrap();
        let tools = result["tools"].as_array().unwrap();
        assert!(!tools.is_empty());
        let names: Vec<&str> = tools.iter().map(|t| t["name"].as_str().unwrap()).collect();
        assert!(names.contains(&"echo"));
        assert!(names.contains(&"health"));
    }

    #[test]
    fn tools_call_echo_returns_text_content() {
        let response = handle_mcp_message(load_fixture("tools-call-echo-request.json"));
        let resp: JsonRpcResponse = serde_json::from_value(response).unwrap();
        assert!(resp.error.is_none());
        let result = resp.result.unwrap();
        let content = result["content"].as_array().unwrap();
        assert_eq!(content[0]["type"], "text");
        assert_eq!(content[0]["text"], "hello from mcp");
    }

    #[test]
    fn tools_call_unknown_returns_jsonrpc_error() {
        let response = handle_mcp_message(load_fixture("tools-call-unknown-request.json"));
        let resp: JsonRpcResponse = serde_json::from_value(response).unwrap();
        assert!(resp.result.is_none());
        let err = resp.error.unwrap();
        assert_eq!(err.code, -32602);
        assert!(err.message.contains("Unknown tool"));
    }

    #[test]
    fn invalid_method_returns_jsonrpc_error() {
        let response = handle_mcp_message(load_fixture("invalid-method-request.json"));
        let resp: JsonRpcResponse = serde_json::from_value(response).unwrap();
        assert!(resp.result.is_none());
        let err = resp.error.unwrap();
        assert_eq!(err.code, -32601);
        assert_eq!(err.message, "Method not found");
    }

    #[test]
    fn notification_returns_no_response() {
        let json = json!({
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        });
        let response = handle_mcp_message(json);
        assert!(response.is_null());
    }

    #[test]
    fn invalid_jsonrpc_version_rejected() {
        let json = json!({
            "jsonrpc": "1.0",
            "method": "initialize",
            "id": "bad-version"
        });
        let response = handle_mcp_message(json);
        let resp: JsonRpcResponse = serde_json::from_value(response).unwrap();
        assert!(resp.result.is_none());
        let err = resp.error.unwrap();
        assert_eq!(err.code, -32700);
    }
}
