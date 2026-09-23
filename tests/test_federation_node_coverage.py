from __future__ import annotations

import copy
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from deepseek_infra.infra.workspace import (
    backup_object_set,
    backup_targets,
    federated_dr_drill,
    federated_durability,
    federation_challenge,
    federation_custody_capability,
    federation_identity,
    federation_node,
    federation_peer_trust,
    federation_readiness_attestation,
    federation_replica_receiver,
    federation_transfer,
    federation_transfer_journal,
    resilience_federation_readiness,
)
from tests.test_backup_480_federated_replica_commit import TARGET_ID
from tests.test_backup_480_federated_replica_receiver import NOW, _fixture, _metadata


def _make_full_signer(
    tmp_path: Path,
    identity: dict[str, object],
    *,
    fleet_id: str = "fleet-b",
    passphrase: bytes = b"fleet-b-signer-passphrase-replica",
    sequence: int = 10,
) -> tuple[federation_identity.OnlineFleetSigner, dict[str, object]]:
    signer_path = tmp_path / fleet_id / f"signer_full_{sequence}.bundle.json"
    root_path = tmp_path / fleet_id / "root.bundle.json"
    root_passphrase = f"{fleet_id}-root-passphrase-replica".encode("utf-8")
    certificate = federation_identity.issue_online_signer(
        root_bundle_path=root_path,
        root_passphrase=root_passphrase,
        signer_bundle_path=signer_path,
        signer_passphrase=passphrase,
        sequence=sequence,
        not_before=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=2),
        purposes=federation_identity.DEFAULT_ONLINE_SIGNER_PURPOSES,
    )
    signer = federation_identity.load_online_signer(
        signer_path,
        passphrase,
        root_identity=identity,
        now=NOW,
    )
    return signer, certificate


def _node(
    root: Path,
    *,
    identity: dict[str, object],
    signer: federation_identity.OnlineFleetSigner,
    registry: federation_peer_trust.PeerTrustRegistry,
    journal: federation_transfer_journal.FederatedTransferJournal,
    receiver: federation_replica_receiver.FederatedReplicaReceiver,
    readiness: dict[str, object] | None = None,
    max_ingress_bytes: int = 64 * 1024 * 1024,
    owner_instance_id: str = "fleet-node-test-worker",
) -> federation_node.FederationNode:
    default_readiness = {
        "wireCompatibility": ["object-set-v1", "receipt-v4", "commit-v4", "fastcdc-v3"],
        "availableFailureDomains": ["cn-south-1"],
        "forecastHeadroom": 1024,
        "costClass": "standard",
        "readiness": "READY",
    }
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
        readiness=readiness or default_readiness,
        max_ingress_bytes=max_ingress_bytes,
        owner_instance_id=owner_instance_id,
        clock=lambda: NOW + timedelta(seconds=10),
    )


# =========================================================================
# Helper & Validation Functions Coverage
# =========================================================================

def test_federation_node_helper_validations(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)

    # _utc_iso invalid tz
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._utc_iso(datetime(2026, 9, 1, 12, 0, 0))  # naive
    assert exc.value.code == "FEDERATION_NODE_TIME_INVALID"

    aware = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert federation_node._utc_iso(aware) == "2026-09-01T12:00:00Z"

    # _canonical_json invalid
    with pytest.raises(federation_node.FederationNodeError):
        federation_node._canonical_json(float("nan"))

    # _document non-dict
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._document("not-a-dict", code="CODE_INVALID")
    assert exc.value.code == "CODE_INVALID"

    # _document with invalid json value (e.g. nan)
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._document({"bad": float("nan")}, code="CODE_INVALID")
    assert exc.value.code == "CODE_INVALID"

    assert federation_node._document({"valid": "value"}, code="CODE_INVALID") == {"valid": "value"}

    # _exact_payload mismatch
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._exact_payload({"a": 1, "extra": 2}, {"a"}, code="PAYLOAD_MISMATCH")
    assert exc.value.code == "PAYLOAD_MISMATCH"

    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._exact_payload({"a": 1}, {"a", "b"}, code="PAYLOAD_MISMATCH")
    assert exc.value.code == "PAYLOAD_MISMATCH"

    assert federation_node._exact_payload({"a": 1, "b": 2}, {"a", "b"}, code="PAYLOAD_MISMATCH") == {"a": 1, "b": 2}

    # _transfer_id invalid
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._transfer_id("bad/id")
    assert exc.value.code == "FEDERATION_TRANSFER_ID_INVALID"
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._transfer_id(123)
    assert exc.value.code == "FEDERATION_TRANSFER_ID_INVALID"
    valid_transfer_id = "sha256:" + "a" * 64
    assert federation_node._transfer_id(valid_transfer_id) == valid_transfer_id

    # _control_id invalid
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._control_id("bad/id", code="CTRL_INVALID")
    assert exc.value.code == "CTRL_INVALID"
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._control_id(456, code="CTRL_INVALID")
    assert exc.value.code == "CTRL_INVALID"
    assert federation_node._control_id("ctrl-123", code="CTRL_INVALID") == "ctrl-123"

    # _positive_int invalid
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._positive_int(True, code="INT_INVALID")  # bool rejected
    assert exc.value.code == "INT_INVALID"
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._positive_int(0, code="INT_INVALID")
    assert exc.value.code == "INT_INVALID"
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._positive_int(-5, code="INT_INVALID")
    assert exc.value.code == "INT_INVALID"
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._positive_int("100", code="INT_INVALID")
    assert exc.value.code == "INT_INVALID"
    assert federation_node._positive_int(100, code="INT_INVALID") == 100

    # _decode_document
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._decode_document("", code="DEC_INVALID")
    assert exc.value.code == "DEC_INVALID"
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._decode_document("not-valid-base64!!!", code="DEC_INVALID")
    assert exc.value.code == "DEC_INVALID"
    assert federation_node._decode_document("aGVsbG8=", code="DEC_INVALID") == b"hello"

    # _config_path
    base = Path("/test/base")
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._config_path(base, "", field="testField")
    assert exc.value.code == "FEDERATION_NODE_CONFIG_PATH_INVALID"
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._config_path(base, 123, field="testField")
    assert exc.value.code == "FEDERATION_NODE_CONFIG_PATH_INVALID"

    rel = federation_node._config_path(base, "sub/file.txt", field="testField")
    assert rel.name == "file.txt"

    # _source_receipt
    receipt = fixture["receipt"]
    res_receipt, obj_digest = federation_node._source_receipt(receipt)
    assert res_receipt == receipt
    assert obj_digest == fixture["federationDigest"]

    # _source_receipt fleet mismatch when sourceFleetId is set
    receipt_with_fleet = {**receipt, "sourceFleetId": "fleet-a"}
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._source_receipt(receipt_with_fleet, expected_source_fleet_id="fleet-b")
    assert exc.value.code == "FEDERATION_SOURCE_RECEIPT_FLEET_MISMATCH"

    # _source_receipt invalid version or not verified
    bad_receipt = copy.deepcopy(receipt)
    bad_receipt["creationVerified"] = False
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._source_receipt(bad_receipt)
    assert exc.value.code == "FEDERATION_SOURCE_RECEIPT_INVALID"

    bad_receipt2 = copy.deepcopy(receipt)
    bad_receipt2["schemaVersion"] = "receipt-wrong"
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._source_receipt(bad_receipt2)
    assert exc.value.code == "FEDERATION_SOURCE_RECEIPT_INVALID"

    bad_receipt3 = copy.deepcopy(receipt)
    bad_receipt3["objectSetDigest"] = "not-64-hex"
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._source_receipt(bad_receipt3)
    assert exc.value.code == "FEDERATION_SOURCE_RECEIPT_INVALID"


# =========================================================================
# Node Initialization Conflicts & Health
# =========================================================================

def test_federation_node_init_conflicts(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)
    root1 = tmp_settings / "test-conflicts-1"
    root2 = tmp_settings / "test-conflicts-2"

    # Local identity mismatch in signer
    other_identity = copy.deepcopy(fixture["identityB"])
    other_identity["fleetId"] = "fleet-wrong"
    with pytest.raises(federation_node.FederationNodeError) as exc:
        _node(
            root1,
            identity=other_identity,
            signer=fixture["signerB"],
            registry=fixture["registry"],
            journal=fixture["journal"],
            receiver=fixture["receiver"],
        )
    assert exc.value.code == "FEDERATION_NODE_LOCAL_IDENTITY_CONFLICT"

    # Signer certificate rootFingerprint mismatch
    tampered_cert = copy.deepcopy(fixture["signerB"].certificate)
    tampered_cert["rootFingerprint"] = "fed-fp-tampered-123456789"
    tampered_signer = federation_identity.OnlineFleetSigner(
        fixture["signerB"]._private_key,
        tampered_cert,
    )
    with pytest.raises(federation_node.FederationNodeError) as exc:
        _node(
            root2,
            identity=fixture["identityB"],
            signer=tampered_signer,
            registry=fixture["registry"],
            journal=fixture["journal"],
            receiver=fixture["receiver"],
        )
    assert exc.value.code == "FEDERATION_NODE_SIGNER_IDENTITY_CONFLICT"


def test_federation_node_properties_and_health(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)
    node = _node(
        tmp_settings / "test-props",
        identity=fixture["identityB"],
        signer=fixture["signerB"],
        registry=fixture["registry"],
        journal=fixture["journal"],
        receiver=fixture["receiver"],
    )

    assert node.identity == fixture["identityB"]
    assert node.durability_ledger is not None
    assert node.state is not None

    health = node.health()
    assert health["schema"] == federation_node.NODE_HEALTH_SCHEMA
    assert health["fleetId"] == "fleet-b"
    assert health["ready"] is True
    assert health["remoteTargetId"] == TARGET_ID


# =========================================================================
# Readiness Flow Coverage
# =========================================================================

def test_federation_node_readiness_flow(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)
    full_signer, full_cert = _make_full_signer(tmp_settings, fixture["identityB"], sequence=2)
    fixture["registryA"].accept_online_signer("fleet-b", full_cert, actor="operator-a", now=NOW)

    # 1. Invalid readiness config missing keys
    bad_node = _node(
        tmp_settings / "test-bad-readiness",
        identity=fixture["identityB"],
        signer=full_signer,
        registry=fixture["registry"],
        journal=fixture["journal"],
        receiver=fixture["receiver"],
        readiness={"readiness": "READY"},  # missing other required fields
    )
    with pytest.raises(federation_node.FederationNodeError) as exc:
        bad_node.issue_readiness()
    assert exc.value.code == "FEDERATION_NODE_READINESS_CONFIG_INVALID"

    # 2. Normal issue_readiness with full signer
    node = _node(
        tmp_settings / "test-readiness",
        identity=fixture["identityB"],
        signer=full_signer,
        registry=fixture["registry"],
        journal=fixture["journal"],
        receiver=fixture["receiver"],
    )
    attestation = node.issue_readiness()
    assert attestation["schema"] == "federation-readiness-attestation-v1"
    assert attestation["fleetId"] == "fleet-b"
    assert attestation["sequence"] == 1

    # 3. Verify readiness invalid payload
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.verify_readiness({"bad": "payload"})
    assert exc.value.code == "FEDERATION_NODE_READINESS_VERIFY_INVALID"

    # 4. Verify readiness against peer
    with pytest.raises(federation_node.FederationNodeError):
        node.verify_readiness({"expectedPeerFleetId": "fleet-unknown", "attestation": attestation})

    # Peer fleet-a signs attestation and node (fleet-b) verifies it
    signer_a_full, cert_a = _make_full_signer(
        tmp_settings,
        fixture["identityA"],
        fleet_id="fleet-a",
        sequence=2,
    )
    fixture["registry"].accept_online_signer("fleet-a", cert_a, actor="operator-b", now=NOW)
    snapshot_a = resilience_federation_readiness.build_federation_snapshot(
        fleet_id="fleet-a",
        wire_compatibility=["object-set-v1", "receipt-v4", "commit-v4", "fastcdc-v3"],
        available_failure_domains=["cn-north-1"],
        forecast_headroom=2048,
        cost_class="standard",
        readiness="READY",
        now=NOW,
    )
    attestation_a = federation_readiness_attestation.issue_readiness_attestation(
        signer_a_full,
        snapshot_a,
        sequence=1,
        signed_at=NOW,
        expires_at=NOW + timedelta(seconds=120),
    )
    verified = node.verify_readiness({"expectedPeerFleetId": "fleet-a", "attestation": attestation_a})
    assert verified["fleetId"] == "fleet-a"
    assert verified["sequence"] == 1


# =========================================================================
# Challenge Flow Coverage
# =========================================================================

def test_federation_node_challenge_flow(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)
    node = _node(
        tmp_settings / "test-challenge",
        identity=fixture["identityB"],
        signer=fixture["signerB"],
        registry=fixture["registry"],
        journal=fixture["journal"],
        receiver=fixture["receiver"],
    )

    # 1. Issue challenge invalid payload
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.issue_challenge({"bad": 123})
    assert exc.value.code == "FEDERATION_NODE_CHALLENGE_REQUEST_INVALID"

    # 2. Respond challenge invalid payload
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.respond_challenge({"bad": 123})
    assert exc.value.code == "FEDERATION_NODE_CHALLENGE_REQUEST_INVALID"

    # 3. Verify challenge invalid payload
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.verify_challenge({"bad": 123})
    assert exc.value.code == "FEDERATION_NODE_CHALLENGE_VERIFY_INVALID"

    # 4. Normal issue_challenge to active peer fleet-a
    challenge = node.issue_challenge({"destinationFleetId": "fleet-a"})
    assert challenge["schema"] == "federation-challenge-v1"
    assert challenge["destinationFleetId"] == "fleet-a"
    assert challenge["sourceFleetId"] == "fleet-b"

    # Respond to invalid challenge schema
    with pytest.raises(federation_node.FederationNodeError):
        node.respond_challenge({"challenge": {"schema": "invalid-schema"}})


# =========================================================================
# Propose Transfer, Ingress Grant & DR Drill Coverage
# =========================================================================

def test_federation_node_transfer_and_dr_branches(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)
    node = _node(
        tmp_settings / "test-dr-branches",
        identity=fixture["identityB"],
        signer=fixture["signerB"],
        registry=fixture["registry"],
        journal=fixture["journal"],
        receiver=fixture["receiver"],
    )

    # Propose transfer invalid payload
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.propose_transfer({"bad": 1})
    assert exc.value.code == "FEDERATION_NODE_TRANSFER_PROPOSAL_INVALID"

    # Propose transfer unknown peer
    with pytest.raises(federation_node.FederationNodeError):
        node.propose_transfer({"destinationFleetId": "fleet-unknown", "sourceReceipt": fixture["receipt"]})

    # Run DR drill invalid payload
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.run_dr_drill("xfer-1", {"bad": 1})
    assert exc.value.code == "FEDERATION_NODE_DR_REQUEST_INVALID"

    # Run DR drill invalid request id format (leading symbol)
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.run_dr_drill("xfer-1", {"requestId": "@bad-id"})
    assert exc.value.code == "FEDERATION_NODE_DR_REQUEST_INVALID"

    # Run DR drill missing commit
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.run_dr_drill("xfer-1", {"requestId": "req-drill-01"})
    assert exc.value.code == "FEDERATION_REPLICA_REMOTE_COMMIT_MISSING"

    # Run DR drill replay with transferId mismatch
    node.state.put_effect("dr-drill:req-replay-test", {"transferId": "xfer-original"}, now=NOW)
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.run_dr_drill("xfer-different", {"requestId": "req-replay-test"})
    assert exc.value.code == "FEDERATION_NODE_EFFECT_IDENTITY_CONFLICT"

    # Run DR drill replay with same transferId (idempotent)
    replayed = node.run_dr_drill("xfer-original", {"requestId": "req-replay-test"})
    assert replayed["transferId"] == "xfer-original"

    # Verify DR attestation invalid payload
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.verify_dr_attestation("xfer-1", {"bad": 1})
    assert exc.value.code == "FEDERATION_NODE_DR_VERIFY_INVALID"

    # Verify DR attestation transferId mismatch
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.verify_dr_attestation("xfer-1", {"attestation": {"transferId": "xfer-other"}})
    assert exc.value.code == "FEDERATED_DR_TRANSFER_ID_MISMATCH"


def test_federation_node_transfer_lifecycle_edge_cases(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)
    node = _node(
        tmp_settings / "test-xfer-lifecycle",
        identity=fixture["identityB"],
        signer=fixture["signerB"],
        registry=fixture["registry"],
        journal=fixture["journal"],
        receiver=fixture["receiver"],
        max_ingress_bytes=1000,
    )

    # 1. Ingress grant invalid payload
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.issue_ingress_grant({"incomplete": True})
    assert exc.value.code == "FEDERATION_NODE_GRANT_REQUEST_INVALID"

    # 2. Ingress grant max bytes exceeded
    grant_req = {
        "sourceFleetId": "fleet-a",
        "sessionNonce": str(fixture["challenge"]["nonce"]),
        "transferId": fixture["transferId"],
        "policyId": "policy-01",
        "backupId": "backup-01",
        "objectSetDigest": fixture["federationDigest"],
        "totalBytes": 10000000,  # exceeds max_ingress_bytes=1000
    }
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.issue_ingress_grant(grant_req)
    assert exc.value.code == "FEDERATION_INGRESS_MAX_BYTES_EXCEEDED"

    # 3. Verify ingress grant invalid payload
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.verify_ingress_grant({"bad": 1})
    assert exc.value.code == "FEDERATION_NODE_GRANT_VERIFY_INVALID"

    # 4. Mark remote verifying on receiver transfer raises role mismatch
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.mark_remote_verifying(fixture["transferId"], {"bad": 1})
    assert exc.value.code == "FEDERATION_TRANSFER_ROLE_MISMATCH"

    # 5. Propose transfer as sender, then test mark_remote_verifying invalid payload
    prop = node.propose_transfer({"destinationFleetId": "fleet-a", "sourceReceipt": fixture["receipt"]})
    sender_xfer_id = prop["transfer"]["transferId"]
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.mark_remote_verifying(sender_xfer_id, {"bad": 1})
    assert exc.value.code == "FEDERATION_NODE_REMOTE_VERIFYING_INVALID"

    # 6. Mark remote verifying missing grant in state
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.mark_remote_verifying(sender_xfer_id, {"grantId": "grant-not-in-state", "remoteTargetId": TARGET_ID})
    assert exc.value.code == "FEDERATION_INGRESS_GRANT_NOT_FOUND"

    # 7. Declare replica invalid payload
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.declare_replica(fixture["transferId"], {"bad": 1})
    assert exc.value.code == "FEDERATION_NODE_REPLICA_DECLARATION_INVALID"

    # 8. Expected component size invalid digest
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.expected_component_size(fixture["transferId"], "invalid-digest", fixture["grant"]["grantId"])
    assert exc.value.code == "FEDERATION_REPLICA_COMPONENT_DIGEST_INVALID"

    # 9. Commit replica invalid payload
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.commit_replica(fixture["transferId"], {"bad": 1})
    assert exc.value.code == "FEDERATION_NODE_COMMIT_REQUEST_INVALID"

    # 10. Commit replica idempotent return if present in state
    node.state.put_effect(f"replica-commit:{fixture['transferId']}", {"already": "committed"}, now=NOW)
    assert node.commit_replica(fixture["transferId"], {"grantId": fixture["grant"]["grantId"]}) == {"already": "committed"}

    # 11. Verify replica attestation invalid payload
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.verify_replica_attestation(fixture["transferId"], {"bad": 1})
    assert exc.value.code == "FEDERATION_NODE_REPLICA_VERIFY_INVALID"


# =========================================================================
# Node State Effect Identity Conflict & Idempotency
# =========================================================================

def test_federation_node_state_effect_conflict(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)
    node_state = federation_node.FederationNodeState(
        tmp_settings / "test-node-state" / "node.sqlite3",
        fixture["identityB"],
    )

    now = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)
    effect_1 = {"schema": "test", "data": "first"}
    saved = node_state.put_effect("effect-key-1", effect_1, now=now)
    assert saved == effect_1

    # Idempotent replay with exact match
    replayed = node_state.put_effect("effect-key-1", effect_1, now=now)
    assert replayed == effect_1

    # Conflict replay with modified payload
    effect_conflict = {"schema": "test", "data": "different"}
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node_state.put_effect("effect-key-1", effect_conflict, now=now)
    assert exc.value.code == "FEDERATION_NODE_EFFECT_IDENTITY_CONFLICT"

    # Empty effect key
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node_state.put_effect("", effect_1, now=now)
    assert exc.value.code == "FEDERATION_NODE_EFFECT_KEY_INVALID"

    # Too long effect key (> 512)
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node_state.put_effect("k" * 513, effect_1, now=now)
    assert exc.value.code == "FEDERATION_NODE_EFFECT_KEY_INVALID"

    # Secret in effect payload fails closed
    with pytest.raises(federation_identity.FederationIdentityError):
        node_state.put_effect("secret-key", {"secret": "super-secret-token"}, now=now)

    # Monotonic sequence allocator
    seq1 = node_state.next_sequence("ns1", now=now)
    seq2 = node_state.next_sequence("ns1", now=now)
    seq3 = node_state.next_sequence("ns1", now=now)
    assert (seq1, seq2, seq3) == (1, 2, 3)

    # Corrupted commitment in SQLite
    with node_state._write() as conn:
        conn.execute("UPDATE federation_node_effects SET effect_digest = 'tampered' WHERE effect_key = 'effect-key-1'")
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node_state.get_effect("effect-key-1")
    assert exc.value.code == "FEDERATION_NODE_EFFECT_COMMITMENT_INVALID"

    # Identity binding conflict when opening same DB with different identity
    other_id = copy.deepcopy(fixture["identityB"])
    other_id["rootFingerprint"] = "fed-fp-" + "9" * 24
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node.FederationNodeState(
            tmp_settings / "test-node-state" / "node.sqlite3",
            other_id,
        )
    assert exc.value.code == "FEDERATION_NODE_STATE_IDENTITY_CONFLICT"


# =========================================================================
# Custody Configuration & Node Config Reading Coverage
# =========================================================================

def test_federation_node_custody_and_config_readers(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)
    registry = fixture["registry"]
    custody_reg = federation_custody_capability.FederationCustodyCapabilityRegistry(
        tmp_settings / "custody-test.sqlite3",
        fixture["identityB"],
    )
    now = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)

    # 1. Cold custody invalid extra fields
    bad_cold_config = {
        "fleetId": "fleet-b",
        "custody": {
            "peerFleetId": "fleet-a",
            "mode": federation_custody_capability.COLD_CUSTODY,
            "actor": "admin",
            "unexpected": "extra",
        },
    }
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._configure_custody(
            bad_cold_config,
            registry=registry,
            custody_registry=custody_reg,
            recovery_age_identity=None,
            now=now,
        )
    assert exc.value.code == "FEDERATION_NODE_CUSTODY_CONFIG_INVALID"

    # 2. Recovery capable missing age recipient or identity
    bad_recovery_config = {
        "fleetId": "fleet-b",
        "custody": {
            "peerFleetId": "fleet-a",
            "mode": federation_custody_capability.RECOVERY_CAPABLE,
            "actor": "admin",
        },
    }
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._configure_custody(
            bad_recovery_config,
            registry=registry,
            custody_registry=custody_reg,
            recovery_age_identity=None,
            now=now,
        )
    assert exc.value.code == "FEDERATION_RECOVERY_IDENTITY_BINDING_REQUIRED"

    # 3. Invalid custody mode
    unknown_mode_config = {
        "fleetId": "fleet-b",
        "custody": {
            "peerFleetId": "fleet-a",
            "mode": "UNKNOWN_MODE",
            "actor": "admin",
        },
    }
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._configure_custody(
            unknown_mode_config,
            registry=registry,
            custody_registry=custody_reg,
            recovery_age_identity=None,
            now=now,
        )
    assert exc.value.code == "FEDERATION_NODE_CUSTODY_CONFIG_INVALID"

    # 4. Valid cold custody configuration
    valid_cold_config = {
        "fleetId": "fleet-b",
        "custody": {
            "peerFleetId": "fleet-a",
            "mode": federation_custody_capability.COLD_CUSTODY,
            "actor": "admin",
        },
    }
    federation_node._configure_custody(
        valid_cold_config,
        registry=registry,
        custody_registry=custody_reg,
        recovery_age_identity=None,
        now=now,
    )

    # 5. _read_node_config missing file
    missing_path = tmp_settings / "nonexistent-config.json"
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._read_node_config(missing_path)
    assert exc.value.code == "FEDERATION_NODE_CONFIG_INVALID"

    # 6. _read_node_config invalid fields
    bad_fields_path = tmp_settings / "bad-fields.json"
    bad_fields_path.write_text(json.dumps({"incomplete": True}), encoding="utf-8")
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._read_node_config(bad_fields_path)
    assert exc.value.code == "FEDERATION_NODE_CONFIG_FIELDS_INVALID"

    # 7. _read_node_config invalid schema
    bad_schema = copy.deepcopy(federation_node._NODE_CONFIG_FIELDS)
    bad_schema_dict = {field: "val" for field in bad_schema}
    bad_schema_dict["schema"] = "wrong-schema-v1"
    bad_schema_path = tmp_settings / "bad-schema.json"
    bad_schema_path.write_text(json.dumps(bad_schema_dict), encoding="utf-8")
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._read_node_config(bad_schema_path)
    assert exc.value.code == "FEDERATION_NODE_CONFIG_SCHEMA_INVALID"


def test_load_federation_node_validation_errors(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)
    base_cfg = {
        "schema": federation_node.NODE_CONFIG_SCHEMA,
        "fleetId": "fleet-b",
        "publicIdentityPath": "public.json",
        "signerBundlePath": "signer.json",
        "peerRegistryPath": str(tmp_settings / "fleet-b" / "trust.sqlite3"),
        "transferJournalPath": "xfer.sqlite3",
        "receiverDbPath": "receiver.sqlite3",
        "stagingDir": "staging",
        "durabilityDbPath": "durability.sqlite3",
        "custodyDbPath": "custody.sqlite3",
        "nodeStateDbPath": "node.sqlite3",
        "remoteTargetId": TARGET_ID,
        "failureDomainMetadata": _metadata(region="cn-south-1"),
        "readiness": {
            "wireCompatibility": ["object-set-v1"],
            "availableFailureDomains": ["cn-south-1"],
            "forecastHeadroom": 1024,
            "costClass": "standard",
            "readiness": "READY",
        },
        "maxIngressBytes": 64 * 1024 * 1024,
        "ownerInstanceId": "worker-1",
        "custody": {
            "peerFleetId": "fleet-a",
            "mode": federation_custody_capability.COLD_CUSTODY,
            "actor": "admin",
        },
    }

    # 1. Path conflict
    cfg_conflict = dict(base_cfg, receiverDbPath="same.sqlite3", stagingDir="same.sqlite3")
    cfg_conflict_path = tmp_settings / "test_load_conflict.json"
    cfg_conflict_path.write_text(json.dumps(cfg_conflict), encoding="utf-8")
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node.load_federation_node(cfg_conflict_path, signer_passphrase=b"pass")
    assert exc.value.code == "FEDERATION_NODE_CONFIG_PATH_CONFLICT"

    # 2. Public identity missing / invalid
    cfg_bad_id = dict(base_cfg, publicIdentityPath="nonexistent_id.json")
    cfg_bad_id_path = tmp_settings / "test_load_bad_id.json"
    cfg_bad_id_path.write_text(json.dumps(cfg_bad_id), encoding="utf-8")
    with pytest.raises(federation_node.FederationNodeError):
        federation_node.load_federation_node(cfg_bad_id_path, signer_passphrase=b"pass")

    # 3. Fleet ID mismatch between config and public identity
    public_a_path = tmp_settings / "public_a.json"
    public_a_path.write_text(json.dumps(fixture["identityA"]), encoding="utf-8")
    cfg_mismatch = dict(base_cfg, publicIdentityPath=str(public_a_path))
    cfg_mismatch_path = tmp_settings / "test_load_mismatch.json"
    cfg_mismatch_path.write_text(json.dumps(cfg_mismatch), encoding="utf-8")
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node.load_federation_node(cfg_mismatch_path, signer_passphrase=b"pass")
    assert exc.value.code == "FEDERATION_NODE_CONFIG_FLEET_MISMATCH"

    # 4. Target not S3
    public_b_path = tmp_settings / "public_b.json"
    public_b_path.write_text(json.dumps(fixture["identityB"]), encoding="utf-8")
    signer_b_path = tmp_settings / "fleet-b" / "signer.bundle.json"
    with patch.object(backup_targets, "get_target", return_value={"kind": "local", "targetId": TARGET_ID}):
        cfg_target = dict(
            base_cfg,
            publicIdentityPath=str(public_b_path),
            signerBundlePath=str(signer_b_path),
        )
        cfg_target_path = tmp_settings / "test_load_bad_target.json"
        cfg_target_path.write_text(json.dumps(cfg_target), encoding="utf-8")
        with pytest.raises(federation_node.FederationNodeError) as exc:
            federation_node.load_federation_node(
                cfg_target_path,
                signer_passphrase=b"fleet-b-signer-passphrase-replica",
                clock=lambda: NOW,
            )
        assert exc.value.code == "FEDERATION_REPLICA_PROVIDER_TARGET_REQUIRED"

    # 5. Successful load_federation_node
    with patch.object(backup_targets, "get_target", return_value={"kind": "s3", "targetId": TARGET_ID}):
        cfg_ok = dict(
            base_cfg,
            publicIdentityPath=str(public_b_path),
            signerBundlePath=str(signer_b_path),
        )
        cfg_ok_path = tmp_settings / "test_load_ok.json"
        cfg_ok_path.write_text(json.dumps(cfg_ok), encoding="utf-8")
        loaded = federation_node.load_federation_node(
            cfg_ok_path,
            signer_passphrase=b"fleet-b-signer-passphrase-replica",
            clock=lambda: NOW,
        )
        assert loaded.identity["fleetId"] == "fleet-b"


def test_federation_node_additional_internal_branches(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)
    node = _node(
        tmp_settings / "test-internal-branches",
        identity=fixture["identityB"],
        signer=fixture["signerB"],
        registry=fixture["registry"],
        journal=fixture["journal"],
        receiver=fixture["receiver"],
    )

    # 1. verify_challenge success
    signer_a_full, cert_a = _make_full_signer(tmp_settings, fixture["identityA"], fleet_id="fleet-a", sequence=3)
    fixture["registry"].accept_online_signer("fleet-a", cert_a, actor="operator-b", now=NOW)
    ch = node.issue_challenge({"destinationFleetId": "fleet-a"})
    resp = federation_challenge.respond_to_federation_challenge(
        ch,
        peer_registry=fixture["registryA"],
        responder_signer=signer_a_full,
        now=NOW + timedelta(seconds=11),
    )
    v_res = node.verify_challenge({"challenge": ch, "response": resp})
    assert v_res["status"] == "AUTHENTICATED"
    assert v_res["sourceFleetId"] == "fleet-b"
    assert v_res["destinationFleetId"] == "fleet-a"

    # 2. Ingress grant issuance, idempotent replay and conflict
    # Replay of fixture grant
    grant_replay = node.issue_ingress_grant({
        "sourceFleetId": "fleet-a",
        "sessionNonce": str(fixture["challenge"]["nonce"]),
        "transferId": fixture["transferId"],
        "policyId": str(fixture["grant"]["policyId"]),
        "backupId": str(fixture["grant"]["backupId"]),
        "objectSetDigest": fixture["federationDigest"],
        "totalBytes": int(fixture["grant"]["maxBytes"]),
    })
    assert grant_replay["grantId"] == fixture["grant"]["grantId"]

    # Replay conflict with mismatched totalBytes
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.issue_ingress_grant({
            "sourceFleetId": "fleet-a",
            "sessionNonce": str(fixture["challenge"]["nonce"]),
            "transferId": fixture["transferId"],
            "policyId": str(fixture["grant"]["policyId"]),
            "backupId": str(fixture["grant"]["backupId"]),
            "objectSetDigest": fixture["federationDigest"],
            "totalBytes": int(fixture["grant"]["maxBytes"]) + 1,
        })
    assert exc.value.code == "FEDERATION_INGRESS_SESSION_REPLAY"

    # Fresh inbound challenge from fleet-a to fleet-b
    ch_in = federation_challenge.issue_federation_challenge(
        peer_registry=fixture["registryA"],
        challenger_signer=signer_a_full,
        destination_fleet_id="fleet-b",
        session_purpose=federation_challenge.SESSION_PURPOSE_REMOTE_CUSTODY,
        now=NOW + timedelta(seconds=5),
    )
    node.respond_challenge({"challenge": ch_in})

    # Fresh grant issuance
    fresh_xfer = federation_transfer.derive_transfer_id(
        source_fleet_id="fleet-a",
        destination_fleet_id="fleet-b",
        backup_id="backup-fresh",
        object_set_digest=fixture["federationDigest"],
    )
    fresh_grant = node.issue_ingress_grant({
        "sourceFleetId": "fleet-a",
        "sessionNonce": str(ch_in["nonce"]),
        "transferId": fresh_xfer,
        "policyId": "policy-fresh",
        "backupId": "backup-fresh",
        "objectSetDigest": fixture["federationDigest"],
        "totalBytes": 4096,
    })
    assert fresh_grant["transferId"] == fresh_xfer

    # 3. reconcile_transfer
    reconciled = node.reconcile_transfer(fixture["transferId"], fixture["grant"]["grantId"])
    assert reconciled["transferId"] == fixture["transferId"]
    assert "committedEffect" in reconciled

    # 4. DR drill success and DR attestation verification success
    node.state.put_effect(
        f"replica-commit:{fixture['transferId']}",
        {"attestation": {"schema": "fed-attest", "transferId": fixture["transferId"]}},
        now=NOW,
    )
    with patch.object(
        federated_dr_drill,
        "run_federated_dr_drill",
        return_value={"schema": "attest-ok", "attestationId": "dr-att-1"},
    ):
        drill_effect = node.run_dr_drill(fixture["transferId"], {"requestId": "req-drill-success"})
        assert drill_effect["effectType"] == "FEDERATED_DR_DRILL"
        assert drill_effect["requestId"] == "req-drill-success"

    with patch.object(
        federated_dr_drill,
        "verify_and_record_dr_drill_attestation",
        return_value={"attestationId": "accepted-dr"},
    ):
        dr_verify_result = node.verify_dr_attestation(
            fixture["transferId"],
            {"attestation": {"schema": "attest", "transferId": fixture["transferId"]}},
        )
        assert dr_verify_result["attestation"]["attestationId"] == "accepted-dr"

    # 5. _require_transfer not found
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node._require_transfer("sha256:" + "0" * 64)
    assert exc.value.code == "FEDERATION_TRANSFER_NOT_FOUND"

    # 6. _grant_for_transfer missing grant
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node._grant_for_transfer(fixture["transferId"], "invalid-grant-id", require_active=False)
    assert exc.value.code == "FEDERATION_INGRESS_GRANT_ID_INVALID"

    with patch.object(fixture["registry"], "get_ingress_grant", return_value=None):
        with pytest.raises(federation_node.FederationNodeError) as exc:
            node._grant_for_transfer(fixture["transferId"], fixture["grant"]["grantId"], require_active=False)
        assert exc.value.code == "FEDERATION_INGRESS_GRANT_NOT_FOUND"

    # 7. _grant_for_transfer binding mismatch
    with patch.object(
        fixture["registry"],
        "get_ingress_grant",
        return_value={
            "transferId": fixture["transferId"],
            "sourceFleetId": "fleet-mismatch",
            "destinationFleetId": "fleet-b",
            "policyId": str(fixture["grant"]["policyId"]),
            "backupId": str(fixture["grant"]["backupId"]),
            "objectSetDigest": fixture["federationDigest"],
            "grant": {},
        },
    ):
        with pytest.raises(federation_node.FederationNodeError) as exc:
            node._grant_for_transfer(fixture["transferId"], fixture["grant"]["grantId"], require_active=False)
        assert exc.value.code == "FEDERATION_INGRESS_GRANT_BINDING_MISMATCH"

    # 7b. _grant_for_transfer identity conflict
    with patch.object(
        fixture["registry"],
        "get_ingress_grant",
        return_value={
            "transferId": fixture["transferId"],
            "sourceFleetId": "fleet-a",
            "destinationFleetId": "fleet-b",
            "policyId": str(fixture["grant"]["policyId"]),
            "backupId": str(fixture["grant"]["backupId"]),
            "objectSetDigest": fixture["federationDigest"],
            "grant": {
                "transferId": fixture["transferId"],
                "sourceFleetId": "fleet-conflict",
                "destinationFleetId": "fleet-b",
                "policyId": str(fixture["grant"]["policyId"]),
                "backupId": str(fixture["grant"]["backupId"]),
                "objectSetDigest": fixture["federationDigest"],
            },
        },
    ):
        with pytest.raises(federation_node.FederationNodeError) as exc:
            node._grant_for_transfer(fixture["transferId"], fixture["grant"]["grantId"], require_active=False)
        assert exc.value.code == "FEDERATION_INGRESS_GRANT_IDENTITY_CONFLICT"

    # 7c. expected_component_size component not declared
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.expected_component_size(fixture["transferId"], "0" * 64, fixture["grant"]["grantId"])
    assert exc.value.code == "FEDERATION_REPLICA_COMPONENT_NOT_DECLARED"

    # 7d. declare_replica domain error
    with pytest.raises(federation_node.FederationNodeError):
        node.declare_replica(
            fixture["transferId"],
            {"grantId": fixture["grant"]["grantId"], "sourceReceipt": {"schemaVersion": "invalid"}},
        )

    # 7e. mark_remote_verifying missing receipt in state
    prop = node.propose_transfer({"destinationFleetId": "fleet-a", "sourceReceipt": fixture["receipt"]})
    xfer_id = prop["transfer"]["transferId"]
    node.state.put_effect(f"sender-grant:{xfer_id}", {"grantId": "grant-1"}, now=NOW)
    # Don't put source-receipt in state for this check
    with pytest.raises(federation_node.FederationNodeError) as exc:
        # State will have sender-grant, but delete source-receipt
        with node.state._write() as conn:
            conn.execute("DELETE FROM federation_node_effects WHERE effect_key = ?", (f"source-receipt:{xfer_id}",))
        node.mark_remote_verifying(xfer_id, {"grantId": "grant-1", "remoteTargetId": TARGET_ID})
    assert exc.value.code == "FEDERATION_SOURCE_RECEIPT_NOT_FOUND"

    # 7f. verify_replica_attestation missing receipt in state
    with pytest.raises(federation_node.FederationNodeError) as exc:
        node.verify_replica_attestation(xfer_id, {
            "attestation": {"valid": True},
            "remoteReceiptBase64": "aGVsbG8=",
            "remoteCommitBase64": "aGVsbG8=",
        })
    assert exc.value.code == "FEDERATION_SOURCE_RECEIPT_NOT_FOUND"

    # 7g. _document normalize returning non-dict
    with patch.object(federation_identity, "normalize_federation_json", return_value="string-not-dict"):
        with pytest.raises(federation_node.FederationNodeError):
            federation_node._document({}, code="ERR")

    # 8. _advance earlier state no-op
    transfer_later = {
        "transferId": fixture["transferId"],
        "state": federation_transfer_journal.TRANSFER_STATES[-1],
        "revision": 1,
    }
    same_rec = node._advance(
        transfer_later,
        federation_transfer_journal.TRANSFER_STATES[0],
        {},
        now=NOW,
    )
    assert same_rec == transfer_later

    # 9. _raise_domain with standard Exception without .code
    with pytest.raises(RuntimeError) as exc_info:
        federation_node.FederationNode._raise_domain(RuntimeError("arbitrary-error"))
    assert str(exc_info.value) == "arbitrary-error"

    # 10. _configure_custody RECOVERY_CAPABLE mode
    custody_reg = federation_custody_capability.FederationCustodyCapabilityRegistry(
        tmp_settings / "custody-recovery.sqlite3",
        fixture["identityB"],
    )
    with patch.object(custody_reg, "configure_peer"):
        recovery_cfg = {
            "fleetId": "fleet-b",
            "custody": {
                "peerFleetId": "fleet-a",
                "mode": federation_custody_capability.RECOVERY_CAPABLE,
                "actor": "admin",
                "ageRecipient": "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p",
            },
        }
        federation_node._configure_custody(
            recovery_cfg,
            registry=fixture["registry"],
            custody_registry=custody_reg,
            recovery_age_identity=b"test-age-secret-key-1234567890",
            now=NOW,
        )


def test_federation_node_additional_exceptions_and_edges(tmp_settings: Path) -> None:
    fixture = _fixture(tmp_settings)
    node = _node(
        tmp_settings / "test-more-edges",
        identity=fixture["identityB"],
        signer=fixture["signerB"],
        registry=fixture["registry"],
        journal=fixture["journal"],
        receiver=fixture["receiver"],
    )

    # 1. _source_receipt invalid committedObjects raises
    with patch.object(backup_object_set, "committed_object_inventory", side_effect=RuntimeError("inventory-bad")):
        with pytest.raises(federation_node.FederationNodeError) as exc:
            federation_node._source_receipt(fixture["receipt"])
        assert exc.value.code == "FEDERATION_SOURCE_RECEIPT_INVALID"

    # 2. Ingress grant session replay with mismatched grant payload
    with patch.object(
        fixture["registry"],
        "get_ingress_grant_by_session_nonce",
        return_value={
            "sourceFleetId": "fleet-a",
            "destinationFleetId": "fleet-b",
            "transferId": fixture["transferId"],
            "policyId": str(fixture["grant"]["policyId"]),
            "backupId": str(fixture["grant"]["backupId"]),
            "objectSetDigest": fixture["federationDigest"],
            "allowedObjectPrefix": f"federation/fleet-a/{fixture['transferId']}/",
            "maxBytes": int(fixture["grant"]["maxBytes"]),
            "grant": {"backupId": "tampered-backup-id"},
        },
    ):
        with pytest.raises(federation_node.FederationNodeError) as exc:
            node.issue_ingress_grant({
                "sourceFleetId": "fleet-a",
                "sessionNonce": str(fixture["challenge"]["nonce"]),
                "transferId": fixture["transferId"],
                "policyId": str(fixture["grant"]["policyId"]),
                "backupId": str(fixture["grant"]["backupId"]),
                "objectSetDigest": fixture["federationDigest"],
                "totalBytes": int(fixture["grant"]["maxBytes"]),
            })
        assert exc.value.code == "FEDERATION_INGRESS_SESSION_REPLAY"

    # 3. receive_component domain error
    with patch.object(
        fixture["receiver"],
        "receive_component",
        side_effect=federation_replica_receiver.FederatedReplicaReceiverError("RECEIVER_ERR"),
    ):
        with pytest.raises(federation_node.FederationNodeError) as exc:
            node.receive_component(
                fixture["transferId"],
                fixture["federationDigest"],
                grant_id=fixture["grant"]["grantId"],
                write_id="w1",
                content=io.BytesIO(b"data"),
            )
        assert exc.value.code == "RECEIVER_ERR"

    # 4. commit_replica domain error
    with node.state._write() as conn:
        conn.execute("DELETE FROM federation_node_effects WHERE effect_key = ?", (f"replica-commit:{fixture['transferId']}",))
    with patch(
        "deepseek_infra.infra.workspace.federated_replica_commit.commit_federated_replica",
        side_effect=RuntimeError("commit-failed"),
    ):
        with pytest.raises(RuntimeError):
            node.commit_replica(fixture["transferId"], {"grantId": fixture["grant"]["grantId"]})

    # 5. verify_replica_attestation domain error
    prop = node.propose_transfer({"destinationFleetId": "fleet-a", "sourceReceipt": fixture["receipt"]})
    xfer_id = prop["transfer"]["transferId"]
    node.state.put_effect(f"source-receipt:{xfer_id}", fixture["receipt"], now=NOW)
    with patch(
        "deepseek_infra.infra.workspace.federated_replica_attestation.verify_and_record_replica_attestation",
        side_effect=RuntimeError("attest-err"),
    ):
        with pytest.raises(RuntimeError):
            node.verify_replica_attestation(xfer_id, {
                "attestation": {"valid": True},
                "remoteReceiptBase64": "aGVsbG8=",
                "remoteCommitBase64": "aGVsbG8=",
            })

    # 6. run_dr_drill domain error
    node.state.put_effect(
        f"replica-commit:{fixture['transferId']}",
        {"attestation": {"schema": "attest", "transferId": fixture["transferId"]}},
        now=NOW,
    )
    with patch.object(
        federated_dr_drill,
        "run_federated_dr_drill",
        side_effect=RuntimeError("dr-err"),
    ):
        with pytest.raises(RuntimeError):
            node.run_dr_drill(fixture["transferId"], {"requestId": "req-drill-fail-branch"})

    # 7. verify_dr_attestation domain error
    with patch.object(
        federated_dr_drill,
        "verify_and_record_dr_drill_attestation",
        side_effect=RuntimeError("dr-verify-err"),
    ):
        with pytest.raises(RuntimeError):
            node.verify_dr_attestation(fixture["transferId"], {"attestation": {"transferId": fixture["transferId"]}})

    # 8. _require_transfer domain error
    with patch.object(fixture["journal"], "get_transfer", side_effect=RuntimeError("journal-err")):
        with pytest.raises(RuntimeError):
            node._require_transfer(fixture["transferId"])

    # 9. _advance domain error
    with patch.object(node._transfer_journal, "advance_transfer", side_effect=RuntimeError("advance-err")):
        with pytest.raises(RuntimeError):
            node._advance(
                {"transferId": fixture["transferId"], "state": federation_transfer_journal.TRANSFER_STATES[0], "revision": 1},
                federation_transfer_journal.TRANSFER_STATES[1],
                {},
                now=NOW,
            )

    # 10. _read_node_config corrupted json
    corrupted_cfg = tmp_settings / "corrupted.json"
    corrupted_cfg.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(federation_node.FederationNodeError) as exc:
        federation_node._read_node_config(corrupted_cfg)
    assert exc.value.code == "FEDERATION_NODE_CONFIG_INVALID"

    # 11. _configure_custody configure_peer domain error
    custody_reg = federation_custody_capability.FederationCustodyCapabilityRegistry(
        tmp_settings / "custody-err.sqlite3",
        fixture["identityB"],
    )
    with patch.object(custody_reg, "configure_peer", side_effect=RuntimeError("custody-err")):
        with pytest.raises(RuntimeError):
            federation_node._configure_custody(
                {
                    "fleetId": "fleet-b",
                    "custody": {
                        "peerFleetId": "fleet-a",
                        "mode": federation_custody_capability.COLD_CUSTODY,
                        "actor": "admin",
                    },
                },
                registry=fixture["registry"],
                custody_registry=custody_reg,
                recovery_age_identity=None,
                now=NOW,
            )

    # 12. load_federation_node load_online_signer error
    public_b_path = tmp_settings / "public_b_err.json"
    public_b_path.write_text(json.dumps(fixture["identityB"]), encoding="utf-8")
    signer_b_path = tmp_settings / "fleet-b" / "signer.bundle.json"
    with patch.object(backup_targets, "get_target", return_value={"kind": "s3", "targetId": TARGET_ID}):
        cfg_err = {
            "schema": federation_node.NODE_CONFIG_SCHEMA,
            "fleetId": "fleet-b",
            "publicIdentityPath": str(public_b_path),
            "signerBundlePath": str(signer_b_path),
            "peerRegistryPath": str(tmp_settings / "fleet-b" / "trust.sqlite3"),
            "transferJournalPath": "xfer_err.sqlite3",
            "receiverDbPath": "receiver_err.sqlite3",
            "stagingDir": "staging_err",
            "durabilityDbPath": "durability_err.sqlite3",
            "custodyDbPath": "custody_err.sqlite3",
            "nodeStateDbPath": "node_err.sqlite3",
            "remoteTargetId": TARGET_ID,
            "failureDomainMetadata": _metadata(region="cn-south-1"),
            "readiness": {
                "wireCompatibility": ["object-set-v1"],
                "availableFailureDomains": ["cn-south-1"],
                "forecastHeadroom": 1024,
                "costClass": "standard",
                "readiness": "READY",
            },
            "maxIngressBytes": 64 * 1024 * 1024,
            "ownerInstanceId": "worker-1",
            "custody": {
                "peerFleetId": "fleet-a",
                "mode": federation_custody_capability.COLD_CUSTODY,
                "actor": "admin",
            },
        }
        cfg_err_path = tmp_settings / "test_load_signer_err.json"
        cfg_err_path.write_text(json.dumps(cfg_err), encoding="utf-8")
        with patch.object(federation_identity, "load_online_signer", side_effect=RuntimeError("signer-err")):
            with pytest.raises(RuntimeError):
                federation_node.load_federation_node(
                    cfg_err_path,
                    signer_passphrase=b"fleet-b-signer-passphrase-replica",
                    clock=lambda: NOW,
                )

    # 13. propose_transfer and verify_ingress_grant domain error
    with patch("deepseek_infra.infra.workspace.federation_transfer.propose_transfer", side_effect=RuntimeError("prop-err")):
        with pytest.raises(RuntimeError):
            node.propose_transfer({"destinationFleetId": "fleet-a", "sourceReceipt": fixture["receipt"]})

    with patch("deepseek_infra.infra.workspace.federation_ingress_grant.verify_ingress_grant", side_effect=RuntimeError("verify-grant-err")):
        with pytest.raises(RuntimeError):
            node.verify_ingress_grant({"grant": {"transferId": xfer_id, "schema": "test"}})

    # 14. issue_challenge and verify_challenge domain error
    with patch("deepseek_infra.infra.workspace.federation_challenge.issue_federation_challenge", side_effect=RuntimeError("chal-err")):
        with pytest.raises(RuntimeError):
            node.issue_challenge({"destinationFleetId": "fleet-a"})

    with patch("deepseek_infra.infra.workspace.federation_challenge.verify_federation_challenge_response", side_effect=RuntimeError("resp-err")):
        with pytest.raises(RuntimeError):
            node.verify_challenge({"challenge": {"schema": "test"}, "response": {"schema": "test"}})

    # 15. reconcile_transfer domain error
    with patch("deepseek_infra.infra.workspace.federation_transfer.reconcile_transfer", side_effect=RuntimeError("rec-err")):
        with pytest.raises(RuntimeError):
            node.reconcile_transfer(fixture["transferId"], fixture["grant"]["grantId"])

