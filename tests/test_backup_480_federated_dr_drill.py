from __future__ import annotations

import copy
import json
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_crypto,
    backup_recovery_credential,
    backup_recovery_drill,
    backup_remote_restore,
    federated_dr_drill,
    federation_custody_capability,
    federation_identity,
    federation_peer_trust,
    federation_transfer_journal,
)
from tests.test_backup_480_federated_replica_attestation import _attestation_fixture, _verify
from tests.test_backup_480_federated_replica_commit import TARGET_ID
from tests.test_backup_480_federated_replica_receiver import NOW


IDENTITY_TEXT = "AGE-SECRET-KEY-1FEDERATED-DR-RECEIVER"
RECIPIENT = "age1federateddrreceiveridentity"


def _assert_dr_error(call: Any, code: str) -> None:
    with pytest.raises(federated_dr_drill.FederatedDrDrillError) as rejected:
        call()
    assert rejected.value.code == code


def _capability(
    tmp_settings: Path,
    fixture: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    mode: str = federation_custody_capability.RECOVERY_CAPABLE,
) -> federation_custody_capability.FederationCustodyCapabilityRegistry:
    registry = federation_custody_capability.FederationCustodyCapabilityRegistry(
        tmp_settings / "fleet-b" / f"custody-{mode.lower()}.sqlite3",
        fixture["identityB"],
    )
    kwargs: dict[str, Any] = {}
    if mode == federation_custody_capability.RECOVERY_CAPABLE:
        provider_name = "fleet-b-federated-dr"
        provider = backup_recovery_credential.InMemoryCredentialProvider()
        provider.set_credential("age-slot", IDENTITY_TEXT)
        backup_recovery_credential.register_provider(provider_name, provider)
        monkeypatch.setattr(backup_crypto, "derive_recipients", lambda _secret: (RECIPIENT,))
        kwargs = {
            "credential_provider": provider_name,
            "credential_ref": "age-slot",
            "age_recipient": RECIPIENT,
        }
    registry.configure_peer(
        fixture["registry"],
        "fleet-a",
        mode=mode,
        actor="operator-b",
        now=NOW + timedelta(seconds=12),
        **kwargs,
    )
    return registry


def _issue(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sequence: int = 1,
    restore_id: str = "restore_federateddr0001",
) -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    fixture = _attestation_fixture(tmp_settings, monkeypatch)
    _verify(fixture)
    shutil.rmtree(fixture["stagingDir"])
    assert not fixture["stagingDir"].exists()
    capability = _capability(tmp_settings, fixture, monkeypatch)
    calls: list[Any] = []

    def create_restore(**kwargs: Any) -> dict[str, Any]:
        calls.append(("create", kwargs))
        return {"restoreId": restore_id, "targetId": kwargs["target_id"], "backupId": kwargs["backup_id"]}

    def run_restore(value: str, *, client: Any = None) -> dict[str, Any]:
        calls.append(("run", value, client))
        kind, secret = backup_crypto.consume_secret(value, expected_kind="age-identity")
        calls.append(("identity", kind, bytes(secret)))
        secret[:] = b"\x00" * len(secret)
        return {
            "schemaVersion": 1,
            "restoreId": value,
            "result": "success",
            "startedAt": (NOW + timedelta(seconds=13)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "completedAt": (NOW + timedelta(seconds=15)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "durationMs": 2_000,
            "workspaceDigest": "sha256:" + ("c" * 64),
            "sourceRevision": "source-revision-a",
            "cleanupCompleted": True,
            "chainLength": 1,
            "components": 2,
            "ciphertextBytes": 64,
            "logicalBytes": 32,
            "verifiedContributors": 1,
        }

    monkeypatch.setattr(backup_remote_restore, "create_restore_from_target", create_restore)
    monkeypatch.setattr(backup_recovery_drill, "run_recovery_drill", run_restore)
    attestation = federated_dr_drill.run_federated_dr_drill(
        signer=fixture["signerB"],
        receiver=fixture["receiver"],
        peer_registry=fixture["registry"],
        custody_registry=capability,
        replica_attestation=fixture["attestation"],
        transfer_id=fixture["transferId"],
        remote_target_id=TARGET_ID,
        sequence=sequence,
        signed_at=NOW + timedelta(seconds=16),
        expires_at=NOW + timedelta(seconds=76),
        client="receiver-client",
    )
    assert not backup_crypto.has_secret(restore_id)
    return fixture, attestation, calls


def _resign(fixture: dict[str, Any], attestation: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    payload = {
        key: copy.deepcopy(value)
        for key, value in attestation.items()
        if key not in {"signerKeyId", "signatureAlgorithm", "signature"}
    }
    payload.update(changes)
    return federation_identity.sign_federation_document(
        fixture["signerB"],
        payload,
        purpose=federation_identity.PURPOSE_DR_ATTESTATION,
    )


def _verify_dr(fixture: dict[str, Any], attestation: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    now = kwargs.pop("now", NOW + timedelta(seconds=17))
    return federated_dr_drill.verify_and_record_dr_drill_attestation(
        attestation,
        peer_registry=fixture["registryA"],
        sender_journal=fixture["senderJournal"],
        now=now,
        **kwargs,
    )


def test_federated_dr_drill_uses_production_restore_and_records_signed_semantics(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, attestation, calls = _issue(tmp_settings, monkeypatch)

    assert set(attestation) == federated_dr_drill.DR_DRILL_ATTESTATION_FIELDS
    assert attestation["schema"] == federated_dr_drill.DR_DRILL_ATTESTATION_SCHEMA
    assert attestation["fleetId"] == attestation["destinationFleetId"] == "fleet-b"
    assert attestation["sourceFleetId"] == "fleet-a"
    assert attestation["transferId"] == fixture["transferId"]
    assert attestation["backupId"] == fixture["package"].backup_id
    assert attestation["objectSetDigest"] == fixture["federationDigest"]
    assert attestation["remoteTargetId"] == TARGET_ID
    assert attestation["remoteReceiptDigest"] == fixture["attestation"]["remoteReceiptDigest"]
    assert attestation["remoteCommitDigest"] == fixture["attestation"]["remoteCommitDigest"]
    assert attestation["replicaAttestationDigest"] == federated_dr_drill.replica_attestation_digest(fixture["attestation"])
    assert attestation["restoreId"] == "restore_federateddr0001"
    assert attestation["restorePath"] == federated_dr_drill.PRODUCTION_RESTORE_PATH
    assert attestation["workspaceDigest"] == "sha256:" + ("c" * 64)
    assert attestation["sourceRevision"] == "source-revision-a"
    assert attestation["rtoMs"] == 2_000
    assert attestation["cleanupCompleted"] is True
    assert IDENTITY_TEXT not in json.dumps(attestation, sort_keys=True)
    assert calls[0][0] == "create"
    assert calls[0][1]["target_id"] == TARGET_ID
    assert calls[0][1]["backup_id"] == fixture["package"].backup_id
    assert calls[1] == ("run", "restore_federateddr0001", "receiver-client")
    assert calls[2] == ("identity", "age-identity", IDENTITY_TEXT.encode("utf-8"))

    assert _verify_dr(fixture, attestation) == attestation
    accepted = fixture["registryA"].get_dr_attestation("fleet-b", "restore_federateddr0001")
    assert accepted is not None and accepted["attestation"] == attestation
    assert _verify_dr(fixture, attestation) == attestation


def test_cold_custody_and_incomplete_cleanup_cannot_issue_dr_attestation(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _attestation_fixture(tmp_settings, monkeypatch)
    cold = _capability(tmp_settings, fixture, monkeypatch, mode=federation_custody_capability.COLD_CUSTODY)
    _assert_dr_error(
        lambda: federated_dr_drill.run_federated_dr_drill(
            signer=fixture["signerB"],
            receiver=fixture["receiver"],
            peer_registry=fixture["registry"],
            custody_registry=cold,
            replica_attestation=fixture["attestation"],
            transfer_id=fixture["transferId"],
            remote_target_id=TARGET_ID,
            sequence=1,
            signed_at=NOW + timedelta(seconds=16),
            expires_at=NOW + timedelta(seconds=76),
        ),
        "FEDERATION_PEER_COLD_CUSTODY_ONLY",
    )

    recovery = _capability(tmp_settings / "cleanup", fixture, monkeypatch)
    monkeypatch.setattr(
        backup_remote_restore,
        "create_restore_from_target",
        lambda **_kwargs: {"restoreId": "restore_cleanupfailed"},
    )
    monkeypatch.setattr(
        backup_recovery_drill,
        "run_recovery_drill",
        lambda *_args, **_kwargs: {
            "restoreId": "restore_cleanupfailed",
            "result": "success",
            "startedAt": (NOW + timedelta(seconds=13)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "completedAt": (NOW + timedelta(seconds=15)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "durationMs": 2_000,
            "workspaceDigest": "sha256:" + ("c" * 64),
            "sourceRevision": "source-revision-a",
            "cleanupCompleted": False,
        },
    )
    _assert_dr_error(
        lambda: federated_dr_drill.run_federated_dr_drill(
            signer=fixture["signerB"],
            receiver=fixture["receiver"],
            peer_registry=fixture["registry"],
            custody_registry=recovery,
            replica_attestation=fixture["attestation"],
            transfer_id=fixture["transferId"],
            remote_target_id=TARGET_ID,
            sequence=1,
            signed_at=NOW + timedelta(seconds=16),
            expires_at=NOW + timedelta(seconds=76),
        ),
        "FEDERATED_DR_CLEANUP_INCOMPLETE",
    )
    assert not backup_crypto.has_secret("restore_cleanupfailed")


def test_validly_resigned_dr_semantic_tampering_fails_closed(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, attestation, _calls = _issue(tmp_settings, monkeypatch)
    cases = (
        ({"sourceFleetId": "fleet-c"}, "FEDERATED_DR_SOURCE_FLEET_MISMATCH"),
        ({"destinationFleetId": "fleet-c"}, "FEDERATED_DR_DESTINATION_FLEET_MISMATCH"),
        ({"backupId": "different-backup"}, "FEDERATED_DR_BACKUP_ID_MISMATCH"),
        ({"objectSetDigest": "sha256:" + ("f" * 64)}, "FEDERATED_DR_OBJECT_SET_DIGEST_MISMATCH"),
        ({"remoteTargetId": "different-target"}, "FEDERATED_DR_REMOTE_TARGET_MISMATCH"),
        ({"remoteReceiptDigest": "sha256:" + ("f" * 64)}, "FEDERATED_DR_REMOTE_RECEIPT_MISMATCH"),
        ({"remoteCommitDigest": "sha256:" + ("f" * 64)}, "FEDERATED_DR_REMOTE_COMMIT_MISMATCH"),
        ({"replicaAttestationDigest": "sha256:" + ("f" * 64)}, "FEDERATED_DR_REPLICA_ATTESTATION_MISMATCH"),
        ({"restorePath": "self-declared-success"}, "FEDERATED_DR_RESTORE_PATH_INVALID"),
        ({"workspaceDigest": "short"}, "FEDERATED_DR_WORKSPACE_DIGEST_INVALID"),
        ({"sourceRevision": ""}, "FEDERATED_DR_SOURCE_REVISION_INVALID"),
        ({"cleanupCompleted": False}, "FEDERATED_DR_CLEANUP_INCOMPLETE"),
        ({"rtoMs": True}, "FEDERATED_DR_RTO_INVALID"),
    )
    for changes, code in cases:
        _assert_dr_error(lambda changes=changes: _verify_dr(fixture, _resign(fixture, attestation, changes)), code)

    tampered = copy.deepcopy(attestation)
    tampered["signature"] = ("A" if tampered["signature"][0] != "A" else "B") + tampered["signature"][1:]
    _assert_dr_error(lambda: _verify_dr(fixture, tampered), "FEDERATION_DOCUMENT_SIGNATURE_INVALID")
    assert fixture["registryA"].get_dr_attestation("fleet-b", "restore_federateddr0001") is None
    assert _verify_dr(fixture, attestation) == attestation


def test_dr_attestation_sequence_replay_conflict_and_revocation_fail_closed(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, first, _calls = _issue(tmp_settings, monkeypatch, sequence=3)
    assert _verify_dr(fixture, first) == first

    conflicting = _resign(fixture, first, {"workspaceDigest": "sha256:" + ("d" * 64)})
    _assert_dr_error(lambda: _verify_dr(fixture, conflicting), "FEDERATED_DR_ATTESTATION_IDENTITY_CONFLICT")

    replay = _resign(
        fixture,
        first,
        {
            "restoreId": "restore_federateddr0002",
            "sequence": 2,
            "workspaceDigest": "sha256:" + ("e" * 64),
        },
    )
    _assert_dr_error(lambda: _verify_dr(fixture, replay), "FEDERATED_DR_ATTESTATION_SEQUENCE_REPLAY")

    sequence_conflict = _resign(
        fixture,
        first,
        {"restoreId": "restore_federateddr0003", "workspaceDigest": "sha256:" + ("e" * 64)},
    )
    _assert_dr_error(lambda: _verify_dr(fixture, sequence_conflict), "FEDERATED_DR_ATTESTATION_SEQUENCE_CONFLICT")

    fixture["registryA"].revoke_online_signer(
        "fleet-b",
        str(fixture["certificateB"]["signerKeyId"]),
        actor="operator-a",
        reason="incident",
        revoked_at=NOW + timedelta(seconds=18),
    )
    future = _resign(
        fixture,
        first,
        {"restoreId": "restore_federateddr0004", "sequence": 4, "workspaceDigest": "sha256:" + ("f" * 64)},
    )
    _assert_dr_error(lambda: _verify_dr(fixture, future, now=NOW + timedelta(seconds=19)), "FEDERATION_SIGNER_REVOKED")


def test_dr_scalar_and_canonical_validators_fail_closed() -> None:
    for call, code in (
        (lambda: federated_dr_drill._normalize({"ratio": float("nan")}), "FEDERATED_DR_CANONICAL_PAYLOAD_INVALID"),
        (lambda: federated_dr_drill._canonical_json({"ratio": float("inf")}), "FEDERATED_DR_CANONICAL_PAYLOAD_INVALID"),
        (lambda: federated_dr_drill._utc_iso(NOW.replace(tzinfo=None)), "FEDERATED_DR_TIMESTAMP_INVALID"),
        (lambda: federated_dr_drill._parse_timestamp(None), "FEDERATED_DR_TIMESTAMP_INVALID"),
        (lambda: federated_dr_drill._parse_timestamp("not-a-time"), "FEDERATED_DR_TIMESTAMP_INVALID"),
        (lambda: federated_dr_drill._parse_timestamp("2026-09-01T08:00:00"), "FEDERATED_DR_TIMESTAMP_INVALID"),
        (lambda: federated_dr_drill._parse_timestamp("2026-09-01T08:00:00+00:00"), "FEDERATED_DR_TIMESTAMP_INVALID"),
        (lambda: federated_dr_drill._fleet_id("Fleet A"), "FEDERATED_DR_FLEET_ID_INVALID"),
        (lambda: federated_dr_drill._control_id("", code="CONTROL_INVALID"), "CONTROL_INVALID"),
        (lambda: federated_dr_drill._restore_id("restore_bad_id"), "FEDERATED_DR_RESTORE_ID_INVALID"),
        (lambda: federated_dr_drill._sequence(False), "FEDERATED_DR_ATTESTATION_SEQUENCE_INVALID"),
        (lambda: federated_dr_drill._rto(-1), "FEDERATED_DR_RTO_INVALID"),
        (lambda: federated_dr_drill._rto(federated_dr_drill.MAX_DR_RTO_MS + 1), "FEDERATED_DR_RTO_INVALID"),
        (lambda: federated_dr_drill._source_revision(" revision"), "FEDERATED_DR_SOURCE_REVISION_INVALID"),
        (lambda: federated_dr_drill._source_revision("revision\n"), "FEDERATED_DR_SOURCE_REVISION_INVALID"),
        (lambda: federated_dr_drill._source_revision("x" * 513), "FEDERATED_DR_SOURCE_REVISION_INVALID"),
        (
            lambda: federated_dr_drill._validate_window(NOW, NOW + timedelta(seconds=301)),
            "FEDERATED_DR_ATTESTATION_LIFETIME_INVALID",
        ),
    ):
        _assert_dr_error(call, code)


def test_receiver_local_signer_registry_and_replica_fail_closed(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _attestation_fixture(tmp_settings, monkeypatch)
    signed_at = NOW + timedelta(seconds=16)
    expires_at = NOW + timedelta(seconds=76)
    _assert_dr_error(
        lambda: federated_dr_drill._require_local_signer(
            fixture["signerB"], fixture["identityA"], signed_at=signed_at, expires_at=expires_at
        ),
        "FEDERATED_DR_LOCAL_SIGNER_MISMATCH",
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(federation_identity, "validate_online_signer_certificate", lambda *_args, **_kwargs: ["DR_PURPOSE_MISSING"])
        _assert_dr_error(
            lambda: federated_dr_drill._require_local_signer(
                fixture["signerB"], fixture["identityB"], signed_at=signed_at, expires_at=expires_at
            ),
            "DR_PURPOSE_MISSING",
        )
    certificate_expiry = federated_dr_drill._parse_timestamp(fixture["certificateB"]["expiresAt"])
    _assert_dr_error(
        lambda: federated_dr_drill._require_local_signer(
            fixture["signerB"],
            fixture["identityB"],
            signed_at=signed_at,
            expires_at=certificate_expiry + timedelta(seconds=1),
        ),
        "FEDERATED_DR_SIGNER_WINDOW_INVALID",
    )

    foreign_capability = federation_custody_capability.FederationCustodyCapabilityRegistry(
        tmp_settings / "fleet-a" / "foreign-custody.sqlite3",
        fixture["identityA"],
    )
    _assert_dr_error(
        lambda: federated_dr_drill._require_local_registry_identity(
            receiver=fixture["receiver"],
            peer_registry=fixture["registry"],
            custody_registry=foreign_capability,
        ),
        "FEDERATED_DR_LOCAL_IDENTITY_MISMATCH",
    )

    binding_kwargs = {
        "receiver": fixture["receiver"],
        "transfer_id": fixture["transferId"],
        "remote_target_id": TARGET_ID,
        "local_identity": fixture["identityB"],
    }
    _assert_dr_error(
        lambda: federated_dr_drill._local_replica_binding({}, **binding_kwargs),
        "FEDERATED_DR_REPLICA_ATTESTATION_INVALID",
    )
    no_certificate = {**fixture["attestation"], "signerCertificate": None}
    _assert_dr_error(
        lambda: federated_dr_drill._local_replica_binding(no_certificate, **binding_kwargs),
        "FEDERATED_DR_REPLICA_ATTESTATION_INVALID",
    )
    tampered = copy.deepcopy(fixture["attestation"])
    tampered["signature"] = ("A" if tampered["signature"][0] != "A" else "B") + tampered["signature"][1:]
    _assert_dr_error(
        lambda: federated_dr_drill._local_replica_binding(tampered, **binding_kwargs),
        "FEDERATION_DOCUMENT_SIGNATURE_INVALID",
    )

    with monkeypatch.context() as scoped:
        scoped.setattr(
            fixture["receiver"].transfer_journal,
            "get_transfer",
            lambda _transfer: (_ for _ in ()).throw(federation_transfer_journal.FederatedTransferJournalError("JOURNAL_FAILED")),
        )
        _assert_dr_error(
            lambda: federated_dr_drill._local_replica_binding(fixture["attestation"], **binding_kwargs),
            "JOURNAL_FAILED",
        )
    with monkeypatch.context() as scoped:
        scoped.setattr(fixture["receiver"].transfer_journal, "get_transfer", lambda _transfer: None)
        _assert_dr_error(
            lambda: federated_dr_drill._local_replica_binding(fixture["attestation"], **binding_kwargs),
            "FEDERATION_TRANSFER_NOT_FOUND",
        )

    replica_payload = {
        key: copy.deepcopy(value)
        for key, value in fixture["attestation"].items()
        if key not in {"signerKeyId", "signatureAlgorithm", "signature"}
    }
    replica_payload["sourceFleetId"] = "fleet-c"
    rebound = federation_identity.sign_federation_document(
        fixture["signerB"],
        replica_payload,
        purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
    )
    _assert_dr_error(
        lambda: federated_dr_drill._local_replica_binding(rebound, **binding_kwargs),
        "FEDERATION_TRANSFER_ID_INVALID",
    )
    invalid_window_payload = {
        key: copy.deepcopy(value)
        for key, value in fixture["attestation"].items()
        if key not in {"signerKeyId", "signatureAlgorithm", "signature"}
    }
    invalid_window_payload["expiresAt"] = invalid_window_payload["signedAt"]
    invalid_window = federation_identity.sign_federation_document(
        fixture["signerB"],
        invalid_window_payload,
        purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
    )
    _assert_dr_error(
        lambda: federated_dr_drill._local_replica_binding(invalid_window, **binding_kwargs),
        "FEDERATION_REPLICA_ATTESTATION_LIFETIME_INVALID",
    )

    semantic_cases = (
        (
            {
                "committedAt": (NOW + timedelta(seconds=12)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            },
            "FEDERATION_REPLICA_ATTESTATION_COMMIT_TIME_INVALID",
        ),
        ({"remoteTargetId": "fleet-b-other-custody"}, "FEDERATED_DR_REPLICA_ATTESTATION_BINDING_INVALID"),
        (
            {
                "signedAt": (certificate_expiry - timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "expiresAt": (certificate_expiry + timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            },
            "FEDERATION_REPLICA_ATTESTATION_SIGNER_WINDOW_INVALID",
        ),
    )
    for changes, code in semantic_cases:
        payload = {
            key: copy.deepcopy(value)
            for key, value in fixture["attestation"].items()
            if key not in {"signerKeyId", "signatureAlgorithm", "signature"}
        }
        payload.update(changes)
        candidate = federation_identity.sign_federation_document(
            fixture["signerB"],
            payload,
            purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
        )
        _assert_dr_error(
            lambda candidate=candidate: federated_dr_drill._local_replica_binding(candidate, **binding_kwargs),
            code,
        )

    transfer = fixture["journal"].get_transfer(fixture["transferId"])
    assert transfer is not None
    receipt_bytes, commit_bytes = federated_dr_drill._inspect_durable_remote_documents(
        transfer=transfer,
        remote_target_id=TARGET_ID,
        replica_attestation=fixture["attestation"],
    )
    assert receipt_bytes == fixture["receiptBytes"]
    assert commit_bytes == fixture["commitBytes"]
    rebound_digest = {**fixture["attestation"], "remoteReceiptDigest": "sha256:" + ("f" * 64)}
    _assert_dr_error(
        lambda: federated_dr_drill._inspect_durable_remote_documents(
            transfer=transfer,
            remote_target_id=TARGET_ID,
            replica_attestation=rebound_digest,
        ),
        "FEDERATION_REPLICA_ATTESTATION_REMOTE_RECEIPT_DIGEST_MISMATCH",
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(
            federated_dr_drill.backup_publish,
            "resolve_target",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
        )
        _assert_dr_error(
            lambda: federated_dr_drill._inspect_durable_remote_documents(
                transfer=transfer,
                remote_target_id=TARGET_ID,
                replica_attestation=fixture["attestation"],
            ),
            "FEDERATED_DR_REMOTE_TARGET_UNAVAILABLE",
        )


def test_durable_storage_and_cleanup_internal_guards(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _attestation_fixture(tmp_settings, monkeypatch)
    transfer = fixture["journal"].get_transfer(fixture["transferId"])
    assert transfer is not None
    assert federated_dr_drill._now().utcoffset() == timedelta(0)

    invalid_target = federated_dr_drill.backup_publish.ResolvedTarget(
        target_id=TARGET_ID,
        root=tmp_settings,
        managed=False,
        kind="filesystem",
        store=fixture["store"],
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(federated_dr_drill.backup_publish, "resolve_target", lambda *_args, **_kwargs: invalid_target)
        _assert_dr_error(
            lambda: federated_dr_drill._inspect_durable_remote_documents(
                transfer=transfer,
                remote_target_id=TARGET_ID,
                replica_attestation=fixture["attestation"],
            ),
            "FEDERATED_DR_PROVIDER_TARGET_REQUIRED",
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(federated_dr_drill.backup_publish, "resolve_target", lambda *_args, **_kwargs: fixture["target"])
        scoped.setattr(fixture["store"], "get_bytes", lambda *_args, **_kwargs: None)
        _assert_dr_error(
            lambda: federated_dr_drill._inspect_durable_remote_documents(
                transfer=transfer,
                remote_target_id=TARGET_ID,
                replica_attestation=fixture["attestation"],
            ),
            "FEDERATED_DR_REMOTE_RECEIPT_MISSING",
        )

    receipt_key = federated_dr_drill.backup_target_store.receipt_key(str(transfer["backupId"]))
    with monkeypatch.context() as scoped:
        scoped.setattr(federated_dr_drill.backup_publish, "resolve_target", lambda *_args, **_kwargs: fixture["target"])
        scoped.setattr(
            fixture["store"],
            "get_bytes",
            lambda key, **_kwargs: fixture["receiptBytes"] if key == receipt_key else None,
        )
        _assert_dr_error(
            lambda: federated_dr_drill._inspect_durable_remote_documents(
                transfer=transfer,
                remote_target_id=TARGET_ID,
                replica_attestation=fixture["attestation"],
            ),
            "FEDERATED_DR_REMOTE_COMMIT_MISSING",
        )

    federated_dr_drill._cleanup_restore_session(None)
    with monkeypatch.context() as scoped:
        scoped.setattr(backup_crypto, "clear_secret", lambda _restore_id: None)
        scoped.setattr(backup_remote_restore, "read_restore_session", lambda _restore_id: None)
        federated_dr_drill._cleanup_restore_session("restore_missingcleanup")
    with monkeypatch.context() as scoped:
        scoped.setattr(
            backup_crypto,
            "clear_secret",
            lambda _restore_id: (_ for _ in ()).throw(federated_dr_drill.FederatedDrDrillError("CLEANUP_FAIL_CLOSED")),
        )
        _assert_dr_error(lambda: federated_dr_drill._cleanup_restore_session("restore_cleanupfailure"), "CLEANUP_FAIL_CLOSED")
    with monkeypatch.context() as scoped:
        scoped.setattr(backup_crypto, "clear_secret", lambda _restore_id: None)
        scoped.setattr(
            backup_remote_restore,
            "read_restore_session",
            lambda _restore_id: (_ for _ in ()).throw(OSError("journal unavailable")),
        )
        _assert_dr_error(
            lambda: federated_dr_drill._cleanup_restore_session("restore_cleanupfailure"),
            "FEDERATED_DR_CLEANUP_INCOMPLETE",
        )
    with monkeypatch.context() as scoped:
        scoped.setattr(backup_crypto, "clear_secret", lambda _restore_id: None)
        scoped.setattr(backup_remote_restore, "read_restore_session", lambda restore_id: {"restoreId": restore_id})
        scoped.setattr(
            backup_remote_restore,
            "_release_session_holds",
            lambda _session: (_ for _ in ()).throw(OSError("hold release failed")),
        )
        _assert_dr_error(
            lambda: federated_dr_drill._cleanup_restore_session("restore_cleanupfailure"),
            "FEDERATED_DR_CLEANUP_INCOMPLETE",
        )


def test_drill_result_semantics_reject_failure_time_and_rto_mismatch() -> None:
    base = {
        "result": "success",
        "restoreId": "restore_federateddr0001",
        "cleanupCompleted": True,
        "workspaceDigest": "sha256:" + ("c" * 64),
        "sourceRevision": "revision-a",
        "startedAt": (NOW + timedelta(seconds=13)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "completedAt": (NOW + timedelta(seconds=15)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "durationMs": 2_000,
    }
    cases = (
        ({"result": "failed"}, "FEDERATED_DR_PRODUCTION_RESTORE_FAILED"),
        ({"restoreId": "restore_different"}, "FEDERATED_DR_RESTORE_ID_MISMATCH"),
        (
            {"startedAt": (NOW - timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z")},
            "FEDERATED_DR_TIME_BINDING_INVALID",
        ),
        ({"durationMs": 9_000}, "FEDERATED_DR_RTO_TIME_MISMATCH"),
    )
    for changes, code in cases:
        result = {**base, **changes}
        _assert_dr_error(
            lambda result=result: federated_dr_drill._drill_evidence(
                result,
                restore_id="restore_federateddr0001",
                committed_at=NOW + timedelta(seconds=9),
                signed_at=NOW + timedelta(seconds=16),
            ),
            code,
        )


def test_producer_wraps_peer_storage_secret_and_signing_failures(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _attestation_fixture(tmp_settings, monkeypatch)
    capability = _capability(tmp_settings, fixture, monkeypatch)
    kwargs = {
        "signer": fixture["signerB"],
        "receiver": fixture["receiver"],
        "peer_registry": fixture["registry"],
        "custody_registry": capability,
        "replica_attestation": fixture["attestation"],
        "transfer_id": fixture["transferId"],
        "remote_target_id": TARGET_ID,
        "sequence": 1,
        "signed_at": NOW + timedelta(seconds=16),
        "expires_at": NOW + timedelta(seconds=76),
    }
    with monkeypatch.context() as scoped:
        scoped.setattr(
            fixture["registry"],
            "require_active_peer",
            lambda _peer: (_ for _ in ()).throw(federation_peer_trust.FederationTrustError("PEER_BLOCKED")),
        )
        _assert_dr_error(lambda: federated_dr_drill.run_federated_dr_drill(**kwargs), "PEER_BLOCKED")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            backup_remote_restore,
            "create_restore_from_target",
            lambda **_kwargs: (_ for _ in ()).throw(AppError("offline", code=ErrorCode.INVALID_REQUEST)),
        )
        _assert_dr_error(
            lambda: federated_dr_drill.run_federated_dr_drill(**kwargs),
            "FEDERATED_DR_PRODUCTION_RESTORE_FAILED",
        )

    with monkeypatch.context() as scoped:
        scoped.setattr(backup_remote_restore, "create_restore_from_target", lambda **_kwargs: {"restoreId": "invalid"})
        _assert_dr_error(
            lambda: federated_dr_drill.run_federated_dr_drill(**kwargs),
            "FEDERATED_DR_RESTORE_ID_INVALID",
        )

    captured: list[bytearray] = []
    released: list[str] = []
    with monkeypatch.context() as scoped:
        scoped.setattr(backup_remote_restore, "create_restore_from_target", lambda **_kwargs: {"restoreId": "restore_secretfailure"})
        scoped.setattr(
            backup_remote_restore,
            "read_restore_session",
            lambda _restore_id: {"restoreId": "restore_secretfailure", "targetId": TARGET_ID},
        )
        scoped.setattr(
            backup_remote_restore,
            "_release_session_holds",
            lambda session: released.append(str(session["restoreId"])),
        )

        def fail_secret(_restore_id: str, _kind: str, secret: bytearray) -> dict[str, object]:
            captured.append(secret)
            raise AppError("slot failed", code=ErrorCode.INVALID_REQUEST)

        scoped.setattr(backup_crypto, "put_secret_bytes", fail_secret)
        _assert_dr_error(
            lambda: federated_dr_drill.run_federated_dr_drill(**kwargs),
            "FEDERATED_DR_PRODUCTION_RESTORE_FAILED",
        )
    assert captured and captured[0] == bytearray(len(IDENTITY_TEXT))
    assert released == ["restore_secretfailure"]

    transfer = fixture["journal"].get_transfer(fixture["transferId"])
    assert transfer is not None
    receiver_mismatch = {**transfer, "role": federation_transfer_journal.ROLE_SENDER}
    with monkeypatch.context() as scoped:
        scoped.setattr(
            federated_dr_drill,
            "_local_replica_binding",
            lambda *_args, **_kwargs: (receiver_mismatch, fixture["attestation"]),
        )
        _assert_dr_error(
            lambda: federated_dr_drill.run_federated_dr_drill(**kwargs),
            "FEDERATED_DR_RECEIVER_IDENTITY_MISMATCH",
        )

    valid_result = {
        "result": "success",
        "restoreId": "restore_signfailure",
        "cleanupCompleted": True,
        "workspaceDigest": "sha256:" + ("c" * 64),
        "sourceRevision": "revision-a",
        "startedAt": (NOW + timedelta(seconds=13)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "completedAt": (NOW + timedelta(seconds=15)).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "durationMs": 2_000,
    }
    with monkeypatch.context() as scoped:
        scoped.setattr(backup_remote_restore, "create_restore_from_target", lambda **_kwargs: {"restoreId": "restore_signfailure"})
        scoped.setattr(backup_recovery_drill, "run_recovery_drill", lambda *_args, **_kwargs: valid_result)
        scoped.setattr(
            federation_identity,
            "sign_federation_document",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(federation_identity.FederationIdentityError("SIGN_FAILED")),
        )
        _assert_dr_error(lambda: federated_dr_drill.run_federated_dr_drill(**kwargs), "SIGN_FAILED")

    with monkeypatch.context() as scoped:
        scoped.setattr(backup_remote_restore, "create_restore_from_target", lambda **_kwargs: {"restoreId": "restore_runtimefailure"})
        scoped.setattr(
            backup_recovery_drill,
            "run_recovery_drill",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("unexpected")),
        )
        _assert_dr_error(
            lambda: federated_dr_drill.run_federated_dr_drill(**kwargs),
            "FEDERATED_DR_PRODUCTION_RESTORE_FAILED",
        )

    with monkeypatch.context() as scoped:
        active_checks = 0
        original_require_active = fixture["registry"].require_active_peer

        def revoke_during_dr(peer_fleet_id: str) -> dict[str, Any]:
            nonlocal active_checks
            active_checks += 1
            if active_checks >= 3:
                raise federation_peer_trust.FederationTrustError("PEER_REVOKED_DURING_DR")
            return original_require_active(peer_fleet_id)

        scoped.setattr(fixture["registry"], "require_active_peer", revoke_during_dr)
        scoped.setattr(backup_remote_restore, "create_restore_from_target", lambda **_kwargs: {"restoreId": "restore_peerrevoked"})
        scoped.setattr(
            backup_recovery_drill,
            "run_recovery_drill",
            lambda *_args, **_kwargs: {**valid_result, "restoreId": "restore_peerrevoked"},
        )
        _assert_dr_error(
            lambda: federated_dr_drill.run_federated_dr_drill(**kwargs),
            "PEER_REVOKED_DURING_DR",
        )
        assert active_checks == 3


def test_production_default_signing_time_is_observed_after_restore(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _attestation_fixture(tmp_settings, monkeypatch)
    capability = _capability(tmp_settings, fixture, monkeypatch)
    observed_times = iter((NOW + timedelta(seconds=12), NOW + timedelta(seconds=16)))
    monkeypatch.setattr(
        backup_remote_restore,
        "create_restore_from_target",
        lambda **_kwargs: {"restoreId": "restore_postdrillsigning"},
    )
    monkeypatch.setattr(
        backup_recovery_drill,
        "run_recovery_drill",
        lambda *_args, **_kwargs: {
            "result": "success",
            "restoreId": "restore_postdrillsigning",
            "cleanupCompleted": True,
            "workspaceDigest": "sha256:" + ("c" * 64),
            "sourceRevision": "revision-after-restore",
            "startedAt": (NOW + timedelta(seconds=13)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "completedAt": (NOW + timedelta(seconds=15)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "durationMs": 2_000,
        },
    )

    attestation = federated_dr_drill.run_federated_dr_drill(
        signer=fixture["signerB"],
        receiver=fixture["receiver"],
        peer_registry=fixture["registry"],
        custody_registry=capability,
        replica_attestation=fixture["attestation"],
        transfer_id=fixture["transferId"],
        remote_target_id=TARGET_ID,
        sequence=1,
        clock=lambda: next(observed_times),
    )

    assert attestation["signedAt"] == (NOW + timedelta(seconds=16)).isoformat(timespec="seconds").replace("+00:00", "Z")
    assert attestation["expiresAt"] == (NOW + timedelta(seconds=316)).isoformat(timespec="seconds").replace("+00:00", "Z")
    assert not backup_crypto.has_secret("restore_postdrillsigning")


def test_sender_outer_guards_and_semantic_time_checks(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture, attestation, _calls = _issue(tmp_settings, monkeypatch)
    verify_kwargs = {
        "peer_registry": fixture["registryA"],
        "sender_journal": fixture["senderJournal"],
        "now": NOW + timedelta(seconds=17),
    }
    _assert_dr_error(
        lambda: federated_dr_drill.verify_and_record_dr_drill_attestation([], **verify_kwargs),  # type: ignore[arg-type]
        "FEDERATED_DR_ATTESTATION_INVALID",
    )
    _assert_dr_error(
        lambda: federated_dr_drill.verify_and_record_dr_drill_attestation(
            {**attestation, "padding": "x" * federated_dr_drill.MAX_DR_ATTESTATION_BYTES}, **verify_kwargs
        ),
        "FEDERATED_DR_ATTESTATION_TOO_LARGE",
    )
    missing = copy.deepcopy(attestation)
    missing.pop("workspaceDigest")
    _assert_dr_error(
        lambda: federated_dr_drill.verify_and_record_dr_drill_attestation(missing, **verify_kwargs),
        "FEDERATED_DR_ATTESTATION_FIELDS_INVALID",
    )
    unknown = {**attestation, "transferId": "sha256:" + ("f" * 64)}
    _assert_dr_error(
        lambda: federated_dr_drill.verify_and_record_dr_drill_attestation(unknown, **verify_kwargs),
        "FEDERATION_TRANSFER_NOT_FOUND",
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(
            fixture["senderJournal"],
            "get_transfer",
            lambda _transfer: (_ for _ in ()).throw(federation_transfer_journal.FederatedTransferJournalError("SENDER_JOURNAL_FAILED")),
        )
        _assert_dr_error(
            lambda: federated_dr_drill.verify_and_record_dr_drill_attestation(attestation, **verify_kwargs),
            "SENDER_JOURNAL_FAILED",
        )
    _assert_dr_error(
        lambda: federated_dr_drill.verify_and_record_dr_drill_attestation(
            attestation,
            peer_registry=fixture["registry"],
            sender_journal=fixture["senderJournal"],
            now=NOW + timedelta(seconds=17),
        ),
        "FEDERATED_DR_SENDER_IDENTITY_MISMATCH",
    )
    with monkeypatch.context() as scoped:
        transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
        assert transfer is not None
        scoped.setattr(
            fixture["senderJournal"],
            "get_transfer",
            lambda _transfer: {**transfer, "state": federation_transfer_journal.STATE_TRANSFERRING},
        )
        _assert_dr_error(
            lambda: federated_dr_drill.verify_and_record_dr_drill_attestation(attestation, **verify_kwargs),
            "FEDERATED_DR_REPLICA_NOT_COMMITTED",
        )
    with monkeypatch.context() as scoped:
        scoped.setattr(fixture["registryA"], "get_replica_attestation", lambda *_args: None)
        _assert_dr_error(
            lambda: federated_dr_drill.verify_and_record_dr_drill_attestation(attestation, **verify_kwargs),
            "FEDERATED_DR_REPLICA_ATTESTATION_NOT_ACCEPTED",
        )

    certificate_missing = {**attestation, "signerCertificate": None}
    _assert_dr_error(
        lambda: federated_dr_drill._verify_signature_and_trust(
            certificate_missing,
            peer_registry=fixture["registryA"],
            destination_fleet_id="fleet-b",
            now=NOW + timedelta(seconds=17),
        ),
        "FEDERATED_DR_SIGNER_CERTIFICATE_INVALID",
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(fixture["registryA"], "get_peer", lambda _peer: None)
        _assert_dr_error(
            lambda: federated_dr_drill._verify_signature_and_trust(
                attestation,
                peer_registry=fixture["registryA"],
                destination_fleet_id="fleet-b",
                now=NOW + timedelta(seconds=17),
            ),
            "FEDERATION_PEER_NOT_PINNED",
        )
    with monkeypatch.context() as scoped:
        scoped.setattr(fixture["registryA"], "get_peer", lambda _peer: {"fleetIdentity": None})
        _assert_dr_error(
            lambda: federated_dr_drill._verify_signature_and_trust(
                attestation,
                peer_registry=fixture["registryA"],
                destination_fleet_id="fleet-b",
                now=NOW + timedelta(seconds=17),
            ),
            "FEDERATION_PEER_IDENTITY_INVALID",
        )

    transfer = fixture["senderJournal"].get_transfer(fixture["transferId"])
    replica_record = fixture["registryA"].get_replica_attestation("fleet-b", fixture["transferId"])
    assert transfer is not None and replica_record is not None
    _assert_dr_error(
        lambda: federated_dr_drill._sender_semantics(
            attestation,
            transfer={**transfer, "transferId": "sha256:" + ("e" * 64)},
            replica_record=replica_record,
            now=NOW + timedelta(seconds=17),
            max_future_skew_seconds=30,
        ),
        "FEDERATED_DR_TRANSFER_ID_MISMATCH",
    )
    derived_mismatch = {**attestation, "transferId": "sha256:" + ("e" * 64)}
    _assert_dr_error(
        lambda: federated_dr_drill._sender_semantics(
            derived_mismatch,
            transfer={**transfer, "transferId": derived_mismatch["transferId"]},
            replica_record=replica_record,
            now=NOW + timedelta(seconds=17),
            max_future_skew_seconds=30,
        ),
        "FEDERATION_TRANSFER_ID_INVALID",
    )
    _assert_dr_error(
        lambda: federated_dr_drill._sender_semantics(
            attestation,
            transfer=transfer,
            replica_record={**replica_record, "attestation": None},
            now=NOW + timedelta(seconds=17),
            max_future_skew_seconds=30,
        ),
        "FEDERATED_DR_REPLICA_ATTESTATION_NOT_ACCEPTED",
    )
    _assert_dr_error(
        lambda: federated_dr_drill._sender_semantics(
            attestation,
            transfer=transfer,
            replica_record={**replica_record, "attestationDigest": "sha256:" + ("f" * 64)},
            now=NOW + timedelta(seconds=17),
            max_future_skew_seconds=30,
        ),
        "FEDERATED_DR_REPLICA_ATTESTATION_RECORD_INVALID",
    )
    with monkeypatch.context() as scoped:
        scoped.setattr(
            federated_dr_drill,
            "replica_attestation_digest",
            lambda _attestation: (_ for _ in ()).throw(
                federated_dr_drill.federated_replica_attestation.FederatedReplicaAttestationError("DIGEST_INVALID")
            ),
        )
        _assert_dr_error(
            lambda: federated_dr_drill._sender_semantics(
                attestation,
                transfer=transfer,
                replica_record=replica_record,
                now=NOW + timedelta(seconds=17),
                max_future_skew_seconds=30,
            ),
            "FEDERATED_DR_REPLICA_ATTESTATION_RECORD_INVALID",
        )

    semantic_cases = (
        (
            attestation,
            NOW + timedelta(seconds=77),
            30,
            "FEDERATED_DR_ATTESTATION_EXPIRED",
        ),
        (
            {
                **attestation,
                "signedAt": (NOW + timedelta(seconds=60)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "expiresAt": (NOW + timedelta(seconds=120)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            },
            NOW + timedelta(seconds=17),
            30,
            "FEDERATED_DR_ATTESTATION_FROM_FUTURE",
        ),
        (
            {
                **attestation,
                "startedAt": (NOW - timedelta(seconds=1)).isoformat(timespec="seconds").replace("+00:00", "Z"),
            },
            NOW + timedelta(seconds=17),
            30,
            "FEDERATED_DR_TIME_BINDING_INVALID",
        ),
        (
            {**attestation, "rtoMs": 9_000},
            NOW + timedelta(seconds=17),
            30,
            "FEDERATED_DR_RTO_TIME_MISMATCH",
        ),
        (
            {
                **attestation,
                "signerCertificate": {
                    **attestation["signerCertificate"],
                    "expiresAt": (NOW + timedelta(seconds=30)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                },
            },
            NOW + timedelta(seconds=17),
            30,
            "FEDERATED_DR_SIGNER_WINDOW_INVALID",
        ),
    )
    for candidate, current, skew, code in semantic_cases:
        _assert_dr_error(
            lambda candidate=candidate, current=current, skew=skew: federated_dr_drill._sender_semantics(
                candidate,
                transfer=transfer,
                replica_record=replica_record,
                now=current,
                max_future_skew_seconds=skew,
            ),
            code,
        )
