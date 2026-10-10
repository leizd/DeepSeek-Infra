from __future__ import annotations

import copy
import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_dr_readiness,
    backup_policies,
    backup_replication,
    backup_targets,
    backup_write_continuity,
    federated_durability,
    federation_transfer_journal,
)
from tests.test_backup_480_federated_replica_attestation import _attestation_fixture, _verify
from tests.test_backup_480_federated_replica_commit import TARGET_ID
from tests.test_backup_480_federated_replica_receiver import NOW


def _verified_fixture(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    fixture = _attestation_fixture(tmp_settings, monkeypatch)
    _verify(fixture)
    return fixture


def _ledger(tmp_settings: Path, fixture: dict[str, Any]) -> federated_durability.FederatedDurabilityLedger:
    return federated_durability.FederatedDurabilityLedger(
        tmp_settings / "fleet-a" / "federated-durability.sqlite3",
        fixture["identityA"],
    )


def _policy(*, max_age: int = 3600, allowed_fleets: list[str] | None = None, allowed_jurisdictions: list[str] | None = None) -> dict[str, Any]:
    return backup_policies.normalize_policy(
        {
            "name": "federated durability policy",
            "replication": {
                "enabled": True,
                "targets": [{"targetId": "target_local_b", "mode": "required"}],
                "minCommittedCopies": 2,
                "minFailureDomains": 2,
            },
            "federatedDurability": {
                "enabled": True,
                "minFederatedCopies": 1,
                "minDistinctFleets": 1,
                "maxFederatedCopyAge": max_age,
                "allowedPeerFleets": ["fleet-b"] if allowed_fleets is None else allowed_fleets,
                "allowedJurisdictions": ["CN"] if allowed_jurisdictions is None else allowed_jurisdictions,
            },
        },
        policy_id="policy-federated-replica",
    )


def _record(
    fixture: dict[str, Any],
    ledger: federated_durability.FederatedDurabilityLedger,
    *,
    now_offset: int = 13,
) -> dict[str, Any]:
    return federated_durability.record_verified_federated_copy(
        ledger=ledger,
        peer_registry=fixture["registryA"],
        sender_journal=fixture["senderJournal"],
        transfer_id=fixture["transferId"],
        now=NOW + timedelta(seconds=now_offset),
    )


def _evidence(fixture: dict[str, Any]) -> dict[str, Any]:
    transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
    accepted = fixture["registryA"].get_replica_attestation("fleet-b", fixture["transferId"])
    peer = fixture["registryA"].get_peer("fleet-b")
    assert transfer is not None and accepted is not None and peer is not None
    return federated_durability._copy_evidence(
        transfer,
        accepted,
        accepted["attestation"],
        peer["pinnedMetadata"],
    )


def _assert_durability_error(call: Any, code: str) -> None:
    with pytest.raises(federated_durability.FederatedDurabilityError) as rejected:
        call()
    assert rejected.value.code == code


def test_federated_durability_policy_is_separate_and_fail_closed() -> None:
    policy = _policy()

    assert policy["replication"]["minCommittedCopies"] == 2
    assert policy["replication"]["minFailureDomains"] == 2
    assert policy["federatedDurability"] == {
        "enabled": True,
        "minFederatedCopies": 1,
        "minDistinctFleets": 1,
        "maxFederatedCopyAge": 3600,
        "allowedPeerFleets": ["fleet-b"],
        "allowedJurisdictions": ["CN"],
    }

    invalid = (
        "bad",
        {"enabled": True},
        {
            "enabled": True,
            "minFederatedCopies": 0,
            "minDistinctFleets": 1,
            "maxFederatedCopyAge": 1,
            "allowedPeerFleets": ["fleet-b"],
            "allowedJurisdictions": ["CN"],
        },
        {
            "enabled": True,
            "minFederatedCopies": 1,
            "minDistinctFleets": 2,
            "maxFederatedCopyAge": 1,
            "allowedPeerFleets": ["fleet-b"],
            "allowedJurisdictions": ["CN"],
        },
        {
            "enabled": True,
            "minFederatedCopies": 1,
            "minDistinctFleets": 1,
            "maxFederatedCopyAge": 1,
            "allowedPeerFleets": ["Fleet Bad"],
            "allowedJurisdictions": ["CN"],
        },
        {
            "enabled": True,
            "minFederatedCopies": 1,
            "minDistinctFleets": 1,
            "maxFederatedCopyAge": 1,
            "allowedPeerFleets": ["fleet-b"],
            "allowedJurisdictions": [],
        },
        {
            "enabled": True,
            "minFederatedCopies": 2,
            "minDistinctFleets": 1,
            "maxFederatedCopyAge": 1,
            "allowedPeerFleets": ["fleet-b"],
            "allowedJurisdictions": ["CN"],
        },
    )
    for value in invalid:
        with pytest.raises(AppError):
            backup_policies._normalize_federated_durability(value)


def test_federated_durability_policy_round_trips_without_mutating_local_objectives(
    tmp_settings: Path,
) -> None:
    created = backup_policies.create_policy(
        {
            "name": "federation policy round trip",
            "replication": {
                "enabled": False,
                "minCommittedCopies": 3,
                "minFailureDomains": 2,
            },
        }
    )
    local_before = copy.deepcopy(created["replication"])
    updated = backup_policies.update_policy(
        created["policyId"],
        {
            "federatedDurability": {
                "enabled": True,
                "minFederatedCopies": 2,
                "minDistinctFleets": 2,
                "maxFederatedCopyAge": 86400,
                "allowedPeerFleets": ["fleet-c", "fleet-b"],
                "allowedJurisdictions": ["SG", "CN"],
            }
        },
    )

    assert updated["replication"] == local_before
    assert updated["federatedDurability"]["allowedPeerFleets"] == ["fleet-b", "fleet-c"]
    assert updated["federatedDurability"]["allowedJurisdictions"] == ["CN", "SG"]
    assert backup_policies.get_policy(created["policyId"])["federatedDurability"] == updated["federatedDurability"]


def test_verified_copy_is_recorded_once_then_completes_sender_state(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    ledger = _ledger(tmp_settings, fixture)

    record = _record(fixture, ledger)

    assert set(record) == federated_durability.FEDERATED_COPY_RECORD_FIELDS
    assert record["status"] == federated_durability.FEDERATED_COMMITTED
    assert record["transferId"] == fixture["transferId"]
    assert record["sourceFleetId"] == "fleet-a"
    assert record["destinationFleetId"] == "fleet-b"
    assert record["policyId"] == fixture["receipt"]["policyId"]
    assert record["backupId"] == fixture["receipt"]["backupId"]
    assert record["objectSetDigest"] == fixture["federationDigest"]
    assert record["attestationDigest"] == fixture["senderJournal"].list_transfer_events(fixture["transferId"])[-3]["stateDetails"]["attestationDigest"]
    assert record["peerMetadata"]["jurisdiction"] == "CN"
    assert record["localDurabilityCredit"] is False

    transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
    assert transfer is not None and transfer["state"] == federation_transfer_journal.STATE_SUCCEEDED
    assert transfer["stateDetails"] == {
        "outcome": federated_durability.FEDERATED_COMMITTED,
        "federatedCopyRecordDigest": record["recordDigest"],
    }
    assert _record(fixture, ledger, now_offset=14) == record
    assert ledger.list_copies() == [record]
    assert len(fixture["senderJournal"].list_transfer_events(fixture["transferId"])) == 8


def test_ledger_first_crash_window_resumes_without_duplicate_copy(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    ledger = _ledger(tmp_settings, fixture)
    real_advance = fixture["senderJournal"].advance_transfer
    attempts = 0

    def fail_once(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise federation_transfer_journal.FederatedTransferJournalError("INJECTED_AFTER_LEDGER_COMMIT")
        return real_advance(*args, **kwargs)

    monkeypatch.setattr(fixture["senderJournal"], "advance_transfer", fail_once)
    with pytest.raises(federated_durability.FederatedDurabilityError) as interrupted:
        _record(fixture, ledger)
    assert interrupted.value.code == "INJECTED_AFTER_LEDGER_COMMIT"
    assert len(ledger.list_copies()) == 1
    assert fixture["senderJournal"].get_transfer(fixture["transferId"])["state"] == federation_transfer_journal.STATE_REMOTE_COMMITTED

    monkeypatch.setattr(fixture["senderJournal"], "advance_transfer", real_advance)
    fixture["registryA"].revoke_peer(
        "fleet-b",
        actor="operator-a",
        reason="reconcile durable pre-revocation record",
        now=NOW + timedelta(seconds=14),
    )
    resumed = _record(fixture, ledger, now_offset=15)
    assert resumed == ledger.get_copy(fixture["transferId"])
    assert len(ledger.list_copies()) == 1
    assert fixture["senderJournal"].get_transfer(fixture["transferId"])["state"] == federation_transfer_journal.STATE_SUCCEEDED


def test_objective_counts_only_current_allowed_fleet_jurisdiction_and_age(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    ledger = _ledger(tmp_settings, fixture)
    record = _record(fixture, ledger)

    healthy = federated_durability.evaluate_federated_durability(
        policy=_policy(),
        backup_id=record["backupId"],
        object_set_digest=record["objectSetDigest"],
        ledger=ledger,
        peer_registry=fixture["registryA"],
        now=NOW + timedelta(seconds=20),
    )
    assert healthy["satisfied"] is True
    assert healthy["federatedCopies"] == 1
    assert healthy["distinctFleets"] == 1
    assert healthy["distinctFailureDomains"] == 1
    assert healthy["creditedTransferIds"] == [fixture["transferId"]]

    cases = (
        (_policy(max_age=5), NOW + timedelta(seconds=20), "FEDERATED_COPY_TOO_OLD"),
        (_policy(allowed_fleets=["fleet-c"]), NOW + timedelta(seconds=20), "FEDERATED_PEER_NOT_ALLOWED"),
        (_policy(allowed_jurisdictions=["US"]), NOW + timedelta(seconds=20), "FEDERATED_JURISDICTION_NOT_ALLOWED"),
    )
    for policy, current, issue in cases:
        status = federated_durability.evaluate_federated_durability(
            policy=policy,
            backup_id=record["backupId"],
            object_set_digest=record["objectSetDigest"],
            ledger=ledger,
            peer_registry=fixture["registryA"],
            now=current,
        )
        assert status["satisfied"] is False
        assert issue in status["issues"]


def test_remote_copy_never_credits_local_durability_or_primary_promotion(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    ledger = _ledger(tmp_settings, fixture)
    policy = _policy()
    policy_before = copy.deepcopy(policy)
    source_bytes = {component.path: component.path.read_bytes() for component in fixture["package"].components}

    record = _record(fixture, ledger)
    federated = federated_durability.evaluate_federated_durability(
        policy=policy,
        backup_id=record["backupId"],
        object_set_digest=record["objectSetDigest"],
        ledger=ledger,
        peer_registry=fixture["registryA"],
        now=NOW + timedelta(seconds=20),
    )
    assert federated["satisfied"] is True
    assert policy == policy_before
    assert {path: path.read_bytes() for path in source_bytes} == source_bytes
    assert backup_dr_ledger.list_logical_recovery_copies(policy_id=record["policyId"], backup_id=record["backupId"]) == []

    monkeypatch.setattr(
        backup_dr_ledger,
        "list_logical_recovery_copies",
        lambda **_kwargs: [
            {
                "targetId": "managed-local",
                "backupId": record["backupId"],
                "objectSetDigest": record["objectSetDigest"],
                "recoverable": True,
                "state": "healthy",
            }
        ],
    )
    monkeypatch.setattr(backup_targets, "list_targets", lambda: [{"targetId": "managed-local", "failureDomain": "local-a"}])
    monkeypatch.setattr(backup_replication, "calculate_replica_lag", lambda *_args, **_kwargs: {"lagRecoveryPoints": 0, "lagSeconds": 0})
    local = backup_dr_readiness._replication_summary(
        "managed-local",
        record["policyId"],
        {"backupId": record["backupId"], "objectSetDigest": record["objectSetDigest"]},
        policy=policy,
        now=NOW + timedelta(seconds=20),
    )
    assert local["requiredCopies"] == 2
    assert local["committedCopies"] == 1
    assert local["compliance"] == "degraded"

    monkeypatch.setattr(backup_policies, "get_policy", lambda _policy_id: policy)
    monkeypatch.setattr(backup_write_continuity, "get_write_continuity_state", lambda _policy_id: {"failoverEpoch": 0})
    with pytest.raises(AppError, match="not a configured replica"):
        backup_write_continuity.promote_primary_target(record["policyId"], TARGET_ID)


def test_revoked_peer_and_unverified_sender_never_enter_durability_ledger(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unverified = _attestation_fixture(tmp_settings / "unverified", monkeypatch)
    unverified_ledger = _ledger(tmp_settings / "unverified", unverified)
    with pytest.raises(federated_durability.FederatedDurabilityError) as rejected:
        _record(unverified, unverified_ledger)
    assert rejected.value.code == "FEDERATION_TRANSFER_NOT_VERIFIED"
    assert unverified_ledger.list_copies() == []

    fixture = _verified_fixture(tmp_settings / "revoked", monkeypatch)
    ledger = _ledger(tmp_settings / "revoked", fixture)
    fixture["registryA"].revoke_peer(
        "fleet-b",
        actor="operator-a",
        reason="incident",
        now=NOW + timedelta(seconds=13),
    )
    with pytest.raises(federated_durability.FederatedDurabilityError) as revoked:
        _record(fixture, ledger, now_offset=14)
    assert revoked.value.code == "FEDERATION_PEER_REVOKED"
    assert ledger.list_copies() == []


def test_ledger_validates_identity_evidence_sequence_and_database_integrity(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    evidence = _evidence(fixture)

    _assert_durability_error(
        lambda: federated_durability.FederatedDurabilityLedger(
            tmp_settings / "invalid.sqlite3",
            {"fleetId": "not-an-identity"},
        ),
        "FEDERATION_ROOT_IDENTITY_SCHEMA_INVALID",
    )
    identity_path = tmp_settings / "identity.sqlite3"
    federated_durability.FederatedDurabilityLedger(identity_path, fixture["identityA"])
    assert federated_durability.FederatedDurabilityLedger(identity_path, fixture["identityA"]).list_copies() == []
    _assert_durability_error(
        lambda: federated_durability.FederatedDurabilityLedger(identity_path, fixture["identityB"]),
        "FEDERATED_DURABILITY_LOCAL_IDENTITY_CONFLICT",
    )

    invalid_cases: list[tuple[Any, str]] = [
        ([], "FEDERATED_DURABILITY_RECORD_INVALID"),
        ({key: value for key, value in evidence.items() if key != "backupId"}, "FEDERATED_DURABILITY_RECORD_FIELDS_INVALID"),
        ({**evidence, "schema": "wrong"}, "FEDERATED_DURABILITY_RECORD_BINDING_INVALID"),
        ({**evidence, "status": "LOCAL"}, "FEDERATED_DURABILITY_RECORD_BINDING_INVALID"),
        ({**evidence, "localDurabilityCredit": True}, "FEDERATED_DURABILITY_RECORD_BINDING_INVALID"),
        ({**evidence, "sourceFleetId": "fleet-x"}, "FEDERATED_DURABILITY_RECORD_BINDING_INVALID"),
        ({**evidence, "attestationSequence": True}, "FEDERATED_DURABILITY_ATTESTATION_SEQUENCE_INVALID"),
        ({**evidence, "attestationSequence": 0}, "FEDERATED_DURABILITY_ATTESTATION_SEQUENCE_INVALID"),
        ({**evidence, "destinationFleetId": "Fleet Bad"}, "FEDERATED_DURABILITY_FLEET_ID_INVALID"),
        ({**evidence, "policyId": "bad/id"}, "FEDERATED_DURABILITY_POLICY_ID_INVALID"),
        ({**evidence, "objectSetDigest": "bad"}, "FEDERATED_DURABILITY_OBJECT_SET_DIGEST_INVALID"),
        ({**evidence, "peerMetadata": {"provider": "only-one"}}, "FEDERATED_DURABILITY_PEER_METADATA_INVALID"),
        (
            {**evidence, "peerMetadata": {**evidence["peerMetadata"], "provider": "bad\nmetadata"}},
            "FEDERATED_DURABILITY_PEER_METADATA_INVALID",
        ),
        (
            {**evidence, "committedAt": "bad"},
            "FEDERATED_DURABILITY_TIMESTAMP_INVALID",
        ),
        (
            {**evidence, "committedAt": (NOW + timedelta(seconds=13)).isoformat(timespec="seconds").replace("+00:00", "Z")},
            "FEDERATED_DURABILITY_TIMESTAMP_ORDER_INVALID",
        ),
    ]
    for index, (candidate, code) in enumerate(invalid_cases):
        ledger = federated_durability.FederatedDurabilityLedger(
            tmp_settings / f"invalid-{index}.sqlite3",
            fixture["identityA"],
        )
        _assert_durability_error(
            lambda candidate=candidate, ledger=ledger: ledger._record_verified_copy(
                candidate,
                recorded_at=NOW + timedelta(seconds=13),
            ),
            code,
        )

    ledger = federated_durability.FederatedDurabilityLedger(
        tmp_settings / "integrity.sqlite3",
        fixture["identityA"],
    )
    first = ledger._record_verified_copy(evidence, recorded_at=NOW + timedelta(seconds=13))
    assert ledger._record_verified_copy(evidence, recorded_at=NOW + timedelta(seconds=14)) == first
    _assert_durability_error(
        lambda: ledger._record_verified_copy(
            {**evidence, "remoteTargetId": "different-target"},
            recorded_at=NOW + timedelta(seconds=14),
        ),
        "FEDERATED_DURABILITY_RECORD_IDENTITY_CONFLICT",
    )
    _assert_durability_error(
        lambda: ledger._record_verified_copy(
            {
                **evidence,
                "transferId": "sha256:" + ("f" * 64),
                "backupId": "backup-sequence-conflict",
            },
            recorded_at=NOW + timedelta(seconds=14),
        ),
        "FEDERATED_DURABILITY_ATTESTATION_SEQUENCE_CONFLICT",
    )
    with sqlite3.connect(ledger.db_path) as connection:
        connection.execute(
            "UPDATE federated_copies SET record_digest = ? WHERE transfer_id = ?",
            ("sha256:" + ("0" * 64), fixture["transferId"]),
        )
    _assert_durability_error(
        lambda: ledger.get_copy(fixture["transferId"]),
        "FEDERATED_DURABILITY_RECORD_CORRUPT",
    )


def test_durability_primitive_validators_reject_noncanonical_values() -> None:
    for call, code in (
        (lambda: federated_durability._normalize(math.nan), "FEDERATED_DURABILITY_CANONICAL_PAYLOAD_INVALID"),
        (lambda: federated_durability._canonical_json(math.inf), "FEDERATED_DURABILITY_CANONICAL_PAYLOAD_INVALID"),
        (lambda: federated_durability._utc_iso(datetime(2026, 9, 1)), "FEDERATED_DURABILITY_TIMESTAMP_INVALID"),
        (lambda: federated_durability._parse_timestamp(None), "FEDERATED_DURABILITY_TIMESTAMP_INVALID"),
        (lambda: federated_durability._parse_timestamp("2026-09-01T08:00:00"), "FEDERATED_DURABILITY_TIMESTAMP_INVALID"),
        (lambda: federated_durability._parse_timestamp("2026-09-01T08:00:00+00:00"), "FEDERATED_DURABILITY_TIMESTAMP_INVALID"),
        (
            lambda: federated_durability._metadata(
                {
                    "provider": " leading-space",
                    "region": "r1",
                    "jurisdiction": "CN",
                    "siteClass": "site",
                }
            ),
            "FEDERATED_DURABILITY_PEER_METADATA_INVALID",
        ),
    ):
        _assert_durability_error(call, code)


def test_durability_requires_both_accepted_attestation_and_exact_sender_event(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    ledger = _ledger(tmp_settings, fixture)
    transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
    assert transfer is not None
    real_get = fixture["registryA"].get_replica_attestation
    real_events = fixture["senderJournal"].list_transfer_events

    monkeypatch.setattr(fixture["registryA"], "get_replica_attestation", lambda *_args: None)
    _assert_durability_error(lambda: _record(fixture, ledger), "FEDERATED_DURABILITY_ATTESTATION_NOT_ACCEPTED")
    monkeypatch.setattr(fixture["registryA"], "get_replica_attestation", real_get)

    accepted = real_get("fleet-b", fixture["transferId"])
    assert accepted is not None
    monkeypatch.setattr(fixture["registryA"], "get_replica_attestation", lambda *_args: {**accepted, "attestation": []})
    _assert_durability_error(lambda: _record(fixture, ledger), "FEDERATED_DURABILITY_ATTESTATION_RECORD_INVALID")
    monkeypatch.setattr(fixture["registryA"], "get_replica_attestation", real_get)

    monkeypatch.setattr(
        fixture["registryA"],
        "get_replica_attestation",
        lambda *_args: {**accepted, "attestationDigest": "sha256:" + ("f" * 64)},
    )
    _assert_durability_error(lambda: _record(fixture, ledger), "FEDERATED_DURABILITY_ATTESTATION_RECORD_INVALID")
    monkeypatch.setattr(fixture["registryA"], "get_replica_attestation", real_get)

    events = real_events(fixture["transferId"])
    without_remote = [event for event in events if event["nextState"] != federation_transfer_journal.STATE_REMOTE_COMMITTED]
    monkeypatch.setattr(fixture["senderJournal"], "list_transfer_events", lambda *_args: without_remote)
    _assert_durability_error(lambda: _record(fixture, ledger), "FEDERATED_DURABILITY_REMOTE_COMMIT_EVENT_INVALID")

    conflicting = copy.deepcopy(events)
    for event in conflicting:
        if event["nextState"] == federation_transfer_journal.STATE_REMOTE_COMMITTED:
            event["stateDetails"]["remoteCommitDigest"] = "sha256:" + ("f" * 64)
    monkeypatch.setattr(fixture["senderJournal"], "list_transfer_events", lambda *_args: conflicting)
    _assert_durability_error(lambda: _record(fixture, ledger), "FEDERATED_DURABILITY_REMOTE_COMMIT_EVENT_CONFLICT")
    monkeypatch.setattr(fixture["senderJournal"], "list_transfer_events", real_events)
    assert ledger.list_copies() == []


def test_sender_completion_second_crash_and_event_conflicts_are_recoverable_or_rejected(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    ledger = _ledger(tmp_settings, fixture)
    real_advance = fixture["senderJournal"].advance_transfer
    calls = 0

    def fail_second(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise federation_transfer_journal.FederatedTransferJournalError("INJECTED_BEFORE_SUCCEEDED")
        return real_advance(*args, **kwargs)

    monkeypatch.setattr(fixture["senderJournal"], "advance_transfer", fail_second)
    _assert_durability_error(lambda: _record(fixture, ledger), "INJECTED_BEFORE_SUCCEEDED")
    assert fixture["senderJournal"].get_transfer(fixture["transferId"])["state"] == federation_transfer_journal.STATE_LOCAL_RECORDED
    monkeypatch.setattr(fixture["senderJournal"], "advance_transfer", real_advance)
    record = _record(fixture, ledger, now_offset=14)
    assert fixture["senderJournal"].get_transfer(fixture["transferId"])["state"] == federation_transfer_journal.STATE_SUCCEEDED

    real_events = fixture["senderJournal"].list_transfer_events
    events = real_events(fixture["transferId"])
    duplicate = events + [copy.deepcopy(events[-1])]
    monkeypatch.setattr(fixture["senderJournal"], "list_transfer_events", lambda *_args: duplicate)
    _assert_durability_error(
        lambda: federated_durability._event_for_state(
            fixture["senderJournal"],
            fixture["transferId"],
            federation_transfer_journal.STATE_SUCCEEDED,
        ),
        "FEDERATED_DURABILITY_SENDER_COMPLETION_CONFLICT",
    )
    monkeypatch.setattr(fixture["senderJournal"], "list_transfer_events", real_events)
    _assert_durability_error(
        lambda: federated_durability._complete_sender_journal(
            fixture["senderJournal"],
            "sha256:" + ("f" * 64),
            record,
            now=NOW + timedelta(seconds=15),
        ),
        "FEDERATION_TRANSFER_NOT_FOUND",
    )

    conflicting_events = copy.deepcopy(events)
    for event in conflicting_events:
        if event["nextState"] == federation_transfer_journal.STATE_LOCAL_RECORDED:
            event["stateDetails"]["attestationDigest"] = "sha256:" + ("0" * 64)
    monkeypatch.setattr(fixture["senderJournal"], "list_transfer_events", lambda *_args: conflicting_events)
    _assert_durability_error(
        lambda: federated_durability._complete_sender_journal(
            fixture["senderJournal"],
            fixture["transferId"],
            record,
            now=NOW + timedelta(seconds=15),
        ),
        "FEDERATED_DURABILITY_SENDER_COMPLETION_CONFLICT",
    )


def test_sender_state_cannot_reconstruct_a_missing_durability_record(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    ledger = _ledger(tmp_settings, fixture)
    transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
    assert transfer is not None
    fixture["senderJournal"].advance_transfer(
        fixture["transferId"],
        expected_revision=int(transfer["revision"]),
        next_state=federation_transfer_journal.STATE_LOCAL_RECORDED,
        details={"forged": "without-ledger"},
        now=NOW + timedelta(seconds=13),
    )

    _assert_durability_error(
        lambda: _record(fixture, ledger, now_offset=14),
        "FEDERATED_DURABILITY_LEDGER_RECORD_MISSING",
    )
    assert ledger.list_copies() == []


def test_signer_and_semantic_failures_never_create_federated_copy(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    ledger = _ledger(tmp_settings, fixture)
    signer_key_id = fixture["attestation"]["signerKeyId"]
    fixture["registryA"].revoke_online_signer(
        "fleet-b",
        signer_key_id,
        actor="operator-a",
        reason="compromised",
        revoked_at=NOW + timedelta(seconds=13),
    )
    _assert_durability_error(lambda: _record(fixture, ledger, now_offset=14), "FEDERATION_SIGNER_REVOKED")
    assert ledger.list_copies() == []

    semantic = _verified_fixture(tmp_settings / "semantic", monkeypatch)
    semantic_ledger = _ledger(tmp_settings / "semantic", semantic)
    from deepseek_infra.infra.workspace import federated_replica_attestation

    real_semantics = federated_replica_attestation._attestation_semantics

    def reject_semantics(*_args: Any, **_kwargs: Any) -> Any:
        raise federated_replica_attestation.FederatedReplicaAttestationError("INJECTED_SEMANTIC_REJECTION")

    monkeypatch.setattr(
        "deepseek_infra.infra.workspace.federated_replica_attestation._attestation_semantics",
        reject_semantics,
    )
    _assert_durability_error(lambda: _record(semantic, semantic_ledger), "INJECTED_SEMANTIC_REJECTION")
    assert semantic_ledger.list_copies() == []
    monkeypatch.setattr(federated_replica_attestation, "_attestation_semantics", real_semantics)

    trust = _verified_fixture(tmp_settings / "trust", monkeypatch)
    trust_ledger = _ledger(tmp_settings / "trust", trust)
    peer = trust["registryA"].get_peer("fleet-b")
    assert peer is not None
    monkeypatch.setattr(trust["registryA"], "require_active_peer", lambda *_args: {**peer, "fleetIdentity": None})
    _assert_durability_error(
        lambda: _record(trust, trust_ledger),
        "FEDERATED_DURABILITY_ATTESTATION_TRUST_INVALID",
    )
    assert trust_ledger.list_copies() == []


def test_objective_is_disabled_by_default_and_rechecks_current_trust(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    ledger = _ledger(tmp_settings, fixture)
    record = _record(fixture, ledger)

    disabled_policy = backup_policies.normalize_policy(
        {"name": "no federation objective"},
        policy_id=record["policyId"],
    )
    disabled = federated_durability.evaluate_federated_durability(
        policy=disabled_policy,
        backup_id=record["backupId"],
        object_set_digest=record["objectSetDigest"],
        ledger=ledger,
        peer_registry=fixture["registryA"],
        now=NOW + timedelta(days=365),
    )
    assert disabled["objectiveEnabled"] is False
    assert disabled["satisfied"] is True
    assert disabled["federatedCopies"] == 0

    future = federated_durability.evaluate_federated_durability(
        policy=_policy(),
        backup_id=record["backupId"],
        object_set_digest=record["objectSetDigest"],
        ledger=ledger,
        peer_registry=fixture["registryA"],
        now=NOW + timedelta(seconds=5),
    )
    assert "FEDERATED_COPY_FROM_FUTURE" in future["issues"]

    fixture["registryA"].suspend_peer(
        "fleet-b",
        actor="operator-a",
        reason="audit",
        now=NOW + timedelta(seconds=21),
    )
    inactive = federated_durability.evaluate_federated_durability(
        policy=_policy(),
        backup_id=record["backupId"],
        object_set_digest=record["objectSetDigest"],
        ledger=ledger,
        peer_registry=fixture["registryA"],
        now=NOW + timedelta(seconds=22),
    )
    assert inactive["satisfied"] is False
    assert "FEDERATION_PEER_NOT_ACTIVE" in inactive["issues"]


def test_objective_rechecks_pinned_metadata_and_accepted_attestation(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    ledger = _ledger(tmp_settings, fixture)
    record = _record(fixture, ledger)
    peer = fixture["registryA"].get_peer("fleet-b")
    assert peer is not None
    real_require = fixture["registryA"].require_active_peer
    real_get = fixture["registryA"].get_replica_attestation

    changed = copy.deepcopy(peer)
    changed["pinnedMetadata"]["provider"] = "different-operator-known-provider"
    monkeypatch.setattr(fixture["registryA"], "require_active_peer", lambda *_args: changed)
    mismatch = federated_durability.evaluate_federated_durability(
        policy=_policy(),
        backup_id=record["backupId"],
        object_set_digest=record["objectSetDigest"],
        ledger=ledger,
        peer_registry=fixture["registryA"],
        now=NOW + timedelta(seconds=20),
    )
    assert "FEDERATED_PEER_METADATA_MISMATCH" in mismatch["issues"]

    monkeypatch.setattr(fixture["registryA"], "require_active_peer", real_require)
    monkeypatch.setattr(fixture["registryA"], "get_replica_attestation", lambda *_args: None)
    missing = federated_durability.evaluate_federated_durability(
        policy=_policy(),
        backup_id=record["backupId"],
        object_set_digest=record["objectSetDigest"],
        ledger=ledger,
        peer_registry=fixture["registryA"],
        now=NOW + timedelta(seconds=20),
    )
    assert "FEDERATED_ATTESTATION_RECORD_MISSING" in missing["issues"]
    monkeypatch.setattr(fixture["registryA"], "get_replica_attestation", real_get)


def test_invalid_evaluation_inputs_and_sender_identity_fail_closed(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _verified_fixture(tmp_settings, monkeypatch)
    ledger = _ledger(tmp_settings, fixture)

    def evaluate(**changes: Any) -> dict[str, Any]:
        return federated_durability.evaluate_federated_durability(
            policy=changes.get("policy", _policy()),
            backup_id=changes.get("backup_id", fixture["receipt"]["backupId"]),
            object_set_digest=changes.get("object_set_digest", fixture["federationDigest"]),
            ledger=ledger,
            peer_registry=fixture["registryA"],
            now=NOW + timedelta(seconds=20),
        )
    for changes, code in (
        ({"policy": []}, "FEDERATED_DURABILITY_POLICY_INVALID"),
        ({"policy": {**_policy(), "policyId": "bad/id"}}, "FEDERATED_DURABILITY_POLICY_ID_INVALID"),
        ({"backup_id": "bad/id"}, "FEDERATED_DURABILITY_BACKUP_ID_INVALID"),
        ({"object_set_digest": "bad"}, "FEDERATED_DURABILITY_OBJECT_SET_DIGEST_INVALID"),
        (
            {"policy": {**_policy(), "federatedDurability": {"enabled": True}}},
            "FEDERATED_DURABILITY_OBJECTIVE_INVALID",
        ),
    ):
        _assert_durability_error(lambda changes=changes: evaluate(**changes), code)

    real_transfer = fixture["senderJournal"].get_transfer
    monkeypatch.setattr(fixture["senderJournal"], "get_transfer", lambda *_args: None)
    _assert_durability_error(lambda: _record(fixture, ledger), "FEDERATION_TRANSFER_NOT_FOUND")
    monkeypatch.setattr(fixture["senderJournal"], "get_transfer", real_transfer)

    alien = federated_durability.FederatedDurabilityLedger(
        tmp_settings / "alien.sqlite3",
        fixture["identityB"],
    )
    _assert_durability_error(
        lambda: federated_durability.record_verified_federated_copy(
            ledger=alien,
            peer_registry=fixture["registryA"],
            sender_journal=fixture["senderJournal"],
            transfer_id=fixture["transferId"],
            now=NOW + timedelta(seconds=13),
        ),
        "FEDERATED_DURABILITY_LOCAL_IDENTITY_MISMATCH",
    )

    assert federated_durability.get_ledger(
        fixture["identityA"],
        tmp_settings / "factory.sqlite3",
    ).list_copies() == []
