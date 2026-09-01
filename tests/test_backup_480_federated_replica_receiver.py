from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.infra.workspace import (
    backup_object_set,
    backup_publish,
    federation_challenge,
    federation_identity,
    federation_ingress_grant,
    federation_peer_trust,
    federation_replica_receiver,
    federation_transfer,
    federation_transfer_journal,
)


UTC = timezone.utc
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
POLICY_ID = "policy-federated-replica"
BACKUP_ID = "backup-20260901-replica"


def _metadata(*, region: str) -> dict[str, str]:
    return {
        "provider": "operator-known-provider",
        "region": region,
        "jurisdiction": "CN",
        "siteClass": "independent-datacenter",
    }


def _component(root: Path, component_id: str, content: bytes, *, control: bool = False) -> backup_object_set.EncryptedComponent:
    path = root / f"{component_id}.age"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return backup_object_set.EncryptedComponent(
        component_id=component_id,
        path=path,
        ciphertext_digest=hashlib.sha256(content).hexdigest(),
        ciphertext_size=len(content),
        control=control,
    )


def _fixture(
    tmp_path: Path,
    *,
    payload_content: bytes = b"randomized-age-payload-ciphertext",
    grant_max_bytes: int | None = None,
) -> dict[str, Any]:
    root_a_path = tmp_path / "fleet-a" / "root.bundle.json"
    root_b_path = tmp_path / "fleet-b" / "root.bundle.json"
    root_a_passphrase = b"fleet-a-root-passphrase-replica"
    root_b_passphrase = b"fleet-b-root-passphrase-replica"
    identity_a = federation_identity.create_fleet_root(
        "fleet-a",
        bundle_path=root_a_path,
        passphrase=root_a_passphrase,
        now=NOW - timedelta(hours=2),
    )
    identity_b = federation_identity.create_fleet_root(
        "fleet-b",
        bundle_path=root_b_path,
        passphrase=root_b_passphrase,
        now=NOW - timedelta(hours=2),
    )
    signer_a_path = tmp_path / "fleet-a" / "signer.bundle.json"
    signer_b_path = tmp_path / "fleet-b" / "signer.bundle.json"
    certificate_a = federation_identity.issue_online_signer(
        root_bundle_path=root_a_path,
        root_passphrase=root_a_passphrase,
        signer_bundle_path=signer_a_path,
        signer_passphrase=b"fleet-a-signer-passphrase-replica",
        sequence=1,
        not_before=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
        purposes=(federation_identity.PURPOSE_SESSION_AUTHENTICATION,),
    )
    certificate_b = federation_identity.issue_online_signer(
        root_bundle_path=root_b_path,
        root_passphrase=root_b_passphrase,
        signer_bundle_path=signer_b_path,
        signer_passphrase=b"fleet-b-signer-passphrase-replica",
        sequence=1,
        not_before=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
        purposes=(
            federation_identity.PURPOSE_SESSION_AUTHENTICATION,
            federation_identity.PURPOSE_INGRESS_GRANT,
            federation_identity.PURPOSE_REPLICA_ATTESTATION,
        ),
    )
    signer_a = federation_identity.load_online_signer(
        signer_a_path,
        b"fleet-a-signer-passphrase-replica",
        root_identity=identity_a,
        now=NOW,
    )
    signer_b = federation_identity.load_online_signer(
        signer_b_path,
        b"fleet-b-signer-passphrase-replica",
        root_identity=identity_b,
        now=NOW,
    )
    registry_a = federation_peer_trust.PeerTrustRegistry(tmp_path / "fleet-a" / "trust.sqlite3", identity_a)
    registry_b = federation_peer_trust.PeerTrustRegistry(tmp_path / "fleet-b" / "trust.sqlite3", identity_b)
    for registry, peer_identity, peer_fleet, region, actor in (
        (registry_a, identity_b, "fleet-b", "cn-south-1", "operator-a"),
        (registry_b, identity_a, "fleet-a", "cn-north-1", "operator-b"),
    ):
        registry.pin_peer(
            peer_identity,
            expected_root_fingerprint=str(peer_identity["rootFingerprint"]),
            metadata=_metadata(region=region),
            operator_id=actor,
            now=NOW - timedelta(minutes=30),
        )
        registry.verify_peer(peer_fleet, peer_identity, actor=actor, now=NOW - timedelta(minutes=29))
        registry.activate_peer(peer_fleet, actor=actor, now=NOW - timedelta(minutes=28))
    registry_a.accept_online_signer("fleet-b", certificate_b, actor="operator-a", now=NOW)
    registry_b.accept_online_signer("fleet-a", certificate_a, actor="operator-b", now=NOW)

    challenge = federation_challenge.issue_federation_challenge(
        peer_registry=registry_a,
        challenger_signer=signer_a,
        destination_fleet_id="fleet-b",
        session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
        now=NOW,
    )
    federation_challenge.respond_to_federation_challenge(
        challenge,
        peer_registry=registry_b,
        responder_signer=signer_b,
        now=NOW + timedelta(seconds=1),
    )

    source_dir = tmp_path / "fleet-a" / "ciphertext"
    control = _component(source_dir, "control", b"randomized-age-control-ciphertext", control=True)
    payload = _component(source_dir, "payload-0000", payload_content)
    package = backup_object_set.ObjectSetPackage(
        backup_id=BACKUP_ID,
        components=(control, payload),
        manifest_digest="a" * 64,
        coverage_digest="b" * 64,
        manifest={"snapshotKind": "full"},
    )
    source_receipt = backup_publish.receipt_for(
        package,
        run_id="source-run",
        policy_id=POLICY_ID,
        target_id="fleet-a-primary",
        schedule_slot="source-slot",
    )
    federation_digest = "sha256:" + package.object_set_digest
    transfer_id = federation_transfer.derive_transfer_id(
        source_fleet_id="fleet-a",
        destination_fleet_id="fleet-b",
        backup_id=BACKUP_ID,
        object_set_digest=federation_digest,
    )
    journal_b = federation_transfer_journal.FederatedTransferJournal(
        tmp_path / "fleet-b" / "transfers.sqlite3",
        identity_b,
    )
    federation_transfer.accept_or_resume_transfer(
        journal=journal_b,
        transfer_id=transfer_id,
        source_fleet_id="fleet-a",
        destination_fleet_id="fleet-b",
        policy_id=POLICY_ID,
        backup_id=BACKUP_ID,
        object_set_digest=federation_digest,
        now=NOW + timedelta(seconds=1),
    )
    prefix = f"federation/fleet-a/{transfer_id}/"
    grant = federation_ingress_grant.issue_ingress_grant(
        peer_registry=registry_b,
        receiver_signer=signer_b,
        source_fleet_id="fleet-a",
        session_nonce=str(challenge["nonce"]),
        transfer_id=transfer_id,
        policy_id=POLICY_ID,
        backup_id=BACKUP_ID,
        object_set_digest=federation_digest,
        allowed_object_prefix=prefix,
        max_bytes=package.size if grant_max_bytes is None else grant_max_bytes,
        now=NOW + timedelta(seconds=2),
        expires_at=NOW + timedelta(minutes=1),
    )
    receiver = federation_replica_receiver.FederatedReplicaReceiver(
        transfer_journal=journal_b,
        peer_registry=registry_b,
        db_path=tmp_path / "fleet-b" / "replica-receiver.sqlite3",
        staging_dir=tmp_path / "fleet-b" / "replica-staging",
    )
    return {
        "identityA": identity_a,
        "identityB": identity_b,
        "journal": journal_b,
        "registryA": registry_a,
        "registry": registry_b,
        "signerB": signer_b,
        "certificateB": certificate_b,
        "receiver": receiver,
        "grant": grant,
        "package": package,
        "receipt": source_receipt,
        "transferId": transfer_id,
        "federationDigest": federation_digest,
        "dbPath": tmp_path / "fleet-b" / "replica-receiver.sqlite3",
        "stagingDir": tmp_path / "fleet-b" / "replica-staging",
    }


def _receive_all(fixture: dict[str, Any]) -> None:
    receiver = fixture["receiver"]
    package = fixture["package"]
    for index, component in enumerate(package.components):
        receiver.receive_component(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            component_digest=component.ciphertext_digest,
            write_id=f"write-{index:04d}",
            content=io.BytesIO(component.path.read_bytes()),
            now=NOW + timedelta(seconds=3 + index),
        )


def test_receiver_binds_typed_federation_digest_to_unchanged_object_set_v1(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receiver = fixture["receiver"]

    declaration = receiver.declare_object_set(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        source_receipt=fixture["receipt"],
        now=NOW + timedelta(seconds=3),
    )

    assert declaration["storageProtocol"] == backup_object_set.OBJECT_SET_V1
    assert declaration["objectSetDigest"] == fixture["federationDigest"]
    assert declaration["storageObjectSetDigest"] == fixture["package"].object_set_digest
    assert declaration["objects"] == backup_object_set.remote_object_inventory(fixture["package"].components)
    assert declaration["controlObjectDigest"] == fixture["package"].control.ciphertext_digest
    assert declaration["totalBytes"] == fixture["package"].size
    assert federation_replica_receiver.storage_object_set_digest(fixture["federationDigest"]) == fixture["package"].object_set_digest
    assert federation_replica_receiver.federation_object_set_digest(fixture["package"].object_set_digest) == fixture["federationDigest"]
    assert receiver.get_declaration(fixture["transferId"]) == declaration
    assert receiver.db_path == fixture["dbPath"]
    assert receiver.staging_dir == fixture["stagingDir"]


@pytest.mark.parametrize(
    ("field", "value", "code"),
    (
        ("schemaVersion", 3, "FEDERATION_REPLICA_RECEIPT_VERSION_INVALID"),
        ("storageProtocol", "object-set-v2", "FEDERATION_REPLICA_STORAGE_PROTOCOL_INVALID"),
        ("objectSetDigest", "f" * 64, "FEDERATION_REPLICA_OBJECT_SET_DIGEST_MISMATCH"),
        ("controlObjectDigest", "f" * 64, "FEDERATION_REPLICA_CONTROL_OBJECT_INVALID"),
    ),
)
def test_receiver_rejects_source_receipt_wire_or_binding_changes(
    tmp_path: Path,
    field: str,
    value: object,
    code: str,
) -> None:
    fixture = _fixture(tmp_path)
    receipt = dict(fixture["receipt"])
    receipt[field] = value

    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as caught:
        fixture["receiver"].declare_object_set(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            source_receipt=receipt,
            now=NOW + timedelta(seconds=3),
        )

    assert caught.value.code == code


def test_component_write_is_digest_verified_idempotent_and_replay_fenced(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receiver = fixture["receiver"]
    receiver.declare_object_set(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        source_receipt=fixture["receipt"],
        now=NOW + timedelta(seconds=3),
    )
    component = fixture["package"].control
    content = component.path.read_bytes()

    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as caught:
        receiver.receive_component(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            component_digest=component.ciphertext_digest,
            write_id="write-control",
            content=io.BytesIO(content + b"tamper"),
            now=NOW + timedelta(seconds=4),
        )
    assert caught.value.code == "FEDERATION_REPLICA_COMPONENT_SIZE_MISMATCH"
    assert receiver.list_components(fixture["transferId"])[0]["receivedAt"] is None

    first = receiver.receive_component(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        component_digest=component.ciphertext_digest,
        write_id="write-control",
        content=io.BytesIO(content),
        now=NOW + timedelta(seconds=5),
    )
    repeated = receiver.receive_component(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        component_digest=component.ciphertext_digest,
        write_id="write-control",
        content=io.BytesIO(content),
        now=NOW + timedelta(seconds=6),
    )
    assert repeated == first
    assert repeated["ciphertextDigest"] == component.ciphertext_digest
    assert repeated["ciphertextSize"] == len(content)
    assert len(fixture["registry"].list_ingress_writes(str(fixture["grant"]["grantId"]))) == 1

    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as replayed:
        receiver.receive_component(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            component_digest=component.ciphertext_digest,
            write_id="write-control-replay",
            content=io.BytesIO(content),
            now=NOW + timedelta(seconds=7),
        )
    assert replayed.value.code == "FEDERATION_REPLICA_COMPONENT_WRITE_REPLAY"
    assert len(fixture["registry"].list_ingress_writes(str(fixture["grant"]["grantId"]))) == 1


def test_receiver_helper_boundaries_fail_closed() -> None:
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as typed:
        federation_replica_receiver.storage_object_set_digest("2" * 64)
    assert typed.value.code == "FEDERATION_REPLICA_OBJECT_SET_DIGEST_INVALID"
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as plain:
        federation_replica_receiver.federation_object_set_digest("sha256:" + ("2" * 64))
    assert plain.value.code == "FEDERATION_REPLICA_STORAGE_OBJECT_SET_DIGEST_INVALID"
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as timestamp:
        federation_replica_receiver._utc_iso(datetime(2026, 9, 1, 8, 0))
    assert timestamp.value.code == "FEDERATION_REPLICA_TIMESTAMP_INVALID"
    for value in (None, "not-a-time", "2026-09-01T08:00:00"):
        with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as parsed:
            federation_replica_receiver._parse_timestamp(value)
        assert parsed.value.code == "FEDERATION_REPLICA_TIMESTAMP_INVALID"
    for helper in (federation_replica_receiver._canonical_json, federation_replica_receiver._normalize):
        with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as canonical:
            helper({"notCanonical": float("nan")})
        assert canonical.value.code == "FEDERATION_REPLICA_CANONICAL_PAYLOAD_INVALID"


def test_declaration_is_idempotent_but_semantic_rebinding_fails_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receiver = fixture["receiver"]
    first = receiver.declare_object_set(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        source_receipt=fixture["receipt"],
        now=NOW + timedelta(seconds=3),
    )
    repeated = receiver.declare_object_set(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        source_receipt=fixture["receipt"],
        now=NOW + timedelta(seconds=4),
    )
    assert repeated == first

    rebound = dict(fixture["receipt"])
    rebound["snapshotKind"] = "incremental"
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as conflict:
        receiver.declare_object_set(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            source_receipt=rebound,
            now=NOW + timedelta(seconds=5),
        )
    assert conflict.value.code == "FEDERATION_REPLICA_DECLARATION_IDENTITY_CONFLICT"


def test_declaration_rejects_unverified_identity_size_and_lineage_claims(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    cases = (
        ({"backupId": "another-backup"}, "FEDERATION_REPLICA_RECEIPT_IDENTITY_MISMATCH"),
        ({"creationVerified": False}, "FEDERATION_REPLICA_SOURCE_CREATION_UNVERIFIED"),
        ({"size": fixture["package"].size + 1}, "FEDERATION_REPLICA_RECEIPT_SIZE_MISMATCH"),
        ({"snapshotKind": ""}, "FEDERATION_REPLICA_SNAPSHOT_KIND_INVALID"),
        ({"chainDepth": True}, "FEDERATION_REPLICA_LINEAGE_INVALID"),
        ({"lineageId": ""}, "FEDERATION_REPLICA_LINEAGE_INVALID"),
    )
    for changes, code in cases:
        receipt = dict(fixture["receipt"])
        receipt.update(changes)
        with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as caught:
            fixture["receiver"].declare_object_set(
                grant=fixture["grant"],
                transfer_id=fixture["transferId"],
                source_receipt=receipt,
                now=NOW + timedelta(seconds=3),
            )
        assert caught.value.code == code


def test_component_rejects_undeclared_digest_tamper_and_invalid_stream(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receiver = fixture["receiver"]
    receiver.declare_object_set(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        source_receipt=fixture["receipt"],
        now=NOW + timedelta(seconds=3),
    )
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as missing:
        receiver.receive_component(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            component_digest="c" * 64,
            write_id="write-missing",
            content=b"not-declared",
            now=NOW + timedelta(seconds=4),
        )
    assert missing.value.code == "FEDERATION_REPLICA_COMPONENT_NOT_DECLARED"

    component = fixture["package"].control
    same_size_tamper = b"x" * component.ciphertext_size
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as tampered:
        receiver.receive_component(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            component_digest=component.ciphertext_digest,
            write_id="write-tampered",
            content=bytearray(same_size_tamper),
            now=NOW + timedelta(seconds=5),
        )
    assert tampered.value.code == "FEDERATION_REPLICA_COMPONENT_DIGEST_MISMATCH"

    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as stream:
        receiver.receive_component(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            component_digest=fixture["package"].components[1].ciphertext_digest,
            write_id="write-invalid-stream",
            content=object(),
            now=NOW + timedelta(seconds=6),
        )
    assert stream.value.code == "FEDERATION_REPLICA_COMPONENT_STREAM_INVALID"


def test_staged_ciphertext_tamper_is_detected_before_object_set_assembly(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    fixture["receiver"].declare_object_set(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        source_receipt=fixture["receipt"],
        now=NOW + timedelta(seconds=3),
    )
    _receive_all(fixture)
    control_record = fixture["receiver"].list_components(fixture["transferId"])[0]
    transfer_hex = str(fixture["transferId"]).removeprefix("sha256:")
    digest = str(control_record["ciphertextDigest"])
    staged = fixture["stagingDir"] / transfer_hex / "objects" / "sha256" / digest[:2] / f"{digest}.age"
    staged.write_bytes(b"tampered-stage")

    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as caught:
        fixture["receiver"].assemble_verified_package(fixture["transferId"])
    assert caught.value.code == "FEDERATION_REPLICA_STAGED_COMPONENT_INVALID"


def test_receiver_and_transfer_journal_are_bound_to_the_same_sovereign_fleet(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as local_conflict:
        federation_replica_receiver.FederatedReplicaReceiver(
            transfer_journal=fixture["journal"],
            peer_registry=fixture["registryA"],
            db_path=tmp_path / "mixed-receiver.sqlite3",
            staging_dir=tmp_path / "mixed-staging",
        )
    assert local_conflict.value.code == "FEDERATION_REPLICA_LOCAL_IDENTITY_CONFLICT"

    journal_a = federation_transfer_journal.FederatedTransferJournal(
        tmp_path / "fleet-a" / "transfers.sqlite3",
        fixture["identityA"],
    )
    federation_transfer.accept_or_resume_transfer(
        journal=journal_a,
        transfer_id=fixture["transferId"],
        source_fleet_id="fleet-a",
        destination_fleet_id="fleet-b",
        policy_id=POLICY_ID,
        backup_id=BACKUP_ID,
        object_set_digest=fixture["federationDigest"],
        now=NOW + timedelta(seconds=1),
    )
    sender_side = federation_replica_receiver.FederatedReplicaReceiver(
        transfer_journal=journal_a,
        peer_registry=fixture["registryA"],
        db_path=tmp_path / "sender-receiver.sqlite3",
        staging_dir=tmp_path / "sender-staging",
    )
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as wrong_role:
        sender_side.get_declaration(fixture["transferId"])
    assert wrong_role.value.code == "FEDERATION_REPLICA_RECEIVER_FLEET_MISMATCH"

    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as rebound_db:
        federation_replica_receiver.FederatedReplicaReceiver(
            transfer_journal=journal_a,
            peer_registry=fixture["registryA"],
            db_path=fixture["dbPath"],
            staging_dir=fixture["stagingDir"],
        )
    assert rebound_db.value.code == "FEDERATION_REPLICA_RECEIVER_IDENTITY_CONFLICT"


def test_receiver_requires_an_existing_receiver_transfer_record(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as malformed:
        fixture["receiver"].get_declaration("not-a-transfer-id")
    assert malformed.value.code == "FEDERATION_TRANSFER_ID_INVALID"
    absent = "sha256:" + ("f" * 64)
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as missing:
        fixture["receiver"].get_declaration(absent)
    assert missing.value.code == "FEDERATION_TRANSFER_NOT_FOUND"
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as undeclared:
        fixture["receiver"].assemble_verified_package(fixture["transferId"])
    assert undeclared.value.code == "FEDERATION_REPLICA_DECLARATION_NOT_FOUND"


def test_receiver_rejects_unknown_tampered_future_expired_and_revoked_grants(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receiver = fixture["receiver"]
    invalid_grants: tuple[tuple[dict[str, Any], datetime, str], ...] = (
        ({}, NOW + timedelta(seconds=3), "FEDERATION_INGRESS_GRANT_DOCUMENT_INVALID"),
        ({**fixture["grant"], "grantId": "grant-" + ("0" * 32)}, NOW + timedelta(seconds=3), "FEDERATION_INGRESS_GRANT_NOT_FOUND"),
        ({**fixture["grant"], "policyId": "tampered-policy"}, NOW + timedelta(seconds=3), "FEDERATION_INGRESS_GRANT_IDENTITY_CONFLICT"),
        (fixture["grant"], NOW + timedelta(seconds=1), "FEDERATION_INGRESS_GRANT_FROM_FUTURE"),
        (fixture["grant"], NOW + timedelta(minutes=1), "FEDERATION_INGRESS_GRANT_EXPIRED"),
    )
    for grant, now, code in invalid_grants:
        with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as caught:
            receiver.declare_object_set(
                grant=grant,
                transfer_id=fixture["transferId"],
                source_receipt=fixture["receipt"],
                now=now,
            )
        assert caught.value.code == code

    fixture["registry"].revoke_peer(
        "fleet-a",
        actor="operator-b",
        reason="incident",
        now=NOW + timedelta(seconds=4),
    )
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as revoked:
        receiver.declare_object_set(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            source_receipt=fixture["receipt"],
            now=NOW + timedelta(seconds=5),
        )
    assert revoked.value.code == "FEDERATION_PEER_REVOKED"


def test_receiver_rejects_non_document_invalid_inventory_zero_component_and_small_grant(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as document:
        fixture["receiver"].declare_object_set(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            source_receipt=[],
            now=NOW + timedelta(seconds=3),
        )
    assert document.value.code == "FEDERATION_REPLICA_SOURCE_RECEIPT_INVALID"

    invalid_inventory = dict(fixture["receipt"])
    invalid_inventory["objects"] = [dict(item) for item in fixture["receipt"]["objects"]]
    invalid_inventory["objects"][1]["size"] = -1
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as inventory:
        fixture["receiver"].declare_object_set(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            source_receipt=invalid_inventory,
            now=NOW + timedelta(seconds=3),
        )
    assert inventory.value.code == "FEDERATION_REPLICA_OBJECT_INVENTORY_INVALID"

    zero = _fixture(tmp_path / "zero", payload_content=b"")
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as empty:
        zero["receiver"].declare_object_set(
            grant=zero["grant"],
            transfer_id=zero["transferId"],
            source_receipt=zero["receipt"],
            now=NOW + timedelta(seconds=3),
        )
    assert empty.value.code == "FEDERATION_REPLICA_COMPONENT_SIZE_INVALID"

    limited = _fixture(tmp_path / "limited", grant_max_bytes=10)
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as quota:
        limited["receiver"].declare_object_set(
            grant=limited["grant"],
            transfer_id=limited["transferId"],
            source_receipt=limited["receipt"],
            now=NOW + timedelta(seconds=3),
        )
    assert quota.value.code == "FEDERATION_INGRESS_MAX_BYTES_EXCEEDED"


def test_receiver_bounds_source_receipt_and_component_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path)
    oversized = dict(fixture["receipt"])
    oversized["ignoredExtension"] = "x" * 256
    monkeypatch.setattr(federation_replica_receiver, "MAX_SOURCE_RECEIPT_BYTES", 128)
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as receipt_limit:
        fixture["receiver"].declare_object_set(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            source_receipt=oversized,
            now=NOW + timedelta(seconds=3),
        )
    assert receipt_limit.value.code == "FEDERATION_REPLICA_SOURCE_RECEIPT_TOO_LARGE"

    monkeypatch.setattr(federation_replica_receiver, "MAX_SOURCE_RECEIPT_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(federation_replica_receiver, "MAX_REPLICA_COMPONENTS", 1)
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as component_limit:
        fixture["receiver"].declare_object_set(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            source_receipt=fixture["receipt"],
            now=NOW + timedelta(seconds=3),
        )
    assert component_limit.value.code == "FEDERATION_REPLICA_COMPONENT_COUNT_INVALID"


def test_component_reservation_and_staging_conflicts_fail_closed(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receiver = fixture["receiver"]
    receiver.declare_object_set(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        source_receipt=fixture["receipt"],
        now=NOW + timedelta(seconds=3),
    )
    component = fixture["package"].control
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as short:
        receiver.receive_component(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            component_digest=component.ciphertext_digest,
            write_id="write-reserved",
            content=b"short",
            now=NOW + timedelta(seconds=4),
        )
    assert short.value.code == "FEDERATION_REPLICA_COMPONENT_SIZE_MISMATCH"
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as replay:
        receiver.receive_component(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            component_digest=component.ciphertext_digest,
            write_id="write-after-reservation",
            content=component.path.read_bytes(),
            now=NOW + timedelta(seconds=5),
        )
    assert replay.value.code == "FEDERATION_REPLICA_COMPONENT_WRITE_REPLAY"

    received = receiver.receive_component(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        component_digest=component.ciphertext_digest,
        write_id="write-reserved",
        content=component.path.read_bytes(),
        now=NOW + timedelta(seconds=6),
    )
    staged = Path(str(received["objectKey"]))
    assert staged.as_posix().startswith("federation/")
    transfer_hex = str(fixture["transferId"]).removeprefix("sha256:")
    local_path = fixture["stagingDir"] / transfer_hex / "objects" / "sha256" / component.ciphertext_digest[:2] / f"{component.ciphertext_digest}.age"
    local_path.write_bytes(b"corrupt")
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as conflict:
        receiver.receive_component(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            component_digest=component.ciphertext_digest,
            write_id="write-reserved",
            content=component.path.read_bytes(),
            now=NOW + timedelta(seconds=7),
        )
    assert conflict.value.code == "FEDERATION_REPLICA_STAGED_COMPONENT_CONFLICT"


def test_assembly_rejects_durable_declaration_commitment_tamper(tmp_path: Path) -> None:
    for field, value in (
        ("storageObjectSetDigest", "f" * 64),
        ("controlObjectDigest", None),
    ):
        fixture = _fixture(tmp_path / field)
        fixture["receiver"].declare_object_set(
            grant=fixture["grant"],
            transfer_id=fixture["transferId"],
            source_receipt=fixture["receipt"],
            now=NOW + timedelta(seconds=3),
        )
        _receive_all(fixture)
        with closing(sqlite3.connect(fixture["dbPath"])) as connection:
            row = connection.execute(
                "SELECT declaration_json FROM federated_replica_declarations WHERE transfer_id = ?",
                (fixture["transferId"],),
            ).fetchone()
            assert row is not None
            declaration = json.loads(str(row[0]))
            declaration[field] = value
            connection.execute(
                "UPDATE federated_replica_declarations SET declaration_json = ? WHERE transfer_id = ?",
                (federation_identity.canonical_federation_json(declaration), fixture["transferId"]),
            )
            connection.commit()
        with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as caught:
            fixture["receiver"].assemble_verified_package(fixture["transferId"])
        assert caught.value.code == "FEDERATION_REPLICA_DECLARATION_COMMITMENT_INVALID"


def test_open_receiver_and_incremental_receipt_manifest(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)
    receipt = dict(fixture["receipt"])
    receipt.update(
        snapshotKind="incremental",
        lineageId="lineage-1",
        parentBackupId="backup-parent",
        baseBackupId="backup-base",
        chainDepth=1,
        chunkProtocol="fastcdc-v3",
    )
    opened = federation_replica_receiver.open_federated_replica_receiver(
        transfer_journal=fixture["journal"],
        peer_registry=fixture["registry"],
        db_path=fixture["dbPath"],
        staging_dir=fixture["stagingDir"],
    )
    declaration = opened.declare_object_set(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        source_receipt=receipt,
        now=NOW + timedelta(seconds=3),
    )
    assert declaration["receiptManifest"]["chunkProtocol"] == "fastcdc-v3"


def test_receiver_restart_assembles_same_ciphertext_without_reencryption(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    receiver = fixture["receiver"]
    receiver.declare_object_set(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        source_receipt=fixture["receipt"],
        now=NOW + timedelta(seconds=3),
    )
    first_component = fixture["package"].control
    receiver.receive_component(
        grant=fixture["grant"],
        transfer_id=fixture["transferId"],
        component_digest=first_component.ciphertext_digest,
        write_id="write-0000",
        content=io.BytesIO(first_component.path.read_bytes()),
        now=NOW + timedelta(seconds=4),
    )
    with pytest.raises(federation_replica_receiver.FederatedReplicaReceiverError) as incomplete:
        receiver.assemble_verified_package(fixture["transferId"])
    assert incomplete.value.code == "FEDERATION_REPLICA_OBJECT_SET_INCOMPLETE"

    reopened_registry = federation_peer_trust.PeerTrustRegistry(
        fixture["registry"].db_path,
        fixture["registry"].local_identity,
    )
    reopened_journal = federation_transfer_journal.FederatedTransferJournal(
        fixture["journal"].db_path,
        fixture["identityB"],
    )
    reopened = federation_replica_receiver.FederatedReplicaReceiver(
        transfer_journal=reopened_journal,
        peer_registry=reopened_registry,
        db_path=fixture["dbPath"],
        staging_dir=fixture["stagingDir"],
    )
    fixture["receiver"] = reopened
    _receive_all(fixture)

    assembled = reopened.assemble_verified_package(fixture["transferId"])

    assert assembled.storage_protocol == backup_object_set.OBJECT_SET_V1
    assert assembled.backup_id == BACKUP_ID
    assert assembled.object_set_digest == fixture["package"].object_set_digest
    assert assembled.control.ciphertext_digest == fixture["package"].control.ciphertext_digest
    assert [component.path.read_bytes() for component in assembled.components] == [
        component.path.read_bytes() for component in fixture["package"].components
    ]
    assert all(component.path.parent.is_relative_to(fixture["stagingDir"]) for component in assembled.components)
