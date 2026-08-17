#!/usr/bin/env python3
"""Run multi-target replica self-healing and write failover E2E scenarios."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_evidence import stamp_release_report  # noqa: E402

SCENARIOS: dict[str, tuple[str, ...]] = {
    "durable-repair-state-machine": (
        "tests/test_backup_454_replica_healing_contract.py::test_replica_repair_job_durability_and_resumption",
    ),
    "in-place-committed-healing": (
        "tests/test_backup_454_replica_healing_contract.py::test_in_place_committed_copy_healing_contract",
    ),
    "bounded-streaming-and-corrupt-replacement": (
        "tests/test_backup_454_replica_healing_contract.py::test_bounded_streaming_and_remote_corruption_replacement",
    ),
    "target-side-protection-leases": (
        "tests/test_backup_454_replica_healing_contract.py::test_target_side_durable_protection_lease_preserves_source",
    ),
    "two-phase-remote-audit": (
        "tests/test_backup_454_replica_healing_contract.py::test_two_phase_dr_remote_audit_chain_validation",
    ),
    "deterministic-write-failover": (
        "tests/test_backup_454_replica_healing_contract.py::test_deterministic_write_failover_and_force_full",
    ),
    "dual-minio-s3-e2e": (
        "tests/test_backup_454_dual_minio_e2e.py::test_dual_minio_replica_healing_and_write_failover_e2e",
    ),
}

CHECK_SCENARIOS = {
    "durableRepairJobStateMachine": "durable-repair-state-machine",
    "inPlaceCommittedCopyHealingContract": "in-place-committed-healing",
    "boundedStreamingAndCorruptReplacement": "bounded-streaming-and-corrupt-replacement",
    "targetSideDurableProtectionLeases": "target-side-protection-leases",
    "twoPhaseRemoteAuditWithGlobalCommitChainValidation": "two-phase-remote-audit",
    "deterministicWritePlacementAndForcedFullFailover": "deterministic-write-failover",
    "realDualMinioE2EIntegration": "dual-minio-s3-e2e",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run replica self-healing and write failover E2E scenarios")
    parser.add_argument("--out", type=Path, help="Path to write JSON evidence report")
    parser.add_argument("--json", action="store_true", help="Emit JSON report to stdout")
    args = parser.parse_args(argv)

    results: dict[str, dict[str, object]] = {}
    for name, node_ids in SCENARIOS.items():
        command = [sys.executable, "-m", "pytest", "--no-cov", "-q", *node_ids]
        completed = subprocess.run(command, cwd=ROOT, check=False)
        results[name] = {"nodeIds": list(node_ids), "exitCode": completed.returncode}

    checks = {
        check: "PASS" if results[scenario]["exitCode"] == 0 else "FAIL"
        for check, scenario in CHECK_SCENARIOS.items()
    }

    report = stamp_release_report(
        {
            "ok": all(value == "PASS" for value in checks.values()),
            "status": "PASS" if all(value == "PASS" for value in checks.values()) else "FAIL",
            "title": "Dual-Target Autonomous Replica Self-Healing & Backup Write Failover E2E",
            "checks": checks,
            "checkProvenance": dict(CHECK_SCENARIOS),
            "scenarios": results,
        },
        root=ROOT,
    )

    if args.out:
        output = args.out if args.out.is_absolute() else ROOT / args.out
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json or not args.out:
        print(json.dumps(report, ensure_ascii=False, indent=2))

    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
