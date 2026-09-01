from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import federation_identity, federation_transfer_journal


UTC = timezone.utc
NOW = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)
TRANSFER_ID = "sha256:" + ("1" * 64)
SECOND_TRANSFER_ID = "sha256:" + ("2" * 64)
OBJECT_SET_DIGEST = "sha256:" + ("3" * 64)
OTHER_OBJECT_SET_DIGEST = "sha256:" + ("4" * 64)
POLICY_ID = "policy-offsite-custody"
BACKUP_ID = "backup-20260901-002"


def _fixture(
    tmp_path: Path,
) -> tuple[
    federation_transfer_journal.FederatedTransferJournal,
    federation_transfer_journal.FederatedTransferJournal,
    dict[str, Any],
    dict[str, Any],
]:
    identity_a = federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=tmp_path / "fleet-a" / "root.bundle.json",
        passphrase=b"fleet-a-transfer-journal-root",
        now=NOW - timedelta(hours=1),
    )
    identity_b = federation_identity.create_fleet_root(
        "fleet-b",
        bundle_path=tmp_path / "fleet-b" / "root.bundle.json",
        passphrase=b"fleet-b-transfer-journal-root",
        now=NOW - timedelta(hours=1),
    )
    journal_a = federation_transfer_journal.FederatedTransferJournal(
        tmp_path / "fleet-a" / "transfers.sqlite3",
        identity_a,
    )
    journal_b = federation_transfer_journal.FederatedTransferJournal(
        tmp_path / "fleet-b" / "transfers.sqlite3",
        identity_b,
    )
    return journal_a, journal_b, identity_a, identity_b


def _persist(
    journal: federation_transfer_journal.FederatedTransferJournal,
    *,
    transfer_id: str = TRANSFER_ID,
    source_fleet_id: str = "fleet-a",
    destination_fleet_id: str = "fleet-b",
    policy_id: str = POLICY_ID,
    backup_id: str = BACKUP_ID,
    object_set_digest: str = OBJECT_SET_DIGEST,
    now: datetime = NOW,
) -> dict[str, Any]:
    return journal.persist_proposed_transfer(
        transfer_id=transfer_id,
        source_fleet_id=source_fleet_id,
        destination_fleet_id=destination_fleet_id,
        policy_id=policy_id,
        backup_id=backup_id,
        object_set_digest=object_set_digest,
        now=now,
    )


def test_sender_and_receiver_journals_are_sovereign_durable_and_idempotent(tmp_path: Path) -> None:
    journal_a, journal_b, _, identity_b = _fixture(tmp_path)
    sender = _persist(journal_a)
    receiver = _persist(journal_b)

    assert sender["schema"] == receiver["schema"] == "federated-transfer-journal-record-v1"
    assert sender["role"] == "SENDER"
    assert receiver["role"] == "RECEIVER"
    assert sender["localFleetId"] == "fleet-a"
    assert receiver["localFleetId"] == "fleet-b"
    assert sender["transferId"] == receiver["transferId"] == TRANSFER_ID
    assert sender["identityDigest"] == receiver["identityDigest"]
    assert sender["state"] == receiver["state"] == "PROPOSED"
    assert sender["revision"] == receiver["revision"] == 1
    assert _persist(journal_a, now=NOW + timedelta(seconds=1)) == sender
    assert len(journal_a.list_transfer_events(TRANSFER_ID)) == 1

    reopened_b = federation_transfer_journal.FederatedTransferJournal(journal_b.db_path, identity_b)
    assert reopened_b.get_transfer(TRANSFER_ID) == receiver
    assert reopened_b.list_transfers() == [receiver]


def test_transfer_journal_rejects_identity_rebinding_and_wrong_local_fleet(tmp_path: Path) -> None:
    journal_a, _, _, _ = _fixture(tmp_path)
    original = _persist(journal_a)
    conflicts: tuple[dict[str, Any], ...] = (
        {"object_set_digest": OTHER_OBJECT_SET_DIGEST},
        {"policy_id": "policy-other"},
        {"backup_id": "backup-other"},
        {"source_fleet_id": "fleet-c"},
        {"destination_fleet_id": "fleet-c"},
    )
    for changes in conflicts:
        with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as conflict:
            _persist(journal_a, **changes)
        assert conflict.value.code == "FEDERATION_TRANSFER_IDENTITY_CONFLICT"
    assert journal_a.get_transfer(TRANSFER_ID) == original

    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as unrelated:
        _persist(journal_a, transfer_id=SECOND_TRANSFER_ID, source_fleet_id="fleet-b", destination_fleet_id="fleet-c")
    assert unrelated.value.code == "FEDERATION_TRANSFER_LOCAL_FLEET_NOT_PARTY"
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as reflection:
        _persist(journal_a, transfer_id=SECOND_TRANSFER_ID, destination_fleet_id="fleet-a")
    assert reflection.value.code == "FEDERATION_TRANSFER_REFLECTION_REJECTED"


def test_transfer_state_machine_is_monotonic_cas_and_retry_idempotent(tmp_path: Path) -> None:
    journal_a, _, _, _ = _fixture(tmp_path)
    record = _persist(journal_a)
    details_by_state: dict[str, dict[str, Any]] = {
        "GRANT_REQUESTED": {"requestDigest": "sha256:" + ("5" * 64)},
        "GRANT_VERIFIED": {"grantId": "grant-" + ("6" * 32), "grantDigest": "sha256:" + ("7" * 64)},
        "TRANSFERRING": {"bytesTransferred": 4096},
        "REMOTE_VERIFYING": {"objectCount": 7},
        "REMOTE_COMMITTED": {
            "remoteReceiptDigest": "sha256:" + ("8" * 64),
            "remoteCommitDigest": "sha256:" + ("9" * 64),
        },
        "LOCAL_RECORDED": {"attestationDigest": "sha256:" + ("a" * 64)},
        "SUCCEEDED": {"outcome": "FEDERATED_COMMITTED"},
    }
    for state in federation_transfer_journal.TRANSFER_STATES[1:]:
        previous_revision = int(record["revision"])
        details = details_by_state[state]
        record = journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=previous_revision,
            next_state=state,
            details=details,
            now=NOW + timedelta(seconds=previous_revision),
        )
        assert record["state"] == state
        assert record["revision"] == previous_revision + 1
        assert record["stateDetails"] == details
        assert journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=previous_revision,
            next_state=state,
            details=details,
            now=NOW + timedelta(seconds=previous_revision + 1),
        ) == record

    assert len(journal_a.list_transfer_events(TRANSFER_ID)) == len(federation_transfer_journal.TRANSFER_STATES)
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as terminal_conflict:
        journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=int(record["revision"]),
            next_state="SUCCEEDED",
            details={"outcome": "DIFFERENT"},
            now=NOW + timedelta(minutes=1),
        )
    assert terminal_conflict.value.code == "FEDERATION_TRANSFER_STATE_CONFLICT"


def test_transfer_state_machine_rejects_skip_stale_revision_and_sensitive_payload(tmp_path: Path) -> None:
    journal_a, _, _, _ = _fixture(tmp_path)
    record = _persist(journal_a)
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as skipped:
        journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=1,
            next_state="TRANSFERRING",
            details={},
            now=NOW + timedelta(seconds=1),
        )
    assert skipped.value.code == "FEDERATION_TRANSFER_STATE_TRANSITION_INVALID"
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as stale:
        journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=0,
            next_state="GRANT_REQUESTED",
            details={},
            now=NOW + timedelta(seconds=1),
        )
    assert stale.value.code == "FEDERATION_TRANSFER_REVISION_CONFLICT"
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as timestamp_regression:
        journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=1,
            next_state="GRANT_REQUESTED",
            details={},
            now=NOW - timedelta(seconds=1),
        )
    assert timestamp_regression.value.code == "FEDERATION_TRANSFER_TIMESTAMP_REGRESSION"
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as sensitive:
        journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=int(record["revision"]),
            next_state="GRANT_REQUESTED",
            details={"nested": {"receiverAccessKey": "must-not-be-journaled"}},
            now=NOW + timedelta(seconds=1),
        )
    assert sensitive.value.code == "FEDERATION_TRANSFER_SENSITIVE_STATE_REJECTED"
    assert journal_a.get_transfer(TRANSFER_ID) == record


def test_transfer_transition_races_converge_or_conflict_without_duplicate_events(tmp_path: Path) -> None:
    journal_a, _, _, _ = _fixture(tmp_path)
    _persist(journal_a)

    same_barrier = threading.Barrier(2)

    def advance_same() -> dict[str, Any] | str:
        same_barrier.wait(timeout=10)
        try:
            return journal_a.advance_transfer(
                TRANSFER_ID,
                expected_revision=1,
                next_state="GRANT_REQUESTED",
                details={"requestDigest": "sha256:" + ("5" * 64)},
                now=NOW + timedelta(seconds=1),
            )
        except federation_transfer_journal.FederatedTransferJournalError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        same_results = list(executor.map(lambda _: advance_same(), range(2)))
    assert all(isinstance(result, dict) for result in same_results)
    assert same_results[0] == same_results[1]
    assert len(journal_a.list_transfer_events(TRANSFER_ID)) == 2

    conflict_barrier = threading.Barrier(2)

    def advance_conflicting(index: int) -> dict[str, Any] | str:
        conflict_barrier.wait(timeout=10)
        try:
            return journal_a.advance_transfer(
                TRANSFER_ID,
                expected_revision=2,
                next_state="GRANT_VERIFIED",
                details={"grantDigest": "sha256:" + (str(index + 6) * 64)},
                now=NOW + timedelta(seconds=2),
            )
        except federation_transfer_journal.FederatedTransferJournalError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        conflict_results = list(executor.map(advance_conflicting, range(2)))
    assert sum(isinstance(result, dict) for result in conflict_results) == 1
    assert conflict_results.count("FEDERATION_TRANSFER_STATE_CONFLICT") == 1
    assert len(journal_a.list_transfer_events(TRANSFER_ID)) == 3


def test_transfer_journal_binding_and_input_validation_fail_closed(tmp_path: Path) -> None:
    journal_a, _, _, identity_b = _fixture(tmp_path)
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as wrong_identity:
        federation_transfer_journal.FederatedTransferJournal(journal_a.db_path, identity_b)
    assert wrong_identity.value.code == "FEDERATION_TRANSFER_JOURNAL_IDENTITY_CONFLICT"
    assert journal_a.get_transfer(TRANSFER_ID) is None

    invalid_cases = (
        ({"transfer_id": "bad"}, "FEDERATION_TRANSFER_ID_INVALID"),
        ({"source_fleet_id": "Fleet A"}, "FEDERATION_TRANSFER_FLEET_ID_INVALID"),
        ({"policy_id": "bad/policy"}, "FEDERATION_TRANSFER_POLICY_ID_INVALID"),
        ({"backup_id": "bad/backup"}, "FEDERATION_TRANSFER_BACKUP_ID_INVALID"),
        ({"object_set_digest": "bad"}, "FEDERATION_TRANSFER_OBJECT_SET_DIGEST_INVALID"),
        ({"now": datetime(2026, 9, 1, 7, 0)}, "FEDERATION_TRANSFER_TIMESTAMP_INVALID"),
    )
    for changes, expected_code in invalid_cases:
        with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as invalid:
            _persist(journal_a, **changes)
        assert invalid.value.code == expected_code

    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as missing:
        journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=1,
            next_state="GRANT_REQUESTED",
            details={},
            now=NOW,
        )
    assert missing.value.code == "FEDERATION_TRANSFER_NOT_FOUND"
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as invalid_state:
        journal_a.advance_transfer(
            SECOND_TRANSFER_ID,
            expected_revision=1,
            next_state="UNKNOWN",
            details={},
            now=NOW,
        )
    assert invalid_state.value.code == "FEDERATION_TRANSFER_STATE_INVALID"


def test_transfer_journal_defensive_payload_and_factory_paths(tmp_path: Path) -> None:
    journal_a, _, identity_a, _ = _fixture(tmp_path)
    record = _persist(journal_a)
    assert journal_a.local_identity == identity_a
    assert federation_transfer_journal.open_federated_transfer_journal(
        identity_a,
        db_path=journal_a.db_path,
    ).get_transfer(TRANSFER_ID) == record

    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as invalid_identity:
        federation_transfer_journal.FederatedTransferJournal(
            tmp_path / "invalid.sqlite3",
            {"schema": "fleet-identity-v1"},
        )
    assert invalid_identity.value.code == "FEDERATION_FLEET_ID_INVALID"
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as non_json:
        journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=1,
            next_state="GRANT_REQUESTED",
            details={"value": object()},
            now=NOW + timedelta(seconds=1),
        )
    assert non_json.value.code == "FEDERATION_TRANSFER_CANONICAL_PAYLOAD_INVALID"
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as canonical_non_json:
        federation_transfer_journal._canonical_json({"value": object()})  # noqa: SLF001
    assert canonical_non_json.value.code == "FEDERATION_TRANSFER_CANONICAL_PAYLOAD_INVALID"
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as invalid_details:
        journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=1,
            next_state="GRANT_REQUESTED",
            details=[],  # type: ignore[arg-type]
            now=NOW + timedelta(seconds=1),
        )
    assert invalid_details.value.code == "FEDERATION_TRANSFER_STATE_DETAILS_INVALID"
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as list_sensitive:
        journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=1,
            next_state="GRANT_REQUESTED",
            details={"items": [{"private_key": "forbidden"}]},  # pragma: allowlist secret
            now=NOW + timedelta(seconds=1),
        )
    assert list_sensitive.value.code == "FEDERATION_TRANSFER_SENSITIVE_STATE_REJECTED"
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as oversized:
        journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=1,
            next_state="GRANT_REQUESTED",
            details={"padding": "x" * (64 * 1024)},
            now=NOW + timedelta(seconds=1),
        )
    assert oversized.value.code == "FEDERATION_TRANSFER_STATE_DETAILS_TOO_LARGE"
    with pytest.raises(federation_transfer_journal.FederatedTransferJournalError) as invalid_revision:
        journal_a.advance_transfer(
            TRANSFER_ID,
            expected_revision=True,
            next_state="GRANT_REQUESTED",
            details={},
            now=NOW + timedelta(seconds=1),
        )
    assert invalid_revision.value.code == "FEDERATION_TRANSFER_REVISION_INVALID"
