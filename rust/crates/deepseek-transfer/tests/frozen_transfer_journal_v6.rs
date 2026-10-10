use deepseek_transfer::{
    FederatedTransferJournal, ProposedTransfer, TransferRecord, TransferRole, TransferState,
    derive_transfer_id,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::collections::BTreeMap;

#[derive(Debug, Deserialize)]
struct Fixture {
    schema_version: u32,
    source_version: String,
    source_commit: String,
    sender_identity: Value,
    receiver_identity: Value,
    proposed: ProposedTransfer,
    identity_digest: String,
    steps: Vec<Step>,
    expected_errors: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
struct Step {
    state: TransferState,
    at: String,
    details: Value,
    state_payload_digest: String,
}

fn fixture() -> Fixture {
    serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../compat/native-runtime/v6/transfer/federated_transfer_journal_vector.json"
    )))
    .unwrap()
}

fn assert_record(
    record: &TransferRecord,
    fixture: &Fixture,
    step_index: usize,
    role: TransferRole,
    local_fleet_id: &str,
) {
    let step = &fixture.steps[step_index];
    assert_eq!(record.schema, "federated-transfer-journal-record-v1");
    assert_eq!(record.transfer_id, fixture.proposed.transfer_id);
    assert_eq!(record.identity_digest, fixture.identity_digest);
    assert_eq!(record.local_fleet_id, local_fleet_id);
    assert_eq!(record.role, role);
    assert_eq!(record.source_fleet_id, fixture.proposed.source_fleet_id);
    assert_eq!(
        record.destination_fleet_id,
        fixture.proposed.destination_fleet_id
    );
    assert_eq!(record.policy_id, fixture.proposed.policy_id);
    assert_eq!(record.backup_id, fixture.proposed.backup_id);
    assert_eq!(record.object_set_digest, fixture.proposed.object_set_digest);
    assert_eq!(record.state, step.state);
    assert_eq!(record.state_details, step.details);
    assert_eq!(record.state_payload_digest, step.state_payload_digest);
    assert_eq!(record.created_at, fixture.steps[0].at);
    assert_eq!(record.updated_at, step.at);
    assert_eq!(record.revision, (step_index + 1) as u64);
}

#[test]
fn rust_replays_every_python_4_8_0_journal_state_and_event() {
    let fixture = fixture();
    assert_eq!(fixture.schema_version, 1);
    assert_eq!(fixture.source_version, "4.8.0");
    assert_eq!(
        fixture.source_commit,
        "a37735c68398fc8f795babaa269e2de6a5acd567"
    );
    assert_eq!(
        derive_transfer_id(
            &fixture.proposed.source_fleet_id,
            &fixture.proposed.destination_fleet_id,
            &fixture.proposed.backup_id,
            &fixture.proposed.object_set_digest,
        )
        .unwrap(),
        fixture.proposed.transfer_id
    );

    let temp = tempfile::tempdir().unwrap();
    let sender = FederatedTransferJournal::open(
        temp.path().join("sender.sqlite3"),
        &fixture.sender_identity,
    )
    .unwrap();
    let mut record = sender
        .persist_proposed_transfer(&fixture.proposed, &fixture.steps[0].at)
        .unwrap();
    assert_record(&record, &fixture, 0, TransferRole::Sender, "fleet-a");
    for index in 1..fixture.steps.len() {
        let step = &fixture.steps[index];
        record = sender
            .advance_transfer(
                &fixture.proposed.transfer_id,
                index as u64,
                step.state,
                step.details.clone(),
                &step.at,
            )
            .unwrap();
        assert_record(&record, &fixture, index, TransferRole::Sender, "fleet-a");
    }

    let events = sender
        .list_transfer_events(&fixture.proposed.transfer_id)
        .unwrap();
    assert_eq!(events.len(), fixture.steps.len());
    for (index, event) in events.iter().enumerate() {
        let step = &fixture.steps[index];
        assert_eq!(event.schema, "federated-transfer-journal-event-v1");
        assert_eq!(event.sequence, (index + 1) as u64);
        assert_eq!(event.transfer_id, fixture.proposed.transfer_id);
        assert_eq!(
            event.previous_state,
            index
                .checked_sub(1)
                .map(|previous| fixture.steps[previous].state)
        );
        assert_eq!(event.next_state, step.state);
        assert_eq!(event.state_payload_digest, step.state_payload_digest);
        assert_eq!(event.state_details, step.details);
        assert_eq!(event.occurred_at, step.at);
        assert_eq!(event.revision, (index + 1) as u64);
    }

    let receiver = FederatedTransferJournal::open(
        temp.path().join("receiver.sqlite3"),
        &fixture.receiver_identity,
    )
    .unwrap();
    let receiver_record = receiver
        .persist_proposed_transfer(&fixture.proposed, &fixture.steps[0].at)
        .unwrap();
    assert_record(
        &receiver_record,
        &fixture,
        0,
        TransferRole::Receiver,
        "fleet-b",
    );
}

#[test]
fn rust_replays_the_frozen_fail_closed_errors() {
    let fixture = fixture();
    let temp = tempfile::tempdir().unwrap();
    let path = temp.path().join("sender.sqlite3");
    let sender = FederatedTransferJournal::open(&path, &fixture.sender_identity).unwrap();
    sender
        .persist_proposed_transfer(&fixture.proposed, &fixture.steps[0].at)
        .unwrap();

    let conflict = FederatedTransferJournal::open(&path, &fixture.receiver_identity).unwrap_err();
    assert_eq!(
        conflict.code(),
        fixture.expected_errors["identity_conflict"]
    );
    let skipped = sender
        .advance_transfer(
            &fixture.proposed.transfer_id,
            1,
            TransferState::Transferring,
            json!({}),
            "2026-09-01T07:00:01Z",
        )
        .unwrap_err();
    assert_eq!(
        skipped.code(),
        fixture.expected_errors["transition_invalid"]
    );
    let stale = sender
        .advance_transfer(
            &fixture.proposed.transfer_id,
            0,
            TransferState::GrantRequested,
            json!({}),
            "2026-09-01T07:00:01Z",
        )
        .unwrap_err();
    assert_eq!(stale.code(), fixture.expected_errors["revision_conflict"]);
    let sensitive = sender
        .advance_transfer(
            &fixture.proposed.transfer_id,
            1,
            TransferState::GrantRequested,
            json!({"receiverAccessKey": "must-not-be-journaled"}),
            "2026-09-01T07:00:01Z",
        )
        .unwrap_err();
    assert_eq!(sensitive.code(), fixture.expected_errors["sensitive_state"]);

    sender
        .advance_transfer(
            &fixture.proposed.transfer_id,
            1,
            TransferState::GrantRequested,
            fixture.steps[1].details.clone(),
            &fixture.steps[1].at,
        )
        .unwrap();
    let state_conflict = sender
        .advance_transfer(
            &fixture.proposed.transfer_id,
            1,
            TransferState::GrantRequested,
            json!({"requestDigest": format!("sha256:{}", "0".repeat(64))}),
            "2026-09-01T07:00:02Z",
        )
        .unwrap_err();
    assert_eq!(
        state_conflict.code(),
        fixture.expected_errors["state_conflict"]
    );
}
