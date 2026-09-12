//! Real providers and RPC handlers; not a process-kill or production-auth proof.
use super::{PutFault, endpoints, faulted_put_relay, minio_configured, sign_request, store, test_signer};
use std::sync::Arc;

use deepseek_protocol::ActionFence;
use deepseek_protocol::generated::deepseek::action::v1::{
    QueryStorageEffectRequest, StorageConditionType, StorageMutationRequest,
    StorageMutationResponse, StorageMutationStatus, StoragePrecondition,
    worker_server::Worker as WorkerRpc,
};
use deepseek_protocol::generated::deepseek::common::v1::EffectState;
use deepseek_worker::{CallerIdentity, StaticTokenAuthenticator, Worker, WorkerRpcService};
use sha2::{Digest, Sha256};
use tonic::Request;

fn authenticated<T>(message: T) -> Request<T> {
    let mut request = Request::new(message);
    request.metadata_mut().insert(
        "authorization",
        "Bearer rpc-operation-qualification".parse().unwrap(),
    );
    request
}

async fn query(
    service: &WorkerRpcService,
    fence: &ActionFence,
    operation: &str,
) -> StorageMutationResponse {
    WorkerRpc::query_storage_effect(
        service,
        authenticated(QueryStorageEffectRequest {
            fence: Some(fence.clone()),
            operation_id: operation.into(),
        }),
    )
    .await
    .unwrap()
    .into_inner()
}

fn assert_mismatch(response: &StorageMutationResponse) {
    assert_eq!(response.status(), StorageMutationStatus::Rejected);
    assert_eq!(response.state(), EffectState::Unknown);
    assert_eq!(
        response.error.as_ref().unwrap().code,
        "STORAGE_OPERATION_MISMATCH"
    );
    assert!(response.effect_id.is_empty());
    assert!(response.etag.is_empty());
    assert!(response.provider_metadata.is_empty());
}

#[tokio::test]
async fn rpc_created_intent_preserves_operation_identity_on_three_real_providers() {
    if !minio_configured() {
        return;
    }
    for endpoint in endpoints() {
        let directory = tempfile::tempdir().unwrap();
        let (key, config) = test_signer();
        let transport = Arc::new(store(&endpoint));
        let fence = ActionFence {
            action_id: "rpc-operation-provider".into(),
            execution_epoch: 21,
        };
        let signed = sign_request(&key, &fence.action_id, 21, 4, "a", "b");
        let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
        worker.install_signed_epoch(&fence, &signed).unwrap();
        let auth = Arc::new(StaticTokenAuthenticator::new(
            "rpc-operation-qualification",
            CallerIdentity {
                service_name: "qualification".into(),
                role: "controller".into(),
            },
        ));
        let service = WorkerRpcService::new_with_authenticator(worker, auth.clone())
            .with_transport(transport.clone());
        let payload = b"RPC-created durable intent and real provider bytes";
        // Identity is opaque: do not trim or normalize it in storage or responses.
        let operation = " Operation-A ";
        let mut target_identity = String::with_capacity(64);
        for byte in transport.target_identity() {
            use std::fmt::Write as _;
            write!(target_identity, "{byte:02x}").unwrap();
        }
        let request = StorageMutationRequest {
            fence: Some(fence.clone()),
            operation_id: operation.into(),
            mutation_type: "PUT_CHUNK".into(),
            target_identity,
            object_key: "rpc-operation-identity".into(),
            payload_digest: format!("{:x}", Sha256::digest(payload)),
            expected_length: payload.len() as u64,
            payload: payload.to_vec(),
            precondition: Some(StoragePrecondition {
                condition_type: StorageConditionType::CreateOnly as i32,
                expected_etag: String::new(),
            }),
            ..Default::default()
        };
        // The actual mutation handler creates and dispatches the intent. No
        // effect row or remote object is manually inserted for this operation.
        let result = WorkerRpc::execute_storage_mutation(&service, authenticated(request.clone()))
            .await
            .unwrap()
            .into_inner();
        assert_eq!(result.status(), StorageMutationStatus::Confirmed);
        assert_eq!(result.state(), EffectState::Applied);
        assert_eq!(result.fence.as_ref(), Some(&fence));
        assert_eq!(result.operation_id, operation);
        assert!(!result.etag.is_empty());
        assert!(result.error.is_none());
        transport
            .download_verified(
                &request.object_key,
                payload.len() as u64,
                Sha256::digest(payload).into(),
                &mut tokio::io::sink(),
            )
            .await
            .unwrap();
        let confirmed = query(&service, &fence, operation).await;
        assert_eq!(confirmed.status(), StorageMutationStatus::Confirmed);
        assert_eq!(confirmed.operation_id, operation);
        assert_eq!(confirmed.etag, result.etag);

        for substitute in ["Operation-A", " Operation-B "] {
            assert_mismatch(&query(&service, &fence, substitute).await);
            let mut retry = request.clone();
            retry.operation_id = substitute.into();
            assert_mismatch(
                &WorkerRpc::execute_storage_mutation(&service, authenticated(retry))
                    .await
                    .unwrap()
                    .into_inner(),
            );
        }
        drop(service);

        // Reopen the same durable journal, without claiming a process kill.
        let worker = Worker::open_with_authority(config, directory.path()).unwrap();
        let record = worker.query_storage_effect(&fence).unwrap().unwrap();
        assert_eq!(record.operation_id.as_deref(), Some(operation));
        let service = WorkerRpcService::new_with_authenticator(worker, auth)
            .with_transport(transport.clone());
        assert_eq!(query(&service, &fence, operation).await, confirmed);
        assert_mismatch(&query(&service, &fence, "Operation-A").await);
        let mut retry = request.clone();
        retry.operation_id = "Operation-A".into();
        assert_mismatch(
            &WorkerRpc::execute_storage_mutation(&service, authenticated(retry))
                .await
                .unwrap()
                .into_inner(),
        );
        let observed = transport.stat(&request.object_key).await.unwrap().unwrap();
        assert_eq!(observed.etag, result.etag);
        transport
            .download_verified(
                &request.object_key,
                payload.len() as u64,
                Sha256::digest(payload).into(),
                &mut tokio::io::sink(),
            )
            .await
            .unwrap();
    }
}

#[tokio::test]
async fn rpc_unknown_effect_reconciles_only_for_its_persisted_operation() {
    if !minio_configured() {
        return;
    }
    let directory = tempfile::tempdir().unwrap();
    let (key, config) = test_signer();
    let endpoint = endpoints()[0].clone();
    let provider = store(&endpoint);
    let (fault_endpoint, _release, mut relay) =
        faulted_put_relay(&endpoint, PutFault::DropAck).await;
    let transport = Arc::new(store(&fault_endpoint));
    let fence = ActionFence {
        action_id: "rpc-operation-dropped-ack".into(),
        execution_epoch: 22,
    };
    let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    worker
        .install_signed_epoch(
            &fence,
            &sign_request(&key, &fence.action_id, 22, 4, "c", "d"),
        )
        .unwrap();
    let auth = Arc::new(StaticTokenAuthenticator::new(
        "rpc-operation-qualification",
        CallerIdentity {
            service_name: "qualification".into(),
            role: "controller".into(),
        },
    ));
    let service = WorkerRpcService::new_with_authenticator(worker, auth.clone())
        .with_transport(transport.clone());
    let payload = b"real PUT committed but RPC handler did not receive its ACK";
    let request = StorageMutationRequest {
        fence: Some(fence.clone()),
        operation_id: "uncertain-operation".into(),
        mutation_type: "PUT_CHUNK".into(),
        object_key: "rpc-operation-dropped-ack".into(),
        payload_digest: format!("{:x}", Sha256::digest(payload)),
        expected_length: payload.len() as u64,
        payload: payload.to_vec(),
        precondition: Some(StoragePrecondition {
            condition_type: StorageConditionType::CreateOnly as i32,
            expected_etag: String::new(),
        }),
        ..Default::default()
    };
    let unknown = WorkerRpc::execute_storage_mutation(&service, authenticated(request.clone()))
        .await
        .unwrap()
        .into_inner();
    assert_eq!(unknown.status(), StorageMutationStatus::EffectUnknown);
    assert_eq!(unknown.state(), EffectState::Unknown);
    relay.wait_committed().await;
    // Independent byte verification uses the real provider. Worker reconciliation
    // must keep using the original relay origin stored in the placement binding.
    provider
        .download_verified(
            &request.object_key,
            payload.len() as u64,
            Sha256::digest(payload).into(),
            &mut tokio::io::sink(),
        )
        .await
        .unwrap();
    drop(service);
    let worker = Worker::open_with_authority(config, directory.path()).unwrap();
    let record = worker.query_storage_effect(&fence).unwrap().unwrap();
    assert_eq!(
        record.state,
        deepseek_worker::StorageEffectState::EffectUnknown
    );
    assert_eq!(
        record.operation_id.as_deref(),
        Some(request.operation_id.as_str())
    );
    let service = WorkerRpcService::new_with_authenticator(worker, auth).with_transport(transport);

    assert_mismatch(&query(&service, &fence, "substituted-operation").await);
    assert_eq!(relay.observed_gets(), 0);
    let confirmed = query(&service, &fence, &request.operation_id).await;
    assert_eq!(confirmed.status(), StorageMutationStatus::Confirmed);
    assert_eq!(confirmed.state(), EffectState::Applied);
    assert_eq!(confirmed.fence.as_ref(), Some(&fence));
    assert_eq!(confirmed.operation_id, request.operation_id);
    assert_eq!(relay.observed_gets(), 1);
    let metadata: serde_json::Value = serde_json::from_str(&confirmed.provider_metadata).unwrap();
    assert_eq!(metadata["bytesVerified"], true);
    assert_eq!(metadata["sha256"], request.payload_digest);
    assert_mismatch(&query(&service, &fence, "substituted-operation").await);
    assert_eq!(relay.observed_gets(), 1);
}
