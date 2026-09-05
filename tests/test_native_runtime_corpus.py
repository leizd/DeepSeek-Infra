from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.infra.mcp.protocol_preparation import prepare_mcp_protocol_json
from deepseek_infra.infra.workspace import (
    backup_control_authority,
    backup_object_set,
    backup_publish,
    backup_target_store,
    evidence_proof,
    federated_dr_proof,
    federated_replica_proof,
    federated_replica_attestation,
    federation_runtime_proof,
    federation_identity,
    federation_trust_proof,
    federation_transfer_journal,
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
    assert len(manifests) == 23
    assert manifests[1]["compatibility_reason"]
    assert manifests[2]["compatibility_reason"]
    assert manifests[3]["compatibility_reason"]
    assert manifests[4]["compatibility_reason"]
    assert manifests[5]["compatibility_reason"]
    assert manifests[6]["compatibility_reason"]
    assert manifests[7]["compatibility_reason"]
    assert manifests[8]["compatibility_reason"]
    assert manifests[9]["compatibility_reason"]
    assert manifests[10]["compatibility_reason"]
    assert manifests[11]["compatibility_reason"]
    assert manifests[12]["compatibility_reason"]
    assert manifests[13]["compatibility_reason"]
    assert manifests[14]["compatibility_reason"]
    assert manifests[15]["compatibility_reason"]
    assert manifests[16]["compatibility_reason"]
    assert manifests[17]["compatibility_reason"]
    assert manifests[18]["compatibility_reason"]
    assert manifests[19]["compatibility_reason"]
    assert manifests[20]["compatibility_reason"]
    assert manifests[21]["compatibility_reason"]
    assert manifests[22]["compatibility_reason"]


def _apply_frozen_mutation(value: Any, *, op: str, pointer: str, replacement: Any) -> None:
    parts = pointer.lstrip("/").split("/")
    target = value
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    leaf = parts[-1]
    if op in {"add", "replace"}:
        if isinstance(target, list):
            target[int(leaf)] = copy.deepcopy(replacement)
        else:
            target[leaf] = copy.deepcopy(replacement)
        return
    if op == "remove":
        if isinstance(target, list):
            target.pop(int(leaf))
        else:
            target.pop(leaf)
        return
    raise AssertionError(f"unsupported frozen mutation operation: {op}")


def test_federated_replica_v10_semantic_vector_matches_python_4_8_0_validator() -> None:
    manifest_path = ROOT / "compat" / "native-runtime" / "v10" / "manifest.json"
    manifest = validate_corpus(manifest_path)
    path = next(
        item["path"]
        for item in manifest["corpora"]
        if item["id"] == "federated-replica-proof-semantics-v10"
    )
    fixture = json.loads((ROOT / path).read_text(encoding="utf-8"))
    proof = fixture["valid_proof"]
    assert fixture["check_names"] == list(federated_replica_proof.FEDERATED_REPLICA_PROOF_CHECKS)
    assert federated_replica_proof.validate_federated_replica_proof(proof) == []
    assert federated_replica_proof.validate_federated_replica_proof([]) == fixture["non_object_errors"]
    assert federated_replica_proof.validate_federated_replica_proof({}) == fixture["empty_object_errors"]
    for mutation in fixture["mutation_cases"]:
        candidate = copy.deepcopy(proof)
        _apply_frozen_mutation(
            candidate,
            op=mutation["op"],
            pointer=mutation["pointer"],
            replacement=mutation["value"],
        )
        if mutation["rebind_proof"]:
            candidate["proofDigest"] = federated_replica_proof.proof_digest(candidate)
        assert federated_replica_proof.validate_federated_replica_proof(candidate) == mutation["expected_errors"], mutation["name"]


def test_federated_dr_v11_semantic_vector_matches_python_4_8_0_validator() -> None:
    manifest_path = ROOT / "compat" / "native-runtime" / "v11" / "manifest.json"
    manifest = validate_corpus(manifest_path)
    path = next(
        item["path"]
        for item in manifest["corpora"]
        if item["id"] == "federated-dr-proof-semantics-v11"
    )
    fixture = json.loads((ROOT / path).read_text(encoding="utf-8"))
    proof = fixture["valid_proof"]
    assert fixture["check_names"] == list(federated_dr_proof.FEDERATED_DR_PROOF_CHECKS)
    assert federated_dr_proof.validate_federated_dr_proof(proof) == []
    assert federated_dr_proof.validate_federated_dr_proof([]) == fixture["non_object_errors"]
    assert federated_dr_proof.validate_federated_dr_proof({}) == fixture["empty_object_errors"]
    for mutation in fixture["mutation_cases"]:
        candidate = copy.deepcopy(proof)
        _apply_frozen_mutation(
            candidate,
            op=mutation["op"],
            pointer=mutation["pointer"],
            replacement=mutation["value"],
        )
        if mutation["rebind_proof"]:
            candidate["proofDigest"] = federated_dr_proof.proof_digest(candidate)
        assert federated_dr_proof.validate_federated_dr_proof(candidate) == mutation["expected_errors"], mutation["name"]


def test_federation_trust_v12_semantic_vector_matches_python_4_8_0_validator() -> None:
    manifest_path = ROOT / "compat" / "native-runtime" / "v12" / "manifest.json"
    manifest = validate_corpus(manifest_path)
    path = next(
        item["path"]
        for item in manifest["corpora"]
        if item["id"] == "federation-trust-proof-semantics-v12"
    )
    fixture = json.loads((ROOT / path).read_text(encoding="utf-8"))
    proof = fixture["valid_proof"]
    assert fixture["check_names"] == list(federation_trust_proof.FEDERATION_TRUST_PROOF_CHECKS)
    assert federation_trust_proof.validate_federation_trust_proof(proof) == []
    assert federation_trust_proof.validate_federation_trust_proof([]) == fixture["non_object_errors"]
    assert federation_trust_proof.validate_federation_trust_proof({}) == fixture["empty_object_errors"]
    for mutation in fixture["mutation_cases"]:
        candidate = copy.deepcopy(proof)
        for operation in mutation["operations"]:
            _apply_frozen_mutation(
                candidate,
                op=operation["op"],
                pointer=operation["pointer"],
                replacement=operation["value"],
            )
        if mutation["rebind_proof"]:
            candidate["proofDigest"] = federation_trust_proof.proof_digest(candidate)
        assert federation_trust_proof.validate_federation_trust_proof(candidate) == mutation["expected_errors"], mutation["name"]


def test_recovery_evidence_v13_semantic_vector_matches_python_4_8_0_validators() -> None:
    manifest_path = ROOT / "compat" / "native-runtime" / "v13" / "manifest.json"
    manifest = validate_corpus(manifest_path)
    path = next(
        item["path"]
        for item in manifest["corpora"]
        if item["id"] == "recovery-evidence-semantics-v13"
    )
    fixture = json.loads((ROOT / path).read_text(encoding="utf-8"))
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"

    validators = {
        "restore": evidence_proof.validate_restore_proof,
        "backup_commit": evidence_proof.validate_backup_commit_proof,
        "distinct_pid": evidence_proof.validate_distinct_pid_proof,
        "sigkill": evidence_proof.validate_sigkill_proof,
        "epoch_increase": evidence_proof.validate_epoch_increase_proof,
        "minio_endpoints": evidence_proof.validate_minio_endpoints_proof,
        "schema_only": evidence_proof.validate_pass_with_schema_only,
    }
    expected_checks = {
        "realPreDisasterBackupIsActuallyRestored",
        "realFreshProcessRestoresPreDisasterBackup",
        "restoredWorkspaceDigestMatchesPreDisasterDigest",
        "realPostRecoveryBackupHasValidCommit",
        "realFreshProcessCreatesPostRecoveryBackup",
        "realPostRecoveryBackupHasValidReceiptBinding",
        "freshProcessAAndBHaveDifferentPids",
        "processAIsDeadBeforeProcessBStarts",
        "processAExitedBySigkill",
        "realFreshProcessBootEpochStrictlyIncreases",
        "realThreeMinioProcessReplacementE2E",
        "realThreeMinioFreshProcessAuthorityRecoveryE2E",
        "realThreeMinioAutonomousRepairE2E",
        "realThreeMinioAutonomousRebalanceE2E",
        "realThreeMinioPredictivePlanningE2E",
        "evidenceCheckCannotPassWithoutStructuredProof",
    }
    observed_checks: set[str] = set()
    for group in fixture["groups"]:
        validator = validators[group["validator"]]
        valid_evidence = next(case["evidence"] for case in group["cases"] if not case["expected_errors"])
        for case in group["cases"]:
            assert validator(case["evidence"], "frozen-v13") == case["expected_errors"], case["name"]
        for check_name in group["check_names"]:
            observed_checks.add(check_name)
            assert evidence_proof.VALIDATORS[check_name] is validator
            assert evidence_proof.validate_check(
                check_name,
                {"status": "PASS", "evidence": valid_evidence},
            ) == []
    assert observed_checks == expected_checks


def test_recovery_coercions_v13_match_python_on_parsed_documents() -> None:
    path = ROOT / "compat" / "native-runtime" / "v13" / "evidence" / "recovery_coercions_vector.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert fixture["source_commit"] == "a37735c68398fc8f795babaa269e2de6a5acd567"
    for case in fixture["cases"]:
        document = json.loads(case["document"])
        check_name = case["check_name"]
        assert evidence_proof.validate_check(check_name, document["checks"][check_name]) == case["expected_errors"], case["name"]


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


def test_evidence_v4_semantic_vector_matches_python_4_8_0_validator() -> None:
    manifest_path = ROOT / "compat" / "native-runtime" / "v4" / "manifest.json"
    manifest = validate_corpus(manifest_path)
    path = next(
        item["path"]
        for item in manifest["corpora"]
        if item["id"] == "evidence-proof-v2-dr-readiness-semantics-v4"
    )
    fixture = json.loads((ROOT / path).read_text(encoding="utf-8"))
    envelope = fixture["valid_envelope"]
    assert envelope["schema"] == evidence_proof.EVIDENCE_PROOF_SCHEMA
    assert envelope["scenario"] == "native-evidence-proof-parity"
    assert envelope["checks"]
    for check_name, item in envelope["checks"].items():
        assert evidence_proof.validate_check(check_name, item) == []
    for invalid in fixture["invalid_checks"]:
        assert evidence_proof.validate_check(invalid["check_name"], invalid["item"]) == invalid["expected_errors"]


def test_federation_runtime_v5_semantic_vector_matches_python_4_8_0_validator() -> None:
    manifest_path = ROOT / "compat" / "native-runtime" / "v5" / "manifest.json"
    manifest = validate_corpus(manifest_path)
    path = next(
        item["path"]
        for item in manifest["corpora"]
        if item["id"] == "federation-runtime-e2e-proof-semantics-v5"
    )
    fixture = json.loads((ROOT / path).read_text(encoding="utf-8"))
    proof = fixture["valid_proof"]
    assert federation_runtime_proof.validate_federation_runtime_proof(proof) == []
    assert federation_runtime_proof.proof_digest(proof) == proof["proofDigest"]
    for check_name in federation_runtime_proof.FEDERATION_RUNTIME_PROOF_CHECKS:
        assert evidence_proof.validate_check(check_name, {"status": "PASS", "evidence": proof}) == []

    for invalid in fixture["invalid_cases"]:
        mutated = copy.deepcopy(proof)
        parts = invalid["path"].lstrip("/").split("/")
        target = mutated
        for part in parts[:-1]:
            target = target[int(part)] if isinstance(target, list) else target[part]
        leaf = parts[-1]
        if isinstance(target, list):
            target[int(leaf)] = invalid["replacement"]
        else:
            target[leaf] = invalid["replacement"]
        assert federation_runtime_proof.validate_federation_runtime_proof(mutated) == invalid["expected_errors"]


def test_transfer_journal_v6_semantic_vector_matches_python_4_8_0_state_machine(tmp_path: Path) -> None:
    manifest_path = ROOT / "compat" / "native-runtime" / "v6" / "manifest.json"
    manifest = validate_corpus(manifest_path)
    path = next(
        item["path"]
        for item in manifest["corpora"]
        if item["id"] == "federated-transfer-journal-semantics-v6"
    )
    fixture = json.loads((ROOT / path).read_text(encoding="utf-8"))
    proposed = fixture["proposed"]
    steps = fixture["steps"]
    assert federation_transfer_journal.derive_transfer_id(
        source_fleet_id=proposed["sourceFleetId"],
        destination_fleet_id=proposed["destinationFleetId"],
        backup_id=proposed["backupId"],
        object_set_digest=proposed["objectSetDigest"],
    ) == proposed["transferId"]

    sender = federation_transfer_journal.FederatedTransferJournal(
        tmp_path / "sender.sqlite3",
        fixture["sender_identity"],
    )
    receiver = federation_transfer_journal.FederatedTransferJournal(
        tmp_path / "receiver.sqlite3",
        fixture["receiver_identity"],
    )
    now = datetime(2026, 9, 1, 7, 0, tzinfo=timezone.utc)
    record = sender.persist_proposed_transfer(
        transfer_id=proposed["transferId"],
        source_fleet_id=proposed["sourceFleetId"],
        destination_fleet_id=proposed["destinationFleetId"],
        policy_id=proposed["policyId"],
        backup_id=proposed["backupId"],
        object_set_digest=proposed["objectSetDigest"],
        now=now,
    )
    assert record["role"] == "SENDER"
    assert record["identityDigest"] == fixture["identity_digest"]
    for index, step in enumerate(steps):
        if index:
            record = sender.advance_transfer(
                proposed["transferId"],
                expected_revision=index,
                next_state=step["state"],
                details=step["details"],
                now=now + timedelta(seconds=index),
            )
        assert record["state"] == step["state"]
        assert record["stateDetails"] == step["details"]
        assert record["statePayloadDigest"] == step["state_payload_digest"]
        assert record["revision"] == index + 1
        assert record["updatedAt"] == step["at"]

    events = sender.list_transfer_events(proposed["transferId"])
    assert len(events) == len(steps)
    for index, (event, step) in enumerate(zip(events, steps, strict=True)):
        assert event["sequence"] == index + 1
        assert event["previousState"] == (steps[index - 1]["state"] if index else None)
        assert event["nextState"] == step["state"]
        assert event["stateDetails"] == step["details"]
        assert event["statePayloadDigest"] == step["state_payload_digest"]
        assert event["occurredAt"] == step["at"]

    receiver_record = receiver.persist_proposed_transfer(
        transfer_id=proposed["transferId"],
        source_fleet_id=proposed["sourceFleetId"],
        destination_fleet_id=proposed["destinationFleetId"],
        policy_id=proposed["policyId"],
        backup_id=proposed["backupId"],
        object_set_digest=proposed["objectSetDigest"],
        now=now,
    )
    assert receiver_record["role"] == "RECEIVER"
    assert receiver_record["statePayloadDigest"] == steps[0]["state_payload_digest"]


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
