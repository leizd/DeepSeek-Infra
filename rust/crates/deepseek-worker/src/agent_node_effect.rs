//! Node effects remain in Rust custody; a Go intent is never outcome evidence.
use deepseek_protocol::ActionFence;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentNodeDispatchIntent, AgentRunExecutionClaim, BeginAgentNodeDispatchResponse,
};
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tonic::{Request, Status};

use crate::{WorkerAuthorityConfig, WorkerRpcService, agent_execution_grant, authority_request};

pub const MAX_AGENT_NODE_OUTCOME_BYTES: usize = 1024 * 1024;

pub struct BeginAgentNodeEffect {
    pub claim: AgentRunExecutionClaim,
    pub signed_phase_grant: Vec<u8>,
    pub operation_id: String,
    pub actor: String,
    pub node_id: String,
}

impl std::fmt::Debug for BeginAgentNodeEffect {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BeginAgentNodeEffect")
            .field("run_id", &self.claim.run_id)
            .field("node_id", &self.node_id)
            .finish_non_exhaustive()
    }
}

#[derive(Debug)]
pub struct QueryAgentNodeEffect {
    pub operation_id: String,
    pub fence: ActionFence,
    pub run_id: String,
    pub node_id: String,
}

/// Scheduling metadata only. Output bytes remain in the Rust journal.
#[derive(Debug, PartialEq)]
pub struct AgentNodeEffectSnapshot {
    pub operation_id: String,
    pub fence: ActionFence,
    pub run_id: String,
    pub node_id: String,
    pub state: String,
    pub output_sha256: Option<String>,
    pub output_length: Option<u64>,
}

/// Created only after an authenticated Go decision and a committed Rust fence.
/// Its fields are private and it is deliberately not Clone or deserializable.
pub struct AgentNodeEffectPermit {
    pub(crate) operation_id: String,
    pub(crate) fence: ActionFence,
    pub(crate) fencing_token: i64,
    pub(crate) grant_digest: String,
    pub(crate) intent_digest: String,
    pub(crate) intent: Box<AgentNodeDispatchIntent>,
}

impl std::fmt::Debug for AgentNodeEffectPermit {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AgentNodeEffectPermit")
            .field("operation_id", &self.operation_id)
            .field("fence", &self.fence)
            .finish_non_exhaustive()
    }
}

impl AgentNodeEffectPermit {
    pub fn operation_id(&self) -> &str {
        &self.operation_id
    }
}

#[derive(PartialEq)]
pub struct AgentNodeEffectOutcome {
    pub operation_id: String,
    pub fence: ActionFence,
    pub node_id: String,
    pub output_sha256: String,
    pub output: Vec<u8>,
}

impl std::fmt::Debug for AgentNodeEffectOutcome {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AgentNodeEffectOutcome")
            .field("operation_id", &self.operation_id)
            .field("fence", &self.fence)
            .field("node_id", &self.node_id)
            .field("output_sha256", &self.output_sha256)
            .field("output_length", &self.output.len())
            .finish_non_exhaustive()
    }
}

#[derive(Debug)]
pub enum AgentNodeEffectStart {
    Started(AgentNodeEffectPermit),
    Confirmed(AgentNodeEffectOutcome),
    EffectUnknown,
}

pub(crate) fn denied() -> Status {
    Status::failed_precondition("AGENT_NODE_EFFECT_DENIED")
}

pub(crate) fn digest(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct NodeIdentity<'a> {
    schema_version: u32,
    operation_id: &'static str,
    action_id: &'a str,
    execution_epoch: u64,
    run_id: &'a str,
    node_id: &'a str,
    phase: &'a str,
    task: &'a str,
    depends_on: &'a [String],
    request_ref: ArtifactIdentity<'a>,
    plan_digest: &'a str,
    metadata_index: i64,
    metadata_epoch: u64,
    state: &'static str,
    recorded_at: i64,
}

#[derive(Serialize)]
struct ArtifactIdentity<'a> {
    sha256: &'a str,
    length: u64,
}

/// Match Go's fixed struct field order and encoding/json HTML escaping. The
/// digest includes the entire selected task, not only the phase plan digest.
pub(crate) fn node_operation_id(intent: &AgentNodeDispatchIntent) -> Result<String, Status> {
    let fence = intent.fence.as_ref().ok_or_else(denied)?;
    let request = intent.request.as_ref().ok_or_else(denied)?;
    let phase = match intent.phase {
        1 => "plan",
        2 => "tasks",
        _ => return Err(denied()),
    };
    let identity = NodeIdentity {
        schema_version: intent.schema_version,
        operation_id: "",
        action_id: &fence.action_id,
        execution_epoch: fence.execution_epoch,
        run_id: &intent.run_id,
        node_id: &intent.node_id,
        phase,
        task: &intent.task,
        depends_on: &intent.depends_on,
        request_ref: ArtifactIdentity {
            sha256: &request.sha256,
            length: request.length,
        },
        plan_digest: &intent.plan_digest,
        metadata_index: intent.metadata_index,
        metadata_epoch: intent.metadata_epoch,
        state: "",
        recorded_at: 0,
    };
    let encoded = serde_json::to_string(&identity)
        .map_err(|_| denied())?
        .replace('<', "\\u003c")
        .replace('>', "\\u003e")
        .replace('&', "\\u0026")
        .replace('\u{2028}', "\\u2028")
        .replace('\u{2029}', "\\u2029");
    Ok(format!(
        "agent-node-{}",
        digest(format!("agent-node-dispatch-v1\n{encoded}").as_bytes())
    ))
}

pub(crate) fn validate_scope(
    response: &BeginAgentNodeDispatchResponse,
    grant: &[u8],
    config: &WorkerAuthorityConfig,
    now: &str,
) -> Result<(), Status> {
    let claim = response.claim.as_ref().ok_or_else(denied)?;
    let intent = response.intent.as_ref().ok_or_else(denied)?;
    let seconds = authority_request::parse_utc_z_str(now).map_err(|_| denied())?;
    agent_execution_grant::validate_node_dispatch(response, claim, &intent.node_id, seconds)?;
    if intent.operation_id != node_operation_id(intent)?
        || intent.recorded_at > seconds.saturating_add(30)
    {
        return Err(denied());
    }
    crate::verify_agent_execution_grant(
        grant,
        &crate::AgentExecutionGrantContext {
            now,
            authority: config,
            claim,
        },
    )?;
    Ok(())
}

impl WorkerRpcService {
    /// Read historical evidence even after a lease expires or a successor epoch
    /// is installed. Absence never authorizes a retry or proves NOT_APPLIED.
    pub async fn query_agent_node_effect(
        &self,
        request: Request<QueryAgentNodeEffect>,
    ) -> Result<Option<AgentNodeEffectSnapshot>, Status> {
        let identity = self
            .authenticator
            .authenticate(request.metadata())
            .map_err(|error| Status::unauthenticated(error.code()))?;
        if identity.service_name != "go-control-plane" || identity.role != "controller" {
            return Err(Status::permission_denied("AGENT_NODE_CALLER_DENIED"));
        }
        self.worker
            .lock()
            .await
            .authority_store
            .as_ref()
            .ok_or_else(denied)?
            .agent_node_snapshot(&request.into_inner())
    }

    /// The service obtains Fresh from its own pinned Go RPC. A caller cannot
    /// manufacture a dispatch permission by supplying a reservation response.
    pub async fn begin_agent_node_effect(
        &self,
        request: Request<BeginAgentNodeEffect>,
    ) -> Result<AgentNodeEffectStart, Status> {
        let identity = self
            .authenticator
            .authenticate(request.metadata())
            .map_err(|error| Status::unauthenticated(error.code()))?;
        if identity.service_name != "go-control-plane" || identity.role != "controller" {
            return Err(Status::permission_denied("AGENT_NODE_CALLER_DENIED"));
        }
        let input = request.into_inner();
        let control = self
            .agent_execution_control
            .as_ref()
            .ok_or_else(|| Status::unavailable("AGENT_EXECUTION_CONTROL_UNAVAILABLE"))?;
        let now = authority_request::utc_z_now().map_err(|_| denied())?;
        {
            let worker = self.worker.lock().await;
            let config = worker.agent_node_authority()?;
            crate::verify_agent_execution_grant(
                &input.signed_phase_grant,
                &crate::AgentExecutionGrantContext {
                    now: &now,
                    authority: &config,
                    claim: &input.claim,
                },
            )?;
            worker
                .authority_store
                .as_ref()
                .ok_or_else(denied)?
                .admit(input.claim.fence.as_ref().ok_or_else(denied)?)
                .map_err(|_| denied())?;
        }
        let response = control
            .begin_node_dispatch(
                &input.claim,
                &input.operation_id,
                &input.actor,
                &input.node_id,
            )
            .await?;
        let now = authority_request::utc_z_now().map_err(|_| denied())?;
        let mut worker = self.worker.lock().await;
        let config = worker.agent_node_authority()?;
        validate_scope(&response, &input.signed_phase_grant, &config, &now)?;
        worker
            .authority_store
            .as_mut()
            .ok_or_else(denied)?
            .begin_agent_node(&response, &input.signed_phase_grant, &config, &now)
    }

    /// An actual executor returns the complete bounded response here. Confirmation
    /// binds its opaque permit; it is not selected by a Go/browser status flag.
    pub async fn confirm_agent_node_effect(
        &self,
        permit: &AgentNodeEffectPermit,
        output: &Value,
    ) -> Result<AgentNodeEffectOutcome, Status> {
        let bytes = authority_request::canonical_json_bytes(output).map_err(|_| denied())?;
        if bytes.is_empty() || bytes.len() > MAX_AGENT_NODE_OUTCOME_BYTES {
            return Err(denied());
        }
        let now = authority_request::utc_z_now().map_err(|_| denied())?;
        self.worker
            .lock()
            .await
            .authority_store
            .as_mut()
            .ok_or_else(denied)?
            .confirm_agent_node(permit, &bytes, &now)
    }

    pub async fn mark_agent_node_effect_unknown(
        &self,
        permit: &AgentNodeEffectPermit,
    ) -> Result<(), Status> {
        let now = authority_request::utc_z_now().map_err(|_| denied())?;
        self.worker
            .lock()
            .await
            .authority_store
            .as_mut()
            .ok_or_else(denied)?
            .mark_agent_node_unknown(permit, &now)
    }
}

impl crate::Worker {
    pub(crate) fn agent_node_authority(&self) -> Result<WorkerAuthorityConfig, Status> {
        let authority = self.authority.as_ref().ok_or_else(denied)?;
        Ok(WorkerAuthorityConfig {
            signer_public_key: authority.signer_public_key.clone(),
            fleet_id: authority.fleet_id.clone(),
            environment: authority.environment.clone(),
            fencing_token: authority.fencing_token,
            now: None,
        })
    }
}
