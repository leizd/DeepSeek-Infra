//! Real MinIO tests for authorized worker storage mutation, crash recovery, and reconciliation.
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use bytes::Bytes;
use deepseek_protocol::ActionFence;
use deepseek_storage::s3::{ConditionalWrite, S3Config, S3Credentials, S3Transport};
use deepseek_worker::{StorageEffectState, Worker, WorkerAuthorityConfig, WorkerStorageError};
use ed25519_dalek::{Signer as _, SigningKey};
use serde_json::Value;
use sha2::{Digest, Sha256};

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
    S3Transport::new(
        S3Config {
            endpoint: endpoint.into(),
            bucket,
            prefix: "worker-authorized-e2e".into(),
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

    // Process restart: open fresh worker instance over same SQLite directory
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
async fn dropped_ack_unknown_effect_reconciles_against_real_minio() {
    let directory = tempfile::tempdir().unwrap();
    let (key, config) = test_signer();
    let transport = store(&endpoints()[0]);
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

    let payload = Bytes::from_static(b"reconcile-simulated-ack-loss-payload");
    let digest = Sha256::digest(&payload).into();
    let target_key = "reconcile-target-key";

    // 1. Worker reserves mutation in durable SQLite
    let proof = worker
        .reserve_storage_mutation(&fence, target_key, &digest)
        .unwrap();

    // 2. Put object to MinIO via transport directly with the proof
    let observation = transport
        .put_chunk(
            target_key,
            payload.clone(),
            digest,
            &proof,
            ConditionalWrite::Create,
        )
        .await
        .unwrap();

    // 3. Simulate dropped ACK / network partition before commit: record EFFECT_UNKNOWN
    worker
        .record_storage_mutation_effect_unknown(&fence)
        .unwrap();

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
    let reconciled = worker
        .reconcile_storage_mutation(&transport, &fence)
        .await
        .unwrap();

    assert_eq!(reconciled.state, StorageEffectState::Committed);
    assert_eq!(reconciled.etag.as_deref(), Some(observation.etag.as_str()));

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
async fn unwritten_unknown_effect_reconciles_to_rejected() {
    let directory = tempfile::tempdir().unwrap();
    let (key, config) = test_signer();
    let transport = store(&endpoints()[0]);
    let action_id = "real-unwritten-reconcile-action";
    let fence = ActionFence {
        action_id: action_id.into(),
        execution_epoch: 12,
    };
    let request = sign_request(&key, action_id, 12, 4, "0", "1");
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    worker.install_signed_epoch(&fence, &request).unwrap();

    let payload = Bytes::from_static(b"never-written-payload");
    let digest = Sha256::digest(&payload).into();
    let target_key = "never-written-target-key";

    // 1. Reserve mutation
    worker
        .reserve_storage_mutation(&fence, target_key, &digest)
        .unwrap();

    // 2. Simulate failed request before network reach: recorded as EFFECT_UNKNOWN
    worker
        .record_storage_mutation_effect_unknown(&fence)
        .unwrap();

    // 3. Blind retry is blocked
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

    // 4. Reconcile: object was never written in MinIO, so it transitions to REJECTED
    let reconciled = worker
        .reconcile_storage_mutation(&transport, &fence)
        .await
        .unwrap();

    assert_eq!(reconciled.state, StorageEffectState::Rejected);

    // 5. Subsequent put attempt is rejected with PreconditionRejected
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
        Err(WorkerStorageError::PreconditionRejected)
    );
}
