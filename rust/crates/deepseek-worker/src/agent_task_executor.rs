//! Task text comes from Go's durable intent; result bodies stay in Rust custody.
use std::time::{Duration, Instant};

use deepseek_policy::multi_agent;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentNodeEffectMetadata, ExecuteAgentTaskRequest,
};
use serde_json::{Value, json};
use tonic::{Request, Response, Status};

use crate::{AgentNodeEffectStart, BeginAgentNodeEffect, WorkerRpcService, authority_request};

fn denied() -> Status {
    Status::failed_precondition("AGENT_TASK_EXECUTION_DENIED")
}

fn valid_request(input: &ExecuteAgentTaskRequest, synthesis: bool) -> bool {
    let Some(claim) = input.claim.as_ref() else {
        return false;
    };
    let Some(fence) = input.fence.as_ref() else {
        return false;
    };
    input.schema_version == 1
        && claim.phase == if synthesis { 3 } else { 2 }
        && !claim.run_id.is_empty()
        && claim.fence.as_ref() == Some(fence)
        && !fence.action_id.is_empty()
        && fence.execution_epoch > 0
        && !input.operation_id.is_empty()
        && input.operation_id.len() <= 256
        && input.actor == "rust-agent-worker"
        && !input.signed_phase_grant.is_empty()
        && input.signed_phase_grant.len() <= 16 * 1024
        && if synthesis {
            input.node_id == "synthesizer"
        } else {
            multi_agent::profile(&input.node_id).is_some()
        }
}

fn task_model(role: &str) -> &'static str {
    match std::env::var(format!("AGENT_MODEL_{}", role.to_ascii_uppercase())) {
        Ok(model)
            if matches!(
                model.trim().to_ascii_lowercase().as_str(),
                "flash" | "deepseek-v4-flash"
            ) =>
        {
            "deepseek-v4-flash"
        }
        _ => "deepseek-v4-pro",
    }
}

fn task_envelope(
    scope: (&str, &str, &str),
    message: Value,
    usage: Value,
    calls: Vec<Value>,
    response_sha256: &str,
    latency_ms: u64,
) -> Result<Value, Status> {
    let (run_id, node, task) = scope;
    if multi_agent::profile(node).is_none() || task.trim().is_empty() {
        return Err(denied());
    }
    if !calls.is_empty() {
        // Tool arguments and provider reasoning stay in the private journal.
        // This event is not a task result and receives no completion receipt.
        return Ok(json!({"runId":run_id,
            "publicEvent":{"type":"agent_tool_request","phase":node,"toolCount":calls.len()},
            "providerResponseSha256":response_sha256,
            "toolContinuation":{"assistant":message,"toolCalls":calls}}));
    }
    let content = message.get("content").and_then(Value::as_str).unwrap_or("");
    if content.trim().is_empty() {
        return Err(Status::failed_precondition("AGENT_TASK_RESULT_UNKNOWN"));
    }
    let mut output = multi_agent::worker_output(node, task, content, usage, &Value::Null);
    output["latencyMs"] = json!(latency_ms);
    Ok(
        json!({"runId":run_id,"publicEvent":{"type":"agent_output","phase":node,"output":output},
        "providerResponseSha256":response_sha256}),
    )
}

fn synthesis_envelope(
    run_id: &str,
    message: Value,
    usage: Value,
    calls: Vec<Value>,
    response_sha256: &str,
    latency_ms: u64,
) -> Result<Value, Status> {
    let content = message
        .get("content")
        .and_then(Value::as_str)
        .filter(|text| !text.trim().is_empty())
        .ok_or_else(denied)?;
    if !calls.is_empty() || latency_ms > i64::MAX as u64 {
        return Err(denied());
    }
    Ok(
        json!({"runId":run_id, "publicEvent":{"type":"content","text":content},
        "synthesisOutput":{"usage":usage,"latencyMs":latency_ms},"providerResponseSha256":response_sha256}),
    )
}

impl WorkerRpcService {
    pub(crate) async fn execute_agent_synthesis(
        &self,
        request: Request<ExecuteAgentTaskRequest>,
    ) -> Result<Response<AgentNodeEffectMetadata>, Status> {
        self.execute_agent_output(request, true).await
    }

    pub(crate) async fn execute_agent_task(
        &self,
        request: Request<ExecuteAgentTaskRequest>,
    ) -> Result<Response<AgentNodeEffectMetadata>, Status> {
        self.execute_agent_output(request, false).await
    }

    async fn execute_agent_output(
        &self,
        request: Request<ExecuteAgentTaskRequest>,
        synthesis: bool,
    ) -> Result<Response<AgentNodeEffectMetadata>, Status> {
        let identity = self
            .authenticator
            .authenticate(request.metadata())
            .map_err(|error| Status::unauthenticated(error.code()))?;
        if identity.service_name != "go-control-plane" || identity.role != "controller" {
            return Err(Status::permission_denied("AGENT_NODE_CALLER_DENIED"));
        }
        let metadata = request.metadata().clone();
        let input = request.into_inner();
        if !valid_request(&input, synthesis) {
            return Err(Status::invalid_argument("AGENT_TASK_REQUEST_INVALID"));
        }
        if self.control_signer.is_none() {
            return Err(denied());
        }
        let executor = self.agent_plan_executor.as_ref().ok_or_else(denied)?;
        let claim = input.claim.as_ref().ok_or_else(denied)?;
        {
            let worker = self.worker.lock().await;
            let config = worker.agent_node_authority()?;
            let now = authority_request::utc_z_now().map_err(|_| denied())?;
            crate::verify_agent_execution_grant(
                &input.signed_phase_grant,
                &crate::AgentExecutionGrantContext {
                    now: &now,
                    authority: &config,
                    claim,
                },
            )?;
            worker
                .authority_store
                .as_ref()
                .ok_or_else(denied)?
                .admit(claim.fence.as_ref().ok_or_else(denied)?)
                .map_err(|_| denied())?;
        }
        let mut begin = Request::new(BeginAgentNodeEffect {
            claim: claim.clone(),
            signed_phase_grant: input.signed_phase_grant,
            operation_id: input.operation_id,
            actor: input.actor,
            node_id: input.node_id,
        });
        *begin.metadata_mut() = metadata;
        let permit = match self.begin_agent_node_effect(begin).await? {
            AgentNodeEffectStart::Confirmed(outcome) => {
                return self
                    .publish_attested_node(executor, outcome, &claim.run_id)
                    .await
                    .map(Response::new);
            }
            AgentNodeEffectStart::EffectUnknown => {
                return Err(Status::failed_precondition("EFFECT_UNKNOWN"));
            }
            AgentNodeEffectStart::Started(permit) => permit,
        };
        let sent = async {
            let outputs = self
                .worker
                .lock()
                .await
                .authority_store
                .as_ref()
                .ok_or_else(denied)?
                .agent_task_prior_outputs(&permit)?;
            let intent = &permit.intent;
            let role = intent.node_id.clone();
            let task = intent.task.clone();
            let model = task_model(&role);
            let body = executor
                .prepare_payload(
                    intent.request.as_ref().ok_or_else(denied)?,
                    move |payload| {
                        if synthesis {
                            let query = payload["messages"]
                                .as_array()
                                .and_then(|messages| {
                                    messages
                                        .iter()
                                        .rev()
                                        .find(|message| message["role"] == "user")
                                })
                                .and_then(|message| message["content"].as_str())
                                .filter(|text| !text.trim().is_empty())
                                .ok_or_else(denied)?;
                            Ok(multi_agent::synthesis_payload(
                                payload, model, query, &outputs,
                            ))
                        } else {
                            multi_agent::worker_payload(payload, &role, &task, &outputs, model)
                                .ok_or_else(denied)
                        }
                    },
                )
                .await?;
            let deadline = self.node_send_lease(&permit).await?;
            let now = authority_request::parse_utc_z_str(
                &authority_request::utc_z_now().map_err(|_| denied())?,
            )
            .map_err(|_| denied())?;
            if deadline <= now {
                return Err(Status::failed_precondition("AGENT_EXECUTION_LEASE_DENIED"));
            }
            let mut config = executor.upstream.clone();
            config.timeout = config
                .timeout
                .min(Duration::from_secs((deadline - now) as u64));
            let started = Instant::now();
            let (turn, receipt) = deepseek_gateway::chat_execution::exchange_fenced_turn(
                &config,
                &body,
                &permit.fence,
                permit.operation_id(),
                crate::MAX_AGENT_NODE_OUTCOME_BYTES / 2,
            )
            .await
            .map_err(|_| Status::unavailable("AGENT_PROVIDER_RESULT_UNKNOWN"))?;
            let calls = turn.tool_calls();
            match turn.message.get("tool_calls") {
                None | Some(Value::Null) => {}
                Some(Value::Array(raw)) if raw.len() == calls.len() => {}
                _ => return Err(Status::failed_precondition("AGENT_TASK_RESULT_UNKNOWN")),
            }
            let elapsed = started.elapsed().as_millis().min(i64::MAX as u128) as u64;
            let envelope = if synthesis {
                synthesis_envelope(
                    &claim.run_id,
                    turn.message,
                    turn.usage,
                    calls,
                    &receipt,
                    elapsed,
                )?
            } else {
                task_envelope(
                    (&claim.run_id, &intent.node_id, &intent.task),
                    turn.message,
                    turn.usage,
                    calls,
                    &receipt,
                    elapsed,
                )?
            };
            self.confirm_agent_node_effect(&permit, &envelope).await
        }
        .await;
        match sent {
            Ok(outcome) => self
                .publish_attested_node(executor, outcome, &claim.run_id)
                .await
                .map(Response::new),
            Err(error) => {
                let _ = self.mark_agent_node_effect_unknown(&permit).await;
                Err(error)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use deepseek_protocol::ActionFence;
    use deepseek_protocol::generated::deepseek::agent::v1::AgentRunExecutionClaim;

    #[test]
    fn synthesis_rejects_task_claims_tools_empty_content_and_private_reasoning() {
        let fence = ActionFence {
            action_id: "action".into(),
            execution_epoch: 1,
        };
        let input = ExecuteAgentTaskRequest {
            schema_version: 1,
            operation_id: "dispatch".into(),
            actor: "rust-agent-worker".into(),
            signed_phase_grant: vec![1],
            node_id: "synthesizer".into(),
            fence: Some(fence.clone()),
            claim: Some(AgentRunExecutionClaim {
                phase: 3,
                run_id: "run".into(),
                fence: Some(fence),
                ..Default::default()
            }),
        };
        assert!(valid_request(&input, true));
        assert!(!valid_request(&input, false));
        let mut wrong = input.clone();
        wrong.claim.as_mut().unwrap().phase = 2;
        assert!(!valid_request(&wrong, true));
        let mut wrong = input;
        wrong.node_id = "coder".into();
        assert!(!valid_request(&wrong, true));
        let envelope = synthesis_envelope(
            "run",
            json!({"content":"final answer","reasoning_content":"private reasoning"}),
            json!({"prompt_tokens":2,"completion_tokens":3}),
            vec![],
            "digest",
            19,
        )
        .unwrap();
        assert_eq!(
            envelope["publicEvent"],
            json!({"type":"content","text":"final answer"})
        );
        assert!(!envelope.to_string().contains("private reasoning"));
        assert_eq!(envelope["synthesisOutput"]["latencyMs"], 19);
        for message in [json!({"content":" "}), json!({"content":1}), Value::Null] {
            assert!(synthesis_envelope("run", message, Value::Null, vec![], "digest", 0).is_err());
        }
        assert!(
            synthesis_envelope(
                "run",
                json!({"content":"final"}),
                Value::Null,
                vec![json!({"function":{"name":"write_file"}})],
                "digest",
                1
            )
            .is_err()
        );
        assert!(
            synthesis_envelope(
                "run",
                json!({"content":"final"}),
                Value::Null,
                vec![],
                "digest",
                u64::MAX
            )
            .is_err()
        );
    }

    #[test]
    fn task_request_requires_selected_role_and_identical_positive_fence() {
        let fence = ActionFence {
            action_id: "action".into(),
            execution_epoch: 1,
        };
        let input = ExecuteAgentTaskRequest {
            schema_version: 1,
            operation_id: "dispatch".into(),
            actor: "rust-agent-worker".into(),
            signed_phase_grant: vec![1],
            node_id: "coder".into(),
            fence: Some(fence.clone()),
            claim: Some(AgentRunExecutionClaim {
                phase: 2,
                run_id: "run".into(),
                fence: Some(fence),
                ..Default::default()
            }),
        };
        assert!(valid_request(&input, false));
        let mut wrong = input.clone();
        wrong.node_id = "leader".into();
        assert!(!valid_request(&wrong, false));
        let mut wrong = input.clone();
        wrong.claim.as_mut().unwrap().phase = 1;
        assert!(!valid_request(&wrong, false));
        let mut wrong = input.clone();
        wrong.fence.as_mut().unwrap().execution_epoch = 0;
        assert!(!valid_request(&wrong, false));
        let mut wrong = input;
        wrong.actor = "caller".into();
        assert!(!valid_request(&wrong, false));
    }

    #[test]
    fn tool_request_is_retained_without_public_arguments_or_task_completion() {
        let message = json!({"content":"","reasoning_content":"private reasoning"});
        let calls = vec![json!({"function":{"name":"read_file","arguments":"private argument"}})];
        let envelope = task_envelope(
            ("run", "coder", "inspect"),
            message,
            json!({}),
            calls,
            "digest",
            1,
        )
        .unwrap();
        assert_eq!(envelope["publicEvent"]["type"], "agent_tool_request");
        assert_eq!(envelope["publicEvent"]["toolCount"], 1);
        assert!(envelope["publicEvent"].get("output").is_none());
        let public = envelope["publicEvent"].to_string();
        assert!(!public.contains("private"));
        assert_eq!(
            envelope["toolContinuation"]["assistant"]["reasoning_content"],
            "private reasoning"
        );
    }

    #[test]
    fn complete_task_projects_role_and_usage_without_provider_reasoning() {
        let envelope = task_envelope(
            ("run", "coder", "inspect"),
            json!({"content":"## 摘要\nchecked",
            "reasoning_content":"private reasoning"}),
            json!({"total_tokens":9}),
            vec![],
            "digest",
            8,
        )
        .unwrap();
        assert_eq!(envelope["publicEvent"]["type"], "agent_output");
        assert_eq!(envelope["publicEvent"]["output"]["task"], "inspect");
        assert_eq!(envelope["publicEvent"]["output"]["id"], "coder");
        assert!(!envelope.to_string().contains("private reasoning"));
        assert!(
            task_envelope(
                ("run", "coder", "inspect"),
                json!({"content":" "}),
                Value::Null,
                vec![],
                "digest",
                0
            )
            .is_err()
        );
    }
}
