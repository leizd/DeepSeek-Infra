//! In-memory admission regressions only; none of these fixtures are provider proof.
use crate::{StorageOperationCommand, Worker, WorkerAuthorityConfig};
use serde_json::Value;

fn setup() -> (Worker, Vec<u8>, Value) {
    let fixture: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v31/control/storage_operation_grant_vector.json"
    )))
    .unwrap();
    let raw = fixture["canonical_request"]
        .as_str()
        .unwrap()
        .as_bytes()
        .to_vec();
    let document: Value = serde_json::from_slice(&raw).unwrap();
    let mut worker = Worker::new();
    worker
        .configure_authority(WorkerAuthorityConfig {
            signer_public_key: fixture["signer_public_key"].as_str().unwrap().into(),
            fleet_id: "fleet-a".into(),
            environment: "test".into(),
            fencing_token: 4,
            now: Some(fixture["now"].as_str().unwrap().into()),
        })
        .unwrap();
    worker
        .install_authoritative_epoch(&deepseek_protocol::ActionFence {
            action_id: "act-1".into(),
            execution_epoch: 4,
        })
        .unwrap();
    worker
        .admit_storage_operation_grant(&raw, &command(&document))
        .unwrap();
    (worker, raw, document)
}

fn command(document: &Value) -> StorageOperationCommand<'_> {
    let payload = &document["payload"];
    StorageOperationCommand {
        action_id: document["actionId"].as_str().unwrap(),
        execution_epoch: document["executionEpoch"].as_u64().unwrap(),
        operation_id: document["operationId"].as_str().unwrap(),
        mutation_type: payload["mutationType"].as_str().unwrap(),
        provider: payload["provider"].as_str().unwrap(),
        target_identity: payload["targetIdentity"].as_str().unwrap(),
        bucket: payload["bucket"].as_str().unwrap(),
        prefix: payload["prefix"].as_str().unwrap(),
        object_key: payload["objectKey"].as_str().unwrap(),
        object_digest: payload["objectDigest"].as_str().unwrap(),
        expected_length: payload["expectedLength"].as_u64().unwrap(),
        condition_type: payload["conditionType"].as_str().unwrap(),
        expected_etag: payload["expectedEtag"].as_str().unwrap(),
        claim_revision: payload["claimRevision"].as_i64().unwrap(),
    }
}

#[test]
fn cached_grant_rechecks_expiry_and_writer_fence() {
    for expired in [false, true] {
        let (mut worker, raw, document) = setup();
        let authority = worker.authority.as_mut().unwrap();
        let expected = if expired {
            authority.now = Some(document["expiresAt"].as_str().unwrap().into());
            "STORAGE_OPERATION_GRANT_EXPIRED"
        } else {
            authority.fencing_token += 1;
            "STORAGE_OPERATION_GRANT_STALE_FENCING_TOKEN"
        };
        assert_eq!(
            worker
                .admit_storage_operation_grant(&raw, &command(&document))
                .unwrap_err()
                .code,
            expected
        );
    }
}

#[test]
fn rejected_cached_substitution_does_not_replace_original_grant() {
    let (mut worker, raw, document) = setup();
    let mut substituted = command(&document);
    substituted.object_key = "objects/substitution";
    assert_eq!(
        worker
            .admit_storage_operation_grant(&raw, &substituted)
            .unwrap_err()
            .code,
        "STORAGE_OPERATION_GRANT_COMMAND_MISMATCH"
    );
    worker
        .admit_storage_operation_grant(&raw, &command(&document))
        .unwrap();
    let mut changed = document.clone();
    changed["payload"]["objectKey"] = Value::from("objects/substitution");
    let bytes = serde_json::to_vec(&changed).unwrap();
    assert_eq!(
        worker
            .admit_storage_operation_grant(&bytes, &command(&changed))
            .unwrap_err()
            .code,
        "STORAGE_OPERATION_GRANT_REPLAY"
    );
    worker
        .admit_storage_operation_grant(&raw, &command(&document))
        .unwrap();
}

#[test]
fn worker_bounds_grant_input_before_decoding_or_authority_lookup() {
    let (_, _, document) = setup();
    let mut worker = Worker::new();
    let oversized = vec![b' '; crate::MAX_STORAGE_OPERATION_GRANT_BYTES + 1];
    assert_eq!(
        worker
            .admit_storage_operation_grant(&oversized, &command(&document))
            .unwrap_err()
            .code,
        "STORAGE_OPERATION_GRANT_TOO_LARGE"
    );
}
