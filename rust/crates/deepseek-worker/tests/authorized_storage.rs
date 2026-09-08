//! Tests enforcing durable authority barriers on storage mutation.
use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use bytes::Bytes;
use deepseek_protocol::ActionFence;
use deepseek_storage::s3::{ConditionalWrite, S3Config, S3Credentials, S3Transport};
use deepseek_worker::{StorageEffectState, Worker, WorkerAuthorityConfig, WorkerStorageError};
use ed25519_dalek::{Signer as _, SigningKey};
use serde_json::Value;
use sha2::{Digest, Sha256};

fn fixture() -> (WorkerAuthorityConfig, Vec<u8>, ActionFence, SigningKey) {
    let fixture: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v7/control/authority_request_vector.json"
    )))
    .unwrap();
    let bytes = fixture["canonical_request"]
        .as_str()
        .unwrap()
        .as_bytes()
        .to_vec();
    let document: Value = serde_json::from_slice(&bytes).unwrap();
    let directory = tempfile::tempdir().unwrap();
    let key = SigningKey::from_bytes(
        &Sha256::digest(directory.path().to_string_lossy().as_bytes()).into(),
    );
    let config = WorkerAuthorityConfig {
        signer_public_key: URL_SAFE_NO_PAD.encode(key.verifying_key().as_bytes()),
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
        fencing_token: 4,
        now: Some(fixture["now"].as_str().unwrap().into()),
    };
    let fence = ActionFence {
        action_id: document["actionId"].as_str().unwrap().into(),
        execution_epoch: 4,
    };
    (config, bytes, fence, key)
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

fn dummy_transport() -> S3Transport {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    drop(listener);
    S3Transport::new(
        S3Config {
            endpoint: format!("http://127.0.0.1:{}", addr.port()),
            bucket: "test-bucket".into(),
            prefix: "prefix".into(),
            region: "us-east-1".into(),
            allow_http_loopback: true,
        },
        S3Credentials::new("access".into(), "secret".into(), None).unwrap(),
    )
    .unwrap()
}

#[tokio::test]
async fn bound_intent_is_immutable_and_legacy_intent_cannot_dispatch() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    worker
        .install_signed_epoch(
            &fence,
            &sign_request(&key, &fence.action_id, 4, 4, "1", "1"),
        )
        .unwrap();
    let transport = dummy_transport();
    let digest = Sha256::digest(b"immutable intent").into();
    worker
        .reserve_bound_storage_mutation(
            &fence,
            &transport,
            "immutable-key",
            &digest,
            16,
            &ConditionalWrite::Match("\"prior-etag\"".into()),
        )
        .unwrap();
    let record = worker.query_storage_effect(&fence).unwrap().unwrap();
    assert_eq!(
        record.binding.as_ref().unwrap().expected_etag.as_deref(),
        Some("\"prior-etag\"")
    );
    assert_eq!(
        worker.reserve_bound_storage_mutation(
            &fence,
            &transport,
            "immutable-key",
            &digest,
            0,
            &ConditionalWrite::Match("\"prior-etag\"".into())
        ),
        Err(WorkerStorageError::DigestMismatch)
    );
    assert_eq!(
        worker.reserve_bound_storage_mutation(
            &fence,
            &transport,
            "immutable-key",
            &digest,
            16,
            &ConditionalWrite::Create
        ),
        Err(WorkerStorageError::TargetMismatch)
    );
    let connection =
        rusqlite::Connection::open(directory.path().join("rust-worker/authority.sqlite3")).unwrap();
    let reject_immutable = |sql: &str| {
        let error = connection.execute(sql, []).unwrap_err();
        assert!(
            error.to_string().contains("immutable"),
            "wrong rejection for {sql}: {error}"
        );
    };
    for sql in [
        "UPDATE storage_effect_bindings SET expected_etag=NULL",
        "DELETE FROM storage_effect_bindings",
        "UPDATE storage_effects SET target_key='another-key'",
        "UPDATE storage_effects SET payload_digest='changed-digest'",
        "UPDATE storage_effects SET expected_length=17",
        "UPDATE storage_effects SET expected_version=NULL",
        "UPDATE storage_effects SET authority_principal='another-principal'",
        "UPDATE storage_effects SET rowid=rowid+1",
        "INSERT OR REPLACE INTO storage_effect_bindings SELECT action_id,epoch,target_identity,NULL FROM storage_effect_bindings",
        "INSERT OR REPLACE INTO storage_effects SELECT action_id,epoch,fencing_token,request_id,nonce,operation_kind,'replacement-key',payload_digest,expected_length,expected_version,authority_principal,state,etag,provider_metadata,created_at,updated_at FROM storage_effects",
    ] {
        reject_immutable(sql);
    }
    let legacy = ActionFence {
        action_id: "unbound-legacy-intent".into(),
        execution_epoch: 1,
    };
    worker
        .install_signed_epoch(
            &legacy,
            &sign_request(&key, &legacy.action_id, 1, 4, "2", "2"),
        )
        .unwrap();
    reject_immutable(
        "INSERT OR REPLACE INTO storage_effects (rowid,action_id,epoch,fencing_token,request_id,nonce,operation_kind,target_key,payload_digest,expected_length,expected_version,authority_principal,state,etag,provider_metadata,created_at,updated_at) SELECT rowid,'unbound-legacy-intent',1,fencing_token,request_id,nonce,operation_kind,target_key,payload_digest,expected_length,expected_version,authority_principal,state,etag,provider_metadata,created_at,updated_at FROM storage_effects",
    );
    worker
        .reserve_storage_mutation(&legacy, "legacy-key", &digest)
        .unwrap();
    reject_immutable(
        "INSERT OR REPLACE INTO storage_effect_bindings (rowid,action_id,epoch,target_identity,expected_etag) SELECT rowid,'unbound-legacy-intent',1,target_identity,expected_etag FROM storage_effect_bindings",
    );
    assert_eq!(
        worker.transition_storage_mutation(&legacy, StorageEffectState::Dispatching, None, None),
        Err(WorkerStorageError::FenceMismatch)
    );
    assert_eq!(
        worker.reconcile_storage_mutation(&transport, &legacy).await,
        Err(WorkerStorageError::TargetMismatch)
    );
}

#[tokio::test]
async fn storage_dispatch_claim_is_unique_across_existing_handles() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    // Open both handles before any intent exists so startup recovery cannot settle it.
    let mut first = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    let mut second = Worker::open_with_authority(config, directory.path()).unwrap();
    first
        .install_signed_epoch(
            &fence,
            &sign_request(&key, &fence.action_id, 4, 4, "1", "1"),
        )
        .unwrap();
    let transport = dummy_transport();
    let payload = b"single dispatch claim";
    let digest = Sha256::digest(payload).into();
    for worker in [&mut first, &mut second] {
        worker
            .reserve_bound_storage_mutation(
                &fence,
                &transport,
                "single-dispatch-key",
                &digest,
                payload.len() as u64,
                &ConditionalWrite::Create,
            )
            .unwrap();
    }
    assert_eq!(
        second.query_storage_effect(&fence).unwrap().unwrap().state,
        StorageEffectState::Reserved
    );
    first
        .transition_storage_mutation(&fence, StorageEffectState::Dispatching, None, None)
        .unwrap();

    assert_eq!(
        second.transition_storage_mutation(&fence, StorageEffectState::Dispatching, None, None),
        Err(WorkerStorageError::UnknownEffectRetryBlocked)
    );
    assert_eq!(
        second.reserve_bound_storage_mutation(
            &fence,
            &transport,
            "single-dispatch-key",
            &digest,
            payload.len() as u64,
            &ConditionalWrite::Create,
        ),
        Err(WorkerStorageError::UnknownEffectRetryBlocked)
    );
    assert_eq!(
        first.query_storage_effect(&fence).unwrap().unwrap().state,
        StorageEffectState::Dispatching
    );
}

#[tokio::test]
async fn storage_dispatch_rechecks_epoch_after_reservation() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let mut first = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    let mut second = Worker::open_with_authority(config, directory.path()).unwrap();
    first
        .install_signed_epoch(
            &fence,
            &sign_request(&key, &fence.action_id, 4, 4, "1", "1"),
        )
        .unwrap();
    let transport = dummy_transport();
    let payload = b"stale dispatch claim";
    first
        .reserve_bound_storage_mutation(
            &fence,
            &transport,
            "stale-dispatch-key",
            &Sha256::digest(payload).into(),
            payload.len() as u64,
            &ConditionalWrite::Create,
        )
        .unwrap();
    let next_fence = ActionFence {
        action_id: fence.action_id.clone(),
        execution_epoch: 5,
    };
    second
        .install_signed_epoch(
            &next_fence,
            &sign_request(&key, &next_fence.action_id, 5, 4, "2", "2"),
        )
        .unwrap();

    assert_eq!(
        first.transition_storage_mutation(&fence, StorageEffectState::Dispatching, None, None),
        Err(WorkerStorageError::StaleEpoch)
    );
    assert_eq!(
        first.query_storage_effect(&fence).unwrap().unwrap().state,
        StorageEffectState::Reserved
    );
}

#[tokio::test]
async fn v1_migration_preserves_legacy_journal_and_rolls_back_on_identity_failure() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    let signed = sign_request(&key, &fence.action_id, 4, 4, "1", "1");
    worker.install_signed_epoch(&fence, &signed).unwrap();
    worker
        .reserve_storage_mutation(&fence, "historical-key", &Sha256::digest(b"legacy").into())
        .unwrap();
    worker
        .record_storage_mutation_effect_unknown(&fence)
        .unwrap();
    let prior = worker.query_storage_effect(&fence).unwrap().unwrap();
    drop(worker);
    let path = directory.path().join("rust-worker/authority.sqlite3");
    let connection = rusqlite::Connection::open(&path).unwrap();
    // Construct the exact historical schema only in this newly isolated test DB.
    // No provider effect is inferred from this fixture and no release downgrade API exists.
    assert_eq!(
        connection
            .query_row("SELECT COUNT(*) FROM storage_effect_bindings", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap(),
        0
    );
    let additions: Vec<String> = connection.prepare("SELECT name FROM sqlite_schema WHERE type='trigger' AND (name LIKE 'storage_binding_%' OR name LIKE 'storage_dispatch_%' OR name LIKE 'storage_effect_identity_%' OR name LIKE 'storage_rpc_%')").unwrap()
        .query_map([], |row| row.get(0)).unwrap().collect::<Result<_, _>>().unwrap();
    for name in additions {
        connection
            .execute(&format!("DROP TRIGGER {name}"), [])
            .unwrap();
    }
    connection
        .execute("DROP TABLE storage_rpc_operations", [])
        .unwrap();
    connection
        .execute("DROP TABLE storage_effect_bindings", [])
        .unwrap();
    connection.pragma_update(None, "user_version", 1).unwrap();
    let wrong_identity = WorkerAuthorityConfig {
        fleet_id: "wrong-fleet".into(),
        ..config.clone()
    };
    assert!(Worker::open_with_authority(wrong_identity, directory.path()).is_err());
    assert_eq!(
        connection
            .pragma_query_value(None, "user_version", |row| row.get::<_, i64>(0))
            .unwrap(),
        1
    );
    assert_eq!(
        connection
            .query_row(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name IN ('storage_effect_bindings','storage_rpc_operations')",
                [],
                |row| row.get::<_, i64>(0)
            )
            .unwrap(),
        0
    );
    let mut migrated = Worker::open_with_authority(config, directory.path()).unwrap();
    assert_eq!(
        connection
            .pragma_query_value(None, "user_version", |row| row.get::<_, i64>(0))
            .unwrap(),
        3
    );
    assert_eq!(
        connection
            .query_row("SELECT request FROM epoch_installs", [], |row| row
                .get::<_, Vec<u8>>(0))
            .unwrap(),
        signed
    );
    assert_eq!(
        migrated.query_storage_effect(&fence).unwrap().unwrap(),
        prior
    );
    assert_eq!(
        migrated
            .reconcile_storage_mutation(&dummy_transport(), &fence)
            .await,
        Err(WorkerStorageError::TargetMismatch)
    );
}

#[tokio::test]
async fn unconfigured_worker_rejects_storage_mutation() {
    let mut worker = Worker::new();
    let transport = dummy_transport();
    let payload = Bytes::from_static(b"payload");
    let digest = Sha256::digest(&payload).into();
    let fence = ActionFence {
        action_id: "act-1".into(),
        execution_epoch: 1,
    };
    assert_eq!(
        worker
            .execute_storage_put(
                &transport,
                "key",
                payload,
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::WorkerWithoutAuthority)
    );
}

#[tokio::test]
async fn unauthorized_action_without_authority_request_is_rejected() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, _, _) = fixture();
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    let transport = dummy_transport();
    let payload = Bytes::from_static(b"payload");
    let digest = Sha256::digest(&payload).into();
    let fence = ActionFence {
        action_id: "unknown-act".into(),
        execution_epoch: 1,
    };
    assert_eq!(
        worker
            .execute_storage_put(
                &transport,
                "key",
                payload,
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::FenceMismatch)
    );
}

#[tokio::test]
async fn tampered_signature_install_fails_and_storage_mutation_is_rejected() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    let transport = dummy_transport();
    let mut bad_request = sign_request(&key, &fence.action_id, 4, 4, "1", "1");
    // Tamper one byte
    let len = bad_request.len();
    bad_request[len - 2] ^= 0xff;
    assert!(worker.install_signed_epoch(&fence, &bad_request).is_err());
    let payload = Bytes::from_static(b"payload");
    let digest = Sha256::digest(&payload).into();
    assert_eq!(
        worker
            .execute_storage_put(
                &transport,
                "key",
                payload,
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::FenceMismatch)
    );
}

#[tokio::test]
async fn stale_epoch_is_rejected() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    let transport = dummy_transport();
    let request = sign_request(&key, &fence.action_id, 4, 4, "1", "1");
    worker.install_signed_epoch(&fence, &request).unwrap();
    let stale_fence = ActionFence {
        action_id: fence.action_id.clone(),
        execution_epoch: 3,
    };
    let payload = Bytes::from_static(b"payload");
    let digest = Sha256::digest(&payload).into();
    assert_eq!(
        worker
            .execute_storage_put(
                &transport,
                "key",
                payload,
                digest,
                &stale_fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::StaleEpoch)
    );
}

#[tokio::test]
async fn stale_fencing_token_is_rejected_on_takeover() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let mut first = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    let request = sign_request(&key, &fence.action_id, 4, 4, "1", "1");
    first.install_signed_epoch(&fence, &request).unwrap();
    // Successor worker takes over with higher fencing token 5
    let successor_config = WorkerAuthorityConfig {
        fencing_token: 5,
        ..config
    };
    let _successor = Worker::open_with_authority(successor_config, directory.path()).unwrap();
    let transport = dummy_transport();
    let payload = Bytes::from_static(b"payload");
    let digest = Sha256::digest(&payload).into();
    assert_eq!(
        first
            .execute_storage_put(
                &transport,
                "key",
                payload,
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::StaleFencingToken)
    );
}

#[tokio::test]
async fn wrong_object_target_and_digest_are_rejected() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    let request = sign_request(&key, &fence.action_id, 4, 4, "1", "1");
    worker.install_signed_epoch(&fence, &request).unwrap();
    let payload1 = Bytes::from_static(b"payload1");
    let digest1 = Sha256::digest(&payload1).into();
    // Reserve effect for "target-1" and digest1
    worker
        .reserve_storage_mutation(&fence, "target-1", &digest1)
        .unwrap();

    // Trying to mutate different target under the same action and epoch is rejected
    let transport = dummy_transport();
    assert_eq!(
        worker
            .execute_storage_put(
                &transport,
                "different-target",
                payload1.clone(),
                digest1,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::TargetMismatch)
    );

    // Trying to mutate with wrong digest is rejected
    let payload2 = Bytes::from_static(b"payload2");
    let digest2 = Sha256::digest(&payload2).into();
    assert_eq!(
        worker
            .execute_storage_put(
                &transport,
                "target-1",
                payload2,
                digest2,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::DigestMismatch)
    );
}

#[tokio::test]
async fn committed_effect_rejects_replay_even_after_restart() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    {
        let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
        let request = sign_request(&key, &fence.action_id, 4, 4, "1", "1");
        worker.install_signed_epoch(&fence, &request).unwrap();
        let payload = Bytes::from_static(b"payload");
        let digest = Sha256::digest(&payload).into();
        worker
            .reserve_storage_mutation(&fence, "key", &digest)
            .unwrap();
        worker
            .record_storage_mutation_committed(&fence, "etag-1")
            .unwrap();
        let transport = dummy_transport();
        assert_eq!(
            worker
                .execute_storage_put(
                    &transport,
                    "key",
                    payload.clone(),
                    digest,
                    &fence,
                    ConditionalWrite::Create,
                )
                .await,
            Err(WorkerStorageError::ReplayRejected)
        );
    }
    // Restart worker from disk
    let mut restarted = Worker::open_with_authority(config, directory.path()).unwrap();
    let transport = dummy_transport();
    let payload = Bytes::from_static(b"payload");
    let digest = Sha256::digest(&payload).into();
    assert_eq!(
        restarted
            .execute_storage_put(
                &transport,
                "key",
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
async fn unknown_effect_cannot_be_blindly_retried_and_reconciles() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    let request = sign_request(&key, &fence.action_id, 4, 4, "1", "1");
    worker.install_signed_epoch(&fence, &request).unwrap();
    let payload = Bytes::from_static(b"payload");
    let digest = Sha256::digest(&payload).into();
    worker
        .reserve_storage_mutation(&fence, "key", &digest)
        .unwrap();
    worker
        .record_storage_mutation_effect_unknown(&fence)
        .unwrap();

    let transport = dummy_transport();
    // Blind retry is blocked
    assert_eq!(
        worker
            .execute_storage_put(
                &transport,
                "key",
                payload,
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::UnknownEffectRetryBlocked)
    );

    // Query effect reports EFFECT_UNKNOWN
    let effect = worker.query_storage_effect(&fence).unwrap().unwrap();
    assert_eq!(effect.state, StorageEffectState::EffectUnknown);
}

#[tokio::test]
async fn crash_during_dispatching_recovers_to_effect_unknown_and_requires_reconciliation() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let payload = Bytes::from_static(b"crash-window-payload");
    let digest: [u8; 32] = Sha256::digest(&payload).into();

    {
        let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
        let request = sign_request(&key, &fence.action_id, 4, 4, "1", "1");
        worker.install_signed_epoch(&fence, &request).unwrap();

        // 1. Reserve mutation with extended audit identity
        let proof = worker
            .reserve_bound_storage_mutation(
                &fence,
                &dummy_transport(),
                "data/chunk-01",
                &digest,
                payload.len() as u64,
                &ConditionalWrite::Match("\"v1\"".into()),
            )
            .unwrap();
        assert_eq!(proof.action_id, fence.action_id);

        // 2. Transition to DISPATCHING
        worker
            .transition_storage_mutation(&fence, StorageEffectState::Dispatching, None, None)
            .unwrap();

        let in_flight = worker.query_storage_effect(&fence).unwrap().unwrap();
        assert_eq!(in_flight.state, StorageEffectState::Dispatching);
        assert_eq!(in_flight.operation_kind, "PUT_CHUNK");
        assert_eq!(in_flight.target_key, "data/chunk-01");
        assert_eq!(in_flight.expected_length, payload.len() as u64);
        assert_eq!(in_flight.expected_version.as_deref(), Some("\"v1\""));
        assert!(in_flight.binding.is_some());
        assert_eq!(in_flight.authority_principal, config.signer_public_key);
        assert!(in_flight.etag.is_none());
        assert!(in_flight.provider_metadata.is_none());
        // Worker crashes while dispatching... (drop worker)
    }

    // 3. Worker restarts: AuthorityStore::open must recover DISPATCHING to EFFECT_UNKNOWN
    let mut restarted = Worker::open_with_authority(config, directory.path()).unwrap();
    let recovered = restarted.query_storage_effect(&fence).unwrap().unwrap();
    assert_eq!(recovered.state, StorageEffectState::EffectUnknown);

    // 4. Blind retry via execute_storage_put is blocked
    let transport = dummy_transport();
    assert_eq!(
        restarted
            .execute_storage_put(
                &transport,
                "data/chunk-01",
                payload.clone(),
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::UnknownEffectRetryBlocked)
    );

    // 5. Direct illegal transition EFFECT_UNKNOWN -> CONFIRMED is rejected by trigger
    assert!(
        restarted
            .transition_storage_mutation(
                &fence,
                StorageEffectState::Confirmed,
                Some("etag-illegal"),
                None
            )
            .is_err()
    );

    // 6. Transition EFFECT_UNKNOWN -> RECONCILING is permitted
    restarted
        .transition_storage_mutation(&fence, StorageEffectState::Reconciling, None, None)
        .unwrap();

    // 7. Transition RECONCILING -> CONFIRMED is permitted
    restarted
        .transition_storage_mutation(
            &fence,
            StorageEffectState::Confirmed,
            Some("etag-valid"),
            Some("{\"verified\":true}"),
        )
        .unwrap();

    let final_record = restarted.query_storage_effect(&fence).unwrap().unwrap();
    assert_eq!(final_record.state, StorageEffectState::Confirmed);
    assert_eq!(final_record.etag.as_deref(), Some("etag-valid"));
    assert_eq!(
        final_record.provider_metadata.as_deref(),
        Some("{\"verified\":true}")
    );

    // 8. Terminal state cannot be mutated (storage_effects_monotonic)
    assert!(
        restarted
            .transition_storage_mutation(&fence, StorageEffectState::Reconciling, None, None)
            .is_err()
    );
}

#[tokio::test]
async fn authority_installed_before_effect_reservation_survives_crash_and_allows_reservation() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let payload = Bytes::from_static(b"pre-reservation-crash-payload");
    let digest: [u8; 32] = Sha256::digest(&payload).into();

    {
        // 1. Install authoritative epoch into SQLite store
        let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
        let request = sign_request(&key, &fence.action_id, 4, 4, "1", "1");
        worker.install_signed_epoch(&fence, &request).unwrap();
        // Worker crashes right here before reserving any effect
    }

    // 2. Restart worker: installed epoch survives, effect table is empty
    let mut restarted = Worker::open_with_authority(config, directory.path()).unwrap();
    assert_eq!(restarted.query_storage_effect(&fence).unwrap(), None);

    // 3. Effect can now be cleanly reserved with the surviving installed epoch
    let proof = restarted
        .reserve_storage_mutation(&fence, "data/unreserved-key", &digest)
        .unwrap();
    assert_eq!(proof.action_id, fence.action_id);
    assert_eq!(proof.execution_epoch, 4);

    let recorded = restarted.query_storage_effect(&fence).unwrap().unwrap();
    assert_eq!(recorded.state, StorageEffectState::Reserved);
}

#[tokio::test]
async fn crash_during_reserved_recovers_to_effect_unknown_and_blocks_blind_retry() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let payload = Bytes::from_static(b"reserved-crash-payload");
    let digest: [u8; 32] = Sha256::digest(&payload).into();

    {
        let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
        let request = sign_request(&key, &fence.action_id, 4, 4, "1", "1");
        worker.install_signed_epoch(&fence, &request).unwrap();
        worker
            .reserve_storage_mutation(&fence, "data/reserved-key", &digest)
            .unwrap();

        let reserved = worker.query_storage_effect(&fence).unwrap().unwrap();
        assert_eq!(reserved.state, StorageEffectState::Reserved);
        // Worker crashes before dispatching to provider
    }

    // Restart worker: AuthorityStore::open must recover RESERVED to EFFECT_UNKNOWN
    let mut restarted = Worker::open_with_authority(config, directory.path()).unwrap();
    let recovered = restarted.query_storage_effect(&fence).unwrap().unwrap();
    assert_eq!(recovered.state, StorageEffectState::EffectUnknown);

    // Blind retry is blocked
    let transport = dummy_transport();
    assert_eq!(
        restarted
            .execute_storage_put(
                &transport,
                "data/reserved-key",
                payload,
                digest,
                &fence,
                ConditionalWrite::Create,
            )
            .await,
        Err(WorkerStorageError::UnknownEffectRetryBlocked)
    );
}

#[tokio::test]
async fn crash_during_reconciliation_recovers_to_effect_unknown_and_allows_re_reconciliation() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let payload = Bytes::from_static(b"reconciliation-crash-payload");
    let digest: [u8; 32] = Sha256::digest(&payload).into();

    {
        let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
        let request = sign_request(&key, &fence.action_id, 4, 4, "1", "1");
        worker.install_signed_epoch(&fence, &request).unwrap();
        worker
            .reserve_storage_mutation(&fence, "data/reconcile-crash-key", &digest)
            .unwrap();
        worker
            .transition_storage_mutation(&fence, StorageEffectState::EffectUnknown, None, None)
            .unwrap();

        // Worker enters RECONCILING
        worker
            .transition_storage_mutation(&fence, StorageEffectState::Reconciling, None, None)
            .unwrap();
        assert_eq!(
            worker.query_storage_effect(&fence).unwrap().unwrap().state,
            StorageEffectState::Reconciling
        );
        // Worker crashes during reconciliation
    }

    // Restart worker: AuthorityStore::open must recover RECONCILING to EFFECT_UNKNOWN
    let mut restarted = Worker::open_with_authority(config, directory.path()).unwrap();
    let recovered = restarted.query_storage_effect(&fence).unwrap().unwrap();
    assert_eq!(recovered.state, StorageEffectState::EffectUnknown);

    // Worker can safely re-enter RECONCILING and proceed to CONFIRMED
    restarted
        .transition_storage_mutation(&fence, StorageEffectState::Reconciling, None, None)
        .unwrap();
    restarted
        .transition_storage_mutation(
            &fence,
            StorageEffectState::Confirmed,
            Some("etag-reconciled"),
            Some("{\"reconciled\":true}"),
        )
        .unwrap();

    let final_record = restarted.query_storage_effect(&fence).unwrap().unwrap();
    assert_eq!(final_record.state, StorageEffectState::Confirmed);
    assert_eq!(final_record.etag.as_deref(), Some("etag-reconciled"));
}

#[tokio::test]
async fn takeover_during_effect_unknown_rejects_stale_worker_and_allows_successor_reconciliation() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence, key) = fixture();
    let payload = Bytes::from_static(b"takeover-crash-payload");
    let digest: [u8; 32] = Sha256::digest(&payload).into();

    let mut first_worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    let request = sign_request(&key, &fence.action_id, 4, 4, "1", "1");
    first_worker.install_signed_epoch(&fence, &request).unwrap();
    first_worker
        .reserve_storage_mutation(&fence, "data/takeover-key", &digest)
        .unwrap();
    first_worker
        .transition_storage_mutation(&fence, StorageEffectState::EffectUnknown, None, None)
        .unwrap();

    // Successor worker takes over with higher fencing token 5
    let successor_config = WorkerAuthorityConfig {
        fencing_token: 5,
        ..config
    };
    let mut successor_worker =
        Worker::open_with_authority(successor_config, directory.path()).unwrap();

    // First worker with stale fencing token 4 attempts to reconcile -> rejected
    assert_eq!(
        first_worker.transition_storage_mutation(
            &fence,
            StorageEffectState::Reconciling,
            None,
            None
        ),
        Err(WorkerStorageError::StaleFencingToken)
    );

    // Successor worker with live fencing token 5 successfully reconciles the unknown effect
    successor_worker
        .transition_storage_mutation(&fence, StorageEffectState::Reconciling, None, None)
        .unwrap();
    successor_worker
        .transition_storage_mutation(
            &fence,
            StorageEffectState::Confirmed,
            Some("etag-successor"),
            Some("{\"successor\":true}"),
        )
        .unwrap();

    let final_record = successor_worker
        .query_storage_effect(&fence)
        .unwrap()
        .unwrap();
    assert_eq!(final_record.state, StorageEffectState::Confirmed);
    assert_eq!(final_record.etag.as_deref(), Some("etag-successor"));
}
