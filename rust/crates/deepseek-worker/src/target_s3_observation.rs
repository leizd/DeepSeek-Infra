//! Signed reads of the existing provider identity and head.
use crate::WorkerRpcService;
use deepseek_protocol::{
    backup_target_inspection as wire,
    generated::deepseek::action::v1::{
        InspectS3BackupTargetRequest, S3BackupTargetInspectionReceipt,
        S3BackupTargetObjectObservation, S3BackupTargetObservation,
    },
};
use deepseek_storage::s3::ObservedTargetObject;
use sha2::{Digest as _, Sha256};
use std::time::Duration;
use tonic::{Request, Response, Status};

fn object_observation(object: ObservedTargetObject) -> S3BackupTargetObjectObservation {
    S3BackupTargetObjectObservation {
        sha256: Sha256::digest(&object.bytes).to_vec(),
        json: object.bytes.to_vec(),
        etag: object.observation.etag,
        version_id: object.observation.version.unwrap_or_default(),
    }
}

impl WorkerRpcService {
    pub(super) async fn inspect_configured_s3_target(
        &self,
        request: Request<InspectS3BackupTargetRequest>,
    ) -> Result<Response<S3BackupTargetInspectionReceipt>, Status> {
        let input = request.get_ref();
        if !wire::valid_s3_request(input) {
            return Err(Status::invalid_argument(
                "BACKUP_TARGET_S3_INSPECTION_INVALID",
            ));
        }
        let initial = self.worker.lock().await.agent_node_authority()?;
        if input.writer_fencing_token != initial.fencing_token as u64 {
            return Err(Status::failed_precondition("FENCE_MISMATCH"));
        }
        let transport = self
            .transport
            .as_ref()
            .ok_or_else(|| Status::unavailable("BACKUP_TARGET_S3_TRANSPORT_UNAVAILABLE"))?;
        if input.target_identity.as_slice() != transport.target_identity().as_slice() {
            return Err(Status::failed_precondition(
                "BACKUP_TARGET_S3_PLACEMENT_MISMATCH",
            ));
        }
        let signer = self
            .control_signer
            .as_ref()
            .ok_or_else(|| Status::unavailable("CONTROL_SIGNER_UNAVAILABLE"))?;
        let unavailable = || Status::unavailable("BACKUP_TARGET_S3_OBSERVATION_UNRESOLVED");
        let objects =
            tokio::time::timeout(Duration::from_secs(10), transport.observe_backup_target())
                .await
                .map_err(|_| unavailable())?
                .map_err(|_| unavailable())?;
        self.target_inspection_caller(&request)?;
        let worker = self.worker.lock().await;
        let authority = worker.agent_node_authority()?;
        let signer = signer.lock().await;
        let public_key = signer.key.public_key();
        if authority.fencing_token != initial.fencing_token
            || authority.fleet_id != initial.fleet_id
            || authority.environment != initial.environment
            || authority.signer_public_key != initial.signer_public_key
            || authority.signer_public_key != public_key
        {
            return Err(Status::failed_precondition("FENCE_MISMATCH"));
        }
        let observation = S3BackupTargetObservation {
            schema_version: 1,
            fence: input.fence.clone(),
            operation_id: input.operation_id.clone(),
            writer_fencing_token: input.writer_fencing_token,
            target_identity: input.target_identity.clone(),
            bucket: transport.bucket().into(),
            prefix: transport.prefix().into(),
            identity: objects.identity.map(object_observation),
            head: objects.head.map(object_observation),
            observed_at_unix: crate::service::unix_now_seconds().map_err(|_| unavailable())?,
            fleet_id: authority.fleet_id,
            environment: authority.environment,
        };
        let signature = signer
            .key
            .sign_s3_backup_target_observation(&observation)
            .map_err(|_| unavailable())?;
        Ok(Response::new(S3BackupTargetInspectionReceipt {
            schema_version: 1,
            observation: Some(observation),
            signature,
            signer_public_key: public_key,
        }))
    }
}
