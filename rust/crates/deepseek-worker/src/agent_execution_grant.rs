//! Go owns execution leases; Rust owns issuance and verification of effect grants.
use crate::{WorkerAuthorityConfig, authority_request};
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentRunExecutionClaim, AgentRunExecutionPhase, RenewAgentRunExecutionRequest,
    agent_run_control_client::AgentRunControlClient,
};
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
