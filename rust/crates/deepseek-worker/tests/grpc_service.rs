use std::sync::Arc;

use deepseek_protocol::generated::deepseek::action::v1::{
    AdmitCommandRequest, AdmitStatus, CommandKind, InstallAuthoritativeEpochRequest,
    QueryEffectRequest, QueryStorageEffectRequest, StorageConditionType, StorageMutationRequest,
    StorageMutationStatus, StoragePrecondition, worker_server::Worker as WorkerRpc,
};
use deepseek_protocol::generated::deepseek::common::v1::{ActionFence, EffectState};
use deepseek_worker::{
    CallerIdentity, StaticTokenAuthenticator, Worker, WorkerAuthorityConfig, WorkerRpcService,
};
use tonic::Request;

fn fence(epoch: u64) -> ActionFence {
    ActionFence {
        action_id: "act-1".to_string(),
        execution_epoch: epoch,
    }
}

#[cfg(feature = "s3")]
#[tokio::test]
async fn rpc_query_cannot_assign_an_operation_to_a_legacy_effect() {
    let directory = tempfile::tempdir().unwrap();
    let (config, canonical, installed) = frozen_authority();
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    worker.install_signed_epoch(&installed, &canonical).unwrap();
    worker
        .reserve_storage_mutation(&installed, "historical", &[1; 32])
        .unwrap();
    let auth = Arc::new(StaticTokenAuthenticator::new(
        "qualification-token",
        CallerIdentity {
            service_name: "test-caller".into(),
            role: "controller".into(),
        },
    ));
    let service = WorkerRpcService::new_with_authenticator(worker, auth);
    let mut request = Request::new(QueryStorageEffectRequest {
        fence: Some(installed),
        operation_id: "invented-operation".into(),
    });
    request.metadata_mut().insert(
        "authorization",
        "Bearer qualification-token".parse().unwrap(),
    );
    let response = WorkerRpc::query_storage_effect(&service, request)
        .await
        .unwrap()
        .into_inner();
    assert_eq!(response.status(), StorageMutationStatus::Rejected);
    assert_eq!(response.state(), EffectState::Unknown);
    assert_eq!(response.error.unwrap().code, "STORAGE_OPERATION_UNBOUND");
    assert!(response.effect_id.is_empty());
}

#[tokio::test]
async fn rpc_query_rejects_empty_operation_before_storage_lookup() {
    let auth = Arc::new(StaticTokenAuthenticator::new(
        "qualification-token",
        CallerIdentity {
            service_name: "test-caller".into(),
            role: "controller".into(),
        },
    ));
    let service = WorkerRpcService::new_with_authenticator(Worker::new(), auth);
    let mut request = Request::new(QueryStorageEffectRequest {
        fence: Some(fence(1)),
        operation_id: "".into(),
    });
    request.metadata_mut().insert(
        "authorization",
        "Bearer qualification-token".parse().unwrap(),
    );
    let response = WorkerRpc::query_storage_effect(&service, request)
        .await
        .unwrap()
        .into_inner();
    assert_eq!(response.error.unwrap().code, "OPERATION_INVALID");
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

fn frozen_authority() -> (WorkerAuthorityConfig, Vec<u8>, ActionFence) {
    let fixture: serde_json::Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v7/control/authority_request_vector.json"
    )))
    .unwrap();
    (
        WorkerAuthorityConfig {
            signer_public_key: fixture["signer_public_key"].as_str().unwrap().to_string(),
            fleet_id: "fleet-a".to_string(),
            environment: "test".to_string(),
            fencing_token: 4,
            now: Some(fixture["now"].as_str().unwrap().to_string()),
        },
        fixture["canonical_request"]
            .as_str()
            .unwrap()
            .as_bytes()
            .to_vec(),
        fence(4),
    )
}

async fn install(
    service: &WorkerRpcService,
    fence: ActionFence,
    canonical_request: Vec<u8>,
) -> deepseek_protocol::generated::deepseek::action::v1::InstallAuthoritativeEpochResponse {
    WorkerRpc::install_authoritative_epoch(
        service,
        Request::new(InstallAuthoritativeEpochRequest {
            fence: Some(fence),
            canonical_request,
        }),
    )
    .await
    .unwrap()
    .into_inner()
}

#[tokio::test]
async fn rpc_installs_epoch_only_from_signed_authority_request() {
    let (config, canonical, installed) = frozen_authority();
    let unconfigured = WorkerRpcService::new(Worker::new());
    let denied = install(&unconfigured, installed.clone(), canonical.clone()).await;
    assert_eq!(denied.status(), AdmitStatus::Rejected);
    assert!(denied.fence.is_none());
    assert_eq!(
        denied.error.unwrap().code,
        "AUTHORITY_REQUEST_SIGNER_MISMATCH"
    );

    let mut worker = Worker::new();
    worker.configure_authority(config).unwrap();
    let service = WorkerRpcService::new(worker);
    let mismatched = install(
        &service,
        ActionFence {
            action_id: "other".to_string(),
            execution_epoch: 4,
        },
        canonical.clone(),
    )
    .await;
    assert_eq!(mismatched.status(), AdmitStatus::Rejected);
    assert_eq!(mismatched.error.unwrap().code, "FENCE_MISMATCH");

    let accepted = install(&service, installed.clone(), canonical.clone()).await;
    assert_eq!(accepted.status(), AdmitStatus::Admitted);
    assert_eq!(accepted.fence.as_ref(), Some(&installed));
    assert!(accepted.error.is_none());

    let admitted = admit(&service, installed.clone(), u64::MAX).await;
    assert_eq!(admitted.status(), AdmitStatus::Admitted);

    let replayed = install(&service, installed.clone(), canonical).await;
    assert_eq!(replayed.status(), AdmitStatus::Rejected);
    assert!(replayed.fence.is_none());
    assert_eq!(replayed.error.unwrap().code, "STALE_EXECUTION_EPOCH");
}

#[tokio::test]
async fn rpc_install_rejects_missing_fence_and_tampered_bytes() {
    let (config, mut canonical, installed) = frozen_authority();
    let mut worker = Worker::new();
    worker.configure_authority(config).unwrap();
    let service = WorkerRpcService::new(worker);

    let missing = WorkerRpc::install_authoritative_epoch(
        &service,
        Request::new(InstallAuthoritativeEpochRequest {
            fence: None,
            canonical_request: canonical.clone(),
        }),
    )
    .await
    .unwrap()
    .into_inner();
    assert_eq!(missing.status(), AdmitStatus::Rejected);
    assert_eq!(missing.error.unwrap().code, "EMPTY_ACTION_ID");

    canonical.push(b'0');
    let tampered = install(&service, installed, canonical).await;
    assert_eq!(tampered.status(), AdmitStatus::Rejected);
    assert_eq!(tampered.error.unwrap().code, "AUTHORITY_REQUEST_INVALID");
}

#[tokio::test]
async fn rpc_storage_mutation_fails_closed_by_default_without_approved_auth() {
    let service = WorkerRpcService::new(Worker::new());
    let response = WorkerRpc::execute_storage_mutation(
        &service,
        Request::new(StorageMutationRequest {
            fence: Some(fence(1)),
            operation_id: "op-1".to_string(),
            mutation_type: "PUT_CHUNK".to_string(),
            payload: vec![1, 2, 3],
            expected_length: 3,
            payload_digest: "039058c6f2c0cb492c533b0a4d14ef77cc0f78abccced5287d84a1a2011cfb81"
                .to_string(),
            precondition: Some(StoragePrecondition {
                condition_type: StorageConditionType::CreateOnly as i32,
                expected_etag: String::new(),
            }),
            ..Default::default()
        }),
    )
    .await
    .unwrap()
    .into_inner();

    assert_eq!(response.status(), StorageMutationStatus::Rejected);
    assert_eq!(
        response.error.unwrap().code,
        "SERVICE_AUTHENTICATION_UNAVAILABLE"
    );
}

#[tokio::test]
async fn rpc_storage_mutation_rejects_missing_or_invalid_bearer_token() {
    let auth = Arc::new(StaticTokenAuthenticator::new(
        "secret-token",
        CallerIdentity {
            service_name: "test-caller".to_string(),
            role: "controller".to_string(),
        },
    ));
    let service = WorkerRpcService::new_with_authenticator(Worker::new(), auth);

    // 1. Missing auth header
    let response = WorkerRpc::execute_storage_mutation(
        &service,
        Request::new(StorageMutationRequest {
            fence: Some(fence(1)),
            operation_id: "op-1".to_string(),
            ..Default::default()
        }),
    )
    .await
    .unwrap()
    .into_inner();
    assert_eq!(response.status(), StorageMutationStatus::Rejected);
    assert_eq!(response.error.unwrap().code, "AUTHENTICATION_MISSING");

    // 2. Wrong token
    let mut req = Request::new(StorageMutationRequest {
        fence: Some(fence(1)),
        operation_id: "op-1".to_string(),
        ..Default::default()
    });
    req.metadata_mut()
        .insert("authorization", "Bearer wrong-token".parse().unwrap());
    let response = WorkerRpc::execute_storage_mutation(&service, req)
        .await
        .unwrap()
        .into_inner();
    assert_eq!(response.status(), StorageMutationStatus::Rejected);
    assert_eq!(response.error.unwrap().code, "AUTHENTICATION_INVALID");
}

#[tokio::test]
async fn rpc_storage_mutation_rejects_mismatched_payload_digest() {
    let auth = Arc::new(StaticTokenAuthenticator::new(
        "secret-token",
        CallerIdentity {
            service_name: "test-caller".to_string(),
            role: "controller".to_string(),
        },
    ));
    let service = WorkerRpcService::new_with_authenticator(Worker::new(), auth);

    let mut req = Request::new(StorageMutationRequest {
        fence: Some(fence(1)),
        operation_id: "op-1".to_string(),
        mutation_type: "PUT_CHUNK".to_string(),
        payload: vec![1, 2, 3],
        expected_length: 3,
        payload_digest: "0000000000000000000000000000000000000000000000000000000000000000"
            .to_string(),
        precondition: Some(StoragePrecondition {
            condition_type: StorageConditionType::CreateOnly as i32,
            expected_etag: String::new(),
        }),
        ..Default::default()
    });
    req.metadata_mut()
        .insert("authorization", "Bearer secret-token".parse().unwrap());

    let response = WorkerRpc::execute_storage_mutation(&service, req)
        .await
        .unwrap()
        .into_inner();
    assert_eq!(response.status(), StorageMutationStatus::Rejected);
    if cfg!(feature = "s3") {
        assert_eq!(response.error.unwrap().code, "DIGEST_MISMATCH");
    } else {
        assert_eq!(response.error.unwrap().code, "STORAGE_FEATURE_DISABLED");
    }
}

#[tokio::test]
async fn rpc_query_storage_effect_fails_closed_without_auth_and_reports_unknown_when_missing() {
    // 1. Unauthenticated query fails closed
    let unauth_service = WorkerRpcService::new(Worker::new());
    let response = WorkerRpc::query_storage_effect(
        &unauth_service,
        Request::new(QueryStorageEffectRequest {
            fence: Some(fence(1)),
            operation_id: "op-1".to_string(),
        }),
    )
    .await
    .unwrap()
    .into_inner();
    assert_eq!(response.status(), StorageMutationStatus::Rejected);
    assert_eq!(
        response.error.unwrap().code,
        "SERVICE_AUTHENTICATION_UNAVAILABLE"
    );

    // 2. Authenticated query against worker without authority is rejected
    let auth = Arc::new(StaticTokenAuthenticator::new(
        "secret-token",
        CallerIdentity {
            service_name: "test-caller".to_string(),
            role: "controller".to_string(),
        },
    ));
    let service = WorkerRpcService::new_with_authenticator(Worker::new(), auth.clone());
    let mut req = Request::new(QueryStorageEffectRequest {
        fence: Some(fence(1)),
        operation_id: "op-1".to_string(),
    });
    req.metadata_mut()
        .insert("authorization", "Bearer secret-token".parse().unwrap());

    let response = WorkerRpc::query_storage_effect(&service, req)
        .await
        .unwrap()
        .into_inner();
    assert_eq!(response.status(), StorageMutationStatus::Rejected);
    if cfg!(feature = "s3") {
        assert_eq!(response.error.unwrap().code, "WORKER_WITHOUT_AUTHORITY");
    } else {
        assert_eq!(response.error.unwrap().code, "STORAGE_FEATURE_DISABLED");
    }

    // 3. Authenticated query against authorized worker with missing record reports EFFECT_UNKNOWN
    let tempdir = tempfile::tempdir().unwrap();
    let (config, _, installed) = frozen_authority();
    let worker = Worker::open_with_authority(config, tempdir.path()).unwrap();
    let auth_service = WorkerRpcService::new_with_authenticator(worker, auth);
    let mut req = Request::new(QueryStorageEffectRequest {
        fence: Some(installed),
        operation_id: "op-1".to_string(),
    });
    req.metadata_mut()
        .insert("authorization", "Bearer secret-token".parse().unwrap());

    let response = WorkerRpc::query_storage_effect(&auth_service, req)
        .await
        .unwrap()
        .into_inner();
    if cfg!(feature = "s3") {
        assert_eq!(response.status(), StorageMutationStatus::EffectUnknown);
        assert_eq!(response.state(), EffectState::Unknown);
        assert_eq!(response.error.unwrap().code, "EFFECT_UNKNOWN");
    } else {
        assert_eq!(response.status(), StorageMutationStatus::Rejected);
        assert_eq!(response.error.unwrap().code, "STORAGE_FEATURE_DISABLED");
    }
}
