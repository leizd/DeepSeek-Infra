//! Real MinIO tests for durable worker mutations, stale tokens, and transport faults.
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use bytes::Bytes;
use deepseek_protocol::ActionFence;
use deepseek_protocol::generated::deepseek::action::v1::{
    StorageConditionType, StorageMutationRequest,
};
use deepseek_storage::s3::{ConditionalWrite, S3Config, S3Credentials, S3Transport};
use deepseek_worker::{StorageEffectState, Worker, WorkerAuthorityConfig, WorkerStorageError};
use ed25519_dalek::{Signer as _, SigningKey};
use serde_json::Value;
use sha2::{Digest, Sha256};

#[path = "authorized_storage_provider/rpc_operations.rs"]
mod rpc_operations;

fn minio_configured() -> bool {
    std::env::var("DEEPSEEK_NATIVE_S3_ENDPOINTS")
        .ok()
        .is_some_and(|value| !value.trim().is_empty())
}

fn endpoints() -> Vec<String> {
    let endpoints: Vec<_> = std::env::var("DEEPSEEK_NATIVE_S3_ENDPOINTS")
        .expect("run scripts/run_native_s3_e2e.py with real MinIO")
        .split(',')
        .map(str::to_owned)
        .collect();
    assert_eq!(endpoints.len(), 3);
    assert!(
        endpoints[0] != endpoints[1]
            && endpoints[1] != endpoints[2]
            && endpoints[0] != endpoints[2]
    );
    endpoints
}

fn store(endpoint: &str) -> S3Transport {
    store_in_bucket(
        endpoint,
        std::env::var("DEEPSEEK_NATIVE_S3_BUCKET").unwrap(),
    )
}

fn store_in_bucket(endpoint: &str, bucket: String) -> S3Transport {
    store_at(endpoint, bucket, "worker-authorized-e2e")
}

fn store_at(endpoint: &str, bucket: String, prefix: &str) -> S3Transport {
    S3Transport::new(
        S3Config {
            endpoint: endpoint.into(),
            bucket,
            prefix: prefix.into(),
            region: "us-east-1".into(),
            allow_http_loopback: true,
        },
        S3Credentials::new(
            std::env::var("AWS_ACCESS_KEY_ID").unwrap(),
            std::env::var("AWS_SECRET_ACCESS_KEY").unwrap(),
            None,
        )
        .unwrap(),
    )
    .unwrap()
}

fn canonical(value: &Value) -> Vec<u8> {
    fn sorted(value: &Value) -> Value {
        match value {
            Value::Object(map) => {
                let ordered: std::collections::BTreeMap<_, _> =
                    map.iter().map(|(k, v)| (k.clone(), sorted(v))).collect();
                serde_json::to_value(ordered).unwrap()
            }
            Value::Array(items) => Value::Array(items.iter().map(sorted).collect()),
            other => other.clone(),
        }
    }
    serde_json::to_vec(&sorted(value)).unwrap()
}

fn sign_request(
    key: &SigningKey,
    action: &str,
    epoch: u64,
    token: i64,
    request: &str,
    nonce: &str,
) -> Vec<u8> {
    let mut value: Value = serde_json::json!({
        "schema": "control-authority-request-v1",
        "schemaVersion": 1,
        "domain": "action",
        "operation": "install-epoch",
        "actionId": action,
        "executionEpoch": epoch,
        "fencingToken": token,
        "revision": 1,
        "requestId": request.repeat(64),
        "nonce": nonce.repeat(64),
        "issuedAt": "2026-09-04T00:00:30Z",
        "expiresAt": "2026-09-04T00:05:30Z",
        "runtime": "go",
        "mode": "shadow",
        "fleetId": "fleet-a",
        "environment": "test",
        "role": "control-plane",
        "payload": {},
        "payloadDigest": "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
        "signatureAlgorithm": "Ed25519",
        "signerKeyId": format!(
            "ctrl-signer-{}",
            &format!("{:x}", Sha256::digest(key.verifying_key().as_bytes()))[..16]
        )
    });
    value["digest"] = format!("sha256:{:x}", Sha256::digest(canonical(&value))).into();
    let mut message = b"deepseek-infra:control-authority-request-v1\0".to_vec();
    message.extend(canonical(&value));
    value["signature"] = URL_SAFE_NO_PAD.encode(key.sign(&message).to_bytes()).into();
    canonical(&value)
}

fn sign_grant(key: &SigningKey, request: &StorageMutationRequest) -> Vec<u8> {
    let fence = request.fence.as_ref().unwrap();
    let pre = request.precondition.as_ref().unwrap();
    let condition = match StorageConditionType::try_from(pre.condition_type) {
        Ok(StorageConditionType::CreateOnly) => "CREATE_ONLY",
        Ok(StorageConditionType::IfMatch) => "IF_MATCH",
        _ => "",
    };
    let request_id = format!(
        "{:x}",
        Sha256::digest(format!("rid:{}", request.operation_id).as_bytes())
    );
    let nonce = format!(
        "{:x}",
        Sha256::digest(format!("n:{}", request.operation_id).as_bytes())
    );
    let payload = serde_json::json!({
        "bucket": request.bucket,
        "claimRevision": 1,
        "conditionType": condition,
        "expectedEtag": pre.expected_etag,
        "expectedLength": request.expected_length,
        "mutationType": request.mutation_type,
        "objectDigest": request.payload_digest,
        "objectKey": request.object_key,
        "prefix": request.prefix,
        "provider": request.provider,
        "targetIdentity": request.target_identity,
    });
    let payload_digest = format!("sha256:{:x}", Sha256::digest(canonical(&payload)));
    let mut value = serde_json::json!({
        "schema": "control-storage-operation-grant-v1",
        "schemaVersion": 1,
        "domain": "action",
        "operation": "execute-storage-put",
        "actionId": fence.action_id,
        "executionEpoch": fence.execution_epoch,
        "fencingToken": 4,
        "revision": 1,
        "requestId": request_id,
        "nonce": nonce,
        "operationId": request.operation_id,
        "issuedAt": "2026-09-04T00:00:30Z",
        "expiresAt": "2026-09-04T00:05:30Z",
        "runtime": "go",
        "mode": "shadow",
        "fleetId": "fleet-a",
        "environment": "test",
        "role": "control-plane",
        "payload": payload,
        "payloadDigest": payload_digest,
        "signatureAlgorithm": "Ed25519",
        "signerKeyId": format!(
            "ctrl-signer-{}",
            &format!("{:x}", Sha256::digest(key.verifying_key().as_bytes()))[..16]
        )
    });
    value["digest"] = format!("sha256:{:x}", Sha256::digest(canonical(&value))).into();
    let mut message = b"deepseek-infra:control-storage-operation-grant-v1\0".to_vec();
    message.extend(canonical(&value));
    value["signature"] = URL_SAFE_NO_PAD.encode(key.sign(&message).to_bytes()).into();
    canonical(&value)
}

fn test_signer() -> (SigningKey, WorkerAuthorityConfig) {
    let seed = [42u8; 32];
    let key = SigningKey::from_bytes(&seed);
    let config = WorkerAuthorityConfig {
        signer_public_key: URL_SAFE_NO_PAD.encode(key.verifying_key().as_bytes()),
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
        fencing_token: 4,
        now: Some("2026-09-04T00:00:30Z".into()),
    };
    (key, config)
}

#[tokio::test]
async fn authorized_storage_put_reaches_real_minio_and_is_durable() {
    if !minio_configured() {
        return;
    }
    let directory = tempfile::tempdir().unwrap();
    let (key, config) = test_signer();
    let transport = store(&endpoints()[0]);
    let action_id = "real-e2e-action";
    let fence = ActionFence {
        action_id: action_id.into(),
        execution_epoch: 10,
    };
    let request_id_char = "a";
    let nonce_char = "b";
    let request = sign_request(&key, action_id, 10, 4, request_id_char, nonce_char);
    let payload = Bytes::from_static(b"real-authorized-minio-payload-content-test-12345");
    let digest = Sha256::digest(&payload).into();
    let digest_hex = format!("{:x}", Sha256::digest(&payload));
    let target_key = "real-chunk-key-1";

    {
        let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
        worker.install_signed_epoch(&fence, &request).unwrap();

        // Perform authorized storage put to real MinIO
        let observation = worker
            .execute_storage_put(
                &transport,
                target_key,
                payload.clone(),
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await
            .unwrap();

        assert_eq!(observation.length, payload.len() as u64);
        assert!(!observation.etag.is_empty());

        // Verify object exists in MinIO with all 6 bound metadata attributes
        let stat = transport.stat(target_key).await.unwrap().unwrap();
        assert_eq!(stat.claimed_action_id.as_deref(), Some(action_id));
        assert_eq!(stat.claimed_execution_epoch.as_deref(), Some("10"));
        assert_eq!(stat.claimed_fencing_token.as_deref(), Some("4"));
        assert_eq!(
            stat.claimed_request_id.as_deref(),
            Some(request_id_char.repeat(64).as_str())
        );
        assert_eq!(
            stat.claimed_nonce.as_deref(),
            Some(nonce_char.repeat(64).as_str())
        );
        assert_eq!(stat.claimed_sha256.as_deref(), Some(digest_hex.as_str()));
        assert_eq!(stat.etag, observation.etag);

        // Verify effect journal record
        let effect = worker.query_storage_effect(&fence).unwrap().unwrap();
        assert_eq!(effect.state, StorageEffectState::Committed);
        assert_eq!(effect.etag.as_deref(), Some(observation.etag.as_str()));
        let metadata: Value =
            serde_json::from_str(effect.provider_metadata.as_deref().unwrap()).unwrap();
        assert_eq!(metadata["etag"], observation.etag);
        assert_eq!(metadata["size"], payload.len());
        assert!(effect.binding.is_some());

        // Replay attempt on same worker is rejected
        assert_eq!(
            worker
                .execute_storage_put(
                    &transport,
                    target_key,
                    payload.clone(),
                    digest,
                    &fence,
                    ConditionalWrite::Create,
                )
                .await,
            Err(WorkerStorageError::ReplayRejected)
        );
    }

    // Reopen a worker handle over the same SQLite directory (not process kill).
    let mut restarted = Worker::open_with_authority(config, directory.path()).unwrap();

    // Replay attempt on restarted worker is rejected from durable journal
    assert_eq!(
        restarted
            .execute_storage_put(
                &transport,
                target_key,
                payload,
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::ReplayRejected)
    );

    // Verified effect persists across restart
    let effect = restarted.query_storage_effect(&fence).unwrap().unwrap();
    assert_eq!(effect.state, StorageEffectState::Committed);
}

#[tokio::test]
async fn takeover_fencing_token_rejects_storage_put_on_real_minio() {
    if !minio_configured() {
        return;
    }
    let directory = tempfile::tempdir().unwrap();
    let (key, config) = test_signer();
    let transport = store(&endpoints()[0]);
    let action_id = "real-takeover-action";
    let fence = ActionFence {
        action_id: action_id.into(),
        execution_epoch: 10,
    };
    let request = sign_request(&key, action_id, 10, 4, "c", "d");
    let mut first_worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    first_worker.install_signed_epoch(&fence, &request).unwrap();

    // Successor worker takes over with fencing token 5
    let successor_config = WorkerAuthorityConfig {
        fencing_token: 5,
        ..config
    };
    let _successor = Worker::open_with_authority(successor_config, directory.path()).unwrap();

    let payload = Bytes::from_static(b"takeover-blocked-payload");
    let digest = Sha256::digest(&payload).into();
    let target_key = "takeover-blocked-key";

    // First worker storage mutation is rejected due to stale fencing token
    assert_eq!(
        first_worker
            .execute_storage_put(
                &transport,
                target_key,
                payload,
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::StaleFencingToken)
    );

    // Verify object was NOT created in real MinIO
    assert!(transport.stat(target_key).await.unwrap().is_none());
}

#[tokio::test]
async fn confirmed_effect_is_not_reused_for_a_different_provider_target() {
    if !minio_configured() {
        return;
    }
    let directory = tempfile::tempdir().unwrap();
    let (key, config) = test_signer();
    let endpoints = endpoints();
    let original = store(&endpoints[0]);
    let unrelated = store(&endpoints[1]);
    let fence = ActionFence {
        action_id: "provider-bound-confirmation".into(),
        execution_epoch: 17,
    };
    let signed = sign_request(&key, &fence.action_id, 17, 4, "6", "7");
    let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    worker.install_signed_epoch(&fence, &signed).unwrap();
    let payload = Bytes::from_static(b"only the first provider received these bytes");
    let digest = Sha256::digest(&payload).into();
    let target_key = "provider-bound-confirmation";
    worker
        .execute_storage_put(
            &original,
            target_key,
            payload.clone(),
            digest,
            &fence,
            ConditionalWrite::Create,
        )
        .await
        .unwrap();
    assert!(unrelated.stat(target_key).await.unwrap().is_none());
    drop(worker);
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    let bucket = std::env::var("DEEPSEEK_NATIVE_S3_BUCKET").unwrap();
    for other_target in [
        store_in_bucket(&endpoints[0], format!("{bucket}-different")),
        store_at(&endpoints[0], bucket, "different-prefix"),
    ] {
        assert_eq!(
            worker
                .reconcile_storage_mutation(&other_target, &fence)
                .await,
            Err(WorkerStorageError::TargetMismatch)
        );
    }
    assert_eq!(
        worker.reconcile_storage_mutation(&unrelated, &fence).await,
        Err(WorkerStorageError::TargetMismatch),
        "terminal journal result belongs to another provider"
    );
    assert_eq!(
        worker
            .reconcile_storage_mutation(&original, &fence)
            .await
            .unwrap()
            .state,
        StorageEffectState::Confirmed
    );
}

#[tokio::test]
async fn dropped_ack_unknown_effect_reconciles_against_real_minio() {
    if !minio_configured() {
        return;
    }
    let directory = tempfile::tempdir().unwrap();
    let (key, config) = test_signer();
    let endpoint = endpoints()[0].clone();
    let transport = store(&endpoint);
    let action_id = "real-reconcile-action";
    let fence = ActionFence {
        action_id: action_id.into(),
        execution_epoch: 11,
    };
    let request_id_char = "e";
    let nonce_char = "f";
    let request = sign_request(&key, action_id, 11, 4, request_id_char, nonce_char);
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    worker.install_signed_epoch(&fence, &request).unwrap();

    let payload = Bytes::from_static(b"reconcile-real-ack-loss-payload");
    let digest = Sha256::digest(&payload).into();
    let target_key = "reconcile-target-key";

    // 1. Execute normally; the relay drops only real MinIO's successful ACK.
    // No manual journal state is used as provider evidence.
    let (fault_endpoint, _release, mut relay) =
        faulted_put_relay(&endpoint, PutFault::DropAck).await;
    let fault_transport = store(&fault_endpoint);
    let result = worker
        .execute_storage_put(
            &fault_transport,
            target_key,
            payload.clone(),
            digest,
            &fence,
            ConditionalWrite::Create,
        )
        .await;
    assert_eq!(
        result,
        Err(WorkerStorageError::Transport(
            deepseek_storage::s3::S3Error::EffectUnknown
        ))
    );
    relay.wait_committed().await;
    let observation = transport.stat(target_key).await.unwrap().unwrap();
    assert_eq!(
        worker.query_storage_effect(&fence).unwrap().unwrap().state,
        StorageEffectState::EffectUnknown
    );

    // 4. Blind retry must be blocked
    assert_eq!(
        worker
            .execute_storage_put(
                &transport,
                target_key,
                payload.clone(),
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::UnknownEffectRetryBlocked)
    );

    // 5. Worker reconciles the mutation against real MinIO
    assert_eq!(relay.observed_gets(), 0);
    let reconciled = worker
        .reconcile_storage_mutation(&fault_transport, &fence)
        .await
        .unwrap();

    assert_eq!(reconciled.state, StorageEffectState::Committed);
    assert_eq!(reconciled.etag.as_deref(), Some(observation.etag.as_str()));
    assert_eq!(relay.observed_gets(), 1);
    let metadata: Value =
        serde_json::from_str(reconciled.provider_metadata.as_deref().unwrap()).unwrap();
    assert_eq!(metadata["bytesVerified"], true);
    assert_eq!(
        metadata["sha256"],
        format!("{:x}", Sha256::digest(&payload))
    );

    // 6. Once reconciled to COMMITTED, retry is rejected as ReplayRejected
    assert_eq!(
        worker
            .execute_storage_put(
                &transport,
                target_key,
                payload,
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::ReplayRejected)
    );
}

#[tokio::test]
async fn temporarily_absent_unknown_effect_stays_unknown_before_late_provider_write() {
    if !minio_configured() {
        return;
    }
    late_provider_write_keeps_negative_observation_unknown(false).await;
}

#[tokio::test]
async fn mismatched_old_object_stays_unknown_before_late_provider_overwrite() {
    if !minio_configured() {
        return;
    }
    late_provider_write_keeps_negative_observation_unknown(true).await;
}

#[tokio::test]
async fn cancelled_put_cannot_be_redispatched_before_original_minio_commit() {
    if !minio_configured() {
        return;
    }
    let directory = tempfile::tempdir().unwrap();
    let (key, config) = test_signer();
    let endpoint = endpoints()[0].clone();
    let direct = store(&endpoint);
    let (origin, release, mut relay) =
        faulted_put_relay(&endpoint, PutFault::WaitForCancellation).await;
    let transport = store(&origin);
    let fence = ActionFence {
        action_id: "cancelled-native-put".into(),
        execution_epoch: 19,
    };
    let signed = sign_request(&key, &fence.action_id, 19, 4, "8", "9");
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    worker.install_signed_epoch(&fence, &signed).unwrap();
    let payload = Bytes::from_static(b"original cancelled task may still reach MinIO");
    let digest = Sha256::digest(&payload).into();
    let target_key = "cancelled-before-original-commit";
    {
        let pending = worker.execute_storage_put(
            &transport,
            target_key,
            payload.clone(),
            digest,
            &fence,
            ConditionalWrite::Create,
        );
        tokio::pin!(pending);
        tokio::select! {
            result = &mut pending => panic!("PUT completed before cancellation: {result:?}"),
            _ = relay.wait_captured() => {}
        }
        // Drop the actual execution future after its complete signed PUT was
        // captured, without manufacturing an error response or journal row.
    }
    assert_eq!(
        worker.query_storage_effect(&fence).unwrap().unwrap().state,
        StorageEffectState::Dispatching
    );
    assert!(direct.stat(target_key).await.unwrap().is_none());
    let retry = worker
        .execute_storage_put(
            &transport,
            target_key,
            payload.clone(),
            digest,
            &fence,
            ConditionalWrite::Create,
        )
        .await;

    // Even on RED, deliver the first request and independently verify its real
    // provider ACK and bytes before checking that a replacement was forbidden.
    release.send(()).unwrap();
    relay.wait_original_commit().await;
    direct
        .download_verified(
            target_key,
            payload.len() as u64,
            digest,
            &mut tokio::io::sink(),
        )
        .await
        .unwrap();
    assert_eq!(retry, Err(WorkerStorageError::UnknownEffectRetryBlocked));
    assert_eq!(
        relay
            .unexpected_writes
            .load(std::sync::atomic::Ordering::SeqCst),
        0
    );
    // Recovery must work on the same live handle, without requiring a restart.
    let reconciled = worker
        .reconcile_storage_mutation(&transport, &fence)
        .await
        .unwrap();
    assert_eq!(reconciled.state, StorageEffectState::Confirmed);
    assert_eq!(relay.observed_gets(), 1);
}

async fn late_provider_write_keeps_negative_observation_unknown(preexisting: bool) {
    let directory = tempfile::tempdir().unwrap();
    let (key, config) = test_signer();
    let endpoint = endpoints()[0].clone();
    let transport = store(&endpoint);
    let fence = ActionFence {
        action_id: format!("real-delayed-reconcile-action-{preexisting}"),
        execution_epoch: 12,
    };
    let request = sign_request(&key, &fence.action_id, 12, 4, "0", "1");
    let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    worker.install_signed_epoch(&fence, &request).unwrap();
    let payload =
        Bytes::from_static(b"provider will receive these bytes after HEAD reports absent");
    let digest = Sha256::digest(&payload).into();
    let target_key = if preexisting {
        "delayed-provider-existing-key"
    } else {
        "delayed-provider-absent-key"
    };
    let condition = if preexisting {
        let seed_fence = ActionFence {
            action_id: "seed-existing-object".into(),
            execution_epoch: 1,
        };
        let seed_request = sign_request(&key, &seed_fence.action_id, 1, 4, "2", "3");
        worker
            .install_signed_epoch(&seed_fence, &seed_request)
            .unwrap();
        let seed_payload =
            Bytes::from_static(b"old provider object awaiting conditional replacement");
        let seed_digest = Sha256::digest(&seed_payload).into();
        let seed = worker
            .execute_storage_put(
                &transport,
                target_key,
                seed_payload,
                seed_digest,
                &seed_fence,
                ConditionalWrite::Create,
            )
            .await
            .unwrap();
        ConditionalWrite::Match(seed.etag)
    } else {
        ConditionalWrite::Create
    };

    // Capture the real signed PUT, disconnect the worker, and delay the original
    // request's delivery to real MinIO. No manual effect-journal writes.
    let (fault_endpoint, release, mut relay) =
        faulted_put_relay(&endpoint, PutFault::DelayDelivery).await;
    let fault_transport = store(&fault_endpoint);
    let result = worker
        .execute_storage_put(
            &fault_transport,
            target_key,
            payload.clone(),
            digest,
            &fence,
            condition,
        )
        .await;
    assert_eq!(
        result,
        Err(WorkerStorageError::Transport(
            deepseek_storage::s3::S3Error::EffectUnknown
        ))
    );
    assert_eq!(
        worker.query_storage_effect(&fence).unwrap().unwrap().state,
        StorageEffectState::EffectUnknown
    );
    assert_eq!(
        transport.stat(target_key).await.unwrap().is_some(),
        preexisting
    );

    // Reopening the durable worker does not make an absent remote object a
    // definitive NOT_APPLIED proof. This is not a process-kill/takeover test.
    drop(worker);
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    let reconciled = worker
        .reconcile_storage_mutation(&fault_transport, &fence)
        .await
        .unwrap();

    // Complete the captured request even on the old failing implementation, so
    // the RED assertion is backed by an actual late committed provider write.
    release.send(()).unwrap();
    relay.wait_committed().await;
    transport
        .download_verified(
            target_key,
            payload.len() as u64,
            digest,
            &mut tokio::io::sink(),
        )
        .await
        .unwrap();
    assert_eq!(
        reconciled.state,
        StorageEffectState::EffectUnknown,
        "a negative HEAD preceded a real successful PUT; absence was not final"
    );
    assert_eq!(
        worker
            .execute_storage_put(
                &transport,
                target_key,
                payload,
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::UnknownEffectRetryBlocked)
    );
}

struct FaultRelay {
    first_put: Option<tokio::task::JoinHandle<()>>,
    reads: tokio::task::JoinHandle<()>,
    unexpected_writes: std::sync::Arc<std::sync::atomic::AtomicUsize>,
    gets: std::sync::Arc<std::sync::atomic::AtomicUsize>,
    captured: Option<tokio::sync::oneshot::Receiver<()>>,
}

impl FaultRelay {
    async fn wait_captured(&mut self) {
        tokio::time::timeout(
            std::time::Duration::from_secs(10),
            self.captured.take().unwrap(),
        )
        .await
        .unwrap()
        .unwrap();
    }

    async fn wait_original_commit(&mut self) {
        self.first_put.take().unwrap().await.unwrap();
    }

    fn observed_gets(&self) -> usize {
        self.gets.load(std::sync::atomic::Ordering::SeqCst)
    }

    async fn wait_committed(&mut self) {
        self.wait_original_commit().await;
        assert_eq!(
            self.unexpected_writes
                .load(std::sync::atomic::Ordering::SeqCst),
            0,
            "hidden PUT retry"
        );
    }
}

impl Drop for FaultRelay {
    fn drop(&mut self) {
        if let Some(first_put) = &self.first_put {
            first_put.abort();
        }
        self.reads.abort();
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum PutFault {
    DropAck,
    DelayDelivery,
    WaitForCancellation,
}

async fn faulted_put_relay(
    endpoint: &str,
    fault: PutFault,
) -> (String, tokio::sync::oneshot::Sender<()>, FaultRelay) {
    use std::time::Duration;
    use tokio::{
        io::{AsyncReadExt, AsyncWriteExt},
        net::{TcpListener, TcpStream},
        time::timeout,
    };
    let upstream_address = endpoint.strip_prefix("http://").unwrap().to_owned();
    let read_address = upstream_address.clone();
    let listener = std::sync::Arc::new(TcpListener::bind("127.0.0.1:0").await.unwrap());
    let write_listener = listener.clone();
    let relay_endpoint = format!("http://{}", listener.local_addr().unwrap());
    let (release, released) = tokio::sync::oneshot::channel();
    let (accepted, read_ready) = tokio::sync::oneshot::channel();
    let (captured, capture_ready) = tokio::sync::oneshot::channel();
    let relay = tokio::spawn(async move {
        let (mut downstream, _) = timeout(Duration::from_secs(10), write_listener.accept())
            .await
            .unwrap()
            .unwrap();
        accepted.send(()).unwrap();
        let mut request = Vec::new();
        let mut expected = None;
        timeout(Duration::from_secs(10), async {
            loop {
                let mut part = [0; 4096];
                let count = downstream.read(&mut part).await.unwrap();
                assert!(count > 0, "worker closed before sending the signed PUT");
                request.extend_from_slice(&part[..count]);
                assert!(request.len() <= 8 * 1024 * 1024 + 65536);
                if expected.is_none() {
                    if let Some(end) = request.windows(4).position(|window| window == b"\r\n\r\n") {
                        let headers = std::str::from_utf8(&request[..end]).unwrap();
                        assert!(headers.starts_with("PUT "));
                        let length: usize = headers
                            .lines()
                            .find_map(|line| {
                                let (name, value) = line.split_once(':')?;
                                name.eq_ignore_ascii_case("content-length")
                                    .then(|| value.trim().parse().unwrap())
                            })
                            .expect("Bytes PUT must carry Content-Length");
                        expected = Some(end + 4 + length);
                    }
                }
                if expected == Some(request.len()) {
                    break;
                }
            }
        })
        .await
        .unwrap();
        captured.send(()).unwrap();
        // The provider has not seen any bytes. The worker cannot infer that
        // from its transport error; the relay may still finish the original PUT.
        let mut downstream = Some(downstream);
        if fault == PutFault::DelayDelivery {
            let mut connection = downstream.take().unwrap();
            connection.shutdown().await.unwrap();
            drop(connection);
        }
        if fault != PutFault::DropAck {
            timeout(Duration::from_secs(20), released)
                .await
                .unwrap()
                .unwrap();
        }
        let mut upstream = TcpStream::connect(upstream_address).await.unwrap();
        upstream.write_all(&request).await.unwrap();
        let mut response = Vec::new();
        timeout(Duration::from_secs(10), async {
            loop {
                let mut part = [0; 1024];
                let count = upstream.read(&mut part).await.unwrap();
                assert!(count > 0);
                response.extend_from_slice(&part[..count]);
                assert!(response.len() <= 65536);
                if response.windows(4).any(|window| window == b"\r\n\r\n") {
                    break;
                }
            }
        })
        .await
        .unwrap();
        assert!(
            response.starts_with(b"HTTP/1.1 200 "),
            "real MinIO must ACK the original signed PUT"
        );
        if let Some(mut connection) = downstream {
            if fault == PutFault::DropAck {
                connection.shutdown().await.unwrap();
            }
            drop(connection);
        }
    });
    let unexpected_writes = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let writes = unexpected_writes.clone();
    let gets = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let read_gets = gets.clone();
    // Keep the exact configured origin alive for HEAD/GET after the fault.
    // A durable target binding must never be bypassed by switching to a direct alias.
    let reads = tokio::spawn(async move {
        read_ready.await.unwrap();
        loop {
            let (mut client, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            timeout(Duration::from_secs(10), async {
                loop {
                    let mut part = [0; 4096];
                    let count = client.read(&mut part).await.unwrap();
                    assert!(count > 0);
                    request.extend_from_slice(&part[..count]);
                    assert!(request.len() <= 65536);
                    if request.windows(4).any(|window| window == b"\r\n\r\n") {
                        break;
                    }
                }
            })
            .await
            .unwrap();
            if !request.starts_with(b"GET ") && !request.starts_with(b"HEAD ") {
                writes.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                client.shutdown().await.unwrap();
                continue;
            }
            if request.starts_with(b"GET ") {
                read_gets.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
            }
            let mut provider = TcpStream::connect(&read_address).await.unwrap();
            provider.write_all(&request).await.unwrap();
            // All provider responses are forwarded unchanged; no synthetic S3.
            let _ = timeout(
                Duration::from_secs(10),
                tokio::io::copy_bidirectional(&mut client, &mut provider),
            )
            .await;
        }
    });
    (
        relay_endpoint,
        release,
        FaultRelay {
            first_put: Some(relay),
            reads,
            unexpected_writes,
            gets,
            captured: Some(capture_ready),
        },
    )
}
