//! Native MCP JSON-RPC hub: `initialize` / `ping` / `tools/*` plus resources
//! and prompts. External `mcp__*` bridging is refused as a tool error, not a
//! protocol success.

use serde_json::{Value, json};

use crate::chat_tool_loop::{ToolRoundExecutor, WorkspaceBundle};
use crate::gateway_version;
use deepseek_policy::generated_files::{self, GENERATED_EXTENSIONS};
use deepseek_policy::python_json::dumps_compact;
use deepseek_policy::tool_batch::stable_tool_output_for_model;
use deepseek_policy::tool_catalog::mcp_tools;
use deepseek_policy::tool_policy::{ToolPolicy, ToolPolicyConfig};

pub const MCP_PROTOCOL_VERSION: &str = "2025-06-18";
const PARSE_ERROR: i64 = -32700;
const INVALID_REQUEST: i64 = -32600;
const METHOD_NOT_FOUND: i64 = -32601;
const INVALID_PARAMS: i64 = -32602;
const MAX_MCP_RESULT_CHARS: usize = 24_000;

const SERVER_INSTRUCTIONS: &str = "DeepSeek Infra 本地 Tool Hub：搜索、抓取、本地文件检索、Python 计算、图表、思维导图、PPT/Word/PDF 生成、记忆与提醒。所有工具调用都经过本地 Tool Policy 安全闸门。";

fn env_flag(name: &str, default: bool) -> bool {
    match std::env::var(name) {
        Ok(raw) if !raw.trim().is_empty() => matches!(
            raw.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        _ => default,
    }
}

fn hub_capability() -> String {
    let capability = std::env::var("MCP_CAPABILITY")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "full".to_string());
    let known = [
        "full",
        "researcher",
        "browser_reader",
        "coder",
        "reasoner",
        "critic",
    ];
    if known.contains(&capability.as_str()) {
        capability
    } else {
        "full".to_string()
    }
}

fn expose_resources() -> bool {
    env_flag("MCP_EXPOSE_RESOURCES", true)
}
fn expose_prompts() -> bool {
    env_flag("MCP_EXPOSE_PROMPTS", true)
}

fn rpc_result(id: &Value, result: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": result})
}

fn rpc_error(id: &Value, code: i64, message: &str, data: Option<Value>) -> Value {
    let mut error = json!({"code": code, "message": message});
    if let Some(data) = data {
        error
            .as_object_mut()
            .unwrap()
            .insert("data".to_string(), data);
    }
    json!({"jsonrpc": "2.0", "id": id, "error": error})
}

fn params_object(message: &Value) -> Value {
    match message.get("params") {
        Some(Value::Object(_)) => message["params"].clone(),
        _ => json!({}),
    }
}

fn python_str(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Null) | None => String::new(),
        Some(other) => other.to_string(),
    }
}

fn initialize() -> Value {
    let mut capabilities = json!({"tools": {"listChanged": false}});
    if expose_resources() {
        capabilities.as_object_mut().unwrap().insert(
            "resources".to_string(),
            json!({"subscribe": false, "listChanged": false}),
        );
    }
    if expose_prompts() {
        capabilities
            .as_object_mut()
            .unwrap()
            .insert("prompts".to_string(), json!({"listChanged": false}));
    }
    json!({
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": capabilities,
        "serverInfo": {
            "name": "deepseek-infra",
            "title": "DeepSeek Infra MCP Tool Hub",
            "version": gateway_version(),
        },
        "instructions": SERVER_INSTRUCTIONS,
    })
}

fn tools_call(params: &Value) -> Result<Value, (i64, String, Option<Value>)> {
    let name = python_str(params.get("name")).trim().to_string();
    if name.is_empty() {
        return Err((INVALID_PARAMS, "name is required".to_string(), None));
    }
    if let Some(arguments) = params.get("arguments") {
        if !arguments.is_null() && !arguments.is_object() {
            return Err((
                INVALID_PARAMS,
                "arguments must be an object".to_string(),
                None,
            ));
        }
    }
    let arguments = match params.get("arguments") {
        Some(Value::Object(_)) => params["arguments"].clone(),
        _ => json!({}),
    };
    if name.starts_with("mcp__") {
        let output = json!({
            "ok": false,
            "tool": name,
            "error": "external MCP bridging is not wired on the native hub",
            "code": "not_implemented",
        });
        return Ok(shape_tool_result(&output));
    }
    let output = call_local_tool(&name, &arguments, params.get("_meta"));
    Ok(shape_tool_result(&output))
}

fn approvals_from_meta(meta: Option<&Value>) -> Vec<String> {
    let Some(Value::Object(fields)) = meta else {
        return Vec::new();
    };
    let Some(Value::Array(items)) = fields.get("approvedTools") else {
        return Vec::new();
    };
    items
        .iter()
        .map(|item| python_str(Some(item)).trim().to_string())
        .filter(|item| !item.is_empty())
        .collect()
}

fn call_local_tool(name: &str, arguments: &Value, meta: Option<&Value>) -> Value {
    let config = ToolPolicyConfig {
        capability: hub_capability(),
        scope: "mcp".to_string(),
        approvals: approvals_from_meta(meta),
        ..ToolPolicyConfig::default()
    };
    let workspace = std::env::var_os("DEEPSEEK_INFRA_ROOT")
        .map(std::path::PathBuf::from)
        .map(WorkspaceBundle::new);
    let executor = ToolRoundExecutor::new(workspace, Some(ToolPolicy::new(config)));
    executor.execute_call_sync(name, arguments)
}

fn shape_tool_result(output: &Value) -> Value {
    let stable = stable_tool_output_for_model(output);
    let text: String = dumps_compact(&stable)
        .chars()
        .take(MAX_MCP_RESULT_CHARS)
        .collect();
    json!({
        "content": [{"type": "text", "text": text}],
        "structuredContent": stable,
        "isError": stable.get("ok") != Some(&Value::Bool(true)),
    })
}

fn resources_list() -> Value {
    if !expose_resources() {
        return json!({"resources": []});
    }
    let mut resources = vec![json!({
        "uri": "runtime://capabilities",
        "name": "runtime-capabilities",
        "title": "DeepSeek Infra tool policy & capability profiles",
        "mimeType": "application/json",
    })];
    if let Some(root) = std::env::var_os("DEEPSEEK_INFRA_ROOT") {
        let dir = generated_files::generated_dir(std::path::Path::new(&root));
        if let Ok(entries) = std::fs::read_dir(&dir) {
            let mut files: Vec<_> = entries.filter_map(Result::ok).map(|e| e.path()).collect();
            files.sort();
            for path in files {
                let Some(ext) = path.extension().and_then(|ext| ext.to_str()) else {
                    continue;
                };
                if !GENERATED_EXTENSIONS.contains(&ext) {
                    continue;
                }
                let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
                let name = path.file_name().and_then(|s| s.to_str()).unwrap_or("");
                resources.push(json!({
                    "uri": format!("generated://{stem}"),
                    "name": name,
                    "title": format!("Generated artifact {name}"),
                    "mimeType": mime_for(ext),
                }));
            }
        }
    }
    json!({"resources": resources})
}

fn mime_for(ext: &str) -> &'static str {
    match ext {
        "pptx" => "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "docx" => "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf" => "application/pdf",
        "md" => "text/markdown; charset=utf-8",
        "svg" => "image/svg+xml",
        _ => "application/octet-stream",
    }
}

fn resources_read(params: &Value) -> Result<Value, (i64, String, Option<Value>)> {
    if !expose_resources() {
        return Err((
            INVALID_PARAMS,
            "MCP resources are disabled".to_string(),
            Some(json!({"code": "forbidden"})),
        ));
    }
    let uri = python_str(params.get("uri")).trim().to_string();
    if uri.is_empty() {
        return Err((INVALID_PARAMS, "uri is required".to_string(), None));
    }
    if uri == "runtime://capabilities" {
        let document = json!({
            "capability": hub_capability(),
            "toolPolicy": {"capability": hub_capability()},
        });
        let text = serde_json::to_string(&document).unwrap_or_else(|_| "{}".to_string());
        return Ok(json!({"contents": [{
            "uri": uri,
            "mimeType": "application/json",
            "text": text,
        }]}));
    }
    if let Some(file_id) = uri.strip_prefix("generated://") {
        let root = std::env::var_os("DEEPSEEK_INFRA_ROOT").map(std::path::PathBuf::from);
        let Some(root) = root else {
            return Err((
                INVALID_PARAMS,
                "Resource not found".to_string(),
                Some(json!({"code": "not_found"})),
            ));
        };
        let Some(path) = generated_files::resolve_generated_file(&root, file_id) else {
            return Err((
                INVALID_PARAMS,
                "Resource not found".to_string(),
                Some(json!({"code": "not_found"})),
            ));
        };
        let ext = path.extension().and_then(|ext| ext.to_str()).unwrap_or("");
        let media_type = mime_for(ext);
        if media_type == "image/svg+xml" || media_type.starts_with("text/") {
            let text = std::fs::read_to_string(&path).unwrap_or_default();
            return Ok(json!({"contents": [{"uri": uri, "mimeType": media_type, "text": text}]}));
        }
        let blob = data_encoding_base64(&std::fs::read(&path).unwrap_or_default());
        return Ok(json!({"contents": [{"uri": uri, "mimeType": media_type, "blob": blob}]}));
    }
    Err((
        INVALID_PARAMS,
        "Resource not found".to_string(),
        Some(json!({"code": "not_found"})),
    ))
}

fn data_encoding_base64(bytes: &[u8]) -> String {
    const TABLE: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let a = chunk[0];
        let b = chunk.get(1).copied().unwrap_or(0);
        let c = chunk.get(2).copied().unwrap_or(0);
        out.push(TABLE[(a >> 2) as usize] as char);
        out.push(TABLE[(((a & 3) << 4) | (b >> 4)) as usize] as char);
        if chunk.len() > 1 {
            out.push(TABLE[(((b & 15) << 2) | (c >> 6)) as usize] as char);
        } else {
            out.push('=');
        }
        if chunk.len() > 2 {
            out.push(TABLE[(c & 63) as usize] as char);
        } else {
            out.push('=');
        }
    }
    out
}

fn prompts_list() -> Value {
    if !expose_prompts() {
        return json!({"prompts": []});
    }
    json!({"prompts": [
        {
            "name": "slides-outline",
            "title": "生成 PPT 大纲并落成文件",
            "description": "围绕一个主题规划 6-10 页演示文稿大纲，并调用 create_pptx 生成真实的 .pptx 文件。",
            "arguments": [
                {"name": "topic", "description": "演示文稿主题", "required": true},
                {"name": "audience", "description": "目标听众（可选）", "required": false}
            ]
        },
        {
            "name": "research-brief",
            "title": "联网检索并输出带引用的简报",
            "description": "用 web_search / fetch_url 检索一个主题，输出带 [^Wn] 引用标记的事实简报。",
            "arguments": [
                {"name": "topic", "description": "要调研的主题", "required": true}
            ]
        }
    ]})
}

fn prompts_get(params: &Value) -> Result<Value, (i64, String, Option<Value>)> {
    if !expose_prompts() {
        return Err((
            INVALID_PARAMS,
            "MCP prompts are disabled".to_string(),
            Some(json!({"code": "forbidden"})),
        ));
    }
    let name = python_str(params.get("name")).trim().to_string();
    if name.is_empty() {
        return Err((INVALID_PARAMS, "name is required".to_string(), None));
    }
    if let Some(arguments) = params.get("arguments") {
        if !arguments.is_null() && !arguments.is_object() {
            return Err((
                INVALID_PARAMS,
                "arguments must be an object".to_string(),
                None,
            ));
        }
    }
    let args = match params.get("arguments") {
        Some(Value::Object(_)) => params["arguments"].clone(),
        _ => json!({}),
    };
    let topic = python_str(args.get("topic")).trim().to_string();
    let topic = if topic.is_empty() {
        "（未提供主题）".to_string()
    } else {
        topic
    };
    let (description, text) = match name.as_str() {
        "slides-outline" => {
            let audience = python_str(args.get("audience")).trim().to_string();
            let audience_line = if audience.is_empty() {
                String::new()
            } else {
                format!("目标听众：{audience}。")
            };
            (
                "围绕一个主题规划 6-10 页演示文稿大纲，并调用 create_pptx 生成真实的 .pptx 文件。",
                format!(
                    "请为主题「{topic}」规划一份 6-10 页的演示文稿大纲。{audience_line}每页一个结论式标题，3-6 个 lead：detail 形式的要点，并为关键页选择 cards/process/comparison/summary 版式；随后调用 create_pptx 工具生成真实的 .pptx 文件并返回下载链接。"
                ),
            )
        }
        "research-brief" => (
            "用 web_search / fetch_url 检索一个主题，输出带 [^Wn] 引用标记的事实简报。",
            format!(
                "请围绕主题「{topic}」做联网调研：先用 web_search 检索（必要时 fetch_url 读取关键页面），然后输出一份事实简报：核心结论、关键事实（每条后跟来源的 [^Wn] 引用标记）、不确定之处。"
            ),
        ),
        _ => {
            return Err((
                INVALID_PARAMS,
                "Prompt not found".to_string(),
                Some(json!({"code": "not_found"})),
            ));
        }
    };
    Ok(json!({
        "description": description,
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }))
}

/// Dispatch one JSON-RPC MCP message. `None` means a notification (HTTP 202).
pub fn handle_mcp_message(message: &Value) -> Option<Value> {
    let Value::Object(fields) = message else {
        return Some(rpc_error(
            &Value::Null,
            INVALID_REQUEST,
            "Request must be a JSON object",
            None,
        ));
    };
    let is_notification = !fields.contains_key("id");
    let id = message.get("id").cloned().unwrap_or(Value::Null);
    if message.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return if is_notification {
            None
        } else {
            Some(rpc_error(
                &id,
                INVALID_REQUEST,
                "jsonrpc must be '2.0'",
                None,
            ))
        };
    }
    let method = python_str(message.get("method"));
    if method.is_empty() {
        return if is_notification {
            None
        } else {
            Some(rpc_error(&id, INVALID_REQUEST, "method is required", None))
        };
    }
    if method.starts_with("notifications/") {
        return None;
    }
    let params = params_object(message);
    let result = match method.as_str() {
        "initialize" => Ok(initialize()),
        "ping" => Ok(json!({})),
        "tools/list" => Ok(json!({"tools": mcp_tools(&hub_capability())})),
        "tools/call" => tools_call(&params),
        "resources/list" => Ok(resources_list()),
        "resources/read" => resources_read(&params),
        "prompts/list" => Ok(prompts_list()),
        "prompts/get" => prompts_get(&params),
        _ => {
            return if is_notification {
                None
            } else {
                Some(rpc_error(
                    &id,
                    METHOD_NOT_FOUND,
                    &format!("Method not found: {method}"),
                    None,
                ))
            };
        }
    };
    if is_notification {
        return None;
    }
    match result {
        Ok(value) => Some(rpc_result(&id, value)),
        Err((code, message, data)) => Some(rpc_error(&id, code, &message, data)),
    }
}

/// HTTP entry: parse body then dispatch. Empty body is invalid request.
pub fn handle_mcp_bytes(body: &[u8]) -> (u16, Option<Value>) {
    if body.is_empty() {
        return (
            200,
            Some(rpc_error(
                &Value::Null,
                INVALID_REQUEST,
                "Invalid Request: empty body",
                None,
            )),
        );
    }
    let value: Value = match serde_json::from_slice(body) {
        Ok(value) => value,
        Err(_) => {
            return (
                200,
                Some(rpc_error(&Value::Null, PARSE_ERROR, "Parse error", None)),
            );
        }
    };
    match handle_mcp_message(&value) {
        None => (202, None),
        Some(response) => (200, Some(response)),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tools_list_includes_runtime_tools() {
        let response = handle_mcp_message(&json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list"
        }))
        .unwrap();
        let names: Vec<&str> = response["result"]["tools"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|tool| tool["name"].as_str())
            .collect();
        assert!(names.contains(&"python_eval"));
        assert!(names.contains(&"create_pptx"));
        assert!(names.contains(&"web_search"));
        let pptx = response["result"]["tools"]
            .as_array()
            .unwrap()
            .iter()
            .find(|tool| tool["name"] == "create_pptx")
            .unwrap();
        assert_eq!(pptx["annotations"]["readOnlyHint"], false);
    }

    #[test]
    fn tools_call_runs_python_eval() {
        let response = handle_mcp_message(&json!({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "python_eval", "arguments": {"expression": "2+2"}}
        }))
        .unwrap();
        assert_eq!(response["result"]["isError"], false);
        assert_eq!(
            response["result"]["structuredContent"]["result"]["result"],
            "4"
        );
    }

    #[test]
    fn missing_name_is_invalid_params() {
        let response = handle_mcp_message(&json!({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {}
        }))
        .unwrap();
        assert_eq!(response["error"]["code"], INVALID_PARAMS);
    }
}
