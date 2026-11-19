//! One fenced Rust marker publication and a durable receipt for reconciliation.
use super::{LocationPolicy, observation_path, wire};
use crate::{
    AgentExecutionControl, WorkerAuthorityConfig, WorkerRpcService,
    target_marker_files::MarkerDirectory,
};
use deepseek_protocol::generated::deepseek::action::v1::{
    ReconcileTargetMarkerRequest, TargetMarkerClaim, TargetMarkerDispatchPermit,
    TargetMarkerDispatchRequest, TargetMarkerEffect, TargetMarkerPreparation, TargetMarkerReceipt,
    backup_target_marker_control_client::BackupTargetMarkerControlClient,
    backup_target_marker_worker_server::BackupTargetMarkerWorker,
};
use prost::Message as _;
use sha2::{Digest as _, Sha256};
use std::{io, path::Path, sync::Arc, time::Duration};
use tokio::sync::OwnedSemaphorePermit;
use tonic::{Request, Response, Status};

#[path = "target_marker_journal.rs"]
mod journal;
pub(super) use journal::MarkerJournal;
use journal::preparation_digest;

#[cfg(test)]
#[path = "target_marker_tests.rs"]
mod tests;

enum MarkerProof {
    Observed(TargetMarkerEffect),
    Retained(TargetMarkerReceipt),
}

fn claim_denied() -> Status {
    Status::failed_precondition("BACKUP_TARGET_MARKER_CLAIM_DENIED")
}

fn effect_unknown() -> Status {
    Status::unavailable("BACKUP_TARGET_MARKER_EFFECT_UNKNOWN")
}

fn lease_live(claim: &TargetMarkerClaim) -> bool {
    crate::service::unix_now_seconds()
        .is_ok_and(|now| now >= claim.issued_at_unix && now < claim.lease_until_unix)
}

fn authority_binds(
    authority: &WorkerAuthorityConfig,
    claim: &TargetMarkerClaim,
    current_writer: u64,
) -> bool {
    authority.fencing_token > 0
        && authority.fencing_token as u64 == current_writer
        && current_writer >= claim.writer_fencing_token
        && claim.fleet_id == authority.fleet_id
        && claim.environment == authority.environment
}

fn permit_binds(
    permit: &TargetMarkerDispatchPermit,
    claim: &TargetMarkerClaim,
    preparation: &[u8; 32],
) -> bool {
    permit.schema_version == 1
        && permit.lease_until_unix == claim.lease_until_unix
        && permit.preparation_sha256.as_slice() == preparation
}

async fn dispatch(
    control: &AgentExecutionControl,
    claim: &TargetMarkerClaim,
    preparation: &[u8; 32],
) -> Result<TargetMarkerDispatchPermit, Status> {
    let request = async {
        let channel = control
            .endpoint
            .connect()
            .await
            .map_err(|_| effect_unknown())?;
        let mut client =
            BackupTargetMarkerControlClient::new(channel).max_decoding_message_size(1024);
        let mut request = Request::new(TargetMarkerDispatchRequest {
            claim: Some(claim.clone()),
            preparation_sha256: preparation.to_vec(),
            fence: claim.fence.clone(),
        });
        request
            .metadata_mut()
            .insert("authorization", control.authorization.clone());
        let response = client
            .begin_dispatch(request)
            .await
            .map_err(|_| effect_unknown())?
            .into_inner();
        if !permit_binds(&response, claim, preparation) {
            return Err(effect_unknown());
        }
        Ok(response)
    };
    tokio::time::timeout(Duration::from_secs(10), request)
        .await
        .map_err(|_| effect_unknown())?
}

fn open_directory(
    policy: &LocationPolicy,
    claim: &TargetMarkerClaim,
) -> Result<MarkerDirectory, Status> {
    let path = policy.validated_path(&claim.requested_path)?;
    policy.check_contributors(&path)?;
    if observation_path(&path, &claim.requested_path, true)? != claim.canonical_path {
        return Err(claim_denied());
    }
    let directory = MarkerDirectory::open(&path).map_err(|_| claim_denied())?;
    if directory.identity().map_err(|_| claim_denied())?.as_slice() != claim.directory_identity {
        return Err(claim_denied());
    }
    Ok(directory)
}

fn read_effect(
    policy: &LocationPolicy,
    prepared: &TargetMarkerPreparation,
) -> Result<TargetMarkerEffect, Status> {
    let claim = prepared.claim.as_ref().ok_or_else(effect_unknown)?;
    let directory = open_directory(policy, claim).map_err(|_| effect_unknown())?;
    let bytes = directory
        .marker(wire::MARKER_INITIALIZATION_MAX_JSON)
        .map_err(|_| effect_unknown())?
        .filter(|bytes| bytes == &prepared.marker_json)
        .ok_or_else(effect_unknown)?;
    #[cfg(unix)]
    {
        // Android exclusive creation can release the mode before syncing it.
        #[cfg(target_os = "android")]
        directory
            .sync_observed_marker_file(&bytes)
            .map_err(|_| effect_unknown())?;
        // A worker can exit before syncing the published directory entry.
        directory.sync().map_err(|_| effect_unknown())?;
        if directory
            .marker(wire::MARKER_INITIALIZATION_MAX_JSON)
            .map_err(|_| effect_unknown())?
            .as_deref()
            != Some(bytes.as_slice())
        {
            return Err(effect_unknown());
        }
    }
    open_directory(policy, claim).map_err(|_| effect_unknown())?;
    Ok(TargetMarkerEffect {
        schema_version: 1,
        fence: claim.fence.clone(),
        operation_id: claim.operation_id.clone(),
        claim_sha256: Sha256::digest(claim.encode_to_vec()).to_vec(),
        preparation_sha256: preparation_digest(prepared).to_vec(),
        canonical_path: claim.canonical_path.clone(),
        directory_identity: prepared.directory_identity.clone(),
        marker_sha256: Sha256::digest(&bytes).to_vec(),
        marker_json: bytes,
    })
}

impl LocationPolicy {
    fn marker_journal(&self) -> Result<&MarkerJournal, Status> {
        self.marker_journal.as_ref().ok_or_else(claim_denied)
    }

    fn marker_gate(&self) -> Result<OwnedSemaphorePermit, Status> {
        self.readers
            .clone()
            .try_acquire_owned()
            .map_err(|_| Status::resource_exhausted("BACKUP_TARGET_INSPECTION_BUSY"))
    }
}

impl WorkerRpcService {
    pub fn with_backup_target_marker_journal(mut self, state_root: &Path) -> Result<Self, Status> {
        let policy = self
            .target_inspection
            .as_mut()
            .and_then(Arc::get_mut)
            .ok_or_else(claim_denied)?;
        let state_root = state_root.canonicalize().map_err(|_| claim_denied())?;
        let workspace = policy.protected.first().ok_or_else(claim_denied)?;
        policy.marker_journal =
            Some(MarkerJournal::new(&state_root, workspace).map_err(|_| claim_denied())?);
        policy.protected.push(state_root);
        Ok(self)
    }

    async fn marker_receipt<T>(
        &self,
        request: &Request<T>,
        claim: &TargetMarkerClaim,
        current_writer: u64,
        proof: MarkerProof,
        gate: OwnedSemaphorePermit,
    ) -> Result<Response<TargetMarkerReceipt>, Status> {
        self.target_inspection_caller(request)?;
        let authority = self.worker.lock().await.agent_node_authority()?;
        if !authority_binds(&authority, claim, current_writer) {
            return Err(effect_unknown());
        }
        let receipt = {
            let signer = self
                .control_signer
                .as_ref()
                .ok_or_else(effect_unknown)?
                .lock()
                .await;
            let signer_public_key = signer.key.public_key();
            if signer_public_key != authority.signer_public_key {
                return Err(effect_unknown());
            }
            match proof {
                MarkerProof::Retained(receipt) => {
                    signer
                        .key
                        .verify_backup_target_marker_receipt(claim, &receipt)
                        .map_err(|_| effect_unknown())?;
                    return Ok(Response::new(receipt));
                }
                MarkerProof::Observed(effect) => {
                    let signature = signer
                        .key
                        .sign_backup_target_marker_effect(claim, &effect)
                        .map_err(|_| effect_unknown())?;
                    TargetMarkerReceipt {
                        effect: Some(effect),
                        signature,
                        signer_public_key,
                    }
                }
            }
        };
        let policy = self.target_inspection.clone().ok_or_else(effect_unknown)?;
        let claim = claim.clone();
        let receipt = tokio::task::spawn_blocking(move || {
            let _gate = gate;
            policy.marker_journal()?.retain_receipt(&claim, &receipt)?;
            Ok::<_, Status>(receipt)
        })
        .await
        .map_err(|_| effect_unknown())??;
        self.target_inspection_caller(request)?;
        Ok(Response::new(receipt))
    }
}

#[tonic::async_trait]
impl BackupTargetMarkerWorker for WorkerRpcService {
    async fn initialize(
        &self,
        request: Request<TargetMarkerClaim>,
    ) -> Result<Response<TargetMarkerReceipt>, Status> {
        self.target_inspection_caller(&request)?;
        let claim = request.get_ref();
        let authority = self.worker.lock().await.agent_node_authority()?;
        if !wire::valid_marker_claim(claim)
            || !lease_live(claim)
            || !authority_binds(&authority, claim, claim.writer_fencing_token)
        {
            return Err(claim_denied());
        }
        let control = self
            .agent_execution_control
            .as_ref()
            .filter(|control| control.endpoint.uri().scheme_str() == Some("https"))
            .ok_or_else(claim_denied)?;
        let policy = self
            .target_inspection
            .as_ref()
            .ok_or_else(claim_denied)?
            .clone();
        let gate = policy.marker_gate()?;
        let admitted = claim.clone();
        let io_policy = policy.clone();
        let (directory, prepared, gate) = tokio::task::spawn_blocking(move || {
            let directory = open_directory(&io_policy, &admitted)?;
            if directory
                .marker(wire::MARKER_INITIALIZATION_MAX_JSON)
                .map_err(|_| claim_denied())?
                .is_some()
            {
                return Err(claim_denied());
            }
            let prepared = io_policy.marker_journal()?.prepare(&admitted)?;
            Ok::<_, Status>((directory, prepared, gate))
        })
        .await
        .map_err(|_| claim_denied())??;
        self.target_inspection_caller(&request)?;
        if !lease_live(claim) {
            return Err(claim_denied());
        }
        let digest = preparation_digest(&prepared);
        let permit = dispatch(control, claim, &digest).await?;
        self.target_inspection_caller(&request)?;
        let authority = self.worker.lock().await.agent_node_authority()?;
        if !authority_binds(&authority, claim, claim.writer_fencing_token) || !lease_live(claim) {
            return Err(effect_unknown());
        }
        let original = claim.clone();
        let (effect, gate) = tokio::task::spawn_blocking(move || {
            policy.marker_journal()?.accept(&original, &permit)?;
            directory
                .create_marker(&prepared.marker_json, &digest, || {
                    open_directory(&policy, &original).map_err(|_| {
                        io::Error::other("backup target directory no longer matches claim")
                    })?;
                    if !lease_live(&original) {
                        return Err(io::Error::other("backup target marker permit expired"));
                    }
                    Ok(())
                })
                .map_err(|_| effect_unknown())?;
            Ok::<_, Status>((read_effect(&policy, &prepared)?, gate))
        })
        .await
        .map_err(|_| effect_unknown())??;
        self.marker_receipt(
            &request,
            claim,
            claim.writer_fencing_token,
            MarkerProof::Observed(effect),
            gate,
        )
        .await
    }

    async fn reconcile(
        &self,
        request: Request<ReconcileTargetMarkerRequest>,
    ) -> Result<Response<TargetMarkerReceipt>, Status> {
        self.target_inspection_caller(&request)?;
        let input = request.get_ref();
        let fence = input.fence.as_ref().ok_or_else(claim_denied)?;
        let authority = self.worker.lock().await.agent_node_authority()?;
        if input.schema_version != 1
            || deepseek_protocol::validate_fence(fence).is_err()
            || input.operation_id != fence.action_id
            || input.operation_id.len() > 128
            || input.claim_sha256.len() != 32
            || input.current_writer_fencing_token != authority.fencing_token as u64
        {
            return Err(claim_denied());
        }
        let policy = self
            .target_inspection
            .as_ref()
            .ok_or_else(claim_denied)?
            .clone();
        let gate = policy.marker_gate()?;
        let operation = input.operation_id.clone();
        let original_fence = fence.clone();
        let digest = input.claim_sha256.clone();
        let writer = input.current_writer_fencing_token;
        let (claim, proof, gate) = tokio::task::spawn_blocking(move || {
            let journal = policy.marker_journal()?;
            let prepared =
                journal.dispatched(&operation, original_fence.execution_epoch, &digest)?;
            let claim = prepared.claim.as_ref().ok_or_else(effect_unknown)?;
            if !authority_binds(&authority, claim, writer)
                || claim.fence.as_ref() != Some(&original_fence)
            {
                return Err(effect_unknown());
            }
            let proof = match journal.retained_receipt(&prepared)? {
                Some(receipt) => MarkerProof::Retained(receipt),
                None => MarkerProof::Observed(read_effect(&policy, &prepared)?),
            };
            Ok::<_, Status>((claim.clone(), proof, gate))
        })
        .await
        .map_err(|_| effect_unknown())??;
        self.marker_receipt(&request, &claim, writer, proof, gate)
            .await
    }
}
