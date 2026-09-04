from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from deepseek_infra.infra.mcp.protocol_preparation import prepare_mcp_protocol_json
from deepseek_infra.infra.workspace import (
    backup_control_authority,
    backup_object_set,
    backup_publish,
    backup_target_store,
    federated_replica_attestation,
    federation_identity,
)
from deepseek_infra.infra.workspace.federated_replica_attestation import REPLICA_ATTESTATION_FIELDS
from deepseek_infra.infra.workspace.federated_replica_commit import COMMIT_V4_FIELDS, RECEIPT_V4_FIELDS
from scripts import check_mcp_protocol_parity as mcp_parity
from scripts.native_runtime_contract import sha256_file, validate_corpora, validate_corpus


ROOT = Path(__file__).resolve().parents[1]


def test_corpus_hash_is_stable_across_crlf_checkouts(tmp_path: Path) -> None:
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    lf.write_bytes(b'{"ok":true}\n')
    crlf.write_bytes(b'{"ok":true}\r\n')
    assert sha256_file(lf) == sha256_file(crlf)


def test_canonical_corpora_match_frozen_digests() -> None:
    manifest = validate_corpus()
    ids = {item["id"] for item in manifest["corpora"]}
    assert {
        "mcp-protocol-preparation",
        "gateway-request-preparation",
        "rag-parity",
        "storage-wire-inventory",
        "federation-wire-inventory",
        "evidence-envelope",
        "state-legal-transitions",
        "http-rest-inventory",
        "control-shadow-decisions",
        "control-authority-checkpoints",
    } <= ids

    manifests = validate_corpora()
    assert len(manifests) == 3
    assert manifests[1]["compatibility_reason"]
    assert manifests[2]["compatibility_reason"]


def test_storage_v2_semantic_vector_matches_python_4_8_0_bytes() -> None:
    manifest_path = ROOT / "compat" / "native-runtime" / "v2" / "manifest.json"
    manifest = validate_corpus(manifest_path)
    path = next(
        item["path"]
        for item in manifest["corpora"]
        if item["id"] == "storage-receipt-commit-semantics-v2"
    )
    corpus = json.loads((ROOT / path).read_text(encoding="utf-8"))
    case = corpus["cases"][0]

    assert backup_object_set.object_inventory_digest(case["objects"]) == case["object_set_digest"]
    normalized = sorted(case["objects"], key=lambda item: (item["digest"], item["size"]))
    commitment = "".join(f"{item['digest']}:{item['size']}\n" for item in normalized)
    assert commitment == case["commitment"]

    receipt_bytes = (json.dumps(case["receipt"], ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert hashlib.sha256(receipt_bytes).hexdigest() == case["receipt_digest"]
    assert backup_object_set.committed_object_inventory(case["receipt"]) == normalized

    commit = case["commit"]
    assert backup_target_store.commit_slot_digest(commit["scheduleSlot"]) == commit["slotDigest"]
    assert backup_publish._commit_hash(commit) == commit["commitHash"]


def test_federation_v3_semantic_vector_matches_python_4_8_0_verifier() -> None:
    manifest_path = ROOT / "compat" / "native-runtime" / "v3" / "manifest.json"
    manifest = validate_corpus(manifest_path)
    path = next(
        item["path"]
        for item in manifest["corpora"]
        if item["id"] == "federated-replica-attestation-semantics-v3"
    )
    fixture = json.loads((ROOT / path).read_text(encoding="utf-8"))
    attestation = fixture["attestation"]
    certificate = attestation["signerCertificate"]
    now = datetime.fromisoformat(fixture["now"].replace("Z", "+00:00"))

    verified = federation_identity.verify_federation_document(
        attestation,
        certificate=certificate,
        root_identity=fixture["root_identity"],
        expected_schema=federated_replica_attestation.REPLICA_ATTESTATION_SCHEMA,
        now=now,
        required_purpose=federation_identity.PURPOSE_REPLICA_ATTESTATION,
    )
    sequence, _committed_at = federated_replica_attestation._attestation_semantics(
        verified,
        transfer=fixture["transfer"],
        pinned_metadata=fixture["pinned_metadata"],
        now=now,
        max_future_skew_seconds=fixture["max_future_skew_seconds"],
    )
    assert sequence == 1
    remote_receipt_bytes = federated_replica_attestation._storage_document_bytes(fixture["remote_receipt"])
    remote_commit_bytes = federated_replica_attestation._storage_document_bytes(fixture["remote_commit"])
    federated_replica_attestation._validate_remote_documents(
        source_receipt=fixture["source_receipt"],
        remote_receipt_bytes=remote_receipt_bytes,
        remote_commit_bytes=remote_commit_bytes,
        transfer=fixture["transfer"],
        attestation=verified,
    )
    assert federated_replica_attestation.attestation_digest(verified) == fixture["attestation_digest"]


def test_control_authority_corpus_matches_frozen_python_v1_bytes() -> None:
    manifest = validate_corpus()
    path = next(
        item["path"]
        for item in manifest["corpora"]
        if item["id"] == "control-authority-checkpoints"
    )
    corpus = json.loads((ROOT / path).read_text(encoding="utf-8"))
    assert corpus["schema_version"] == 1
    assert corpus["source_version"] == "4.8.0"
    assert corpus["source_commit"] == "a37735c68398fc8f795babaa269e2de6a5acd567"
    checkpoints = corpus["checkpoints"]
    assert len(checkpoints) == 2
    for checkpoint in checkpoints:
        backup_control_authority.verify_authority_checkpoint_integrity(checkpoint)
        assert checkpoint["payloadDigest"] == backup_control_authority.compute_payload_digest(checkpoint)
        assert checkpoint["digest"] == backup_control_authority.compute_checkpoint_digest(checkpoint)
    backup_control_authority.verify_authority_chain(checkpoints)
    backup_control_authority.assert_logical_head_transition(
        current_generation=None,
        current_digest=None,
        candidate=checkpoints[0],
    )
    backup_control_authority.assert_logical_head_transition(
        current_generation=checkpoints[0]["authorityGeneration"],
        current_digest=checkpoints[0]["digest"],
        candidate=checkpoints[1],
    )


def test_storage_inventory_matches_python_4_8_0_field_sets() -> None:
    inventory = validate_corpus()
    path = next(item["path"] for item in inventory["corpora"] if item["id"] == "storage-wire-inventory")
    data = (ROOT / path).read_text(encoding="utf-8")
    assert "object-set-v1" in data
    stored = json.loads((ROOT / path).read_text(encoding="utf-8"))
    assert set(stored["receipt_v4_fields"]) == set(RECEIPT_V4_FIELDS)
    assert set(stored["commit_v4_fields"]) == set(COMMIT_V4_FIELDS)


def test_federation_inventory_matches_python_attestation_fields() -> None:
    path = ROOT / "compat/native-runtime/v1/federation/wire_inventory.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert set(stored["replica_attestation_fields"]) == set(REPLICA_ATTESTATION_FIELDS)


def test_python_mcp_oracle_still_replays_canonical_fixture() -> None:
    case = next(item for item in mcp_parity.load_fixture() if item["name"] == "ping_integer_id")
    result = prepare_mcp_protocol_json(mcp_parity.raw_case(case))
    assert mcp_parity._matches_expectation(result, case["expect"])
