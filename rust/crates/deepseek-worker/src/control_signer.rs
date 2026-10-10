use crate::{
    Worker, WorkerAuthorityConfig, WorkerRpcService, agent_execution_grant,
    authority_request::{self, canonical_json_bytes},
    operation_grant,
    signature_journal::SignatureJournal,
};
use deepseek_federation::{ControlSigningKey, load_control_signer};
use deepseek_protocol::generated::deepseek::action::v1::{
    ControlSigningPurpose, SignControlRequest, SignControlResponse, StorageConditionType,
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
    key: ControlSigningKey,
    journal: SignatureJournal,
}
impl std::fmt::Debug for ControlSigner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ControlSigner")
            .field("public_key", &self.key.public_key())
            .finish()
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
    Ok(Some(ControlSigner { key, journal }))
}
fn request_error() -> Status {
    Status::invalid_argument("CONTROL_SIGNER_REQUEST_INVALID")
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
