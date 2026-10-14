//! Final public bodies are derived solely from confirmed synthesis custody.
use crate::WorkerRpcService;
use deepseek_policy::agent_run_data::{ArtifactReference, ArtifactStore, DataError};
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentArtifactReference, AgentSynthesisProjectionEvidence, AgentSynthesisProjectionReceipt,
    ProjectAgentSynthesisRequest, agent_node_worker_server::AgentNodeWorker,
};
use prost::Message;
use serde_json::json;
use sha2::{Digest, Sha256};
use tonic::{Request, Response, Status};

fn invalid() -> Status {
    Status::failed_precondition("AGENT_SYNTHESIS_PROJECTION_INVALID")
}

fn derive_done_body(
    store: &mut ArtifactStore,
    content: &AgentArtifactReference,
) -> Result<ArtifactReference, DataError> {
    let body = store.get(&ArtifactReference {
        sha256: content.sha256.clone(),
        length: content.length,
    })?;
    if body["type"] != "content"
        || body["text"]
            .as_str()
            .is_none_or(|text| text.trim().is_empty())
        || body.as_object().is_none_or(|fields| fields.len() != 2)
    {
        return Err(DataError::Corrupt);
    }
    store.put(&json!({"type":"done"}))
}

impl WorkerRpcService {
    pub(crate) async fn project_agent_synthesis(
        &self,
        request: Request<ProjectAgentSynthesisRequest>,
    ) -> Result<Response<AgentSynthesisProjectionReceipt>, Status> {
        let headers = request.metadata().clone();
        let input = request.into_inner();
        let source = input.source.clone().ok_or_else(invalid)?;
        let source_fence = source.fence.as_ref().ok_or_else(invalid)?;
        let fence = input.settlement_fence.clone().ok_or_else(invalid)?;
        if input.schema_version != 1
            || deepseek_protocol::validate_fence(&fence).is_err()
            || source.node_id != "synthesizer"
            || fence.action_id != source_fence.action_id
            || fence.execution_epoch < source_fence.execution_epoch
            || input.settlement_writer_token <= 0
            || input.metadata_epoch == 0
            || input.expected_index < 0
            || input.expected_index > i64::MAX - 2
        {
            return Err(invalid());
        }
        // Query performs authenticated custody lookup, never a fresh dispatch.
        let mut query = Request::new(source);
        *query.metadata_mut() = headers;
        let effect = AgentNodeWorker::query_effect(self, query)
            .await?
            .into_inner()
            .effect
            .filter(|e| e.state == "CONFIRMED")
            .ok_or_else(invalid)?;
        let receipt = effect.outcome_receipt.ok_or_else(invalid)?;
        let evidence = receipt.evidence.as_ref().ok_or_else(invalid)?;
        if evidence.phase != 3
            || evidence.node_id != "synthesizer"
            || evidence.metadata_epoch != input.metadata_epoch
            || evidence.metadata_index != input.expected_index
            || evidence
                .task_output
                .as_ref()
                .is_none_or(|stats| stats.failed)
        {
            return Err(invalid());
        }
        let executor = self.agent_plan_executor.as_ref().ok_or_else(invalid)?;
        let root = executor.artifact_root.clone();
        let content = evidence.public_event.clone().ok_or_else(invalid)?;
        let content_copy = content.clone();
        let done = tokio::task::spawn_blocking(move || {
            let mut store = ArtifactStore::open_native(&root, "python_disabled")?;
            derive_done_body(&mut store, &content_copy)
        })
        .await
        .map_err(|_| invalid())?
        .map_err(|_| invalid())?;
        let worker = self.worker.lock().await;
        let config = worker.agent_node_authority()?;
        let projection = AgentSynthesisProjectionEvidence {
            schema_version: 1,
            source_receipt_sha256: format!("{:x}", Sha256::digest(receipt.encode_to_vec())),
            settlement_fence: Some(fence),
            settlement_writer_token: input.settlement_writer_token,
            run_id: evidence.run_id.clone(),
            metadata_epoch: input.metadata_epoch,
            expected_index: input.expected_index,
            content_body: Some(content),
            done_body: Some(AgentArtifactReference {
                sha256: done.sha256,
                length: done.length,
            }),
            fleet_id: config.fleet_id,
            environment: config.environment,
        };
        let receipt = self
            .control_signer
            .as_ref()
            .ok_or_else(invalid)?
            .lock()
            .await
            .sign_synthesis_projection(projection)?;
        Ok(Response::new(receipt))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn final_body_requires_confirmed_public_content_without_private_fields() {
        let root = tempfile::tempdir().unwrap();
        let mut store = ArtifactStore::open_native(root.path(), "python_disabled").unwrap();
        for body in [
            json!({"type":"agent_output","text":"task"}),
            json!({"type":"content","text":""}),
            json!({"type":"content","text":"  "}),
            json!({"type":"content","text":null}),
            json!({"type":"content","text":"answer","reasoning_content":"private"}),
        ] {
            let source = store.put(&body).unwrap();
            assert!(
                derive_done_body(
                    &mut store,
                    &AgentArtifactReference {
                        sha256: source.sha256,
                        length: source.length
                    }
                )
                .is_err()
            );
        }
        let source = store
            .put(&json!({"type":"content","text":"Verified final answer"}))
            .unwrap();
        let reference = AgentArtifactReference {
            sha256: source.sha256.clone(),
            length: source.length,
        };
        let done = derive_done_body(&mut store, &reference).unwrap();
        assert_eq!(store.get(&done).unwrap(), json!({"type":"done"}));
        assert_eq!(
            store.get(&source).unwrap(),
            json!({"type":"content","text":"Verified final answer"})
        );
        let again = derive_done_body(&mut store, &reference).unwrap();
        assert_eq!(done.sha256, again.sha256);
        assert_eq!(done.length, again.length);
        let mut wrong = reference;
        wrong.length += 1;
        assert!(derive_done_body(&mut store, &wrong).is_err());
    }
}
