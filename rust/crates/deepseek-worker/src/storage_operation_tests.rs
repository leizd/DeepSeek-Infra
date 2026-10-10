use super::*;
use deepseek_storage::s3::{ConditionalWrite, S3Config, S3Credentials, S3Transport};
use rusqlite::Connection;
use serde_json::Value;

fn fixture(root: &std::path::Path) -> (Worker, WorkerAuthorityConfig, ActionFence) {
    let fixture: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v7/control/authority_request_vector.json"
    )))
    .unwrap();
    let bytes = fixture["canonical_request"].as_str().unwrap().as_bytes();
    let document: Value = serde_json::from_slice(bytes).unwrap();
    let fence = ActionFence {
        action_id: document["actionId"].as_str().unwrap().into(),
        execution_epoch: document["executionEpoch"].as_u64().unwrap(),
    };
    let config = WorkerAuthorityConfig {
        signer_public_key: fixture["signer_public_key"].as_str().unwrap().into(),
        fleet_id: "fleet-a".into(),
        environment: "test".into(),
        fencing_token: 4,
        now: Some(fixture["now"].as_str().unwrap().into()),
    };
    let mut worker = Worker::open_with_authority(config.clone(), root).unwrap();
    worker.install_signed_epoch(&fence, bytes).unwrap();
    (worker, config, fence)
}

fn transport() -> S3Transport {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    S3Transport::new(
        S3Config {
            endpoint: format!("http://{}", listener.local_addr().unwrap()),
            bucket: "test-bucket".into(),
            prefix: "operation-test".into(),
            region: "us-east-1".into(),
            allow_http_loopback: true,
        },
        S3Credentials::new("test-access".into(), "test-secret".into(), None).unwrap(),
    )
    .unwrap()
}

fn reserve(
    worker: &mut Worker,
    fence: &ActionFence,
    transport: &S3Transport,
    operation: Option<&str>,
) -> Result<deepseek_storage::s3::StorageAuthorityProof, WorkerStorageError> {
    worker.reserve_bound_storage_mutation_for_operation(
        fence,
        transport,
        "object",
        &[1; 32],
        16,
        &ConditionalWrite::Create,
        operation,
    )
}

#[test]
fn rpc_operation_is_exact_immutable_and_survives_reopen() {
    let directory = tempfile::tempdir().unwrap();
    let (mut worker, config, fence) = fixture(directory.path());
    let transport = transport();
    reserve(&mut worker, &fence, &transport, Some("operation-A")).unwrap();
    reserve(&mut worker, &fence, &transport, Some("operation-A")).unwrap();
    for other in [Some("operation-B"), Some("operation-a"), None] {
        assert_eq!(
            reserve(&mut worker, &fence, &transport, other),
            Err(WorkerStorageError::OperationMismatch)
        );
    }
    let record = worker.query_storage_effect(&fence).unwrap().unwrap();
    assert_eq!(record.operation_id.as_deref(), Some("operation-A"));
    assert_eq!(record.state, StorageEffectState::Reserved);
    drop(worker);
    let mut worker = Worker::open_with_authority(config, directory.path()).unwrap();
    let recovered = worker.query_storage_effect(&fence).unwrap().unwrap();
    assert_eq!(recovered.operation_id, record.operation_id);
    assert_eq!(recovered.state, StorageEffectState::EffectUnknown);
    assert_eq!(
        reserve(&mut worker, &fence, &transport, Some("operation-B")),
        Err(WorkerStorageError::OperationMismatch)
    );
    assert_eq!(
        reserve(&mut worker, &fence, &transport, Some("operation-A")),
        Err(WorkerStorageError::UnknownEffectRetryBlocked)
    );
}

#[test]
fn rpc_cannot_adopt_a_library_intent() {
    let directory = tempfile::tempdir().unwrap();
    let (mut worker, _, fence) = fixture(directory.path());
    let transport = transport();
    reserve(&mut worker, &fence, &transport, None).unwrap();
    assert_eq!(
        reserve(&mut worker, &fence, &transport, Some("invented")),
        Err(WorkerStorageError::OperationMismatch)
    );
    assert_eq!(
        worker
            .query_storage_effect(&fence)
            .unwrap()
            .unwrap()
            .operation_id,
        None
    );
}

#[tokio::test]
async fn library_cannot_transition_or_reconcile_an_rpc_intent() {
    let directory = tempfile::tempdir().unwrap();
    let (mut worker, _, fence) = fixture(directory.path());
    let transport = transport();
    reserve(&mut worker, &fence, &transport, Some("operation")).unwrap();
    assert_eq!(
        worker.transition_storage_mutation_for_operation(
            &fence,
            StorageEffectState::Dispatching,
            None,
            None,
            Some("substitute")
        ),
        Err(WorkerStorageError::OperationMismatch)
    );
    for state in [
        StorageEffectState::Dispatching,
        StorageEffectState::EffectUnknown,
        StorageEffectState::Confirmed,
        StorageEffectState::Rejected,
    ] {
        assert_eq!(
            worker.transition_storage_mutation(&fence, state, None, None),
            Err(WorkerStorageError::OperationMismatch)
        );
    }
    assert_eq!(
        worker.reconcile_storage_mutation(&transport, &fence).await,
        Err(WorkerStorageError::OperationMismatch)
    );
    assert_eq!(
        worker.query_storage_effect(&fence).unwrap().unwrap().state,
        StorageEffectState::Reserved
    );
}

#[test]
fn failed_operation_insert_rolls_back_parent_and_placement() {
    let directory = tempfile::tempdir().unwrap();
    let (mut worker, _, fence) = fixture(directory.path());
    let connection =
        Connection::open(directory.path().join("rust-worker/authority.sqlite3")).unwrap();
    connection.execute_batch("CREATE TRIGGER injected_rpc_failure BEFORE INSERT ON storage_rpc_operations BEGIN SELECT RAISE(ABORT,'injected final insert failure'); END").unwrap();
    let transport = transport();
    assert_eq!(
        reserve(&mut worker, &fence, &transport, Some("operation")),
        Err(WorkerStorageError::FenceMismatch)
    );
    for table in [
        "storage_effects",
        "storage_effect_bindings",
        "storage_rpc_operations",
    ] {
        assert_eq!(
            connection
                .query_row(&format!("SELECT COUNT(*) FROM {table}"), [], |row| row
                    .get::<_, i64>(0))
                .unwrap(),
            0
        );
    }
    connection
        .execute("DROP TRIGGER injected_rpc_failure", [])
        .unwrap();
    reserve(&mut worker, &fence, &transport, Some("operation")).unwrap();
}

#[test]
fn rpc_identity_guards_reject_update_delete_replace_and_orphans() {
    let directory = tempfile::tempdir().unwrap();
    let (mut worker, _, fence) = fixture(directory.path());
    reserve(&mut worker, &fence, &transport(), Some("operation")).unwrap();
    let connection =
        Connection::open(directory.path().join("rust-worker/authority.sqlite3")).unwrap();
    for sql in [
        "UPDATE storage_rpc_operations SET operation_id='changed'",
        "DELETE FROM storage_rpc_operations",
        "INSERT OR REPLACE INTO storage_rpc_operations SELECT action_id,epoch,'changed' FROM storage_rpc_operations",
        "INSERT OR REPLACE INTO storage_rpc_operations(rowid,action_id,epoch,operation_id) SELECT rowid,'other',epoch,'changed' FROM storage_rpc_operations",
        "INSERT INTO storage_rpc_operations VALUES ('missing-parent',1,'orphan')",
    ] {
        assert!(connection.execute(sql, []).is_err(), "{sql}");
    }
    assert_eq!(
        worker
            .query_storage_effect(&fence)
            .unwrap()
            .unwrap()
            .operation_id
            .as_deref(),
        Some("operation")
    );
}

#[test]
fn operation_validation_bounds_utf8_bytes_without_normalizing() {
    for id in ["", "\n\t", "\u{2003}", "a\0b"] {
        assert!(!valid_storage_operation_id(id));
    }
    assert!(!valid_storage_operation_id(&"a".repeat(1025)));
    assert!(valid_storage_operation_id(&"é".repeat(512)));
    assert!(!valid_storage_operation_id(&"é".repeat(513)));
    assert!(valid_storage_operation_id(" Operation-A "));
}

#[test]
fn v2_migration_preserves_unbound_intent_and_rolls_back_on_identity_failure() {
    let directory = tempfile::tempdir().unwrap();
    let (mut worker, config, fence) = fixture(directory.path());
    let transport = transport();
    reserve(&mut worker, &fence, &transport, None).unwrap();
    worker
        .record_storage_mutation_effect_unknown(&fence)
        .unwrap();
    let prior = worker.query_storage_effect(&fence).unwrap().unwrap();
    drop(worker);
    let connection =
        Connection::open(directory.path().join("rust-worker/authority.sqlite3")).unwrap();
    let signed: Vec<u8> = connection
        .query_row("SELECT request FROM epoch_installs", [], |row| row.get(0))
        .unwrap();
    // Construct v2 only in this isolated test DB. This is not a supported
    // downgrade or evidence of a provider effect.
    connection
        .execute_batch(
            "BEGIN IMMEDIATE;
        DROP TRIGGER storage_grant_no_update;
        DROP TRIGGER storage_grant_no_delete;
        DROP TRIGGER storage_grant_no_replace;
        DROP TRIGGER storage_grant_fence;
        DROP TRIGGER storage_grant_epoch_replay;
        DROP TRIGGER epoch_grant_replay;
        DROP TRIGGER storage_grant_operation_binding;
        DROP TABLE storage_operation_grants;
        DROP TRIGGER storage_rpc_parent;
        DROP TRIGGER storage_rpc_no_update;
        DROP TRIGGER storage_rpc_no_delete;
        DROP TRIGGER storage_rpc_no_replace;
        DROP TABLE storage_rpc_operations;
        PRAGMA user_version=2;
        COMMIT;",
        )
        .unwrap();
    let wrong_identity = WorkerAuthorityConfig {
        fleet_id: "wrong-fleet".into(),
        ..config.clone()
    };
    assert!(Worker::open_with_authority(wrong_identity, directory.path()).is_err());
    assert_eq!(
        connection
            .pragma_query_value(None, "user_version", |row| row.get::<_, i64>(0))
            .unwrap(),
        2
    );
    assert_eq!(
        connection
            .query_row(
                "SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE 'storage_rpc_%' OR name LIKE 'storage_grant_%' OR name IN ('storage_operation_grants','epoch_grant_replay')",
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
        4
    );
    assert_eq!(
        connection
            .query_row("SELECT COUNT(*) FROM storage_operation_grants", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap(),
        0,
        "migration must not infer grants for historical intents"
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
        reserve(&mut migrated, &fence, &transport, Some("invented")),
        Err(WorkerStorageError::OperationMismatch)
    );
}

#[test]
fn startup_rejects_invalid_or_orphaned_rpc_associations_without_repair() {
    for corruption in ["blank-id", "missing-effect", "missing-placement"] {
        let directory = tempfile::tempdir().unwrap();
        let (mut worker, config, fence) = fixture(directory.path());
        if corruption == "missing-placement" {
            worker
                .reserve_storage_mutation(&fence, "object", &[1; 32])
                .unwrap();
        } else {
            reserve(&mut worker, &fence, &transport(), None).unwrap();
        }
        drop(worker);
        let connection =
            Connection::open(directory.path().join("rust-worker/authority.sqlite3")).unwrap();
        let guard: String = connection
            .query_row(
                "SELECT sql FROM sqlite_schema WHERE name='storage_rpc_parent'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        // Simulate corrupted rows in an isolated journal, then restore the exact
        // schema. Startup must inspect data, not merely trust table/trigger names.
        connection
            .execute("DROP TRIGGER storage_rpc_parent", [])
            .unwrap();
        let action = if corruption == "missing-effect" {
            "orphan"
        } else {
            &fence.action_id
        };
        let operation = if corruption == "blank-id" {
            "\t"
        } else {
            "operation"
        };
        connection
            .execute(
                "INSERT INTO storage_rpc_operations VALUES (?1,?2,?3)",
                rusqlite::params![action, fence.execution_epoch as i64, operation],
            )
            .unwrap();
        connection.execute(&guard, []).unwrap();
        assert!(
            Worker::open_with_authority(config, directory.path()).is_err(),
            "{corruption}"
        );
        assert_eq!(
            connection
                .query_row(
                    "SELECT operation_id FROM storage_rpc_operations",
                    [],
                    |row| row.get::<_, String>(0)
                )
                .unwrap(),
            operation
        );
        assert_eq!(
            connection
                .query_row("SELECT state FROM storage_effects", [], |row| row
                    .get::<_, String>(0))
                .unwrap(),
            "RESERVED"
        );
    }
}
