//! Public projections are derived from Rust's confirmed journal, never raw caller bytes.
use crate::WorkerRpcService;
use deepseek_policy::agent_run_data::{ArtifactReference, ArtifactStore, DataError};
use deepseek_protocol::generated::deepseek::agent::v1::{
    AgentArtifactReference, AgentPlanProjectionEvidence, AgentPlanProjectionReceipt,
    ProjectAgentPlanRequest, agent_node_worker_server::AgentNodeWorker,
};
use prost::Message;
use serde_json::json;
use sha2::{Digest, Sha256};
use tonic::{Request, Response, Status};

fn invalid() -> Status {
    Status::failed_precondition("AGENT_PLAN_PROJECTION_INVALID")
}

impl WorkerRpcService {
    pub(crate) async fn project_agent_plan(
        &self,
        request: Request<ProjectAgentPlanRequest>,
    ) -> Result<Response<AgentPlanProjectionReceipt>, Status> {
        let headers = request.metadata().clone();
        let input = request.into_inner();
        let source = input.source.clone().ok_or_else(invalid)?;
        let source_fence = source.fence.as_ref().ok_or_else(invalid)?;
        let fence = input.settlement_fence.clone().ok_or_else(invalid)?;
        if input.schema_version != 1
            || deepseek_protocol::validate_fence(&fence).is_err()
            || source.node_id != "leader"
            || fence.action_id != source_fence.action_id
            || fence.execution_epoch < source_fence.execution_epoch
            || input.settlement_writer_token <= 0
            || input.metadata_epoch == 0
            || input.expected_index < 0
            || !matches!(input.next_status.as_str(), "running" | "awaiting_plan")
            || input.normalized_plan.is_empty()
            || input.normalized_plan.len() > 32
            || input
                .normalized_plan
                .iter()
                .any(|node| node.id.is_empty() || node.id.len() > 64 || node.task.len() > 64 * 1024)
        {
            return Err(invalid());
        }
        // Query authenticates the controller and signs only the worker's retained
        // source result. It never spends a grant or starts provider I/O.
        let mut query = Request::new(source);
        *query.metadata_mut() = headers;
        let effect = AgentNodeWorker::query_effect(self, query)
            .await?
            .into_inner()
            .effect
            .filter(|effect| effect.state == "CONFIRMED")
            .ok_or_else(invalid)?;
        let receipt = effect.outcome_receipt.ok_or_else(invalid)?;
        let evidence = receipt.evidence.as_ref().ok_or_else(invalid)?;
        if evidence.phase != 1
            || evidence.metadata_epoch != input.metadata_epoch
            || evidence.metadata_index != input.expected_index
        {
            return Err(invalid());
        }
        let executor = self.agent_plan_executor.as_ref().ok_or_else(invalid)?;
        let root = executor.artifact_root.clone();
        let plan = input
            .normalized_plan
            .iter()
            .map(|node| json!({"id":node.id,"task":node.task,"depends_on":node.depends_on}))
            .collect::<Vec<_>>();
        let raw_ref = evidence.public_event.clone().ok_or_else(invalid)?;
        let next_status = input.next_status.clone();
        let planning_required = input.planning_required;
        let (plan_body, status_body, planning_body) = tokio::task::spawn_blocking(move || {
            let mut store = ArtifactStore::open_native(&root, "python_disabled")?;
            let mut public = store.get(&ArtifactReference {
                sha256: raw_ref.sha256,
                length: raw_ref.length,
            })?;
            if public["type"] != "agent_plan" {
                return Err(DataError::Corrupt);
            }
            public["plan"] = json!(plan);
            let plan_ref = store.put(&public)?;
            let status_ref = store.put(&json!({"type":"run_status","status":next_status}))?;
            let planning_ref = if planning_required {
                Some(store.put(&json!({"type":"run_status","status":"planning"}))?)
            } else {
                None
            };
            Ok((plan_ref, status_ref, planning_ref))
        })
        .await
        .map_err(|_| invalid())?
        .map_err(|_| invalid())?;
        let worker = self.worker.lock().await;
        // This signs derived public metadata, not an execution grant. Historical
        // custody remains queryable across epoch takeover; Go independently
        // checks the successor fence and writer inside its settlement transaction.
        let config = worker.agent_node_authority()?;
        let projection = AgentPlanProjectionEvidence {
            schema_version: 1,
            source_receipt_sha256: format!("{:x}", Sha256::digest(receipt.encode_to_vec())),
            settlement_fence: Some(fence),
            settlement_writer_token: input.settlement_writer_token,
            run_id: evidence.run_id.clone(),
            metadata_epoch: input.metadata_epoch,
            expected_index: input.expected_index,
            normalized_plan: input.normalized_plan,
            next_status: input.next_status,
            plan_body: Some(AgentArtifactReference {
                sha256: plan_body.sha256,
                length: plan_body.length,
            }),
            status_body: Some(AgentArtifactReference {
                sha256: status_body.sha256,
                length: status_body.length,
            }),
            fleet_id: config.fleet_id,
            environment: config.environment,
            planning_body: planning_body.map(|body| AgentArtifactReference {
                sha256: body.sha256,
                length: body.length,
            }),
            planning_required,
        };
        let receipt = self
            .control_signer
            .as_ref()
            .ok_or_else(invalid)?
            .lock()
            .await
            .sign_plan_projection(projection)?;
        Ok(Response::new(receipt))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{CallerIdentity, StaticTokenAuthenticator, Worker};
    use deepseek_protocol::ActionFence;
    use deepseek_protocol::generated::deepseek::agent::v1::{
        AgentRunPlanNode, QueryAgentNodeEffectRequest,
    };
    use std::sync::Arc;

    #[tokio::test]
    async fn projection_requires_authenticated_controller_and_retained_custody() {
        for role in ["controller", "worker"] {
            let service = WorkerRpcService::new_with_authenticator(
                Worker::new(),
                Arc::new(StaticTokenAuthenticator::new(
                    "fixture-bearer",
                    CallerIdentity {
                        service_name: "go-control-plane".into(),
                        role: role.into(),
                    },
                )),
            );
            assert_eq!(
                service
                    .project_agent_plan(Request::new(ProjectAgentPlanRequest::default()))
                    .await
                    .unwrap_err()
                    .code(),
                tonic::Code::FailedPrecondition
            );
            let fence = ActionFence {
                action_id: "projection-action".into(),
                execution_epoch: 1,
            };
            let input = ProjectAgentPlanRequest {
                schema_version: 1,
                source: Some(QueryAgentNodeEffectRequest {
                    expected_receipt_sha256: String::new(),
                    operation_id: format!("agent-node-{}", "a".repeat(64)),
                    fence: Some(fence.clone()),
                    run_id: "run".into(),
                    node_id: "leader".into(),
                }),
                settlement_fence: Some(fence),
                settlement_writer_token: 1,
                metadata_epoch: 1,
                expected_index: 2,
                normalized_plan: vec![AgentRunPlanNode {
                    id: "coder".into(),
                    task: "write".into(),
                    depends_on: vec![],
                }],
                next_status: "awaiting_plan".into(),
                planning_required: false,
            };
            assert_eq!(
                service
                    .project_agent_plan(Request::new(input.clone()))
                    .await
                    .unwrap_err()
                    .code(),
                tonic::Code::Unauthenticated
            );
            let mut request = Request::new(input);
            request
                .metadata_mut()
                .insert("authorization", "Bearer fixture-bearer".parse().unwrap());
            assert_eq!(
                service
                    .project_agent_plan(request)
                    .await
                    .unwrap_err()
                    .code(),
                if role == "worker" {
                    tonic::Code::PermissionDenied
                } else {
                    tonic::Code::FailedPrecondition
                }
            );
        }
    }
}
