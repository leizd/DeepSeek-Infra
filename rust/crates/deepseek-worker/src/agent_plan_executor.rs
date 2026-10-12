//! Durable planner execution; Go alone decides the resulting run/phase state.
use std::{path::PathBuf, sync::Arc, time::Duration};

use deepseek_gateway::{
    assembly_env::NativeAssembly, chat_execution::UpstreamConfig,
    request_assembly::build_deepseek_request,
};
use deepseek_policy::{
    agent_run_data::{ArtifactReference, ArtifactStore},
    model_router::ModelRouterSettings,
    multi_agent,
};
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentArtifactReference, AgentNodeEffectMetadata, ExecuteAgentPlanRequest,
    QueryAgentNodeEffectRequest, QueryAgentNodeEffectResponse,
    agent_node_worker_server::AgentNodeWorker,
};
use serde_json::{Value, json};
use tonic::{Request, Response, Status};

use crate::{
    AgentNodeEffectOutcome, AgentNodeEffectPermit, AgentNodeEffectStart, BeginAgentNodeEffect,
    QueryAgentNodeEffect, WorkerRpcService, authority_request,
};

pub struct AgentPlanExecutor {
    pub(crate) artifact_root: PathBuf,
    workspace_root: PathBuf,
    upstream: UpstreamConfig,
    router: ModelRouterSettings,
}

impl std::fmt::Debug for AgentPlanExecutor {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AgentPlanExecutor")
            .field("artifact_root", &self.artifact_root)
            .field("workspace_root", &self.workspace_root)
            .finish_non_exhaustive()
    }
}

fn unavailable() -> Status {
    Status::unavailable("AGENT_PLAN_EXECUTOR_UNAVAILABLE")
}

pub fn load_agent_plan_executor_from_env() -> Result<Option<AgentPlanExecutor>, Status> {
    let artifact_root = match std::env::var_os("DEEPSEEK_AGENT_ARTIFACT_DIR") {
        None => return Ok(None),
        Some(value) if !value.is_empty() => PathBuf::from(value),
        _ => return Err(unavailable()),
    };
    let workspace_root = std::env::var_os(deepseek_gateway::assembly_env::ROOT_ENV)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .ok_or_else(unavailable)?;
    let mode = std::env::var("DEEPSEEK_AGENT_DATA_MODE").map_err(|_| unavailable())?;
    if !mode.eq_ignore_ascii_case("python_disabled")
        || !artifact_root.is_absolute()
        || !workspace_root.is_absolute()
    {
        return Err(unavailable());
    }
    let upstream = UpstreamConfig::from_env();
    upstream.validate().map_err(|_| unavailable())?;
    Ok(Some(AgentPlanExecutor {
        artifact_root,
        workspace_root,
        upstream,
        router: ModelRouterSettings::from_env(),
    }))
}

impl WorkerRpcService {
    async fn publish_attested_plan(
        &self,
        executor: &AgentPlanExecutor,
        outcome: AgentNodeEffectOutcome,
        run_id: &str,
    ) -> Result<AgentNodeEffectMetadata, Status> {
        let query = QueryAgentNodeEffect {
            operation_id: outcome.operation_id.clone(),
            fence: outcome.fence.clone(),
            run_id: run_id.into(),
            node_id: outcome.node_id.clone(),
        };
        let mut metadata = executor.publish_known(outcome, run_id).await?;
        // Lock order matches the existing signer RPC: worker, then custody.
        let worker = self.worker.lock().await;
        let config = worker.agent_node_authority()?;
        let evidence = worker
            .authority_store
            .as_ref()
            .ok_or_else(unavailable)?
            .agent_node_outcome_evidence(
                &query,
                metadata.public_event.clone().ok_or_else(unavailable)?,
                &config,
            )?;
        let receipt = self
            .control_signer
            .as_ref()
            .ok_or_else(unavailable)?
            .lock()
            .await
            .sign_node_outcome(evidence)?;
        metadata.outcome_receipt = Some(receipt);
        Ok(metadata)
    }

    pub fn with_agent_plan_executor(mut self, executor: AgentPlanExecutor) -> Self {
        self.agent_plan_executor = Some(Arc::new(executor));
        self
    }

    async fn node_send_lease(&self, permit: &AgentNodeEffectPermit) -> Result<i64, Status> {
        let now = authority_request::utc_z_now().map_err(|_| unavailable())?;
        let worker = self.worker.lock().await;
        let config = worker.agent_node_authority()?;
        worker
            .authority_store
            .as_ref()
            .ok_or_else(unavailable)?
            .agent_node_send_deadline(permit, &config, &now)
    }
}

impl AgentPlanExecutor {
    async fn prepare(&self, request: &ExecuteAgentPlanRequest) -> Result<Value, Status> {
        let reference = request
            .claim
            .as_ref()
            .and_then(|claim| claim.request.as_ref())
            .ok_or_else(unavailable)?;
        let reference = ArtifactReference {
            sha256: reference.sha256.clone(),
            length: reference.length,
        };
        let root = self.artifact_root.clone();
        let workspace = self.workspace_root.clone();
        let router = self.router.clone();
        let key = self.upstream.api_key.clone();
        tokio::task::spawn_blocking(move || {
            let body = ArtifactStore::open_readonly(&root)
                .and_then(|store| store.get(&reference))
                .map_err(|_| Status::failed_precondition("AGENT_REQUEST_ARTIFACT_DENIED"))?;
            let payload = body
                .get("payload")
                .or_else(|| body.get("requestPayload"))
                .unwrap_or(&body);
            if !payload.is_object() {
                return Err(Status::invalid_argument("AGENT_REQUEST_INVALID"));
            }
            let planner = multi_agent::planner_payload(payload, &router.refine_model);
            let assembly = NativeAssembly::with_root(workspace)
                .with_router_settings(router)
                .with_readonly_budget();
            let (result, index_needed) = assembly.with_env(|environment| {
                let environment = deepseek_gateway::request_assembly::AssemblyEnv {
                    api_key_fallback: &key,
                    expander: environment.expander,
                    router: environment.router,
                    budget: environment.budget,
                    ledger: environment.ledger,
                    taint: environment.taint,
                    context_manager: environment.context_manager,
                    engine: environment.engine,
                    dynamic: environment.dynamic,
                };
                build_deepseek_request(&planner, false, None, None, &environment)
            });
            if index_needed {
                return Err(Status::failed_precondition("AGENT_FILE_INDEX_UNAVAILABLE"));
            }
            result
                .map(|prepared| prepared.body)
                .map_err(|_| Status::invalid_argument("AGENT_REQUEST_INVALID"))
        })
        .await
        .map_err(|_| unavailable())?
    }

    async fn publish_known(
        &self,
        outcome: AgentNodeEffectOutcome,
        run_id: &str,
    ) -> Result<AgentNodeEffectMetadata, Status> {
        let envelope: Value = serde_json::from_slice(&outcome.output).map_err(|_| unavailable())?;
        let event = envelope
            .get("publicEvent")
            .filter(|value| value.is_object())
            .ok_or_else(unavailable)?
            .clone();
        if envelope["runId"] != run_id
            || event["type"] != "agent_plan"
            || outcome.node_id != "leader"
        {
            return Err(Status::data_loss("AGENT_PLAN_OUTCOME_CORRUPT"));
        }
        let root = self.artifact_root.clone();
        let reference = tokio::task::spawn_blocking(move || {
            ArtifactStore::open_native(&root, "python_disabled")
                .and_then(|mut store| store.put(&event))
        })
        .await
        .map_err(|_| unavailable())?
        .map_err(|_| unavailable())?;
        Ok(AgentNodeEffectMetadata {
            operation_id: outcome.operation_id,
            fence: Some(outcome.fence),
            run_id: run_id.into(),
            node_id: outcome.node_id,
            state: "CONFIRMED".into(),
            public_event: Some(AgentArtifactReference {
                sha256: reference.sha256,
                length: reference.length,
            }),
            outcome_sha256: outcome.output_sha256,
            outcome_length: outcome.output.len() as u64,
            outcome_receipt: None,
        })
    }
}

#[tonic::async_trait]
impl AgentNodeWorker for WorkerRpcService {
    async fn project_plan(
        &self,
        request: Request<
            deepseek_protocol::generated::deepseek::agent::v1::ProjectAgentPlanRequest,
        >,
    ) -> Result<
        Response<deepseek_protocol::generated::deepseek::agent::v1::AgentPlanProjectionReceipt>,
        Status,
    > {
        self.project_agent_plan(request).await
    }
    async fn execute_plan(
        &self,
        request: Request<ExecuteAgentPlanRequest>,
    ) -> Result<Response<AgentNodeEffectMetadata>, Status> {
        let identity = self
            .authenticator
            .authenticate(request.metadata())
            .map_err(|error| Status::unauthenticated(error.code()))?;
        if identity.service_name != "go-control-plane" || identity.role != "controller" {
            return Err(Status::permission_denied("AGENT_NODE_CALLER_DENIED"));
        }
        if self.control_signer.is_none() {
            return Err(unavailable());
        }
        let executor = self.agent_plan_executor.as_ref().ok_or_else(unavailable)?;
        let metadata = request.metadata().clone();
        let input = request.into_inner();
        let claim = input
            .claim
            .as_ref()
            .ok_or_else(|| Status::invalid_argument("AGENT_PLAN_REQUEST_INVALID"))?;
        if input.fence.as_ref() != claim.fence.as_ref() || input.fence.is_none() {
            return Err(Status::invalid_argument("FENCE_MISMATCH"));
        }
        if input.schema_version != 1
            || claim.phase != 1
            || input.operation_id.is_empty()
            || input.actor.is_empty()
            || input.signed_phase_grant.is_empty()
            || input.signed_phase_grant.len() > 16 * 1024
        {
            return Err(Status::invalid_argument("AGENT_PLAN_REQUEST_INVALID"));
        }
        {
            let worker = self.worker.lock().await;
            let config = worker.agent_node_authority()?;
            let now = authority_request::utc_z_now().map_err(|_| unavailable())?;
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
                .ok_or_else(unavailable)?
                .admit(claim.fence.as_ref().ok_or_else(unavailable)?)
                .map_err(|_| unavailable())?;
        }
        // Resolve/validate the immutable Rust body before spending the sole Go
        // reservation. Neither the RPC caller nor Go supplies a replacement body.
        let body = executor.prepare(&input).await?;
        let mut begin = Request::new(BeginAgentNodeEffect {
            claim: claim.clone(),
            signed_phase_grant: input.signed_phase_grant,
            operation_id: input.operation_id,
            actor: input.actor,
            node_id: "leader".into(),
        });
        *begin.metadata_mut() = metadata;
        let result = self.begin_agent_node_effect(begin).await?;
        let permit = match result {
            AgentNodeEffectStart::Confirmed(outcome) => {
                return self
                    .publish_attested_plan(executor, outcome, &claim.run_id)
                    .await
                    .map(Response::new);
            }
            AgentNodeEffectStart::EffectUnknown => {
                return Err(Status::failed_precondition("EFFECT_UNKNOWN"));
            }
            AgentNodeEffectStart::Started(permit) => permit,
        };
        let sent = async {
            let deadline = self.node_send_lease(&permit).await?;
            let now = authority_request::parse_utc_z_str(&authority_request::utc_z_now().map_err(|_| unavailable())?).map_err(|_| unavailable())?;
            if deadline <= now { return Err(Status::failed_precondition("AGENT_EXECUTION_LEASE_DENIED")); }
            let mut config = executor.upstream.clone();
            config.timeout = config.timeout.min(Duration::from_secs((deadline - now) as u64));
            let (turn, receipt) = deepseek_gateway::chat_execution::exchange_fenced_turn(&config, &body, &permit.fence,
                permit.operation_id(), crate::MAX_AGENT_NODE_OUTCOME_BYTES / 2).await.map_err(|_| Status::unavailable("AGENT_PROVIDER_RESULT_UNKNOWN"))?;
            let content = turn.content();
            if content.is_empty() || !turn.tool_calls().is_empty() { return Err(Status::failed_precondition("AGENT_PLAN_RESULT_UNKNOWN")); }
            let event = json!({"type":"agent_plan","plan":multi_agent::parse_plan_response(&content),"text":content,"usage":turn.usage});
            let envelope = json!({"runId":claim.run_id,"publicEvent":event,"providerResponseSha256":receipt});
            self.confirm_agent_node_effect(&permit, &envelope).await
        }.await;
        match sent {
            Ok(outcome) => self
                .publish_attested_plan(executor, outcome, &claim.run_id)
                .await
                .map(Response::new),
            Err(error) => {
                // Stale authority can prevent this state update; the retained
                // reservation still cannot be fresh, and restart makes it unknown.
                let _ = self.mark_agent_node_effect_unknown(&permit).await;
                Err(error)
            }
        }
    }

    async fn query_effect(
        &self,
        request: Request<QueryAgentNodeEffectRequest>,
    ) -> Result<Response<QueryAgentNodeEffectResponse>, Status> {
        let metadata = request.metadata().clone();
        let input = request.into_inner();
        let selector = QueryAgentNodeEffect {
            operation_id: input.operation_id,
            fence: input
                .fence
                .ok_or_else(|| Status::invalid_argument("FENCE_MISMATCH"))?,
            run_id: input.run_id,
            node_id: input.node_id,
        };
        let mut query = Request::new(QueryAgentNodeEffect {
            operation_id: selector.operation_id.clone(),
            fence: selector.fence.clone(),
            run_id: selector.run_id.clone(),
            node_id: selector.node_id.clone(),
        });
        *query.metadata_mut() = metadata;
        let known = self.query_agent_node_effect(query).await?;
        if known
            .as_ref()
            .is_some_and(|effect| effect.state == "CONFIRMED")
        {
            let outcome = self
                .worker
                .lock()
                .await
                .authority_store
                .as_ref()
                .ok_or_else(unavailable)?
                .agent_node_outcome(&selector)?
                .ok_or_else(unavailable)?;
            let executor = self.agent_plan_executor.as_ref().ok_or_else(unavailable)?;
            let effect = self
                .publish_attested_plan(executor, outcome, &selector.run_id)
                .await?;
            return Ok(Response::new(QueryAgentNodeEffectResponse {
                found: true,
                effect: Some(effect),
            }));
        }
        Ok(Response::new(QueryAgentNodeEffectResponse {
            found: known.is_some(),
            effect: known.map(|effect| AgentNodeEffectMetadata {
                operation_id: effect.operation_id,
                fence: Some(effect.fence),
                run_id: effect.run_id,
                node_id: effect.node_id,
                state: effect.state,
                public_event: None,
                outcome_sha256: effect.output_sha256.unwrap_or_default(),
                outcome_length: effect.output_length.unwrap_or(0),
                outcome_receipt: None,
            }),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{CallerIdentity, StaticTokenAuthenticator, Worker};
    use deepseek_protocol::{ActionFence, generated::deepseek::agent::v1::AgentRunExecutionClaim};
    use sha2::{Digest as _, Sha256};

    fn executor(root: &std::path::Path) -> AgentPlanExecutor {
        AgentPlanExecutor {
            artifact_root: root.join("artifacts"),
            workspace_root: root.to_path_buf(),
            upstream: UpstreamConfig {
                url: "http://127.0.0.1:9/chat".into(),
                api_key: "fixture-provider-secret".into(),
                timeout: Duration::from_secs(1),
            },
            router: ModelRouterSettings::default(),
        }
    }

    fn request(root: &std::path::Path, body: &Value) -> ExecuteAgentPlanRequest {
        let reference = ArtifactStore::open_native(&root.join("artifacts"), "python_disabled")
            .unwrap()
            .put(body)
            .unwrap();
        ExecuteAgentPlanRequest {
            claim: Some(AgentRunExecutionClaim {
                request: Some(AgentArtifactReference {
                    sha256: reference.sha256,
                    length: reference.length,
                }),
                ..Default::default()
            }),
            ..Default::default()
        }
    }

    #[tokio::test]
    async fn planner_reads_immutable_rust_body_and_uses_the_retained_request_policy() {
        let root = tempfile::tempdir().unwrap();
        let executor = executor(root.path());
        let input = request(
            root.path(),
            &json!({"requestPayload":{"messages":[{"role":"user","content":"inspect"}],"toolsEnabled":true,"searchEnabled":true}}),
        );
        let body = executor.prepare(&input).await.unwrap();
        assert_eq!(body["model"], executor.router.refine_model);
        assert!(!body.to_string().contains("fixture-provider-secret"));
        assert!(body.get("tools").is_none());
        assert_eq!(body["stream"], false);
        assert!(!root.path().join(".budget").exists());
        let mut rebound = input;
        rebound
            .claim
            .as_mut()
            .unwrap()
            .request
            .as_mut()
            .unwrap()
            .sha256 = "f".repeat(64);
        assert_eq!(
            executor.prepare(&rebound).await.unwrap_err().message(),
            "AGENT_REQUEST_ARTIFACT_DENIED"
        );
        assert!(!root.path().join("rust-worker").exists());
    }

    #[tokio::test]
    async fn invalid_body_is_rejected_before_any_reservation_or_provider_call() {
        let root = tempfile::tempdir().unwrap();
        let executor = executor(root.path());
        let input = request(root.path(), &json!({"requestPayload":false}));
        assert_eq!(
            executor.prepare(&input).await.unwrap_err().code(),
            tonic::Code::InvalidArgument
        );
        assert!(
            executor
                .prepare(&ExecuteAgentPlanRequest::default())
                .await
                .is_err()
        );
        assert!(!root.path().join(".budget").exists());
        assert!(!root.path().join("rust-worker").exists());
    }

    #[tokio::test]
    async fn public_projection_is_immutable_and_does_not_leak_provider_fields() {
        let root = tempfile::tempdir().unwrap();
        let executor = executor(root.path());
        let event = json!({"type":"agent_plan","plan":{"summary":"inspect","apiKey":"hidden-fixture-value"},"text":"plan"});
        let output = authority_request::canonical_json_bytes(&json!({"runId":"run-unit","publicEvent":event,"providerResponseSha256":"a".repeat(64)})).unwrap();
        let outcome = || AgentNodeEffectOutcome {
            operation_id: format!("agent-node-{}", "a".repeat(64)),
            fence: ActionFence {
                action_id: "agent-exec-unit".into(),
                execution_epoch: 1,
            },
            node_id: "leader".into(),
            output_sha256: format!("{:x}", Sha256::digest(&output)),
            output: output.clone(),
        };
        let first = executor.publish_known(outcome(), "run-unit").await.unwrap();
        assert_eq!(
            executor.publish_known(outcome(), "run-unit").await.unwrap(),
            first
        );
        let reference = first.public_event.unwrap();
        let value = ArtifactStore::open_readonly(&executor.artifact_root)
            .unwrap()
            .get(&ArtifactReference {
                sha256: reference.sha256,
                length: reference.length,
            })
            .unwrap();
        assert!(!value.to_string().contains("hidden-fixture-value"));
        assert_eq!(
            executor
                .publish_known(outcome(), "other-run")
                .await
                .unwrap_err()
                .code(),
            tonic::Code::DataLoss
        );
        assert!(!format!("{executor:?}").contains("fixture-provider-secret"));
    }

    #[tokio::test]
    async fn production_rpc_requires_authenticated_go_controller_and_configured_custody() {
        for role in ["controller", "worker"] {
            let service = WorkerRpcService::new_with_authenticator(
                Worker::new(),
                Arc::new(StaticTokenAuthenticator::new(
                    "fixture-bearer",
                    CallerIdentity {
                        service_name: "go-control-plane".into(),
                        role: role.into(),
                    },
                )),
            );
            let missing = service
                .execute_plan(Request::new(ExecuteAgentPlanRequest::default()))
                .await
                .unwrap_err();
            assert_eq!(missing.code(), tonic::Code::Unauthenticated);
            let mut input = Request::new(ExecuteAgentPlanRequest::default());
            input
                .metadata_mut()
                .insert("authorization", "Bearer fixture-bearer".parse().unwrap());
            let rejected = service.execute_plan(input).await.unwrap_err();
            assert_eq!(
                rejected.code(),
                if role == "controller" {
                    tonic::Code::Unavailable
                } else {
                    tonic::Code::PermissionDenied
                }
            );
        }
    }
}
