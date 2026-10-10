use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{Path as AxumPath, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde_json::{Value, json};

use crate::model::{
    BackupFence, CreateTaskInput, RestoreJournal, StoreError, TaskArguments, TaskStatus,
};
use crate::store::TaskBackend;

#[derive(Clone)]
pub struct ServiceConfig {
    pub instance_id: String,
    pub workspace_root: PathBuf,
    pub allowed_hosts: Vec<String>,
    pub auth_token: Option<String>,
    pub internal_backup_token: Option<String>,
    pub durable_task_state: String,
    pub max_output_bytes: usize,
    pub task_timeout_seconds: u64,
    pub lease_ms: i64,
}

#[derive(Clone)]
pub struct AppState {
    pub store: Arc<TaskBackend>,
    pub config: ServiceConfig,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/instance", get(instance))
        .route("/mcp", post(mcp))
        .route("/internal/backups/capabilities", get(backup_capabilities))
        .route("/internal/backups/prepare", post(prepare_backup))
        .route("/internal/backups/:backup_id/stream", get(stream_backup))
        .route("/internal/backups/:backup_id/release", post(release_backup))
        .route("/internal/restores/:restore_id", get(restore_status))
        .route(
            "/internal/restores/:restore_id/prepare",
            post(prepare_restore),
        )
        .route(
            "/internal/restores/:restore_id/commit-intent",
            post(commit_intent),
        )
        .route(
            "/internal/restores/:restore_id/commit",
            post(commit_restore),
        )
        .route(
            "/internal/restores/:restore_id/complete",
            post(complete_restore),
        )
        .route("/internal/restores/:restore_id/abort", post(abort_restore))
        .fallback(not_found)
        .with_state(state)
}

fn host_allowed(headers: &HeaderMap, allowed: &[String]) -> bool {
    let Some(raw) = headers
        .get(header::HOST)
        .and_then(|value| value.to_str().ok())
    else {
        return false;
    };
    let name = hostname(raw);
    allowed.iter().any(|item| item.eq_ignore_ascii_case(name))
}

fn hostname(host: &str) -> &str {
    if let Some(rest) = host.strip_prefix('[') {
        return rest.split(']').next().unwrap_or(host);
    }
    host.split(':').next().unwrap_or(host)
}

fn bearer_matches(expected: &str, headers: &HeaderMap) -> bool {
    let Some(header) = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
    else {
        return false;
    };
    let Some(provided) = header.strip_prefix("Bearer ") else {
        return false;
    };
    let left = expected.as_bytes();
    let right = provided.as_bytes();
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0u8;
    for (lhs, rhs) in left.iter().zip(right.iter()) {
        diff |= lhs ^ rhs;
    }
    diff == 0
}

fn gate(state: &AppState, headers: &HeaderMap) -> Result<(), Response> {
    if !host_allowed(headers, &state.config.allowed_hosts) {
        return Err(json_status(
            StatusCode::FORBIDDEN,
            json!({"error": "forbidden host"}),
        ));
    }
    Ok(())
}

fn internal_gate(state: &AppState, headers: &HeaderMap) -> Result<(), Response> {
    gate(state, headers)?;
    match &state.config.internal_backup_token {
        None => Err(json_status(
            StatusCode::NOT_FOUND,
            json!({"error": "not found"}),
        )),
        Some(token) if bearer_matches(token, headers) => Ok(()),
        Some(_) => Err(json_status(
            StatusCode::UNAUTHORIZED,
            json!({"error": "unauthorized"}),
        )),
    }
}

fn mcp_gate(state: &AppState, headers: &HeaderMap) -> Result<(), Response> {
    gate(state, headers)?;
    if let Some(token) = &state.config.auth_token {
        if !bearer_matches(token, headers) {
            return Err((
                StatusCode::UNAUTHORIZED,
                [(header::WWW_AUTHENTICATE, "Bearer realm=\"stateless-mcp\"")],
                Json(json!({"error": "unauthorized"})),
            )
                .into_response());
        }
    }
    Ok(())
}

fn json_status(status: StatusCode, body: Value) -> Response {
    (status, Json(body)).into_response()
}

fn store_error(error: StoreError) -> Response {
    let status = if matches!(error, StoreError::RestoreFenced) {
        StatusCode::LOCKED
    } else {
        StatusCode::CONFLICT
    };
    json_status(status, json!({"error": error.to_string()}))
}

async fn healthz(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(response) = gate(&state, &headers) {
        return response;
    }
    json_status(
        StatusCode::OK,
        json!({"status": "ok", "instanceId": state.config.instance_id}),
    )
}

async fn readyz(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(response) = gate(&state, &headers) {
        return response;
    }
    match state.store.get("readiness-probe") {
        Ok(_) => json_status(
            StatusCode::OK,
            json!({"status": "ready", "instanceId": state.config.instance_id}),
        ),
        Err(error) => json_status(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"status": "not-ready", "error": error.to_string()}),
        ),
    }
}

async fn instance(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(response) = gate(&state, &headers) {
        return response;
    }
    json_status(
        StatusCode::OK,
        json!({"instanceId": state.config.instance_id, "clientSessionState": "none"}),
    )
}

async fn not_found(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(response) = gate(&state, &headers) {
        return response;
    }
    json_status(StatusCode::NOT_FOUND, json!({"error": "not found"}))
}

async fn mcp(State(state): State<AppState>, headers: HeaderMap, body: Bytes) -> Response {
    if let Err(response) = mcp_gate(&state, &headers) {
        return response;
    }
    let text = match std::str::from_utf8(&body) {
        Ok(text) => text,
        Err(_) => return rpc_error(Value::Null, -32700, "parse error"),
    };
    let request: Value = match serde_json::from_str(text) {
        Ok(value) => value,
        Err(_) => return rpc_error(Value::Null, -32700, "parse error"),
    };
    if request.get("id").is_none() {
        return StatusCode::ACCEPTED.into_response();
    }
    let id = request.get("id").cloned().unwrap_or(Value::Null);
    if request.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return rpc_error(id, -32600, "invalid request");
    }
    let method = request.get("method").and_then(Value::as_str).unwrap_or("");
    let params = request.get("params").cloned().unwrap_or_else(|| json!({}));
    let result = match method {
        "initialize" => initialize_result(&state),
        "tools/list" => tools_list(),
        "tools/call" => tools_call(&state, &params),
        "ping" => json!({}),
        _ => return rpc_error(id, -32601, "method not found"),
    };
    Json(json!({"jsonrpc": "2.0", "id": id, "result": result})).into_response()
}

fn initialize_result(state: &AppState) -> Value {
    json!({
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {"listChanged": false}},
        "serverInfo": {"name": "deepseek-infra-stateless-mcp", "version": "1.0.0"},
        "instructions": format!("Stateless MCP task plane for {}", state.config.instance_id),
    })
}

fn tools_list() -> Value {
    json!({
        "tools": [
            tool_schema("server_info", "Stateless MCP server information", "Returns the serving instance and statelessness contract.", json!({"type": "object", "properties": {}}), true),
            tool_schema("code_search", "Search code", "Searches literal text with ripgrep inside the configured workspace.", json!({"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string"}, "glob": {"type": "string"}, "maxResults": {"type": "integer"}}, "required": ["query"]}), true),
            tool_schema("start_test_run", "Start test run", "Queues a durable test task. idempotencyKey is required and safely deduplicates retries.", json!({"type": "object", "properties": {"idempotencyKey": {"type": "string"}, "target": {"type": "string"}, "keyword": {"type": "string"}, "markers": {"type": "string"}, "timeoutSeconds": {"type": "integer"}}, "required": ["idempotencyKey", "target"]}), false),
            tool_schema("get_task", "Get test task", "Gets the durable status and result of a test task.", json!({"type": "object", "properties": {"taskId": {"type": "string"}}, "required": ["taskId"]}), true),
            tool_schema("query_logs", "Query test logs", "Queries persisted stdout and stderr from a durable test task.", json!({"type": "object", "properties": {"taskId": {"type": "string"}, "stream": {"type": "string"}, "contains": {"type": "string"}, "maxLines": {"type": "integer"}}, "required": ["taskId"]}), true)
        ]
    })
}

fn tool_schema(
    name: &str,
    title: &str,
    description: &str,
    schema: Value,
    read_only: bool,
) -> Value {
    json!({
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": schema,
        "annotations": {"readOnlyHint": read_only, "idempotentHint": true}
    })
}

fn tools_call(state: &AppState, params: &Value) -> Value {
    let name = params.get("name").and_then(Value::as_str).unwrap_or("");
    let arguments = params
        .get("arguments")
        .cloned()
        .unwrap_or_else(|| json!({}));
    match name {
        "server_info" => tool_ok(json!({
            "instanceId": state.config.instance_id,
            "clientSessionState": "none",
            "durableTaskState": state.config.durable_task_state,
        })),
        "code_search" => match code_search(state, &arguments) {
            Ok(value) => tool_ok(value),
            Err(error) => tool_err(error),
        },
        "start_test_run" => match start_test_run(state, &arguments) {
            Ok(value) => tool_ok(value),
            Err(error) => tool_err(error),
        },
        "get_task" => match get_task(state, &arguments) {
            Ok(value) => tool_ok(value),
            Err(error) => tool_err(error),
        },
        "query_logs" => match query_logs(state, &arguments) {
            Ok(value) => tool_ok(value),
            Err(error) => tool_err(error),
        },
        _ => tool_err(format!("unknown tool {name}")),
    }
}

fn tool_ok(value: Value) -> Value {
    json!({"content": [{"type": "text", "text": value.to_string()}]})
}

fn tool_err(message: String) -> Value {
    json!({"content": [{"type": "text", "text": message}], "isError": true})
}

fn start_test_run(state: &AppState, arguments: &Value) -> Result<Value, String> {
    let idempotency_key = required_len(arguments, "idempotencyKey", 8, 200)?;
    let target = required_len(arguments, "target", 1, 500)?;
    let keyword = optional_len(arguments, "keyword", 200)?;
    let markers = optional_len(arguments, "markers", 200)?;
    let timeout_seconds = arguments
        .get("timeoutSeconds")
        .and_then(Value::as_u64)
        .unwrap_or(600);
    if !(1..=3_600).contains(&timeout_seconds) {
        return Err("timeoutSeconds is out of range".to_string());
    }
    let now = now_millis();
    let created = state
        .store
        .create_or_get(CreateTaskInput {
            idempotency_key,
            arguments: TaskArguments {
                target,
                keyword,
                markers,
                timeout_seconds,
            },
            now,
        })
        .map_err(|error| error.to_string())?;
    Ok(json!({
        "taskId": created.task.id,
        "status": status_name(created.task.status),
        "attempts": created.task.attempts,
        "deduplicated": created.deduplicated,
    }))
}

fn get_task(state: &AppState, arguments: &Value) -> Result<Value, String> {
    let task_id = required_len(arguments, "taskId", 1, 80)?;
    let task = state
        .store
        .get(&task_id)
        .map_err(|error| error.to_string())?
        .ok_or_else(|| format!("task {task_id} was not found"))?;
    serde_json::to_value(task).map_err(|error| error.to_string())
}

fn query_logs(state: &AppState, arguments: &Value) -> Result<Value, String> {
    let task_id = required_len(arguments, "taskId", 1, 80)?;
    let stream = arguments
        .get("stream")
        .and_then(Value::as_str)
        .unwrap_or("all");
    let contains = arguments
        .get("contains")
        .and_then(Value::as_str)
        .map(str::to_string);
    let max_lines = arguments
        .get("maxLines")
        .and_then(Value::as_u64)
        .unwrap_or(200) as usize;
    if !(1..=1_000).contains(&max_lines) {
        return Err("maxLines is out of range".to_string());
    }
    let task = state
        .store
        .get(&task_id)
        .map_err(|error| error.to_string())?
        .ok_or_else(|| format!("task {task_id} was not found"))?;
    let mut entries = Vec::new();
    if stream != "stderr" {
        for line in task.stdout.split(['\n', '\r']) {
            if !line.is_empty() {
                entries.push(json!({"stream": "stdout", "line": line}));
            }
        }
    }
    if stream != "stdout" {
        for line in task.stderr.split(['\n', '\r']) {
            if !line.is_empty() {
                entries.push(json!({"stream": "stderr", "line": line}));
            }
        }
    }
    let filtered: Vec<Value> = entries
        .into_iter()
        .filter(|entry| {
            contains.as_ref().is_none_or(|needle| {
                entry
                    .get("line")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .contains(needle)
            })
        })
        .collect();
    let truncated = filtered.len() >= max_lines;
    let lines: Vec<Value> = filtered
        .into_iter()
        .rev()
        .take(max_lines)
        .collect::<Vec<_>>()
        .into_iter()
        .rev()
        .collect();
    Ok(
        json!({"taskId": task_id, "status": status_name(task.status), "lines": lines, "truncated": truncated}),
    )
}

fn code_search(state: &AppState, arguments: &Value) -> Result<Value, String> {
    let query = required_len(arguments, "query", 1, 500)?;
    let path = arguments.get("path").and_then(Value::as_str).unwrap_or(".");
    if path.is_empty() {
        return Err("path is empty".to_string());
    }
    let glob = arguments
        .get("glob")
        .and_then(Value::as_str)
        .map(str::to_string);
    let max_results = arguments
        .get("maxResults")
        .and_then(Value::as_u64)
        .unwrap_or(50);
    if !(1..=200).contains(&max_results) {
        return Err("maxResults is out of range".to_string());
    }
    let root = resolve_workspace_path(&state.config.workspace_root, path)?;
    crate::search::search_code(
        &state.config.workspace_root,
        &root,
        &query,
        glob.as_deref(),
        max_results as usize,
        state.config.max_output_bytes,
    )
}

pub fn resolve_workspace_path(workspace_root: &Path, candidate: &str) -> Result<PathBuf, String> {
    if candidate.contains('\0') {
        return Err("path contains a null byte".to_string());
    }
    let candidate_path = Path::new(candidate);
    let joined = if candidate_path.is_absolute() {
        candidate_path.to_path_buf()
    } else {
        workspace_root.join(candidate_path)
    };
    let normalized = normalize_lexical(&joined);
    let root = normalize_lexical(workspace_root);
    if normalized != root && !normalized.starts_with(root.join("")) {
        return Err("path must stay within the configured workspace".to_string());
    }
    Ok(normalized)
}

fn normalize_lexical(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for component in path.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            other => out.push(other.as_os_str()),
        }
    }
    out
}

fn required_len(value: &Value, field: &str, min: usize, max: usize) -> Result<String, String> {
    let text = value
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("{field} is required"))?;
    if text.len() < min || text.len() > max {
        return Err(format!("{field} length is out of range"));
    }
    Ok(text.to_string())
}

fn optional_len(value: &Value, field: &str, max: usize) -> Result<Option<String>, String> {
    match value.get(field).and_then(Value::as_str) {
        None => Ok(None),
        Some(text) if text.len() <= max && !text.is_empty() => Ok(Some(text.to_string())),
        Some(_) => Err(format!("{field} length is out of range")),
    }
}

fn status_name(status: TaskStatus) -> &'static str {
    match status {
        TaskStatus::Queued => "queued",
        TaskStatus::Running => "running",
        TaskStatus::Succeeded => "succeeded",
        TaskStatus::Failed => "failed",
        TaskStatus::Cancelled => "cancelled",
        TaskStatus::Interrupted => "interrupted",
    }
}

fn now_millis() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_millis() as i64)
        .unwrap_or(0)
}

fn rpc_error(id: Value, code: i64, message: &str) -> Response {
    Json(json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}))
        .into_response()
}

async fn backup_capabilities(State(state): State<AppState>, headers: HeaderMap) -> Response {
    if let Err(response) = internal_gate(&state, &headers) {
        return response;
    }
    match state.store.backup_capabilities() {
        Ok(body) => json_status(
            StatusCode::OK,
            serde_json::to_value(body).unwrap_or_else(|_| json!({})),
        ),
        Err(error) => store_error(error),
    }
}

async fn prepare_backup(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    if let Err(response) = internal_gate(&state, &headers) {
        return response;
    }
    let parsed: Value = match serde_json::from_slice(&body) {
        Ok(value) => value,
        Err(_) => return store_error(StoreError::Message("backupId is required".to_string())),
    };
    let Some(backup_id) = parsed.get("backupId").and_then(Value::as_str) else {
        return store_error(StoreError::Message("backupId is required".to_string()));
    };
    if backup_id.len() < 8 {
        return store_error(StoreError::Message("backupId is required".to_string()));
    }
    respond_fence(state.store.prepare_backup(backup_id, now_millis()))
}

fn respond_fence(result: Result<BackupFence, StoreError>) -> Response {
    match result {
        Ok(fence) => json_status(
            StatusCode::OK,
            serde_json::to_value(fence).unwrap_or_else(|_| json!({})),
        ),
        Err(error) => store_error(error),
    }
}

async fn stream_backup(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(backup_id): AxumPath<String>,
) -> Response {
    if let Err(response) = internal_gate(&state, &headers) {
        return response;
    }
    match state.store.export_backup(&backup_id) {
        Ok(body) => Response::builder()
            .status(StatusCode::OK)
            .header(header::CONTENT_TYPE, "application/x-ndjson")
            .header(header::CACHE_CONTROL, "no-store")
            .body(axum::body::Body::from(body))
            .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response()),
        Err(error) => store_error(error),
    }
}

async fn release_backup(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(backup_id): AxumPath<String>,
) -> Response {
    if let Err(response) = internal_gate(&state, &headers) {
        return response;
    }
    match state.store.release_backup(&backup_id) {
        Ok(()) => json_status(StatusCode::OK, json!({"ok": true, "backupId": backup_id})),
        Err(error) => store_error(error),
    }
}

async fn restore_status(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(restore_id): AxumPath<String>,
) -> Response {
    if let Err(response) = internal_gate(&state, &headers) {
        return response;
    }
    match state.store.restore_status(&restore_id) {
        Ok(Some(journal)) => journal_response(journal),
        Ok(None) => json_status(StatusCode::NOT_FOUND, json!({"error": "not found"})),
        Err(error) => store_error(error),
    }
}

async fn prepare_restore(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(restore_id): AxumPath<String>,
    body: Bytes,
) -> Response {
    if let Err(response) = internal_gate(&state, &headers) {
        return response;
    }
    let Some(transaction_digest) = headers
        .get("x-transaction-digest")
        .and_then(|value| value.to_str().ok())
    else {
        return store_error(StoreError::Message(
            "X-Transaction-Digest is required".to_string(),
        ));
    };
    if transaction_digest.len() < 8 {
        return store_error(StoreError::Message(
            "X-Transaction-Digest is required".to_string(),
        ));
    }
    let source = match std::str::from_utf8(&body) {
        Ok(text) => text,
        Err(_) => return store_error(StoreError::Message("snapshot is not utf-8".to_string())),
    };
    let journal =
        match state
            .store
            .prepare_restore(&restore_id, transaction_digest, source, now_millis())
        {
            Ok(journal) => journal,
            Err(error) => return store_error(error),
        };
    if let Some(expected) = headers
        .get("x-content-sha256")
        .and_then(|value| value.to_str().ok())
    {
        if !expected.is_empty() && expected != journal.source_digest {
            let _ = state.store.abort_restore(&restore_id, now_millis());
            return store_error(StoreError::Message(
                "snapshot digest does not match X-Content-SHA256".to_string(),
            ));
        }
    }
    journal_response(journal)
}

async fn commit_intent(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(restore_id): AxumPath<String>,
    body: Bytes,
) -> Response {
    restore_with_digest(&state, &headers, &restore_id, &body, |digest, now| {
        state.store.commit_restore_intent(&restore_id, digest, now)
    })
}

async fn commit_restore(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(restore_id): AxumPath<String>,
    body: Bytes,
) -> Response {
    restore_with_digest(&state, &headers, &restore_id, &body, |digest, now| {
        state.store.commit_restore(&restore_id, digest, now)
    })
}

async fn complete_restore(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(restore_id): AxumPath<String>,
) -> Response {
    if let Err(response) = internal_gate(&state, &headers) {
        return response;
    }
    match state.store.complete_restore(&restore_id, now_millis()) {
        Ok(journal) => journal_response(journal),
        Err(error) => store_error(error),
    }
}

async fn abort_restore(
    State(state): State<AppState>,
    headers: HeaderMap,
    AxumPath(restore_id): AxumPath<String>,
) -> Response {
    if let Err(response) = internal_gate(&state, &headers) {
        return response;
    }
    match state.store.abort_restore(&restore_id, now_millis()) {
        Ok(journal) => journal_response(journal),
        Err(error) => store_error(error),
    }
}

fn restore_with_digest(
    state: &AppState,
    headers: &HeaderMap,
    _restore_id: &str,
    body: &[u8],
    call: impl FnOnce(&str, i64) -> Result<RestoreJournal, StoreError>,
) -> Response {
    if let Err(response) = internal_gate(state, headers) {
        return response;
    }
    let parsed: Value = match serde_json::from_slice(body) {
        Ok(value) => value,
        Err(_) => {
            return store_error(StoreError::Message(
                "transactionDigest is required".to_string(),
            ));
        }
    };
    let Some(digest) = parsed.get("transactionDigest").and_then(Value::as_str) else {
        return store_error(StoreError::Message(
            "transactionDigest is required".to_string(),
        ));
    };
    if digest.len() < 8 {
        return store_error(StoreError::Message(
            "transactionDigest is required".to_string(),
        ));
    }
    match call(digest, now_millis()) {
        Ok(journal) => journal_response(journal),
        Err(error) => store_error(error),
    }
}

fn journal_response(journal: RestoreJournal) -> Response {
    json_status(
        StatusCode::OK,
        serde_json::to_value(journal).unwrap_or_else(|_| json!({})),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt;

    fn state() -> AppState {
        AppState {
            store: Arc::new(TaskBackend::memory()),
            config: ServiceConfig {
                instance_id: "test-instance".to_string(),
                workspace_root: PathBuf::from("."),
                allowed_hosts: vec!["localhost".to_string()],
                auth_token: Some("mcp-token".to_string()),
                internal_backup_token: Some("backup-token".to_string()),
                durable_task_state: "redis".to_string(),
                max_output_bytes: 32_768,
                task_timeout_seconds: 60,
                lease_ms: 1_000,
            },
        }
    }

    async fn call(
        app: Router,
        method: &str,
        uri: &str,
        token: Option<&str>,
        body: Option<&str>,
    ) -> (StatusCode, Value) {
        let mut builder = Request::builder()
            .method(method)
            .uri(uri)
            .header(header::HOST, "localhost");
        if let Some(token) = token {
            builder = builder.header(header::AUTHORIZATION, format!("Bearer {token}"));
        }
        let request = builder
            .body(Body::from(body.unwrap_or("").to_string()))
            .unwrap();
        let response = app.oneshot(request).await.unwrap();
        let status = response.status();
        let bytes = axum::body::to_bytes(response.into_body(), 1_000_000)
            .await
            .unwrap();
        let value = serde_json::from_slice(&bytes)
            .unwrap_or(json!({"raw": String::from_utf8_lossy(&bytes).to_string()}));
        (status, value)
    }

    #[tokio::test]
    async fn mcp_initialize_lists_tools_and_deduplicates_tasks() {
        let app = router(state());
        let (status, init) = call(
            app.clone(),
            "POST",
            "/mcp",
            Some("mcp-token"),
            Some(r#"{"jsonrpc":"2.0","id":"init-1","method":"initialize","params":{}}"#),
        )
        .await;
        assert_eq!(status, StatusCode::OK);
        assert_eq!(
            init["result"]["serverInfo"]["name"],
            "deepseek-infra-stateless-mcp"
        );
        assert_eq!(init["result"]["protocolVersion"], "2025-06-18");
        let (_, listed) = call(
            app.clone(),
            "POST",
            "/mcp",
            Some("mcp-token"),
            Some(r#"{"jsonrpc":"2.0","id":2,"method":"tools/list"}"#),
        )
        .await;
        let names: Vec<&str> = listed["result"]["tools"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|tool| tool.get("name").and_then(Value::as_str))
            .collect();
        assert_eq!(
            names,
            vec![
                "server_info",
                "code_search",
                "start_test_run",
                "get_task",
                "query_logs"
            ]
        );
        let info_body = r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"server_info","arguments":{}}}"#;
        let (_, info) = call(
            app.clone(),
            "POST",
            "/mcp",
            Some("mcp-token"),
            Some(info_body),
        )
        .await;
        let text = info["result"]["content"][0]["text"].as_str().unwrap();
        let parsed: Value = serde_json::from_str(text).unwrap();
        assert_eq!(parsed["instanceId"], "test-instance");
        assert_eq!(parsed["clientSessionState"], "none");
        assert_eq!(parsed["durableTaskState"], "redis");
        let start = r#"{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"start_test_run","arguments":{"idempotencyKey":"unit-request-123","target":"tests/test_mcp.py","timeoutSeconds":30}}}"#;
        let (_, first) = call(app.clone(), "POST", "/mcp", Some("mcp-token"), Some(start)).await;
        let (_, replay) = call(app, "POST", "/mcp", Some("mcp-token"), Some(start)).await;
        let first_body: Value =
            serde_json::from_str(first["result"]["content"][0]["text"].as_str().unwrap()).unwrap();
        let replay_body: Value =
            serde_json::from_str(replay["result"]["content"][0]["text"].as_str().unwrap()).unwrap();
        assert_eq!(first_body["deduplicated"], false);
        assert_eq!(replay_body["deduplicated"], true);
        assert_eq!(first_body["taskId"], replay_body["taskId"]);
    }

    #[tokio::test]
    async fn internal_backup_requires_its_own_token() {
        let app = router(state());
        let (missing, _) = call(
            app.clone(),
            "GET",
            "/internal/backups/capabilities",
            None,
            None,
        )
        .await;
        assert_eq!(missing, StatusCode::UNAUTHORIZED);
        let (ok, body) = call(
            app,
            "GET",
            "/internal/backups/capabilities",
            Some("backup-token"),
            None,
        )
        .await;
        assert_eq!(ok, StatusCode::OK);
        assert_eq!(body["contributorId"], "stateless-mcp");
        assert_eq!(body["available"], true);
    }

    #[test]
    fn workspace_path_rejects_parent_escape() {
        let error = resolve_workspace_path(Path::new("D:/workspace"), "../outside").unwrap_err();
        assert_eq!(error, "path must stay within the configured workspace");
    }
}
