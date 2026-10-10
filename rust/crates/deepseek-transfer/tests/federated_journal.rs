use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use deepseek_transfer::{
    FederatedTransferJournal, ProposedTransfer, TransferRole, TransferState, derive_transfer_id,
};
use rusqlite::Connection;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fmt::Write as _;
use std::sync::{Arc, Barrier};

const OBJECT_SET_DIGEST: &str =
    "sha256:3333333333333333333333333333333333333333333333333333333333333333";
const POLICY_ID: &str = "policy-offsite-custody";
const BACKUP_ID: &str = "backup-20260901-002";
const NOW: &str = "2026-09-01T07:00:00Z";

fn identity(fleet_id: &str, public_byte: u8) -> Value {
    let public_key = [public_byte; 32];
    let digest = hex(&Sha256::digest(public_key));
    json!({
        "schema": "fleet-identity-v1",
        "fleetId": fleet_id,
        "rootKeyId": format!("fed-root-{}", &digest[..24]),
        "rootPublicKey": URL_SAFE_NO_PAD.encode(public_key),
        "rootFingerprint": format!("sha256:{digest}"),
        "signatureAlgorithm": "Ed25519",
        "createdAt": "2026-09-01T06:00:00Z"
    })
}

fn hex(bytes: &[u8]) -> String {
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        let _ = write!(encoded, "{byte:02x}");
    }
    encoded
}

fn proposed() -> ProposedTransfer {
    let transfer_id = derive_transfer_id("fleet-a", "fleet-b", BACKUP_ID, OBJECT_SET_DIGEST)
        .expect("derive transfer id");
    ProposedTransfer {
        transfer_id,
        source_fleet_id: "fleet-a".to_string(),
        destination_fleet_id: "fleet-b".to_string(),
        policy_id: POLICY_ID.to_string(),
        backup_id: BACKUP_ID.to_string(),
        object_set_digest: OBJECT_SET_DIGEST.to_string(),
    }
}

#[test]
fn journal_is_sovereign_durable_and_idempotent() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("transfers.sqlite3");
    let fleet_a = identity("fleet-a", 0x11);
    let journal = FederatedTransferJournal::open(&path, &fleet_a).unwrap();

    let created = journal.persist_proposed_transfer(&proposed(), NOW).unwrap();
    assert_eq!(created.schema, "federated-transfer-journal-record-v1");
    assert_eq!(created.role, TransferRole::Sender);
    assert_eq!(created.state, TransferState::Proposed);
    assert_eq!(created.revision, 1);
    assert_eq!(
        journal
            .persist_proposed_transfer(&proposed(), "2026-09-01T07:00:01Z")
            .unwrap(),
        created
    );
    assert_eq!(
        journal
            .list_transfer_events(&created.transfer_id)
            .unwrap()
            .len(),
        1
    );

    drop(journal);
    let reopened = FederatedTransferJournal::open(&path, &fleet_a).unwrap();
    assert_eq!(
        reopened.get_transfer(&created.transfer_id).unwrap(),
        Some(created)
    );
}

#[test]
fn journal_enforces_monotonic_cas_and_secret_free_state() {
    let temp = tempfile::tempdir().unwrap();
    let journal = FederatedTransferJournal::open(
        temp.path().join("transfers.sqlite3"),
        &identity("fleet-a", 0x11),
    )
    .unwrap();
    let created = journal.persist_proposed_transfer(&proposed(), NOW).unwrap();

    let skipped = journal
        .advance_transfer(
            &created.transfer_id,
            1,
            TransferState::Transferring,
            json!({}),
            "2026-09-01T07:00:01Z",
        )
        .unwrap_err();
    assert_eq!(
        skipped.code(),
        "FEDERATION_TRANSFER_STATE_TRANSITION_INVALID"
    );

    let stale = journal
        .advance_transfer(
            &created.transfer_id,
            0,
            TransferState::GrantRequested,
            json!({}),
            "2026-09-01T07:00:01Z",
        )
        .unwrap_err();
    assert_eq!(stale.code(), "FEDERATION_TRANSFER_REVISION_CONFLICT");

    let regression = journal
        .advance_transfer(
            &created.transfer_id,
            1,
            TransferState::GrantRequested,
            json!({}),
            "2026-09-01T06:59:59Z",
        )
        .unwrap_err();
    assert_eq!(
        regression.code(),
        "FEDERATION_TRANSFER_TIMESTAMP_REGRESSION"
    );

    let sensitive = journal
        .advance_transfer(
            &created.transfer_id,
            1,
            TransferState::GrantRequested,
            json!({"nested": {"receiverAccessKey": "must-not-be-journaled"}}),
            "2026-09-01T07:00:01Z",
        )
        .unwrap_err();
    assert_eq!(
        sensitive.code(),
        "FEDERATION_TRANSFER_SENSITIVE_STATE_REJECTED"
    );

    let advanced = journal
        .advance_transfer(
            &created.transfer_id,
            1,
            TransferState::GrantRequested,
            json!({"requestDigest": format!("sha256:{}", "5".repeat(64))}),
            "2026-09-01T07:00:01Z",
        )
        .unwrap();
    assert_eq!(advanced.revision, 2);
    let retry = journal
        .advance_transfer(
            &created.transfer_id,
            1,
            TransferState::GrantRequested,
            advanced.state_details.clone(),
            "2026-09-01T07:00:02Z",
        )
        .unwrap();
    assert_eq!(retry, advanced);
}

#[test]
fn concurrent_transitions_converge_or_conflict_without_duplicate_events() {
    let temp = tempfile::tempdir().unwrap();
    let journal = Arc::new(
        FederatedTransferJournal::open(
            temp.path().join("transfers.sqlite3"),
            &identity("fleet-a", 0x11),
        )
        .unwrap(),
    );
    let transfer_id = journal
        .persist_proposed_transfer(&proposed(), NOW)
        .unwrap()
        .transfer_id;
    let barrier = Arc::new(Barrier::new(2));
    let handles: Vec<_> = (0..2)
        .map(|_| {
            let journal = Arc::clone(&journal);
            let transfer_id = transfer_id.clone();
            let barrier = Arc::clone(&barrier);
            std::thread::spawn(move || {
                barrier.wait();
                journal.advance_transfer(
                    &transfer_id,
                    1,
                    TransferState::GrantRequested,
                    json!({"requestDigest": format!("sha256:{}", "5".repeat(64))}),
                    "2026-09-01T07:00:01Z",
                )
            })
        })
        .collect();
    let results: Vec<_> = handles
        .into_iter()
        .map(|handle| handle.join().unwrap())
        .collect();
    assert!(results.iter().all(Result::is_ok));
    assert_eq!(results[0].as_ref().unwrap(), results[1].as_ref().unwrap());
    assert_eq!(journal.list_transfer_events(&transfer_id).unwrap().len(), 2);
}

#[test]
fn journal_identity_is_bound_to_one_fleet() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("transfers.sqlite3");
    FederatedTransferJournal::open(&path, &identity("fleet-a", 0x11)).unwrap();
    let conflict = FederatedTransferJournal::open(&path, &identity("fleet-b", 0x22)).unwrap_err();
    assert_eq!(
        conflict.code(),
        "FEDERATION_TRANSFER_JOURNAL_IDENTITY_CONFLICT"
    );
}

#[test]
fn failed_event_insert_rolls_back_the_record_transition() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("transfers.sqlite3");
    let journal = FederatedTransferJournal::open(&path, &identity("fleet-a", 0x11)).unwrap();
    let created = journal.persist_proposed_transfer(&proposed(), NOW).unwrap();
    let raw = Connection::open(&path).unwrap();
    raw.execute_batch(
        r#"
        CREATE TRIGGER reject_revision_two
        BEFORE INSERT ON federation_transfer_events
        WHEN NEW.revision = 2
        BEGIN
            SELECT RAISE(ABORT, 'injected event failure');
        END;
        "#,
    )
    .unwrap();

    let failed = journal
        .advance_transfer(
            &created.transfer_id,
            1,
            TransferState::GrantRequested,
            json!({"requestDigest": format!("sha256:{}", "5".repeat(64))}),
            "2026-09-01T07:00:01Z",
        )
        .unwrap_err();
    assert_eq!(failed.code(), "FEDERATION_TRANSFER_JOURNAL_IO_ERROR");
    raw.execute_batch("DROP TRIGGER reject_revision_two;")
        .unwrap();
    assert_eq!(
        journal.get_transfer(&created.transfer_id).unwrap(),
        Some(created)
    );
    assert_eq!(
        journal
            .list_transfer_events(&proposed().transfer_id)
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn corrupted_record_or_event_chain_fails_closed() {
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("transfers.sqlite3");
    let journal = FederatedTransferJournal::open(&path, &identity("fleet-a", 0x11)).unwrap();
    let created = journal.persist_proposed_transfer(&proposed(), NOW).unwrap();
    let raw = Connection::open(&path).unwrap();
    raw.execute(
        "UPDATE federation_transfers SET local_fleet_id = 'fleet-b' WHERE transfer_id = ?1",
        [&created.transfer_id],
    )
    .unwrap();
    let corrupt = journal.get_transfer(&created.transfer_id).unwrap_err();
    assert_eq!(corrupt.code(), "FEDERATION_TRANSFER_JOURNAL_CORRUPT");
    raw.execute(
        "UPDATE federation_transfers SET local_fleet_id = 'fleet-a' WHERE transfer_id = ?1",
        [&created.transfer_id],
    )
    .unwrap();
    raw.execute(
        "DELETE FROM federation_transfer_events WHERE transfer_id = ?1",
        [&created.transfer_id],
    )
    .unwrap();
    let corrupt_events = journal
        .list_transfer_events(&created.transfer_id)
        .unwrap_err();
    assert_eq!(corrupt_events.code(), "FEDERATION_TRANSFER_JOURNAL_CORRUPT");
}
