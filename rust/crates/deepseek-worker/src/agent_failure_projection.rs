//! Historical failure evidence performs no provider I/O or Go phase mutation.
use crate::WorkerRpcService;
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentFailureProjectionEvidence, AgentFailureProjectionReceipt, ProjectAgentFailureRequest,
    agent_node_worker_server::AgentNodeWorker,
};
use prost::Message;
use sha2::{Digest, Sha256};
use tonic::{Request, Response, Status};

fn invalid() -> Status {
    Status::failed_precondition("AGENT_FAILURE_PROJECTION_INVALID")
}

impl WorkerRpcService {
    pub(crate) async fn project_agent_failure(
        &self,
        request: Request<ProjectAgentFailureRequest>,
    ) -> Result<Response<AgentFailureProjectionReceipt>, Status> {
        let headers = request.metadata().clone();
        let input = request.into_inner();
        let source = input.source.clone().ok_or_else(invalid)?;
        let original_fence = source.fence.as_ref().ok_or_else(invalid)?;
        let fence = input.settlement_fence.clone().ok_or_else(invalid)?;
        if input.schema_version != 1
            || deepseek_protocol::validate_fence(&fence).is_err()
            || !matches!(source.node_id.as_str(), "leader" | "synthesizer")
            || source.expected_receipt_sha256.is_empty()
            || fence.action_id != original_fence.action_id
            || fence.execution_epoch < original_fence.execution_epoch
            || input.settlement_writer_token <= 0
            || input.metadata_epoch == 0
            || input.expected_index < 0
            || input.expected_index == i64::MAX
        {
            return Err(invalid());
        }
        // Authenticated lookup reuses the ORIGINAL operation, action and epoch.
        // An absent or unknown effect cannot be converted into terminal failure.
        let mut query = Request::new(source);
        *query.metadata_mut() = headers;
        let effect = AgentNodeWorker::read_retained_effect(self, query)
            .await?
            .into_inner()
            .effect
            .filter(|effect| effect.state == "CONFIRMED")
            .ok_or_else(invalid)?;
        let receipt = effect.outcome_receipt.ok_or_else(invalid)?;
        let evidence = receipt.evidence.as_ref().ok_or_else(invalid)?;
        if evidence.failure.is_none()
            || !matches!(
                (evidence.phase, evidence.node_id.as_str()),
                (1, "leader") | (3, "synthesizer")
            )
            || evidence.task_output.is_some()
            || !evidence.provider_plan.is_empty()
            || evidence.metadata_epoch != input.metadata_epoch
            || evidence.metadata_index != input.expected_index
        {
            return Err(invalid());
        }
        let worker = self.worker.lock().await;
        let config = worker.agent_node_authority()?;
        let projection = AgentFailureProjectionEvidence {
            schema_version: 1,
            source_receipt_sha256: format!("{:x}", Sha256::digest(receipt.encode_to_vec())),
            settlement_fence: Some(fence),
            settlement_writer_token: input.settlement_writer_token,
            run_id: evidence.run_id.clone(),
            phase: evidence.phase,
            metadata_epoch: input.metadata_epoch,
            expected_index: input.expected_index,
            error_body: evidence.public_event.clone(),
            fleet_id: config.fleet_id,
            environment: config.environment,
        };
        let result = self
            .control_signer
            .as_ref()
            .ok_or_else(invalid)?
            .lock()
            .await
            .sign_failure_projection(projection)?;
        Ok(Response::new(result))
    }
}
