//! Native A2A JSON-RPC mesh: Agent Cards, task store, and message/send.
//!
//! Execution uses the native chat runner or an injected test runner.
//! Task changes also drive `message/stream` and `tasks/resubscribe` SSE.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

use crate::gateway_version;
use deepseek_policy::core_utils::text_or_empty;
use deepseek_policy::entropy::{Entropy, SystemEntropy};
use deepseek_policy::tool_policy::capability_tools;

pub const A2A_PROTOCOL_VERSION: &str = "0.3.0";
const PARSE_ERROR: i64 = -32700;
const INVALID_REQUEST: i64 = -32600;
const METHOD_NOT_FOUND: i64 = -32601;
const INVALID_PARAMS: i64 = -32602;
const TASK_NOT_FOUND: i64 = -32001;
const TASK_NOT_CANCELABLE: i64 = -32002;

const SUBMITTED: &str = "submitted";
const WORKING: &str = "working";
const COMPLETED: &str = "completed";
const FAILED: &str = "failed";
const CANCELING: &str = "canceling";
const CANCELED: &str = "canceled";

const ORCHESTRATOR_ID: &str = "orchestrator";

type TaskRunner = Arc<dyn Fn(&str, &str) -> Result<String, String> + Send + Sync>;

/// One `message/send` waiting for the native chat runner.
#[derive(Debug, Clone)]
pub struct PendingRun {
    pub task_id: String,
    pub agent_id: String,
    pub text: String,
}

fn profiles() -> &'static [(&'static str, &'static str, &'static str)] {
    &[
        (
            ORCHESTRATOR_ID,
            "DeepSeek Infra Orchestrator",
            "You are DeepSeek Infra's general-purpose assistant Agent with the full local tool surface.",
        ),
        (
            "researcher",
            "资料检索 Agent",
            "你负责事实、资料、背景、来源和最新信息核查。",
        ),
        (
            "coder",
            "代码分析 Agent",
            "你负责代码、架构、bug、接口、实现路径和工程风险分析。",
        ),
        (
            "reasoner",
            "逻辑推理 Agent",
            "你负责严谨推理、边界条件、因果关系和方案权衡。",
        ),
        (
            "critic",
            "反驳审查 Agent",
            "你负责挑错、找漏洞、检查遗漏、质疑假设和风险复核。",
        ),
    ]
}

fn profile(agent_id: &str) -> Option<(&'static str, &'static str, &'static str)> {
    profiles()
        .iter()
        .copied()
        .find(|(id, _, _)| *id == agent_id)
}

fn utc_timestamp() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or(0);
    let dt = time_utc(secs);
    format!("{dt}Z")
}

fn time_utc(secs: u64) -> String {
    // YYYY-MM-DDTHH:MM:SS in UTC without an external time crate.
    let days = secs / 86400;
    let rem = secs % 86400;
    let hour = rem / 3600;
    let min = (rem % 3600) / 60;
    let sec = rem % 60;
    let (year, month, day) = civil_from_days(days as i64);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{min:02}:{sec:02}")
}

fn civil_from_days(mut z: i64) -> (i32, u32, u32) {
    z += 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = (z - era * 146097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i32 + era as i32 * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    (y, m as u32, d as u32)
}

fn hex_id(entropy: &dyn Entropy, bytes: usize) -> Result<String, String> {
    let mut out = String::new();
    while out.len() < bytes * 2 {
        out.push_str(&entropy.new_id().map_err(|error| error.message)?);
    }
    Ok(out.chars().take(bytes * 2).collect())
}

fn tasks() -> &'static Mutex<HashMap<String, Value>> {
    static TASKS: OnceLock<Mutex<HashMap<String, Value>>> = OnceLock::new();
    TASKS.get_or_init(|| Mutex::new(HashMap::new()))
}

fn changes() -> &'static tokio::sync::watch::Sender<()> {
    static CHANGES: OnceLock<tokio::sync::watch::Sender<()>> = OnceLock::new();
    CHANGES.get_or_init(|| tokio::sync::watch::channel(()).0)
}

pub(super) fn subscribe() -> tokio::sync::watch::Receiver<()> {
    changes().subscribe()
}

fn runner_slot() -> &'static Mutex<Option<TaskRunner>> {
    static RUNNER: OnceLock<Mutex<Option<TaskRunner>>> = OnceLock::new();
    RUNNER.get_or_init(|| Mutex::new(None))
}

fn pending() -> &'static Mutex<Vec<PendingRun>> {
    static PENDING: OnceLock<Mutex<Vec<PendingRun>>> = OnceLock::new();
    PENDING.get_or_init(|| Mutex::new(Vec::new()))
}

/// Drain `message/send` jobs that should run on the native chat loop.
pub fn take_pending() -> Vec<PendingRun> {
    std::mem::take(&mut *pending().lock().expect("a2a pending"))
}

/// Tests inject a completion function. Production leaves this unset.
pub fn set_task_runner(runner: TaskRunner) {
    *runner_slot().lock().expect("a2a runner") = Some(runner);
}

pub fn reset_a2a_for_tests() {
    tasks().lock().expect("a2a tasks").clear();
    *runner_slot().lock().expect("a2a runner") = None;
    pending().lock().expect("a2a pending").clear();
}

fn status(state: &str, message: &str) -> Value {
    let mut status = json!({"state": state, "timestamp": utc_timestamp()});
    if !message.is_empty() {
        status.as_object_mut().unwrap().insert(
            "message".to_string(),
            agent_text_message(message, &SystemEntropy),
        );
    }
    status
}

fn agent_text_message(text: &str, entropy: &dyn Entropy) -> Value {
    let id = hex_id(entropy, 8).unwrap_or_else(|_| "0".repeat(16));
    json!({
        "role": "agent",
        "parts": [{"kind": "text", "text": text}],
        "messageId": format!("msg_{id}"),
        "kind": "message",
    })
}

fn python_str(value: Option<&Value>) -> String {
    text_or_empty(value)
}

pub(super) fn text_from_message(message: Option<&Value>) -> String {
    let Some(Value::Object(fields)) = message else {
        return String::new();
    };
    let Some(Value::Array(parts)) = fields.get("parts") else {
        return String::new();
    };
    let mut texts = Vec::new();
    for part in parts {
        let kind = python_str(part.get("kind"));
        let kind = if kind.is_empty() {
            python_str(part.get("type"))
        } else {
            kind
        };
        if kind == "text" {
            let text = python_str(part.get("text"));
            if !text.is_empty() {
                texts.push(text);
            }
        }
    }
    texts
        .join("\n")
        .trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
        .to_string()
}

/// Mirrors `agent_card`.
pub fn agent_card(agent_id: &str, base_url: &str) -> Result<Value, String> {
    let resolved = resolve_agent_id(agent_id)?;
    let (_, name, system) = profile(&resolved).expect("known agent");
    let base = {
        let trimmed = base_url.trim().trim_end_matches('/');
        if trimmed.is_empty() {
            "http://127.0.0.1:8000"
        } else {
            trimmed
        }
    };
    let tools: Vec<String> = if resolved == ORCHESTRATOR_ID {
        vec!["full_tool_surface".to_string()]
    } else {
        capability_tools(&resolved)
            .into_iter()
            .map(str::to_string)
            .collect()
    };
    let mut tags = vec![resolved.clone()];
    tags.extend(tools);
    Ok(json!({
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": name,
        "description": system,
        "url": format!("{base}/a2a/agents/{resolved}"),
        "preferredTransport": "JSONRPC",
        "version": gateway_version(),
        "capabilities": {
            "streaming": true,
            "pushNotifications": false,
            "stateTransitionHistory": false,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{
            "id": format!("{resolved}.respond"),
            "name": name,
            "description": system,
            "tags": tags,
        }],
    }))
}

pub fn agent_cards(base_url: &str) -> Vec<Value> {
    profiles()
        .iter()
        .filter_map(|(id, _, _)| agent_card(id, base_url).ok())
        .collect()
}

pub(super) fn resolve_agent_id(value: &str) -> Result<String, String> {
    let agent_id = value.trim();
    let agent_id = if agent_id.is_empty() {
        ORCHESTRATOR_ID
    } else {
        agent_id
    };
    if profile(agent_id).is_none() {
        return Err(format!("Unknown agent: {agent_id}"));
    }
    Ok(agent_id.to_string())
}

pub(super) fn public_task(task: &Value, history_length: Option<i64>) -> Value {
    let mut result = json!({});
    if let Some(object) = task.as_object() {
        let mut out = serde_json::Map::new();
        for (key, value) in object {
            if !key.starts_with('_') {
                out.insert(key.clone(), value.clone());
            }
        }
        if let Some(Value::Array(history)) = out.get("history").cloned() {
            let limit = history_length.unwrap_or(20).max(0) as usize;
            let sliced = if limit == 0 {
                Vec::new()
            } else if history.len() > limit {
                history[history.len() - limit..].to_vec()
            } else {
                history
            };
            out.insert("history".to_string(), Value::Array(sliced));
        }
        result = Value::Object(out);
    }
    result
}

pub(super) fn get_task(task_id: &str) -> Result<Value, i64> {
    let id = task_id.trim();
    let map = tasks().lock().expect("a2a tasks");
    map.get(id).cloned().ok_or(TASK_NOT_FOUND)
}

fn put_task(task: Value) {
    if let Some(id) = task.get("id").and_then(Value::as_str) {
        tasks()
            .lock()
            .expect("a2a tasks")
            .insert(id.to_string(), task);
        changes().send_replace(());
    }
}

pub(super) fn submit_message(params: &Value, agent_id: &str) -> Result<Value, (i64, String)> {
    let resolved = resolve_agent_id(agent_id).map_err(|message| (TASK_NOT_FOUND, message))?;
    let text = text_from_message(params.get("message"));
    if text.is_empty() {
        return Err((
            INVALID_PARAMS,
            "message.parts must contain non-empty text".to_string(),
        ));
    }
    let entropy = SystemEntropy;
    let task_id = format!(
        "task_{}",
        hex_id(&entropy, 12).unwrap_or_else(|_| "0".repeat(24))
    );
    let ctx = python_str(params.get("contextId"));
    let context_id = if ctx.is_empty() {
        python_str(params.get("message").and_then(|m| m.get("contextId")))
    } else {
        ctx
    };
    let context_id = if context_id.is_empty() {
        format!(
            "ctx_{}",
            hex_id(&entropy, 8).unwrap_or_else(|_| "0".repeat(16))
        )
    } else {
        context_id
    };
    let mut incoming = match params.get("message") {
        Some(Value::Object(fields)) => Value::Object(fields.clone()),
        _ => json!({}),
    };
    if incoming
        .get("messageId")
        .and_then(Value::as_str)
        .unwrap_or("")
        .is_empty()
    {
        incoming.as_object_mut().unwrap().insert(
            "messageId".to_string(),
            json!(format!(
                "msg_{}",
                hex_id(&entropy, 8).unwrap_or_else(|_| "0".repeat(16))
            )),
        );
    }
    incoming
        .as_object_mut()
        .unwrap()
        .insert("kind".to_string(), json!("message"));
    incoming
        .as_object_mut()
        .unwrap()
        .insert("taskId".to_string(), json!(task_id.clone()));
    let task = json!({
        "id": task_id,
        "contextId": context_id,
        "kind": "task",
        "agentId": resolved,
        "createdAt": utc_timestamp(),
        "status": status(SUBMITTED, ""),
        "history": [incoming],
        "artifacts": [],
        "artifactChunks": [],
    });
    put_task(task.clone());
    let runner = runner_slot().lock().expect("a2a runner").clone();
    let run_id = task_id.clone();
    let run_agent = resolved.clone();
    if runner.is_some() {
        std::thread::spawn(move || execute_task(&run_id, &run_agent, &text, runner));
    } else {
        pending().lock().expect("a2a pending").push(PendingRun {
            task_id: run_id,
            agent_id: run_agent,
            text,
        });
    }
    get_task(&task_id).map_err(|_| (TASK_NOT_FOUND, "Task not found".to_string()))
}

fn append_chunk(task: &mut Value, artifact: Value, final_chunk: bool) {
    let chunk_index = task["artifactChunks"].as_array().map_or(0, Vec::len);
    let chunk = json!({
        "taskId": task["id"], "contextId": task["contextId"],
        "artifactId": artifact["artifactId"], "artifact": artifact,
        "chunkIndex": chunk_index, "append": true, "final": final_chunk,
        "createdAt": utc_timestamp(),
    });
    task["artifactChunks"]
        .as_array_mut()
        .expect("task chunks")
        .push(chunk);
}

fn new_artifact(name: &str, content: &str) -> Value {
    json!({
        "artifactId": format!("artifact_{}", hex_id(&SystemEntropy, 8).unwrap_or_else(|_| "0".repeat(16))),
        "name": name, "parts": [{"kind": "text", "text": content}],
    })
}

/// Start and cancellation share the task lock, so a queued cancellation cannot
/// be overwritten by WORKING or start an upstream request.
fn mark_working(task_id: &str) -> bool {
    let mut map = tasks().lock().expect("a2a tasks");
    let Some(task) = map.get_mut(task_id) else {
        return false;
    };
    match task["status"]["state"].as_str().unwrap_or("") {
        CANCELING => {
            task["status"] = status(CANCELED, "");
            changes().send_replace(());
            false
        }
        SUBMITTED => {
            task["status"] = status(WORKING, "");
            append_chunk(
                task,
                new_artifact("progress", "A2A worker accepted the task."),
                false,
            );
            changes().send_replace(());
            true
        }
        _ => false,
    }
}

fn finish_task(task_id: &str, result: Result<String, String>) {
    let mut map = tasks().lock().expect("a2a tasks");
    let Some(task) = map.get_mut(task_id) else {
        return;
    };
    if task["status"]["state"] == CANCELING {
        task["status"] = status(CANCELED, "");
        changes().send_replace(());
        return;
    }
    let state = task["status"]["state"].as_str().unwrap_or("").to_string();
    if matches!(state.as_str(), "completed" | "failed" | "canceled") {
        return;
    }
    match result {
        Ok(content) => {
            let artifact = new_artifact("answer", &content);
            append_chunk(task, artifact.clone(), true);
            task["artifacts"] = json!([artifact]);
            if let Some(Value::Array(history)) = task.get_mut("history") {
                history.push(agent_text_message(&content, &SystemEntropy));
            }
            task.as_object_mut()
                .unwrap()
                .insert("status".to_string(), status(COMPLETED, ""));
        }
        Err(message) => {
            task.as_object_mut()
                .unwrap()
                .insert("status".to_string(), status(FAILED, &message));
        }
    }
    changes().send_replace(());
}

fn execute_task(task_id: &str, agent_id: &str, text: &str, runner: Option<TaskRunner>) {
    if !mark_working(task_id) {
        return;
    }
    let result = match runner {
        Some(runner) => runner(agent_id, text),
        None => Err("native A2A task runner is not attached".to_string()),
    };
    finish_task(task_id, result);
}

/// Complete a queued `message/send` on the native chat loop.
pub async fn execute_native(run: PendingRun) {
    if !mark_working(&run.task_id) {
        return;
    }
    let result = crate::a2a_runner::run_native_a2a(&run.agent_id, &run.text).await;
    finish_task(&run.task_id, result);
}

fn cancel_task(task_id: &str) -> Result<Value, (i64, String)> {
    let mut map = tasks().lock().expect("a2a tasks");
    let task = map
        .get_mut(task_id)
        .ok_or((TASK_NOT_FOUND, "Task not found".to_string()))?;
    let state = task["status"]["state"].as_str().unwrap_or("");
    if matches!(state, "completed" | "failed" | "canceled") {
        return Err((TASK_NOT_CANCELABLE, format!("Task is already {state}")));
    }
    task.as_object_mut()
        .unwrap()
        .insert("cancelRequestedAt".to_string(), json!(utc_timestamp()));
    task.as_object_mut().unwrap().insert(
        "status".to_string(),
        status(
            CANCELING,
            "Cancellation requested; pending upstream boundary.",
        ),
    );
    changes().send_replace(());
    Ok(task.clone())
}

fn list_tasks(limit: i64) -> Vec<Value> {
    let capped = limit.clamp(1, 200) as usize;
    let map = tasks().lock().expect("a2a tasks");
    let mut items: Vec<Value> = map.values().cloned().collect();
    items.sort_by_key(|item| std::cmp::Reverse(python_str(item.get("createdAt"))));
    items
        .into_iter()
        .take(capped)
        .map(|task| public_task(&task, None))
        .collect()
}

fn rpc_result(id: &Value, result: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": result})
}

fn rpc_error(id: &Value, code: i64, message: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
}

/// Dispatch one A2A JSON-RPC message.
pub fn handle_a2a_message(message: &Value, agent_id: &str, base_url: &str) -> Value {
    let Value::Object(_) = message else {
        return rpc_error(
            &Value::Null,
            INVALID_REQUEST,
            "Request must be a JSON object",
        );
    };
    let id = message.get("id").cloned().unwrap_or(Value::Null);
    if message.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
        return rpc_error(&id, INVALID_REQUEST, "jsonrpc must be '2.0'");
    }
    let method = python_str(message.get("method"));
    let params = match message.get("params") {
        Some(Value::Object(_)) => message["params"].clone(),
        _ => json!({}),
    };
    match method.as_str() {
        "message/send" => match submit_message(&params, agent_id) {
            Ok(task) => rpc_result(&id, public_task(&task, None)),
            Err((code, message)) => rpc_error(&id, code, &message),
        },
        "tasks/get" => {
            let task_id = python_str(params.get("id")).trim().to_string();
            if task_id.is_empty() {
                return rpc_error(&id, INVALID_PARAMS, "id is required");
            }
            let length = params.get("historyLength").and_then(Value::as_i64);
            match get_task(&task_id) {
                Ok(task) => rpc_result(&id, public_task(&task, length)),
                Err(code) => rpc_error(&id, code, "Task not found"),
            }
        }
        "tasks/cancel" => {
            let task_id = python_str(params.get("id")).trim().to_string();
            if task_id.is_empty() {
                return rpc_error(&id, INVALID_PARAMS, "id is required");
            }
            match cancel_task(&task_id) {
                Ok(task) => rpc_result(&id, public_task(&task, None)),
                Err((code, message)) => rpc_error(&id, code, &message),
            }
        }
        "tasks/list" => {
            let limit = params
                .get("limit")
                .and_then(Value::as_i64)
                .filter(|n| *n != 0)
                .unwrap_or(20);
            rpc_result(&id, json!({"tasks": list_tasks(limit)}))
        }
        "agent/getAuthenticatedExtendedCard" => match agent_card(agent_id, base_url) {
            Ok(card) => rpc_result(&id, card),
            Err(message) => rpc_error(&id, TASK_NOT_FOUND, &message),
        },
        _ => rpc_error(
            &id,
            METHOD_NOT_FOUND,
            &format!("Method not found: {method}"),
        ),
    }
}

pub fn handle_a2a_bytes(body: &[u8], agent_id: &str, base_url: &str) -> Value {
    if body.is_empty() {
        return rpc_error(&Value::Null, INVALID_REQUEST, "Invalid Request: empty body");
    }
    match serde_json::from_slice::<Value>(body) {
        Ok(value) => handle_a2a_message(&value, agent_id, base_url),
        Err(_) => rpc_error(&Value::Null, PARSE_ERROR, "Parse error"),
    }
}

pub fn a2a_enabled() -> bool {
    match std::env::var("A2A_ENABLED") {
        Ok(raw) if !raw.trim().is_empty() => matches!(
            raw.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        _ => true,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    static TEST_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn message_coercion_matches_python_oracle() {
        let cases: Vec<Value> =
            serde_json::from_str(include_str!("../tests/fixtures/a2a_message_oracle.json"))
                .unwrap();
        for case in cases {
            assert_eq!(
                text_from_message(case.get("message")),
                case["text"].as_str().unwrap(),
                "{}",
                case["message"]
            );
        }
    }

    fn wait_completed(task_id: &str) -> Value {
        for _ in 0..50 {
            if let Ok(task) = get_task(task_id) {
                let state = task["status"]["state"].as_str().unwrap_or("");
                if matches!(state, "completed" | "failed" | "canceled") {
                    return task;
                }
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        panic!("task {task_id} did not finish");
    }

    #[test]
    fn agent_cards_cover_orchestrator_and_workers() {
        let cards = agent_cards("http://127.0.0.1:8000");
        let names: Vec<&str> = cards
            .iter()
            .filter_map(|card| card["url"].as_str()?.rsplit('/').next())
            .collect();
        assert_eq!(
            names,
            vec!["orchestrator", "researcher", "coder", "reasoner", "critic"]
        );
        let researcher = agent_card("researcher", "http://127.0.0.1:8000").unwrap();
        let tags = researcher["skills"][0]["tags"].as_array().unwrap();
        assert!(tags.iter().any(|tag| tag == "web_search"));
        assert!(agent_card("nope", "").is_err());
    }

    #[test]
    fn message_send_runs_injected_runner() {
        let _guard = TEST_LOCK.lock().unwrap();
        reset_a2a_for_tests();
        set_task_runner(Arc::new(|_agent, text| Ok(format!("echo:{text}"))));
        let response = handle_a2a_message(
            &json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "message/send",
                "params": {"message": {"parts": [{"kind": "text", "text": "hello"}]}}
            }),
            "reasoner",
            "http://127.0.0.1:8000",
        );
        let task_id = response["result"]["id"].as_str().unwrap().to_string();
        assert_eq!(response["result"]["agentId"], "reasoner");
        let done = wait_completed(&task_id);
        assert_eq!(done["status"]["state"], "completed");
        assert_eq!(done["artifacts"][0]["parts"][0]["text"], "echo:hello");
    }

    #[test]
    fn empty_message_is_invalid_params() {
        let _guard = TEST_LOCK.lock().unwrap();
        reset_a2a_for_tests();
        let response = handle_a2a_message(
            &json!({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "message/send",
                "params": {"message": {"parts": []}}
            }),
            "reasoner",
            "",
        );
        assert_eq!(response["error"]["code"], INVALID_PARAMS);
    }

    #[test]
    fn message_send_without_runner_queues_native_work() {
        let _guard = TEST_LOCK.lock().unwrap();
        reset_a2a_for_tests();
        let response = handle_a2a_message(
            &json!({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "message/send",
                "params": {"message": {"parts": [{"kind": "text", "text": "queue me"}]}}
            }),
            "reasoner",
            "",
        );
        assert_eq!(response["result"]["status"]["state"], "submitted");
        let pending = take_pending();
        assert_eq!(pending.len(), 1);
        assert_eq!(pending[0].agent_id, "reasoner");
        assert_eq!(pending[0].text, "queue me");
    }

    #[test]
    fn queued_cancellation_skips_execution_and_terminal_results_cannot_be_replaced() {
        let _guard = TEST_LOCK.lock().unwrap();
        reset_a2a_for_tests();
        let task = submit_message(
            &json!({"message":{"parts":[{"kind":"text","text":"cancel"}]}}),
            "reasoner",
        )
        .unwrap();
        let id = task["id"].as_str().unwrap();
        cancel_task(id).unwrap();
        assert!(!mark_working(id));
        finish_task(id, Ok("must not publish".into()));
        let canceled = get_task(id).unwrap();
        assert_eq!(canceled["status"]["state"], "canceled");
        assert_eq!(canceled["artifactChunks"], json!([]));
        assert_eq!(cancel_task(id).unwrap_err().0, TASK_NOT_CANCELABLE);
        assert!(take_pending().len() == 1);
    }

    #[test]
    fn terminal_streams_match_python_oracle_for_all_resume_cursors() {
        let _guard = TEST_LOCK.lock().unwrap();
        reset_a2a_for_tests();
        let cases: Vec<Value> =
            serde_json::from_str(include_str!("../tests/fixtures/a2a_stream_oracle.json")).unwrap();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        for case in cases {
            put_task(case["task"].clone());
            let response = crate::a2a_stream::streaming_response(
                &json!({
                    "jsonrpc":"2.0", "id":"oracle", "method":"tasks/resubscribe",
                    "params":{"id":case["task"]["id"], "afterChunkIndex":case["cursor"]},
                }),
                "reasoner",
            );
            let bytes = runtime
                .block_on(axum::body::to_bytes(response.into_body(), 100_000))
                .unwrap();
            let frames: Vec<Value> = std::str::from_utf8(&bytes)
                .unwrap()
                .split("\n\n")
                .filter(|frame| !frame.is_empty())
                .map(|frame| serde_json::from_str(frame.strip_prefix("data: ").unwrap()).unwrap())
                .collect();
            assert_eq!(json!(frames), case["events"], "{}", case["name"]);
        }
    }
}
