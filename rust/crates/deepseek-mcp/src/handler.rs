use serde_json::Value;

use crate::protocol_preparation::prepare_protocol_value;

/// Backward-compatible entry point for the sidecar's historical `/mcp` route.
///
/// Since 3.6.0 this performs protocol preparation only. It never dispatches a
/// method, consults a tool registry, or executes tools/resources/prompts.
pub fn handle_mcp_message(value: Value) -> Value {
    let payload_size = serde_json::to_vec(&value).map_or(0, |encoded| encoded.len());
    prepare_protocol_value(&value, payload_size)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn historical_handler_is_preparation_only() {
        let response = handle_mcp_message(json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "echo", "arguments": {"message": "hello"}}
        }));
        assert_eq!(response["ok"], true);
        assert_eq!(response["routing"]["owner"], "python");
        assert!(response.get("result").is_none());
    }

    #[test]
    fn notification_is_described_without_execution() {
        let response = handle_mcp_message(json!({
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }));
        assert_eq!(response["messageType"], "notification");
        assert_eq!(response["routing"]["category"], "lifecycle");
    }
}
