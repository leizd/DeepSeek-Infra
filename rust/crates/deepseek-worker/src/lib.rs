use std::collections::{HashMap, HashSet};
use std::env::VarError;

use deepseek_federation::{SignRequest, plan as plan_federation};
use deepseek_proof::{ProofRequest, plan as plan_proof};
use deepseek_protocol::{
    ActionFence, AdmitError, CommandKind, EffectState, admit_command, interpret_remote_outcome,
    is_federation_command, is_transfer_command, validate_authoritative_epoch_update,
    validate_fence,
};
use deepseek_storage::{StorageRequest, plan as plan_storage};
use deepseek_transfer::{TransferRequest, plan as plan_transfer};
#[cfg(feature = "s3")]
use sha2::Digest;

mod authority_request;
mod authority_store;
mod mutation_request;
mod service;

pub use authority_request::{
    AUTHORITY_REQUEST_SCHEMA, AuthorityRequestContext, AuthorityRequestError,
    MAX_AUTHORITY_REQUEST_BYTES, verify_authority_request_document,
};
pub use mutation_request::{
    MAX_MUTATION_REQUEST_BYTES, MUTATION_REQUEST_SCHEMA, MutationRequestContext,
    MutationRequestError, verify_mutation_request_document,
};
pub use service::WorkerRpcService;

const AUTH_SIGNER_PUBLIC_KEY: &str = "DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY";
const AUTH_FLEET_ID: &str = "DEEPSEEK_WORKER_AUTHORITY_FLEET_ID";
const AUTH_ENVIRONMENT: &str = "DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT";
const AUTH_FENCING_TOKEN: &str = "DEEPSEEK_WORKER_AUTHORITY_FENCING_TOKEN";
const AUTH_NOW: &str = "DEEPSEEK_WORKER_AUTHORITY_NOW";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WorkerAuthorityConfig {
    pub signer_public_key: String,
    pub fleet_id: String,
    pub environment: String,
    pub fencing_token: i64,
    pub now: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StorageEffectState {
    Reserved,
    Dispatching,
    Confirmed,
    EffectUnknown,
    Reconciling,
    Rejected,
    Failed,
}

impl StorageEffectState {
    #[allow(non_upper_case_globals)]
    pub const Pending: Self = Self::Reserved;
    #[allow(non_upper_case_globals)]
    pub const Committed: Self = Self::Confirmed;

    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Reserved => "RESERVED",
            Self::Dispatching => "DISPATCHING",
            Self::Confirmed => "CONFIRMED",
            Self::EffectUnknown => "EFFECT_UNKNOWN",
            Self::Reconciling => "RECONCILING",
            Self::Rejected => "REJECTED",
            Self::Failed => "FAILED",
        }
    }

    pub fn from_db_str(s: &str) -> Option<Self> {
        match s {
            "RESERVED" | "PENDING" => Some(Self::Reserved),
            "DISPATCHING" => Some(Self::Dispatching),
            "CONFIRMED" | "COMMITTED" => Some(Self::Confirmed),
            "EFFECT_UNKNOWN" => Some(Self::EffectUnknown),
            "RECONCILING" => Some(Self::Reconciling),
            "REJECTED" => Some(Self::Rejected),
            "FAILED" => Some(Self::Failed),
            _ => None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StorageEffectRecord {
    pub action_id: String,
    pub execution_epoch: u64,
    pub fencing_token: i64,
    pub request_id: String,
    pub nonce: String,
    pub operation_kind: String,
    pub target_key: String,
    pub payload_digest: String,
    pub expected_length: u64,
    pub expected_version: Option<String>,
    pub authority_principal: String,
    pub state: StorageEffectState,
    pub etag: Option<String>,
    pub provider_metadata: Option<String>,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WorkerStorageError {
    WorkerWithoutAuthority,
    FenceMismatch,
    StaleEpoch,
    StaleFencingToken,
    TargetMismatch,
    DigestMismatch,
    ReplayRejected,
    UnknownEffectRetryBlocked,
    PreconditionRejected,
    #[cfg(feature = "s3")]
    Transport(deepseek_storage::s3::S3Error),
    #[cfg(not(feature = "s3"))]
    Transport(String),
}

impl std::fmt::Display for WorkerStorageError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}

impl std::error::Error for WorkerStorageError {}

#[cfg(feature = "s3")]
impl From<deepseek_storage::s3::S3Error> for WorkerStorageError {
    fn from(err: deepseek_storage::s3::S3Error) -> Self {
        match err {
            deepseek_storage::s3::S3Error::PreconditionRejected => Self::PreconditionRejected,
            other => Self::Transport(other),
        }
    }
}

#[derive(Debug)]
struct WorkerAuthority {
    signer_public_key: String,
    signer_key_id: String,
    fleet_id: String,
    environment: String,
    fencing_token: i64,
    now: Option<String>,
    seen_request_ids: HashSet<String>,
    seen_nonces: HashSet<String>,
}

#[derive(Debug, Default)]
pub struct Worker {
    authority_store: Option<authority_store::AuthorityStore>,
    live_epochs: HashMap<String, u64>,
    effects: HashMap<(String, u64), EffectState>,
    authority: Option<WorkerAuthority>,
}

impl Worker {
    pub fn new() -> Self {
        Self::default()
    }

    /// Open Rust-owned durable fencing state beneath `state_root/rust-worker`.
    /// The directory must be writable only by the trusted worker/operator.
    /// Signing and all production mutation remain subject to existing denial gates.
    pub fn open_with_authority(
        config: WorkerAuthorityConfig,
        state_root: &std::path::Path,
    ) -> Result<Self, AuthorityRequestError> {
        let mut worker = Self::new();
        worker.configure_authority(config)?;
        worker.authority_store = Some(authority_store::AuthorityStore::open(
            state_root,
            worker.authority.as_ref().unwrap(),
        )?);
        Ok(worker)
    }

    pub fn configure_authority(
        &mut self,
        config: WorkerAuthorityConfig,
    ) -> Result<(), AuthorityRequestError> {
        if self.authority.is_some() {
            return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"));
        }
        if config.fencing_token < 1 {
            return Err(AuthorityRequestError::new(
                "AUTHORITY_REQUEST_STALE_FENCING_TOKEN",
            ));
        }
        if !authority_request::valid_fleet_id_str(&config.fleet_id) {
            return Err(AuthorityRequestError::new(
                "AUTHORITY_REQUEST_FLEET_MISMATCH",
            ));
        }
        if config.environment.is_empty() {
            return Err(AuthorityRequestError::new(
                "AUTHORITY_REQUEST_ENVIRONMENT_MISMATCH",
            ));
        }
        if let Some(now) = &config.now {
            authority_request::parse_utc_z_str(now)?;
        }
        let signer_key_id =
            authority_request::signer_key_id_for_public_key(&config.signer_public_key)?;
        self.authority = Some(WorkerAuthority {
            signer_public_key: config.signer_public_key,
            signer_key_id,
            fleet_id: config.fleet_id,
            environment: config.environment,
            fencing_token: config.fencing_token,
            now: config.now,
            seen_request_ids: HashSet::new(),
            seen_nonces: HashSet::new(),
        });
        Ok(())
    }

    pub fn authority_configured(&self) -> bool {
        self.authority.is_some()
    }

    pub fn install_authoritative_epoch(&mut self, fence: &ActionFence) -> Result<(), AdmitError> {
        if self.authority_store.is_some() {
            return Err(AdmitError::FenceMismatch);
        }
        let live = self.live_epochs.get(&fence.action_id).copied().unwrap_or(0);
        validate_authoritative_epoch_update(fence, live)?;
        self.live_epochs
            .insert(fence.action_id.clone(), fence.execution_epoch);
        Ok(())
    }

    pub fn install_signed_epoch(
        &mut self,
        envelope_fence: &ActionFence,
        canonical_request: &[u8],
    ) -> Result<ActionFence, AuthorityRequestError> {
        if let Some(store) = self.authority_store.as_mut() {
            let authority = self
                .authority
                .as_ref()
                .ok_or_else(|| AuthorityRequestError::new("AUTHORITY_REQUEST_SIGNER_MISMATCH"))?;
            return store.install(authority, envelope_fence, canonical_request);
        }
        validate_fence(envelope_fence).map_err(AuthorityRequestError::from)?;
        let live = self
            .live_epochs
            .get(&envelope_fence.action_id)
            .copied()
            .unwrap_or(0);
        let now_owned;
        let document = {
            let authority = self
                .authority
                .as_ref()
                .ok_or_else(|| AuthorityRequestError::new("AUTHORITY_REQUEST_SIGNER_MISMATCH"))?;
            let now = if let Some(now) = authority.now.as_deref() {
                now
            } else {
                now_owned = authority_request::utc_z_now()?;
                now_owned.as_str()
            };
            let context = AuthorityRequestContext {
                now,
                signer_public_key: &authority.signer_public_key,
                signer_key_id: &authority.signer_key_id,
                expected_domain: "action",
                expected_operation: "install-epoch",
                expected_runtime: "go",
                expected_mode: "shadow",
                expected_fleet_id: &authority.fleet_id,
                expected_environment: &authority.environment,
                expected_role: "control-plane",
                current_fencing_token: authority.fencing_token,
                live_epoch: live as i64,
                seen_request_ids: authority.seen_request_ids.clone(),
                seen_nonces: authority.seen_nonces.clone(),
                max_future_skew_seconds: 30,
            };
            verify_authority_request_document(canonical_request, &context)?
        };
        let fields = authority_request::authority_request_install_fields(&document)?;
        if fields.action_id != envelope_fence.action_id
            || fields.execution_epoch != envelope_fence.execution_epoch
        {
            return Err(AuthorityRequestError::new("FENCE_MISMATCH"));
        }
        validate_authoritative_epoch_update(envelope_fence, live)
            .map_err(AuthorityRequestError::from)?;
        self.live_epochs
            .insert(fields.action_id.clone(), fields.execution_epoch);
        if let Some(authority) = self.authority.as_mut() {
            authority.seen_request_ids.insert(fields.request_id);
            authority.seen_nonces.insert(fields.nonce);
        }
        Ok(ActionFence {
            action_id: fields.action_id,
            execution_epoch: fields.execution_epoch,
        })
    }

    pub fn admit(&self, fence: &ActionFence) -> Result<(), AdmitError> {
        if let Some(store) = &self.authority_store {
            return store.admit(fence);
        }
        let live = self.live_epochs.get(&fence.action_id).copied().unwrap_or(0);
        admit_command(fence, live)
    }

    pub fn query_effect(&self, fence: &ActionFence) -> Result<EffectState, AdmitError> {
        deepseek_protocol::validate_fence(fence)?;
        match self
            .effects
            .get(&(fence.action_id.clone(), fence.execution_epoch))
            .copied()
        {
            Some(state) => interpret_remote_outcome(state),
            None => Err(AdmitError::UnknownEffect),
        }
    }

    pub fn execute(&mut self, kind: CommandKind, fence: &ActionFence) -> Result<(), AdmitError> {
        self.admit(fence)?;
        if is_transfer_command(kind) {
            return plan_transfer(
                &TransferRequest {
                    kind,
                    fence: fence.clone(),
                    object_set_digest: String::new(),
                },
                fence.execution_epoch,
            );
        }
        if is_federation_command(kind) {
            return plan_federation(
                &SignRequest {
                    kind,
                    fence: fence.clone(),
                    payload_digest: String::new(),
                },
                fence.execution_epoch,
            );
        }
        plan_storage(
            &StorageRequest {
                kind,
                fence: fence.clone(),
                object_set_digest: String::new(),
            },
            fence.execution_epoch,
        )
    }

    pub fn verify_proof(
        &mut self,
        fence: &ActionFence,
        receipt_digest: String,
        commit_digest: String,
    ) -> Result<(), AdmitError> {
        self.admit(fence)?;
        plan_proof(
            &ProofRequest {
                fence: fence.clone(),
                receipt_digest,
                commit_digest,
            },
            fence.execution_epoch,
        )
    }

    pub fn record_effect(
        &mut self,
        fence: &ActionFence,
        state: EffectState,
    ) -> Result<(), AdmitError> {
        self.admit(fence)?;
        let interpreted = interpret_remote_outcome(state)?;
        self.effects.insert(
            (fence.action_id.clone(), fence.execution_epoch),
            interpreted,
        );
        Ok(())
    }

    #[cfg(feature = "s3")]
    pub fn reserve_storage_mutation(
        &mut self,
        fence: &ActionFence,
        key: &str,
        payload_digest: &[u8; 32],
    ) -> Result<deepseek_storage::s3::StorageAuthorityProof, WorkerStorageError> {
        let principal = self
            .authority
            .as_ref()
            .map(|a| a.signer_public_key.clone())
            .unwrap_or_else(|| "default-control-plane".to_string());
        self.reserve_storage_mutation_ext(
            fence,
            key,
            payload_digest,
            0,
            "PUT_CHUNK",
            None,
            &principal,
        )
    }

    #[cfg(feature = "s3")]
    #[allow(clippy::too_many_arguments)]
    pub fn reserve_storage_mutation_ext(
        &mut self,
        fence: &ActionFence,
        key: &str,
        payload_digest: &[u8; 32],
        expected_length: u64,
        operation_kind: &str,
        expected_version: Option<&str>,
        authority_principal: &str,
    ) -> Result<deepseek_storage::s3::StorageAuthorityProof, WorkerStorageError> {
        let store = self
            .authority_store
            .as_mut()
            .ok_or(WorkerStorageError::WorkerWithoutAuthority)?;
        store.reserve_storage_mutation(
            fence,
            key,
            payload_digest,
            expected_length,
            operation_kind,
            expected_version,
            authority_principal,
        )
    }

    #[cfg(feature = "s3")]
    pub fn transition_storage_mutation(
        &mut self,
        fence: &ActionFence,
        state: StorageEffectState,
        etag: Option<&str>,
        provider_metadata: Option<&str>,
    ) -> Result<(), WorkerStorageError> {
        let store = self
            .authority_store
            .as_mut()
            .ok_or(WorkerStorageError::WorkerWithoutAuthority)?;
        store.record_storage_mutation_outcome(fence, etag, state, provider_metadata)
    }

    #[cfg(feature = "s3")]
    pub fn record_storage_mutation_committed(
        &mut self,
        fence: &ActionFence,
        etag: &str,
    ) -> Result<(), WorkerStorageError> {
        self.transition_storage_mutation(fence, StorageEffectState::Confirmed, Some(etag), None)
    }

    #[cfg(feature = "s3")]
    pub fn record_storage_mutation_effect_unknown(
        &mut self,
        fence: &ActionFence,
    ) -> Result<(), WorkerStorageError> {
        self.transition_storage_mutation(fence, StorageEffectState::EffectUnknown, None, None)
    }

    #[cfg(feature = "s3")]
    pub fn record_storage_mutation_rejected(
        &mut self,
        fence: &ActionFence,
    ) -> Result<(), WorkerStorageError> {
        self.transition_storage_mutation(fence, StorageEffectState::Rejected, None, None)
    }

    #[cfg(feature = "s3")]
    pub fn query_storage_effect(
        &self,
        fence: &ActionFence,
    ) -> Result<Option<StorageEffectRecord>, WorkerStorageError> {
        let store = self
            .authority_store
            .as_ref()
            .ok_or(WorkerStorageError::WorkerWithoutAuthority)?;
        store.query_storage_effect(fence)
    }

    #[cfg(feature = "s3")]
    pub async fn execute_storage_put(
        &mut self,
        transport: &deepseek_storage::s3::S3Transport,
        key: &str,
        payload: bytes::Bytes,
        payload_digest: [u8; 32],
        fence: &ActionFence,
        condition: deepseek_storage::s3::ConditionalWrite,
    ) -> Result<deepseek_storage::s3::PutObservation, WorkerStorageError> {
        if self.authority_store.is_none() {
            return Err(WorkerStorageError::WorkerWithoutAuthority);
        }
        let principal = self
            .authority
            .as_ref()
            .map(|a| a.signer_public_key.clone())
            .unwrap_or_else(|| "default-control-plane".to_string());
        let payload_len = payload.len() as u64;
        let proof = self.reserve_storage_mutation_ext(
            fence,
            key,
            &payload_digest,
            payload_len,
            "PUT_CHUNK",
            None,
            &principal,
        )?;
        if <[u8; 32]>::from(sha2::Sha256::digest(&payload)) != payload_digest {
            return Err(WorkerStorageError::DigestMismatch);
        }
        self.transition_storage_mutation(fence, StorageEffectState::Dispatching, None, None)?;
        match transport
            .put_chunk(key, payload, payload_digest, &proof, condition)
            .await
        {
            Ok(observation) => {
                let metadata = format!(
                    "{{\"etag\":\"{}\",\"size\":{}}}",
                    observation.etag, observation.length
                );
                self.transition_storage_mutation(
                    fence,
                    StorageEffectState::Confirmed,
                    Some(&observation.etag),
                    Some(&metadata),
                )?;
                Ok(observation)
            }
            Err(deepseek_storage::s3::S3Error::PreconditionRejected) => {
                let _ = self.transition_storage_mutation(
                    fence,
                    StorageEffectState::Rejected,
                    None,
                    Some("{\"error\":\"PreconditionRejected\"}"),
                );
                Err(WorkerStorageError::PreconditionRejected)
            }
            Err(deepseek_storage::s3::S3Error::EffectUnknown) => {
                let _ = self.transition_storage_mutation(
                    fence,
                    StorageEffectState::EffectUnknown,
                    None,
                    Some("{\"error\":\"EffectUnknown\"}"),
                );
                Err(WorkerStorageError::Transport(
                    deepseek_storage::s3::S3Error::EffectUnknown,
                ))
            }
            Err(other) => {
                let _ = self.transition_storage_mutation(
                    fence,
                    StorageEffectState::Failed,
                    None,
                    Some(&format!("{{\"error\":\"{}\"}}", other)),
                );
                Err(WorkerStorageError::Transport(other))
            }
        }
    }

    #[cfg(feature = "s3")]
    pub async fn reconcile_storage_mutation(
        &mut self,
        transport: &deepseek_storage::s3::S3Transport,
        fence: &ActionFence,
    ) -> Result<StorageEffectRecord, WorkerStorageError> {
        let record = self
            .query_storage_effect(fence)?
            .ok_or(WorkerStorageError::FenceMismatch)?;
        if record.state == StorageEffectState::Confirmed
            || record.state == StorageEffectState::Rejected
            || record.state == StorageEffectState::Failed
        {
            return Ok(record);
        }
        self.transition_storage_mutation(fence, StorageEffectState::Reconciling, None, None)?;

        match transport.stat(&record.target_key).await {
            Ok(Some(observation)) => {
                let length_match = if record.expected_length > 0 {
                    observation.length == record.expected_length
                } else {
                    true
                };
                let matches = length_match
                    && observation.claimed_action_id.as_deref() == Some(&record.action_id)
                    && observation.claimed_execution_epoch.as_deref()
                        == Some(&record.execution_epoch.to_string())
                    && observation.claimed_fencing_token.as_deref()
                        == Some(&record.fencing_token.to_string())
                    && observation.claimed_request_id.as_deref() == Some(&record.request_id)
                    && observation.claimed_nonce.as_deref() == Some(&record.nonce)
                    && observation.claimed_sha256.as_deref() == Some(&record.payload_digest);
                if matches {
                    let metadata = format!(
                        "{{\"etag\":\"{}\",\"size\":{}}}",
                        observation.etag, observation.length
                    );
                    self.transition_storage_mutation(
                        fence,
                        StorageEffectState::Confirmed,
                        Some(&observation.etag),
                        Some(&metadata),
                    )?;
                } else {
                    self.transition_storage_mutation(
                        fence,
                        StorageEffectState::Rejected,
                        None,
                        Some("{\"reconciliation\":\"target_metadata_mismatch\"}"),
                    )?;
                }
            }
            Ok(None) => {
                self.transition_storage_mutation(
                    fence,
                    StorageEffectState::Rejected,
                    None,
                    Some("{\"reconciliation\":\"object_not_found\"}"),
                )?;
            }
            Err(err) => {
                self.transition_storage_mutation(
                    fence,
                    StorageEffectState::EffectUnknown,
                    None,
                    Some(&format!("{{\"error\":\"{}\"}}", err)),
                )?;
                return Err(WorkerStorageError::Transport(err));
            }
        }
        self.query_storage_effect(fence)?
            .ok_or(WorkerStorageError::FenceMismatch)
    }
}

pub fn authority_config_from_env(
    get: impl Fn(&str) -> Result<String, VarError>,
) -> Result<Option<WorkerAuthorityConfig>, AuthorityRequestError> {
    let signer = optional_env(&get, AUTH_SIGNER_PUBLIC_KEY)?;
    let fleet_id = optional_env(&get, AUTH_FLEET_ID)?;
    let environment = optional_env(&get, AUTH_ENVIRONMENT)?;
    let fencing_token = optional_env(&get, AUTH_FENCING_TOKEN)?;
    let now = optional_env(&get, AUTH_NOW)?;
    let present = [&signer, &fleet_id, &environment, &fencing_token]
        .iter()
        .filter(|value| value.is_some())
        .count();
    if present == 0 {
        if now.is_some() {
            return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"));
        }
        return Ok(None);
    }
    if present != 4 {
        return Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"));
    }
    Ok(Some(WorkerAuthorityConfig {
        signer_public_key: signer.unwrap(),
        fleet_id: fleet_id.unwrap(),
        environment: environment.unwrap(),
        fencing_token: fencing_token
            .as_ref()
            .unwrap()
            .parse::<i64>()
            .map_err(|_| AuthorityRequestError::new("AUTHORITY_REQUEST_STALE_FENCING_TOKEN"))?,
        now,
    }))
}

fn optional_env(
    get: &impl Fn(&str) -> Result<String, VarError>,
    name: &str,
) -> Result<Option<String>, AuthorityRequestError> {
    match get(name) {
        Ok(value) => {
            let trimmed = value.trim();
            if trimmed.is_empty() {
                Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))
            } else {
                Ok(Some(trimmed.to_string()))
            }
        }
        Err(VarError::NotPresent) => Ok(None),
        Err(VarError::NotUnicode(_)) => {
            Err(AuthorityRequestError::new("AUTHORITY_REQUEST_INVALID"))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fence(epoch: u64) -> ActionFence {
        ActionFence {
            action_id: "act-1".to_string(),
            execution_epoch: epoch,
        }
    }

    #[test]
    fn missing_effect_is_unknown_not_not_applied() {
        let worker = Worker::new();
        assert_eq!(
            worker.query_effect(&fence(1)),
            Err(AdmitError::UnknownEffect)
        );
    }

    #[test]
    fn invalid_fence_cannot_query_effect_state() {
        let worker = Worker::new();
        assert_eq!(
            worker.query_effect(&ActionFence {
                action_id: String::new(),
                execution_epoch: 1,
            }),
            Err(AdmitError::EmptyActionId)
        );
    }

    #[test]
    fn effect_cannot_establish_its_own_epoch() {
        let mut worker = Worker::new();
        assert_eq!(
            worker
                .record_effect(&fence(1), EffectState::Applied)
                .unwrap_err()
                .code(),
            "FENCE_MISMATCH"
        );
        assert_eq!(
            worker.query_effect(&fence(1)),
            Err(AdmitError::UnknownEffect)
        );
    }

    #[test]
    fn stale_epoch_cannot_commit() {
        let mut worker = Worker::new();
        worker.install_authoritative_epoch(&fence(4)).unwrap();
        assert_eq!(
            worker.record_effect(&fence(3), EffectState::Applied),
            Err(AdmitError::StaleEpoch)
        );
    }

    #[test]
    fn applied_effect_round_trips() {
        let mut worker = Worker::new();
        worker.install_authoritative_epoch(&fence(1)).unwrap();
        worker
            .record_effect(&fence(1), EffectState::Applied)
            .unwrap();
        assert_eq!(worker.query_effect(&fence(1)), Ok(EffectState::Applied));
    }

    #[test]
    fn storage_commands_admit_but_do_not_move_bytes() {
        let mut worker = Worker::new();
        worker.install_authoritative_epoch(&fence(1)).unwrap();
        assert_eq!(
            worker.execute(CommandKind::ExecuteBackup, &fence(1)),
            Err(AdmitError::StorageNotAuthoritative)
        );
        assert_eq!(
            worker.execute(CommandKind::ExecuteRepair, &fence(1)),
            Err(AdmitError::StorageNotAuthoritative)
        );
        assert_eq!(
            worker.query_effect(&fence(1)),
            Err(AdmitError::UnknownEffect)
        );
        worker.install_authoritative_epoch(&fence(2)).unwrap();
        assert_eq!(
            worker.execute(CommandKind::SignReadiness, &fence(2)),
            Err(AdmitError::FederationNotAuthoritative)
        );
        worker.install_authoritative_epoch(&fence(3)).unwrap();
        assert_eq!(
            worker.execute(CommandKind::ExecuteFederatedTransfer, &fence(3)),
            Err(AdmitError::TransferNotAuthoritative)
        );
    }

    #[test]
    fn proof_commands_do_not_claim_verification() {
        let mut worker = Worker::new();
        worker.install_authoritative_epoch(&fence(1)).unwrap();
        assert_eq!(
            worker.verify_proof(
                &fence(1),
                "sha256:receipt-v4".to_string(),
                "sha256:commit-v4".to_string()
            ),
            Err(AdmitError::ProofNotAuthoritative)
        );
        assert_eq!(
            worker.query_effect(&fence(1)),
            Err(AdmitError::UnknownEffect)
        );
    }

    #[test]
    fn unknown_recorded_state_is_rejected() {
        let mut worker = Worker::new();
        worker.install_authoritative_epoch(&fence(1)).unwrap();
        assert_eq!(
            worker.record_effect(&fence(1), EffectState::Unknown),
            Err(AdmitError::UnknownEffect)
        );
    }

    #[test]
    fn future_effect_epoch_cannot_advance_worker_authority() {
        let mut worker = Worker::new();
        worker.install_authoritative_epoch(&fence(2)).unwrap();
        assert_eq!(
            worker.record_effect(&fence(3), EffectState::Applied),
            Err(AdmitError::FenceMismatch)
        );
        worker
            .record_effect(&fence(2), EffectState::Applied)
            .unwrap();
        assert_eq!(worker.query_effect(&fence(2)), Ok(EffectState::Applied));
    }

    fn frozen_request() -> (WorkerAuthorityConfig, Vec<u8>, ActionFence) {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../../compat/native-runtime/v7/control/authority_request_vector.json"
        )))
        .unwrap();
        let canonical = fixture["canonical_request"]
            .as_str()
            .unwrap()
            .as_bytes()
            .to_vec();
        (
            WorkerAuthorityConfig {
                signer_public_key: fixture["signer_public_key"].as_str().unwrap().to_string(),
                fleet_id: "fleet-a".to_string(),
                environment: "test".to_string(),
                fencing_token: 4,
                now: Some(fixture["now"].as_str().unwrap().to_string()),
            },
            canonical,
            fence(4),
        )
    }

    #[test]
    fn signed_request_is_required_to_install_a_live_epoch() {
        let (config, canonical, installed) = frozen_request();
        let mut worker = Worker::new();
        assert_eq!(
            worker
                .install_signed_epoch(&installed, &canonical)
                .unwrap_err()
                .code,
            "AUTHORITY_REQUEST_SIGNER_MISMATCH"
        );
        assert_eq!(
            worker.admit(&installed).unwrap_err().code(),
            "FENCE_MISMATCH"
        );

        worker.configure_authority(config).unwrap();
        assert_eq!(
            worker
                .install_signed_epoch(
                    &ActionFence {
                        action_id: "other".to_string(),
                        execution_epoch: 4,
                    },
                    &canonical
                )
                .unwrap_err()
                .code,
            "FENCE_MISMATCH"
        );
        assert_eq!(
            worker.install_signed_epoch(&installed, &canonical).unwrap(),
            installed
        );
        worker.admit(&installed).unwrap();
        assert_eq!(
            worker.execute(CommandKind::ExecuteBackup, &installed),
            Err(AdmitError::StorageNotAuthoritative)
        );
        assert_eq!(
            worker
                .install_signed_epoch(&installed, &canonical)
                .unwrap_err()
                .code,
            "STALE_EXECUTION_EPOCH"
        );
    }

    #[test]
    fn authority_env_is_fail_closed_when_partial() {
        assert!(
            authority_config_from_env(|_| Err(VarError::NotPresent))
                .unwrap()
                .is_none()
        );
        let error = authority_config_from_env(|name| {
            if name == AUTH_SIGNER_PUBLIC_KEY {
                Ok("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo".to_string())
            } else {
                Err(VarError::NotPresent)
            }
        })
        .unwrap_err();
        assert_eq!(error.code, "AUTHORITY_REQUEST_INVALID");

        let config = authority_config_from_env(|name| match name {
            AUTH_SIGNER_PUBLIC_KEY => Ok("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo".to_string()),
            AUTH_FLEET_ID => Ok("fleet-a".to_string()),
            AUTH_ENVIRONMENT => Ok("test".to_string()),
            AUTH_FENCING_TOKEN => Ok("4".to_string()),
            AUTH_NOW => Ok("2026-09-04T00:00:40Z".to_string()),
            _ => Err(VarError::NotPresent),
        })
        .unwrap()
        .unwrap();
        let mut worker = Worker::new();
        worker.configure_authority(config.clone()).unwrap();
        assert!(worker.authority_configured());
        assert_eq!(
            worker.configure_authority(config).unwrap_err().code,
            "AUTHORITY_REQUEST_INVALID"
        );
        assert_eq!(
            Worker::new()
                .configure_authority(WorkerAuthorityConfig {
                    signer_public_key: "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo".to_string(),
                    fleet_id: "fleet-a".to_string(),
                    environment: "test".to_string(),
                    fencing_token: 0,
                    now: None,
                })
                .unwrap_err()
                .code,
            "AUTHORITY_REQUEST_STALE_FENCING_TOKEN"
        );
        assert_eq!(
            Worker::new()
                .configure_authority(WorkerAuthorityConfig {
                    signer_public_key: "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo".to_string(),
                    fleet_id: "NOPE".to_string(),
                    environment: "test".to_string(),
                    fencing_token: 4,
                    now: None,
                })
                .unwrap_err()
                .code,
            "AUTHORITY_REQUEST_FLEET_MISMATCH"
        );
        assert_eq!(
            Worker::new()
                .configure_authority(WorkerAuthorityConfig {
                    signer_public_key: "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo".to_string(),
                    fleet_id: "fleet-a".to_string(),
                    environment: String::new(),
                    fencing_token: 4,
                    now: None,
                })
                .unwrap_err()
                .code,
            "AUTHORITY_REQUEST_ENVIRONMENT_MISMATCH"
        );
        assert_eq!(
            Worker::new()
                .configure_authority(WorkerAuthorityConfig {
                    signer_public_key: "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo".to_string(),
                    fleet_id: "fleet-a".to_string(),
                    environment: "test".to_string(),
                    fencing_token: 4,
                    now: Some("not-a-timestamp".to_string()),
                })
                .unwrap_err()
                .code,
            "AUTHORITY_REQUEST_INVALID"
        );
        assert_eq!(
            authority_config_from_env(|name| {
                if name == AUTH_NOW {
                    Ok("2026-09-04T00:00:40Z".to_string())
                } else {
                    Err(VarError::NotPresent)
                }
            })
            .unwrap_err()
            .code,
            "AUTHORITY_REQUEST_INVALID"
        );
        assert_eq!(
            authority_config_from_env(|_| Ok(String::new()))
                .unwrap_err()
                .code,
            "AUTHORITY_REQUEST_INVALID"
        );
        assert_eq!(
            authority_config_from_env(|name| match name {
                AUTH_SIGNER_PUBLIC_KEY => {
                    Ok("11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo".to_string())
                }
                AUTH_FLEET_ID => Ok("fleet-a".to_string()),
                AUTH_ENVIRONMENT => Ok("test".to_string()),
                AUTH_FENCING_TOKEN => Ok("nope".to_string()),
                _ => Err(VarError::NotPresent),
            })
            .unwrap_err()
            .code,
            "AUTHORITY_REQUEST_STALE_FENCING_TOKEN"
        );
        let mut worker = Worker::new();
        assert_eq!(
            worker
                .install_signed_epoch(
                    &ActionFence {
                        action_id: String::new(),
                        execution_epoch: 1,
                    },
                    b"{}"
                )
                .unwrap_err()
                .code,
            "EMPTY_ACTION_ID"
        );
    }
}
