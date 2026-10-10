//! A2A lifecycle bridge to the Go-owned durable task controller over mTLS gRPC.
//! No task database or execution epoch is written by the Rust edge.

use crate::{
    a2a_hub, a2a_runner,
    a2a_stream::{self, TaskCursor, error, frame, response_body, result},
};
use axum::{
    Json,
    body::Body,
    response::{IntoResponse, Response},
};
use deepseek_policy::{
    core_utils::{python_int_opt, python_truthy, text_or_empty},
    entropy::{Entropy, SystemEntropy},
};
use deepseek_protocol::generated::deepseek::{
    agent::v1::{
        A2aFinishRequest, A2aListFilter, A2aSubmitRequest, A2aTaskRef, A2aTaskSnapshot,
        a2a_task_control_client::A2aTaskControlClient,
    },
    common::v1::ActionFence,
};
use serde_json::{Value, json};
use std::time::Duration;
use tonic::{
    Code, Status,
    transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity},
};

type Client = A2aTaskControlClient<Channel>;
const ENV: [&str; 5] = [
    "DEEPSEEK_A2A_CONTROL_URL",
    "DEEPSEEK_A2A_TLS_CA",
    "DEEPSEEK_A2A_TLS_SERVER_NAME",
    "DEEPSEEK_A2A_TLS_CERT",
    "DEEPSEEK_A2A_TLS_KEY",
];

pub fn configured() -> bool {
    ENV.iter()
        .any(|name| std::env::var(name).is_ok_and(|s| !s.trim().is_empty()))
}

async fn connect() -> Result<Client, Status> {
    let fields: Vec<String> = ENV
        .iter()
        .map(|name| std::env::var(name).unwrap_or_default().trim().to_string())
        .collect();
    if fields.iter().any(String::is_empty) || !fields[0].starts_with("https://") {
        return Err(Status::unavailable(
            "Native A2A control requires complete mTLS configuration",
        ));
    }
    let unavailable = || Status::unavailable("Native A2A control unavailable");
    let ca = tokio::fs::read(&fields[1])
        .await
        .map_err(|_| unavailable())?;
    let cert = tokio::fs::read(&fields[3])
        .await
        .map_err(|_| unavailable())?;
    let key = tokio::fs::read(&fields[4])
        .await
        .map_err(|_| unavailable())?;
    let tls = ClientTlsConfig::new()
        .domain_name(&fields[2])
        .ca_certificate(Certificate::from_pem(ca))
        .identity(Identity::from_pem(cert, key));
    let endpoint = Endpoint::from_shared(fields[0].clone())
        .map_err(|_| unavailable())?
        .connect_timeout(Duration::from_secs(3))
        .timeout(Duration::from_secs(5))
        .tls_config(tls)
        .map_err(|_| unavailable())?;
    let channel = endpoint.connect().await.map_err(|_| unavailable())?;
    Ok(Client::new(channel).max_decoding_message_size(2 << 20))
}

fn public_task(snapshot: A2aTaskSnapshot) -> Result<Value, Status> {
    let task: Value = serde_json::from_slice(&snapshot.public_task_json)
        .map_err(|_| Status::data_loss("Invalid A2A task snapshot"))?;
    if task["id"] != snapshot.task_id
        || task["status"]["state"] != snapshot.state
        || !task["artifactChunks"].is_array()
        || !task["history"].is_array()
    {
        return Err(Status::data_loss("Invalid A2A task snapshot"));
    }
    Ok(task)
}

fn rpc_error(id: &Value, status: Status) -> Value {
    let code = match status.code() {
        Code::NotFound => -32001,
        Code::InvalidArgument => -32602,
        Code::FailedPrecondition => -32002,
        _ => -32603,
    };
    error(id, code, status.message())
}

fn error_response(value: Value, stream: bool) -> Response {
    if stream {
        response_body(Body::from_stream(futures_util::stream::once(async move {
            frame(value)
        })))
    } else {
        Json(value).into_response()
    }
}

fn string_value(value: Option<&Value>) -> String {
    text_or_empty(value)
}

pub async fn handle_bytes(body: &[u8], agent_id: &str) -> Response {
    let message: Value = match serde_json::from_slice(body) {
        Ok(Value::Object(fields)) => Value::Object(fields),
        _ => {
            return Json(a2a_hub::handle_a2a_bytes(
                body,
                agent_id,
                "http://127.0.0.1:8000",
            ))
            .into_response();
        }
    };
    let method = message["method"].as_str().unwrap_or("");
    let stream = a2a_stream::is_stream_request(&message);
    if !stream && message["jsonrpc"] != "2.0" {
        return Json(a2a_hub::handle_a2a_message(
            &message,
            agent_id,
            "http://127.0.0.1:8000",
        ))
        .into_response();
    }
    if !matches!(
        method,
        "message/send"
            | "message/stream"
            | "tasks/get"
            | "tasks/cancel"
            | "tasks/list"
            | "tasks/resubscribe"
    ) {
        return Json(a2a_hub::handle_a2a_message(
            &message,
            agent_id,
            "http://127.0.0.1:8000",
        ))
        .into_response();
    }
    let id = message.get("id").cloned().unwrap_or(Value::Null);
    let mut client = match connect().await {
        Ok(client) => client,
        Err(status) => return error_response(rpc_error(&id, status), stream),
    };
    let params = message.get("params").cloned().unwrap_or(json!({}));
    if matches!(method, "message/send" | "message/stream") {
        let agent = match a2a_hub::resolve_agent_id(agent_id) {
            Ok(agent) => agent,
            Err(reason) => {
                return error_response(
                    error(&id, if stream { -32602 } else { -32001 }, &reason),
                    stream,
                );
            }
        };
        let text = a2a_hub::text_from_message(params.get("message"));
        if text.is_empty() {
            return error_response(
                error(&id, -32602, "message.parts must contain non-empty text"),
                stream,
            );
        }
        let nonce = match SystemEntropy.new_file_id() {
            Ok(nonce) => nonce,
            Err(_) => {
                return error_response(
                    error(&id, -32603, "Task identity generation failed"),
                    stream,
                );
            }
        };
        let proposal = A2aSubmitRequest {
            agent_id: agent.clone(),
            context_id: string_value(
                params
                    .get("contextId")
                    .filter(|v| python_truthy(v))
                    .or_else(|| params["message"].get("contextId")),
            ),
            message_json: serde_json::to_vec(&params["message"]).expect("JSON value"),
            fence: Some(ActionFence {
                action_id: format!("task_{}", nonce.chars().take(24).collect::<String>()),
                execution_epoch: 0,
            }),
        };
        let task = match client
            .submit(proposal)
            .await
            .and_then(|response| public_task(response.into_inner()))
        {
            Ok(task) => task,
            Err(status) => return error_response(rpc_error(&id, status), stream),
        };
        let task_id = task["id"].as_str().expect("validated task id").to_string();
        let execution_client = client.clone();
        tokio::spawn(async move {
            execute(execution_client, task_id, agent, text).await;
        });
        if stream {
            stream_task(client, id, task, -1)
        } else {
            Json(result(&id, task)).into_response()
        }
    } else if method == "tasks/list" {
        let limit = python_int_opt(params.get("limit"))
            .filter(|n| *n != 0)
            .unwrap_or(20)
            .clamp(1, 200) as i32;
        match client.list(A2aListFilter { limit }).await {
            Ok(reply) => match reply
                .into_inner()
                .tasks
                .into_iter()
                .map(public_task)
                .collect::<Result<Vec<_>, _>>()
            {
                Ok(tasks) => Json(result(&id, json!({"tasks":tasks}))).into_response(),
                Err(status) => error_response(rpc_error(&id, status), false),
            },
            Err(status) => error_response(rpc_error(&id, status), false),
        }
    } else {
        let task_id = string_value(params.get("id")).trim().to_string();
        if task_id.is_empty() {
            return error_response(error(&id, -32602, "id is required"), stream);
        }
        let reference = A2aTaskRef { task_id };
        let reply = if method == "tasks/cancel" {
            client.cancel(reference).await
        } else {
            client.get(reference).await
        };
        match reply.and_then(|response| public_task(response.into_inner())) {
            Ok(task) if stream => stream_task(
                client,
                id,
                task,
                python_int_opt(params.get("afterChunkIndex")).unwrap_or(-1),
            ),
            Ok(task) => Json(result(
                &id,
                a2a_hub::public_task(&task, params.get("historyLength").and_then(Value::as_i64)),
            ))
            .into_response(),
            Err(status) => error_response(rpc_error(&id, status), stream),
        }
    }
}

async fn execute(mut client: Client, task_id: String, agent: String, text: String) {
    let claim = match client.claim(A2aTaskRef { task_id }).await {
        Ok(reply) => reply.into_inner(),
        Err(_) => return,
    };
    let Some(execution) = claim.execution else {
        return;
    };
    let run = a2a_runner::run_native_a2a(&agent, &text);
    tokio::pin!(run);
    let mut renew = tokio::time::interval(Duration::from_secs(5));
    renew.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    renew.tick().await;
    let outcome = loop {
        tokio::select! {
            result = &mut run => break result,
            _ = renew.tick() => {
                match client.renew(execution.clone()).await {
                    Ok(reply) if reply.get_ref().state == "canceling" => break Err("canceled".to_string()),
                    Ok(_) => {},
                    // Loss of Go authority cancels further native execution. The
                    // durable lease determines recovery; never resubmit the task.
                    Err(_) => return,
                }
            }
        }
    };
    let (content, failure) = match outcome {
        Ok(text) => (text, String::new()),
        Err(error) => (String::new(), error),
    };
    // If a commit acknowledgement is lost, the public Get/resubscribe path reads
    // the durable record. An uncertain finish never causes another execution.
    let _ = client
        .finish(A2aFinishRequest {
            fence: execution.fence,
            execution_token: execution.execution_token,
            content,
            failure,
        })
        .await;
}

fn stream_task(mut client: Client, id: Value, task: Value, after: i64) -> Response {
    let stream = async_stream::stream! {
        let task_id = task["id"].as_str().expect("validated task id").to_string();
        let mut cursor = TaskCursor::new(&task, after);
        yield frame(result(&id,task));
        loop {
            let current = match client.get(A2aTaskRef { task_id: task_id.clone() }).await.and_then(|reply|public_task(reply.into_inner())) {
                Ok(task)=>task,
                Err(status)=>{ yield frame(rpc_error(&id,status)); break; }
            };
            let (events, terminal) = cursor.events(&id,&current);
            for event in events { yield frame(event); }
            if terminal { break; }
            tokio::time::sleep(Duration::from_millis(250)).await;
        }
    };
    response_body(Body::from_stream(stream))
}
