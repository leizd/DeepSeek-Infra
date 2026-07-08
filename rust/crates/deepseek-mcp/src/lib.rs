use deepseek_core::RequestId;
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub mod handler;
pub mod registry;

pub fn mcp_version() -> &'static str {
    deepseek_core::version_info().version
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum JsonRpcVersion {
    V2_0,
}

impl Serialize for JsonRpcVersion {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.serialize_str("2.0")
    }
}

impl<'de> Deserialize<'de> for JsonRpcVersion {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let value = String::deserialize(deserializer)?;
        if value == "2.0" {
            Ok(JsonRpcVersion::V2_0)
        } else {
            Err(serde::de::Error::custom(format!(
                "invalid JSON-RPC version: {value}, expected 2.0"
            )))
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JsonRpcRequest {
    pub jsonrpc: JsonRpcVersion,
    pub method: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
    pub id: RequestId,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JsonRpcNotification {
    pub jsonrpc: JsonRpcVersion,
    pub method: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub params: Option<Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JsonRpcResponse {
    pub jsonrpc: JsonRpcVersion,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<JsonRpcError>,
    pub id: RequestId,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct JsonRpcError {
    pub code: i64,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub data: Option<Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolDescriptor {
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(rename = "inputSchema", skip_serializing_if = "Option::is_none")]
    pub input_schema: Option<Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolCallParams {
    pub name: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub arguments: Option<Value>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ToolCallResult {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub content: Option<Vec<ToolContent>>,
    #[serde(rename = "isError", skip_serializing_if = "Option::is_none")]
    pub is_error: Option<bool>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum ToolContent {
    #[serde(rename = "text")]
    Text { text: String },
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

    fn load_fixture(name: &str) -> String {
        std::fs::read_to_string(fixture_path(name)).unwrap()
    }

    #[test]
    fn mcp_version_matches_core() {
        assert_eq!(mcp_version(), deepseek_core::version_info().version);
    }

    #[test]
    fn tools_list_request_parses() {
        let json = load_fixture("tools-list-request.json");
        let req: JsonRpcRequest = serde_json::from_str(&json).unwrap();
        assert_eq!(req.method, "tools/list");
        assert_eq!(req.id, RequestId::String("list-1".to_string()));
    }

    #[test]
    fn tools_call_request_parses() {
        let json = load_fixture("tools-call-request.json");
        let req: JsonRpcRequest = serde_json::from_str(&json).unwrap();
        assert_eq!(req.method, "tools/call");
        assert_eq!(req.id, RequestId::Number(42));
        let params = req.params.unwrap();
        assert_eq!(params["name"], "calculator");
    }

    #[test]
    fn error_response_parses() {
        let json = load_fixture("error-response.json");
        let resp: JsonRpcResponse = serde_json::from_str(&json).unwrap();
        assert!(resp.result.is_none());
        let err = resp.error.unwrap();
        assert_eq!(err.code, -32601);
        assert_eq!(err.message, "Method not found");
        assert_eq!(resp.id, RequestId::String("resp-1".to_string()));
    }

    #[test]
    fn notification_parses_without_id() {
        let json = load_fixture("notification.json");
        let notif: JsonRpcNotification = serde_json::from_str(&json).unwrap();
        assert_eq!(notif.method, "notifications/initialized");
    }

    fn assert_roundtrip_preserved<T>(name: &str, json: &str)
    where
        T: serde::Serialize + serde::de::DeserializeOwned + std::fmt::Debug + PartialEq,
    {
        let original: Value = serde_json::from_str(json).unwrap();
        let typed: T = serde_json::from_str(json).unwrap();
        let serialized = serde_json::to_string(&typed).unwrap();
        let roundtripped: Value = serde_json::from_str(&serialized).unwrap();
        assert_eq!(original, roundtripped, "fixture {name} lost fields");
    }

    #[test]
    fn fixtures_roundtrip_without_field_loss() {
        assert_roundtrip_preserved::<JsonRpcRequest>(
            "tools-list-request.json",
            &load_fixture("tools-list-request.json"),
        );
        assert_roundtrip_preserved::<JsonRpcRequest>(
            "tools-call-request.json",
            &load_fixture("tools-call-request.json"),
        );
        assert_roundtrip_preserved::<JsonRpcResponse>(
            "error-response.json",
            &load_fixture("error-response.json"),
        );
        assert_roundtrip_preserved::<JsonRpcNotification>(
            "notification.json",
            &load_fixture("notification.json"),
        );
    }

    #[test]
    fn request_roundtrip_preserves_fields() {
        let json = load_fixture("tools-call-request.json");
        let original: Value = serde_json::from_str(&json).unwrap();
        let req: JsonRpcRequest = serde_json::from_str(&json).unwrap();
        let serialized = serde_json::to_string(&req).unwrap();
        let roundtripped: Value = serde_json::from_str(&serialized).unwrap();
        assert_eq!(original, roundtripped);
    }

    #[test]
    fn response_roundtrip_preserves_fields() {
        let json = load_fixture("error-response.json");
        let original: Value = serde_json::from_str(&json).unwrap();
        let resp: JsonRpcResponse = serde_json::from_str(&json).unwrap();
        let serialized = serde_json::to_string(&resp).unwrap();
        let roundtripped: Value = serde_json::from_str(&serialized).unwrap();
        assert_eq!(original, roundtripped);
    }

    #[test]
    fn invalid_jsonrpc_version_errors() {
        let json = r#"{"jsonrpc":"1.0","method":"tools/list","id":"x"}"#;
        let result: Result<JsonRpcRequest, _> = serde_json::from_str(json);
        assert!(result.is_err());
    }

    #[test]
    fn missing_method_errors() {
        let json = r#"{"jsonrpc":"2.0","id":"x"}"#;
        let result: Result<JsonRpcRequest, _> = serde_json::from_str(json);
        assert!(result.is_err());
    }

    #[test]
    fn request_id_can_be_string_number_or_null() {
        let cases = [
            ("\"abc\"", RequestId::String("abc".to_string())),
            ("42", RequestId::Number(42)),
            ("null", RequestId::Null),
        ];
        for (raw, expected) in cases {
            let json = format!(r#"{{"jsonrpc":"2.0","method":"tools/list","id":{raw}}}"#);
            let req: JsonRpcRequest = serde_json::from_str(&json).unwrap();
            assert_eq!(req.id, expected);
        }
    }

    #[test]
    fn notification_has_no_id_field() {
        let json =
            r#"{"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":50}}"#;
        let notif: JsonRpcNotification = serde_json::from_str(json).unwrap();
        assert_eq!(notif.method, "notifications/progress");
        assert!(notif.params.is_some());
    }

    #[test]
    fn tool_descriptor_roundtrips() {
        let tool = ToolDescriptor {
            name: "calculator".to_string(),
            description: Some("A simple calculator".to_string()),
            input_schema: Some(serde_json::json!({
                "type": "object",
                "properties": {
                    "a": {"type": "number"},
                    "b": {"type": "number"}
                }
            })),
        };
        let json = serde_json::to_string(&tool).unwrap();
        let parsed: ToolDescriptor = serde_json::from_str(&json).unwrap();
        assert_eq!(tool, parsed);
    }

    #[test]
    fn tool_call_params_roundtrips() {
        let params = ToolCallParams {
            name: "calculator".to_string(),
            arguments: Some(serde_json::json!({"a": 1, "b": 2})),
        };
        let json = serde_json::to_string(&params).unwrap();
        let parsed: ToolCallParams = serde_json::from_str(&json).unwrap();
        assert_eq!(params, parsed);
    }

    #[test]
    fn tool_call_result_roundtrips() {
        let result = ToolCallResult {
            content: Some(vec![ToolContent::Text {
                text: "The answer is 3".to_string(),
            }]),
            is_error: Some(false),
        };
        let json = serde_json::to_string(&result).unwrap();
        let parsed: ToolCallResult = serde_json::from_str(&json).unwrap();
        assert_eq!(result, parsed);
    }
}
