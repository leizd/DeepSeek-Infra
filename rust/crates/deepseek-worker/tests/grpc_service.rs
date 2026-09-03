use deepseek_protocol::generated::deepseek::action::v1::{
    AdmitCommandRequest, AdmitStatus, CommandKind, QueryEffectRequest,
    worker_server::Worker as WorkerRpc,
};
use deepseek_protocol::generated::deepseek::common::v1::{ActionFence, EffectState};
use deepseek_worker::{Worker, WorkerRpcService};
use tonic::Request;

fn fence(epoch: u64) -> ActionFence {
    ActionFence {
        action_id: "act-1".to_string(),
        execution_epoch: epoch,
    }
}

async fn admit(
    service: &WorkerRpcService,
    command_fence: ActionFence,
    untrusted_live_epoch: u64,
) -> deepseek_protocol::generated::deepseek::action::v1::AdmitCommandResponse {
    WorkerRpc::admit_command(
        service,
        Request::new(AdmitCommandRequest {
            kind: CommandKind::ExecuteBackup as i32,
            fence: Some(command_fence),
            live_epoch: untrusted_live_epoch,
        }),
    )
    .await
    .unwrap()
    .into_inner()
}

#[tokio::test]
async fn rpc_admission_uses_local_authority_not_request_live_epoch() {
    let mut worker = Worker::new();
    worker.install_authoritative_epoch(&fence(7)).unwrap();
    let service = WorkerRpcService::new(worker);

    let response = admit(&service, fence(7), u64::MAX).await;
    assert_eq!(response.status(), AdmitStatus::Admitted);
    assert_eq!(response.state(), EffectState::Unknown);
    assert!(response.error.is_none());
}

#[tokio::test]
async fn rpc_admission_rejects_self_advanced_and_missing_authority() {
    let mut worker = Worker::new();
    worker.install_authoritative_epoch(&fence(7)).unwrap();
    let service = WorkerRpcService::new(worker);

    let future = admit(&service, fence(8), 8).await;
    assert_eq!(future.status(), AdmitStatus::Rejected);
    assert_eq!(future.error.unwrap().code, "FENCE_MISMATCH");

    let missing = admit(&WorkerRpcService::new(Worker::new()), fence(1), 1).await;
    assert_eq!(missing.status(), AdmitStatus::Rejected);
    assert_eq!(missing.error.unwrap().code, "FENCE_MISMATCH");
}

#[tokio::test]
async fn rpc_rejects_unknown_command_and_reports_unknown_effect() {
    let mut worker = Worker::new();
    worker.install_authoritative_epoch(&fence(7)).unwrap();
    let service = WorkerRpcService::new(worker);

    let invalid = WorkerRpc::admit_command(
        &service,
        Request::new(AdmitCommandRequest {
            kind: i32::MAX,
            fence: Some(fence(7)),
            live_epoch: 7,
        }),
    )
    .await
    .unwrap()
    .into_inner();
    assert_eq!(invalid.status(), AdmitStatus::Rejected);
    assert_eq!(invalid.error.unwrap().code, "EFFECT_UNKNOWN");

    let queried = WorkerRpc::query_effect(
        &service,
        Request::new(QueryEffectRequest {
            fence: Some(fence(7)),
        }),
    )
    .await
    .unwrap()
    .into_inner();
    assert_eq!(queried.fence.as_ref(), Some(&fence(7)));
    assert_eq!(queried.state(), EffectState::Unknown);
    assert_eq!(queried.error.unwrap().code, "EFFECT_UNKNOWN");

    let missing_fence =
        WorkerRpc::query_effect(&service, Request::new(QueryEffectRequest { fence: None }))
            .await
            .unwrap()
            .into_inner();
    assert_eq!(missing_fence.state(), EffectState::Unknown);
    assert_eq!(missing_fence.error.unwrap().code, "EMPTY_ACTION_ID");
}

#[tokio::test]
async fn rpc_never_exposes_unbound_applied_state_as_verified() {
    let mut worker = Worker::new();
    worker.install_authoritative_epoch(&fence(7)).unwrap();
    worker
        .record_effect(&fence(7), EffectState::Applied)
        .unwrap();
    let service = WorkerRpcService::new(worker);

    let queried = WorkerRpc::query_effect(
        &service,
        Request::new(QueryEffectRequest {
            fence: Some(fence(7)),
        }),
    )
    .await
    .unwrap()
    .into_inner();
    assert_eq!(queried.state(), EffectState::Unknown);
    assert_eq!(queried.error.unwrap().code, "PROOF_NOT_AUTHORITATIVE");
    assert!(queried.effect_id.is_empty());
    assert!(queried.receipt_digest.is_empty());
    assert!(queried.commit_digest.is_empty());
    assert!(queried.proof_digest.is_empty());
}
