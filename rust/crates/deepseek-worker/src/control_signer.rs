use crate::{
    Worker, WorkerAuthorityConfig, WorkerRpcService, agent_execution_grant,
    authority_request::{self, canonical_json_bytes},
    operation_grant,
    signature_journal::SignatureJournal,
};
use deepseek_federation::{ControlSigningKey, load_control_signer};
use deepseek_protocol::generated::deepseek::action::v1::{
    ControlSignerBindingRequest, ControlSignerBindingResponse, ControlSigningPurpose,
    SignControlRequest, SignControlResponse, StorageConditionType,
    control_signer_bootstrap_server::ControlSignerBootstrap as BootstrapRpc,
    control_signer_server::ControlSigner as SignerRpc,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    env::VarError,
    fs,
    io::{self, Read},
    path::Path,
};
use tonic::{Request, Response, Status};
use zeroize::Zeroizing;

pub const BUNDLE_FILE: &str = "DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE";
pub const PASSPHRASE_FILE: &str = "DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE";
pub struct ControlSigner {
    pub(crate) key: ControlSigningKey,
    journal: SignatureJournal,
    pub(crate) native_origin: Option<crate::native_origin::NativeOriginCustody>,
}

#[cfg(test)]
#[path = "node_outcome_signer_tests.rs"]
mod node_outcome_tests;
impl std::fmt::Debug for ControlSigner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ControlSigner")
            .field("public_key", &self.key.public_key())
            .finish()
    }
}

impl ControlSigner {
    pub(crate) fn sign_recovery_progress(
        &mut self,
        evidence: deepseek_protocol::generated::deepseek::agent::v1::AgentRecoveryProgressEvidence,
    ) -> Result<
        deepseek_protocol::generated::deepseek::agent::v1::AgentRecoveryProgressReceipt,
        Status,
    > {
        use deepseek_protocol::generated::deepseek::agent::v1::AgentRecoveryProgressReceipt;
        use prost::Message as _;
        let payload = evidence.encode_to_vec();
        let signature = self
            .key
            .sign_agent_recovery_progress(&evidence)
            .map_err(|_| Status::failed_precondition("AGENT_RECOVERY_PROGRESS_INVALID"))?;
        let receipt = AgentRecoveryProgressReceipt {
            schema_version: 1,
            evidence: Some(evidence),
            signature,
            signer_public_key: self.key.public_key(),
        };
        let bytes = receipt.encode_to_vec();
        let mut identity = b"deepseek-infra:agent-recovery-progress:request\0".to_vec();
        identity.extend(&payload);
        let request_id = format!("{:x}", Sha256::digest(identity));
        let mut nonce = b"deepseek-infra:agent-recovery-progress:nonce\0".to_vec();
        nonce.extend(&payload);
        let nonce = format!("{:x}", Sha256::digest(nonce));
        if let Some(prior) = self.journal.existing(&request_id)? {
            if prior != bytes {
                return Err(Status::data_loss("AGENT_RECOVERY_PROGRESS_CORRUPT"));
            }
        } else {
            self.journal.record(&request_id, &nonce, &bytes)?;
        }
        Ok(receipt)
    }

    /// Only accept an existing original receipt. Recovery must not recreate a
    /// missing signature journal entry by calling the signing path again.
    pub(crate) fn verify_retained_node_outcome(
        &self,
        receipt: &deepseek_protocol::generated::deepseek::agent::v1::AgentNodeOutcomeReceipt,
    ) -> Result<(), Status> {
        use prost::Message as _;
        let denied = || Status::failed_precondition("AGENT_RECOVERY_RECEIPT_NOT_RETAINED");
        let evidence = receipt.evidence.as_ref().ok_or_else(denied)?;
        if receipt.schema_version != 1
            || receipt.signature.len() != 64
            || receipt.signer_public_key != self.key.public_key()
        {
            return Err(denied());
        }
        let mut identity = b"deepseek-infra:agent-node-outcome-receipt-v1:request\0".to_vec();
        identity.extend(evidence.encode_to_vec());
        let request_id = format!("{:x}", Sha256::digest(&identity));
        if self.journal.existing(&request_id)?.as_deref()
            != Some(receipt.encode_to_vec().as_slice())
        {
            return Err(denied());
        }
        Ok(())
    }

    /// Read the already published proof without entering any signing/record path.
    /// Go supplies the digest of its original retained document, not a new scope.
    pub(crate) fn retained_node_outcome(
        &self,
        evidence: &deepseek_protocol::generated::deepseek::agent::v1::AgentNodeOutcomeEvidence,
        expected_receipt_sha256: &str,
    ) -> Result<deepseek_protocol::generated::deepseek::agent::v1::AgentNodeOutcomeReceipt, Status>
    {
        use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
        use deepseek_protocol::generated::deepseek::agent::v1::AgentNodeOutcomeReceipt;
        use ed25519_dalek::{Signature, VerifyingKey};
        use prost::Message as _;
        let denied = || Status::failed_precondition("AGENT_RECOVERY_RECEIPT_NOT_RETAINED");
        if expected_receipt_sha256.len() != 64
            || !expected_receipt_sha256
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(denied());
        }
        let payload = evidence.encode_to_vec();
        let mut identity = b"deepseek-infra:agent-node-outcome-receipt-v1:request\0".to_vec();
        identity.extend(&payload);
        let request_id = format!("{:x}", Sha256::digest(identity));
        let document = self.journal.existing(&request_id)?.ok_or_else(denied)?;
        if document.is_empty()
            || document.len() > 1024 * 1024
            || format!("{:x}", Sha256::digest(&document)) != expected_receipt_sha256
        {
            return Err(denied());
        }
        let receipt = AgentNodeOutcomeReceipt::decode(document.as_slice()).map_err(|_| denied())?;
        if receipt.encode_to_vec() != document
            || receipt.schema_version != 1
            || receipt.evidence.as_ref() != Some(evidence)
            || receipt.signer_public_key != self.key.public_key()
        {
            return Err(denied());
        }
        let public: [u8; 32] = URL_SAFE_NO_PAD
            .decode(&receipt.signer_public_key)
            .map_err(|_| denied())?
            .try_into()
            .map_err(|_| denied())?;
        let key = VerifyingKey::from_bytes(&public).map_err(|_| denied())?;
        let signature = Signature::from_slice(&receipt.signature).map_err(|_| denied())?;
        let mut message = b"deepseek-infra:agent-node-outcome-proof-v1\0".to_vec();
        message.extend(payload);
        key.verify_strict(&message, &signature)
            .map_err(|_| denied())?;
        self.verify_retained_node_outcome(&receipt)?;
        Ok(receipt)
    }

    pub(crate) fn prepare_agent_credentials(
        &self,
        root: &Path,
        scope: &deepseek_federation::AgentCredentialScope,
        credentials: &deepseek_federation::AgentProviderCredentials,
    ) -> Result<deepseek_federation::AgentCredentialObject, Status> {
        deepseek_federation::AgentCredentialVault::open(root)
            .and_then(|vault| vault.put(&self.key, scope, credentials))
            .map_err(|_| Status::failed_precondition("AGENT_CREDENTIAL_CUSTODY_DENIED"))
    }

    pub(crate) fn open_agent_credentials(
        &self,
        root: &Path,
        scope: &deepseek_federation::AgentCredentialScope,
        object: &deepseek_federation::AgentCredentialObject,
    ) -> Result<deepseek_federation::AgentProviderCredentials, Status> {
        deepseek_federation::AgentCredentialVault::open(root)
            .and_then(|vault| vault.get(&self.key, scope, object))
            .map_err(|_| Status::failed_precondition("AGENT_CREDENTIAL_CUSTODY_DENIED"))
    }

    pub(crate) fn agent_credential_binding(&self) -> Result<(String, String), Status> {
        let binding = self.key.binding();
        let fleet = binding["fleetId"].as_str();
        let environment = binding["environment"].as_str();
        match (fleet, environment) {
            (Some(fleet), Some(environment)) => Ok((fleet.into(), environment.into())),
            _ => Err(Status::failed_precondition(
                "AGENT_CREDENTIAL_CUSTODY_DENIED",
            )),
        }
    }

    pub(crate) fn sign_failure_projection(
        &mut self,
        evidence: deepseek_protocol::generated::deepseek::agent::v1::AgentFailureProjectionEvidence,
    ) -> Result<
        deepseek_protocol::generated::deepseek::agent::v1::AgentFailureProjectionReceipt,
        Status,
    > {
        use deepseek_protocol::generated::deepseek::agent::v1::AgentFailureProjectionReceipt;
        use prost::Message as _;
        let payload = evidence.encode_to_vec();
        let signature = self
            .key
            .sign_agent_failure_projection(&evidence)
            .map_err(|_| Status::failed_precondition("AGENT_FAILURE_PROJECTION_INVALID"))?;
        let receipt = AgentFailureProjectionReceipt {
            schema_version: 1,
            evidence: Some(evidence),
            signature,
            signer_public_key: self.key.public_key(),
        };
        let bytes = receipt.encode_to_vec();
        let mut request = b"deepseek-infra:agent-failure-projection:request\0".to_vec();
        request.extend(&payload);
        let request_id = format!("{:x}", Sha256::digest(&request));
        let mut nonce = b"deepseek-infra:agent-failure-projection:nonce\0".to_vec();
        nonce.extend(&payload);
        let nonce = format!("{:x}", Sha256::digest(&nonce));
        if let Some(prior) = self.journal.existing(&request_id)? {
            if prior != bytes {
                return Err(Status::data_loss("AGENT_FAILURE_PROJECTION_CORRUPT"));
            }
        } else {
            self.journal.record(&request_id, &nonce, &bytes)?;
        }
        Ok(receipt)
    }
    pub(crate) fn sign_synthesis_projection(
        &mut self,
        evidence: deepseek_protocol::generated::deepseek::agent::v1::AgentSynthesisProjectionEvidence,
    ) -> Result<
        deepseek_protocol::generated::deepseek::agent::v1::AgentSynthesisProjectionReceipt,
        Status,
    > {
        use deepseek_protocol::generated::deepseek::agent::v1::AgentSynthesisProjectionReceipt;
        use prost::Message as _;
        let payload = evidence.encode_to_vec();
        let signature = self
            .key
            .sign_agent_synthesis_projection(&evidence)
            .map_err(|_| Status::failed_precondition("AGENT_SYNTHESIS_PROJECTION_INVALID"))?;
        let receipt = AgentSynthesisProjectionReceipt {
            schema_version: 1,
            evidence: Some(evidence),
            signature,
            signer_public_key: self.key.public_key(),
        };
        let bytes = receipt.encode_to_vec();
        let mut request = b"deepseek-infra:agent-synthesis-projection:request\0".to_vec();
        request.extend(&payload);
        let request_id = format!("{:x}", Sha256::digest(&request));
        let mut nonce = b"deepseek-infra:agent-synthesis-projection:nonce\0".to_vec();
        nonce.extend(&payload);
        let nonce = format!("{:x}", Sha256::digest(&nonce));
        if let Some(prior) = self.journal.existing(&request_id)? {
            if prior != bytes {
                return Err(Status::data_loss("AGENT_SYNTHESIS_PROJECTION_CORRUPT"));
            }
        } else {
            self.journal.record(&request_id, &nonce, &bytes)?;
        }
        Ok(receipt)
    }
    pub(crate) fn sign_plan_projection(
        &mut self,
        evidence: deepseek_protocol::generated::deepseek::agent::v1::AgentPlanProjectionEvidence,
    ) -> Result<deepseek_protocol::generated::deepseek::agent::v1::AgentPlanProjectionReceipt, Status>
    {
        use deepseek_protocol::generated::deepseek::agent::v1::AgentPlanProjectionReceipt;
        use prost::Message as _;
        let payload = evidence.encode_to_vec();
        let signature = self
            .key
            .sign_agent_plan_projection(&evidence)
            .map_err(|_| Status::failed_precondition("AGENT_PLAN_PROJECTION_INVALID"))?;
        let receipt = AgentPlanProjectionReceipt {
            schema_version: 1,
            evidence: Some(evidence),
            signature,
            signer_public_key: self.key.public_key(),
        };
        let bytes = receipt.encode_to_vec();
        let mut request = b"deepseek-infra:agent-plan-projection:request\0".to_vec();
        request.extend(&payload);
        let request_id = format!("{:x}", Sha256::digest(&request));
        let mut nonce = b"deepseek-infra:agent-plan-projection:nonce\0".to_vec();
        nonce.extend(&payload);
        let nonce = format!("{:x}", Sha256::digest(&nonce));
        if let Some(prior) = self.journal.existing(&request_id)? {
            if prior != bytes {
                return Err(Status::data_loss("AGENT_PLAN_PROJECTION_CORRUPT"));
            }
        } else {
            self.journal.record(&request_id, &nonce, &bytes)?;
        }
        Ok(receipt)
    }
    pub(crate) fn sign_node_outcome(
        &mut self,
        evidence: deepseek_protocol::generated::deepseek::agent::v1::AgentNodeOutcomeEvidence,
    ) -> Result<deepseek_protocol::generated::deepseek::agent::v1::AgentNodeOutcomeReceipt, Status>
    {
        use deepseek_protocol::generated::deepseek::agent::v1::AgentNodeOutcomeReceipt;
        use prost::Message as _;
        let payload = evidence.encode_to_vec();
        let signature = self
            .key
            .sign_agent_node_outcome(&evidence)
            .map_err(|_| Status::failed_precondition("AGENT_NODE_OUTCOME_INVALID"))?;
        let receipt = AgentNodeOutcomeReceipt {
            schema_version: 1,
            evidence: Some(evidence),
            signature,
            signer_public_key: self.key.public_key(),
        };
        let bytes = receipt.encode_to_vec();
        let mut identity = b"deepseek-infra:agent-node-outcome-receipt-v1:request\0".to_vec();
        identity.extend(&payload);
        let request_id = format!("{:x}", Sha256::digest(&identity));
        let mut nonce = b"deepseek-infra:agent-node-outcome-receipt-v1:nonce\0".to_vec();
        nonce.extend(&payload);
        let nonce = format!("{:x}", Sha256::digest(&nonce));
        if let Some(prior) = self.journal.existing(&request_id)? {
            if prior != bytes {
                return Err(Status::failed_precondition(
                    "AGENT_NODE_OUTCOME_RECEIPT_CORRUPT",
                ));
            }
        } else {
            self.journal.record(&request_id, &nonce, &bytes)?;
        }
        Ok(receipt)
    }
}
fn invalid() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidInput,
        "worker control signer configuration rejected",
    )
}

pub(crate) fn read_bounded(path: &Path, limit: u64) -> io::Result<Vec<u8>> {
    let metadata = fs::symlink_metadata(path).map_err(|_| invalid())?;
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.len() == 0
        || metadata.len() > limit
    {
        return Err(invalid());
    }
    let mut bytes = Vec::new();
    fs::File::open(path)
        .map_err(|_| invalid())?
        .take(limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| invalid())?;
    if bytes.is_empty() || bytes.len() as u64 > limit {
        return Err(invalid());
    }
    Ok(bytes)
}
pub fn load_control_signer_from_env(
    get: impl Fn(&str) -> Result<String, VarError>,
    config: Option<&WorkerAuthorityConfig>,
    state_root: Option<&Path>,
    tls: bool,
) -> io::Result<Option<ControlSigner>> {
    let read = |name| match get(name) {
        Ok(value) if !value.is_empty() => Ok(Some(value)),
        Err(VarError::NotPresent) => Ok(None),
        _ => Err(invalid()),
    };
    let (bundle_path, password_path) = (read(BUNDLE_FILE)?, read(PASSPHRASE_FILE)?);
    if bundle_path.is_none() && password_path.is_none() {
        return Ok(None);
    }
    let (Some(bundle_path), Some(password_path), Some(config), Some(root)) =
        (bundle_path, password_path, config, state_root)
    else {
        return Err(invalid());
    };
    if !tls {
        return Err(invalid());
    }
    let password = Zeroizing::new(read_bounded(Path::new(&password_path), 1024)?);
    let bundle: Value = serde_json::from_slice(&read_bounded(Path::new(&bundle_path), 64 * 1024)?)
        .map_err(|_| invalid())?;
    let key = load_control_signer(
        &bundle,
        &password,
        &config.fleet_id,
        &config.environment,
        &config.signer_public_key,
    )
    .map_err(|_| invalid())?;
    let bound = canonical_json_bytes(key.binding()).map_err(|_| invalid())?;
    let journal_binding = String::from_utf8(bound).map_err(|_| invalid())?;
    let journal = SignatureJournal::open(root, &journal_binding).map_err(|_| invalid())?;
    let native_origin = match get("DEEPSEEK_WORKER_MANAGED_ROOT") {
        Ok(path) => crate::native_origin::NativeOriginCustody::load(Path::new(&path), &key)?,
        Err(VarError::NotPresent) => None,
        Err(_) => return Err(invalid()),
    };
    Ok(Some(ControlSigner {
        key,
        journal,
        native_origin,
    }))
}
fn request_error() -> Status {
    Status::invalid_argument("CONTROL_SIGNER_REQUEST_INVALID")
}

#[tonic::async_trait]
impl BootstrapRpc for WorkerRpcService {
    async fn get_binding(
        &self,
        request: Request<ControlSignerBindingRequest>,
    ) -> Result<Response<ControlSignerBindingResponse>, Status> {
        let identity = self
            .authenticator
            .authenticate(request.metadata())
            .map_err(|error| Status::unauthenticated(error.code()))?;
        if identity.service_name != "go-control-plane" || identity.role != "controller" {
            return Err(Status::permission_denied("CONTROL_SIGNER_CALLER_DENIED"));
        }
        let input = request.into_inner();
        if input.schema_version != 1
            || input.fleet_id.is_empty()
            || input.environment.is_empty()
            || input.fencing_token == 0
            || input.fencing_token > i64::MAX as u64
        {
            return Err(Status::invalid_argument("CONTROL_SIGNER_BINDING_INVALID"));
        }
        let signer = self
            .control_signer
            .as_ref()
            .ok_or_else(|| Status::unavailable("CONTROL_SIGNER_UNAVAILABLE"))?;
        // Keep the existing worker -> signer lock order. This reads already
        // loaded public metadata and never signs, admits, or writes a journal.
        let worker = self.worker.lock().await;
        let authority = worker
            .authority
            .as_ref()
            .ok_or_else(|| Status::failed_precondition("CONTROL_SIGNER_BINDING_UNAVAILABLE"))?;
        let signer = signer.lock().await;
        let public = signer.key.public_key();
        if input.fleet_id != authority.fleet_id
            || input.environment != authority.environment
            || input.fencing_token as i64 != authority.fencing_token
            || public != authority.signer_public_key
            || (!input.expected_signer_public_key.is_empty()
                && input.expected_signer_public_key != public)
        {
            return Err(Status::failed_precondition(
                "CONTROL_SIGNER_BINDING_MISMATCH",
            ));
        }
        Ok(Response::new(ControlSignerBindingResponse {
            schema_version: 1,
            signer_public_key: public,
            signer_key_id: authority.signer_key_id.clone(),
            fleet_id: authority.fleet_id.clone(),
            environment: authority.environment.clone(),
            fencing_token: authority.fencing_token as u64,
        }))
    }
}
fn digest(value: &Value) -> Result<String, Status> {
    Ok(format!(
        "sha256:{:x}",
        Sha256::digest(canonical_json_bytes(value).map_err(|_| request_error())?)
    ))
}

impl ControlSigner {
    fn sign(
        &mut self,
        mut input: SignControlRequest,
        worker: &Worker,
        live_agent_claim: Option<
            &deepseek_protocol::generated::deepseek::agent::v1::AgentRunExecutionClaim,
        >,
    ) -> Result<SignControlResponse, Status> {
        if input.purpose == ControlSigningPurpose::AgentExecution as i32 {
            return self.sign_agent(input, worker, live_agent_claim.ok_or_else(request_error)?);
        }
        let agent_install = input.purpose == ControlSigningPurpose::AgentInstallEpoch as i32;
        if agent_install {
            let claim = live_agent_claim.ok_or_else(request_error)?;
            let now = authority_request::utc_z_now().map_err(|_| request_error())?;
            let seconds = authority_request::parse_utc_z_str(&now).map_err(|_| request_error())?;
            agent_execution_grant::validate_epoch_claim(claim, seconds)?;
            if input.agent_intent.is_none()
                || input.storage_intent.is_some()
                || input.fence.as_ref() != claim.fence.as_ref()
                || input.revision != claim.claim_revision
                || input.fencing_token > i64::MAX as u64
                || input.fencing_token as i64 != claim.writer_fencing_token
            {
                return Err(request_error());
            }
            // The existing frozen authority-request schema is preserved. Only
            // this typed issuance path may sign an Agent epoch, after Go renewal.
            input.purpose = ControlSigningPurpose::InstallEpoch as i32;
            input.agent_intent = None;
            if input.issued_at.is_empty()
                && input.expires_at.is_empty()
                && self.journal.existing(&input.request_id)?.is_none()
            {
                input.issued_at = now;
                input.expires_at = authority_request::format_utc_z(
                    (seconds + 300)
                        .min(claim.lease_until)
                        .min(claim.writer_lease_until),
                )
                .map_err(|_| request_error())?;
            }
        } else if input.agent_intent.is_some() || live_agent_claim.is_some() {
            return Err(request_error());
        }
        if !agent_install
            && input
                .fence
                .as_ref()
                .is_some_and(|fence| fence.action_id.starts_with("agent-exec-"))
        {
            return Err(Status::failed_precondition(
                "AGENT_EPOCH_AUTHORITY_REQUIRED",
            ));
        }
        // Automatic timestamps retain the original receipt on an exact retry.
        // An expired receipt is refused; retry never silently extends authority.
        if input.issued_at.is_empty() && input.expires_at.is_empty() {
            if let Some(raw) = self.journal.existing(&input.request_id)? {
                let prior: Value = serde_json::from_slice(&raw)
                    .map_err(|_| Status::failed_precondition("CONTROL_SIGNER_JOURNAL_INVALID"))?;
                input.issued_at = prior["issuedAt"].as_str().ok_or_else(request_error)?.into();
                input.expires_at = prior["expiresAt"]
                    .as_str()
                    .ok_or_else(request_error)?
                    .into();
            } else {
                input.issued_at = authority_request::utc_z_now().map_err(|_| request_error())?;
                let seconds = authority_request::parse_utc_z_str(&input.issued_at)
                    .map_err(|_| request_error())?;
                input.expires_at =
                    authority_request::format_utc_z(seconds + 300).map_err(|_| request_error())?;
            }
        }
        if agent_install {
            let claim = live_agent_claim.ok_or_else(request_error)?;
            let expiry = authority_request::parse_utc_z_str(&input.expires_at)
                .map_err(|_| request_error())?;
            if expiry > claim.lease_until || expiry > claim.writer_lease_until {
                return Err(Status::failed_precondition("AGENT_EPOCH_LEASE_EXCEEDED"));
            }
        }
        let fence = input.fence.as_ref().ok_or_else(request_error)?;
        deepseek_protocol::validate_fence(fence)
            .map_err(|e| Status::failed_precondition(e.code()))?;
        let authority = worker
            .authority
            .as_ref()
            .ok_or_else(|| Status::failed_precondition("CONTROL_SIGNER_AUTHORITY_MISSING"))?;
        if input.fencing_token != authority.fencing_token as u64
            || input.fleet_id != authority.fleet_id
            || input.environment != authority.environment
            || self.key.public_key() != authority.signer_public_key
            || input.fencing_token > i64::MAX as u64
            || fence.execution_epoch > i64::MAX as u64
        {
            return Err(Status::failed_precondition(
                "CONTROL_SIGNER_BINDING_MISMATCH",
            ));
        }
        let purpose =
            ControlSigningPurpose::try_from(input.purpose).map_err(|_| request_error())?;
        let (schema, operation, payload) = match purpose {
            ControlSigningPurpose::InstallEpoch if input.storage_intent.is_none() => (
                authority_request::AUTHORITY_REQUEST_SCHEMA,
                "install-epoch",
                json!({}),
            ),
            ControlSigningPurpose::StoragePut => {
                worker
                    .admit(fence)
                    .map_err(|e| Status::failed_precondition(e.code()))?;
                let intent = input.storage_intent.as_ref().ok_or_else(request_error)?;
                if intent.fence.as_ref() != Some(fence)
                    || !intent.payload.is_empty()
                    || !intent.canonical_authorization.is_empty()
                    || intent.schema_version != 1
                    || intent.expected_length > i64::MAX as u64
                    || intent.request_id != input.request_id
                    || intent.nonce != input.nonce
                {
                    return Err(request_error());
                }
                let condition = intent.precondition.as_ref().ok_or_else(request_error)?;
                let condition_type = match StorageConditionType::try_from(condition.condition_type)
                {
                    Ok(StorageConditionType::CreateOnly) => "CREATE_ONLY",
                    Ok(StorageConditionType::IfMatch) => "IF_MATCH",
                    _ => return Err(request_error()),
                };
                (
                    operation_grant::STORAGE_OPERATION_GRANT_SCHEMA,
                    "execute-storage-put",
                    json!({"mutationType":intent.mutation_type,"provider":intent.provider,"targetIdentity":intent.target_identity,"bucket":intent.bucket,"prefix":intent.prefix,"objectKey":intent.object_key,"objectDigest":intent.payload_digest,"expectedLength":intent.expected_length,"conditionType":condition_type,"expectedEtag":condition.expected_etag,"claimRevision":input.revision}),
                )
            }
            _ => return Err(request_error()),
        };
        let mut document = json!({"schema":schema,"schemaVersion":1,"domain":"action","operation":operation,"runtime":"go","mode":"shadow","fleetId":input.fleet_id,"environment":input.environment,"role":"control-plane","fencingToken":input.fencing_token,"actionId":fence.action_id,"executionEpoch":fence.execution_epoch,"revision":input.revision,"issuedAt":input.issued_at,"expiresAt":input.expires_at,"requestId":input.request_id,"nonce":input.nonce,"payload":payload,"signerKeyId":authority.signer_key_id,"signatureAlgorithm":"Ed25519"});
        if let Some(intent) = &input.storage_intent {
            document["operationId"] = Value::from(intent.operation_id.clone());
        }
        document["payloadDigest"] = Value::from(digest(&document["payload"])?);
        document["digest"] = Value::from(digest(&document)?);
        document["signature"] = Value::from(
            self.key
                .sign_control_document(&document)
                .map_err(|_| request_error())?,
        );
        let raw = canonical_json_bytes(&document).map_err(|_| request_error())?;
        // Use the same frozen full verifiers as execution, including scope,
        // integer bounds, identifier grammar, secrecy, timestamps and lifetime.
        let now = authority_request::utc_z_now().map_err(|_| request_error())?;
        if purpose == ControlSigningPurpose::InstallEpoch {
            if matches!(
                worker.admit(fence),
                Err(deepseek_protocol::AdmitError::StaleEpoch)
            ) {
                return Err(Status::failed_precondition("STALE_EXECUTION_EPOCH"));
            }
            authority_request::verify_authority_request_document(
                &raw,
                &crate::AuthorityRequestContext {
                    now: &now,
                    signer_public_key: &authority.signer_public_key,
                    signer_key_id: &authority.signer_key_id,
                    expected_domain: "action",
                    expected_operation: operation,
                    expected_runtime: "go",
                    expected_mode: "shadow",
                    expected_fleet_id: &authority.fleet_id,
                    expected_environment: &authority.environment,
                    expected_role: "control-plane",
                    current_fencing_token: authority.fencing_token,
                    live_epoch: 0,
                    seen_request_ids: HashSet::new(),
                    seen_nonces: HashSet::new(),
                    max_future_skew_seconds: 30,
                },
            )
            .map_err(|e| Status::invalid_argument(e.code))?;
        } else {
            operation_grant::verify_storage_operation_grant(
                &raw,
                &crate::StorageOperationGrantContext {
                    now: &now,
                    signer_public_key: &authority.signer_public_key,
                    signer_key_id: &authority.signer_key_id,
                    expected_domain: "action",
                    expected_operation: operation,
                    expected_runtime: "go",
                    expected_mode: "shadow",
                    expected_fleet_id: &authority.fleet_id,
                    expected_environment: &authority.environment,
                    expected_role: "control-plane",
                    current_fencing_token: authority.fencing_token,
                    live_epoch: fence.execution_epoch as i64,
                    seen_request_ids: HashSet::new(),
                    seen_nonces: HashSet::new(),
                    seen_operation_digests: HashMap::new(),
                    max_future_skew_seconds: 30,
                },
            )
            .map_err(|e| Status::invalid_argument(e.code))?;
        }
        self.journal.record(&input.request_id, &input.nonce, &raw)?;
        Ok(SignControlResponse {
            fence: input.fence,
            canonical_document: raw,
            signer_public_key: authority.signer_public_key.clone(),
            signer_key_id: authority.signer_key_id.clone(),
        })
    }

    fn sign_agent(
        &mut self,
        mut input: SignControlRequest,
        worker: &Worker,
        claim: &deepseek_protocol::generated::deepseek::agent::v1::AgentRunExecutionClaim,
    ) -> Result<SignControlResponse, Status> {
        let now = authority_request::utc_z_now().map_err(|_| request_error())?;
        let seconds = authority_request::parse_utc_z_str(&now).map_err(|_| request_error())?;
        agent_execution_grant::validate_claim(claim, seconds)?;
        let fence = input.fence.as_ref().ok_or_else(request_error)?;
        worker
            .admit(fence)
            .map_err(|e| Status::failed_precondition(e.code()))?;
        let authority = worker.authority.as_ref().ok_or_else(request_error)?;
        if claim.fence.as_ref() != Some(fence)
            || input.storage_intent.is_some()
            || input.revision != claim.claim_revision
            || input.fencing_token > i64::MAX as u64
            || input.fencing_token as i64 != claim.writer_fencing_token
            || input.fencing_token as i64 != authority.fencing_token
            || input.fleet_id != authority.fleet_id
            || input.environment != authority.environment
            || self.key.public_key() != authority.signer_public_key
        {
            return Err(request_error());
        }
        let config = WorkerAuthorityConfig {
            signer_public_key: authority.signer_public_key.clone(),
            fleet_id: authority.fleet_id.clone(),
            environment: authority.environment.clone(),
            fencing_token: authority.fencing_token,
            now: None,
        };
        let context = crate::AgentExecutionGrantContext {
            now: &now,
            authority: &config,
            claim,
        };
        // Exact retries retain the original dates and receipt. Revalidate the
        // current Go lease first; neither a retry nor a renewal extends a grant.
        if let Some(raw) = self.journal.existing(&input.request_id)? {
            let prior = crate::verify_agent_execution_grant(&raw, &context)?;
            if prior["nonce"] != input.nonce
                || (!input.issued_at.is_empty() && prior["issuedAt"] != input.issued_at)
                || (!input.expires_at.is_empty() && prior["expiresAt"] != input.expires_at)
            {
                return Err(request_error());
            }
            return Ok(SignControlResponse {
                fence: input.fence,
                canonical_document: raw,
                signer_public_key: authority.signer_public_key.clone(),
                signer_key_id: authority.signer_key_id.clone(),
            });
        }
        if input.issued_at.is_empty() && input.expires_at.is_empty() {
            input.issued_at = now.clone();
            input.expires_at = authority_request::format_utc_z(
                (seconds + 300)
                    .min(claim.lease_until)
                    .min(claim.writer_lease_until),
            )
            .map_err(|_| request_error())?;
        }
        let payload = agent_execution_grant::claim_payload(claim)?;
        let mut document = json!({"schema":crate::AGENT_EXECUTION_GRANT_SCHEMA,"schemaVersion":1,
            "domain":"action","operation":"execute-agent-phase","runtime":"go","mode":"authoritative",
            "fleetId":input.fleet_id,"environment":input.environment,"role":"control-plane",
            "fencingToken":input.fencing_token,"actionId":fence.action_id,"executionEpoch":fence.execution_epoch,
            "revision":input.revision,"issuedAt":input.issued_at,"expiresAt":input.expires_at,
            "requestId":input.request_id,"nonce":input.nonce,"payload":payload,
            "signerKeyId":authority.signer_key_id,"signatureAlgorithm":"Ed25519"});
        document["payloadDigest"] = digest(&document["payload"])?.into();
        document["digest"] = digest(&document)?.into();
        document["signature"] = self
            .key
            .sign_control_document(&document)
            .map_err(|_| request_error())?
            .into();
        let raw = canonical_json_bytes(&document).map_err(|_| request_error())?;
        crate::verify_agent_execution_grant(&raw, &context)?;
        self.journal.record(&input.request_id, &input.nonce, &raw)?;
        Ok(SignControlResponse {
            fence: input.fence,
            canonical_document: raw,
            signer_public_key: authority.signer_public_key.clone(),
            signer_key_id: authority.signer_key_id.clone(),
        })
    }
}

#[tonic::async_trait]
impl SignerRpc for WorkerRpcService {
    async fn sign_control(
        &self,
        request: Request<SignControlRequest>,
    ) -> Result<Response<SignControlResponse>, Status> {
        let identity = self
            .authenticator
            .authenticate(request.metadata())
            .map_err(|e| Status::unauthenticated(e.code()))?;
        if identity.service_name != "go-control-plane" || identity.role != "controller" {
            return Err(Status::permission_denied("CONTROL_SIGNER_CALLER_DENIED"));
        }
        let signer = self
            .control_signer
            .as_ref()
            .ok_or_else(|| Status::unavailable("CONTROL_SIGNER_UNAVAILABLE"))?;
        let input = request.into_inner();
        let agent_install = input.purpose == ControlSigningPurpose::AgentInstallEpoch as i32;
        let live_claim = if input.purpose == ControlSigningPurpose::AgentExecution as i32
            || agent_install
        {
            let control = self
                .agent_execution_control
                .as_ref()
                .ok_or_else(|| Status::unavailable("AGENT_EXECUTION_CONTROL_UNAVAILABLE"))?;
            let intent = input.agent_intent.as_ref().ok_or_else(request_error)?;
            if input.storage_intent.is_some() || input.fence.as_ref() != intent.fence.as_ref() {
                return Err(request_error());
            }
            let now = authority_request::utc_z_now().map_err(|_| request_error())?;
            let seconds = authority_request::parse_utc_z_str(&now).map_err(|_| request_error())?;
            if agent_install {
                agent_execution_grant::validate_epoch_claim(intent, seconds)?;
            } else {
                agent_execution_grant::validate_claim(intent, seconds)?;
            }
            Some(control.renew(intent).await?)
        } else {
            None
        };
        let worker = self.worker.lock().await;
        let mut signer = signer.lock().await;
        Ok(Response::new(signer.sign(
            input,
            &worker,
            live_claim.as_ref(),
        )?))
    }
}
