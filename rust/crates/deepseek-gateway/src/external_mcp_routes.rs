//! `GET /api/mcp/external/tools`.
//!
//! Mirrors `_list_external_mcp_tools()` / `ExternalMCPToolRegistry`: when
//! `MCP_CLIENT_ENABLED` is false or no outbound servers are configured, answers
//! quickly without making network requests. When enabled with `MCP_CLIENT_SERVERS`,
//! connects via Streamable HTTP JSON-RPC (`initialize` -> `tools/list`, JSON or
//! SSE `data:` payloads), infers conservative risk/capability profiles, and
//! emits collision-safe `mcp__<server>__<tool>` descriptors.

use std::collections::{HashMap, HashSet};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::http::Method;
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::{Json, Router};
use deepseek_policy::core_utils::python_truthy;
use deepseek_policy::python_json::value_str;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::backup_catalog_routes::utc_iso_from_unix;
use crate::mcp_status_routes::{
    env_bool, env_float_clamped, env_int_clamped, parse_client_servers,
};
use crate::route_support;

const MCP_PROTOCOL_VERSION: &str = "2025-06-18";

const URL_KEYS: [&str; 6] = ["url", "uri", "endpoint", "base_url", "host", "domain"];
const PATH_KEYS: [&str; 7] = [
    "path",
    "file",
    "filename",
    "filepath",
    "directory",
    "folder",
    "dir",
];
const SENSITIVE_KEYS: [&str; 9] = [
    "env",
    "token",
    "secret",
    "key",
    "password",
    "apikey",
    "api_key",
    "credential",
    "auth",
];
const DESTRUCTIVE_WORDS: [&str; 6] = [
    "delete",
    "remove",
    "destroy",
    "drop",
    "truncate",
    "overwrite",
];

#[derive(Clone)]
struct ServerConfig {
    name: String,
    url: String,
    timeout_seconds: i64,
}

struct RpcFailure {
    message: String,
    error_type: &'static str,
    retry_count: usize,
    latency_ms: u64,
    timed_out: bool,
}

struct RpcSuccess {
    body: Value,
    session_id: Option<String>,
    retry_count: usize,
    latency_ms: u64,
}

#[derive(Clone)]
struct ToolProfile {
    server: String,
    tool: String,
    bridged_name: String,
    risk: &'static str,
    network: bool,
    filesystem: bool,
    requires_approval: bool,
}

pub fn router() -> Router {
    Router::new().route("/api/mcp/external/tools", any(dispatch))
}

async fn dispatch(method: Method) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return route_support::unallowed_api_method(method, Method::GET);
    }
    let body = build_external_tools_body().await;
    if method == Method::HEAD {
        axum::http::StatusCode::OK.into_response()
    } else {
        Json(body).into_response()
    }
}

fn configured_server_list() -> (bool, Vec<ServerConfig>, i64, f64, i64) {
    let enabled = env_bool("MCP_CLIENT_ENABLED", false);
    let default_timeout = env_int_clamped("MCP_CLIENT_TIMEOUT_SECONDS", 30, 1, 600);
    let max_retries = env_int_clamped("MCP_CLIENT_MAX_RETRIES", 1, 0, 5);
    let retry_backoff = env_float_clamped("MCP_CLIENT_RETRY_BACKOFF_SECONDS", 0.25, 0.0, 10.0);
    let cb_failures = env_int_clamped("MCP_CLIENT_CIRCUIT_BREAKER_FAILURES", 3, 1, 20);
    let (servers, timeouts) = parse_client_servers();
    let list = servers
        .into_iter()
        .map(|(name, url)| {
            let trimmed_url = url.trim_end_matches('/').to_string();
            let timeout = timeouts.get(&name).copied().unwrap_or(default_timeout);
            ServerConfig {
                name,
                url: trimmed_url,
                timeout_seconds: timeout,
            }
        })
        .collect();
    (enabled, list, max_retries, retry_backoff, cb_failures)
}

pub(crate) async fn build_external_tools_body() -> Value {
    let (enabled, servers, max_retries, retry_backoff, cb_failures) = configured_server_list();
    if !enabled || servers.is_empty() {
        let server_statuses: Vec<Value> = servers
            .iter()
            .map(|server| {
                json!({
                    "name": server.name,
                    "url": server.url,
                    "available": false,
                    "status": "unknown",
                    "timeoutSeconds": server.timeout_seconds,
                    "consecutiveFailures": 0,
                    "failureCount": 0,
                    "timeoutCount": 0,
                    "callCount": 0,
                    "lastError": "",
                    "lastErrorType": "",
                    "lastRefreshAt": "",
                    "lastSuccessAt": "",
                    "lastCallAt": "",
                    "lastLatencyMs": 0,
                    "lastRetryCount": 0,
                    "circuitOpenSeconds": 0.0,
                })
            })
            .collect();
        return json!({
            "ok": true,
            "servers": server_statuses,
            "tools": [],
        });
    }

    let mut server_statuses = Vec::with_capacity(servers.len());
    let mut existing_names = HashSet::new();
    let mut tools_out = Vec::new();

    for server in &servers {
        let (health_json, fetched_tools) =
            fetch_server_tools(server, max_retries, retry_backoff, cb_failures).await;
        server_statuses.push(health_json);
        for tool_def in fetched_tools {
            if !tool_def.is_object() {
                continue;
            }
            let profile = infer_profile(&server.name, &tool_def);
            let profile = collision_safe_profile(profile, &mut existing_names);
            tools_out.push(json!({
                "server": profile.server,
                "tool": profile.tool,
                "bridgedName": profile.bridged_name,
                "risk": profile.risk,
                "network": profile.network,
                "filesystem": profile.filesystem,
                "requiresApproval": profile.requires_approval,
            }));
        }
    }

    json!({
        "ok": true,
        "servers": server_statuses,
        "tools": tools_out,
    })
}

fn now_utc_iso() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0);
    utc_iso_from_unix(secs)
}

async fn fetch_server_tools(
    server: &ServerConfig,
    max_retries: i64,
    retry_backoff: f64,
    cb_failures: i64,
) -> (Value, Vec<Value>) {
    let timeout = Duration::from_secs(server.timeout_seconds.max(1) as u64);
    let client = match reqwest::Client::builder().timeout(timeout).build() {
        Ok(client) => client,
        Err(error) => {
            return (
                failure_health_json(
                    server,
                    &RpcFailure {
                        message: format!("MCP server {} is unreachable: {error}", server.name),
                        error_type: "unreachable",
                        retry_count: 0,
                        latency_ms: 0,
                        timed_out: false,
                    },
                    cb_failures,
                ),
                Vec::new(),
            );
        }
    };

    let init_payload = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": "deepseek-infra-client",
                "version": "1.0",
            },
        },
    });
    let init_resp = match rpc_call(
        &client,
        server,
        &init_payload,
        "",
        "",
        max_retries,
        retry_backoff,
    )
    .await
    {
        Ok(ok) => ok,
        Err(failure) => {
            return (
                failure_health_json(server, &failure, cb_failures),
                Vec::new(),
            );
        }
    };

    let session_id = init_resp.session_id.unwrap_or_default();
    let protocol_version = init_resp
        .body
        .get("result")
        .and_then(|result| result.get("protocolVersion"))
        .filter(|value| python_truthy(value))
        .map(value_str)
        .unwrap_or_default();

    let notify_payload = json!({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    });
    let _ = post_once(
        &client,
        &server.url,
        &notify_payload,
        &protocol_version,
        &session_id,
    )
    .await;

    let list_payload = json!({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
    });
    let list_resp = match rpc_call(
        &client,
        server,
        &list_payload,
        &protocol_version,
        &session_id,
        max_retries,
        retry_backoff,
    )
    .await
    {
        Ok(ok) => ok,
        Err(failure) => {
            return (
                failure_health_json(server, &failure, cb_failures),
                Vec::new(),
            );
        }
    };

    let tools = list_resp
        .body
        .get("result")
        .and_then(|result| result.get("tools"))
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let ts = now_utc_iso();
    let health = json!({
        "name": server.name,
        "url": server.url,
        "available": true,
        "status": "ok",
        "timeoutSeconds": server.timeout_seconds,
        "consecutiveFailures": 0,
        "failureCount": 0,
        "timeoutCount": 0,
        "callCount": 0,
        "lastError": "",
        "lastErrorType": "",
        "lastRefreshAt": ts,
        "lastSuccessAt": ts,
        "lastCallAt": "",
        "lastLatencyMs": list_resp.latency_ms,
        "lastRetryCount": list_resp.retry_count,
        "circuitOpenSeconds": 0.0,
    });
    (health, tools)
}

fn failure_health_json(server: &ServerConfig, failure: &RpcFailure, cb_failures: i64) -> Value {
    let ts = now_utc_iso();
    let open = cb_failures <= 1;
    let reset_seconds = env_int_clamped("MCP_CLIENT_CIRCUIT_BREAKER_RESET_SECONDS", 60, 1, 3600);
    json!({
        "name": server.name,
        "url": server.url,
        "available": false,
        "status": if open { "circuit_open" } else { "unavailable" },
        "timeoutSeconds": server.timeout_seconds,
        "consecutiveFailures": 1,
        "failureCount": 1,
        "timeoutCount": if failure.timed_out { 1 } else { 0 },
        "callCount": 0,
        "lastError": failure.message,
        "lastErrorType": failure.error_type,
        "lastRefreshAt": ts,
        "lastSuccessAt": "",
        "lastCallAt": "",
        "lastLatencyMs": failure.latency_ms,
        "lastRetryCount": failure.retry_count,
        "circuitOpenSeconds": if open { reset_seconds as f64 } else { 0.0 },
    })
}

async fn rpc_call(
    client: &reqwest::Client,
    server: &ServerConfig,
    payload: &Value,
    protocol_version: &str,
    session_id: &str,
    max_retries: i64,
    retry_backoff: f64,
) -> Result<RpcSuccess, RpcFailure> {
    let started = Instant::now();
    let max_attempts = (max_retries.max(0) + 1) as usize;
    let mut attempt = 1usize;
    loop {
        match post_once(client, &server.url, payload, protocol_version, session_id).await {
            Ok((status, resp_headers, raw)) => {
                if status >= 500 && attempt < max_attempts {
                    let delay = retry_backoff * (attempt as f64);
                    if delay > 0.0 {
                        tokio::time::sleep(Duration::from_secs_f64(delay)).await;
                    }
                    attempt += 1;
                    continue;
                }
                let latency_ms = started.elapsed().as_millis() as u64;
                let retry_count = attempt.saturating_sub(1);
                if status >= 400 {
                    return Err(RpcFailure {
                        message: format!("MCP server {} returned HTTP {status}", server.name),
                        error_type: "http_error",
                        retry_count,
                        latency_ms,
                        timed_out: false,
                    });
                }
                let parsed =
                    parse_rpc_body(&server.name, &resp_headers, &raw, retry_count, latency_ms)?;
                if let Some(error_obj) = parsed.get("error").and_then(Value::as_object) {
                    let code_str = error_obj
                        .get("code")
                        .map(value_str)
                        .unwrap_or_else(|| "None".to_string());
                    let msg_str = error_obj.get("message").map(value_str).unwrap_or_default();
                    return Err(RpcFailure {
                        message: format!(
                            "MCP server {} error {}: {}",
                            server.name, code_str, msg_str
                        ),
                        error_type: "upstream_failure",
                        retry_count,
                        latency_ms,
                        timed_out: false,
                    });
                }
                return Ok(RpcSuccess {
                    body: parsed,
                    session_id: resp_headers.get("mcp-session-id").cloned(),
                    retry_count,
                    latency_ms,
                });
            }
            Err(error) => {
                let is_timeout = error.is_timeout();
                if attempt < max_attempts {
                    let delay = retry_backoff * (attempt as f64);
                    if delay > 0.0 {
                        tokio::time::sleep(Duration::from_secs_f64(delay)).await;
                    }
                    attempt += 1;
                    continue;
                }
                return Err(RpcFailure {
                    message: format!("MCP server {} is unreachable: {error}", server.name),
                    error_type: if is_timeout { "timeout" } else { "unreachable" },
                    retry_count: attempt.saturating_sub(1),
                    latency_ms: started.elapsed().as_millis() as u64,
                    timed_out: is_timeout,
                });
            }
        }
    }
}

async fn post_once(
    client: &reqwest::Client,
    url: &str,
    payload: &Value,
    protocol_version: &str,
    session_id: &str,
) -> Result<(u16, HashMap<String, String>, Vec<u8>), reqwest::Error> {
    let encoded = serde_json::to_vec(payload).unwrap_or_default();
    let mut builder = client
        .post(url)
        .header("Content-Type", "application/json")
        .header("Accept", "application/json, text/event-stream")
        .body(encoded);
    if !protocol_version.is_empty() {
        builder = builder.header("MCP-Protocol-Version", protocol_version);
    }
    if !session_id.is_empty() {
        builder = builder.header("Mcp-Session-Id", session_id);
    }
    let response = builder.send().await?;
    let status = response.status().as_u16();
    let mut headers = HashMap::new();
    for (name, value) in response.headers() {
        if let Ok(text) = value.to_str() {
            headers.insert(name.as_str().to_ascii_lowercase(), text.to_string());
        }
    }
    let bytes = response.bytes().await?.to_vec();
    Ok((status, headers, bytes))
}

fn parse_rpc_body(
    server_name: &str,
    headers: &HashMap<String, String>,
    raw: &[u8],
    retry_count: usize,
    latency_ms: u64,
) -> Result<Value, RpcFailure> {
    if raw.is_empty() {
        return Err(RpcFailure {
            message: format!("MCP server {server_name} returned no response"),
            error_type: "upstream_failure",
            retry_count,
            latency_ms,
            timed_out: false,
        });
    }
    let text = std::str::from_utf8(raw).map_err(|_| RpcFailure {
        message: format!("MCP server {server_name} returned invalid JSON"),
        error_type: "schema_error",
        retry_count,
        latency_ms,
        timed_out: false,
    })?;
    let content_type = headers
        .get("content-type")
        .map(|value| value.as_str())
        .unwrap_or("");
    let parsed_opt = if content_type.contains("text/event-stream") {
        parse_sse_jsonrpc(text)
    } else {
        serde_json::from_str::<Value>(text)
            .ok()
            .filter(Value::is_object)
    };
    match parsed_opt {
        Some(obj) => Ok(obj),
        None => Err(RpcFailure {
            message: format!("MCP server {server_name} returned invalid JSON"),
            error_type: "schema_error",
            retry_count,
            latency_ms,
            timed_out: false,
        }),
    }
}

fn parse_sse_jsonrpc(text: &str) -> Option<Value> {
    let normalized = text.replace("\r\n", "\n");
    for block in normalized.split("\n\n") {
        let mut data_parts = Vec::new();
        for line in block.split('\n') {
            if let Some(rest) = line.strip_prefix("data:") {
                data_parts.push(rest.trim_start());
            }
        }
        if data_parts.is_empty() {
            continue;
        }
        let joined = data_parts.join("");
        if let Ok(parsed) = serde_json::from_str::<Value>(&joined) {
            if parsed.is_object() {
                return Some(parsed);
            }
        }
    }
    None
}

fn safe_name(raw: &str) -> String {
    raw.trim()
        .to_ascii_lowercase()
        .chars()
        .map(|character| {
            if character.is_ascii_alphanumeric() || character == '_' || character == '-' {
                character
            } else {
                '_'
            }
        })
        .collect()
}

fn bridged_name(server: &str, tool: &str) -> String {
    format!("mcp__{}__{}", safe_name(server), safe_name(tool))
}

fn collect_schema_keys(schema: &Value) -> HashSet<String> {
    let mut keys = HashSet::new();
    let Some(properties) = schema.get("properties").and_then(Value::as_object) else {
        return keys;
    };
    for (key, value) in properties {
        keys.insert(key.to_ascii_lowercase());
        if let Some(nested) = value.get("properties").and_then(Value::as_object) {
            for nested_key in nested.keys() {
                keys.insert(nested_key.to_ascii_lowercase());
            }
        }
    }
    keys
}

fn infer_profile(server_name: &str, tool_def: &Value) -> ToolProfile {
    let annotations = tool_def
        .get("annotations")
        .filter(|value| value.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}));
    let schema = tool_def
        .get("inputSchema")
        .filter(|value| value.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}));
    let description = tool_def
        .get("description")
        .filter(|value| python_truthy(value))
        .map(value_str)
        .unwrap_or_default()
        .to_ascii_lowercase();
    let original_name = tool_def
        .get("name")
        .filter(|value| python_truthy(value))
        .map(value_str)
        .unwrap_or_default();

    let destructive = annotations
        .get("destructiveHint")
        .is_some_and(python_truthy);
    let open_world = annotations.get("openWorldHint").is_some_and(python_truthy);

    let schema_keys = collect_schema_keys(&schema);
    let has_url_key = URL_KEYS.iter().any(|key| schema_keys.contains(*key));
    let has_path_key = PATH_KEYS.iter().any(|key| schema_keys.contains(*key));
    let has_sensitive_key = SENSITIVE_KEYS.iter().any(|key| schema_keys.contains(*key));
    let has_destructive_desc = DESTRUCTIVE_WORDS
        .iter()
        .any(|word| description.contains(word));

    let mut risk = "medium";
    if destructive || has_sensitive_key || has_destructive_desc {
        risk = "high";
    }
    if has_url_key && has_sensitive_key {
        risk = "critical";
    }
    if (description.contains("critical") || description.contains("sensitive")) && risk == "medium" {
        risk = "high";
    }

    let network = open_world || has_url_key;
    let filesystem = has_path_key;
    let requires_approval =
        destructive || has_sensitive_key || risk == "high" || risk == "critical";

    ToolProfile {
        server: server_name.to_string(),
        tool: original_name.clone(),
        bridged_name: bridged_name(server_name, &original_name),
        risk,
        network,
        filesystem,
        requires_approval,
    }
}

fn collision_safe_profile(mut profile: ToolProfile, existing: &mut HashSet<String>) -> ToolProfile {
    if !existing.contains(&profile.bridged_name) {
        existing.insert(profile.bridged_name.clone());
        return profile;
    }
    let mut hasher = Sha256::new();
    hasher.update(profile.server.as_bytes());
    hasher.update(b"\0");
    hasher.update(profile.tool.as_bytes());
    let digest = format!("{:x}", hasher.finalize());
    let suffix = &digest[..6];
    let mut candidate = format!("{}__{suffix}", profile.bridged_name);
    let mut counter = 2usize;
    while existing.contains(&candidate) {
        candidate = format!("{}__{suffix}{counter}", profile.bridged_name);
        counter += 1;
    }
    existing.insert(candidate.clone());
    profile.bridged_name = candidate;
    profile
}
