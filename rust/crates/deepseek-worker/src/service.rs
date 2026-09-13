use std::sync::Arc;
use tokio::sync::{Mutex, MutexGuard};

#[cfg(feature = "s3")]
use sha2::Digest as _;

use deepseek_protocol::generated::deepseek::action::v1::{
    AdmitCommandRequest, AdmitCommandResponse, AdmitStatus, CommandKind, EffectResult,
    InstallAuthoritativeEpochRequest, InstallAuthoritativeEpochResponse, QueryEffectRequest,
    QueryStorageEffectRequest, StorageConditionType, StorageMutationRequest,
    StorageMutationResponse, StorageMutationStatus, worker_server::Worker as WorkerRpc,
};
use deepseek_protocol::generated::deepseek::common::v1::{ActionFence, EffectState, ErrorDetail};
use deepseek_protocol::{
    AdmitError, is_federation_command, is_storage_command, is_transfer_command, validate_fence,
};
use tonic::{Request, Response, Status};

use crate::Worker;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CallerIdentity {
    pub service_name: String,
    pub role: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AuthError {
    MissingAuthorization,
    InvalidToken,
    ServiceAuthenticationUnavailable,
    Internal(String),
}

impl AuthError {
    pub fn code(&self) -> &'static str {
        match self {
            Self::MissingAuthorization => "AUTHENTICATION_MISSING",
            Self::InvalidToken => "AUTHENTICATION_INVALID",
            Self::ServiceAuthenticationUnavailable => "SERVICE_AUTHENTICATION_UNAVAILABLE",
            Self::Internal(_) => "AUTHENTICATION_FAILED",
        }
    }
}

impl std::fmt::Display for AuthError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.code())
    }
}

impl std::error::Error for AuthError {}

pub(crate) fn unix_now_seconds() -> Result<i64, AuthError> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .map_err(|_| AuthError::ServiceAuthenticationUnavailable)
}

pub(crate) fn valid_service_bearer(token: &str) -> bool {
    !token.is_empty() && token.len() <= 4096 && token.bytes().all(|byte| byte.is_ascii_graphic())
}

pub(crate) const MAX_SERVICE_BEARER_LIFETIME_SECONDS: i64 = 3600;

pub(crate) fn bearer_token_from_metadata(
    metadata: &tonic::metadata::MetadataMap,
) -> Result<&str, AuthError> {
    let mut values = metadata.get_all("authorization").iter();
    let Some(first) = values.next() else {
        return Err(AuthError::MissingAuthorization);
    };
    if values.next().is_some() {
        return Err(AuthError::InvalidToken);
    }
    let auth_str = first.to_str().map_err(|_| AuthError::InvalidToken)?;
    let token = auth_str
        .strip_prefix("Bearer ")
        .ok_or(AuthError::InvalidToken)?;
    if !valid_service_bearer(token) {
        return Err(AuthError::InvalidToken);
    }
    Ok(token)
}

fn tokens_equal(left: &str, right: &str) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0u8;
    for (a, b) in left.bytes().zip(right.bytes()) {
        diff |= a ^ b;
    }
    diff == 0
}

pub trait TransportAuthenticator: Send + Sync + 'static {
    fn authenticate(
        &self,
        metadata: &tonic::metadata::MetadataMap,
    ) -> Result<CallerIdentity, AuthError>;
}

#[derive(Debug, Default, Clone)]
pub struct ProductionFailClosedAuthenticator;

impl TransportAuthenticator for ProductionFailClosedAuthenticator {
    fn authenticate(
        &self,
        _metadata: &tonic::metadata::MetadataMap,
    ) -> Result<CallerIdentity, AuthError> {
        // No configured authenticated transport. Loopback is not caller identity.
        // Transport approval alone never enables production execution authority.
        Err(AuthError::ServiceAuthenticationUnavailable)
    }
}

#[derive(Clone)]
pub struct StaticTokenAuthenticator {
    expected_token: String,
    identity: CallerIdentity,
}

impl std::fmt::Debug for StaticTokenAuthenticator {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("StaticTokenAuthenticator")
            .field("expected_token", &"<redacted>")
            .field("identity", &self.identity)
            .finish()
    }
}

impl StaticTokenAuthenticator {
    pub fn new(token: impl Into<String>, identity: CallerIdentity) -> Self {
        Self {
            expected_token: token.into(),
            identity,
        }
    }
}

impl TransportAuthenticator for StaticTokenAuthenticator {
    fn authenticate(
        &self,
        metadata: &tonic::metadata::MetadataMap,
    ) -> Result<CallerIdentity, AuthError> {
        let token = bearer_token_from_metadata(metadata)?;
        if tokens_equal(token, &self.expected_token) {
            Ok(self.identity.clone())
        } else {
            Err(AuthError::InvalidToken)
        }
    }
}

#[derive(Clone)]
pub struct ServiceBearerAuthenticator {
    expected_token: String,
    expires_at_unix: i64,
    identity: CallerIdentity,
}

impl std::fmt::Debug for ServiceBearerAuthenticator {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ServiceBearerAuthenticator")
            .field("expected_token", &"<redacted>")
            .field("expires_at_unix", &self.expires_at_unix)
            .field("identity", &self.identity)
            .finish()
    }
}

impl ServiceBearerAuthenticator {
    pub fn new(token: impl Into<String>, expires_at_unix: i64, identity: CallerIdentity) -> Self {
        Self {
            expected_token: token.into(),
            expires_at_unix,
            identity,
        }
    }
}

impl TransportAuthenticator for ServiceBearerAuthenticator {
    fn authenticate(
        &self,
        metadata: &tonic::metadata::MetadataMap,
    ) -> Result<CallerIdentity, AuthError> {
        let token = bearer_token_from_metadata(metadata)?;
        let now = unix_now_seconds()?;
        if self.expected_token.len() < 32
            || now >= self.expires_at_unix
            || self.expires_at_unix.saturating_sub(now) > MAX_SERVICE_BEARER_LIFETIME_SECONDS
        {
            return Err(AuthError::InvalidToken);
        }
        if tokens_equal(token, &self.expected_token) {
            Ok(self.identity.clone())
        } else {
            Err(AuthError::InvalidToken)
        }
    }
}

#[derive(Clone)]
pub struct WorkerRpcService {
    worker: Arc<Mutex<Worker>>,
    authenticator: Arc<dyn TransportAuthenticator>,
    #[cfg(feature = "s3")]
    transport: Option<Arc<deepseek_storage::s3::S3Transport>>,
}

impl std::fmt::Debug for WorkerRpcService {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("WorkerRpcService")
            .field("authenticator", &"<TransportAuthenticator>")
            .finish()
    }
}

impl WorkerRpcService {
    pub fn new(worker: Worker) -> Self {
        Self {
            worker: Arc::new(Mutex::new(worker)),
            authenticator: Arc::new(ProductionFailClosedAuthenticator),
            #[cfg(feature = "s3")]
            transport: None,
        }
    }

    pub fn new_with_authenticator(
        worker: Worker,
        authenticator: Arc<dyn TransportAuthenticator>,
    ) -> Self {
        Self {
            worker: Arc::new(Mutex::new(worker)),
            authenticator,
            #[cfg(feature = "s3")]
            transport: None,
        }
    }

    #[cfg(feature = "s3")]
    pub fn with_transport(mut self, transport: Arc<deepseek_storage::s3::S3Transport>) -> Self {
        self.transport = Some(transport);
        self
    }

    async fn lock(&self) -> MutexGuard<'_, Worker> {
        self.worker.lock().await
    }
}

fn validate_kind(raw: i32) -> Result<CommandKind, AdmitError> {
    let kind = CommandKind::try_from(raw).map_err(|_| AdmitError::UnknownEffect)?;
    if is_storage_command(kind) || is_transfer_command(kind) || is_federation_command(kind) {
        Ok(kind)
    } else {
        Err(AdmitError::UnknownEffect)
    }
}

fn detail(error: AdmitError) -> ErrorDetail {
    let category = match error {
        AdmitError::EmptyActionId
        | AdmitError::ZeroEpoch
        | AdmitError::StaleEpoch
        | AdmitError::FenceMismatch => "FENCE",
        AdmitError::UnknownEffect => "EFFECT",
        AdmitError::StorageNotAuthoritative
        | AdmitError::TransferNotAuthoritative
        | AdmitError::FederationNotAuthoritative
        | AdmitError::ProofNotAuthoritative => "AUTHORITY",
    };
    ErrorDetail {
        code: error.code().to_string(),
        category: category.to_string(),
        message: "command rejected".to_string(),
    }
}

fn rejected(error: AdmitError) -> AdmitCommandResponse {
    AdmitCommandResponse {
        status: AdmitStatus::Rejected as i32,
        state: EffectState::Unknown as i32,
        error: Some(detail(error)),
    }
}

fn authentication_detail(error: AuthError) -> ErrorDetail {
    ErrorDetail {
        code: error.code().to_string(),
        category: "AUTHENTICATION".to_string(),
        message: "caller authentication rejected".to_string(),
    }
}

fn configured_transport_identity(
    authenticator: &dyn TransportAuthenticator,
    metadata: &tonic::metadata::MetadataMap,
) -> Result<Option<CallerIdentity>, AuthError> {
    match authenticator.authenticate(metadata) {
        Ok(identity) => Ok(Some(identity)),
        Err(AuthError::ServiceAuthenticationUnavailable) => Ok(None),
        Err(error) => Err(error),
    }
}

fn authority_detail(code: &'static str) -> ErrorDetail {
    let category = if code.starts_with("AUTHORITY_REQUEST_") {
        "AUTHORITY"
    } else {
        "FENCE"
    };
    ErrorDetail {
        code: code.to_string(),
        category: category.to_string(),
        message: "command rejected".to_string(),
    }
}

fn install_rejected(code: &'static str) -> InstallAuthoritativeEpochResponse {
    InstallAuthoritativeEpochResponse {
        status: AdmitStatus::Rejected as i32,
        fence: None,
        error: Some(authority_detail(code)),
    }
}

fn storage_rejected(
    fence: Option<ActionFence>,
    operation_id: String,
    code: &'static str,
    category: &'static str,
    message: &str,
) -> StorageMutationResponse {
    StorageMutationResponse {
        status: StorageMutationStatus::Rejected as i32,
        state: EffectState::Unknown as i32,
        fence,
        operation_id,
        effect_id: String::new(),
        etag: String::new(),
        provider_metadata: String::new(),
        error: Some(ErrorDetail {
            code: code.to_string(),
            category: category.to_string(),
            message: message.to_string(),
        }),
    }
}

#[cfg(feature = "s3")]
fn storage_failed(
    fence: Option<ActionFence>,
    operation_id: String,
    code: &'static str,
    category: &'static str,
    message: &str,
) -> StorageMutationResponse {
    StorageMutationResponse {
        status: StorageMutationStatus::Failed as i32,
        state: EffectState::Unknown as i32,
        fence,
        operation_id,
        effect_id: String::new(),
        etag: String::new(),
        provider_metadata: String::new(),
        error: Some(ErrorDetail {
            code: code.to_string(),
            category: category.to_string(),
            message: message.to_string(),
        }),
    }
}

#[tonic::async_trait]
impl WorkerRpc for WorkerRpcService {
    async fn admit_command(
        &self,
        request: Request<AdmitCommandRequest>,
    ) -> Result<Response<AdmitCommandResponse>, Status> {
        if let Err(auth_err) =
            configured_transport_identity(self.authenticator.as_ref(), request.metadata())
        {
            return Ok(Response::new(AdmitCommandResponse {
                status: AdmitStatus::Rejected as i32,
                state: EffectState::Unknown as i32,
                error: Some(authentication_detail(auth_err)),
            }));
        }
        let input = request.into_inner();
        // v1 retained this field for wire compatibility. It is caller-controlled
        // and must never establish or advance the worker's local authority.
        let _untrusted_live_epoch = input.live_epoch;
        let fence = match input.fence.as_ref() {
            Some(fence) => fence,
            None => return Ok(Response::new(rejected(AdmitError::EmptyActionId))),
        };
        let worker = self.lock().await;
        if let Err(error) = worker.admit(fence) {
            return Ok(Response::new(rejected(error)));
        }
        if let Err(error) = validate_kind(input.kind) {
            return Ok(Response::new(rejected(error)));
        }
        Ok(Response::new(AdmitCommandResponse {
            status: AdmitStatus::Admitted as i32,
            state: EffectState::Unknown as i32,
            error: None,
        }))
    }

    async fn query_effect(
        &self,
        request: Request<QueryEffectRequest>,
    ) -> Result<Response<EffectResult>, Status> {
        let auth_result =
            configured_transport_identity(self.authenticator.as_ref(), request.metadata());
        let input = request.into_inner();
        if let Err(auth_err) = auth_result {
            return Ok(Response::new(EffectResult {
                fence: input.fence,
                state: EffectState::Unknown as i32,
                error: Some(authentication_detail(auth_err)),
                ..EffectResult::default()
            }));
        }
        let fence = match input.fence {
            Some(fence) => fence,
            None => {
                return Ok(Response::new(EffectResult {
                    state: EffectState::Unknown as i32,
                    error: Some(detail(AdmitError::EmptyActionId)),
                    ..EffectResult::default()
                }));
            }
        };
        let worker = self.lock().await;
        match worker.query_effect(&fence) {
            Ok(_) => Ok(Response::new(EffectResult {
                fence: Some(fence),
                state: EffectState::Unknown as i32,
                error: Some(detail(AdmitError::ProofNotAuthoritative)),
                ..EffectResult::default()
            })),
            Err(error) => Ok(Response::new(EffectResult {
                fence: Some(fence),
                state: EffectState::Unknown as i32,
                error: Some(detail(error)),
                ..EffectResult::default()
            })),
        }
    }

    async fn install_authoritative_epoch(
        &self,
        request: Request<InstallAuthoritativeEpochRequest>,
    ) -> Result<Response<InstallAuthoritativeEpochResponse>, Status> {
        if let Err(auth_err) =
            configured_transport_identity(self.authenticator.as_ref(), request.metadata())
        {
            return Ok(Response::new(InstallAuthoritativeEpochResponse {
                status: AdmitStatus::Rejected as i32,
                fence: None,
                error: Some(authentication_detail(auth_err)),
            }));
        }
        let input = request.into_inner();
        let fence = match input.fence.as_ref() {
            Some(fence) => fence,
            None => return Ok(Response::new(install_rejected("EMPTY_ACTION_ID"))),
        };
        let mut worker = self.lock().await;
        match worker.install_signed_epoch(fence, &input.canonical_request) {
            Ok(installed) => Ok(Response::new(InstallAuthoritativeEpochResponse {
                status: AdmitStatus::Admitted as i32,
                fence: Some(installed),
                error: None,
            })),
            Err(error) => Ok(Response::new(install_rejected(error.code))),
        }
    }

    async fn execute_storage_mutation(
        &self,
        request: Request<StorageMutationRequest>,
    ) -> Result<Response<StorageMutationResponse>, Status> {
        let auth_result = self.authenticator.authenticate(request.metadata());
        let input = request.into_inner();
        if let Err(auth_err) = auth_result {
            return Ok(Response::new(StorageMutationResponse {
                status: StorageMutationStatus::Rejected as i32,
                state: EffectState::Unknown as i32,
                fence: input.fence,
                operation_id: input.operation_id,
                effect_id: String::new(),
                etag: String::new(),
                provider_metadata: String::new(),
                error: Some(ErrorDetail {
                    code: auth_err.code().to_string(),
                    category: "AUTHENTICATION".to_string(),
                    message: "caller authentication rejected".to_string(),
                }),
            }));
        }

        let fence = match input.fence.as_ref() {
            Some(fence) => fence,
            None => {
                return Ok(Response::new(storage_rejected(
                    None,
                    input.operation_id,
                    AdmitError::EmptyActionId.code(),
                    "FENCE",
                    "missing fence",
                )));
            }
        };

        if let Err(error) = validate_fence(fence) {
            return Ok(Response::new(storage_rejected(
                Some(fence.clone()),
                input.operation_id,
                error.code(),
                "FENCE",
                "invalid fence",
            )));
        }

        if !crate::valid_storage_operation_id(&input.operation_id) {
            return Ok(Response::new(storage_rejected(
                Some(fence.clone()),
                input.operation_id,
                "OPERATION_INVALID",
                "OPERATION",
                "invalid operation_id",
            )));
        }

        let condition = match input.precondition.as_ref() {
            Some(pre) => match StorageConditionType::try_from(pre.condition_type) {
                Ok(StorageConditionType::CreateOnly) => "CREATE_ONLY",
                Ok(StorageConditionType::IfMatch) => "IF_MATCH",
                _ => "",
            },
            None => "",
        };
        let expected_etag = input
            .precondition
            .as_ref()
            .map(|pre| pre.expected_etag.as_str())
            .unwrap_or("");
        let command = crate::StorageOperationCommand {
            action_id: &fence.action_id,
            execution_epoch: fence.execution_epoch,
            operation_id: &input.operation_id,
            mutation_type: &input.mutation_type,
            provider: &input.provider,
            target_identity: &input.target_identity,
            bucket: &input.bucket,
            prefix: &input.prefix,
            object_key: &input.object_key,
            object_digest: &input.payload_digest,
            expected_length: input.expected_length,
            condition_type: condition,
            expected_etag,
            claim_revision: 0,
        };
        {
            let mut worker = self.lock().await;
            if let Err(error) =
                worker.admit_storage_operation_grant(&input.canonical_authorization, &command)
            {
                return Ok(Response::new(storage_rejected(
                    Some(fence.clone()),
                    input.operation_id.clone(),
                    error.code,
                    "AUTHORIZATION",
                    "storage operation grant rejected",
                )));
            }
        }

        if input.mutation_type != "PUT_CHUNK" {
            return Ok(Response::new(storage_rejected(
                Some(fence.clone()),
                input.operation_id,
                "OPERATION_INVALID",
                "OPERATION",
                "unsupported mutation type",
            )));
        }

        if input.payload.len() as u64 != input.expected_length {
            return Ok(Response::new(storage_rejected(
                Some(fence.clone()),
                input.operation_id,
                "PAYLOAD_LENGTH_MISMATCH",
                "PAYLOAD",
                "payload length does not match expected_length",
            )));
        }

        #[cfg(feature = "s3")]
        {
            let digest_bytes = match crate::storage_digest(&input.payload_digest) {
                Ok(bytes) => bytes,
                Err(_) => {
                    return Ok(Response::new(storage_rejected(
                        Some(fence.clone()),
                        input.operation_id,
                        "DIGEST_MISMATCH",
                        "DIGEST",
                        "invalid payload digest format",
                    )));
                }
            };

            if <[u8; 32]>::from(sha2::Sha256::digest(&input.payload)) != digest_bytes {
                return Ok(Response::new(storage_rejected(
                    Some(fence.clone()),
                    input.operation_id,
                    "DIGEST_MISMATCH",
                    "DIGEST",
                    "payload sha256 does not match payload_digest",
                )));
            }

            let condition = match input.precondition {
                Some(pre) => match StorageConditionType::try_from(pre.condition_type) {
                    Ok(StorageConditionType::CreateOnly) => {
                        deepseek_storage::s3::ConditionalWrite::Create
                    }
                    Ok(StorageConditionType::IfMatch) => {
                        if pre.expected_etag.trim().is_empty() {
                            return Ok(Response::new(storage_rejected(
                                Some(fence.clone()),
                                input.operation_id,
                                "PRECONDITION_INVALID",
                                "PRECONDITION",
                                "expected_etag required for IF_MATCH",
                            )));
                        }
                        deepseek_storage::s3::ConditionalWrite::Match(pre.expected_etag)
                    }
                    _ => {
                        return Ok(Response::new(storage_rejected(
                            Some(fence.clone()),
                            input.operation_id,
                            "PRECONDITION_INVALID",
                            "PRECONDITION",
                            "unsupported condition type",
                        )));
                    }
                },
                None => {
                    return Ok(Response::new(storage_rejected(
                        Some(fence.clone()),
                        input.operation_id,
                        "PRECONDITION_INVALID",
                        "PRECONDITION",
                        "precondition is required",
                    )));
                }
            };

            let transport = match &self.transport {
                Some(t) => t.clone(),
                None => {
                    return Ok(Response::new(storage_failed(
                        Some(fence.clone()),
                        input.operation_id,
                        "STORAGE_TRANSPORT_UNAVAILABLE",
                        "STORAGE",
                        "storage transport not configured",
                    )));
                }
            };

            let expected_target = crate::storage_digest_hex(&transport.target_identity());
            if !input.target_identity.is_empty() && input.target_identity != expected_target {
                return Ok(Response::new(storage_rejected(
                    Some(fence.clone()),
                    input.operation_id,
                    "TARGET_MISMATCH",
                    "STORAGE",
                    "storage target identity mismatch",
                )));
            }

            let bytes = bytes::Bytes::from(input.payload);
            let mut worker = self.lock().await;
            match worker
                .execute_storage_put_for_operation(
                    &transport,
                    &input.object_key,
                    bytes,
                    digest_bytes,
                    fence,
                    condition,
                    Some(&input.operation_id),
                )
                .await
            {
                Ok(observation) => Ok(Response::new(StorageMutationResponse {
                    status: StorageMutationStatus::Confirmed as i32,
                    state: EffectState::Applied as i32,
                    fence: Some(fence.clone()),
                    operation_id: input.operation_id,
                    effect_id: format!("{}:{}", fence.action_id, fence.execution_epoch),
                    etag: observation.etag.clone(),
                    provider_metadata: serde_json::json!({
                        "etag": observation.etag,
                        "size": observation.length,
                        "version": observation.version,
                    })
                    .to_string(),
                    error: None,
                })),
                Err(crate::WorkerStorageError::OperationMismatch) => {
                    Ok(Response::new(storage_rejected(
                        Some(fence.clone()),
                        input.operation_id,
                        "STORAGE_OPERATION_MISMATCH",
                        "STORAGE",
                        "operation does not match the durable intent",
                    )))
                }
                Err(crate::WorkerStorageError::PreconditionRejected) => {
                    Ok(Response::new(storage_rejected(
                        Some(fence.clone()),
                        input.operation_id,
                        "PRECONDITION_REJECTED",
                        "STORAGE",
                        "storage precondition rejected by provider",
                    )))
                }
                Err(crate::WorkerStorageError::ReplayRejected) => {
                    Ok(Response::new(storage_rejected(
                        Some(fence.clone()),
                        input.operation_id,
                        "REPLAY_REJECTED",
                        "STORAGE",
                        "storage mutation replay rejected",
                    )))
                }
                Err(crate::WorkerStorageError::UnknownEffectRetryBlocked) => {
                    Ok(Response::new(StorageMutationResponse {
                        status: StorageMutationStatus::EffectUnknown as i32,
                        state: EffectState::Unknown as i32,
                        fence: Some(fence.clone()),
                        operation_id: input.operation_id,
                        effect_id: String::new(),
                        etag: String::new(),
                        provider_metadata: String::new(),
                        error: Some(ErrorDetail {
                            code: "UNKNOWN_EFFECT_RETRY_BLOCKED".to_string(),
                            category: "STORAGE".to_string(),
                            message: "mutation in uncertain state, retry blocked".to_string(),
                        }),
                    }))
                }
                Err(crate::WorkerStorageError::FenceMismatch) => {
                    Ok(Response::new(storage_rejected(
                        Some(fence.clone()),
                        input.operation_id,
                        "FENCE_MISMATCH",
                        "FENCE",
                        "fence mismatch",
                    )))
                }
                Err(crate::WorkerStorageError::StaleEpoch) => Ok(Response::new(storage_rejected(
                    Some(fence.clone()),
                    input.operation_id,
                    "STALE_EXECUTION_EPOCH",
                    "FENCE",
                    "stale execution epoch",
                ))),
                Err(crate::WorkerStorageError::StaleFencingToken) => {
                    Ok(Response::new(storage_rejected(
                        Some(fence.clone()),
                        input.operation_id,
                        "STALE_FENCING_TOKEN",
                        "AUTHORITY",
                        "stale fencing token",
                    )))
                }
                Err(crate::WorkerStorageError::TargetMismatch) => {
                    Ok(Response::new(storage_rejected(
                        Some(fence.clone()),
                        input.operation_id,
                        "TARGET_MISMATCH",
                        "STORAGE",
                        "target mismatch",
                    )))
                }
                Err(crate::WorkerStorageError::DigestMismatch) => {
                    Ok(Response::new(storage_rejected(
                        Some(fence.clone()),
                        input.operation_id,
                        "DIGEST_MISMATCH",
                        "STORAGE",
                        "digest mismatch",
                    )))
                }
                Err(crate::WorkerStorageError::WorkerWithoutAuthority) => {
                    Ok(Response::new(storage_rejected(
                        Some(fence.clone()),
                        input.operation_id,
                        "WORKER_WITHOUT_AUTHORITY",
                        "AUTHORITY",
                        "worker authority not configured",
                    )))
                }
                Err(crate::WorkerStorageError::Transport(
                    deepseek_storage::s3::S3Error::EffectUnknown,
                )) => Ok(Response::new(StorageMutationResponse {
                    status: StorageMutationStatus::EffectUnknown as i32,
                    state: EffectState::Unknown as i32,
                    fence: Some(fence.clone()),
                    operation_id: input.operation_id,
                    effect_id: String::new(),
                    etag: String::new(),
                    provider_metadata: String::new(),
                    error: Some(ErrorDetail {
                        code: "EFFECT_UNKNOWN".to_string(),
                        category: "STORAGE".to_string(),
                        message: "mutation effect unknown".to_string(),
                    }),
                })),
                Err(crate::WorkerStorageError::Transport(err)) => {
                    Ok(Response::new(storage_failed(
                        Some(fence.clone()),
                        input.operation_id,
                        "STORAGE_TRANSPORT_ERROR",
                        "STORAGE",
                        &err.to_string(),
                    )))
                }
            }
        }

        #[cfg(not(feature = "s3"))]
        {
            Ok(Response::new(storage_rejected(
                Some(fence.clone()),
                input.operation_id,
                "STORAGE_FEATURE_DISABLED",
                "STORAGE",
                "worker compiled without s3 feature",
            )))
        }
    }

    async fn query_storage_effect(
        &self,
        request: Request<QueryStorageEffectRequest>,
    ) -> Result<Response<StorageMutationResponse>, Status> {
        let auth_result = self.authenticator.authenticate(request.metadata());
        let input = request.into_inner();
        if let Err(auth_err) = auth_result {
            return Ok(Response::new(StorageMutationResponse {
                status: StorageMutationStatus::Rejected as i32,
                state: EffectState::Unknown as i32,
                fence: input.fence,
                operation_id: input.operation_id,
                effect_id: String::new(),
                etag: String::new(),
                provider_metadata: String::new(),
                error: Some(ErrorDetail {
                    code: auth_err.code().to_string(),
                    category: "AUTHENTICATION".to_string(),
                    message: "caller authentication rejected".to_string(),
                }),
            }));
        }

        let fence = match input.fence.as_ref() {
            Some(fence) => fence,
            None => {
                return Ok(Response::new(storage_rejected(
                    None,
                    input.operation_id,
                    AdmitError::EmptyActionId.code(),
                    "FENCE",
                    "missing fence",
                )));
            }
        };

        if let Err(error) = validate_fence(fence) {
            return Ok(Response::new(storage_rejected(
                Some(fence.clone()),
                input.operation_id,
                error.code(),
                "FENCE",
                "invalid fence",
            )));
        }
        if !crate::valid_storage_operation_id(&input.operation_id) {
            return Ok(Response::new(storage_rejected(
                Some(fence.clone()),
                input.operation_id,
                "OPERATION_INVALID",
                "STORAGE",
                "invalid storage operation identity",
            )));
        }

        #[cfg(feature = "s3")]
        {
            let mut worker = self.lock().await;
            let record = match worker.query_storage_effect(fence) {
                Ok(Some(rec)) => rec,
                Ok(None) => {
                    return Ok(Response::new(StorageMutationResponse {
                        status: StorageMutationStatus::EffectUnknown as i32,
                        state: EffectState::Unknown as i32,
                        fence: Some(fence.clone()),
                        operation_id: input.operation_id,
                        effect_id: String::new(),
                        etag: String::new(),
                        provider_metadata: String::new(),
                        error: Some(ErrorDetail {
                            code: "EFFECT_UNKNOWN".to_string(),
                            category: "STORAGE".to_string(),
                            message: "no recorded effect for fence".to_string(),
                        }),
                    }));
                }
                Err(crate::WorkerStorageError::WorkerWithoutAuthority) => {
                    return Ok(Response::new(storage_rejected(
                        Some(fence.clone()),
                        input.operation_id,
                        "WORKER_WITHOUT_AUTHORITY",
                        "AUTHORITY",
                        "worker authority not configured",
                    )));
                }
                Err(crate::WorkerStorageError::FenceMismatch) => {
                    return Ok(Response::new(storage_rejected(
                        Some(fence.clone()),
                        input.operation_id,
                        "FENCE_MISMATCH",
                        "FENCE",
                        "fence mismatch",
                    )));
                }
                Err(err) => {
                    return Ok(Response::new(storage_failed(
                        Some(fence.clone()),
                        input.operation_id,
                        "STORAGE_QUERY_ERROR",
                        "STORAGE",
                        &err.to_string(),
                    )));
                }
            };

            let Some(operation_id) = record.operation_id.clone() else {
                return Ok(Response::new(storage_rejected(
                    Some(fence.clone()),
                    input.operation_id,
                    "STORAGE_OPERATION_UNBOUND",
                    "STORAGE",
                    "historical effect has no RPC operation identity",
                )));
            };
            if operation_id != input.operation_id {
                return Ok(Response::new(storage_rejected(
                    Some(fence.clone()),
                    input.operation_id,
                    "STORAGE_OPERATION_MISMATCH",
                    "STORAGE",
                    "operation does not match the durable intent",
                )));
            }

            let record = if matches!(
                record.state,
                crate::StorageEffectState::EffectUnknown | crate::StorageEffectState::Dispatching
            ) {
                if let Some(transport) = &self.transport {
                    let transport = transport.clone();
                    match worker
                        .reconcile_storage_mutation_for_operation(
                            &transport,
                            fence,
                            Some(&operation_id),
                        )
                        .await
                    {
                        Ok(reconciled) => reconciled,
                        Err(_) => match worker.query_storage_effect(fence) {
                            Ok(Some(rec)) => rec,
                            _ => record,
                        },
                    }
                } else {
                    record
                }
            } else {
                record
            };

            let (status, state) = match record.state {
                crate::StorageEffectState::Confirmed => {
                    (StorageMutationStatus::Confirmed, EffectState::Applied)
                }
                crate::StorageEffectState::EffectUnknown => {
                    (StorageMutationStatus::EffectUnknown, EffectState::Unknown)
                }
                crate::StorageEffectState::Reconciling => {
                    (StorageMutationStatus::Reconciling, EffectState::Unknown)
                }
                crate::StorageEffectState::Rejected => {
                    (StorageMutationStatus::Rejected, EffectState::NotApplied)
                }
                crate::StorageEffectState::Failed => {
                    (StorageMutationStatus::Failed, EffectState::NotApplied)
                }
                crate::StorageEffectState::Reserved | crate::StorageEffectState::Dispatching => {
                    (StorageMutationStatus::EffectUnknown, EffectState::Unknown)
                }
            };

            Ok(Response::new(StorageMutationResponse {
                status: status as i32,
                state: state as i32,
                fence: Some(fence.clone()),
                operation_id,
                effect_id: format!("{}:{}", fence.action_id, fence.execution_epoch),
                etag: record.etag.unwrap_or_default(),
                provider_metadata: record.provider_metadata.unwrap_or_default(),
                error: if status == StorageMutationStatus::Confirmed {
                    None
                } else {
                    Some(ErrorDetail {
                        code: format!("{:?}", record.state),
                        category: "STORAGE".to_string(),
                        message: "storage effect state".to_string(),
                    })
                },
            }))
        }

        #[cfg(not(feature = "s3"))]
        {
            Ok(Response::new(StorageMutationResponse {
                status: StorageMutationStatus::EffectUnknown as i32,
                state: EffectState::Unknown as i32,
                fence: Some(fence.clone()),
                operation_id: input.operation_id,
                effect_id: String::new(),
                etag: String::new(),
                provider_metadata: String::new(),
                error: Some(ErrorDetail {
                    code: "EFFECT_UNKNOWN".to_string(),
                    category: "STORAGE".to_string(),
                    message: "no recorded effect for fence".to_string(),
                }),
            }))
        }
    }
}
