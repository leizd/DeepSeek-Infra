from __future__ import annotations

import base64
import io
from datetime import timedelta
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import (
    backup_publish,
    backup_targets,
    federated_durability,
    federation_custody_capability,
    federation_identity,
    federation_node,
    federation_peer_trust,
    federation_replica_receiver,
    federation_transfer_journal,
)
from tests.test_backup_480_federated_replica_commit import TARGET_ID, _memory_target
from tests.test_backup_480_federated_replica_receiver import NOW, _fixture, _metadata


def _node(
    root: Path,
    *,
    identity: dict[str, object],
    signer: federation_identity.OnlineFleetSigner,
    registry: federation_peer_trust.PeerTrustRegistry,
    journal: federation_transfer_journal.FederatedTransferJournal,
    receiver: federation_replica_receiver.FederatedReplicaReceiver,
) -> federation_node.FederationNode:
    return federation_node.FederationNode(
        identity=identity,
        signer=signer,
        peer_registry=registry,
        transfer_journal=journal,
        receiver=receiver,
        durability_ledger=federated_durability.FederatedDurabilityLedger(root / "durability.sqlite3", identity),
        custody_registry=federation_custody_capability.FederationCustodyCapabilityRegistry(
            root / "custody.sqlite3",
            identity,
        ),
        state_db_path=root / "node.sqlite3",
        remote_target_id=TARGET_ID,
        failure_domain_metadata=_metadata(region="cn-south-1"),
        readiness={
            "wireCompatibility": ["object-set-v1", "receipt-v4", "commit-v4", "fastcdc-v3"],
            "availableFailureDomains": ["cn-south-1"],
            "forecastHeadroom": 1024,
            "costClass": "standard",
            "readiness": "READY",
        },
        max_ingress_bytes=64 * 1024 * 1024,
        owner_instance_id="fleet-node-test-worker",
        clock=lambda: NOW + timedelta(seconds=10),
    )


def test_node_receiver_commits_once_and_reconciles_exact_documents_after_reopen(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_settings)
    node = _node(
        tmp_settings / "fleet-b" / "node-state",
        identity=fixture["identityB"],
        signer=fixture["signerB"],
        registry=fixture["registry"],
        journal=fixture["journal"],
        receiver=fixture["receiver"],
    )
    target, store = _memory_target()
    monkeypatch.setattr(backup_publish, "resolve_target", lambda target_id, **_kwargs: target if target_id == TARGET_ID else None)
    monkeypatch.setattr(
        backup_publish,
        "_utc_iso",
        lambda: (NOW + timedelta(seconds=9)).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )

    resumed_grant = node.issue_ingress_grant(
        {
            "sourceFleetId": "fleet-a",
            "sessionNonce": fixture["challenge"]["nonce"],
            "transferId": fixture["transferId"],
            "policyId": fixture["receipt"]["policyId"],
            "backupId": fixture["receipt"]["backupId"],
            "objectSetDigest": fixture["federationDigest"],
            "totalBytes": fixture["package"].size,
        }
    )
    assert resumed_grant == fixture["grant"]

    declaration = node.declare_replica(
        fixture["transferId"],
        {"grantId": fixture["grant"]["grantId"], "sourceReceipt": fixture["receipt"]},
    )
    assert declaration["storageProtocol"] == "object-set-v1"
    for index, component in enumerate(fixture["package"].components):
        assert node.expected_component_size(
            fixture["transferId"], component.ciphertext_digest, fixture["grant"]["grantId"]
        ) == component.ciphertext_size
        node.receive_component(
            fixture["transferId"],
            component.ciphertext_digest,
            grant_id=fixture["grant"]["grantId"],
            write_id=f"node-write-{index}",
            content=io.BytesIO(component.path.read_bytes()),
        )

    committed = node.commit_replica(fixture["transferId"], {"grantId": fixture["grant"]["grantId"]})
    assert committed["attestation"]["schema"] == "federated-replica-attestation-v1"
    assert committed["receipt"]["schemaVersion"] == 4
    assert committed["commit"]["schemaVersion"] == 4
    assert base64.b64decode(committed["remoteReceiptBase64"], validate=True).endswith(b"\n")
    assert base64.b64decode(committed["remoteCommitBase64"], validate=True).endswith(b"\n")
    assert committed["reconciled"] is False

    repeated = node.commit_replica(fixture["transferId"], {"grantId": fixture["grant"]["grantId"]})
    assert repeated == committed
    assert len(
        [
            event
            for event in fixture["journal"].list_transfer_events(fixture["transferId"])
            if event["nextState"] == federation_transfer_journal.STATE_REMOTE_COMMITTED
        ]
    ) == 1
    assert len(store.list_objects("receipts/").objects) == 1

    reopened = _node(
        tmp_settings / "fleet-b" / "node-state",
        identity=fixture["identityB"],
        signer=fixture["signerB"],
        registry=fixture["registry"],
        journal=fixture["journal"],
        receiver=federation_replica_receiver.FederatedReplicaReceiver(
            transfer_journal=fixture["journal"],
            peer_registry=fixture["registry"],
            db_path=fixture["dbPath"],
            staging_dir=fixture["stagingDir"],
        ),
    )
    reconciled = reopened.reconcile_transfer(fixture["transferId"], fixture["grant"]["grantId"])
    assert reconciled["status"] == "RESUME"
    assert reconciled["state"] == federation_transfer_journal.STATE_REMOTE_COMMITTED
    assert reconciled["committedEffect"] == committed


def test_sender_node_verifies_grant_documents_attestation_and_records_zero_local_credit(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_settings)
    signer_a = federation_identity.load_online_signer(
        tmp_settings / "fleet-a" / "signer.bundle.json",
        b"fleet-a-signer-passphrase-replica",
        root_identity=fixture["identityA"],
        now=NOW,
    )
    journal_a = federation_transfer_journal.FederatedTransferJournal(
        tmp_settings / "fleet-a" / "node-transfers.sqlite3",
        fixture["identityA"],
    )
    receiver_a = federation_replica_receiver.FederatedReplicaReceiver(
        transfer_journal=journal_a,
        peer_registry=fixture["registryA"],
        db_path=tmp_settings / "fleet-a" / "receiver.sqlite3",
        staging_dir=tmp_settings / "fleet-a" / "staging",
    )
    node_a = _node(
        tmp_settings / "fleet-a" / "node-state",
        identity=fixture["identityA"],
        signer=signer_a,
        registry=fixture["registryA"],
        journal=journal_a,
        receiver=receiver_a,
    )
    node_b = _node(
        tmp_settings / "fleet-b" / "node-state",
        identity=fixture["identityB"],
        signer=fixture["signerB"],
        registry=fixture["registry"],
        journal=fixture["journal"],
        receiver=fixture["receiver"],
    )
    target, _store = _memory_target()
    monkeypatch.setattr(backup_publish, "resolve_target", lambda target_id, **_kwargs: target if target_id == TARGET_ID else None)
    monkeypatch.setattr(
        backup_publish,
        "_utc_iso",
        lambda: (NOW + timedelta(seconds=9)).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )

    proposed = node_a.propose_transfer(
        {"destinationFleetId": "fleet-b", "sourceReceipt": fixture["receipt"]}
    )
    assert proposed["transfer"]["transferId"] == fixture["transferId"]
    verified_grant = node_a.verify_ingress_grant({"grant": fixture["grant"]})
    assert verified_grant["grantId"] == fixture["grant"]["grantId"]
    remote_verifying = node_a.mark_remote_verifying(
        fixture["transferId"],
        {"grantId": fixture["grant"]["grantId"], "remoteTargetId": TARGET_ID},
    )
    assert remote_verifying["state"] == federation_transfer_journal.STATE_REMOTE_VERIFYING

    node_b.declare_replica(
        fixture["transferId"],
        {"grantId": fixture["grant"]["grantId"], "sourceReceipt": fixture["receipt"]},
    )
    for index, component in enumerate(fixture["package"].components):
        node_b.receive_component(
            fixture["transferId"],
            component.ciphertext_digest,
            grant_id=fixture["grant"]["grantId"],
            write_id=f"sender-flow-{index}",
            content=io.BytesIO(component.path.read_bytes()),
        )
    committed = node_b.commit_replica(fixture["transferId"], {"grantId": fixture["grant"]["grantId"]})
    verified = node_a.verify_replica_attestation(
        fixture["transferId"],
        {
            "attestation": committed["attestation"],
            "remoteReceiptBase64": committed["remoteReceiptBase64"],
            "remoteCommitBase64": committed["remoteCommitBase64"],
        },
    )
    assert verified["federatedCopy"]["localDurabilityCredit"] is False
    assert verified["transfer"]["state"] == federation_transfer_journal.STATE_SUCCEEDED
    recorded_copy = node_a.durability_ledger.get_copy(fixture["transferId"])
    assert recorded_copy is not None
    assert recorded_copy["localDurabilityCredit"] is False


def test_node_loads_only_public_root_and_encrypted_online_signer_config(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_settings)
    public_identity = tmp_settings / "fleet-b" / "fleet-identity.json"
    federation_identity.export_public_fleet_identity(
        tmp_settings / "fleet-b" / "root.bundle.json",
        public_identity,
    )
    monkeypatch.setattr(
        backup_targets,
        "get_target",
        lambda target_id: {"targetId": target_id, "kind": "s3"},
    )
    config_path = tmp_settings / "fleet-b" / "federation-node.json"
    config = {
        "schema": "federation-node-config-v1",
        "fleetId": "fleet-b",
        "publicIdentityPath": str(public_identity),
        "signerBundlePath": str(tmp_settings / "fleet-b" / "signer.bundle.json"),
        "peerRegistryPath": str(tmp_settings / "fleet-b" / "trust.sqlite3"),
        "transferJournalPath": str(tmp_settings / "fleet-b" / "transfers.sqlite3"),
        "receiverDbPath": str(tmp_settings / "fleet-b" / "replica-receiver.sqlite3"),
        "stagingDir": str(tmp_settings / "fleet-b" / "replica-staging"),
        "durabilityDbPath": str(tmp_settings / "fleet-b" / "durability.sqlite3"),
        "custodyDbPath": str(tmp_settings / "fleet-b" / "custody.sqlite3"),
        "nodeStateDbPath": str(tmp_settings / "fleet-b" / "node.sqlite3"),
        "remoteTargetId": TARGET_ID,
        "failureDomainMetadata": _metadata(region="cn-south-1"),
        "readiness": {
            "wireCompatibility": ["object-set-v1", "receipt-v4", "commit-v4", "fastcdc-v3"],
            "availableFailureDomains": ["cn-south-1"],
            "forecastHeadroom": 1024,
            "costClass": "standard",
            "readiness": "READY",
        },
        "maxIngressBytes": 64 * 1024 * 1024,
        "ownerInstanceId": "fleet-b-process-node",
        "custody": {"peerFleetId": "fleet-a", "mode": "COLD_CUSTODY", "actor": "operator-b"},
    }
    config_path.write_text(__import__("json").dumps(config), encoding="utf-8")

    loaded = federation_node.load_federation_node(
        config_path,
        signer_passphrase=bytearray(b"fleet-b-signer-passphrase-replica"),
        clock=lambda: NOW + timedelta(seconds=10),
    )
    assert loaded.health()["fleetId"] == "fleet-b"
    assert loaded.health()["rootFingerprint"] == fixture["identityB"]["rootFingerprint"]
    assert loaded.health()["remoteTargetId"] == TARGET_ID
    assert not any("root.bundle" in str(value) for value in config.values())

    config["agePrivateIdentity"] = "AGE-SECRET-KEY-1MUSTNOTLOAD"
    config_path.write_text(__import__("json").dumps(config), encoding="utf-8")
    with pytest.raises(federation_node.FederationNodeError) as rejected:
        federation_node.load_federation_node(
            config_path,
            signer_passphrase=bytearray(b"fleet-b-signer-passphrase-replica"),
            clock=lambda: NOW + timedelta(seconds=10),
        )
    assert rejected.value.code == "FEDERATION_NODE_CONFIG_FIELDS_INVALID"
