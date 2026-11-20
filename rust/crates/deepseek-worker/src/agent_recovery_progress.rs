//! Signed public progress follows an independently terminated rerun task.
use crate::WorkerRpcService;
use deepseek_policy::{
    agent_run_data::{ArtifactReference, ArtifactStore},
    multi_agent,
};
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentArtifactReference, AgentRecoveryProgressContext, AgentRecoveryProgressEvidence,
    AgentRecoveryProgressReceipt, PrepareAgentRecoveryProgressRequest,
};
use serde_json::json;
use tonic::{Request, Response, Status};

fn denied() -> Status {
    Status::failed_precondition("AGENT_RECOVERY_PROGRESS_INVALID")
}

fn progress_bodies(
    store: &mut ArtifactStore,
    context: &AgentRecoveryProgressContext,
) -> Result<(AgentArtifactReference, AgentArtifactReference), Status> {
    let (name, _) = multi_agent::profile(&context.role).ok_or_else(denied)?;
    let note = json!({"type":"agent_note","phase":context.role,"name":name,
        "text":"其他 Agent 未自动重跑；最终回答将基于最新该 Agent 与现有其他 Agent 输出重新综合。"});
    let next = if context.synthesize {
        json!({"type":"final_reset","scope":"final_answer","reason":"rerun_agent"})
    } else {
        json!({"type":"run_status","status":"done"})
    };
    let reference = |r: ArtifactReference| AgentArtifactReference {
        sha256: r.sha256,
        length: r.length,
    };
    Ok((
        reference(store.put(&note).map_err(|_| denied())?),
        reference(store.put(&next).map_err(|_| denied())?),
    ))
}

impl WorkerRpcService {
    pub(crate) async fn prepare_agent_recovery_progress(
        &self,
        request: Request<PrepareAgentRecoveryProgressRequest>,
    ) -> Result<Response<AgentRecoveryProgressReceipt>, Status> {
        let identity = self
            .authenticator
            .authenticate(request.metadata())
            .map_err(|error| Status::unauthenticated(error.code()))?;
        if identity.service_name != "go-control-plane" || identity.role != "controller" {
            return Err(Status::permission_denied("AGENT_NODE_CALLER_DENIED"));
        }
        let input = request.into_inner();
        let context = input.context.ok_or_else(denied)?;
        let fence = input.fence.ok_or_else(denied)?;
        if input.schema_version != 1
            || context.schema_version != 1
            || context.run_id.is_empty()
            || fence.action_id != context.run_id
            || fence.execution_epoch != context.metadata_epoch
            || deepseek_protocol::validate_fence(&fence).is_err()
            || context.expected_index < 1
            || context.expected_index > i64::MAX - 2
            || context.runtime_revision < 2
            || context.runtime_revision > i64::MAX as u64
        {
            return Err(denied());
        }
        let control = self.agent_execution_control.as_ref().ok_or_else(denied)?;
        let epoch = control.metadata_epoch().await?;
        if context.metadata_epoch < epoch {
            return Err(Status::failed_precondition("STALE_EXECUTION_EPOCH"));
        }
        if context.metadata_epoch > epoch {
            return Err(Status::failed_precondition("FENCE_MISMATCH"));
        }
        // A caller's claimed task completion is never sufficient. Go repeats the
        // original dispatch, signed outcome and independent terminal audit.
        control.validate_recovery_progress(&context).await?;
        let run = control.runtime_binding(&context.run_id, epoch).await?;
        if run.next_index != context.expected_index
            || run.runtime_revision != context.runtime_revision
            || run.request != context.request
            || run.status != "running"
        {
            return Err(denied());
        }
        let executor = self.agent_plan_executor.as_ref().ok_or_else(denied)?;
        let root = executor.artifact_root.clone();
        let request = context.request.as_ref().ok_or_else(denied)?.clone();
        let root_copy = root.clone();
        let recovery = tokio::task::spawn_blocking(move || {
            let store = ArtifactStore::open_readonly(&root_copy).map_err(|_| denied())?;
            let body = store
                .get(&ArtifactReference {
                    sha256: request.sha256,
                    length: request.length,
                })
                .map_err(|_| denied())?;
            crate::agent_recovery_context::read(&root_copy, &body)?.ok_or_else(denied)
        })
        .await
        .map_err(|_| denied())??;
        crate::agent_recovery_context::validate_binding(Some(&recovery), &run)?;
        let authority = {
            let worker = self.worker.lock().await;
            let authority = worker.agent_node_authority()?;
            worker
                .authority_store
                .as_ref()
                .ok_or_else(denied)?
                .verify_agent_recovery_progress(&context, &recovery.context, &authority)?;
            authority
        };
        let signer = self.control_signer.as_ref().ok_or_else(denied)?;
        {
            let signer = signer.lock().await;
            for source in context
                .outputs
                .iter()
                .chain(&recovery.context.retained_outputs)
            {
                signer.verify_retained_node_outcome(source.outcome.as_ref().ok_or_else(denied)?)?;
            }
        }
        let context_copy = context.clone();
        let (note_body, next_body) = tokio::task::spawn_blocking(move || {
            let mut store =
                ArtifactStore::open_native(&root, "python_disabled").map_err(|_| denied())?;
            progress_bodies(&mut store, &context_copy)
        })
        .await
        .map_err(|_| denied())??;
        let receipt =
            signer
                .lock()
                .await
                .sign_recovery_progress(AgentRecoveryProgressEvidence {
                    schema_version: 1,
                    context: Some(context),
                    note_body: Some(note_body),
                    next_body: Some(next_body),
                    fleet_id: authority.fleet_id,
                    environment: authority.environment,
                })?;
        Ok(Response::new(receipt))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rerun_progress_bodies_retain_note_then_reset_or_done_without_private_data() {
        let root = tempfile::tempdir().unwrap();
        let mut store = ArtifactStore::open_native(root.path(), "python_disabled").unwrap();
        for synthesize in [true, false] {
            let context = AgentRecoveryProgressContext {
                role: "critic".into(),
                synthesize,
                ..Default::default()
            };
            let (note, next) = progress_bodies(&mut store, &context).unwrap();
            let read = |r: &AgentArtifactReference| {
                store
                    .get(&ArtifactReference {
                        sha256: r.sha256.clone(),
                        length: r.length,
                    })
                    .unwrap()
            };
            assert_eq!(
                read(&note),
                json!({"type":"agent_note","phase":"critic","name":"反驳审查 Agent",
                "text":"其他 Agent 未自动重跑；最终回答将基于最新该 Agent 与现有其他 Agent 输出重新综合。"})
            );
            assert_eq!(
                read(&next),
                if synthesize {
                    json!({"type":"final_reset","scope":"final_answer","reason":"rerun_agent"})
                } else {
                    json!({"type":"run_status","status":"done"})
                }
            );
            assert_eq!(progress_bodies(&mut store, &context).unwrap(), (note, next));
        }
        assert!(
            progress_bodies(
                &mut store,
                &AgentRecoveryProgressContext {
                    role: "leader".into(),
                    ..Default::default()
                }
            )
            .is_err()
        );
    }
}
