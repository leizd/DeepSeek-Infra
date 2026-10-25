//! Durable planner execution; Go alone decides the resulting run/phase state.
use std::{
    io::Write,
    path::{Path, PathBuf},
    sync::Arc,
    time::Duration,
};

use sha2::{Digest, Sha256};

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

#[derive(Clone)]
pub struct AgentPlanExecutor {
    pub(crate) artifact_root: PathBuf,
    pub(crate) workspace_root: PathBuf,
    pub(crate) upstream: UpstreamConfig,
    pub(crate) router: ModelRouterSettings,
}

impl std::fmt::Debug for AgentPlanExecutor {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AgentPlanExecutor")
            .field("artifact_root", &self.artifact_root)
            .field("workspace_root", &self.workspace_root)
            .finish_non_exhaustive()
    }
}

fn hold_published_answer(mut metadata: AgentNodeEffectMetadata) -> AgentNodeEffectMetadata {
    metadata.state = "AWAITING_TOOLS".into();
    metadata.outcome_receipt = None;
    metadata
}

fn unavailable() -> Status {
    Status::unavailable("AGENT_PLAN_EXECUTOR_UNAVAILABLE")
}

fn unstored() -> Status {
    Status::unavailable("AGENT_TOOL_RESULT_UNSTORED")
}

fn driver_result_scope(value: &str) -> String {
    if value.is_empty() || value.len() > 64 {
        return String::new();
    }
    if value
        .bytes()
        .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b':' | b'-'))
    {
        return value.to_string();
    }
    String::new()
}

/// Tool results live beside the artifact store, not in the confirmed SQL row.
/// A replay reads this file and does not run the tool again.
fn retained_digest_name(operation_id: &str) -> Option<String> {
    if operation_id.is_empty() || operation_id.len() > 256 {
        return None;
    }
    let digest = Sha256::digest(operation_id.as_bytes());
    let mut name = String::with_capacity(digest.len() * 2 + 5);
    const HEX: &[u8; 16] = b"0123456789abcdef";
    for byte in digest {
        name.push(HEX[(byte >> 4) as usize] as char);
        name.push(HEX[(byte & 0x0f) as usize] as char);
    }
    name.push_str(".json");
    Some(name)
}

fn retained_tool_result_path(root: &Path, operation_id: &str) -> Option<PathBuf> {
    Some(
        root.join("retained-tool-results")
            .join(retained_digest_name(operation_id)?),
    )
}

fn model_turn_path(root: &Path, operation_id: &str) -> Option<PathBuf> {
    Some(
        root.join("retained-model-turns")
            .join(retained_digest_name(operation_id)?),
    )
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
    upstream.validate_origin().map_err(|_| unavailable())?;
    Ok(Some(AgentPlanExecutor {
        artifact_root,
        workspace_root,
        upstream,
        router: ModelRouterSettings::from_env(),
    }))
}

impl WorkerRpcService {
    pub(crate) async fn publish_attested_node(
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
        if metadata.state == "AWAITING_TOOLS" || metadata.state == "ANSWER_PUBLISHED" {
            // A tool request stays on scheduling refs. A published answer may
            // carry the receipt attached below. A missing answer stays waiting.
            self.project_retained_driver_tools(executor, &metadata)
                .await;
            if metadata.state == "ANSWER_PUBLISHED" {
                metadata = self
                    .attach_published_answer_receipt(executor, &query, metadata)
                    .await;
            }
            return Ok(metadata);
        }
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

    async fn project_retained_driver_tools(
        &self,
        executor: &AgentPlanExecutor,
        metadata: &AgentNodeEffectMetadata,
    ) {
        let Some(reference) = metadata.public_event.clone() else {
            return;
        };
        if reference.sha256.len() != 64 || reference.length == 0 || metadata.node_id.is_empty() {
            return;
        }
        let root = executor.artifact_root.clone();
        let sha = reference.sha256.clone();
        let length = reference.length;
        let loaded = tokio::task::spawn_blocking(move || {
            ArtifactStore::open_readonly(&root)
                .and_then(|store| {
                    store.get(&ArtifactReference {
                        sha256: sha,
                        length,
                    })
                })
                .ok()
        })
        .await;
        let Ok(Some(event)) = loaded else {
            return;
        };
        let decision = deepseek_policy::agent_runtime_admission::retained_driver_tool_results(
            "AWAITING_TOOLS",
            std::slice::from_ref(&event),
            None,
        );
        if !decision.returned || decision.events.is_empty() {
            return;
        }
        let mut refs = Vec::new();
        for item in &decision.events {
            if !item.is_object() {
                return;
            }
            let scope =
                driver_result_scope(item.get("toolCallId").and_then(Value::as_str).unwrap_or(""));
            let root = executor.artifact_root.clone();
            let stored = item.clone();
            let put = tokio::task::spawn_blocking(move || {
                ArtifactStore::open_native(&root, "python_disabled")
                    .and_then(|mut store| store.put(&stored))
                    .ok()
            })
            .await;
            let Ok(Some(body)) = put else {
                return;
            };
            refs.push((scope, body.sha256, body.length));
        }
        if refs.is_empty() {
            return;
        }
        if let Some(control) = self.agent_execution_control.clone() {
            let _ = control
                .project_driver_tool_results(
                    &metadata.run_id,
                    &metadata.operation_id,
                    &metadata.node_id,
                    &reference.sha256,
                    reference.length,
                    &refs,
                )
                .await;
        }
    }

    async fn attach_published_answer_receipt(
        &self,
        executor: &AgentPlanExecutor,
        query: &QueryAgentNodeEffect,
        metadata: AgentNodeEffectMetadata,
    ) -> AgentNodeEffectMetadata {
        let Some(saved) = executor.read_model_turn(&query.operation_id) else {
            return hold_published_answer(metadata);
        };
        let Some(reference) = metadata.public_event.clone() else {
            return hold_published_answer(metadata);
        };
        if reference.sha256.len() != 64 || reference.length == 0 {
            return hold_published_answer(metadata);
        }
        let root = executor.artifact_root.clone();
        let sha = reference.sha256.clone();
        let length = reference.length;
        let loaded = tokio::task::spawn_blocking(move || {
            ArtifactStore::open_readonly(&root)
                .and_then(|store| {
                    store.get(&ArtifactReference {
                        sha256: sha,
                        length,
                    })
                })
                .ok()
        })
        .await;
        let Ok(Some(public_event)) = loaded else {
            return hold_published_answer(metadata);
        };
        let proof = {
            let worker = self.worker.lock().await;
            let Ok(config) = worker.agent_node_authority() else {
                return hold_published_answer(metadata);
            };
            let Some(store) = worker.authority_store.as_ref() else {
                return hold_published_answer(metadata);
            };
            match store.published_answer_outcome_evidence(query, &public_event, &saved, &config) {
                Ok(proof) => proof,
                Err(_) => return hold_published_answer(metadata),
            }
        };
        let (evidence, event) = proof;
        let root = executor.artifact_root.clone();
        let put = tokio::task::spawn_blocking(move || {
            ArtifactStore::open_native(&root, "python_disabled")
                .and_then(|mut store| store.put(&event))
                .ok()
        })
        .await;
        let Ok(Some(body)) = put else {
            return hold_published_answer(metadata);
        };
        let Some(expected) = evidence.public_event.as_ref() else {
            return hold_published_answer(metadata);
        };
        if expected.sha256 != body.sha256 || expected.length != body.length {
            return hold_published_answer(metadata);
        }
        let Some(signer) = self.control_signer.as_ref() else {
            return hold_published_answer(metadata);
        };
        let mut guard = signer.lock().await;
        let receipt = match guard.sign_node_outcome(evidence) {
            Ok(receipt) => receipt,
            Err(_) => return hold_published_answer(metadata),
        };
        let mut metadata = metadata;
        metadata.outcome_receipt = Some(receipt);
        metadata
    }

    pub fn with_agent_plan_executor(mut self, executor: AgentPlanExecutor) -> Self {
        self.agent_plan_executor = Some(Arc::new(executor));
        self
    }

    pub(crate) async fn node_send_lease(
        &self,
        permit: &AgentNodeEffectPermit,
    ) -> Result<i64, Status> {
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
    pub(crate) fn read_retained_tool_results(&self, operation_id: &str) -> Option<Value> {
        let path = retained_tool_result_path(&self.artifact_root, operation_id)?;
        let bytes = std::fs::read(path).ok()?;
        let value: Value = serde_json::from_slice(&bytes).ok()?;
        if value.as_array().is_none() {
            return None;
        }
        Some(value)
    }

    /// Create the result file once. An existing file is left as it is.
    pub(crate) fn store_retained_tool_results(
        &self,
        operation_id: &str,
        results: &Value,
    ) -> Result<(), Status> {
        if !results.is_array() {
            return Err(unstored());
        }
        let path =
            retained_tool_result_path(&self.artifact_root, operation_id).ok_or_else(unstored)?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|_| unstored())?;
        }
        let mut file = match std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => return Ok(()),
            Err(_) => return Err(unstored()),
        };
        let bytes = serde_json::to_vec(results).map_err(|_| unstored())?;
        file.write_all(&bytes).map_err(|_| unstored())?;
        Ok(())
    }

    /// Write the follow-up answer once. An existing file is left as it is.
    /// This does not settle the node.
    pub(crate) fn store_model_turn(
        &self,
        operation_id: &str,
        envelope: &Value,
    ) -> Result<(), Status> {
        if !envelope.is_object() {
            return Err(unstored());
        }
        let path = model_turn_path(&self.artifact_root, operation_id).ok_or_else(unstored)?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|_| unstored())?;
        }
        let mut file = match std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
        {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => return Ok(()),
            Err(_) => return Err(unstored()),
        };
        let bytes = serde_json::to_vec(envelope).map_err(|_| unstored())?;
        file.write_all(&bytes).map_err(|_| unstored())?;
        Ok(())
    }

    pub(crate) fn read_model_turn(&self, operation_id: &str) -> Option<Value> {
        let path = model_turn_path(&self.artifact_root, operation_id)?;
        let bytes = std::fs::read(path).ok()?;
        let value: Value = serde_json::from_slice(&bytes).ok()?;
        if !value.is_object() {
            return None;
        }
        Some(value)
    }

    async fn prepare_preset(
        &self,
        request: &ExecuteAgentPlanRequest,
    ) -> Result<Option<Value>, Status> {
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
            let preset = body.get("agentPreset").map_or(Ok("full"), |value| {
                value
                    .as_str()
                    .ok_or_else(|| Status::invalid_argument("AGENT_REQUEST_INVALID"))
            })?;
            Ok(multi_agent::preset_plan(payload, preset))
        })
        .await
        .map_err(|_| unavailable())?
    }

    async fn prepare(&self, request: &ExecuteAgentPlanRequest) -> Result<Value, Status> {
        let reference = request
            .claim
            .as_ref()
            .and_then(|claim| claim.request.as_ref())
            .ok_or_else(unavailable)?;
        let model = self.router.refine_model.clone();
        self.prepare_payload(reference, move |payload| {
            Ok(multi_agent::planner_payload(payload, &model))
        })
        .await
    }

    pub(crate) async fn prepare_payload(
        &self,
        reference: &AgentArtifactReference,
        shape: impl FnOnce(&Value) -> Result<Value, Status> + Send + 'static,
    ) -> Result<Value, Status> {
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
            let shaped = shape(payload)?;
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
                build_deepseek_request(&shaped, false, None, None, &environment)
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
        let mut event = envelope
            .get("publicEvent")
            .filter(|value| value.is_object())
            .ok_or_else(unavailable)?
            .clone();
        let planner = outcome.node_id == "leader" && event["type"] == "agent_plan";
        let task = deepseek_policy::multi_agent::profile(&outcome.node_id).is_some()
            && event["phase"] == outcome.node_id
            && event["type"] == "agent_output"
            && event["output"]["id"] == outcome.node_id;
        let pending_tools = deepseek_policy::multi_agent::profile(&outcome.node_id).is_some()
            && event["phase"] == outcome.node_id
            && event["type"] == "agent_tool_request"
            && envelope["toolContinuation"]["toolCalls"]
                .as_array()
                .is_some_and(|calls| !calls.is_empty());
        let synthesis = outcome.node_id == "synthesizer"
            && event["type"] == "content"
            && event["text"]
                .as_str()
                .is_some_and(|text| !text.trim().is_empty());
        if envelope["runId"] != run_id || !(planner || task || pending_tools || synthesis) {
            return Err(Status::data_loss("AGENT_PLAN_OUTCOME_CORRUPT"));
        }
        let mut answered = false;
        if pending_tools {
            let stored = self.read_retained_tool_results(&outcome.operation_id);
            let results = stored.or_else(|| {
                envelope
                    .get("toolResults")
                    .filter(|value| value.is_array())
                    .cloned()
            });
            if let Some(results) = results {
                if let Some(object) = event.as_object_mut() {
                    object.insert("toolResults".to_string(), results);
                }
            }
            if let Some(saved) = self.read_model_turn(&outcome.operation_id) {
                let current = event.clone();
                let decision = deepseek_policy::agent_runtime_admission::published_model_answer(
                    std::slice::from_ref(&current),
                    &saved,
                );
                if decision.published {
                    if let Some(answer) = decision.event {
                        if let Some(object) = event.as_object_mut() {
                            object.insert("publishedAnswer".to_string(), answer);
                            answered = true;
                        }
                    }
                }
            }
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
            state: if answered {
                "ANSWER_PUBLISHED"
            } else if pending_tools {
                "AWAITING_TOOLS"
            } else {
                "CONFIRMED"
            }
            .into(),
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
    async fn prepare_credentials(
        &self,
        request: Request<
            deepseek_protocol::generated::deepseek::agent::v1::PrepareAgentCredentialsRequest,
        >,
    ) -> Result<
        Response<
            deepseek_protocol::generated::deepseek::agent::v1::PrepareAgentCredentialsResponse,
        >,
        Status,
    > {
        self.prepare_agent_provider_credentials(request).await
    }
    async fn execute_task(
        &self,
        request: Request<
            deepseek_protocol::generated::deepseek::agent::v1::ExecuteAgentTaskRequest,
        >,
    ) -> Result<Response<AgentNodeEffectMetadata>, Status> {
        self.execute_agent_task(request).await
    }
    async fn execute_synthesis(
        &self,
        request: Request<
            deepseek_protocol::generated::deepseek::agent::v1::ExecuteAgentTaskRequest,
        >,
    ) -> Result<Response<AgentNodeEffectMetadata>, Status> {
        self.execute_agent_synthesis(request).await
    }
    async fn project_synthesis(
        &self,
        request: Request<
            deepseek_protocol::generated::deepseek::agent::v1::ProjectAgentSynthesisRequest,
        >,
    ) -> Result<
        Response<
            deepseek_protocol::generated::deepseek::agent::v1::AgentSynthesisProjectionReceipt,
        >,
        Status,
    > {
        self.project_agent_synthesis(request).await
    }
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
        let selected = self.agent_executor_for_claim(claim).await?;
        let executor = &selected;
        let preset = executor.prepare_preset(&input).await?;
        let body = if preset.is_none() {
            executor
                .upstream
                .validate()
                .map_err(|_| Status::failed_precondition("AGENT_PROVIDER_CREDENTIAL_MISSING"))?;
            Some(executor.prepare(&input).await?)
        } else {
            None
        };
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
                    .publish_attested_node(executor, outcome, &claim.run_id)
                    .await
                    .map(Response::new);
            }
            AgentNodeEffectStart::EffectUnknown => {
                return Err(Status::failed_precondition("EFFECT_UNKNOWN"));
            }
            AgentNodeEffectStart::Started(permit) => permit,
        };
        let service = self.clone();
        let executor = std::sync::Arc::clone(executor);
        let run_id = claim.run_id.clone();
        crate::agent_task_executor::retain_admitted_agent_output(async move {
        let sent = async {
            let deadline = service.node_send_lease(&permit).await?;
            let now = authority_request::parse_utc_z_str(&authority_request::utc_z_now().map_err(|_| unavailable())?).map_err(|_| unavailable())?;
            let lease = deepseek_policy::agent_runtime_admission::execution_send_lease(
                deadline, now,
            );
            let admit = deepseek_policy::agent_runtime_admission::admitted_provider_send(lease);
            if !admit.allowed {
                return Err(Status::failed_precondition(admit.code));
            }
            let envelope = if let Some(plan) = preset {
                let label = plan["label"].as_str().ok_or_else(unavailable)?;
                let event = json!({"type":"agent_plan","plan":plan,"text":label,"usage":{}});
                // This is a local policy result; do not invent an upstream response.
                json!({"runId":run_id,"publicEvent":event,"planningSource":"retained-preset-policy"})
            } else {
                let mut config = executor.upstream.clone();
                let budget = deepseek_policy::agent_runtime_admission::execution_send_budget(
                    deadline,
                    now,
                    config.timeout.as_secs(),
                );
                if !budget.armed {
                    return Err(Status::failed_precondition(budget.code));
                }
                config.timeout = Duration::from_secs(budget.seconds);
                let (turn, receipt) = deepseek_gateway::chat_execution::exchange_fenced_turn(&config, body.as_ref().ok_or_else(unavailable)?, &permit.fence,
                    permit.operation_id(), crate::MAX_AGENT_NODE_OUTCOME_BYTES / 2).await.map_err(|_| {
                    let failure = deepseek_policy::agent_runtime_admission::provider_exchange_unknown(
                        false,
                    );
                    Status::unavailable(failure.code)
                })?;
                let content = turn.content();
                if content.is_empty() || !turn.tool_calls().is_empty() { return Err(Status::failed_precondition("AGENT_PLAN_RESULT_UNKNOWN")); }
                let event = json!({"type":"agent_plan","plan":multi_agent::parse_plan_response(&content),"text":content,"usage":turn.usage});
                json!({"runId":run_id,"publicEvent":event,"providerResponseSha256":receipt})
            };
            service.confirm_agent_node_effect(&permit, &envelope).await
        }.await;
        match sent {
            Ok(outcome) => service
                .publish_attested_node(&executor, outcome, &run_id)
                .await,
            Err(error) => {
                // Stale authority can prevent this state update; the retained
                // reservation still cannot be fresh, and restart makes it unknown.
                let _ = service.mark_agent_node_effect_unknown(&permit).await;
                Err(error)
            }
        }
        })
        .await
        .map(Response::new)
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
                .publish_attested_node(executor, outcome, &selector.run_id)
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
                // A committed dispatch without a retained result is unresolved
                // even while its original provider future remains in flight.
                state: if effect.state == "DISPATCHING" {
                    "EFFECT_UNKNOWN".into()
                } else {
                    effect.state
                },
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
    async fn static_presets_read_bound_rust_input_without_provider_or_budget_access() {
        let root = tempfile::tempdir().unwrap();
        let executor = executor(root.path());
        for preset in ["full", "code", "research", "reason", "critic"] {
            let input = request(
                root.path(),
                &json!({"agentPreset":preset,"payload":{"messages":[{"role":"user","content":"inspect"}]}}),
            );
            let plan = executor.prepare_preset(&input).await.unwrap().unwrap();
            assert!(
                plan["agents"]
                    .as_array()
                    .is_some_and(|nodes| !nodes.is_empty())
            );
            assert!(!plan.to_string().contains("fixture-provider-secret"));
        }
        let input = request(
            root.path(),
            &json!({"agentPreset":"leader","payload":{"messages":[{"role":"user","content":"inspect"}]}}),
        );
        assert!(executor.prepare_preset(&input).await.unwrap().is_none());
        let input = request(
            root.path(),
            &json!({"agentPreset":"auto","payload":{"messages":[{"role":"user","content":"代码"}]}}),
        );
        assert_eq!(
            executor.prepare_preset(&input).await.unwrap().unwrap()["agents"][0]["id"],
            "coder"
        );
        let input = request(root.path(), &json!({"agentPreset":"full","payload":[]}));
        assert_eq!(
            executor.prepare_preset(&input).await.unwrap_err().message(),
            "AGENT_REQUEST_INVALID"
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
