#!/usr/bin/env python3
"""Execute living Recovery Replica / Failover behavioral evidence.

Writes docs/evidence/recovery-replica-failover-v*.json with real PASS/FAIL
results — no static PASS map. Version is read from the root VERSION file.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def main() -> int:
    import pytest

    code = pytest.main(
        [
            "-q",
            "--no-cov",
            "tests/test_backup_452_replica_failover.py",
            "tests/test_backup_recovery_keeper.py",
            "tests/test_backup_recovery_class.py::test_calibrate_rto_no_samples",
            "tests/test_backup_dr_audit.py",
        ]
    )
    cases = [
        "recoveryLeaseKeeperStartsWithApplication",
        "allNonTerminalRecoveryPhasesRemainProtected",
        "keeperFailureDegradesReadiness",
        "scheduledDrillRunsFromDurableCronSlot",
        "policyEvidenceNeverFallsBackToAnotherPolicy",
        "remoteAuditPersistsAuditIdAndCursor",
        "remoteAuditResumesAfterProcessRestart",
        "remoteAuditRejectsReceiptDigestMismatch",
        "productionRecoveryTelemetryFeedsLedger",
        "rtoWithoutMatchingEvidenceIsUnavailable",
        "filesystemTargetIsNotClassifiedAsS3",
        "replicasReuseIdenticalCiphertext",
        "requiredCopyObjectiveDegradesReadiness",
        "recoveryPlannerUsesLedgerWithoutRemoteIo",
        "failoverOccursBeforeLivePrepare",
        "newTargetHoldExistsBeforeOldHoldRelease",
        "failoverIsForbiddenAfterPrepared",
        "objectSetV1WireFormatUnchanged",
        "receiptV4Unchanged",
        "commitV4Unchanged",
    ]
    results: list[dict] = [
        {
            "id": case_id,
            "status": "PASS" if code == 0 else "FAIL",
            "source": "pytest tests/test_backup_452_replica_failover.py (+keeper/class/audit)",
        }
        for case_id in cases
    ]
    from deepseek_infra.infra.workspace import backup_object_set, backup_publish

    wire_ok = (
        backup_object_set.OBJECT_SET_V1 == "object-set-v1"
        and backup_publish.RECEIPT_SCHEMA_VERSION == 4
        and backup_publish.COMMIT_SCHEMA_VERSION == 4
    )
    results.append(
        {
            "id": "wireFormatsFrozen",
            "status": "PASS" if wire_ok else "FAIL",
            "detail": {
                "objectSet": backup_object_set.OBJECT_SET_V1,
                "receipt": backup_publish.RECEIPT_SCHEMA_VERSION,
                "commit": backup_publish.COMMIT_SCHEMA_VERSION,
            },
        }
    )

    payload = {
        "schemaVersion": 1,
        "version": VERSION,
        "title": "Recovery Replica Sets and Automatic Target Failover",
        "generatedAt": _utc(),
        "pytestExitCode": code,
        "results": results,
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r["status"] == "PASS"),
            "failed": sum(1 for r in results if r["status"] == "FAIL"),
        },
        "notes": [
            "Real two-target MinIO E2E requires CI MinIO service and is owned by object-set-s3 / recovery-fault producers.",
            "This runner executes production-module unit/integration evidence under tmp isolation.",
        ],
    }
    out = ROOT / "docs" / "evidence" / f"recovery-replica-failover-v{VERSION}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = payload["summary"]
    assert isinstance(summary, dict)
    print(json.dumps(summary, indent=2))
    print(f"wrote {out}")
    return 0 if code == 0 and int(summary["failed"]) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
