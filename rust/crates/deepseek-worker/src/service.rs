use std::sync::{Arc, Mutex, MutexGuard};

use deepseek_protocol::generated::deepseek::action::v1::{
    AdmitCommandRequest, AdmitCommandResponse, AdmitStatus, CommandKind, EffectResult,
    InstallAuthoritativeEpochRequest, InstallAuthoritativeEpochResponse, QueryEffectRequest,
    worker_server::Worker as WorkerRpc,
};
use deepseek_protocol::generated::deepseek::common::v1::{EffectState, ErrorDetail};
use deepseek_protocol::{
    AdmitError, is_federation_command, is_storage_command, is_transfer_command,
};
use tonic::{Request, Response, Status};

use crate::Worker;

#[derive(Debug, Clone)]
pub struct WorkerRpcService {
    worker: Arc<Mutex<Worker>>,
}

impl WorkerRpcService {
    pub fn new(worker: Worker) -> Self {
        Self {
            worker: Arc::new(Mutex::new(worker)),
        }
    }

    fn lock(&self) -> Result<MutexGuard<'_, Worker>, Status> {
        self.worker
            .lock()
            .map_err(|_| Status::unavailable("worker state unavailable"))
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

#[tonic::async_trait]
impl WorkerRpc for WorkerRpcService {
    async fn admit_command(
        &self,
        request: Request<AdmitCommandRequest>,
    ) -> Result<Response<AdmitCommandResponse>, Status> {
        let input = request.into_inner();
        // v1 retained this field for wire compatibility. It is caller-controlled
        // and must never establish or advance the worker's local authority.
        let _untrusted_live_epoch = input.live_epoch;
        let fence = match input.fence.as_ref() {
            Some(fence) => fence,
            None => return Ok(Response::new(rejected(AdmitError::EmptyActionId))),
        };
        if let Err(error) = self.lock()?.admit(fence) {
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
        let input = request.into_inner();
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
        match self.lock()?.query_effect(&fence) {
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
        let input = request.into_inner();
        let fence = match input.fence.as_ref() {
            Some(fence) => fence,
            None => return Ok(Response::new(install_rejected("EMPTY_ACTION_ID"))),
        };
        match self
            .lock()?
            .install_signed_epoch(fence, &input.canonical_request)
        {
            Ok(installed) => Ok(Response::new(InstallAuthoritativeEpochResponse {
                status: AdmitStatus::Admitted as i32,
                fence: Some(installed),
                error: None,
            })),
            Err(error) => Ok(Response::new(install_rejected(error.code))),
        }
    }
}
