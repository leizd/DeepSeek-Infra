use serde_json::{Value, json};

pub fn tool_descriptors() -> Vec<Value> {
    vec![
        json!({
            "name": "echo",
            "description": "Echoes the input back as text",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string"}
                },
                "required": ["message"]
            }
        }),
        json!({
            "name": "health",
            "description": "Returns the health status of the MCP server",
            "inputSchema": {
                "type": "object",
                "properties": {}
            }
        }),
    ]
}

pub fn call_tool(name: &str, arguments: &Value) -> Option<Value> {
    match name {
        "echo" => {
            let message = arguments
                .get("message")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            Some(json!({
                "content": [{"type": "text", "text": message}],
                "isError": false
            }))
        }
        "health" => Some(json!({
            "content": [{"type": "text", "text": "ok"}],
            "isError": false
        })),
        _ => None,
    }
}
