#!/usr/bin/env python3
"""Run the real three-MinIO, real-Age Storage Control Plane Evidence gate."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.workspace import backup_crypto  # noqa: E402
from scripts.release_evidence import stamp_release_report  # noqa: E402

REAL_SCENARIO = "real-three-minio-storage-control-plane"
SCENARIOS: dict[str, tuple[str, ...]] = {
    REAL_SCENARIO: (
        "tests/test_backup_458_real_storage_control_plane_e2e.py::test_real_three_minio_storage_control_plane_e2e",
    ),
}
CHECK_SCENARIOS = {
    "realThreeMinioEndpoints": REAL_SCENARIO,
    "productionSchedulerBackupExecutorAndMaintenanceSupervisor": REAL_SCENARIO,
    "realAgeRandomizedEncryption": REAL_SCENARIO,
    "realServerStopFailoverAndRestartCatchup": REAL_SCENARIO,
    "providerBackedRepairReconciliation": REAL_SCENARIO,
    "autonomousDrainRetirementGcRestore": REAL_SCENARIO,
    "formalReceiptCommitHistoryPreserved": REAL_SCENARIO,
    "fakeS3AndStubCryptoForbidden": REAL_SCENARIO,
}
REQUIRED_ENDPOINTS = (
    "DEEPSEEK_TEST_S3_ENDPOINT_A",
    "DEEPSEEK_TEST_S3_ENDPOINT_B",
    "DEEPSEEK_TEST_S3_ENDPOINT_C",
)
REQUIRED_CONTAINERS = (
    "DEEPSEEK_TEST_MINIO_CONTAINER_A",
    "DEEPSEEK_TEST_MINIO_CONTAINER_B",
    "DEEPSEEK_TEST_MINIO_CONTAINER_C",
)


def _prerequisite_errors() -> list[str]:
    errors: list[str] = []
    endpoints = [str(os.environ.get(name) or "").rstrip("/") for name in REQUIRED_ENDPOINTS]
    missing_endpoints = [name for name, value in zip(REQUIRED_ENDPOINTS, endpoints, strict=True) if not value]
    if missing_endpoints:
        errors.append(f"missing endpoints: {', '.join(missing_endpoints)}")
    elif len(set(endpoints)) != 3:
        errors.append("three distinct S3 endpoints are required")
    missing_containers = [name for name in REQUIRED_CONTAINERS if not os.environ.get(name)]
    if missing_containers:
        errors.append(f"missing container identities: {', '.join(missing_containers)}")
    if importlib.util.find_spec("boto3") is None:
        errors.append("boto3 is unavailable")
    if backup_crypto.helper_path() is None:
        errors.append("real Age backup-crypto helper is unavailable")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    prerequisite_errors = _prerequisite_errors()
    results: dict[str, dict[str, object]] = {}
    if prerequisite_errors:
        results[REAL_SCENARIO] = {
            "nodeIds": list(SCENARIOS[REAL_SCENARIO]),
            "exitCode": 2,
            "error": "; ".join(prerequisite_errors),
        }
    else:
        environment = os.environ.copy()
        environment["DEEPSEEK_REQUIRE_REAL_STORAGE_CONTROL_E2E"] = "1"
        command = [sys.executable, "-m", "pytest", "--no-cov", "-q", *SCENARIOS[REAL_SCENARIO]]
        completed = subprocess.run(command, cwd=ROOT, env=environment, check=False)
        results[REAL_SCENARIO] = {"nodeIds": list(SCENARIOS[REAL_SCENARIO]), "exitCode": completed.returncode}

    checks = {
        check: "PASS" if results[scenario]["exitCode"] == 0 else "FAIL"
        for check, scenario in CHECK_SCENARIOS.items()
    }
    report = stamp_release_report(
        {
            "ok": all(value == "PASS" for value in checks.values()),
            "status": "PASS" if all(value == "PASS" for value in checks.values()) else "FAIL",
            "title": "Real Three-MinIO Storage Control Plane and Geo-Aware Lifecycle E2E",
            "checks": checks,
            "checkProvenance": dict(CHECK_SCENARIOS),
            "scenarios": results,
            "endpoints": {name: os.environ.get(name) for name in REQUIRED_ENDPOINTS},
        },
        root=ROOT,
    )
    output = args.out if args.out.is_absolute() else ROOT / args.out
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
