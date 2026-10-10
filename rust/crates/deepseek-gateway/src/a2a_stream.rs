//! A2A task snapshots, resumable artifact chunks, and terminal status over SSE.
//! Shared framing/cursor logic for the local qualification hub and the Go-backed
//! durable controller. The local streaming_response helper keeps process-local state.

use axum::{
    body::{Body, Bytes},
    http::header,
    response::{IntoResponse, Response},
};
use deepseek_policy::{
    core_utils::{python_int_opt, python_truthy},
    python_json::dumps_default_separators,
};
use serde_json::{Value, json};

use crate::a2a_hub;

pub fn is_stream_request(message: &Value) -> bool {
    matches!(
        message.get("method").and_then(Value::as_str),
        Some("message/stream" | "tasks/resubscribe")
    )
}

pub(super) fn frame(value: Value) -> Result<Bytes, std::convert::Infallible> {
    Ok(Bytes::from(format!(
        "data: {}\n\n",
        dumps_default_separators(&value)
    )))
}

pub(super) fn result(id: &Value, value: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": value})
}

pub(super) fn error(id: &Value, code: i64, message: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
}

fn artifact_update(task: &Value, chunk: &Value) -> Value {
    json!({
        "taskId": task["id"], "contextId": task["contextId"],
        "artifact": chunk["artifact"], "artifactId": chunk["artifactId"],
        "chunkIndex": chunk["chunkIndex"], "append": chunk["append"],
        "final": chunk["final"], "kind": "artifact-update",
    })
}

pub(super) struct TaskCursor {
    state: String,
    chunk: i64,
}

impl TaskCursor {
    pub(super) fn new(task: &Value, after: i64) -> Self {
        Self {
            state: task["status"]["state"].as_str().unwrap_or("").to_string(),
            chunk: after,
        }
    }

    pub(super) fn events(&mut self, id: &Value, task: &Value) -> (Vec<Value>, bool) {
        let mut events = Vec::new();
        if let Some(chunks) = task["artifactChunks"].as_array() {
            for chunk in chunks {
                let index = python_int_opt(chunk.get("chunkIndex")).unwrap_or(-1);
                if index > self.chunk {
                    self.chunk = index;
                    events.push(result(id, artifact_update(task, chunk)));
                }
            }
        }
        let state = task["status"]["state"].as_str().unwrap_or("");
        let terminal = matches!(state, "completed" | "failed" | "canceled");
        if state != self.state || terminal {
            self.state = state.to_string();
            events.push(result(
                id,
                json!({"taskId":task["id"], "contextId":task["contextId"],
                "status":task["status"], "kind":"status-update", "final":terminal}),
            ));
        }
        (events, terminal)
    }
}

/// Submission happens before constructing the body so the route can dispatch
/// its native runner even if a client disconnects before reading the first frame.
pub fn streaming_response(message: &Value, agent_id: &str) -> Response {
    let id = message.get("id").cloned().unwrap_or(Value::Null);
    let params = message
        .get("params")
        .filter(|params| params.is_object())
        .cloned()
        .unwrap_or(json!({}));
    let after = if message["method"] == "tasks/resubscribe" {
        python_int_opt(params.get("afterChunkIndex")).unwrap_or(-1)
    } else {
        -1
    };
    // Observe changes before taking the initial task snapshot.
    let mut changes = a2a_hub::subscribe();
    let task = if message["method"] == "tasks/resubscribe" {
        let task_id = params
            .get("id")
            .filter(|value| python_truthy(value))
            .map(|value| {
                value
                    .as_str()
                    .map(str::to_string)
                    .unwrap_or_else(|| value.to_string())
            })
            .unwrap_or_default();
        let task_id = task_id.trim();
        if task_id.is_empty() {
            Err((-32602, "id is required".to_string()))
        } else {
            a2a_hub::get_task(task_id).map_err(|code| (code, "Task not found".to_string()))
        }
    } else {
        a2a_hub::submit_message(&params, agent_id).map_err(|(_, message)| (-32602, message))
    };
    // Subscribe before the first snapshot. A completion between any snapshot
    // and changed().await remains visible; slow consumers cannot lose chunks.
    let stream = async_stream::stream! {
        match task {
            Err((code, message)) => { yield frame(error(&id, code, &message)); }
            Ok(task) => {
                let task_id = task["id"].as_str().expect("task id").to_string();
                let mut cursor = TaskCursor::new(&task, after);
                yield frame(result(&id, a2a_hub::public_task(&task, None)));
                loop {
                    let current = match a2a_hub::get_task(&task_id) {
                        Ok(task) => task,
                        Err(code) => {
                            yield frame(error(&id, code, "Task not found"));
                            break;
                        }
                    };
                    let (events, terminal) = cursor.events(&id, &current);
                    for event in events { yield frame(event); }
                    if terminal { break; }
                    if changes.changed().await.is_err() { break; }
                }
            }
        }
    };
    response_body(Body::from_stream(stream))
}

pub(super) fn response_body(body: Body) -> Response {
    (
        [
            (header::CONTENT_TYPE, "text/event-stream; charset=utf-8"),
            (header::CACHE_CONTROL, "no-cache"),
            (header::HeaderName::from_static("x-accel-buffering"), "no"),
        ],
        body,
    )
        .into_response()
}
