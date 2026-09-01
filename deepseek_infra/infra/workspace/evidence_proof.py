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
from datetime import datetime, timezone
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
            "providerReceiptObject",
            "providerCommitObject",
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

    if receipt:
        receipt_schema = receipt.get("schemaVersion")
        if not isinstance(receipt_schema, int) or isinstance(receipt_schema, bool) or receipt_schema != 4:
            errors.append("receipt-schema-not-v4")
    if commit:
        commit_schema = commit.get("schemaVersion")
        if not isinstance(commit_schema, int) or isinstance(commit_schema, bool) or commit_schema != 4:
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
    for field, expected_key, raw, digest in (
        ("providerReceiptObject", evidence.get("receiptKey"), raw_receipt, receipt_sha256),
        ("providerCommitObject", evidence.get("commitKey"), raw_commit, commit_sha256),
    ):
        raw_object = evidence.get(field)
        if not isinstance(raw_object, dict):
            errors.append(f"{field}-must-be-object")
            continue
        if raw_object.get("key") != expected_key:
            errors.append(f"{field}-key-mismatch")
        if raw_object.get("size") != len(raw):
            errors.append(f"{field}-size-mismatch")
        provider_sha256 = raw_object.get("sha256")
        if provider_sha256 not in (None, "") and provider_sha256 != digest:
            errors.append(f"{field}-sha256-mismatch")
        if not str(raw_object.get("etag") or ""):
            errors.append(f"{field}-etag-missing")
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
    errors = _require_fields(
        evidence,
        (
            "actionId",
            "workerAPid",
            "workerBPid",
            "processAReturnCode",
            "epochA",
            "epochB",
            "repairId",
            "repairPhaseAtCrash",
            "reconciliationDirective",
            "workerALeaseUntil",
            "remoteRepairJobCountBefore",
            "remoteRepairJobCountAfter",
            "remoteRepairJobIdsBefore",
            "remoteRepairJobIdsAfter",
            "journalEvents",
        ),
    )
    try:
        pid_a = int(str(evidence.get("workerAPid")))
        pid_b = int(str(evidence.get("workerBPid")))
        epoch_a = int(str(evidence.get("epochA")))
        epoch_b = int(str(evidence.get("epochB")))
        return_code = int(str(evidence.get("processAReturnCode")))
        before_count = int(str(evidence.get("remoteRepairJobCountBefore")))
        after_count = int(str(evidence.get("remoteRepairJobCountAfter")))
    except (TypeError, ValueError):
        return errors + ["invalid-crash-takeover-numeric-fields"]
    if pid_a <= 0 or pid_b <= 0 or pid_a == pid_b:
        errors.append("worker-pids-not-distinct-positive")
    if return_code == 0:
        errors.append("worker-a-not-hard-terminated")
    if epoch_b <= epoch_a:
        errors.append("takeover-execution-epoch-not-increased")
    if before_count != 1 or after_count != 1:
        errors.append("underlying-repair-job-count-not-exactly-one")
    before_ids = evidence.get("remoteRepairJobIdsBefore")
    after_ids = evidence.get("remoteRepairJobIdsAfter")
    repair_id = str(evidence.get("repairId") or "")
    if (
        not isinstance(before_ids, list)
        or not isinstance(after_ids, list)
        or len(before_ids) != 1
        or len(after_ids) != 1
        or [str(item) for item in before_ids] != [repair_id]
        or [str(item) for item in after_ids] != [repair_id]
        or before_count != len(before_ids)
        or after_count != len(after_ids)
    ):
        errors.append("underlying-repair-job-identity-not-stable")

    lease_expiry: datetime | None = None
    try:
        lease_expiry = datetime.fromisoformat(str(evidence.get("workerALeaseUntil") or "").replace("Z", "+00:00"))
        if lease_expiry.tzinfo is None:
            raise ValueError("timezone required")
        lease_expiry = lease_expiry.astimezone(timezone.utc)
    except ValueError:
        errors.append("invalid-worker-a-lease-expiry")
    if str(evidence.get("repairPhaseAtCrash") or "") not in {
        "selecting-source",
        "acquiring-source-hold",
        "validating-source-control",
        "scanning-destination",
        "transferring-components",
        "verifying-components",
        "finalizing",
    }:
        errors.append("worker-a-not-killed-during-active-repair")
    if str(evidence.get("reconciliationDirective") or "") not in {"RESUME_EXECUTION", "ADVANCE_TO_VERIFYING"}:
        errors.append("invalid-reconciliation-directive")

    raw_events = evidence.get("journalEvents")
    if not isinstance(raw_events, list):
        return errors + ["journal-events-must-be-list"]
    executing_index: int | None = None
    reconciling_index: int | None = None
    takeover_at: datetime | None = None
    for index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, dict):
            continue
        handle = raw_event.get("effectHandle")
        effect = handle if isinstance(handle, dict) else {}
        owner = str(raw_event.get("ownerInstanceId") or "")
        try:
            event_epoch = int(str(raw_event.get("executionEpoch")))
        except (TypeError, ValueError):
            continue
        if (
            str(raw_event.get("state") or "") == "EXECUTING"
            and event_epoch == epoch_a
            and str(effect.get("kind") or "") == "repair"
            and str(effect.get("repairId") or "") == repair_id
            and str(pid_a) in owner
        ):
            executing_index = index
        if (
            str(raw_event.get("state") or "") == "RECONCILING"
            and str(raw_event.get("eventType") or "") == "ACTION_TAKEOVER"
            and event_epoch == epoch_b
            and str(effect.get("kind") or "") == "repair"
            and str(effect.get("repairId") or "") == repair_id
            and str(pid_b) in owner
        ):
            reconciling_index = index
            try:
                parsed_takeover_at = datetime.fromisoformat(
                    str(raw_event.get("createdAt") or "").replace("Z", "+00:00")
                )
                if parsed_takeover_at.tzinfo is not None:
                    takeover_at = parsed_takeover_at.astimezone(timezone.utc)
            except ValueError:
                takeover_at = None
    if executing_index is None:
        errors.append("missing-worker-a-executing-effect-event")
    if reconciling_index is None:
        errors.append("missing-worker-b-reconciling-event")
    elif takeover_at is None:
        errors.append("missing-worker-b-takeover-timestamp")
    if executing_index is not None and reconciling_index is not None and reconciling_index <= executing_index:
        errors.append("reconciling-event-not-after-executing-event")
    if lease_expiry is not None and takeover_at is not None and takeover_at <= lease_expiry:
        errors.append("takeover-occurred-before-worker-a-lease-expiry")
    return errors


def validate_wave_crash_recovery_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    """Validate real Wave Runner heartbeat and multi-epoch crash takeover evidence."""

    errors = validate_crash_recovery_proof(evidence, check_name)
    errors.extend(
        _require_fields(
            evidence,
            (
                "scheduleId",
                "waveIndex",
                "scheduleEpochA",
                "scheduleEpochB",
                "waveEpochA",
                "waveEpochB",
                "waveActionEpochA",
                "waveActionEpochB",
                "workerAScheduleLeaseUntil",
                "workerAWaveLeaseUntil",
                "workerAWaveActionLeaseUntil",
                "firstRunnerLeaseUntil",
                "renewedRunnerLeaseUntil",
                "runnerLeaseObservations",
                "journalStateAtCrash",
                "runnerStateAtCrash",
                "runnerStateAtTakeoverClaim",
                "runnerStateAfterTakeover",
                "settlementEvents",
            ),
        )
    )
    try:
        schedule_epoch_a = int(str(evidence.get("scheduleEpochA")))
        schedule_epoch_b = int(str(evidence.get("scheduleEpochB")))
        wave_epoch_a = int(str(evidence.get("waveEpochA")))
        wave_epoch_b = int(str(evidence.get("waveEpochB")))
        wave_action_epoch_a = int(str(evidence.get("waveActionEpochA")))
        wave_action_epoch_b = int(str(evidence.get("waveActionEpochB")))
        journal_epoch_a = int(str(evidence.get("epochA")))
        journal_epoch_b = int(str(evidence.get("epochB")))
        wave_index = int(str(evidence.get("waveIndex")))
        worker_a_pid = int(str(evidence.get("workerAPid")))
        worker_b_pid = int(str(evidence.get("workerBPid")))
    except (TypeError, ValueError):
        return errors + ["invalid-wave-takeover-numeric-fields"]
    if wave_index < 0:
        errors.append("negative-wave-index")
    if schedule_epoch_b <= schedule_epoch_a:
        errors.append("schedule-execution-epoch-not-increased")
    if wave_epoch_b <= wave_epoch_a:
        errors.append("wave-execution-epoch-not-increased")
    if wave_action_epoch_b <= wave_action_epoch_a:
        errors.append("wave-action-execution-epoch-not-increased")

    raw_crash_state = evidence.get("runnerStateAtCrash")
    raw_claim_state = evidence.get("runnerStateAtTakeoverClaim")
    raw_takeover_state = evidence.get("runnerStateAfterTakeover")
    crash_state = raw_crash_state if isinstance(raw_crash_state, dict) else {}
    claim_state = raw_claim_state if isinstance(raw_claim_state, dict) else {}
    takeover_state = raw_takeover_state if isinstance(raw_takeover_state, dict) else {}
    if not isinstance(raw_crash_state, dict):
        errors.append("runner-state-at-crash-must-be-object")
    if not isinstance(raw_claim_state, dict):
        errors.append("runner-state-at-takeover-claim-must-be-object")
    if not isinstance(raw_takeover_state, dict):
        errors.append("runner-state-after-takeover-must-be-object")

    def runner_record(snapshot: dict[str, Any], phase: str, record_name: str) -> dict[str, Any]:
        raw_record = snapshot.get(record_name)
        if not isinstance(raw_record, dict):
            errors.append(f"runner-state-{phase}-{record_name}-must-be-object")
            return {}
        return raw_record

    crash_schedule = runner_record(crash_state, "crash", "schedule")
    crash_wave = runner_record(crash_state, "crash", "wave")
    crash_action = runner_record(crash_state, "crash", "waveAction")
    claim_schedule = runner_record(claim_state, "takeover-claim", "schedule")
    claim_wave = runner_record(claim_state, "takeover-claim", "wave")
    claim_action = runner_record(claim_state, "takeover-claim", "waveAction")
    takeover_schedule = runner_record(takeover_state, "takeover", "schedule")
    takeover_wave = runner_record(takeover_state, "takeover", "wave")
    takeover_action = runner_record(takeover_state, "takeover", "waveAction")
    schedule_id = str(evidence.get("scheduleId") or "")
    action_id = str(evidence.get("actionId") or "")
    for record in (
        crash_schedule,
        crash_wave,
        crash_action,
        claim_schedule,
        claim_wave,
        claim_action,
        takeover_schedule,
        takeover_wave,
        takeover_action,
    ):
        if str(record.get("scheduleId") or "") != schedule_id:
            errors.append("runner-state-schedule-id-binding-mismatch")
    for record in (crash_action, claim_action, takeover_action):
        if str(record.get("actionId") or "") != action_id:
            errors.append("runner-state-action-id-binding-mismatch")

    for record in (crash_wave, crash_action, claim_wave, claim_action, takeover_wave, takeover_action):
        try:
            record_wave_index = int(str(record.get("waveIndex")))
        except (TypeError, ValueError):
            errors.append("runner-state-invalid-wave-index")
            continue
        if record_wave_index != wave_index:
            errors.append("runner-state-wave-index-binding-mismatch")

    def runner_epoch(record: dict[str, Any], phase: str, field: str) -> int | None:
        try:
            return int(str(record.get(field)))
        except (TypeError, ValueError):
            errors.append(f"runner-state-{phase}-invalid-{field}")
            return None

    crash_schedule_epoch = runner_epoch(crash_schedule, "crash-schedule", "scheduleExecutionEpoch")
    crash_wave_epoch = runner_epoch(crash_wave, "crash-wave", "waveExecutionEpoch")
    crash_action_epoch = runner_epoch(crash_action, "crash-action", "actionExecutionEpoch")
    crash_action_schedule_epoch = runner_epoch(crash_action, "crash-action", "scheduleExecutionEpoch")
    crash_action_wave_epoch = runner_epoch(crash_action, "crash-action", "waveExecutionEpoch")
    claim_schedule_epoch = runner_epoch(claim_schedule, "takeover-claim-schedule", "scheduleExecutionEpoch")
    claim_wave_epoch = runner_epoch(claim_wave, "takeover-claim-wave", "waveExecutionEpoch")
    claim_action_epoch = runner_epoch(claim_action, "takeover-claim-action", "actionExecutionEpoch")
    claim_action_schedule_epoch = runner_epoch(claim_action, "takeover-claim-action", "scheduleExecutionEpoch")
    claim_action_wave_epoch = runner_epoch(claim_action, "takeover-claim-action", "waveExecutionEpoch")
    takeover_schedule_epoch = runner_epoch(takeover_schedule, "takeover-schedule", "scheduleExecutionEpoch")
    takeover_wave_epoch = runner_epoch(takeover_wave, "takeover-wave", "waveExecutionEpoch")
    takeover_action_epoch = runner_epoch(takeover_action, "takeover-action", "actionExecutionEpoch")
    takeover_action_schedule_epoch = runner_epoch(takeover_action, "takeover-action", "scheduleExecutionEpoch")
    takeover_action_wave_epoch = runner_epoch(takeover_action, "takeover-action", "waveExecutionEpoch")
    takeover_journal_epoch = runner_epoch(takeover_action, "takeover-action", "journalExecutionEpoch")
    if crash_schedule_epoch != schedule_epoch_a:
        errors.append("runner-state-schedule-epoch-binding-mismatch")
    if crash_wave_epoch != wave_epoch_a:
        errors.append("runner-state-wave-epoch-binding-mismatch")
    if (
        crash_action_epoch != wave_action_epoch_a
        or crash_action_schedule_epoch != schedule_epoch_a
        or crash_action_wave_epoch != wave_epoch_a
    ):
        errors.append("runner-state-wave-action-epoch-binding-mismatch")
    if claim_schedule_epoch != schedule_epoch_b:
        errors.append("runner-state-takeover-claim-schedule-epoch-binding-mismatch")
    if claim_wave_epoch != wave_epoch_b:
        errors.append("runner-state-takeover-claim-wave-epoch-binding-mismatch")
    if (
        claim_action_epoch != wave_action_epoch_b
        or claim_action_schedule_epoch != schedule_epoch_b
        or claim_action_wave_epoch != wave_epoch_b
    ):
        errors.append("runner-state-takeover-claim-action-epoch-binding-mismatch")
    if takeover_schedule_epoch != schedule_epoch_b:
        errors.append("runner-state-takeover-schedule-epoch-binding-mismatch")
    if takeover_wave_epoch != wave_epoch_b:
        errors.append("runner-state-takeover-wave-epoch-binding-mismatch")
    if (
        takeover_action_epoch != wave_action_epoch_b
        or takeover_action_schedule_epoch != schedule_epoch_b
        or takeover_action_wave_epoch != wave_epoch_b
        or takeover_journal_epoch != journal_epoch_b
    ):
        errors.append("runner-state-takeover-action-epoch-binding-mismatch")
    if str(crash_schedule.get("leaseUntil") or "") != str(evidence.get("workerAScheduleLeaseUntil") or ""):
        errors.append("runner-state-schedule-lease-binding-mismatch")
    if str(crash_wave.get("leaseUntil") or "") != str(evidence.get("workerAWaveLeaseUntil") or ""):
        errors.append("runner-state-wave-lease-binding-mismatch")
    if str(crash_action.get("leaseUntil") or "") != str(evidence.get("workerAWaveActionLeaseUntil") or ""):
        errors.append("runner-state-wave-action-lease-binding-mismatch")
    expected_worker_a_owner = f"crash-worker-a-{worker_a_pid}"
    if any(str(record.get("ownerInstanceId") or "") != expected_worker_a_owner for record in (crash_schedule, crash_wave, crash_action)):
        errors.append("runner-state-worker-a-owner-binding-mismatch")
    expected_worker_b_owner = f"takeover-worker-b-{worker_b_pid}"
    if any(str(record.get("ownerInstanceId") or "") != expected_worker_b_owner for record in (claim_schedule, claim_wave, claim_action)):
        errors.append("runner-state-worker-b-owner-binding-mismatch")
    if str(crash_schedule.get("status") or "") != "RUNNING" or str(crash_wave.get("status") or "") != "EXECUTING":
        errors.append("runner-state-crash-not-active")
    if str(crash_action.get("status") or "") != "EXECUTING":
        errors.append("runner-state-crash-action-not-executing")
    if str(claim_schedule.get("status") or "") != "RUNNING" or str(claim_wave.get("status") or "") != "EXECUTING":
        errors.append("runner-state-takeover-claim-not-active")
    if str(claim_action.get("status") or "") != "CLAIMED":
        errors.append("runner-state-takeover-claim-action-not-claimed")
    if str(takeover_schedule.get("status") or "") != "COMPLETED" or str(takeover_wave.get("status") or "") != "COMPLETED":
        errors.append("runner-state-takeover-not-completed")
    if str(takeover_action.get("status") or "") != "VERIFIED_SUCCESS":
        errors.append("runner-state-takeover-action-not-verified")
    raw_takeover_handle = takeover_action.get("effectHandle")
    takeover_handle = raw_takeover_handle if isinstance(raw_takeover_handle, dict) else {}
    if str(takeover_handle.get("kind") or "") != "repair" or str(takeover_handle.get("repairId") or "") != str(evidence.get("repairId") or ""):
        errors.append("runner-state-takeover-effect-binding-mismatch")

    parsed_leases: dict[str, datetime] = {}
    for field in (
        "workerALeaseUntil",
        "workerAScheduleLeaseUntil",
        "workerAWaveLeaseUntil",
        "workerAWaveActionLeaseUntil",
        "firstRunnerLeaseUntil",
        "renewedRunnerLeaseUntil",
    ):
        try:
            parsed = datetime.fromisoformat(str(evidence.get(field) or "").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
            parsed_leases[field] = parsed.astimezone(timezone.utc)
        except ValueError:
            errors.append(f"invalid-{field}")
    first_lease = parsed_leases.get("firstRunnerLeaseUntil")
    renewed_lease = parsed_leases.get("renewedRunnerLeaseUntil")
    schedule_lease = parsed_leases.get("workerAScheduleLeaseUntil")
    wave_lease = parsed_leases.get("workerAWaveLeaseUntil")
    if first_lease is not None and renewed_lease is not None and renewed_lease <= first_lease:
        errors.append("runner-lease-not-renewed")
    if schedule_lease is not None and wave_lease is not None and schedule_lease != wave_lease:
        errors.append("schedule-wave-lease-diverged")
    if schedule_lease is not None and renewed_lease is not None and schedule_lease != renewed_lease:
        errors.append("crashed-runner-lease-not-last-observed-renewal")

    def parse_record_timestamp(record: dict[str, Any], field: str, error: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(record.get(field) or "").replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone required")
            return parsed.astimezone(timezone.utc)
        except ValueError:
            errors.append(error)
            return None

    raw_observations = evidence.get("runnerLeaseObservations")
    observation_leases: list[datetime] = []
    observation_lease_values: list[str] = []
    observation_update_times: list[datetime] = []
    if not isinstance(raw_observations, list):
        errors.append("runner-lease-observations-must-be-list")
    else:
        for index, raw_observation in enumerate(raw_observations):
            if not isinstance(raw_observation, dict):
                errors.append("runner-lease-observation-not-object")
                continue
            raw_schedule = raw_observation.get("schedule")
            raw_wave = raw_observation.get("wave")
            if not isinstance(raw_schedule, dict) or not isinstance(raw_wave, dict):
                errors.append("runner-lease-observation-records-must-be-objects")
                continue
            if str(raw_schedule.get("scheduleId") or "") != schedule_id or str(raw_wave.get("scheduleId") or "") != schedule_id:
                errors.append("runner-lease-observation-schedule-id-binding-mismatch")
            try:
                observation_wave_index = int(str(raw_wave.get("waveIndex")))
                observation_schedule_epoch = int(str(raw_schedule.get("scheduleExecutionEpoch")))
                observation_wave_epoch = int(str(raw_wave.get("waveExecutionEpoch")))
            except (TypeError, ValueError):
                errors.append("runner-lease-observation-invalid-numeric-fields")
                continue
            if observation_wave_index != wave_index:
                errors.append("runner-lease-observation-wave-index-binding-mismatch")
            if observation_schedule_epoch != schedule_epoch_a or observation_wave_epoch != wave_epoch_a:
                errors.append("runner-lease-observation-epoch-binding-mismatch")
            schedule_owner = str(raw_schedule.get("ownerInstanceId") or "")
            wave_owner = str(raw_wave.get("ownerInstanceId") or "")
            if schedule_owner != expected_worker_a_owner or wave_owner != expected_worker_a_owner:
                errors.append("runner-lease-observation-worker-a-owner-binding-mismatch")
            if str(raw_schedule.get("status") or "") != "RUNNING" or str(raw_wave.get("status") or "") not in {
                "CLAIMING",
                "RUNNING",
                "EXECUTING",
                "VERIFYING",
            }:
                errors.append("runner-lease-observation-not-active")
            schedule_observed_lease = parse_record_timestamp(
                raw_schedule,
                "leaseUntil",
                f"runner-lease-observation-{index}-invalid-schedule-lease",
            )
            wave_observed_lease = parse_record_timestamp(
                raw_wave,
                "leaseUntil",
                f"runner-lease-observation-{index}-invalid-wave-lease",
            )
            schedule_updated_at = parse_record_timestamp(
                raw_schedule,
                "updatedAt",
                f"runner-lease-observation-{index}-invalid-schedule-updated-at",
            )
            wave_updated_at = parse_record_timestamp(
                raw_wave,
                "updatedAt",
                f"runner-lease-observation-{index}-invalid-wave-updated-at",
            )
            if schedule_observed_lease is not None and wave_observed_lease is not None:
                if schedule_observed_lease != wave_observed_lease:
                    errors.append("runner-lease-observation-schedule-wave-lease-diverged")
                else:
                    observation_leases.append(schedule_observed_lease)
                    observation_lease_values.append(str(raw_schedule.get("leaseUntil") or ""))
            if schedule_updated_at is not None and wave_updated_at is not None:
                observation_update_times.append(max(schedule_updated_at, wave_updated_at))
                if schedule_observed_lease is not None and schedule_observed_lease <= schedule_updated_at:
                    errors.append("runner-lease-observation-schedule-lease-not-active")
                if wave_observed_lease is not None and wave_observed_lease <= wave_updated_at:
                    errors.append("runner-lease-observation-wave-lease-not-active")
        if len(raw_observations) < 2 or len(observation_leases) < 2 or len(observation_update_times) < 2:
            errors.append("runner-lease-observations-insufficient")
        if any(current <= previous for previous, current in zip(observation_leases, observation_leases[1:])):
            errors.append("runner-lease-observations-not-strictly-increasing")
        if any(current <= previous for previous, current in zip(observation_update_times, observation_update_times[1:])):
            errors.append("runner-lease-observation-updates-not-strictly-increasing")

    if observation_lease_values:
        if str(evidence.get("firstRunnerLeaseUntil") or "") != observation_lease_values[0]:
            errors.append("first-runner-lease-not-bound-to-durable-observation")
        if str(evidence.get("renewedRunnerLeaseUntil") or "") != observation_lease_values[-1]:
            errors.append("renewed-runner-lease-not-bound-to-durable-observation")
        if str(crash_schedule.get("leaseUntil") or "") != observation_lease_values[-1]:
            errors.append("crash-schedule-lease-not-bound-to-last-observation")
        if str(crash_wave.get("leaseUntil") or "") != observation_lease_values[-1]:
            errors.append("crash-wave-lease-not-bound-to-last-observation")

    raw_journal_crash = evidence.get("journalStateAtCrash")
    journal_crash = raw_journal_crash if isinstance(raw_journal_crash, dict) else {}
    if not isinstance(raw_journal_crash, dict):
        errors.append("journal-state-at-crash-must-be-object")
    if str(journal_crash.get("actionId") or "") != action_id:
        errors.append("journal-state-at-crash-action-id-binding-mismatch")
    try:
        journal_crash_epoch = int(str(journal_crash.get("executionEpoch")))
    except (TypeError, ValueError):
        journal_crash_epoch = None
        errors.append("journal-state-at-crash-invalid-execution-epoch")
    if journal_crash_epoch != journal_epoch_a:
        errors.append("journal-state-at-crash-epoch-binding-mismatch")
    if str(journal_crash.get("ownerInstanceId") or "") != expected_worker_a_owner:
        errors.append("journal-state-at-crash-owner-binding-mismatch")
    if str(journal_crash.get("leaseUntil") or "") != str(evidence.get("workerALeaseUntil") or ""):
        errors.append("journal-state-at-crash-lease-binding-mismatch")
    if str(journal_crash.get("state") or "") != "EXECUTING":
        errors.append("journal-state-at-crash-not-executing")
    raw_journal_crash_handle = journal_crash.get("effectHandle")
    journal_crash_handle = raw_journal_crash_handle if isinstance(raw_journal_crash_handle, dict) else {}
    if (
        str(journal_crash_handle.get("kind") or "") != "repair"
        or str(journal_crash_handle.get("repairId") or "") != str(evidence.get("repairId") or "")
    ):
        errors.append("journal-state-at-crash-effect-binding-mismatch")

    claim_records = (claim_schedule, claim_wave, claim_action)
    claim_leases = [
        parse_record_timestamp(record, "leaseUntil", "runner-state-takeover-claim-invalid-lease")
        for record in claim_records
    ]
    if all(value is not None for value in claim_leases) and len(set(claim_leases)) != 1:
        errors.append("runner-state-takeover-claim-lease-diverged")
    claim_times = [
        parse_record_timestamp(record, "updatedAt", "runner-state-takeover-claim-invalid-updated-at")
        for record in claim_records
    ]
    for claim_lease, claim_time in zip(claim_leases, claim_times):
        if claim_lease is not None and claim_time is not None and claim_lease <= claim_time:
            errors.append("runner-state-takeover-claim-lease-not-active")

    outer_lease_values = [
        parsed_leases.get(field)
        for field in (
            "workerALeaseUntil",
            "workerAScheduleLeaseUntil",
            "workerAWaveLeaseUntil",
            "workerAWaveActionLeaseUntil",
        )
    ]
    max_outer_lease = max(value for value in outer_lease_values if value is not None) if all(
        value is not None for value in outer_lease_values
    ) else None
    if max_outer_lease is not None and any(claim_time is not None and claim_time <= max_outer_lease for claim_time in claim_times):
        errors.append("takeover-claim-occurred-before-all-worker-a-leases-expired")

    terminal_times = [
        parse_record_timestamp(record, "updatedAt", "runner-state-takeover-invalid-updated-at")
        for record in (takeover_schedule, takeover_wave, takeover_action)
    ]
    latest_claim_time = max(value for value in claim_times if value is not None) if all(
        value is not None for value in claim_times
    ) else None
    earliest_terminal_time = min(value for value in terminal_times if value is not None) if all(
        value is not None for value in terminal_times
    ) else None
    if latest_claim_time is not None and earliest_terminal_time is not None and latest_claim_time >= earliest_terminal_time:
        errors.append("takeover-claim-not-before-terminal-runner-state")

    journal_takeover_at: datetime | None = None
    raw_journal_events = evidence.get("journalEvents")
    if isinstance(raw_journal_events, list):
        for raw_event in raw_journal_events:
            if not isinstance(raw_event, dict):
                continue
            raw_handle = raw_event.get("effectHandle")
            event_handle = raw_handle if isinstance(raw_handle, dict) else {}
            try:
                event_epoch = int(str(raw_event.get("executionEpoch")))
            except (TypeError, ValueError):
                continue
            if (
                str(raw_event.get("eventType") or "") == "ACTION_TAKEOVER"
                and str(raw_event.get("state") or "") == "RECONCILING"
                and str(raw_event.get("actionId") or "") == action_id
                and event_epoch == journal_epoch_b
                and str(raw_event.get("ownerInstanceId") or "") == expected_worker_b_owner
                and str(event_handle.get("kind") or "") == "repair"
                and str(event_handle.get("repairId") or "") == str(evidence.get("repairId") or "")
            ):
                journal_takeover_at = parse_record_timestamp(
                    raw_event,
                    "createdAt",
                    "runner-state-invalid-journal-takeover-created-at",
                )
    if journal_takeover_at is None:
        errors.append("missing-action-bound-journal-takeover-event")
    if max_outer_lease is not None and journal_takeover_at is not None and journal_takeover_at <= max_outer_lease:
        errors.append("journal-takeover-occurred-before-all-worker-a-leases-expired")

    settlement_events = evidence.get("settlementEvents")
    if not isinstance(settlement_events, list):
        return errors + ["settlement-events-must-be-list"]
    consuming_indexes: list[int] = []
    consumed_indexes: list[int] = []
    settlement_times: list[datetime] = []
    repair_id = str(evidence.get("repairId") or "")
    for index, raw_event in enumerate(settlement_events):
        if not isinstance(raw_event, dict):
            errors.append("settlement-event-not-object")
            continue
        status = str(raw_event.get("toStatus") or "")
        if status not in {"CONSUMING", "CONSUMED"}:
            continue
        if status == "CONSUMING":
            consuming_indexes.append(index)
        else:
            consumed_indexes.append(index)
        if str(raw_event.get("actionId") or "") != action_id:
            errors.append("settlement-action-id-not-bound-to-action")
        settlement_time = parse_record_timestamp(
            raw_event,
            "createdAt",
            "invalid-settlement-created-at",
        )
        if settlement_time is not None:
            settlement_times.append(settlement_time)
        try:
            settlement_epoch = int(str(raw_event.get("executionEpoch")))
        except (TypeError, ValueError):
            errors.append("invalid-settlement-execution-epoch")
            continue
        if settlement_epoch != journal_epoch_b:
            errors.append("settlement-execution-epoch-not-bound-to-takeover")
        raw_handle = raw_event.get("effectHandle")
        effect_handle = raw_handle if isinstance(raw_handle, dict) else {}
        if str(effect_handle.get("kind") or "") != "repair" or str(effect_handle.get("repairId") or "") != repair_id:
            errors.append("settlement-effect-handle-not-bound-to-repair")
    if len(consuming_indexes) != 1:
        errors.append("settlement-consuming-count-not-exactly-one")
    if len(consumed_indexes) != 1:
        errors.append("settlement-consumed-count-not-exactly-one")
    if consuming_indexes and consumed_indexes and consumed_indexes[0] <= consuming_indexes[0]:
        errors.append("settlement-consumed-before-consuming")
    if latest_claim_time is not None and settlement_times and latest_claim_time >= min(settlement_times):
        errors.append("takeover-claim-not-before-settlement")
    return errors


def validate_blast_radius_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    errors = _require_fields(evidence, ("simulator", "simulationPassed", "proposedActionIds", "simulationDetails"))
    if evidence.get("simulator") != "resilience_coordinator.simulate_coordination_wave":
        errors.append("blast-radius-simulator-identity-mismatch")
    if evidence.get("simulationPassed") is not True:
        errors.append("blast-radius-simulation-not-passed")
    proposed = evidence.get("proposedActionIds")
    if not isinstance(proposed, list):
        errors.append("blast-radius-proposed-actions-must-be-list")
        proposed = []
    details = evidence.get("simulationDetails")
    if not isinstance(details, dict):
        return errors + ["blast-radius-simulation-details-must-be-object"]
    if details.get("passed") is not True:
        errors.append("blast-radius-details-not-passed")
    if details.get("proposedActionIds") != proposed:
        errors.append("blast-radius-proposed-action-binding-mismatch")
    running_ids = details.get("runningActionIds")
    if not isinstance(running_ids, list):
        errors.append("blast-radius-running-actions-must-be-list")
        running_ids = []
    evaluations = details.get("evaluations")
    if not isinstance(evaluations, dict) or not evaluations:
        return errors + ["blast-radius-evaluations-missing"]
    running_effects = 0
    for evaluation_key, raw_evaluation in evaluations.items():
        if not isinstance(raw_evaluation, dict):
            errors.append(f"blast-radius-evaluation-not-object:{evaluation_key}")
            continue
        evaluation = raw_evaluation
        errors.extend(
            f"blast-radius-{evaluation_key}-{error}"
            for error in _require_fields(
                evaluation,
                (
                    "policyId",
                    "backupId",
                    "minCommittedCopies",
                    "minFailureDomains",
                    "copiesBefore",
                    "copiesDuring",
                    "copySafetyFloor",
                    "failureDomainsBefore",
                    "failureDomainsDuring",
                    "failureDomainSafetyFloor",
                    "runningEffectCount",
                    "passed",
                ),
            )
        )
        try:
            min_copies = int(str(evaluation.get("minCommittedCopies")))
            min_domains = int(str(evaluation.get("minFailureDomains")))
            copies_before = int(str(evaluation.get("copiesBefore")))
            copies_during = int(str(evaluation.get("copiesDuring")))
            copy_floor = int(str(evaluation.get("copySafetyFloor")))
            domain_floor = int(str(evaluation.get("failureDomainSafetyFloor")))
            running_effect_count = int(str(evaluation.get("runningEffectCount")))
        except (TypeError, ValueError):
            errors.append(f"blast-radius-invalid-numeric-fields:{evaluation_key}")
            continue
        expected_copy_floor = min_copies if copies_before >= min_copies else copies_before
        if copy_floor != expected_copy_floor or copies_during < copy_floor:
            errors.append(f"blast-radius-copy-floor-violation:{evaluation_key}")
        domains_before = evaluation.get("failureDomainsBefore")
        domains_during = evaluation.get("failureDomainsDuring")
        if not isinstance(domains_before, list) or not isinstance(domains_during, list):
            errors.append(f"blast-radius-failure-domains-must-be-lists:{evaluation_key}")
        else:
            expected_domain_floor = min_domains if len(domains_before) >= min_domains else len(domains_before)
            if domain_floor != expected_domain_floor or len(domains_during) < domain_floor:
                errors.append(f"blast-radius-domain-floor-violation:{evaluation_key}")
        if running_effect_count < 0:
            errors.append(f"blast-radius-negative-running-effect-count:{evaluation_key}")
        running_effects += max(0, running_effect_count)
        if evaluation.get("passed") is not True:
            errors.append(f"blast-radius-evaluation-not-passed:{evaluation_key}")
    if check_name == "runningEffectsParticipateInBlastRadiusSimulation" and (not running_ids or running_effects < 1):
        errors.append("blast-radius-running-effects-not-proven")
    return errors


def validate_atomic_budget_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    errors = _require_fields(evidence, ("scope", "processResults", "admittedCount", "rejectedCount"))
    expected_scope = {
        "twoProcessesCannotOversubscribeGlobalBudget": "global",
        "twoProcessesCannotOversubscribeTargetBudget": "target",
        "twoProcessesCannotOversubscribePolicyBudget": "policy",
        "twoProcessesCannotOversubscribeFailureDomainBudget": "failure-domain",
    }.get(check_name)
    scope = str(evidence.get("scope") or "")
    if expected_scope is not None and scope != expected_scope:
        errors.append(f"atomic-budget-scope-mismatch:{scope}!={expected_scope}")
    elif scope not in {"global", "target", "policy", "failure-domain"}:
        errors.append("invalid-atomic-budget-scope")
    raw_results = evidence.get("processResults")
    if not isinstance(raw_results, list):
        return errors + ["process-results-must-be-list"]
    if len(raw_results) != 2:
        errors.append("atomic-budget-proof-requires-two-process-results")
    pids: set[int] = set()
    admitted = 0
    rejected = 0
    for raw_result in raw_results:
        if not isinstance(raw_result, dict):
            errors.append("process-result-must-be-object")
            continue
        try:
            pid = int(str(raw_result.get("pid")))
        except (TypeError, ValueError):
            errors.append("invalid-process-pid")
            continue
        if pid <= 0:
            errors.append("invalid-process-pid")
        pids.add(pid)
        if raw_result.get("admitted") is True:
            admitted += 1
            try:
                if int(str(raw_result.get("executionEpoch"))) < 1:
                    errors.append("admitted-process-missing-execution-epoch")
            except (TypeError, ValueError):
                errors.append("admitted-process-missing-execution-epoch")
        elif raw_result.get("admitted") is False:
            rejected += 1
            if not str(raw_result.get("reason") or ""):
                errors.append("rejected-process-missing-reason")
        else:
            errors.append("process-result-admitted-must-be-boolean")
    if len(pids) != 2:
        errors.append("atomic-budget-process-pids-not-distinct")
    if admitted != 1 or rejected != 1:
        errors.append("atomic-budget-race-not-one-admitted-one-rejected")
    if evidence.get("admittedCount") != admitted or evidence.get("rejectedCount") != rejected:
        errors.append("atomic-budget-declared-counts-mismatch")
    return errors


def validate_predictive_planning_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    if not isinstance(evidence, dict):
        return ["not-a-dict"]
    required: dict[str, tuple[str, ...]] = {
        "absentAuthoritativeRiskIsClosedOrRetired": ("status", "closureReason", "coverageComplete"),
        "supersededBackupRiskCannotRemainOpenForever": ("status", "closureReason", "previousBackupId"),
        "policyDisabledRiskIsRetired": ("status", "closureReason"),
        "unknownCoverageDoesNotImplicitlyClearRisk": ("status", "closureReason"),
        "schedulerReservationDoesNotCountAsConsumedService": ("reservationStatus", "actionsServed"),
        "preemptedActionReleasesFairnessReservation": ("reservationStatus", "releaseReason"),
        "completedActionChargesObservedBytesExactlyOnce": ("actualBytes", "actionsServed"),
        "serviceConsumptionSurvivesRestart": ("actionsServed", "virtualRuntime"),
        "waveOneCannotStartBeforeWaveZeroVerified": ("admitted", "reason"),
        "failedWavePausesDownstreamActions": ("scheduleStatus", "admitted"),
        "staleWaveRequiresReplan": ("scheduleStatus", "reason"),
        "waveRevalidatesFreshRiskBeforeExecution": ("revalidatedRisk",),
        "waveRevalidatesAuthorityBeforeExecution": ("revalidatedAuthority",),
        "waveRevalidatesBlastRadiusBeforeExecution": ("revalidatedBlastRadius",),
        "fleetSloExposes1h24h7d30dWindows": ("windows",),
        "insufficientSloSamplesAreExplicit": ("status",),
        "capacityForecastUsesDurableObservations": ("sampleCount", "forecastStatus"),
        "forecastWithInsufficientSamplesFailsClosed": ("forecastStatus",),
        "thirtyDayCapacityForecastProduced": ("horizonDays", "forecastStatus"),
        "ninetyDayCapacityForecastProduced": ("horizonDays", "forecastStatus"),
        "forecastProvidesP50AndP90Headroom": ("p50FreeBytes", "p90FreeBytes"),
        "forecastBacktestErrorIsPersisted": ("mae", "bias"),
        "overoptimisticForecastLowersConfidence": ("overoptimistic", "confidence"),
        "costModelUsesVersionedPriceCatalog": ("priceCatalogVersion", "priceCatalogDigest"),
        "unknownTargetPriceDoesNotBecomeZero": ("status",),
        "egressCostIsIncluded": ("egress",),
        "storageCostIsIncluded": ("storage",),
        "optimizerNeverReducesMinCommittedCopies": ("accepted", "violations"),
        "optimizerNeverReducesMinFailureDomains": ("accepted", "violations"),
        "optimizerRejectsUnsafeCheaperPlan": ("accepted", "violations"),
        "candidatePlanIsDeterministicForSameInputs": ("candidatePlanDigest", "repeatDigest"),
        "whatIfProducesNoStorageWrites": ("s3PutCount",),
        "whatIfProducesNoStorageDeletes": ("s3DeleteCount",),
        "whatIfDoesNotMutateAuthority": ("authorityMutationCount",),
        "whatIfDoesNotMutateActionJournal": ("actionJournalMutationCount",),
        "whatIfBindsObservedFleetSnapshot": ("sourceSnapshotDigest",),
        "whatIfIncludesRunningEffects": ("runningEffects",),
        "whatIfIncludesMaintenanceWindows": ("maintenanceWindows",),
        "optimizationProofBindsForecastDigest": ("forecastDigest",),
        "optimizationProofBindsPriceCatalogDigest": ("priceCatalogDigest",),
        "optimizationProofBindsAuthorityHead": ("authorityHeadDigest",),
        "optimizationProofRecomputesSafetyConstraints": ("durability",),
        "federationSnapshotContainsNoCredentials": ("forbiddenKeys",),
        "federationSnapshotIsDigestBound": ("snapshotDigest",),
        "incompatibleFleetWireVersionFailsClosed": ("status",),
        "federatedSimulationCannotMutateRemoteFleet": ("remoteMutations",),
        "objectSetV1WireFormatUnchanged": ("objectSetVersion",),
        "receiptV4Unchanged": ("receiptVersion",),
        "commitV4Unchanged": ("commitVersion",),
        "fastCdcV3Unchanged": ("cdcVersion",),
        "randomizedAgeUnchanged": ("ageRandomized",),
        "controlAuthorityV1Unchanged": ("authoritySchema",),
        "authorityCheckpointV1Unchanged": ("checkpointSchema",),
        "drReadinessProofV1Unchanged": ("drReadinessProofSchema",),
    }
    fields = required.get(check_name)
    errors = _require_fields(evidence, fields) if fields else []
    if check_name in {"absentAuthoritativeRiskIsClosedOrRetired", "supersededBackupRiskCannotRemainOpenForever", "policyDisabledRiskIsRetired"}:
        if str(evidence.get("status") or "") in {"OPEN", "REOPENED", "UNKNOWN_COVERAGE"}:
            errors.append("risk-subject-still-open")
        if evidence.get("coverageComplete") is False:
            errors.append("complete-coverage-required-to-close")
    if check_name == "unknownCoverageDoesNotImplicitlyClearRisk":
        if str(evidence.get("status") or "") in {"CLEARED", "SUPERSEDED", "RETIRED"}:
            errors.append("incomplete-coverage-implicitly-cleared-risk")
        if str(evidence.get("closureReason") or "") != "UNKNOWN_COVERAGE":
            errors.append("unknown-coverage-reason-missing")
    if check_name == "schedulerReservationDoesNotCountAsConsumedService":
        if str(evidence.get("reservationStatus") or "") != "RESERVED":
            errors.append("schedule-did-not-reserve")
        try:
            if int(str(evidence.get("actionsServed"))) != 0:
                errors.append("reservation-counted-as-consumed")
        except (TypeError, ValueError):
            errors.append("invalid-actions-served")
    if check_name == "preemptedActionReleasesFairnessReservation":
        if str(evidence.get("reservationStatus") or "") != "RELEASED":
            errors.append("preempted-reservation-not-released")
        if str(evidence.get("releaseReason") or "") != "PREEMPTED":
            errors.append("preempted-release-reason-mismatch")
    if check_name == "completedActionChargesObservedBytesExactlyOnce":
        try:
            if int(str(evidence.get("actionsServed"))) != 1:
                errors.append("consumed-service-not-charged-once")
        except (TypeError, ValueError):
            errors.append("invalid-actions-served")
    if check_name == "waveOneCannotStartBeforeWaveZeroVerified":
        if evidence.get("admitted") is True:
            errors.append("wave-one-started-before-wave-zero-verified")
        if str(evidence.get("reason") or "") != "PREDECESSOR_WAVE_NOT_VERIFIED":
            errors.append("missing-predecessor-gate")
    if check_name == "staleWaveRequiresReplan":
        if str(evidence.get("scheduleStatus") or "") != "PAUSED_REPLAN":
            errors.append("stale-wave-did-not-pause-replan")
    if check_name == "fleetSloExposes1h24h7d30dWindows":
        windows = evidence.get("windows")
        if not isinstance(windows, list) or set(windows) < {"1h", "24h", "7d", "30d", "lifetime"}:
            errors.append("slo-windows-incomplete")
    if check_name == "insufficientSloSamplesAreExplicit":
        if str(evidence.get("status") or "") != "INSUFFICIENT_DATA":
            errors.append("insufficient-slo-not-explicit")
    if check_name == "forecastWithInsufficientSamplesFailsClosed":
        if str(evidence.get("forecastStatus") or "") != "INSUFFICIENT_DATA":
            errors.append("insufficient-forecast-not-fail-closed")
    if check_name in {"thirtyDayCapacityForecastProduced", "ninetyDayCapacityForecastProduced"}:
        expected = 30 if check_name.startswith("thirty") else 90
        try:
            if int(str(evidence.get("horizonDays"))) != expected:
                errors.append("forecast-horizon-mismatch")
        except (TypeError, ValueError):
            errors.append("invalid-forecast-horizon")
    if check_name == "unknownTargetPriceDoesNotBecomeZero":
        if str(evidence.get("status") or "") != "UNKNOWN_COST":
            errors.append("unknown-price-was-not-unknown-cost")
        if evidence.get("monthlyCost") in {0, 0.0}:
            errors.append("unknown-price-defaulted-to-zero")
    if check_name in {"optimizerNeverReducesMinCommittedCopies", "optimizerNeverReducesMinFailureDomains", "optimizerRejectsUnsafeCheaperPlan"}:
        if evidence.get("accepted") is True:
            errors.append("unsafe-candidate-was-accepted")
        violations = evidence.get("violations")
        if not isinstance(violations, list) or not violations:
            errors.append("durability-violation-not-recorded")
    if check_name == "candidatePlanIsDeterministicForSameInputs":
        if str(evidence.get("candidatePlanDigest") or "") != str(evidence.get("repeatDigest") or ""):
            errors.append("candidate-plan-not-deterministic")
        errors.extend(_require_sha256(evidence.get("candidatePlanDigest"), field="candidatePlanDigest"))
    if check_name.startswith("whatIf"):
        for field in ("s3PutCount", "s3DeleteCount", "authorityMutationCount", "actionJournalMutationCount", "sideEffectsObserved"):
            if field in evidence:
                try:
                    if int(str(evidence.get(field))) != 0:
                        errors.append(f"what-if-side-effect:{field}")
                except (TypeError, ValueError):
                    errors.append(f"invalid-what-if-counter:{field}")
        if check_name == "whatIfBindsObservedFleetSnapshot":
            errors.extend(_require_sha256(evidence.get("sourceSnapshotDigest"), field="sourceSnapshotDigest"))
        if check_name == "whatIfIncludesRunningEffects" and not isinstance(evidence.get("runningEffects"), list):
            errors.append("running-effects-missing")
        if check_name == "whatIfIncludesMaintenanceWindows" and not isinstance(evidence.get("maintenanceWindows"), list):
            errors.append("maintenance-windows-missing")
    if check_name.startswith("optimizationProof"):
        for field in ("forecastDigest", "priceCatalogDigest", "authorityHeadDigest"):
            if field in (fields or ()) or evidence.get(field) not in (None, ""):
                errors.extend(_require_sha256(evidence.get(field), field=field))
        if check_name == "optimizationProofRecomputesSafetyConstraints":
            durability = evidence.get("durability")
            if not isinstance(durability, dict):
                errors.append("durability-missing")
            elif durability.get("copiesPreserved") is not True or durability.get("failureDomainsPreserved") is not True:
                errors.append("safety-constraints-not-preserved")
    if check_name == "federationSnapshotContainsNoCredentials":
        forbidden = evidence.get("forbiddenKeys")
        if not isinstance(forbidden, list) or forbidden:
            errors.append("federation-snapshot-contains-credentials")
    if check_name == "federationSnapshotIsDigestBound":
        errors.extend(_require_sha256(evidence.get("snapshotDigest"), field="snapshotDigest"))
    if check_name == "incompatibleFleetWireVersionFailsClosed":
        if str(evidence.get("status") or "") != "INCOMPATIBLE":
            errors.append("incompatible-wire-did-not-fail-closed")
    if check_name == "federatedSimulationCannotMutateRemoteFleet":
        try:
            if int(str(evidence.get("remoteMutations"))) != 0:
                errors.append("federated-simulation-mutated-remote")
        except (TypeError, ValueError):
            errors.append("invalid-remote-mutation-count")
    return errors


def validate_typed_predictive_planning_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    """Validate a predictive-planning-proof-v1 payload inside the v2 envelope."""
    from deepseek_infra.infra.workspace import resilience_predictive_proof

    if check_name not in resilience_predictive_proof.PREDICTIVE_PROOF_CHECKS:
        return [f"unsupported-predictive-proof-check:{check_name}"]
    return resilience_predictive_proof.validate_predictive_planning_proof(evidence)


def validate_typed_federation_trust_proof(evidence: dict[str, Any], check_name: str) -> list[str]:
    """Validate a federation-trust-proof-v1 payload inside the frozen v2 envelope."""

    from deepseek_infra.infra.workspace import federation_trust_proof

    if check_name not in federation_trust_proof.FEDERATION_TRUST_PROOF_CHECKS:
        return [f"unsupported-federation-trust-proof-check:{check_name}"]
    return federation_trust_proof.validate_federation_trust_proof(evidence)


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
    "autonomousProofUsesActualReceiptBytes": validate_autonomous_storage_bytes_proof,
    "autonomousProofUsesActualCommitBytes": validate_autonomous_storage_bytes_proof,
    "receiptSha256MatchesCommitReceiptDigest": validate_autonomous_storage_bytes_proof,
    "proofObjectSetDigestMatchesCommit": validate_autonomous_storage_bytes_proof,
    "proofObjectKeysExistOnExpectedMinioEndpoint": validate_autonomous_storage_bytes_proof,
    "receiptV4Unchanged": validate_autonomous_storage_bytes_proof,
    "commitV4Unchanged": validate_autonomous_storage_bytes_proof,
    "crashRecoveryObservedExistingEffect": validate_crash_recovery_proof,
    "leaseTakeoverUsedNewExecutionEpoch": validate_crash_recovery_proof,
    "realWorkerCrashOccursDuringRemoteRepair": validate_crash_recovery_proof,
    "freshWorkerTakesOverExpiredAction": validate_crash_recovery_proof,
    "takeoverExecutionEpochStrictlyIncreases": validate_crash_recovery_proof,
    "takeoverEntersReconcilingBeforeMutation": validate_crash_recovery_proof,
    "takeoverFindsExistingRemoteEffect": validate_crash_recovery_proof,
    "takeoverDoesNotCreateSecondRepairJob": validate_crash_recovery_proof,
    "longRunningWaveRenewsScheduleLease": validate_wave_crash_recovery_proof,
    "longRunningWaveRenewsWaveLease": validate_wave_crash_recovery_proof,
    "realProcessWaveSigkillTakeoverUsesHigherEpoch": validate_wave_crash_recovery_proof,
    "realProcessWaveSigkillDoesNotDuplicateEffect": validate_wave_crash_recovery_proof,
    "realProcessWaveSigkillSettlesExactlyOnce": validate_wave_crash_recovery_proof,
    "blastRadiusInvariantVerified": validate_blast_radius_proof,
    "degradedFleetCannotBeFurtherDegraded": validate_blast_radius_proof,
    "runningEffectsParticipateInBlastRadiusSimulation": validate_blast_radius_proof,
    "atomicBudgetAdmissionVerified": validate_atomic_budget_proof,
    "twoProcessesCannotOversubscribeGlobalBudget": validate_atomic_budget_proof,
    "twoProcessesCannotOversubscribeTargetBudget": validate_atomic_budget_proof,
    "twoProcessesCannotOversubscribePolicyBudget": validate_atomic_budget_proof,
    "twoProcessesCannotOversubscribeFailureDomainBudget": validate_atomic_budget_proof,
    "absentAuthoritativeRiskIsClosedOrRetired": validate_predictive_planning_proof,
    "supersededBackupRiskCannotRemainOpenForever": validate_predictive_planning_proof,
    "policyDisabledRiskIsRetired": validate_predictive_planning_proof,
    "unknownCoverageDoesNotImplicitlyClearRisk": validate_predictive_planning_proof,
    "schedulerReservationDoesNotCountAsConsumedService": validate_predictive_planning_proof,
    "preemptedActionReleasesFairnessReservation": validate_predictive_planning_proof,
    "completedActionChargesObservedBytesExactlyOnce": validate_predictive_planning_proof,
    "serviceConsumptionSurvivesRestart": validate_predictive_planning_proof,
    "waveOneCannotStartBeforeWaveZeroVerified": validate_predictive_planning_proof,
    "failedWavePausesDownstreamActions": validate_predictive_planning_proof,
    "staleWaveRequiresReplan": validate_predictive_planning_proof,
    "waveRevalidatesFreshRiskBeforeExecution": validate_predictive_planning_proof,
    "waveRevalidatesAuthorityBeforeExecution": validate_predictive_planning_proof,
    "waveRevalidatesBlastRadiusBeforeExecution": validate_predictive_planning_proof,
    "fleetSloExposes1h24h7d30dWindows": validate_predictive_planning_proof,
    "insufficientSloSamplesAreExplicit": validate_predictive_planning_proof,
    "capacityForecastUsesDurableObservations": validate_typed_predictive_planning_proof,
    "forecastWithInsufficientSamplesFailsClosed": validate_predictive_planning_proof,
    "thirtyDayCapacityForecastProduced": validate_predictive_planning_proof,
    "ninetyDayCapacityForecastProduced": validate_predictive_planning_proof,
    "forecastProvidesP50AndP90Headroom": validate_predictive_planning_proof,
    "forecastBacktestErrorIsPersisted": validate_typed_predictive_planning_proof,
    "overoptimisticForecastLowersConfidence": validate_predictive_planning_proof,
    "costModelUsesVersionedPriceCatalog": validate_typed_predictive_planning_proof,
    "unknownTargetPriceDoesNotBecomeZero": validate_predictive_planning_proof,
    "egressCostIsIncluded": validate_predictive_planning_proof,
    "storageCostIsIncluded": validate_predictive_planning_proof,
    "optimizerNeverReducesMinCommittedCopies": validate_typed_predictive_planning_proof,
    "optimizerNeverReducesMinFailureDomains": validate_typed_predictive_planning_proof,
    "optimizerRejectsUnsafeCheaperPlan": validate_predictive_planning_proof,
    "candidatePlanIsDeterministicForSameInputs": validate_predictive_planning_proof,
    "whatIfProducesNoStorageWrites": validate_typed_predictive_planning_proof,
    "whatIfProducesNoStorageDeletes": validate_typed_predictive_planning_proof,
    "whatIfDoesNotMutateAuthority": validate_typed_predictive_planning_proof,
    "whatIfDoesNotMutateActionJournal": validate_typed_predictive_planning_proof,
    "whatIfBindsObservedFleetSnapshot": validate_typed_predictive_planning_proof,
    "whatIfIncludesRunningEffects": validate_typed_predictive_planning_proof,
    "whatIfIncludesMaintenanceWindows": validate_typed_predictive_planning_proof,
    "optimizationProofBindsForecastDigest": validate_typed_predictive_planning_proof,
    "optimizationProofBindsPriceCatalogDigest": validate_typed_predictive_planning_proof,
    "optimizationProofBindsAuthorityHead": validate_typed_predictive_planning_proof,
    "optimizationProofRecomputesSafetyConstraints": validate_typed_predictive_planning_proof,
    "federationSnapshotContainsNoCredentials": validate_predictive_planning_proof,
    "federationSnapshotIsDigestBound": validate_predictive_planning_proof,
    "incompatibleFleetWireVersionFailsClosed": validate_predictive_planning_proof,
    "federatedSimulationCannotMutateRemoteFleet": validate_predictive_planning_proof,
    "realThreeMinioPredictivePlanningE2E": validate_minio_endpoints_proof,
    "realCapacityChangesDriveForecast": validate_typed_predictive_planning_proof,
    "realMinioInventoryUnchangedByWhatIf": validate_typed_predictive_planning_proof,
    "predictiveProofBindsCapacityObservationSet": validate_typed_predictive_planning_proof,
    "predictiveProofBindsForecastRecord": validate_typed_predictive_planning_proof,
    "predictiveProofBindsForecastBacktest": validate_typed_predictive_planning_proof,
    "predictiveProofBindsFreshStateBundle": validate_typed_predictive_planning_proof,
    "predictiveProofBindsPreAndPostState": validate_typed_predictive_planning_proof,
    "predictiveProofRejectsSelfReportedZeroMutation": validate_typed_predictive_planning_proof,
    "fleetIdentityUsesDedicatedFederationSigningKey": validate_typed_federation_trust_proof,
    "federationKeyIsDistinctFromAgeIdentity": validate_typed_federation_trust_proof,
    "federationKeyIsDistinctFromAuthorityIdentity": validate_typed_federation_trust_proof,
    "peerTrustRequiresPinnedRoot": validate_typed_federation_trust_proof,
    "trustOnFirstUseIsRejected": validate_typed_federation_trust_proof,
    "rotatedOnlineSignerRequiresPinnedRootCertificate": validate_typed_federation_trust_proof,
    "revokedFederationSignerIsRejected": validate_typed_federation_trust_proof,
    "federationReadinessSignatureIsVerified": validate_typed_federation_trust_proof,
    "readinessAttestationBindsFullCanonicalPayload": validate_typed_federation_trust_proof,
    "readinessSequenceReplayIsRejected": validate_typed_federation_trust_proof,
    "expiredReadinessAttestationIsRejected": validate_typed_federation_trust_proof,
    "futureReadinessAttestationBeyondSkewIsRejected": validate_typed_federation_trust_proof,
    "challengeResponseBindsBothFleetIds": validate_typed_federation_trust_proof,
    "challengeNonceReplayIsRejected": validate_typed_federation_trust_proof,
    "federationTrustProofIsSemanticallyValidated": validate_typed_federation_trust_proof,
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
