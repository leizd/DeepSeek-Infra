from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from deepseek_infra.infra.diagnostics.evidence_inventory import evidence_paths_for_producer
from deepseek_infra.infra.workspace import evidence_proof
from scripts import run_storage_control_plane_minio_e2e, validate_evidence_proof

ROOT = Path(__file__).resolve().parents[1]


def _actual_copy_evidence() -> dict[str, object]:
    backup_id = "bak-proof-474"
    policy_id = "policy-proof-474"
    object_set_digest = hashlib.sha256(b"object-set-v1-proof").hexdigest()
    receipt_bytes = json.dumps(
        {
            "schemaVersion": 4,
            "backupId": backup_id,
            "objectSetDigest": object_set_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    commit_bytes = json.dumps(
        {
            "schemaVersion": 4,
            "backupId": backup_id,
            "policyId": policy_id,
            "receiptDigest": receipt_sha256,
            "objectSetDigest": object_set_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    commit_sha256 = hashlib.sha256(commit_bytes).hexdigest()
    return {
        "targetId": "target_minio_b",
        "endpoint": "http://127.0.0.1:9001",
        "bucket": "backup-b-proof",
        "prefix": "resilience-474-proof",
        "backupId": backup_id,
        "policyId": policy_id,
        "actionId": "act-proof-474",
        "receiptKey": f"receipts/{backup_id}.json",
        "commitKey": f"commits/{policy_id}/{backup_id}.json",
        "receiptBytesBase64": base64.b64encode(receipt_bytes).decode("ascii"),
        "commitBytesBase64": base64.b64encode(commit_bytes).decode("ascii"),
        "rawReceiptSha256": receipt_sha256,
        "rawCommitSha256": commit_sha256,
        "commitReceiptDigest": receipt_sha256,
        "objectSetDigest": object_set_digest,
        "providerReceiptObject": {
            "key": f"receipts/{backup_id}.json",
            "size": len(receipt_bytes),
            "etag": "receipt-etag",
            "sha256": receipt_sha256,
        },
        "providerCommitObject": {
            "key": f"commits/{policy_id}/{backup_id}.json",
            "size": len(commit_bytes),
            "etag": "commit-etag",
            "sha256": commit_sha256,
        },
    }


def test_autonomous_copy_proof_recomputes_actual_receipt_and_commit_bytes() -> None:
    evidence = _actual_copy_evidence()

    assert evidence_proof.validate_check(
        "destinationReceiptAuthenticated",
        {"status": "PASS", "evidence": evidence},
    ) == []
    assert evidence_proof.validate_check(
        "realReplicaTransferUsesEndpointAAndB",
        {"status": "PASS", "evidence": {**evidence, "endpointA": "http://127.0.0.1:9000", "endpointB": evidence["endpoint"]}},
    ) == []


def test_autonomous_copy_proof_rejects_synthetic_and_mismatched_digests() -> None:
    synthetic = {
        "backupId": "bak-proof-474",
        "commitKey": "commits/bak-proof-474.commit",
        "receiptKey": "receipts/bak-proof-474.receipt",
        "receiptDigest": hashlib.sha256(b"receipt-b").hexdigest(),
        "objectSetDigest": hashlib.sha256(b"obj-set").hexdigest(),
    }
    synthetic_errors = evidence_proof.validate_check(
        "destinationReceiptAuthenticated",
        {"status": "PASS", "evidence": synthetic},
    )
    assert "missing-field:receiptBytesBase64" in synthetic_errors
    assert "missing-field:commitBytesBase64" in synthetic_errors

    evidence = _actual_copy_evidence()
    evidence["commitReceiptDigest"] = "0" * 64
    mismatch_errors = evidence_proof.validate_check(
        "destinationCommitAuthenticated",
        {"status": "PASS", "evidence": evidence},
    )
    assert "receipt-digest-binding-mismatch" in mismatch_errors


def test_autonomous_copy_proof_rejects_non_v4_or_wrong_object_set_bytes() -> None:
    evidence = _actual_copy_evidence()
    receipt = json.loads(base64.b64decode(str(evidence["receiptBytesBase64"])))
    receipt["schemaVersion"] = 3
    receipt["objectSetDigest"] = "f" * 64
    receipt_bytes = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    evidence["receiptBytesBase64"] = base64.b64encode(receipt_bytes).decode("ascii")
    evidence["rawReceiptSha256"] = hashlib.sha256(receipt_bytes).hexdigest()

    errors = evidence_proof.validate_check(
        "destinationReceiptAuthenticated",
        {"status": "PASS", "evidence": evidence},
    )
    assert "receipt-schema-not-v4" in errors
    assert "receipt-object-set-digest-mismatch" in errors


def test_real_minio_producer_has_no_synthetic_digest_or_fallback_proof_path() -> None:
    source = (ROOT / "tests" / "test_backup_472_real_three_minio_remediation_e2e.py").read_text(encoding="utf-8")

    assert 'hashlib.sha256(b"receipt-' not in source
    assert 'hashlib.sha256(b"commit-' not in source
    assert 'hashlib.sha256(b"obj-set")' not in source
    assert "receiptBytesBase64" in source
    assert "commitBytesBase64" in source
    assert "resolve_proof_path" in source


def test_storage_control_plane_inventory_and_ci_require_exact_proof_artifact() -> None:
    proof_path = "docs/evidence/storage-control-plane-autonomous-proof-v4.7.4.json"
    owned_paths = evidence_paths_for_producer("storage-control-plane-minio-e2e", "4.7.4")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    runner = (ROOT / "scripts" / "run_storage_control_plane_minio_e2e.py").read_text(encoding="utf-8")

    assert proof_path in owned_paths
    assert "proofArtifact" in runner
    assert "storage-control-plane-autonomous-proof-v${{ env.RELEASE_VERSION }}.json" in workflow


def test_474_required_check_names_are_locked_to_proof_or_explicit_scenarios() -> None:
    proof_names = {
        "autonomousProofUsesActualReceiptBytes",
        "autonomousProofUsesActualCommitBytes",
        "receiptSha256MatchesCommitReceiptDigest",
        "proofObjectSetDigestMatchesCommit",
        "proofObjectKeysExistOnExpectedMinioEndpoint",
        "degradedFleetCannotBeFurtherDegraded",
        "runningEffectsParticipateInBlastRadiusSimulation",
        "receiptV4Unchanged",
        "commitV4Unchanged",
    }
    durable_fleet_names = {
        "riskFirstSeenPersistsAcrossControlLoops",
        "riskDebtAgeIncreasesAcrossPlannerRuns",
        "clearedRiskStopsAccumulatingDebt",
        "reopenedRiskUsesPersistentLifecycle",
        "productionSchedulerUsesPersistentFairnessHistory",
        "weightedFairSchedulingPreventsPolicyStarvation",
        "allSchedulableActionsReceiveExecutionWave",
        "dependenciesArePreservedAcrossWaves",
        "conflictingActionsAreSeparatedAcrossWaves",
        "unschedulableActionHasTypedReason",
        "rebalanceCannotConsumeRepairReservedBandwidth",
        "repairReserveUsesRealTransferBudget",
        "safePreemptionReleasesBudgetAtomically",
        "unsafePreemptionCannotModifyVictim",
        "fleetSloSamplesPersistAcrossRestart",
        "riskClearLatencyIsMeasured",
        "remediationQueueDelayIsMeasured",
        "leaseTakeoverLatencyIsMeasured",
        "fastAndSlowBurnRatesAreComputed",
        "criticalRepairOverridesMaintenanceWindow",
        "rebalanceRespectsMaintenanceWindow",
        "criticalDrStalenessMayOverrideWindow",
    }
    required_proof = set(
        run_storage_control_plane_minio_e2e.REQUIRED_PROOF_CHECKS[
            run_storage_control_plane_minio_e2e.AUTONOMOUS_REMEDIATION_SCENARIO
        ]
    )
    assert proof_names <= required_proof
    assert durable_fleet_names <= set(run_storage_control_plane_minio_e2e.CHECK_SCENARIOS)
    assert all(
        run_storage_control_plane_minio_e2e.CHECK_SCENARIOS[name]
        == run_storage_control_plane_minio_e2e.DURABLE_FLEET_SCENARIO
        for name in durable_fleet_names
    )
    assert {
        "fastCdcV3Unchanged",
        "randomizedAgeUnchanged",
        "controlAuthorityV1Unchanged",
        "authorityCheckpointV1Unchanged",
    } <= set(run_storage_control_plane_minio_e2e.CHECK_SCENARIOS)


def test_validate_evidence_proof_cli_reports_exact_bytes_and_digest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "proof.json"
    evidence_proof.write_evidence_proof(
        path,
        scenario="proof-cli-scenario",
        checks={"operatorClaim": {"status": "PASS", "evidence": {"source": "durable-journal"}}},
    )

    assert validate_evidence_proof.main(["--proof", str(path), "--scenario", "proof-cli-scenario"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    raw = path.read_bytes()
    assert result == {
        "bytes": len(raw),
        "checkCount": 1,
        "errors": {},
        "proofPath": str(path.resolve()),
        "scenario": "proof-cli-scenario",
        "schema": evidence_proof.EVIDENCE_PROOF_SCHEMA,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "status": "PASS",
    }


def test_validate_evidence_proof_cli_fails_closed_on_semantic_or_scenario_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "invalid-proof.json"
    evidence_proof.write_evidence_proof(
        path,
        scenario="actual-scenario",
        checks={"operatorClaim": {"status": "PASS", "evidence": {}}},
    )

    assert validate_evidence_proof.main(["--proof", str(path)]) == 1
    semantic_result = json.loads(capsys.readouterr().out)
    assert semantic_result["status"] == "FAIL"
    assert semantic_result["errors"] == {"operatorClaim": ["empty-evidence-for-unknown-check"]}

    assert validate_evidence_proof.main(["--proof", str(path), "--scenario", "wrong-scenario"]) == 1
    scenario_result = json.loads(capsys.readouterr().out)
    assert scenario_result["status"] == "FAIL"
    assert "evidence-proof-scenario-mismatch" in scenario_result["error"]
