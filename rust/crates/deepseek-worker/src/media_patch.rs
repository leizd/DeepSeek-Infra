//! Typed, authenticated, Go-admitted media effects. No Python or task-program path.
use crate::{
    WorkerAuthorityConfig, WorkerRpcService,
    media_journal::MediaJournal,
    media_values::{invalid, to_json},
};
use deepseek_protocol::{
    ActionFence,
    generated::deepseek::media::v1::{
        ExecuteMediaDeleteRequest, ExecuteMediaPatchRequest, ExecuteMediaProcessRequest,
        MediaMutationKind, MediaPatchClaim, MediaPatchOutcome, QueryMediaPatchRequest,
        QueryMediaPatchResponse, media_mutation_worker_server::MediaMutationWorker,
    },
};
use prost::Message as _;
use std::path::Path;
use std::sync::Arc;
use tokio::sync::Mutex;
use tonic::{Request, Response, Status};

pub(super) fn validate_claim(
    c: &MediaPatchClaim,
    operation: &str,
    fence: &ActionFence,
    authority: &WorkerAuthorityConfig,
    now: i64,
) -> Result<(), Status> {
    let intent = c.intent.as_ref().ok_or_else(invalid)?;
    let is_process = intent.kind == MediaMutationKind::Process as i32;
    if !is_process && intent.process.is_some() {
        return Err(invalid());
    }
    let is_delete = intent.kind == MediaMutationKind::Delete as i32;
    let is_create = intent.kind == MediaMutationKind::Create as i32;
    if intent.kind != MediaMutationKind::Unspecified as i32
        && !is_delete
        && !is_create
        && !is_process
    {
        return Err(invalid());
    }
    let prefix = if is_process {
        "media-process-"
    } else if is_create {
        "media-create-"
    } else if is_delete {
        "media-delete-"
    } else {
        "media-patch-"
    };
    if c.schema_version != 1
        || intent.schema_version != 1
        || intent.operation_id != operation
        || c.fence.as_ref() != Some(fence)
        || fence.action_id != operation
        || fence.execution_epoch != 1
        || operation.len() > 128
        || !operation.starts_with(prefix)
        || c.request_sha256 != crate::media_library::sha(&intent.encode_to_vec())
        || c.owner.is_empty()
        || c.claim_token.len() != 64
        || c.writer_fencing_token != authority.fencing_token
        || c.fleet_id != authority.fleet_id
        || c.environment != authority.environment
        || c.lease_until_unix <= now
        || now < 0
        || c.lease_until_unix > now.saturating_add(60)
        || c.state != "CLAIMED"
        || intent.encoded_len() > 4 * 1024 * 1024
    {
        return Err(invalid());
    }
    if is_process {
        return if intent.patch.is_none()
            && intent.create.is_none()
            && operation
                .strip_prefix("media-process-")
                .is_some_and(|nonce| {
                    nonce.len() == 32
                        && nonce
                            .bytes()
                            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
                })
            && intent.process.as_ref().is_some_and(|p| {
                p.correlation_id.len() <= 64
                    && p.correlation_id
                        .bytes()
                        .all(|c| c.is_ascii_alphanumeric() || b"-_.:".contains(&c))
            })
            && !intent.media_id.is_empty()
            && intent.media_id.len() <= 512
        {
            Ok(())
        } else {
            Err(invalid())
        };
    }
    if is_create {
        return if intent.patch.is_none()
            && intent.create.as_ref().is_some_and(|create| {
                create.payload_sha256.len() == 32
                    && create.payload_bytes > 0
                    && create.payload_bytes <= crate::media_create::MAX_PAYLOAD_BYTES as u64
            }) {
            Ok(())
        } else {
            Err(invalid())
        };
    }
    if intent.create.is_some() {
        return Err(invalid());
    }
    if is_delete {
        return if intent.patch.is_none() {
            Ok(())
        } else {
            Err(invalid())
        };
    }
    let patch = intent.patch.as_ref().ok_or_else(invalid)?;
    if patch.title.is_none() && patch.project_id.is_none() && patch.metadata.is_none() {
        return Err(invalid());
    }
    for v in [&patch.title, &patch.project_id, &patch.metadata]
        .into_iter()
        .flatten()
    {
        to_json(v)?;
    }
    Ok(())
}
impl WorkerRpcService {
    pub fn with_media_journal(
        mut self,
        root: &Path,
        state: &Path,
        authority: &WorkerAuthorityConfig,
    ) -> Result<Self, Status> {
        if self.control_signer.is_none() || self.agent_execution_control.is_none() {
            return Err(invalid());
        }
        self.media_journal = Some(Arc::new(Mutex::new(MediaJournal::open(
            root,
            state,
            &authority.signer_public_key,
            &authority.fleet_id,
            &authority.environment,
        )?)));
        Ok(self)
    }
    pub(super) fn media_caller<T>(&self, request: &Request<T>) -> Result<(), Status> {
        let identity = self
            .authenticator
            .authenticate(request.metadata())
            .map_err(|e| Status::unauthenticated(e.code()))?;
        if identity.service_name != "go-control-plane" || identity.role != "controller" {
            return Err(Status::permission_denied("MEDIA_CALLER_DENIED"));
        }
        Ok(())
    }
}
impl WorkerRpcService {
    async fn execute_media_mutation(
        &self,
        request: Request<ExecuteMediaPatchRequest>,
        expected_kind: MediaMutationKind,
    ) -> Result<Response<MediaPatchOutcome>, Status> {
        self.media_caller(&request)?;
        let input = request.into_inner();
        let fence = input.fence.as_ref().ok_or_else(invalid)?;
        if input.schema_version != 1
            || input.operation_id != fence.action_id
            || fence.execution_epoch != 1
        {
            return Err(invalid());
        }
        let control = self.agent_execution_control.as_ref().ok_or_else(invalid)?;
        let claim = control.media_claim(&input.operation_id, fence).await?;
        if claim
            .intent
            .as_ref()
            .is_none_or(|intent| intent.kind != expected_kind as i32)
        {
            return Err(invalid());
        }
        let config = self.worker.lock().await.agent_node_authority()?;
        let now = crate::service::unix_now_seconds().map_err(|_| invalid())?;
        validate_claim(&claim, &input.operation_id, fence, &config, now)?;
        let mut journal = self
            .media_journal
            .as_ref()
            .ok_or_else(invalid)?
            .lock()
            .await;
        // A cancelled old Go admission can leave a prepared Rust row if its
        // process died before receiving the refusal. Reconcile that original
        // row before another operation, never manufacture a new effect.
        if let Some(active) = journal.active()? {
            let original = active.intent.as_ref().ok_or_else(invalid)?;
            if original.operation_id != input.operation_id {
                let original_fence = active.fence.as_ref().ok_or_else(invalid)?;
                let live = control
                    .media_claim(&original.operation_id, original_fence)
                    .await?;
                journal.verify_original_claim(&live)?;
                if !matches!(live.state.as_str(), "CANCELLED" | "REFUSED" | "APPLIED") {
                    return Err(Status::unavailable("EFFECT_UNKNOWN"));
                }
                let signer = self
                    .control_signer
                    .as_ref()
                    .ok_or_else(invalid)?
                    .lock()
                    .await;
                journal.query(&original.operation_id, original_fence, true, &signer.key)?;
            }
        }
        let prepared = journal.prepare(&claim)?;
        if let Some(result) = prepared.terminal {
            return Ok(Response::new(result));
        }
        let permit = control.media_begin(&claim, &prepared.digest).await?;
        let admitted = permit.claim.as_ref().ok_or_else(invalid)?;
        let mut expected = claim.clone();
        expected.state = "DISPATCHED".into();
        if !permit.fresh || *admitted != expected || permit.preparation_sha256 != prepared.digest {
            return Err(Status::unavailable("EFFECT_UNKNOWN"));
        }
        let mut original = claim.clone();
        original.state = "CLAIMED".into();
        let now = crate::service::unix_now_seconds().map_err(|_| invalid())?;
        let config = self.worker.lock().await.agent_node_authority()?;
        validate_claim(&original, &input.operation_id, fence, &config, now)?;
        let signer = self
            .control_signer
            .as_ref()
            .ok_or_else(invalid)?
            .lock()
            .await;
        journal
            .publish(&original, &prepared.digest, &signer.key)
            .map(Response::new)
    }
}

#[tonic::async_trait]
impl MediaMutationWorker for WorkerRpcService {
    async fn execute_process(
        &self,
        request: Request<ExecuteMediaProcessRequest>,
    ) -> Result<Response<MediaPatchOutcome>, Status> {
        let (metadata, extensions, input) = request.into_parts();
        self.execute_media_mutation(
            Request::from_parts(
                metadata,
                extensions,
                ExecuteMediaPatchRequest {
                    schema_version: input.schema_version,
                    operation_id: input.operation_id,
                    fence: input.fence,
                },
            ),
            MediaMutationKind::Process,
        )
        .await
    }
    async fn execute_create(
        &self,
        request: Request<ExecuteMediaPatchRequest>,
    ) -> Result<Response<MediaPatchOutcome>, Status> {
        self.execute_media_mutation(request, MediaMutationKind::Create)
            .await
    }
    async fn execute_patch(
        &self,
        request: Request<ExecuteMediaPatchRequest>,
    ) -> Result<Response<MediaPatchOutcome>, Status> {
        self.execute_media_mutation(request, MediaMutationKind::Unspecified)
            .await
    }
    async fn execute_delete(
        &self,
        request: Request<ExecuteMediaDeleteRequest>,
    ) -> Result<Response<MediaPatchOutcome>, Status> {
        let (metadata, extensions, input) = request.into_parts();
        let forwarded = Request::from_parts(
            metadata,
            extensions,
            ExecuteMediaPatchRequest {
                schema_version: input.schema_version,
                operation_id: input.operation_id,
                fence: input.fence,
            },
        );
        self.execute_media_mutation(forwarded, MediaMutationKind::Delete)
            .await
    }
    async fn query_patch(
        &self,
        request: Request<QueryMediaPatchRequest>,
    ) -> Result<Response<QueryMediaPatchResponse>, Status> {
        self.media_caller(&request)?;
        let input = request.into_inner();
        let fence = input.original_fence.as_ref().ok_or_else(invalid)?;
        if input.schema_version != 1
            || input.operation_id != fence.action_id
            || fence.execution_epoch != 1
            || input.operation_id.len() > 128
        {
            return Err(invalid());
        }
        let live = self
            .agent_execution_control
            .as_ref()
            .ok_or_else(invalid)?
            .media_claim(&input.operation_id, fence)
            .await?;
        if live.fence.as_ref() != Some(fence)
            || live
                .intent
                .as_ref()
                .is_none_or(|i| i.operation_id != input.operation_id)
        {
            return Err(invalid());
        }
        let confirmed = matches!(
            live.state.as_str(),
            "CLAIMED" | "DISPATCHED" | "EFFECT_UNKNOWN" | "CANCELLED" | "APPLIED" | "REFUSED"
        );
        let mut journal = self
            .media_journal
            .as_ref()
            .ok_or_else(invalid)?
            .lock()
            .await;
        journal.verify_original_claim(&live)?;
        let signer = self
            .control_signer
            .as_ref()
            .ok_or_else(invalid)?
            .lock()
            .await;
        let outcome = journal.query(&input.operation_id, fence, confirmed, &signer.key)?;
        Ok(Response::new(QueryMediaPatchResponse {
            found: outcome.is_some(),
            outcome,
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use deepseek_protocol::generated::deepseek::media::v1::{MediaPatch, MediaPatchIntent};
    #[test]
    fn media_execution_admission_binds_epoch_writer_realm_request_and_lease() {
        let intent = MediaPatchIntent {
            kind: 0,
            schema_version: 1,
            operation_id: "media-patch-isolated".into(),
            media_id: "media_one".into(),
            patch: Some(MediaPatch {
                title: Some(crate::media_values::from_json(&serde_json::json!("title")).unwrap()),
                ..Default::default()
            }),
            create: None,
        };
        let fence = ActionFence {
            action_id: intent.operation_id.clone(),
            execution_epoch: 1,
        };
        let authority = WorkerAuthorityConfig {
            signer_public_key: "public-fixture".into(),
            fleet_id: "fleet-a".into(),
            environment: "test".into(),
            fencing_token: 7,
            now: None,
        };
        let claim = MediaPatchClaim {
            schema_version: 1,
            request_sha256: crate::media_library::sha(&intent.encode_to_vec()),
            intent: Some(intent),
            fence: Some(fence.clone()),
            owner: "go".into(),
            claim_token: "a".repeat(64),
            writer_fencing_token: 7,
            lease_until_unix: 1060,
            state: "CLAIMED".into(),
            fleet_id: "fleet-a".into(),
            environment: "test".into(),
        };
        validate_claim(&claim, "media-patch-isolated", &fence, &authority, 1000).unwrap();
        let mutations: [fn(&mut MediaPatchClaim); 10] = [
            |c| c.fence.as_mut().unwrap().execution_epoch = 2,
            |c| c.writer_fencing_token = 8,
            |c| c.fleet_id = "foreign".into(),
            |c| c.environment = "foreign".into(),
            |c| c.request_sha256[0] ^= 1,
            |c| c.lease_until_unix = 1000,
            |c| c.lease_until_unix = 1061,
            |c| c.state = "DISPATCHED".into(),
            |c| c.claim_token.clear(),
            |c| c.intent.as_mut().unwrap().patch = None,
        ];
        for mutate in mutations {
            let mut changed = claim.clone();
            mutate(&mut changed);
            assert!(
                validate_claim(&changed, "media-patch-isolated", &fence, &authority, 1000).is_err()
            );
        }
    }
}
