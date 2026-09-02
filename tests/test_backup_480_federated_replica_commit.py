from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_dr_ledger,
    backup_object_set,
    backup_publish,
    backup_replication,
    backup_target_store,
    backup_writer_lease,
    federated_replica_commit,
    federation_transfer_journal,
)
from tests.test_backup_480_federated_replica_receiver import NOW, _fixture, _receive_all


TARGET_ID = "fleet-b-federated-custody"


def _prepared_fixture(tmp_path: Path) -> dict[str, Any]:
    fixture = _fixture(tmp_path)
    fixture["receiver"].declare_object_set(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        source_receipt=fixture["receipt"],
        now=NOW + timedelta(seconds=3),
    )
    _receive_all(fixture)
    return fixture


def _memory_target() -> tuple[backup_publish.ResolvedTarget, backup_target_store.MemoryTargetStore]:
    store = backup_target_store.MemoryTargetStore()
    return (
        backup_publish.ResolvedTarget(
            target_id=TARGET_ID,
            root=None,
            managed=False,
            kind="s3",
            store=store,
        ),
        store,
    )


def _replace_object(store: backup_target_store.MemoryTargetStore, key: str, content: bytes) -> None:
    metadata = store.stat(key)
    assert metadata is not None
    assert store.delete_if_match(key, expected_etag=metadata.etag)
    store.put_if_absent(key, content, checksum_sha256=hashlib.sha256(content).hexdigest())


def _commit(
    fixture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    target: backup_publish.ResolvedTarget,
    **kwargs: Any,
) -> federated_replica_commit.FederatedReplicaCommitResult:
    monkeypatch.setattr(backup_publish, "resolve_target", lambda target_id, **_kwargs: target if target_id == TARGET_ID else None)
    owner_instance_id = str(kwargs.pop("owner_instance_id", "fleet-b-worker-1"))
    return federated_replica_commit.commit_federated_replica(
        receiver=fixture["receiver"],
        transfer_id=fixture["transferId"],
        target_id=TARGET_ID,
        owner_instance_id=owner_instance_id,
        now=NOW + timedelta(seconds=10),
        **kwargs,
    )


def test_production_commit_keeps_receipt_v4_commit_v4_and_local_durability_separate(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target, store = _memory_target()

    result = _commit(fixture, monkeypatch, target)

    assert set(result.receipt) == federated_replica_commit.RECEIPT_V4_FIELDS
    assert set(result.commit) == federated_replica_commit.COMMIT_V4_FIELDS
    assert result.receipt["schemaVersion"] == backup_publish.RECEIPT_SCHEMA_VERSION == 4
    assert result.commit["schemaVersion"] == backup_publish.COMMIT_SCHEMA_VERSION == 4
    assert result.receipt["storageProtocol"] == result.commit["storageProtocol"] == backup_object_set.OBJECT_SET_V1
    assert result.receipt["objectSetDigest"] == result.commit["objectSetDigest"] == fixture["package"].object_set_digest
    assert result.receipt["controlObjectDigest"] == result.commit["controlObjectDigest"] == fixture["package"].control.ciphertext_digest
    assert result.receipt["objects"] == backup_object_set.remote_object_inventory(fixture["package"].components)
    assert result.commit["receiptDigest"] == hashlib.sha256(
        (json.dumps(result.receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()
    assert result.commit["targetGeneration"] == 1
    assert result.converged is False
    assert result.reconciled is False
    for document in (result.receipt, result.commit):
        assert not ({"transferId", "sourceFleetId", "destinationFleetId", "federation"} & set(document))
    for component in fixture["package"].components:
        assert store.get_bytes(backup_target_store.object_key(component.ciphertext_digest)) == component.path.read_bytes()
    assert store.get_bytes(backup_target_store.receipt_key(fixture["package"].backup_id)) is not None
    assert store.get_bytes(backup_target_store.commit_marker_key(str(result.receipt["policyId"]), str(result.receipt["scheduleSlot"]))) is not None
    assert store.get_bytes(f"catalogs/{result.receipt['policyId']}/{fixture['package'].backup_id}.json") is not None

    assert backup_dr_ledger.list_recovery_points(target_id=TARGET_ID, policy_id=str(result.receipt["policyId"])) == []
    assert backup_dr_ledger.list_logical_recovery_copies(
        policy_id=str(result.receipt["policyId"]),
        backup_id=fixture["package"].backup_id,
    ) == []
    transfer = fixture["journal"].get_transfer(fixture["transferId"])
    assert transfer is not None
    assert transfer["state"] == federation_transfer_journal.STATE_REMOTE_COMMITTED
    assert transfer["stateDetails"]["remoteReceiptDigest"] == "sha256:" + str(result.commit["receiptDigest"])
    assert transfer["stateDetails"]["remoteCommitDigest"] == "sha256:" + hashlib.sha256(
        (json.dumps(result.commit, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()
    assert transfer["stateDetails"]["remoteCommitDigest"] != "sha256:" + str(result.commit["commitHash"])
    events = fixture["journal"].list_transfer_events(fixture["transferId"])
    assert len(events) == 6
    committed_event = next(event for event in events if event["nextState"] == federation_transfer_journal.STATE_REMOTE_COMMITTED)
    assert datetime.fromisoformat(str(committed_event["occurredAt"]).replace("Z", "+00:00")) >= datetime.fromisoformat(
        str(result.receipt["createdAt"]).replace("Z", "+00:00")
    )

    repeated = _commit(fixture, monkeypatch, target)
    assert repeated.receipt == result.receipt
    assert repeated.commit == result.commit
    assert repeated.converged is True
    assert repeated.reconciled is True
    assert len(fixture["journal"].list_transfer_events(fixture["transferId"])) == 6


def test_publish_backup_default_still_records_local_durability_credit(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target, _store = _memory_target()
    package = fixture["receiver"].assemble_verified_package(fixture["transferId"])
    recorded: list[tuple[str, str, str]] = []

    def record_local_credit(
        target_id: str,
        policy_id: str,
        result: backup_publish.PublishResult,
        _package: Any,
    ) -> None:
        recorded.append((target_id, policy_id, str(result.receipt["backupId"])))

    monkeypatch.setattr(backup_publish, "_record_publish_to_dr_ledger", record_local_credit)
    published = backup_publish.publish_backup(
        target,
        package,
        run_id="ordinary-local-publish",
        policy_id=str(fixture["receipt"]["policyId"]),
        schedule_slot="ordinary/local/durability",
        fencing_token=1,
    )

    assert recorded == [(TARGET_ID, str(fixture["receipt"]["policyId"]), fixture["package"].backup_id)]
    assert published.receipt["schemaVersion"] == backup_publish.RECEIPT_SCHEMA_VERSION


def test_unknown_publish_result_is_queried_and_reconciled_without_blind_replay(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target, store = _memory_target()
    real_publish = backup_publish.publish_backup
    publish_calls = 0

    def response_lost(*args: Any, **kwargs: Any) -> backup_publish.PublishResult:
        nonlocal publish_calls
        publish_calls += 1
        real_publish(*args, **kwargs)
        raise RuntimeError("receiver response lost after durable commit")

    monkeypatch.setattr(backup_publish, "resolve_target", lambda _target_id: target)
    monkeypatch.setattr(backup_publish, "publish_backup", response_lost)
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as unknown:
        federated_replica_commit.commit_federated_replica(
            receiver=fixture["receiver"],
            transfer_id=fixture["transferId"],
            target_id=TARGET_ID,
            owner_instance_id="fleet-b-worker-died",
            now=NOW + timedelta(seconds=10),
        )
    assert unknown.value.code == "FEDERATION_REPLICA_REMOTE_RESULT_UNKNOWN"
    transfer = fixture["journal"].get_transfer(fixture["transferId"])
    assert transfer is not None and transfer["state"] == federation_transfer_journal.STATE_REMOTE_VERIFYING
    marker_key = backup_target_store.commit_marker_key(
        str(transfer["policyId"]),
        federated_replica_commit.federated_schedule_slot(fixture["transferId"]),
    )
    assert store.get_bytes(marker_key) is not None

    def must_not_publish(*_args: Any, **_kwargs: Any) -> backup_publish.PublishResult:
        raise AssertionError("query-first reconciliation must not replay publish")

    monkeypatch.setattr(backup_publish, "publish_backup", must_not_publish)
    reconciled = federated_replica_commit.commit_federated_replica(
        receiver=fixture["receiver"],
        transfer_id=fixture["transferId"],
        target_id=TARGET_ID,
        owner_instance_id="fleet-b-worker-restarted",
        now=NOW + timedelta(seconds=20),
    )

    assert publish_calls == 1
    assert reconciled.reconciled is True
    assert reconciled.converged is True
    assert reconciled.commit["targetGeneration"] == 1
    assert store.get_bytes(f"catalogs/{reconciled.receipt['policyId']}/{fixture['package'].backup_id}.json") is not None
    assert fixture["journal"].get_transfer(fixture["transferId"])["state"] == federation_transfer_journal.STATE_REMOTE_COMMITTED


def test_long_remote_commit_renews_target_writer_lease(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target, _ = _memory_target()
    renewals = 0
    original_renew = backup_writer_lease.TargetWriterLease.renew

    def renew_spy(lease: backup_writer_lease.TargetWriterLease) -> None:
        nonlocal renewals
        renewals += 1
        original_renew(lease)

    ticks = iter(range(0, 100, 2))
    monkeypatch.setattr(backup_writer_lease.TargetWriterLease, "renew", renew_spy)
    result = _commit(
        fixture,
        monkeypatch,
        target,
        lease_seconds=3,
        monotonic_clock=lambda: float(next(ticks)),
    )

    assert result.commit["objectSetDigest"] == fixture["package"].object_set_digest
    assert renewals >= 1


def test_federated_replica_commit_rejects_filesystem_target_without_side_effect(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target_root = tmp_settings / "filesystem-target"
    target = backup_publish.ResolvedTarget(
        target_id=TARGET_ID,
        root=target_root,
        managed=False,
        kind="filesystem",
        store=backup_target_store.FilesystemTargetStore(target_root),
    )

    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as rejected:
        _commit(fixture, monkeypatch, target)

    assert rejected.value.code == "FEDERATION_REPLICA_PROVIDER_TARGET_REQUIRED"
    assert fixture["journal"].get_transfer(fixture["transferId"])["state"] == federation_transfer_journal.STATE_PROPOSED
    assert not target_root.exists()


def test_wire_validators_reject_shape_binding_digest_and_json_changes(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target, _ = _memory_target()
    result = _commit(fixture, monkeypatch, target)
    package = fixture["receiver"].assemble_verified_package(fixture["transferId"])
    transfer = fixture["journal"].get_transfer(fixture["transferId"])
    assert transfer is not None

    missing_receipt_field = dict(result.receipt)
    missing_receipt_field.pop("createdAt")
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as receipt_shape:
        federated_replica_commit._validate_receipt(
            missing_receipt_field,
            package=package,
            transfer=transfer,
            target_id=TARGET_ID,
        )
    assert receipt_shape.value.code == "FEDERATION_REPLICA_RECEIPT_V4_INVALID"

    rebound_receipt = dict(result.receipt)
    rebound_receipt["targetId"] = "another-target"
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as receipt_binding:
        federated_replica_commit._validate_receipt(
            rebound_receipt,
            package=package,
            transfer=transfer,
            target_id=TARGET_ID,
        )
    assert receipt_binding.value.code == "FEDERATION_REPLICA_RECEIPT_BINDING_INVALID"

    for field, value in (("snapshotKind", "forged"), ("pinned", True), ("createdAt", "not-a-timestamp")):
        semantically_changed_receipt = dict(result.receipt)
        semantically_changed_receipt[field] = value
        with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as semantic_binding:
            federated_replica_commit._validate_receipt(
                semantically_changed_receipt,
                package=package,
                transfer=transfer,
                target_id=TARGET_ID,
            )
        assert semantic_binding.value.code == "FEDERATION_REPLICA_RECEIPT_BINDING_INVALID"

    missing_commit_field = dict(result.commit)
    missing_commit_field.pop("commitHash")
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as commit_shape:
        federated_replica_commit._validate_commit(
            missing_commit_field,
            receipt=result.receipt,
            receipt_bytes=federated_replica_commit._document_bytes(result.receipt),
            package=package,
            transfer=transfer,
        )
    assert commit_shape.value.code == "FEDERATION_REPLICA_COMMIT_V4_INVALID"

    invalid_commit = dict(result.commit)
    invalid_commit["fencingToken"] = 0
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as commit_binding:
        federated_replica_commit._validate_commit(
            invalid_commit,
            receipt=result.receipt,
            receipt_bytes=federated_replica_commit._document_bytes(result.receipt),
            package=package,
            transfer=transfer,
        )
    assert commit_binding.value.code == "FEDERATION_REPLICA_COMMIT_BINDING_INVALID"

    for raw in (b"not-json", b"[]"):
        with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as invalid_json:
            federated_replica_commit._parse_json(raw, code="INVALID_JSON")
        assert invalid_json.value.code == "INVALID_JSON"
    assert federated_replica_commit._plain_digest_valid("0" * 64) is True
    assert federated_replica_commit._plain_digest_valid("bad") is False
    for transfer_id in ("bad", "sha256:" + ("G" * 64)):
        with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as invalid_transfer:
            federated_replica_commit.federated_run_id(transfer_id)
        assert invalid_transfer.value.code == "FEDERATION_TRANSFER_ID_INVALID"


def test_reconciliation_rejects_missing_receipt_and_remote_component_corruption(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for corruption, expected_code in (
        ("missing-receipt", "FEDERATION_REPLICA_REMOTE_RECEIPT_MISSING"),
        ("missing-component", "FEDERATION_REPLICA_REMOTE_COMPONENT_INVALID"),
        ("corrupt-component", "FEDERATION_REPLICA_REMOTE_COMPONENT_INVALID"),
        ("noncanonical-commit", "FEDERATION_REPLICA_REMOTE_COMMIT_ENCODING_INVALID"),
    ):
        fixture = _prepared_fixture(tmp_settings / corruption)
        target, store = _memory_target()
        result = _commit(fixture, monkeypatch, target)
        if corruption == "missing-receipt":
            key = backup_target_store.receipt_key(fixture["package"].backup_id)
            metadata = store.stat(key)
            assert metadata is not None and store.delete_if_match(key, expected_etag=metadata.etag)
        elif corruption in {"missing-component", "corrupt-component"}:
            component = fixture["package"].control
            key = backup_target_store.object_key(component.ciphertext_digest)
            metadata = store.stat(key)
            assert metadata is not None and store.delete_if_match(key, expected_etag=metadata.etag)
            if corruption == "corrupt-component":
                corrupt = b"x" * component.ciphertext_size
                store.put_if_absent(key, corrupt, checksum_sha256=hashlib.sha256(corrupt).hexdigest())
        else:
            key = backup_target_store.commit_marker_key(
                str(result.receipt["policyId"]),
                str(result.receipt["scheduleSlot"]),
            )
            metadata = store.stat(key)
            assert metadata is not None and store.delete_if_match(key, expected_etag=metadata.etag)
            store.put_if_absent(key, json.dumps(result.commit, sort_keys=True).encode("utf-8"))

        with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as rejected:
            _commit(fixture, monkeypatch, target)
        assert rejected.value.code == expected_code


def test_commit_input_target_clock_and_transfer_state_fail_closed(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target, _ = _memory_target()
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as owner:
        _commit(fixture, monkeypatch, target, owner_instance_id="bad/owner")
    assert owner.value.code == "FEDERATION_REPLICA_OWNER_INSTANCE_INVALID"
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as lease:
        _commit(fixture, monkeypatch, target, lease_seconds=2)
    assert lease.value.code == "FEDERATION_REPLICA_WRITER_LEASE_INVALID"

    monkeypatch.setattr(backup_publish, "resolve_target", lambda _target_id: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as unavailable:
        federated_replica_commit.commit_federated_replica(
            receiver=fixture["receiver"],
            transfer_id=fixture["transferId"],
            target_id=TARGET_ID,
            owner_instance_id="worker-valid",
            now=NOW + timedelta(seconds=10),
        )
    assert unavailable.value.code == "FEDERATION_REPLICA_TARGET_UNAVAILABLE"

    monkeypatch.setattr(backup_publish, "resolve_target", lambda _target_id: None)
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as identity:
        federated_replica_commit.commit_federated_replica(
            receiver=fixture["receiver"],
            transfer_id=fixture["transferId"],
            target_id=TARGET_ID,
            owner_instance_id="worker-valid",
            now=NOW + timedelta(seconds=10),
        )
    assert identity.value.code == "FEDERATION_REPLICA_TARGET_IDENTITY_INVALID"

    monkeypatch.setattr(backup_publish, "resolve_target", lambda _target_id: target)
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as clock:
        federated_replica_commit.commit_federated_replica(
            receiver=fixture["receiver"],
            transfer_id=fixture["transferId"],
            target_id=TARGET_ID,
            owner_instance_id="worker-valid",
            now=NOW + timedelta(seconds=10),
            monotonic_clock=lambda: float("nan"),
        )
    assert clock.value.code == "FEDERATION_REPLICA_MONOTONIC_CLOCK_INVALID"

    conflicting = _prepared_fixture(tmp_settings / "state-conflict")
    record = conflicting["journal"].get_transfer(conflicting["transferId"])
    assert record is not None
    conflicting["journal"].advance_transfer(
        conflicting["transferId"],
        expected_revision=int(record["revision"]),
        next_state=federation_transfer_journal.STATE_GRANT_REQUESTED,
        details={"unexpected": True},
        now=NOW + timedelta(seconds=5),
    )
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as state:
        _commit(conflicting, monkeypatch, target)
    assert state.value.code == "FEDERATION_REPLICA_TRANSFER_STATE_CONFLICT"


def test_known_publish_failure_and_missing_commit_remain_uncommitted(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target, _ = _memory_target()
    monkeypatch.setattr(backup_publish, "resolve_target", lambda _target_id: target)

    def known_failure(*_args: Any, **_kwargs: Any) -> backup_publish.PublishResult:
        raise AppError("conditional write failed", code=ErrorCode.INVALID_REQUEST, status=409)

    monkeypatch.setattr(backup_publish, "publish_backup", known_failure)
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as failed:
        federated_replica_commit.commit_federated_replica(
            receiver=fixture["receiver"],
            transfer_id=fixture["transferId"],
            target_id=TARGET_ID,
            owner_instance_id="worker-known-failure",
            now=NOW + timedelta(seconds=10),
        )
    assert failed.value.code == "FEDERATION_REPLICA_REMOTE_COMMIT_FAILED"
    assert fixture["journal"].get_transfer(fixture["transferId"])["state"] == federation_transfer_journal.STATE_REMOTE_VERIFYING

    def no_effect(*_args: Any, **_kwargs: Any) -> backup_publish.PublishResult:
        return backup_publish.PublishResult({}, None, None, {}, False)

    monkeypatch.setattr(backup_publish, "publish_backup", no_effect)
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as unknown:
        federated_replica_commit.commit_federated_replica(
            receiver=fixture["receiver"],
            transfer_id=fixture["transferId"],
            target_id=TARGET_ID,
            owner_instance_id="worker-no-effect",
            now=NOW + timedelta(seconds=20),
        )
    assert unknown.value.code == "FEDERATION_REPLICA_REMOTE_RESULT_UNKNOWN"


def test_reconciliation_rejects_noncanonical_receipt_encoding(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target, store = _memory_target()
    result = _commit(fixture, monkeypatch, target)
    receipt_key = backup_target_store.receipt_key(fixture["package"].backup_id)
    minified_receipt = json.dumps(result.receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _replace_object(store, receipt_key, minified_receipt)
    rebound_commit = dict(result.commit)
    rebound_commit["receiptDigest"] = hashlib.sha256(minified_receipt).hexdigest()
    rebound_commit["commitHash"] = backup_publish._commit_hash(rebound_commit)
    marker_key = backup_target_store.commit_marker_key(
        str(result.receipt["policyId"]),
        str(result.receipt["scheduleSlot"]),
    )
    _replace_object(store, marker_key, federated_replica_commit._document_bytes(rebound_commit))

    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as encoding:
        _commit(fixture, monkeypatch, target)
    assert encoding.value.code == "FEDERATION_REPLICA_REMOTE_RECEIPT_ENCODING_INVALID"


def test_commit_wraps_incomplete_receiver_timestamp_and_writer_acquire_failures(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incomplete = _fixture(tmp_settings / "incomplete")
    incomplete["receiver"].declare_object_set(
        grant=incomplete["grant"],
        transfer_id=incomplete["transferId"],
        source_receipt=incomplete["receipt"],
        now=NOW + timedelta(seconds=3),
    )
    target, _ = _memory_target()
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as missing:
        _commit(incomplete, monkeypatch, target)
    assert missing.value.code == "FEDERATION_REPLICA_OBJECT_SET_INCOMPLETE"

    regressed = _prepared_fixture(tmp_settings / "timestamp")
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as timestamp:
        monkeypatch.setattr(backup_publish, "resolve_target", lambda _target_id: target)
        federated_replica_commit.commit_federated_replica(
            receiver=regressed["receiver"],
            transfer_id=regressed["transferId"],
            target_id=TARGET_ID,
            owner_instance_id="worker-regressed-time",
            now=NOW,
        )
    assert timestamp.value.code == "FEDERATION_TRANSFER_TIMESTAMP_REGRESSION"

    writer_failed = _prepared_fixture(tmp_settings / "writer")
    failed_target, failed_store = _memory_target()
    failed_store.inject_failure(
        "put_if_absent",
        AppError("writer unavailable", code=ErrorCode.INVALID_REQUEST, status=503),
    )
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as writer:
        _commit(writer_failed, monkeypatch, failed_target)
    assert writer.value.code == "FEDERATION_REPLICA_REMOTE_COMMIT_FAILED"


def test_catalog_checkpoint_and_release_failures_preserve_unknown_or_committed_state(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_fixture = _prepared_fixture(tmp_settings / "catalog")
    catalog_target, _ = _memory_target()
    with monkeypatch.context() as scoped:
        scoped.setattr(
            backup_replication,
            "append_target_local_catalog",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("catalog response lost")),
        )
        with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as catalog:
            _commit(catalog_fixture, scoped, catalog_target)
    assert catalog.value.code == "FEDERATION_REPLICA_REMOTE_RESULT_UNKNOWN"

    checkpoint_fixture = _prepared_fixture(tmp_settings / "checkpoint")
    checkpoint_target, _ = _memory_target()
    with monkeypatch.context() as scoped:
        scoped.setattr(backup_publish, "resolve_target", lambda _target_id: checkpoint_target)
        scoped.setattr(
            federated_replica_commit,
            "_read_existing_commit",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AppError("read uncertain", code=ErrorCode.INVALID_REQUEST, status=503)
            ),
        )
        with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as checkpoint:
            federated_replica_commit.commit_federated_replica(
                receiver=checkpoint_fixture["receiver"],
                transfer_id=checkpoint_fixture["transferId"],
                target_id=TARGET_ID,
                owner_instance_id="worker-checkpoint",
                now=NOW + timedelta(seconds=10),
            )
    assert checkpoint.value.code == "FEDERATION_REPLICA_REMOTE_RESULT_UNKNOWN"

    release_fixture = _prepared_fixture(tmp_settings / "release")
    release_target, _ = _memory_target()
    original_release = backup_writer_lease.TargetWriterLease.release

    def release_after_unlock(lease: backup_writer_lease.TargetWriterLease) -> None:
        original_release(lease)
        raise OSError("release acknowledgement lost")

    with monkeypatch.context() as scoped:
        scoped.setattr(backup_writer_lease.TargetWriterLease, "release", release_after_unlock)
        released = _commit(release_fixture, scoped, release_target)
    assert released.commit["objectSetDigest"] == release_fixture["package"].object_set_digest


def test_remote_commit_event_binding_rejects_a_different_target(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target, _ = _memory_target()
    result = _commit(fixture, monkeypatch, target)
    transfer = fixture["journal"].get_transfer(fixture["transferId"])
    assert transfer is not None
    published = backup_publish.PublishResult(
        result.receipt,
        None,
        None,
        result.commit,
        True,
    )

    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as conflict:
        federated_replica_commit._record_remote_committed(
            fixture["journal"],
            transfer,
            target_id="different-target",
            published=published,
            now=NOW + timedelta(seconds=20),
        )
    assert conflict.value.code == "FEDERATION_REPLICA_REMOTE_COMMIT_STATE_CONFLICT"


def test_inspect_committed_replica_rechecks_provider_journal_and_remote_effect(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _prepared_fixture(tmp_settings)
    target, store = _memory_target()
    committed = _commit(fixture, monkeypatch, target)
    checkpoints = 0

    def checkpoint() -> None:
        nonlocal checkpoints
        checkpoints += 1

    inspected = federated_replica_commit.inspect_committed_federated_replica(
        receiver=fixture["receiver"],
        transfer_id=fixture["transferId"],
        target_id=TARGET_ID,
        checkpoint=checkpoint,
    )
    assert inspected.receipt == committed.receipt
    assert inspected.commit == committed.commit
    assert inspected.fencing_token == committed.commit["fencingToken"]
    assert inspected.converged is True and inspected.reconciled is True
    assert checkpoints > 0
    assert federated_replica_commit._canonical_utc_timestamp_valid(None) is False

    original_get_transfer = fixture["journal"].get_transfer
    with monkeypatch.context() as scoped:
        scoped.setattr(
            fixture["journal"],
            "get_transfer",
            lambda transfer_id: {**original_get_transfer(transfer_id), "role": federation_transfer_journal.ROLE_SENDER},
        )
        with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as role_error:
            federated_replica_commit.inspect_committed_federated_replica(
                receiver=fixture["receiver"],
                transfer_id=fixture["transferId"],
                target_id=TARGET_ID,
            )
        assert role_error.value.code == "FEDERATION_REPLICA_RECEIVER_FLEET_MISMATCH"

    with monkeypatch.context() as scoped:
        scoped.setattr(
            fixture["journal"],
            "get_transfer",
            lambda transfer_id: {**original_get_transfer(transfer_id), "objectSetDigest": "sha256:" + ("f" * 64)},
        )
        with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as digest_error:
            federated_replica_commit.inspect_committed_federated_replica(
                receiver=fixture["receiver"],
                transfer_id=fixture["transferId"],
                target_id=TARGET_ID,
            )
        assert digest_error.value.code == "FEDERATION_REPLICA_OBJECT_SET_DIGEST_MISMATCH"

    for replacement, expected in (
        (lambda _target_id: (_ for _ in ()).throw(RuntimeError("offline")), "FEDERATION_REPLICA_TARGET_UNAVAILABLE"),
        (lambda _target_id: None, "FEDERATION_REPLICA_TARGET_IDENTITY_INVALID"),
        (
            lambda _target_id: backup_publish.ResolvedTarget(
                target_id=TARGET_ID,
                root=tmp_settings / "filesystem",
                managed=False,
                kind="filesystem",
                store=store,
            ),
            "FEDERATION_REPLICA_PROVIDER_TARGET_REQUIRED",
        ),
    ):
        with monkeypatch.context() as scoped:
            scoped.setattr(backup_publish, "resolve_target", replacement)
            with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as target_error:
                federated_replica_commit.inspect_committed_federated_replica(
                    receiver=fixture["receiver"],
                    transfer_id=fixture["transferId"],
                    target_id=TARGET_ID,
                )
            assert target_error.value.code == expected

    real_events = fixture["journal"].list_transfer_events
    with monkeypatch.context() as scoped:
        scoped.setattr(fixture["journal"], "list_transfer_events", lambda _transfer_id: real_events(fixture["transferId"])[:-1])
        with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as event_error:
            federated_replica_commit.inspect_committed_federated_replica(
                receiver=fixture["receiver"],
                transfer_id=fixture["transferId"],
                target_id=TARGET_ID,
            )
        assert event_error.value.code == "FEDERATION_REPLICA_REMOTE_COMMIT_STATE_CONFLICT"

    commit_key = backup_target_store.commit_marker_key(
        str(committed.receipt["policyId"]),
        str(committed.receipt["scheduleSlot"]),
    )
    commit_meta = store.stat(commit_key)
    assert commit_meta is not None and store.delete_if_match(commit_key, expected_etag=commit_meta.etag)
    with pytest.raises(federated_replica_commit.FederatedReplicaCommitError) as missing_commit:
        federated_replica_commit.inspect_committed_federated_replica(
            receiver=fixture["receiver"],
            transfer_id=fixture["transferId"],
            target_id=TARGET_ID,
        )
    assert missing_commit.value.code == "FEDERATION_REPLICA_REMOTE_COMMIT_MISSING"
