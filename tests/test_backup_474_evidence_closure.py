from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.config import APP_VERSION
from deepseek_infra.infra.diagnostics.evidence_inventory import evidence_paths_for_producer
from deepseek_infra.infra.workspace import evidence_proof, resilience_slo_ledger
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


def test_real_predictive_producer_uses_production_sources_without_test_substitution() -> None:
    source = (ROOT / "tests" / "test_backup_476_real_three_minio_predictive_e2e.py").read_text(encoding="utf-8")

    assert "monkeypatch.setattr" not in source
    assert "unittest.mock" not in source
    assert "MemoryTargetStore" not in source
    assert "probe_target_capacity" not in source
    assert "sample_fleet_capacity" in source
    assert "execute_run" in source
    assert "execute_repair_job_instance" in source
    assert "execute_rebalance_job" in source
    assert "simulate_candidate_with_inputs" in source
    assert "capture_predictive_planning_proof" in source
    assert "resolve_proof_path" in source


def test_storage_control_plane_inventory_and_ci_require_exact_proof_artifact() -> None:
    proof_path = f"docs/evidence/storage-control-plane-autonomous-proof-v{APP_VERSION}.json"
    predictive_path = f"docs/evidence/storage-control-plane-predictive-proof-v{APP_VERSION}.json"
    owned_paths = evidence_paths_for_producer("storage-control-plane-minio-e2e", APP_VERSION)
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    runner = (ROOT / "scripts" / "run_storage_control_plane_minio_e2e.py").read_text(encoding="utf-8")

    assert proof_path in owned_paths
    assert predictive_path in owned_paths
    assert "proofArtifacts" in runner
    assert "storage-control-plane-autonomous-proof-v${{ env.RELEASE_VERSION }}.json" in workflow
    assert "storage-control-plane-predictive-proof-v${{ env.RELEASE_VERSION }}.json" in workflow


def test_474_required_check_names_are_locked_to_proof_or_explicit_scenarios() -> None:
    proof_names = {
        "autonomousProofUsesActualReceiptBytes",
        "autonomousProofUsesActualCommitBytes",
        "receiptSha256MatchesCommitReceiptDigest",
        "proofObjectSetDigestMatchesCommit",
        "proofObjectKeysExistOnExpectedMinioEndpoint",
        "degradedFleetCannotBeFurtherDegraded",
        "runningEffectsParticipateInBlastRadiusSimulation",
        "longRunningWaveRenewsScheduleLease",
        "longRunningWaveRenewsWaveLease",
        "realProcessWaveSigkillTakeoverUsesHigherEpoch",
        "realProcessWaveSigkillDoesNotDuplicateEffect",
        "realProcessWaveSigkillSettlesExactlyOnce",
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
        "drReadinessProofV1Unchanged",
        "evidenceProofV2EnvelopeUnchanged",
    } <= set(run_storage_control_plane_minio_e2e.CHECK_SCENARIOS)


def test_validate_evidence_proof_cli_reports_exact_bytes_and_digest(
    tmp_settings: Path,
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
    verification = resilience_slo_ledger.latest_evidence_verification()
    assert verification is not None
    assert verification["metadata"] == {
        "proofSha256": hashlib.sha256(raw).hexdigest(),
        "scenario": "proof-cli-scenario",
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


def test_validate_evidence_proof_cli_fails_when_freshness_cannot_persist(
    tmp_settings: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "proof-slo-failure.json"
    evidence_proof.write_evidence_proof(
        path,
        scenario="proof-slo-failure",
        checks={"operatorClaim": {"status": "PASS", "evidence": {"source": "durable-journal"}}},
    )

    def fail_persistence(**_kwargs: object) -> dict[str, Any]:
        raise OSError("SLO ledger unavailable")

    monkeypatch.setattr(resilience_slo_ledger, "record_evidence_verification", fail_persistence)

    assert validate_evidence_proof.main(["--proof", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "FAIL"
    assert result["errors"] == {
        "$slo": ["evidence-verification-not-durable:OSError:SLO ledger unavailable"]
    }


def test_autonomous_storage_validator_reports_malformed_bytes_and_provider_bindings() -> None:
    not_a_dict: Any = []
    assert evidence_proof.validate_autonomous_storage_bytes_proof(not_a_dict, "proof") == ["not-a-dict"]

    malformed = _actual_copy_evidence()
    malformed.update(
        {
            "receiptBytesBase64": "not-base64%",
            "commitBytesBase64": "also-not-base64%",
            "rawReceiptSha256": "bad",
            "rawCommitSha256": "bad",
            "commitReceiptDigest": "bad",
            "objectSetDigest": "bad",
            "providerReceiptObject": "not-an-object",
            "providerCommitObject": None,
        }
    )
    malformed_errors = evidence_proof.validate_autonomous_storage_bytes_proof(malformed, "proof")
    assert {
        "invalid-base64:receiptBytesBase64",
        "invalid-base64:commitBytesBase64",
        "invalid-sha256:rawReceiptSha256",
        "providerReceiptObject-must-be-object",
        "providerCommitObject-must-be-object",
    } <= set(malformed_errors)

    invalid_json = _actual_copy_evidence()
    receipt_bytes = b"\xff"
    commit_bytes = b"[]"
    invalid_json.update(
        {
            "receiptBytesBase64": base64.b64encode(receipt_bytes).decode("ascii"),
            "commitBytesBase64": base64.b64encode(commit_bytes).decode("ascii"),
            "rawReceiptSha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "rawCommitSha256": hashlib.sha256(commit_bytes).hexdigest(),
            "commitReceiptDigest": hashlib.sha256(receipt_bytes).hexdigest(),
            "providerReceiptObject": {
                "key": invalid_json["receiptKey"],
                "size": len(receipt_bytes),
                "etag": "receipt-etag",
                "sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            },
            "providerCommitObject": {
                "key": invalid_json["commitKey"],
                "size": len(commit_bytes),
                "etag": "commit-etag",
                "sha256": hashlib.sha256(commit_bytes).hexdigest(),
            },
        }
    )
    invalid_json_errors = evidence_proof.validate_autonomous_storage_bytes_proof(invalid_json, "proof")
    assert "invalid-receipt-json" in invalid_json_errors
    assert "commit-must-be-object" in invalid_json_errors

    invalid_schema_type = _actual_copy_evidence()
    receipt = json.loads(base64.b64decode(str(invalid_schema_type["receiptBytesBase64"])))
    commit = json.loads(base64.b64decode(str(invalid_schema_type["commitBytesBase64"])))
    receipt["schemaVersion"] = []
    receipt_bytes = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8")
    receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
    commit["schemaVersion"] = "4"
    commit["receiptDigest"] = receipt_sha256
    commit_bytes = json.dumps(commit, sort_keys=True, separators=(",", ":")).encode("utf-8")
    commit_sha256 = hashlib.sha256(commit_bytes).hexdigest()
    invalid_schema_type.update(
        {
            "receiptBytesBase64": base64.b64encode(receipt_bytes).decode("ascii"),
            "commitBytesBase64": base64.b64encode(commit_bytes).decode("ascii"),
            "rawReceiptSha256": receipt_sha256,
            "rawCommitSha256": commit_sha256,
            "commitReceiptDigest": receipt_sha256,
            "providerReceiptObject": {
                "key": invalid_schema_type["receiptKey"],
                "size": len(receipt_bytes),
                "etag": "receipt-etag",
                "sha256": receipt_sha256,
            },
            "providerCommitObject": {
                "key": invalid_schema_type["commitKey"],
                "size": len(commit_bytes),
                "etag": "commit-etag",
                "sha256": commit_sha256,
            },
        }
    )
    invalid_schema_errors = evidence_proof.validate_autonomous_storage_bytes_proof(invalid_schema_type, "proof")
    assert "receipt-schema-not-v4" in invalid_schema_errors
    assert "commit-schema-not-v4" in invalid_schema_errors

    mismatched = _actual_copy_evidence()
    receipt = {
        "schemaVersion": 3,
        "backupId": "wrong-backup",
        "objectSetDigest": "1" * 64,
    }
    commit = {
        "schemaVersion": 3,
        "backupId": "wrong-backup",
        "policyId": "wrong-policy",
        "receiptDigest": "2" * 64,
        "objectSetDigest": "3" * 64,
    }
    receipt_bytes = json.dumps(receipt).encode("utf-8")
    commit_bytes = json.dumps(commit).encode("utf-8")
    mismatched.update(
        {
            "receiptBytesBase64": base64.b64encode(receipt_bytes).decode("ascii"),
            "commitBytesBase64": base64.b64encode(commit_bytes).decode("ascii"),
            "rawReceiptSha256": "4" * 64,
            "rawCommitSha256": "5" * 64,
            "commitReceiptDigest": "6" * 64,
            "objectSetDigest": "7" * 64,
            "receiptKey": "receipts/placeholder.json",
            "commitKey": "commits/placeholder.json",
            "providerReceiptObject": {"key": "wrong", "size": -1, "etag": "", "sha256": "8" * 64},
            "providerCommitObject": {"key": "wrong", "size": -1, "etag": "", "sha256": "9" * 64},
        }
    )
    mismatch_errors = set(evidence_proof.validate_autonomous_storage_bytes_proof(mismatched, "proof"))
    assert {
        "raw-receipt-sha256-mismatch",
        "raw-commit-sha256-mismatch",
        "receipt-digest-binding-mismatch",
        "receipt-schema-not-v4",
        "commit-schema-not-v4",
        "receipt-backup-id-mismatch",
        "commit-backup-id-mismatch",
        "commit-policy-id-mismatch",
        "commit-receipt-digest-mismatch",
        "receipt-object-set-digest-mismatch",
        "commit-object-set-digest-mismatch",
        "receipt-key-mismatch",
        "commit-key-mismatch",
        "providerReceiptObject-key-mismatch",
        "providerReceiptObject-size-mismatch",
        "providerReceiptObject-sha256-mismatch",
        "providerReceiptObject-etag-missing",
    } <= mismatch_errors


def test_crash_takeover_validator_rejects_fabricated_process_and_event_order() -> None:
    not_a_dict: Any = None
    assert evidence_proof.validate_crash_recovery_proof(not_a_dict, "crash") == ["not-a-dict"]

    invalid_numeric = {"workerAPid": "bad", "journalEvents": []}
    assert "invalid-crash-takeover-numeric-fields" in evidence_proof.validate_crash_recovery_proof(
        invalid_numeric,
        "crash",
    )

    fabricated = {
        "actionId": "action-crash",
        "workerAPid": 10,
        "workerBPid": 10,
        "processAReturnCode": 0,
        "epochA": 2,
        "epochB": 1,
        "repairId": "repair-one",
        "repairPhaseAtCrash": "idle",
        "reconciliationDirective": "CREATE_NEW_EFFECT",
        "workerALeaseUntil": "not-a-time",
        "remoteRepairJobCountBefore": 0,
        "remoteRepairJobCountAfter": 2,
        "remoteRepairJobIdsBefore": [],
        "remoteRepairJobIdsAfter": ["repair-one", "repair-two"],
        "journalEvents": "self-reported",
    }
    fabricated_errors = set(evidence_proof.validate_crash_recovery_proof(fabricated, "crash"))
    assert {
        "worker-pids-not-distinct-positive",
        "worker-a-not-hard-terminated",
        "takeover-execution-epoch-not-increased",
        "underlying-repair-job-count-not-exactly-one",
        "underlying-repair-job-identity-not-stable",
        "invalid-worker-a-lease-expiry",
        "worker-a-not-killed-during-active-repair",
        "invalid-reconciliation-directive",
        "journal-events-must-be-list",
    } <= fabricated_errors

    reversed_events = {
        **fabricated,
        "workerBPid": 20,
        "processAReturnCode": 1,
        "epochA": 1,
        "epochB": 2,
        "repairPhaseAtCrash": "transferring-components",
        "reconciliationDirective": "RESUME_EXECUTION",
        "remoteRepairJobCountBefore": 1,
        "remoteRepairJobCountAfter": 1,
        "workerALeaseUntil": "2026-08-28T12:00:10Z",
        "remoteRepairJobIdsBefore": ["repair-one"],
        "remoteRepairJobIdsAfter": ["repair-one"],
        "journalEvents": [
            None,
            {"executionEpoch": "bad"},
            {
                "eventType": "ACTION_TAKEOVER",
                "state": "RECONCILING",
                "executionEpoch": 2,
                "ownerInstanceId": "worker-b-pid-20",
                "effectHandle": {"kind": "repair", "repairId": "repair-one"},
                "createdAt": "2026-08-28T12:00:09Z",
            },
            {
                "eventType": "STATE_TRANSITION",
                "state": "EXECUTING",
                "executionEpoch": 1,
                "ownerInstanceId": "worker-a-pid-10",
                "effectHandle": {"kind": "repair", "repairId": "repair-one"},
                "createdAt": "2026-08-28T12:00:01Z",
            },
        ],
    }
    assert "reconciling-event-not-after-executing-event" in evidence_proof.validate_crash_recovery_proof(
        reversed_events,
        "crash",
    )
    assert "takeover-occurred-before-worker-a-lease-expiry" in evidence_proof.validate_crash_recovery_proof(
        reversed_events,
        "crash",
    )
    reversed_events["journalEvents"] = []
    missing_event_errors = evidence_proof.validate_crash_recovery_proof(reversed_events, "crash")
    assert "missing-worker-a-executing-effect-event" in missing_event_errors
    assert "missing-worker-b-reconciling-event" in missing_event_errors


def test_blast_radius_validator_rejects_non_monotonic_or_unbound_simulation() -> None:
    not_a_dict: Any = "proof says pass"
    assert evidence_proof.validate_blast_radius_proof(not_a_dict, "blast") == ["not-a-dict"]

    wrong_shape = {
        "simulator": "self-reported",
        "simulationPassed": False,
        "proposedActionIds": "action-one",
        "simulationDetails": [],
    }
    wrong_shape_errors = set(evidence_proof.validate_blast_radius_proof(wrong_shape, "blast"))
    assert {
        "blast-radius-simulator-identity-mismatch",
        "blast-radius-simulation-not-passed",
        "blast-radius-proposed-actions-must-be-list",
        "blast-radius-simulation-details-must-be-object",
    } <= wrong_shape_errors

    missing_evaluations = {
        "simulator": "resilience_coordinator.simulate_coordination_wave",
        "simulationPassed": True,
        "proposedActionIds": ["action-one"],
        "simulationDetails": {
            "passed": False,
            "proposedActionIds": ["other-action"],
            "runningActionIds": "running-action",
            "evaluations": {},
        },
    }
    missing_errors = set(evidence_proof.validate_blast_radius_proof(missing_evaluations, "blast"))
    assert {
        "blast-radius-details-not-passed",
        "blast-radius-proposed-action-binding-mismatch",
        "blast-radius-running-actions-must-be-list",
        "blast-radius-evaluations-missing",
    } <= missing_errors

    unsafe = {
        "simulator": "resilience_coordinator.simulate_coordination_wave",
        "simulationPassed": True,
        "proposedActionIds": ["action-one"],
        "simulationDetails": {
            "passed": True,
            "proposedActionIds": ["action-one"],
            "runningActionIds": [],
            "evaluations": {
                "not-object": [],
                "bad-numeric": {
                    "policyId": "p",
                    "backupId": "b",
                    "minCommittedCopies": "bad",
                    "minFailureDomains": 2,
                    "copiesBefore": 1,
                    "copiesDuring": 1,
                    "copySafetyFloor": 1,
                    "failureDomainsBefore": [],
                    "failureDomainsDuring": [],
                    "failureDomainSafetyFloor": 0,
                    "runningEffectCount": 0,
                    "passed": True,
                },
                "unsafe": {
                    "policyId": "p",
                    "backupId": "b",
                    "minCommittedCopies": 2,
                    "minFailureDomains": 2,
                    "copiesBefore": 1,
                    "copiesDuring": 0,
                    "copySafetyFloor": 2,
                    "failureDomainsBefore": "zone-a",
                    "failureDomainsDuring": "zone-a",
                    "failureDomainSafetyFloor": 2,
                    "runningEffectCount": -1,
                    "passed": False,
                },
                "domain-loss": {
                    "policyId": "p",
                    "backupId": "b",
                    "minCommittedCopies": 2,
                    "minFailureDomains": 2,
                    "copiesBefore": 2,
                    "copiesDuring": 2,
                    "copySafetyFloor": 2,
                    "failureDomainsBefore": ["zone-a", "zone-b"],
                    "failureDomainsDuring": ["zone-a"],
                    "failureDomainSafetyFloor": 2,
                    "runningEffectCount": 0,
                    "passed": True,
                },
            },
        },
    }
    unsafe_errors = set(
        evidence_proof.validate_blast_radius_proof(unsafe, "runningEffectsParticipateInBlastRadiusSimulation")
    )
    assert {
        "blast-radius-evaluation-not-object:not-object",
        "blast-radius-invalid-numeric-fields:bad-numeric",
        "blast-radius-copy-floor-violation:unsafe",
        "blast-radius-failure-domains-must-be-lists:unsafe",
        "blast-radius-negative-running-effect-count:unsafe",
        "blast-radius-evaluation-not-passed:unsafe",
        "blast-radius-domain-floor-violation:domain-loss",
        "blast-radius-running-effects-not-proven",
    } <= unsafe_errors


def test_atomic_budget_validator_rejects_fake_process_results() -> None:
    not_a_dict: Any = 1
    assert evidence_proof.validate_atomic_budget_proof(not_a_dict, "budget") == ["not-a-dict"]
    wrong_shape = {"scope": "invalid", "processResults": {}, "admittedCount": 0, "rejectedCount": 0}
    wrong_shape_errors = evidence_proof.validate_atomic_budget_proof(wrong_shape, "budget")
    assert "invalid-atomic-budget-scope" in wrong_shape_errors
    assert "process-results-must-be-list" in wrong_shape_errors

    fake = {
        "scope": "target",
        "processResults": [
            None,
            {"pid": "bad", "admitted": True},
            {"pid": 0, "admitted": True, "executionEpoch": 0},
            {"pid": 2, "admitted": True, "executionEpoch": "bad"},
            {"pid": 0, "admitted": False, "reason": ""},
            {"pid": 3, "admitted": "self-reported"},
        ],
        "admittedCount": 99,
        "rejectedCount": 99,
    }
    fake_errors = set(evidence_proof.validate_atomic_budget_proof(fake, "twoProcessesCannotOversubscribeGlobalBudget"))
    assert {
        "atomic-budget-scope-mismatch:target!=global",
        "atomic-budget-proof-requires-two-process-results",
        "process-result-must-be-object",
        "invalid-process-pid",
        "admitted-process-missing-execution-epoch",
        "rejected-process-missing-reason",
        "process-result-admitted-must-be-boolean",
        "atomic-budget-race-not-one-admitted-one-rejected",
        "atomic-budget-declared-counts-mismatch",
    } <= fake_errors


def test_semantic_validators_reject_unbound_nested_and_endpoint_claims() -> None:
    not_a_dict: Any = None
    assert evidence_proof._require_fields(not_a_dict, ("field",)) == ["not-a-dict"]  # noqa: SLF001
    assert evidence_proof.validate_resilience_proof(not_a_dict, "resilience") == ["not-a-dict"]
    assert evidence_proof.validate_pass_with_schema_only({}, "schema") == ["empty-evidence"]
    assert evidence_proof.validate_pass_with_schema_only({"schema": "unknown", "source": "journal"}, "schema") == []
    assert evidence_proof.validate_pass_with_schema_only(
        {"schema": evidence_proof.EVIDENCE_PROOF_SCHEMA},
        "schema",
    ) == []

    decision_errors = evidence_proof.validate_decision_proof(
        {
            "riskDigest": "bad",
            "policyVersion": 1,
            "actionAllowed": False,
            "simulationPassed": False,
            "executionVerified": False,
            "effectObserved": False,
        },
        "decision",
    )
    assert "invalid-sha256:riskDigest" in decision_errors
    assert "decision-effectObserved-not-true" in decision_errors

    repair = _actual_copy_evidence()
    repair.update({"endpointA": "http://127.0.0.1:9000", "endpointB": "http://127.0.0.1:9999"})
    assert "destination-endpoint-b-mismatch" in evidence_proof.validate_autonomous_repair_proof(repair, "repair")

    rebalance = _actual_copy_evidence()
    rebalance.update({"endpointA": "http://127.0.0.1:9000", "endpointC": "http://127.0.0.1:9998"})
    assert "destination-endpoint-c-mismatch" in evidence_proof.validate_autonomous_rebalance_proof(
        rebalance,
        "rebalance",
    )
