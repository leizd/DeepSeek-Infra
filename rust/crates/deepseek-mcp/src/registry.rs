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
