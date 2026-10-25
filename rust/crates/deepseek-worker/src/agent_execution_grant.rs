//! Go owns execution leases; Rust owns issuance and verification of effect grants.
use crate::{WorkerAuthorityConfig, authority_request};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentArtifactReference, AgentRunEventMetadata, AgentRunExecutionClaim, AgentRunExecutionPhase,
    AppendAgentRunEventRequest, BeginAgentNodeDispatchRequest, BeginAgentNodeDispatchResponse,
    GetAgentRunRequest, RenewAgentRunExecutionRequest,
    agent_node_control_client::AgentNodeControlClient,
    agent_run_control_client::AgentRunControlClient,
};
use deepseek_protocol::generated::deepseek::common::v1::ActionFence;
use ed25519_dalek::{Signature, VerifyingKey};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{env::VarError, net::SocketAddr, time::Duration};
use tonic::{Request, Status, metadata::MetadataValue, transport::Endpoint};

pub const AGENT_EXECUTION_GRANT_SCHEMA: &str = "control-agent-execution-grant-v1";
const SIGNATURE_DOMAIN: &[u8] = b"deepseek-infra:control-agent-execution-grant-v1\0";
const FIELDS: &[&str] = &[
    "actionId",
    "digest",
    "domain",
    "environment",
    "executionEpoch",
    "expiresAt",
    "fencingToken",
    "fleetId",
    "issuedAt",
    "mode",
    "nonce",
    "operation",
    "payload",
    "payloadDigest",
    "requestId",
    "revision",
    "role",
    "runtime",
    "schema",
    "schemaVersion",
    "signature",
    "signatureAlgorithm",
    "signerKeyId",
];

fn invalid() -> Status {
    Status::failed_precondition("AGENT_EXECUTION_GRANT_INVALID")
}
fn unavailable() -> Status {
    Status::unavailable("AGENT_EXECUTION_CONTROL_UNAVAILABLE")
}
fn hex64(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

pub(crate) fn canonical_agent_operation(operation_id: &str) -> bool {
    operation_id
        .strip_prefix("agent-node-")
        .or_else(|| operation_id.strip_prefix("agent-successor-"))
        .is_some_and(hex64)
}
fn identity_digest(domain: &str, value: &str) -> String {
    let mut digest = Sha256::new();
    digest.update(domain.as_bytes());
    digest.update([0]);
    digest.update(value.as_bytes());
    format!("sha256:{:x}", digest.finalize())
}
fn typed_digest(value: &Value) -> Result<String, Status> {
    Ok(format!(
        "sha256:{:x}",
        Sha256::digest(authority_request::canonical_json_bytes(value).map_err(|_| invalid())?)
    ))
}

/// Deployment configuration, never selected by a signing RPC's caller.
#[derive(Clone)]
pub struct AgentExecutionControl {
    endpoint: Endpoint,
    authorization: MetadataValue<tonic::metadata::Ascii>,
}
impl std::fmt::Debug for AgentExecutionControl {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("AgentExecutionControl")
            .finish_non_exhaustive()
    }
}
impl AgentExecutionControl {
    pub(crate) async fn metadata_epoch(&self) -> Result<u64, Status> {
        use deepseek_protocol::generated::deepseek::agent::v1::AgentRunAuthorityInput;
        let channel = self.endpoint.connect().await.map_err(|_| unavailable())?;
        let mut client = AgentRunControlClient::new(channel).max_decoding_message_size(64 * 1024);
        let mut request = Request::new(AgentRunAuthorityInput {});
        request
            .metadata_mut()
            .insert("authorization", self.authorization.clone());
        let authority = client
            .get_authority(request)
            .await
            .map_err(|_| unavailable())?
            .into_inner();
        if !authority.authoritative || authority.metadata_epoch == 0 {
            return Err(Status::failed_precondition(
                "AGENT_METADATA_AUTHORITY_DENIED",
            ));
        }
        Ok(authority.metadata_epoch)
    }

    pub fn new(origin: &str, bearer: &str) -> Result<Self, Status> {
        let address = origin
            .strip_prefix("http://")
            .and_then(|value| value.parse::<SocketAddr>().ok())
            .ok_or_else(unavailable)?;
        if !address.ip().is_loopback()
            || address.port() == 0
            || !(32..=4096).contains(&bearer.len())
            || !bearer.bytes().all(|b| b.is_ascii_graphic())
        {
            return Err(unavailable());
        }
        Ok(Self {
            endpoint: Endpoint::from_shared(origin.to_string())
                .map_err(|_| unavailable())?
                .connect_timeout(Duration::from_secs(3))
                .timeout(Duration::from_secs(5)),
            authorization: format!("Bearer {bearer}")
                .parse()
                .map_err(|_| unavailable())?,
        })
    }

    pub(crate) async fn renew(
        &self,
        intent: &AgentRunExecutionClaim,
    ) -> Result<AgentRunExecutionClaim, Status> {
        let channel = self.endpoint.connect().await.map_err(|_| unavailable())?;
        let mut client = AgentRunControlClient::new(channel).max_decoding_message_size(64 * 1024);
        let mut request = Request::new(RenewAgentRunExecutionRequest {
            fence: intent.fence.clone(),
            run_id: intent.run_id.clone(),
            owner: intent.owner.clone(),
            claim_token: intent.claim_token.clone(),
            lease_seconds: 60,
        });
        request
            .metadata_mut()
            .insert("authorization", self.authorization.clone());
        let live = client
            .renew_execution(request)
            .await
            .map_err(|_| Status::failed_precondition("AGENT_EXECUTION_LEASE_DENIED"))?
            .into_inner()
            .claim
            .ok_or_else(invalid)?;
        // Renewal may change dates, but it cannot rebind an execution identity.
        let mut intended = intent.clone();
        intended.lease_until = live.lease_until;
        intended.writer_lease_until = live.writer_lease_until;
        if intended != live {
            return Err(Status::failed_precondition(
                "AGENT_EXECUTION_BINDING_MISMATCH",
            ));
        }
        Ok(live)
    }

    /// Reserve a Go-selected node under the existing phase lease. This does not
    /// install a Rust epoch, persist an effect or call a provider. The dispatcher
    /// must verify its signed phase grant and durable Rust fence independently.
    /// A non-fresh response never permits another uncertain provider operation.
    pub async fn begin_node_dispatch(
        &self,
        claim: &AgentRunExecutionClaim,
        operation_id: &str,
        actor: &str,
        node_id: &str,
    ) -> Result<BeginAgentNodeDispatchResponse, Status> {
        let now = authority_request::utc_z_now().map_err(|_| unavailable())?;
        let seconds = authority_request::parse_utc_z_str(&now).map_err(|_| unavailable())?;
        validate_claim(claim, seconds)?;
        if operation_id.is_empty() || actor.is_empty() || node_id.is_empty() {
            return Err(Status::invalid_argument("AGENT_NODE_DISPATCH_REQUIRED"));
        }
        let channel = self.endpoint.connect().await.map_err(|_| unavailable())?;
        let mut client = AgentNodeControlClient::new(channel).max_decoding_message_size(64 * 1024);
        let mut request = Request::new(BeginAgentNodeDispatchRequest {
            schema_version: 1,
            operation_id: operation_id.into(),
            actor: actor.into(),
            fence: claim.fence.clone(),
            run_id: claim.run_id.clone(),
            node_id: node_id.into(),
            owner: claim.owner.clone(),
            claim_token: claim.claim_token.clone(),
        });
        request
            .metadata_mut()
            .insert("authorization", self.authorization.clone());
        let mut response = client
            .begin_dispatch(request)
            .await
            .map_err(|_| Status::failed_precondition("AGENT_NODE_DISPATCH_DENIED"))?
            .into_inner();
        validate_node_dispatch(&response, claim, node_id, seconds)?;
        let live = self
            .renew(response.claim.as_ref().ok_or_else(invalid)?)
            .await?;
        let now = authority_request::utc_z_now().map_err(|_| unavailable())?;
        let seconds = authority_request::parse_utc_z_str(&now).map_err(|_| unavailable())?;
        response.claim = Some(live);
        validate_node_dispatch(&response, claim, node_id, seconds)?;
        Ok(response)
    }

    /// Project stored tool results as scheduling headers. Result bytes stay in
    /// the artifact named by each reference. A refused append does not settle
    /// the node. The caller ignores a transport failure.
    pub(crate) async fn project_driver_tool_results(
        &self,
        run_id: &str,
        operation_id: &str,
        phase: &str,
        public_sha256: &str,
        public_length: u64,
        results: &[(String, String, u64)],
    ) -> Result<(), Status> {
        if run_id.is_empty()
            || operation_id.is_empty()
            || phase.is_empty()
            || !hex64(public_sha256)
            || public_length == 0
            || results.is_empty()
        {
            return Err(invalid());
        }
        let epoch = self.metadata_epoch().await?;
        let channel = self.endpoint.connect().await.map_err(|_| unavailable())?;
        let mut client = AgentRunControlClient::new(channel).max_decoding_message_size(64 * 1024);
        let mut read = Request::new(GetAgentRunRequest {
            run_id: run_id.to_string(),
            fence: Some(ActionFence {
                action_id: run_id.to_string(),
                execution_epoch: epoch,
            }),
        });
        read.metadata_mut()
            .insert("authorization", self.authorization.clone());
        let snapshot = client
            .get_run(read)
            .await
            .map_err(|_| unavailable())?
            .into_inner();
        if !snapshot.authoritative {
            return Ok(());
        }
        let mut expected = snapshot.run.ok_or_else(invalid)?.next_index;
        if expected < 0 {
            return Err(invalid());
        }
        let mut batch = Vec::with_capacity(results.len() + 1);
        batch.push((
            format!("retain-driver-marker:{operation_id}"),
            "agent_tool_request".to_string(),
            deepseek_policy::agent_runtime_admission::RETAINED_TOOL_RESULT_SCOPE.to_string(),
            public_sha256.to_string(),
            public_length,
        ));
        for (index, (scope, sha256, length)) in results.iter().enumerate() {
            if !hex64(sha256) || *length == 0 {
                return Err(invalid());
            }
            batch.push((
                format!("retain-driver-result:{operation_id}:{index}"),
                "agent_tool_result".to_string(),
                scope.clone(),
                sha256.clone(),
                *length,
            ));
        }
        for (operation, kind, scope, sha256, length) in batch {
            let mut request = Request::new(AppendAgentRunEventRequest {
                schema_version: 1,
                operation_id: operation,
                actor: "operator:agent-worker".to_string(),
                run_id: run_id.to_string(),
                expected_index: expected,
                event: Some(AgentRunEventMetadata {
                    r#type: kind,
                    phase: phase.to_string(),
                    scope,
                    body: Some(AgentArtifactReference { sha256, length }),
                    ..Default::default()
                }),
                fence: Some(ActionFence {
                    action_id: run_id.to_string(),
                    execution_epoch: epoch,
                }),
                ..Default::default()
            });
            request
                .metadata_mut()
                .insert("authorization", self.authorization.clone());
            let appended = client
                .append_event(request)
                .await
                .map_err(|_| unavailable())?;
            let run = appended.into_inner().run.ok_or_else(invalid)?;
            if run.next_index < expected {
                return Err(invalid());
            }
            expected = run.next_index;
        }
        Ok(())
    }
}

pub(crate) fn validate_node_dispatch(
    response: &BeginAgentNodeDispatchResponse,
    admitted: &AgentRunExecutionClaim,
    node_id: &str,
    now: i64,
) -> Result<(), Status> {
    let live = response.claim.as_ref().ok_or_else(invalid)?;
    validate_claim(live, now)?;
    let mut expected = admitted.clone();
    // Beginning a node changes only the phase's transient execution state.
    // Claim identity, metadata scope and the original action fence remain bound.
    expected.state = "EXECUTING".into();
    expected.lease_until = live.lease_until;
    expected.writer_lease_until = live.writer_lease_until;
    if expected != *live {
        return Err(Status::failed_precondition(
            "AGENT_EXECUTION_BINDING_MISMATCH",
        ));
    }
    let intent = response.intent.as_ref().ok_or_else(invalid)?;
    let role_allowed = match AgentRunExecutionPhase::try_from(live.phase) {
        Ok(AgentRunExecutionPhase::Plan) => node_id == "leader" && intent.depends_on.is_empty(),
        Ok(AgentRunExecutionPhase::Tasks) => {
            matches!(node_id, "researcher" | "coder" | "reasoner" | "critic")
        }
        Ok(AgentRunExecutionPhase::Synthesis) => {
            node_id == "synthesizer" && !intent.depends_on.is_empty()
        }
        _ => false,
    };
    if intent.schema_version != 1
        || intent.fence != live.fence
        || intent.run_id != live.run_id
        || intent.node_id != node_id
        || intent.phase != live.phase
        || intent.request != live.request
        || intent.plan_digest != live.plan_digest
        || intent.metadata_index != live.metadata_index
        || intent.metadata_epoch != live.metadata_epoch
        || intent.state != "INTENT_RECORDED"
        || intent.recorded_at < 0
        || intent.recorded_at > now.saturating_add(30)
        || intent.task.is_empty()
        || intent.task.len() > 16 * 1024
        || !canonical_agent_operation(&intent.operation_id)
        || !role_allowed
        || intent.depends_on.len() > 4
        || intent.depends_on.iter().any(|id| {
            id == node_id || !matches!(id.as_str(), "researcher" | "coder" | "reasoner" | "critic")
        })
        || intent
            .depends_on
            .iter()
            .enumerate()
            .any(|(index, id)| intent.depends_on[..index].contains(id))
    {
        return Err(Status::failed_precondition("AGENT_NODE_DISPATCH_INVALID"));
    }
    Ok(())
}

pub fn load_agent_execution_control_from_env(
    get: impl Fn(&str) -> Result<String, VarError>,
) -> Result<Option<AgentExecutionControl>, Status> {
    let mut origins = Vec::new();
    for name in ["GO_CONTROL_ADDR", "DEEPSEEK_GO_CONTROL_URL"] {
        match get(name) {
            Ok(value) if !value.is_empty() => origins.push(value),
            Err(VarError::NotPresent) => {}
            _ => return Err(unavailable()),
        }
    }
    if origins.is_empty() {
        return Ok(None);
    }
    if origins.iter().any(|origin| origin != &origins[0]) {
        return Err(unavailable());
    }
    AgentExecutionControl::new(
        &origins[0],
        &get("DEEPSEEK_INTERNAL_BEARER").map_err(|_| unavailable())?,
    )
    .map(Some)
}

pub(crate) fn validate_claim(claim: &AgentRunExecutionClaim, now: i64) -> Result<(), Status> {
    validate_claim_scope(claim, now, false)
}

pub(crate) fn validate_epoch_claim(claim: &AgentRunExecutionClaim, now: i64) -> Result<(), Status> {
    validate_claim_scope(claim, now, true)
}

fn validate_claim_scope(
    claim: &AgentRunExecutionClaim,
    now: i64,
    permit_reconciliation_fence: bool,
) -> Result<(), Status> {
    let fence = claim.fence.as_ref().ok_or_else(invalid)?;
    deepseek_protocol::validate_fence(fence).map_err(|_| invalid())?;
    let phase = match AgentRunExecutionPhase::try_from(claim.phase) {
        Ok(AgentRunExecutionPhase::Plan) => "plan",
        Ok(AgentRunExecutionPhase::Tasks) => "tasks",
        Ok(AgentRunExecutionPhase::Synthesis) => "synthesis",
        _ => return Err(invalid()),
    };
    let request = claim.request.as_ref().ok_or_else(invalid)?;
    if claim.run_id.is_empty()
        || claim.run_id.len() > 128
        || claim.run_id == "."
        || claim.run_id == ".."
        || claim
            .run_id
            .chars()
            .any(|c| c.is_control() || c == '/' || c == '\\')
        || !hex64(&request.sha256)
        || request.length == 0
        || request.length > i64::MAX as u64
        || !hex64(&claim.plan_digest)
        || claim.metadata_index < 1
        || claim.metadata_index >= i64::MAX - 1
        || claim.metadata_epoch == 0
        || claim.metadata_epoch > i64::MAX as u64
        || fence.execution_epoch > i64::MAX as u64
        || claim.claim_revision < 1
        || claim.writer_fencing_token < 1
        || claim.owner.is_empty()
        || claim.owner.len() > 512
        || claim.owner.chars().any(char::is_control)
        || claim.claim_token.is_empty()
        || claim.claim_token.len() > 4096
        || claim.claim_token.chars().any(char::is_control)
        || !((!claim.reconciliation_required
            && matches!(claim.state.as_str(), "CLAIMED" | "EXECUTING"))
            || (permit_reconciliation_fence
                && claim.reconciliation_required
                && claim.state == "RECONCILING"))
        || claim.lease_until <= now
        || claim.writer_lease_until <= now
    {
        return Err(invalid());
    }
    let identity = format!(
        "agent-run-execution-v1\n{}\n{phase}\n{}\n",
        claim.run_id, claim.metadata_index
    );
    if fence.action_id != format!("agent-exec-{:x}", Sha256::digest(identity.as_bytes())) {
        return Err(invalid());
    }
    Ok(())
}

pub(crate) fn claim_payload(claim: &AgentRunExecutionClaim) -> Result<Value, Status> {
    let request = claim.request.as_ref().ok_or_else(invalid)?;
    let phase = match AgentRunExecutionPhase::try_from(claim.phase) {
        Ok(AgentRunExecutionPhase::Plan) => "plan",
        Ok(AgentRunExecutionPhase::Tasks) => "tasks",
        Ok(AgentRunExecutionPhase::Synthesis) => "synthesis",
        _ => return Err(invalid()),
    };
    Ok(
        json!({"runId":claim.run_id,"phase":phase,"requestSha256":request.sha256,
        "requestLength":request.length,"planDigest":claim.plan_digest,"metadataIndex":claim.metadata_index,
        "metadataEpoch":claim.metadata_epoch,"ownerDigest":identity_digest("agent-owner-v1", &claim.owner),
        "claimDigest":identity_digest("agent-claim-v1", &claim.claim_token),"claimRevision":claim.claim_revision,
        "writerFencingToken":claim.writer_fencing_token,"leaseUntil":claim.lease_until,
        "writerLeaseUntil":claim.writer_lease_until}),
    )
}

pub struct AgentExecutionGrantContext<'a> {
    pub now: &'a str,
    pub authority: &'a WorkerAuthorityConfig,
    pub claim: &'a AgentRunExecutionClaim,
}

/// No receipt installs an epoch. The effect dispatcher must separately admit
/// this exact fence against its durable Rust epoch before using the grant.
pub fn verify_agent_execution_grant(
    raw: &[u8],
    context: &AgentExecutionGrantContext<'_>,
) -> Result<Value, Status> {
    if raw.is_empty() || raw.len() > 16 * 1024 {
        return Err(invalid());
    }
    let value: Value = serde_json::from_slice(raw).map_err(|_| invalid())?;
    if authority_request::canonical_json_bytes(&value).map_err(|_| invalid())? != raw {
        return Err(invalid());
    }
    let object = value.as_object().ok_or_else(invalid)?;
    if object.len() != FIELDS.len() || FIELDS.iter().any(|key| !object.contains_key(*key)) {
        return Err(invalid());
    }
    let now = authority_request::parse_utc_z_str(context.now).map_err(|_| invalid())?;
    validate_claim(context.claim, now)?;
    let fence = context.claim.fence.as_ref().ok_or_else(invalid)?;
    let config = context.authority;
    if value["schema"] != AGENT_EXECUTION_GRANT_SCHEMA
        || value["schemaVersion"] != 1
        || value["domain"] != "action"
        || value["operation"] != "execute-agent-phase"
        || value["runtime"] != "go"
        || value["mode"] != "authoritative"
        || value["role"] != "control-plane"
        || value["fleetId"] != config.fleet_id
        || value["environment"] != config.environment
        || value["signerKeyId"]
            != authority_request::signer_key_id_for_public_key(&config.signer_public_key)
                .map_err(|_| invalid())?
        || value["signatureAlgorithm"] != "Ed25519"
        || value["actionId"] != fence.action_id
        || value["executionEpoch"] != fence.execution_epoch
        || value["revision"] != context.claim.claim_revision
        || value["fencingToken"] != config.fencing_token
        || config.fencing_token != context.claim.writer_fencing_token
        || !hex64(value["requestId"].as_str().unwrap_or(""))
        || !hex64(value["nonce"].as_str().unwrap_or(""))
    {
        return Err(invalid());
    }
    let payload = value["payload"].as_object().ok_or_else(invalid)?;
    let mut expected = claim_payload(context.claim)?;
    for field in ["leaseUntil", "writerLeaseUntil"] {
        let held = payload
            .get(field)
            .and_then(Value::as_i64)
            .ok_or_else(invalid)?;
        let live = expected[field].as_i64().ok_or_else(invalid)?;
        if held <= now || held > live {
            return Err(invalid());
        }
        expected[field] = held.into();
    }
    if value["payload"] != expected || value["payloadDigest"] != typed_digest(&expected)? {
        return Err(invalid());
    }
    let issued = authority_request::parse_utc_z_str(value["issuedAt"].as_str().unwrap_or(""))
        .map_err(|_| invalid())?;
    let expires = authority_request::parse_utc_z_str(value["expiresAt"].as_str().unwrap_or(""))
        .map_err(|_| invalid())?;
    if issued > now + 30
        || expires <= now
        || expires <= issued
        || expires - issued > 300
        || expires > expected["leaseUntil"].as_i64().ok_or_else(invalid)?
        || expires > expected["writerLeaseUntil"].as_i64().ok_or_else(invalid)?
    {
        return Err(invalid());
    }
    let mut unsigned = object.clone();
    unsigned.remove("signature");
    let mut digest_document = unsigned.clone();
    digest_document.remove("digest");
    if value["digest"] != typed_digest(&Value::Object(digest_document))? {
        return Err(invalid());
    }
    let public = URL_SAFE_NO_PAD
        .decode(&config.signer_public_key)
        .map_err(|_| invalid())?;
    let signature = URL_SAFE_NO_PAD
        .decode(value["signature"].as_str().unwrap_or(""))
        .map_err(|_| invalid())?;
    let public: [u8; 32] = public.try_into().map_err(|_| invalid())?;
    let signature = Signature::from_slice(&signature).map_err(|_| invalid())?;
    let mut message = SIGNATURE_DOMAIN.to_vec();
    message.extend(
        authority_request::canonical_json_bytes(&Value::Object(unsigned)).map_err(|_| invalid())?,
    );
    VerifyingKey::from_bytes(&public)
        .map_err(|_| invalid())?
        .verify_strict(&message, &signature)
        .map_err(|_| invalid())?;
    Ok(value)
}

#[cfg(test)]
mod node_dispatch_tests {
    use super::*;
    use deepseek_protocol::generated::deepseek::{
        agent::v1::{AgentArtifactReference, AgentNodeDispatchIntent},
        common::v1::ActionFence,
    };

    fn fixture(
        phase: AgentRunExecutionPhase,
    ) -> (AgentRunExecutionClaim, BeginAgentNodeDispatchResponse) {
        let phase_name = if phase == AgentRunExecutionPhase::Plan {
            "plan"
        } else {
            "tasks"
        };
        let identity = format!("agent-run-execution-v1\nrun_rpc\n{phase_name}\n4\n");
        let action_id = format!("agent-exec-{:x}", Sha256::digest(identity.as_bytes()));
        let admitted = AgentRunExecutionClaim {
            fence: Some(ActionFence {
                action_id,
                execution_epoch: 1,
            }),
            run_id: "run_rpc".into(),
            phase: phase as i32,
            request: Some(AgentArtifactReference {
                sha256: "b".repeat(64),
                length: 100,
            }),
            plan_digest: "a".repeat(64),
            metadata_index: 4,
            metadata_epoch: 3,
            owner: "rust-agent-worker".into(),
            claim_token: "fixture-claim-token".into(),
            lease_until: 1060,
            claim_revision: 1,
            writer_fencing_token: 1,
            writer_lease_until: 1200,
            state: "CLAIMED".into(),
            reconciliation_required: false,
        };
        let mut live = admitted.clone();
        live.state = "EXECUTING".into();
        let intent = AgentNodeDispatchIntent {
            schema_version: 1,
            operation_id: format!("agent-node-{}", "c".repeat(64)),
            fence: live.fence.clone(),
            run_id: live.run_id.clone(),
            node_id: if phase == AgentRunExecutionPhase::Plan {
                "leader"
            } else {
                "coder"
            }
            .into(),
            phase: live.phase,
            task: "write the implementation".into(),
            depends_on: vec![],
            request: live.request.clone(),
            plan_digest: live.plan_digest.clone(),
            metadata_index: live.metadata_index,
            metadata_epoch: live.metadata_epoch,
            state: "INTENT_RECORDED".into(),
            recorded_at: 1000,
        };
        (
            admitted,
            BeginAgentNodeDispatchResponse {
                fresh: true,
                intent: Some(intent),
                claim: Some(live),
            },
        )
    }

    #[test]
    fn node_dispatch_scope_preserves_identity_across_execution_and_renewal() {
        for phase in [AgentRunExecutionPhase::Plan, AgentRunExecutionPhase::Tasks] {
            let (admitted, mut response) = fixture(phase);
            let role = response.intent.as_ref().unwrap().node_id.clone();
            validate_node_dispatch(&response, &admitted, &role, 1000).unwrap();
            response.claim.as_mut().unwrap().lease_until = 1100;
            response.claim.as_mut().unwrap().writer_lease_until = 1300;
            response.fresh = false;
            validate_node_dispatch(&response, &admitted, &role, 1000).unwrap();
            let mut executing = admitted;
            executing.state = "EXECUTING".into();
            validate_node_dispatch(&response, &executing, &role, 1000).unwrap();
        }
    }

    #[test]
    fn node_dispatch_scope_rejects_corrupt_or_rebound_intents() {
        type Change = fn(&mut BeginAgentNodeDispatchResponse);
        let changes: &[(&str, Change)] = &[
            ("missing claim", |r| r.claim = None),
            ("missing intent", |r| r.intent = None),
            ("schema", |r| r.intent.as_mut().unwrap().schema_version = 2),
            ("fence", |r| r.intent.as_mut().unwrap().fence = None),
            ("epoch", |r| {
                r.intent
                    .as_mut()
                    .unwrap()
                    .fence
                    .as_mut()
                    .unwrap()
                    .execution_epoch += 1
            }),
            ("run", |r| {
                r.intent.as_mut().unwrap().run_id = "other".into()
            }),
            ("node", |r| {
                r.intent.as_mut().unwrap().node_id = "critic".into()
            }),
            ("phase", |r| {
                r.intent.as_mut().unwrap().phase = AgentRunExecutionPhase::Plan as i32
            }),
            ("request", |r| r.intent.as_mut().unwrap().request = None),
            ("plan", |r| {
                r.intent.as_mut().unwrap().plan_digest = "d".repeat(64)
            }),
            ("index", |r| r.intent.as_mut().unwrap().metadata_index += 1),
            ("metadata fence", |r| {
                r.intent.as_mut().unwrap().metadata_epoch += 1
            }),
            ("state", |r| {
                r.intent.as_mut().unwrap().state = "CONFIRMED".into()
            }),
            ("future time", |r| {
                r.intent.as_mut().unwrap().recorded_at = 1031
            }),
            ("negative time", |r| {
                r.intent.as_mut().unwrap().recorded_at = -1
            }),
            ("empty task", |r| r.intent.as_mut().unwrap().task.clear()),
            ("oversized task", |r| {
                r.intent.as_mut().unwrap().task = "x".repeat(16 * 1024 + 1)
            }),
            ("operation prefix", |r| {
                r.intent.as_mut().unwrap().operation_id = "c".repeat(64)
            }),
            ("operation digest", |r| {
                r.intent.as_mut().unwrap().operation_id = "agent-node-Z".into()
            }),
            ("self dependency", |r| {
                r.intent.as_mut().unwrap().depends_on = vec!["coder".into()]
            }),
            ("unknown dependency", |r| {
                r.intent.as_mut().unwrap().depends_on = vec!["foreign".into()]
            }),
            ("duplicate dependency", |r| {
                r.intent.as_mut().unwrap().depends_on = vec!["reasoner".into(), "reasoner".into()]
            }),
            ("too many dependencies", |r| {
                r.intent.as_mut().unwrap().depends_on = vec!["reasoner".into(); 5]
            }),
        ];
        let (admitted, response) = fixture(AgentRunExecutionPhase::Tasks);
        for (name, change) in changes {
            let mut changed = response.clone();
            change(&mut changed);
            assert!(
                validate_node_dispatch(&changed, &admitted, "coder", 1000).is_err(),
                "{name}"
            );
        }
    }

    #[test]
    fn node_dispatch_scope_rejects_changed_live_claim_and_expiry() {
        type Change = fn(&mut AgentRunExecutionClaim);
        let changes: &[(&str, Change)] = &[
            ("claim token", |c| c.claim_token = "other".into()),
            ("owner", |c| c.owner = "other".into()),
            ("writer fence", |c| c.writer_fencing_token += 1),
            ("claim revision", |c| c.claim_revision += 1),
            ("metadata index", |c| c.metadata_index += 1),
            ("plan digest", |c| c.plan_digest = "d".repeat(64)),
            ("claimed state", |c| c.state = "CLAIMED".into()),
            ("reconciliation", |c| c.reconciliation_required = true),
            ("action expiry", |c| c.lease_until = 1000),
            ("writer expiry", |c| c.writer_lease_until = 1000),
        ];
        let (admitted, response) = fixture(AgentRunExecutionPhase::Tasks);
        for (name, change) in changes {
            let mut changed = response.clone();
            change(changed.claim.as_mut().unwrap());
            assert!(
                validate_node_dispatch(&changed, &admitted, "coder", 1000).is_err(),
                "{name}"
            );
        }
    }

    #[test]
    fn node_dispatch_scope_accepts_only_phase_roles_and_valid_dependencies() {
        let (admitted, mut response) = fixture(AgentRunExecutionPhase::Tasks);
        response.intent.as_mut().unwrap().depends_on = vec!["researcher".into(), "reasoner".into()];
        validate_node_dispatch(&response, &admitted, "coder", 1000).unwrap();
        response.intent.as_mut().unwrap().node_id = "leader".into();
        assert!(validate_node_dispatch(&response, &admitted, "leader", 1000).is_err());
        let (admitted, mut response) = fixture(AgentRunExecutionPhase::Plan);
        response.intent.as_mut().unwrap().depends_on = vec!["coder".into()];
        assert!(validate_node_dispatch(&response, &admitted, "leader", 1000).is_err());
    }
}
