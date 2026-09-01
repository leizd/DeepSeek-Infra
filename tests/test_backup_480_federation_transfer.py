from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import federation_identity, federation_transfer, federation_transfer_journal


UTC = timezone.utc
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
SOURCE_FLEET_ID = "fleet-a"
DESTINATION_FLEET_ID = "fleet-b"
POLICY_ID = "policy-offsite-custody"
BACKUP_ID = "backup-20260901-002"
OBJECT_SET_DIGEST = "sha256:" + ("3" * 64)
OTHER_OBJECT_SET_DIGEST = "sha256:" + ("4" * 64)
EXPECTED_TRANSFER_ID = "sha256:6d5c985826b41ed205f38e3dccd43cc7afe6bb09bbb9f7fe98c2790d3d27e6fb"


def _fixture(
    tmp_path: Path,
) -> tuple[
    federation_transfer_journal.FederatedTransferJournal,
    federation_transfer_journal.FederatedTransferJournal,
    dict[str, Any],
    dict[str, Any],
]:
    identity_a = federation_identity.create_fleet_root(
        SOURCE_FLEET_ID,
        bundle_path=tmp_path / SOURCE_FLEET_ID / "root.bundle.json",
        passphrase=b"fleet-a-transfer-identity-root",
        now=NOW - timedelta(hours=1),
    )
    identity_b = federation_identity.create_fleet_root(
        DESTINATION_FLEET_ID,
        bundle_path=tmp_path / DESTINATION_FLEET_ID / "root.bundle.json",
        passphrase=b"fleet-b-transfer-identity-root",
        now=NOW - timedelta(hours=1),
    )
    journal_a = federation_transfer_journal.FederatedTransferJournal(
        tmp_path / SOURCE_FLEET_ID / "transfers.sqlite3",
        identity_a,
    )
    journal_b = federation_transfer_journal.FederatedTransferJournal(
        tmp_path / DESTINATION_FLEET_ID / "transfers.sqlite3",
        identity_b,
    )
    return journal_a, journal_b, identity_a, identity_b


def _derive(
    *,
    source_fleet_id: str = SOURCE_FLEET_ID,
    destination_fleet_id: str = DESTINATION_FLEET_ID,
    backup_id: str = BACKUP_ID,
    object_set_digest: str = OBJECT_SET_DIGEST,
) -> str:
    return federation_transfer.derive_transfer_id(
        source_fleet_id=source_fleet_id,
        destination_fleet_id=destination_fleet_id,
        backup_id=backup_id,
        object_set_digest=object_set_digest,
    )


def _propose(
    journal: federation_transfer_journal.FederatedTransferJournal,
    *,
    policy_id: str = POLICY_ID,
    object_set_digest: str = OBJECT_SET_DIGEST,
) -> dict[str, Any]:
    return federation_transfer.propose_transfer(
        journal=journal,
        source_fleet_id=SOURCE_FLEET_ID,
        destination_fleet_id=DESTINATION_FLEET_ID,
        policy_id=policy_id,
        backup_id=BACKUP_ID,
        object_set_digest=object_set_digest,
        now=NOW,
    )


def _accept(
    journal: federation_transfer_journal.FederatedTransferJournal,
    *,
    transfer_id: str = EXPECTED_TRANSFER_ID,
    policy_id: str = POLICY_ID,
    object_set_digest: str = OBJECT_SET_DIGEST,
) -> dict[str, Any]:
    return federation_transfer.accept_or_resume_transfer(
        journal=journal,
        transfer_id=transfer_id,
        source_fleet_id=SOURCE_FLEET_ID,
        destination_fleet_id=DESTINATION_FLEET_ID,
        policy_id=policy_id,
        backup_id=BACKUP_ID,
        object_set_digest=object_set_digest,
        now=NOW,
    )


def _reconcile(
    journal: federation_transfer_journal.FederatedTransferJournal,
    *,
    transfer_id: str = EXPECTED_TRANSFER_ID,
    policy_id: str = POLICY_ID,
    object_set_digest: str = OBJECT_SET_DIGEST,
    now: datetime = NOW + timedelta(seconds=1),
) -> dict[str, Any]:
    return federation_transfer.reconcile_transfer(
        journal=journal,
        transfer_id=transfer_id,
        source_fleet_id=SOURCE_FLEET_ID,
        destination_fleet_id=DESTINATION_FLEET_ID,
        policy_id=policy_id,
        backup_id=BACKUP_ID,
        object_set_digest=object_set_digest,
        now=now,
    )


def test_transfer_id_is_domain_separated_canonical_and_stable() -> None:
    assert _derive() == EXPECTED_TRANSFER_ID
    assert _derive() == _derive()
    assert _derive(destination_fleet_id="fleet-c") != EXPECTED_TRANSFER_ID
    assert _derive(backup_id="backup-other") != EXPECTED_TRANSFER_ID
    assert _derive(object_set_digest=OTHER_OBJECT_SET_DIGEST) != EXPECTED_TRANSFER_ID
    identity = federation_transfer.transfer_identity_document(
        source_fleet_id=SOURCE_FLEET_ID,
        destination_fleet_id=DESTINATION_FLEET_ID,
        backup_id=BACKUP_ID,
        object_set_digest=OBJECT_SET_DIGEST,
    )
    assert identity == {
        "schema": "federated-transfer-identity-v1",
        "sourceFleetId": SOURCE_FLEET_ID,
        "destinationFleetId": DESTINATION_FLEET_ID,
        "backupId": BACKUP_ID,
        "objectSetDigest": OBJECT_SET_DIGEST,
    }
    assert "policyId" not in identity


def test_sender_proposes_and_receiver_accepts_same_immutable_transfer(tmp_path: Path) -> None:
    journal_a, journal_b, _, _ = _fixture(tmp_path)
    sender = _propose(journal_a)
    receiver = _accept(journal_b)
    assert sender["transferId"] == receiver["transferId"] == EXPECTED_TRANSFER_ID
    assert sender["identityDigest"] == receiver["identityDigest"]
    assert sender["role"] == "SENDER"
    assert receiver["role"] == "RECEIVER"
    assert _accept(journal_b) == receiver
    assert len(journal_b.list_transfer_events(EXPECTED_TRANSFER_ID)) == 1


def test_same_transfer_id_with_different_content_or_policy_fails_closed(tmp_path: Path) -> None:
    _, journal_b, _, _ = _fixture(tmp_path)
    original = _accept(journal_b)
    with pytest.raises(federation_transfer.FederatedTransferError) as content_conflict:
        _accept(journal_b, object_set_digest=OTHER_OBJECT_SET_DIGEST)
    assert content_conflict.value.code == "FEDERATION_TRANSFER_IDENTITY_CONFLICT"
    with pytest.raises(federation_transfer.FederatedTransferError) as policy_conflict:
        _accept(journal_b, policy_id="policy-other")
    assert policy_conflict.value.code == "FEDERATION_TRANSFER_IDENTITY_CONFLICT"
    assert journal_b.get_transfer(EXPECTED_TRANSFER_ID) == original

    unbound_id = "sha256:" + ("f" * 64)
    with pytest.raises(federation_transfer.FederatedTransferError) as invalid_identity:
        _accept(journal_b, transfer_id=unbound_id)
    assert invalid_identity.value.code == "FEDERATION_TRANSFER_ID_INVALID"


def test_reconcile_unknown_resume_and_succeeded_never_mutates(tmp_path: Path) -> None:
    _, journal_b, _, _ = _fixture(tmp_path)
    missing = _reconcile(journal_b)
    assert missing["schema"] == "federated-transfer-reconciliation-v1"
    assert missing["status"] == "NOT_FOUND"
    assert missing["state"] is None
    assert journal_b.list_transfers() == []

    accepted = _accept(journal_b)
    resumed = _reconcile(journal_b)
    assert resumed["status"] == "RESUME"
    assert resumed["state"] == "PROPOSED"
    assert resumed["revision"] == accepted["revision"]
    assert resumed["recordDigest"].startswith("sha256:")
    assert len(journal_b.list_transfer_events(EXPECTED_TRANSFER_ID)) == 1

    record = accepted
    for state in federation_transfer_journal.TRANSFER_STATES[1:]:
        record = journal_b.advance_transfer(
            EXPECTED_TRANSFER_ID,
            expected_revision=int(record["revision"]),
            next_state=state,
            details={"step": state},
            now=NOW + timedelta(seconds=int(record["revision"])),
        )
    succeeded = _reconcile(journal_b, now=NOW + timedelta(minutes=1))
    assert succeeded["status"] == "SUCCEEDED"
    assert succeeded["state"] == "SUCCEEDED"
    assert succeeded["revision"] == len(federation_transfer_journal.TRANSFER_STATES)


def test_unknown_remote_result_reopens_and_reconciles_before_retry(tmp_path: Path) -> None:
    _, journal_b, _, identity_b = _fixture(tmp_path)
    committed_response_lost = _accept(journal_b)
    restarted_b = federation_transfer_journal.FederatedTransferJournal(journal_b.db_path, identity_b)

    reconciled = _reconcile(restarted_b)
    assert reconciled["status"] == "RESUME"
    assert reconciled["state"] == committed_response_lost["state"]
    assert _accept(restarted_b) == committed_response_lost
    assert len(restarted_b.list_transfer_events(EXPECTED_TRANSFER_ID)) == 1


def test_concurrent_receiver_accepts_converge_on_one_proposal(tmp_path: Path) -> None:
    _, journal_b, _, _ = _fixture(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: _accept(journal_b), range(4)))
    assert all(result == results[0] for result in results)
    assert journal_b.list_transfers() == [results[0]]
    assert len(journal_b.list_transfer_events(EXPECTED_TRANSFER_ID)) == 1


def test_reconcile_rejects_conflicting_binding_and_invalid_inputs(tmp_path: Path) -> None:
    _, journal_b, _, _ = _fixture(tmp_path)
    _accept(journal_b)
    with pytest.raises(federation_transfer.FederatedTransferError) as conflict:
        _reconcile(journal_b, object_set_digest=OTHER_OBJECT_SET_DIGEST)
    assert conflict.value.code == "FEDERATION_TRANSFER_IDENTITY_CONFLICT"
    with pytest.raises(federation_transfer.FederatedTransferError) as invalid_time:
        _reconcile(journal_b, now=datetime(2026, 9, 1, 8, 0))
    assert invalid_time.value.code == "FEDERATION_TRANSFER_TIMESTAMP_INVALID"

    invalid_derivations = (
        ({"source_fleet_id": "Fleet A"}, "FEDERATION_TRANSFER_FLEET_ID_INVALID"),
        ({"destination_fleet_id": SOURCE_FLEET_ID}, "FEDERATION_TRANSFER_REFLECTION_REJECTED"),
        ({"backup_id": "bad/backup"}, "FEDERATION_TRANSFER_BACKUP_ID_INVALID"),
        ({"object_set_digest": "bad"}, "FEDERATION_TRANSFER_OBJECT_SET_DIGEST_INVALID"),
    )
    for changes, expected_code in invalid_derivations:
        with pytest.raises(federation_transfer.FederatedTransferError) as invalid:
            _derive(**changes)
        assert invalid.value.code == expected_code


def test_transfer_api_wraps_journal_failures_and_rejects_corrupt_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal_a, journal_b, _, _ = _fixture(tmp_path)
    with pytest.raises(federation_transfer.FederatedTransferError) as invalid_transfer:
        _accept(journal_b, transfer_id="bad")
    assert invalid_transfer.value.code == "FEDERATION_TRANSFER_ID_INVALID"
    with pytest.raises(federation_transfer.FederatedTransferError) as invalid_policy:
        _accept(journal_b, policy_id="bad/policy")
    assert invalid_policy.value.code == "FEDERATION_TRANSFER_POLICY_ID_INVALID"
    with pytest.raises(federation_transfer.FederatedTransferError) as identity_document_error:
        federation_transfer.transfer_identity_document(
            source_fleet_id="Fleet A",
            destination_fleet_id=DESTINATION_FLEET_ID,
            backup_id=BACKUP_ID,
            object_set_digest=OBJECT_SET_DIGEST,
        )
    assert identity_document_error.value.code == "FEDERATION_TRANSFER_FLEET_ID_INVALID"
    with pytest.raises(federation_transfer.FederatedTransferError) as canonical_error:
        federation_transfer._canonical_json({"value": object()})  # noqa: SLF001
    assert canonical_error.value.code == "FEDERATION_TRANSFER_CANONICAL_PAYLOAD_INVALID"

    with pytest.raises(federation_transfer.FederatedTransferError) as proposed_at_invalid_time:
        federation_transfer.propose_transfer(
            journal=journal_a,
            source_fleet_id=SOURCE_FLEET_ID,
            destination_fleet_id=DESTINATION_FLEET_ID,
            policy_id=POLICY_ID,
            backup_id=BACKUP_ID,
            object_set_digest=OBJECT_SET_DIGEST,
            now=datetime(2026, 9, 1, 8, 0),
        )
    assert proposed_at_invalid_time.value.code == "FEDERATION_TRANSFER_TIMESTAMP_INVALID"

    journal_failure = federation_transfer_journal.FederatedTransferJournalError("FEDERATION_TRANSFER_JOURNAL_UNAVAILABLE")
    monkeypatch.setattr(journal_b, "get_transfer", lambda _transfer_id: (_ for _ in ()).throw(journal_failure))
    with pytest.raises(federation_transfer.FederatedTransferError) as accept_query_failure:
        _accept(journal_b)
    assert accept_query_failure.value.code == "FEDERATION_TRANSFER_JOURNAL_UNAVAILABLE"
    with pytest.raises(federation_transfer.FederatedTransferError) as reconcile_query_failure:
        _reconcile(journal_b)
    assert reconcile_query_failure.value.code == "FEDERATION_TRANSFER_JOURNAL_UNAVAILABLE"

    monkeypatch.setattr(journal_b, "get_transfer", lambda _transfer_id: None)
    monkeypatch.setattr(
        journal_b,
        "persist_proposed_transfer",
        lambda **_kwargs: (_ for _ in ()).throw(journal_failure),
    )
    with pytest.raises(federation_transfer.FederatedTransferError) as accept_persist_failure:
        _accept(journal_b)
    assert accept_persist_failure.value.code == "FEDERATION_TRANSFER_JOURNAL_UNAVAILABLE"

    corrupt_id = "sha256:" + ("f" * 64)
    corrupt_record = {
        **_propose(journal_a),
        "transferId": corrupt_id,
    }
    monkeypatch.setattr(journal_b, "get_transfer", lambda _transfer_id: corrupt_record)
    with pytest.raises(federation_transfer.FederatedTransferError) as accept_corrupt:
        _accept(journal_b, transfer_id=corrupt_id)
    assert accept_corrupt.value.code == "FEDERATION_TRANSFER_IDENTITY_CONFLICT"
    with pytest.raises(federation_transfer.FederatedTransferError) as reconcile_corrupt:
        _reconcile(journal_b, transfer_id=corrupt_id)
    assert reconcile_corrupt.value.code == "FEDERATION_TRANSFER_IDENTITY_CONFLICT"


def test_reconcile_rejects_unbound_identity_before_any_remote_write(tmp_path: Path) -> None:
    _, journal_b, _, _ = _fixture(tmp_path)
    with pytest.raises(federation_transfer.FederatedTransferError) as unbound:
        _reconcile(journal_b, transfer_id="sha256:" + ("f" * 64))
    assert unbound.value.code == "FEDERATION_TRANSFER_ID_INVALID"
