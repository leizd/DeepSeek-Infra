use base64::{Engine as _, engine::general_purpose::URL_SAFE_NO_PAD};
use deepseek_protocol::{ActionFence, AdmitError, CommandKind};
use deepseek_worker::{Worker, WorkerAuthorityConfig};
use ed25519_dalek::{Signer as _, SigningKey};
use serde_json::Value;
use sha2::{Digest, Sha256};

fn fixture() -> (WorkerAuthorityConfig, Vec<u8>, ActionFence) {
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
    (
        WorkerAuthorityConfig {
            signer_public_key: fixture["signer_public_key"].as_str().unwrap().into(),
            fleet_id: "fleet-a".into(),
            environment: "test".into(),
            fencing_token: 4,
            now: Some(fixture["now"].as_str().unwrap().into()),
        },
        bytes,
        ActionFence {
            action_id: document["actionId"].as_str().unwrap().into(),
            execution_epoch: 4,
        },
    )
}

#[test]
fn restart_preserves_epoch_and_rejects_replay_without_storage_authority() {
    let directory = tempfile::tempdir().unwrap();
    let (config, bytes, fence) = fixture();
    {
        let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
        assert_eq!(worker.admit(&fence), Err(AdmitError::FenceMismatch));
        assert_eq!(worker.install_signed_epoch(&fence, &bytes).unwrap(), fence);
    }
    let mut restarted = Worker::open_with_authority(config, directory.path()).unwrap();
    assert_eq!(restarted.admit(&fence), Ok(()));
    assert_eq!(
        restarted
            .install_signed_epoch(&fence, &bytes)
            .unwrap_err()
            .code,
        "STALE_EXECUTION_EPOCH"
    );
    assert_eq!(
        restarted.execute(CommandKind::ExecuteBackup, &fence),
        Err(AdmitError::StorageNotAuthoritative)
    );
    assert_eq!(
        restarted.query_effect(&fence),
        Err(AdmitError::UnknownEffect)
    );
}

#[test]
fn independent_handles_share_installed_epochs_and_fencing_takeover() {
    let directory = tempfile::tempdir().unwrap();
    let (config, bytes, fence) = fixture();
    let mut first = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    let second = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    first.install_signed_epoch(&fence, &bytes).unwrap();
    assert_eq!(second.admit(&fence), Ok(()));
    let next_config = WorkerAuthorityConfig {
        fencing_token: 5,
        ..config.clone()
    };
    let successor = Worker::open_with_authority(next_config, directory.path()).unwrap();
    assert_eq!(first.admit(&fence), Err(AdmitError::FenceMismatch));
    assert_eq!(second.admit(&fence), Err(AdmitError::FenceMismatch));
    assert_eq!(successor.admit(&fence), Err(AdmitError::FenceMismatch));
    assert_eq!(
        first.install_signed_epoch(&fence, &bytes).unwrap_err().code,
        "AUTHORITY_REQUEST_STALE_FENCING_TOKEN"
    );
    assert_eq!(
        Worker::open_with_authority(config, directory.path())
            .unwrap_err()
            .code,
        "AUTHORITY_REQUEST_STALE_FENCING_TOKEN"
    );
}

#[test]
fn durable_worker_rejects_unsigned_epoch_install_and_changed_identity() {
    let directory = tempfile::tempdir().unwrap();
    let (config, _, fence) = fixture();
    let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    assert_eq!(
        worker.install_authoritative_epoch(&fence),
        Err(AdmitError::FenceMismatch)
    );
    let changed = WorkerAuthorityConfig {
        fleet_id: "fleet-b".into(),
        ..config
    };
    assert_eq!(
        Worker::open_with_authority(changed, directory.path())
            .unwrap_err()
            .code,
        "WORKER_AUTHORITY_STORE_IDENTITY_MISMATCH"
    );
    assert_eq!(worker.admit(&fence), Err(AdmitError::FenceMismatch));
}

#[test]
fn tampered_request_and_envelope_mismatch_do_not_poison_retry() {
    let directory = tempfile::tempdir().unwrap();
    let (config, bytes, fence) = fixture();
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    let other = ActionFence {
        action_id: "other".into(),
        ..fence.clone()
    };
    assert_eq!(
        worker
            .install_signed_epoch(&other, &bytes)
            .unwrap_err()
            .code,
        "FENCE_MISMATCH"
    );
    let mut bad = bytes.clone();
    bad.push(b'0');
    assert!(worker.install_signed_epoch(&fence, &bad).is_err());
    assert_eq!(worker.admit(&fence), Err(AdmitError::FenceMismatch));
    worker.install_signed_epoch(&fence, &bytes).unwrap();
}

#[test]
fn late_statement_failure_rolls_back_epoch_and_replay_reservations() {
    let directory = tempfile::tempdir().unwrap();
    let (config, bytes, fence) = fixture();
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    let connection =
        rusqlite::Connection::open(directory.path().join("rust-worker/authority.sqlite3")).unwrap();
    connection.execute_batch("CREATE TRIGGER injected_failure AFTER INSERT ON epoch_installs BEGIN SELECT RAISE(ABORT,'injected failure'); END").unwrap();
    assert_eq!(
        worker
            .install_signed_epoch(&fence, &bytes)
            .unwrap_err()
            .code,
        "WORKER_AUTHORITY_STORE_UNAVAILABLE"
    );
    let count: i64 = connection
        .query_row("SELECT COUNT(*) FROM epoch_installs", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 0);
    assert_eq!(worker.admit(&fence), Err(AdmitError::FenceMismatch));
    connection
        .execute_batch("DROP TRIGGER injected_failure")
        .unwrap();
    worker.install_signed_epoch(&fence, &bytes).unwrap();
}

#[test]
fn foreign_database_is_not_adopted_or_written() {
    for table in ["python_owned", "sqliteX_python_owned"] {
        let directory = tempfile::tempdir().unwrap();
        std::fs::create_dir(directory.path().join("rust-worker")).unwrap();
        let path = directory.path().join("rust-worker/authority.sqlite3");
        let foreign = rusqlite::Connection::open(&path).unwrap();
        foreign
            .execute_batch(&format!("CREATE TABLE {table} (value TEXT)"))
            .unwrap();
        drop(foreign);
        let original = std::fs::read(&path).unwrap();
        assert!(Worker::open_with_authority(fixture().0, directory.path()).is_err());
        assert_eq!(std::fs::read(&path).unwrap(), original);
        assert!(
            !directory
                .path()
                .join("rust-worker/authority.sqlite3-journal")
                .exists()
        );
    }
}

#[test]
fn unknown_sqlite_lookalike_table_is_not_hidden_in_owned_database() {
    let directory = tempfile::tempdir().unwrap();
    let config = fixture().0;
    drop(Worker::open_with_authority(config.clone(), directory.path()).unwrap());
    let connection =
        rusqlite::Connection::open(directory.path().join("rust-worker/authority.sqlite3")).unwrap();
    connection
        .execute_batch("CREATE TABLE sqliteX_unexpected (value TEXT)")
        .unwrap();
    drop(connection);
    assert!(Worker::open_with_authority(config, directory.path()).is_err());
}

#[test]
fn preexisting_empty_or_marker_only_file_is_not_reinitialized() {
    for marked in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        std::fs::create_dir(directory.path().join("rust-worker")).unwrap();
        let path = directory.path().join("rust-worker/authority.sqlite3");
        if marked {
            let connection = rusqlite::Connection::open(&path).unwrap();
            connection
                .pragma_update(None, "application_id", 0x44535741_i64)
                .unwrap();
        } else {
            std::fs::File::create(&path).unwrap();
        }
        let original = std::fs::read(&path).unwrap();
        assert!(Worker::open_with_authority(fixture().0, directory.path()).is_err());
        assert_eq!(std::fs::read(path).unwrap(), original);
    }
}

#[test]
fn corrupted_signed_install_is_rejected_on_restart() {
    let directory = tempfile::tempdir().unwrap();
    let (config, bytes, fence) = fixture();
    let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    worker.install_signed_epoch(&fence, &bytes).unwrap();
    drop(worker);
    let connection =
        rusqlite::Connection::open(directory.path().join("rust-worker/authority.sqlite3")).unwrap();
    let trigger: String = connection
        .query_row(
            "SELECT sql FROM sqlite_schema WHERE name='epoch_no_update'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    // Deliberate corruption injection, not manufactured execution evidence.
    connection
        .execute_batch("DROP TRIGGER epoch_no_update; UPDATE epoch_installs SET request=x'00'")
        .unwrap();
    connection.execute_batch(&trigger).unwrap();
    drop(connection);
    assert_eq!(
        Worker::open_with_authority(config, directory.path())
            .unwrap_err()
            .code,
        "WORKER_AUTHORITY_STORE_UNAVAILABLE"
    );
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

fn signed(key: &SigningKey, epoch: u64, token: i64, request: &str, nonce: &str) -> Vec<u8> {
    let mut value: Value = serde_json::from_slice(&fixture().1).unwrap();
    let map = value.as_object_mut().unwrap();
    map.remove("signature");
    map.remove("digest");
    map.insert(
        "signerKeyId".into(),
        format!(
            "ctrl-signer-{}",
            &format!("{:x}", Sha256::digest(key.verifying_key().as_bytes()))[..16]
        )
        .into(),
    );
    map.insert("executionEpoch".into(), epoch.into());
    map.insert("fencingToken".into(), token.into());
    map.insert("requestId".into(), request.repeat(64).into());
    map.insert("nonce".into(), nonce.repeat(64).into());
    value["digest"] = format!("sha256:{:x}", Sha256::digest(canonical(&value))).into();
    let mut message = b"deepseek-infra:control-authority-request-v1\0".to_vec();
    message.extend(canonical(&value));
    value["signature"] = URL_SAFE_NO_PAD.encode(key.sign(&message).to_bytes()).into();
    canonical(&value)
}

#[test]
fn durable_request_and_nonce_indexes_block_reuse_at_higher_epoch() {
    let directory = tempfile::tempdir().unwrap();
    // Unique ephemeral test signer, never a production key or checked-in secret.
    let key = SigningKey::from_bytes(
        &Sha256::digest(directory.path().to_string_lossy().as_bytes()).into(),
    );
    let mut config = fixture().0;
    config.signer_public_key = URL_SAFE_NO_PAD.encode(key.verifying_key().as_bytes());
    let mut worker = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    let fence = fixture().2;
    worker
        .install_signed_epoch(&fence, &signed(&key, 4, 4, "a", "b"))
        .unwrap();
    drop(worker);
    let mut restarted = Worker::open_with_authority(config, directory.path()).unwrap();
    let next = ActionFence {
        execution_epoch: 5,
        ..fence.clone()
    };
    assert_eq!(
        restarted
            .install_signed_epoch(&next, &signed(&key, 5, 4, "a", "c"))
            .unwrap_err()
            .code,
        "AUTHORITY_REQUEST_REPLAY"
    );
    assert_eq!(
        restarted
            .install_signed_epoch(&next, &signed(&key, 5, 4, "c", "b"))
            .unwrap_err()
            .code,
        "AUTHORITY_REQUEST_NONCE_REUSE"
    );
    assert_eq!(restarted.admit(&next), Err(AdmitError::FenceMismatch));
    restarted
        .install_signed_epoch(&next, &signed(&key, 5, 4, "c", "d"))
        .unwrap();
    assert_eq!(restarted.admit(&fence), Err(AdmitError::StaleEpoch));
    assert_eq!(restarted.admit(&next), Ok(()));
}

#[test]
fn concurrent_installers_commit_exactly_one_identical_epoch() {
    let directory = tempfile::tempdir().unwrap();
    let (config, bytes, fence) = fixture();
    let first = Worker::open_with_authority(config.clone(), directory.path()).unwrap();
    let second = Worker::open_with_authority(config, directory.path()).unwrap();
    let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
    let handles: Vec<_> = [first, second]
        .into_iter()
        .map(|mut worker| {
            let barrier = barrier.clone();
            let bytes = bytes.clone();
            let fence = fence.clone();
            std::thread::spawn(move || {
                barrier.wait();
                worker.install_signed_epoch(&fence, &bytes)
            })
        })
        .collect();
    let results: Vec<_> = handles
        .into_iter()
        .map(|handle| handle.join().unwrap())
        .collect();
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert_eq!(
        results
            .iter()
            .filter_map(|result| result.as_ref().err())
            .next()
            .unwrap()
            .code,
        "STALE_EXECUTION_EPOCH"
    );
}
