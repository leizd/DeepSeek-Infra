"""Machine-readable Evidence proof contract (evidence-proof-v2).

Runners must derive claim PASS/FAIL from typed, semantically validated proofs —
not pytest exit alone, and not bare status=PASS without required evidence fields.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

EVIDENCE_PROOF_SCHEMA = "evidence-proof-v2"
EVIDENCE_PROOF_SCHEMA_V3 = "evidence-proof-v3"
EVIDENCE_PROOF_SCHEMA_V1 = "evidence-proof-v1"  # accepted for non-semantic legacy reads
DR_READINESS_PROOF_SCHEMA = "dr-readiness-proof-v1"
ENV_EVIDENCE_PROOF_PATH = "DEEPSEEK_EVIDENCE_PROOF_PATH"

CheckValidator = Callable[[dict[str, Any], str], list[str]]


def write_evidence_proof(
    path: Path | str,
    *,
    scenario: str,
    checks: dict[str, dict[str, Any]],
    meta: dict[str, Any] | None = None,
    schema: str = EVIDENCE_PROOF_SCHEMA,
) -> Path:
    """Write evidence proof JSON. Each check: {status: PASS|FAIL, evidence: {...}}."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": str(schema),
        "scenario": str(scenario),
        "checks": checks,
        "meta": dict(meta or {}),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def load_evidence_proof(path: Path | str, *, expected_scenario: str | None = None) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("evidence-proof-must-be-object")
    schema = str(data.get("schema") or "")
    if schema not in {EVIDENCE_PROOF_SCHEMA, EVIDENCE_PROOF_SCHEMA_V3, EVIDENCE_PROOF_SCHEMA_V1}:
        raise ValueError(f"evidence-proof-schema-mismatch:{schema}")
    if not isinstance(data.get("checks"), dict):
        raise ValueError("evidence-proof-checks-required")
    if expected_scenario is not None and str(data.get("scenario") or "") != expected_scenario:
        raise ValueError(
            f"evidence-proof-scenario-mismatch:expected={expected_scenario}:got={data.get('scenario')}"
        )
    return data


def _require_fields(evidence: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    missing = [name for name in fields if evidence.get(name) in (None, "")]
    return [f"missing-field:{name}" for name in missing]


def _require_sha256(value: Any, *, field: str) -> list[str]:
    text = str(value or "")
    if not re.fullmatch(r"[0-9a-f]{64}", text):
        return [f"invalid-sha256:{field}"]
    return []


def validate_restore_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    errors = _require_fields(
        evidence,
        (
            "backupId",
            "targetId",
            "restoreId",
            "preBackupWorkspaceDigest",
            "corruptedWorkspaceDigest",
            "postRestoreWorkspaceDigest",
        ),
    )
    for field in (
        "preBackupWorkspaceDigest",
        "corruptedWorkspaceDigest",
        "postRestoreWorkspaceDigest",
    ):
        if evidence.get(field) not in (None, ""):
            errors.extend(_require_sha256(evidence.get(field), field=field))
    pre = str(evidence.get("preBackupWorkspaceDigest") or "")
    corrupted = str(evidence.get("corruptedWorkspaceDigest") or "")
    post = str(evidence.get("postRestoreWorkspaceDigest") or "")
    if pre and post and pre != post:
        errors.append("restore-digest-mismatch")
    if pre and corrupted and pre == corrupted:
        errors.append("workspace-was-not-corrupted")
    phase = str(evidence.get("restorePhase") or evidence.get("phase") or "").casefold()
    if phase and phase not in {"complete", "backend-committed", "committed"}:
        errors.append(f"restore-phase-incomplete:{phase}")
    return errors


def validate_backup_commit_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    errors = _require_fields(
        evidence,
        (
            "backupId",
            "commitKey",
            "receiptKey",
            "receiptDigest",
            "objectSetDigest",
        ),
    )
    for field in ("receiptDigest", "objectSetDigest"):
        if evidence.get(field) not in (None, ""):
            errors.extend(_require_sha256(evidence.get(field), field=field))
    # Optional binding verification if raw digests provided by producer.
    computed = evidence.get("computedReceiptSha256")
    declared = evidence.get("receiptDigest")
    if computed and declared and str(computed) != str(declared):
        errors.append("receipt-digest-binding-mismatch")
    return errors


def validate_autonomous_storage_bytes_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    """Recompute Receipt v4 / Commit v4 bindings from the proof's exact bytes."""
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    errors = _require_fields(
        evidence,
        (
            "targetId",
            "endpoint",
            "bucket",
            "backupId",
            "policyId",
            "actionId",
            "receiptKey",
            "commitKey",
            "receiptBytesBase64",
            "commitBytesBase64",
            "rawReceiptSha256",
            "rawCommitSha256",
            "commitReceiptDigest",
            "objectSetDigest",
        ),
    )
    for field in ("rawReceiptSha256", "rawCommitSha256", "commitReceiptDigest", "objectSetDigest"):
        if evidence.get(field) not in (None, ""):
            errors.extend(_require_sha256(evidence.get(field), field=field))

    raw_receipt = b""
    raw_commit = b""
    for field, destination in (("receiptBytesBase64", "receipt"), ("commitBytesBase64", "commit")):
        value = evidence.get(field)
        if value in (None, ""):
            continue
        try:
            decoded = base64.b64decode(str(value), validate=True)
        except (binascii.Error, ValueError):
            errors.append(f"invalid-base64:{field}")
            continue
        if not decoded:
            errors.append(f"empty-bytes:{field}")
        elif destination == "receipt":
            raw_receipt = decoded
        else:
            raw_commit = decoded

    receipt_sha256 = hashlib.sha256(raw_receipt).hexdigest() if raw_receipt else ""
    commit_sha256 = hashlib.sha256(raw_commit).hexdigest() if raw_commit else ""
    if receipt_sha256 and receipt_sha256 != str(evidence.get("rawReceiptSha256") or ""):
        errors.append("raw-receipt-sha256-mismatch")
    if commit_sha256 and commit_sha256 != str(evidence.get("rawCommitSha256") or ""):
        errors.append("raw-commit-sha256-mismatch")
    if receipt_sha256 and receipt_sha256 != str(evidence.get("commitReceiptDigest") or ""):
        errors.append("receipt-digest-binding-mismatch")

    receipt: dict[str, Any] = {}
    commit: dict[str, Any] = {}
    for raw, destination in ((raw_receipt, "receipt"), (raw_commit, "commit")):
        if not raw:
            continue
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append(f"invalid-{destination}-json")
            continue
        if not isinstance(parsed, dict):
            errors.append(f"{destination}-must-be-object")
        elif destination == "receipt":
            receipt = parsed
        else:
            commit = parsed

    if receipt and int(receipt.get("schemaVersion") or 0) != 4:
        errors.append("receipt-schema-not-v4")
    if commit and int(commit.get("schemaVersion") or 0) != 4:
        errors.append("commit-schema-not-v4")

    backup_id = str(evidence.get("backupId") or "")
    policy_id = str(evidence.get("policyId") or "")
    object_set_digest = str(evidence.get("objectSetDigest") or "")
    if receipt and str(receipt.get("backupId") or "") != backup_id:
        errors.append("receipt-backup-id-mismatch")
    if commit and str(commit.get("backupId") or "") != backup_id:
        errors.append("commit-backup-id-mismatch")
    if commit and str(commit.get("policyId") or "") != policy_id:
        errors.append("commit-policy-id-mismatch")
    if commit and str(commit.get("receiptDigest") or "") != str(evidence.get("commitReceiptDigest") or ""):
        errors.append("commit-receipt-digest-mismatch")
    if receipt and str(receipt.get("objectSetDigest") or "") != object_set_digest:
        errors.append("receipt-object-set-digest-mismatch")
    if commit and str(commit.get("objectSetDigest") or "") != object_set_digest:
        errors.append("commit-object-set-digest-mismatch")

    if backup_id and str(evidence.get("receiptKey") or "") != f"receipts/{backup_id}.json":
        errors.append("receipt-key-mismatch")
    if backup_id and policy_id and str(evidence.get("commitKey") or "") != f"commits/{policy_id}/{backup_id}.json":
        errors.append("commit-key-mismatch")
    return errors


def validate_distinct_pid_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    errors = _require_fields(evidence, ("pidA", "pidB"))
    try:
        pid_a = int(str(evidence.get("pidA")))
        pid_b = int(str(evidence.get("pidB")))
    except (TypeError, ValueError):
        return errors + ["invalid-pid-types"]
    if pid_a <= 0 or pid_b <= 0:
        errors.append("non-positive-pid")
    if pid_a == pid_b:
        errors.append("pids-not-distinct")
    return errors


def validate_sigkill_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    errors = _require_fields(evidence, ("returncode",))
    try:
        code = int(str(evidence.get("returncode")))
    except (TypeError, ValueError):
        return errors + ["invalid-returncode"]
    # POSIX signal kill → negative; Windows terminate often != 0
    if code == 0:
        errors.append("process-a-exited-cleanly-not-killed")
    return errors


def validate_epoch_increase_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    errors = _require_fields(evidence, ("epochA", "epochB"))
    try:
        if int(str(evidence.get("epochB"))) <= int(str(evidence.get("epochA"))):
            errors.append("boot-epoch-not-increased")
    except (TypeError, ValueError):
        errors.append("invalid-epoch-types")
    return errors


def validate_minio_endpoints_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    endpoints = evidence.get("endpoints")
    if not isinstance(endpoints, list) or len(endpoints) < 3:
        return ["need-three-endpoints"]
    unique = {str(item).rstrip("/") for item in endpoints}
    if len(unique) < 3:
        return ["endpoints-not-distinct"]
    return []


def validate_pass_with_schema_only(evidence: dict[str, Any], check_name: str) -> list[str]:
    if str(evidence.get("schema") or "") not in {EVIDENCE_PROOF_SCHEMA, EVIDENCE_PROOF_SCHEMA_V3, EVIDENCE_PROOF_SCHEMA_V1, "ok"}:
        if not evidence:
            return ["empty-evidence"]
    return []


def validate_dr_readiness_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    errors = _require_fields(
        evidence,
        (
            "schema",
            "drillId",
            "backupId",
            "testedBackupId",
            "restoreDurationMs",
            "workspaceDigestBefore",
            "workspaceDigestAfter",
            "objectCount",
            "commitVerified",
            "receiptVerified",
            "ageVerified",
            "cleanupCompleted",
        ),
    )
    schema = str(evidence.get("schema") or "")
    if schema and schema != DR_READINESS_PROOF_SCHEMA:
        errors.append(f"invalid-dr-readiness-proof-schema:{schema}")
    backup_id = str(evidence.get("backupId") or "")
    tested_backup_id = str(evidence.get("testedBackupId") or "")
    if backup_id and tested_backup_id and backup_id != tested_backup_id:
        errors.append("drill-backupId-mismatch")
    for field in ("workspaceDigestBefore", "workspaceDigestAfter"):
        val = evidence.get(field)
        if val not in (None, ""):
            errors.extend(_require_sha256(val, field=field))
    pre = str(evidence.get("workspaceDigestBefore") or "")
    post = str(evidence.get("workspaceDigestAfter") or "")
    if pre and post and pre != post:
        errors.append("drill-workspace-digest-mismatch")
    for bool_field in ("commitVerified", "receiptVerified", "ageVerified", "cleanupCompleted"):
        if evidence.get(bool_field) is not True:
            errors.append(f"{bool_field}-not-true")
    dur = evidence.get("restoreDurationMs")
    if dur is not None and (not isinstance(dur, (int, float)) or dur < 0):
        errors.append("invalid-restore-duration")
    count = evidence.get("objectCount")
    if count is not None and (not isinstance(count, int) or isinstance(count, bool) or count < 0):
        errors.append("invalid-object-count")
    return errors


def validate_retention_safety_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    safety = evidence.get("retentionSafety")
    if not isinstance(safety, dict):
        safety = evidence
    errors = _require_fields(
        safety,
        (
            "checkpointVerified",
            "ancestorCoverage",
            "replicaAgreement",
            "dependencyClosure",
        ),
    )
    for bool_field in ("checkpointVerified", "ancestorCoverage", "replicaAgreement", "dependencyClosure"):
        if safety.get(bool_field) is not True:
            errors.append(f"retention-safety-{bool_field}-not-true")
    return errors


def validate_decision_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    decision = evidence.get("decisionProof")
    if not isinstance(decision, dict):
        decision = evidence
    errors = _require_fields(
        decision,
        (
            "riskDigest",
            "policyVersion",
            "actionAllowed",
            "simulationPassed",
            "executionVerified",
        ),
    )
    if decision.get("riskDigest") not in (None, ""):
        errors.extend(_require_sha256(decision.get("riskDigest"), field="riskDigest"))
    for d_field in ("riskBeforeDigest", "riskAfterDigest"):
        if decision.get(d_field) not in (None, ""):
            errors.extend(_require_sha256(decision.get(d_field), field=d_field))
    for bool_field in ("actionAllowed", "simulationPassed", "executionVerified"):
        if decision.get(bool_field) is not True:
            errors.append(f"decision-{bool_field}-not-true")
    if "effectObserved" in decision and decision.get("effectObserved") is not True:
        errors.append("decision-effectObserved-not-true")
    return errors


def validate_resilience_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    errors = _require_fields(
        evidence,
        (
            "riskDigest",
            "score",
            "overallRisk",
        ),
    )
    if evidence.get("riskDigest") not in (None, ""):
        errors.extend(_require_sha256(evidence.get("riskDigest"), field="riskDigest"))
    score = evidence.get("score")
    if score is not None and (not isinstance(score, (int, float)) or isinstance(score, bool) or score < 0 or score > 100):
        errors.append("invalid-resilience-score")
    return errors


def validate_autonomous_repair_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    errors = validate_autonomous_storage_bytes_proof(evidence, check_name)
    errors.extend(_require_fields(evidence, ("endpointA", "endpointB")))
    if evidence.get("endpoint") and evidence.get("endpointB") and str(evidence["endpoint"]).rstrip("/") != str(evidence["endpointB"]).rstrip("/"):
        errors.append("destination-endpoint-b-mismatch")
    return errors


def validate_autonomous_rebalance_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    errors = validate_autonomous_storage_bytes_proof(evidence, check_name)
    errors.extend(_require_fields(evidence, ("endpointA", "endpointC")))
    if evidence.get("endpoint") and evidence.get("endpointC") and str(evidence["endpoint"]).rstrip("/") != str(evidence["endpointC"]).rstrip("/"):
        errors.append("destination-endpoint-c-mismatch")
    return errors


def validate_crash_recovery_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    return _require_fields(
        evidence,
        (
            "actionId",
            "oldEpoch",
            "newEpoch",
            "reconciliationDirective",
        ),
    )


def validate_blast_radius_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    errors = _require_fields(
        evidence,
        (
            "minCommittedCopies",
            "copiesDuring",
        ),
    )
    if evidence.get("blastRadiusVerified") is not True:
        errors.append("blast-radius-not-verified")
    try:
        min_c = int(str(evidence.get("minCommittedCopies") or 0))
        during_c = int(str(evidence.get("copiesDuring") or 0))
        if during_c < min_c:
            errors.append("copies-during-less-than-minimum")
    except (ValueError, TypeError):
        errors.append("invalid-copies-counts")
    return errors


def validate_atomic_budget_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    errors = _require_fields(
        evidence,
        (
            "actionId",
            "executionEpoch",
        ),
    )
    if evidence.get("atomicAdmissionVerified") is not True:
        errors.append("atomic-admission-not-verified")
    return errors


VALIDATORS: dict[str, CheckValidator] = {
    "realPreDisasterBackupIsActuallyRestored": validate_restore_proof,
    "realFreshProcessRestoresPreDisasterBackup": validate_restore_proof,
    "restoredWorkspaceDigestMatchesPreDisasterDigest": validate_restore_proof,
    "realPostRecoveryBackupHasValidCommit": validate_backup_commit_proof,
    "realFreshProcessCreatesPostRecoveryBackup": validate_backup_commit_proof,
    "realPostRecoveryBackupHasValidReceiptBinding": validate_backup_commit_proof,
    "freshProcessAAndBHaveDifferentPids": validate_distinct_pid_proof,
    "processAIsDeadBeforeProcessBStarts": validate_sigkill_proof,
    "processAExitedBySigkill": validate_sigkill_proof,
    "realFreshProcessBootEpochStrictlyIncreases": validate_epoch_increase_proof,
    "realThreeMinioProcessReplacementE2E": validate_minio_endpoints_proof,
    "realThreeMinioFreshProcessAuthorityRecoveryE2E": validate_minio_endpoints_proof,
    "evidenceCheckCannotPassWithoutStructuredProof": validate_pass_with_schema_only,
    "continuousDrillProducesReadinessProof": validate_dr_readiness_proof,
    "drReadinessProofValid": validate_dr_readiness_proof,
    "realDrReadinessProof": validate_dr_readiness_proof,
    "retentionSafetyProof": validate_retention_safety_proof,
    "authorityRetentionSafety": validate_retention_safety_proof,
    "decisionProof": validate_decision_proof,
    "resilienceDecisionProof": validate_decision_proof,
    "resilienceScoreProof": validate_resilience_proof,
    "resilienceSnapshotProof": validate_resilience_proof,
    "realThreeMinioAutonomousRepairE2E": validate_minio_endpoints_proof,
    "realThreeMinioAutonomousRebalanceE2E": validate_minio_endpoints_proof,
    "realThreeMinioAutonomousDrillE2E": validate_dr_readiness_proof,
    "realReplicaTransferUsesEndpointAAndB": validate_autonomous_repair_proof,
    "realRebalanceUsesEndpointAAndC": validate_autonomous_rebalance_proof,
    "destinationReceiptAuthenticated": validate_autonomous_storage_bytes_proof,
    "destinationCommitAuthenticated": validate_autonomous_storage_bytes_proof,
    "crashRecoveryObservedExistingEffect": validate_crash_recovery_proof,
    "leaseTakeoverUsedNewExecutionEpoch": validate_epoch_increase_proof,
    "blastRadiusInvariantVerified": validate_blast_radius_proof,
    "atomicBudgetAdmissionVerified": validate_atomic_budget_proof,
}


def validate_check(check_name: str, item: dict[str, Any]) -> list[str]:
    """Return semantic errors for one check item; empty list means PASS-eligible."""
    status = str(item.get("status") or "").upper()
    if status != "PASS":
        return [f"status-not-pass:{status or 'missing'}"]
    evidence = item.get("evidence")
    if not isinstance(evidence, dict):
        return ["evidence-must-be-object"]
    validator = VALIDATORS.get(check_name)
    if validator is None:
        # Unknown checks: require non-empty evidence object (no self-assert without payload).
        if not evidence:
            return ["empty-evidence-for-unknown-check"]
        return []
    return validator(evidence, check_name)


def proof_check_status(proof: dict[str, Any], check_name: str, *, semantic: bool = True) -> str:
    raw_checks = proof.get("checks")
    checks: dict[str, Any] = raw_checks if isinstance(raw_checks, dict) else {}
    item = checks.get(check_name)
    if not isinstance(item, dict):
        return "FAIL"
    if not semantic:
        status = str(item.get("status") or "").upper()
        return "PASS" if status == "PASS" else "FAIL"
    errors = validate_check(check_name, item)
    return "PASS" if not errors else "FAIL"


def resolve_proof_path(*, env: dict[str, str] | None = None, scenario: str | None = None) -> Path | None:
    environ = env if env is not None else dict(os.environ)
    raw = environ.get(ENV_EVIDENCE_PROOF_PATH)
    if raw:
        return Path(raw)
    if scenario:
        candidate = Path("artifacts") / f"evidence-proof-{scenario}.json"
        if candidate.is_file():
            return candidate
    return None


def merge_checks_from_proof(
    *,
    checks: dict[str, str],
    check_to_scenario: dict[str, str],
    scenario_results: dict[str, dict[str, Any]],
    required_proof_checks: dict[str, tuple[str, ...]],
) -> dict[str, str]:
    """Upgrade/downgrade checks using typed proof validators."""
    out = dict(checks)
    for scenario, required in required_proof_checks.items():
        result = scenario_results.get(scenario) or {}
        exit_code = result.get("exitCode")
        if exit_code is None or int(exit_code) != 0:
            for check in required:
                out[check] = "FAIL"
            continue
        proof_path_raw = result.get("proofPath")
        path: Path | None = Path(str(proof_path_raw)) if proof_path_raw else None
        if path is None or not path.is_file():
            candidate = Path("artifacts") / f"evidence-proof-{scenario}.json"
            path = candidate if candidate.is_file() else None
        if path is None or not path.is_file():
            for check in required:
                out[check] = "FAIL"
            continue
        try:
            proof = load_evidence_proof(path, expected_scenario=scenario)
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            for check in required:
                out[check] = "FAIL"
            continue
        for check in required:
            out[check] = proof_check_status(proof, check, semantic=True)
    return out
