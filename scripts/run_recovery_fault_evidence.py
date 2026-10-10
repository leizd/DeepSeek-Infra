#!/usr/bin/env python3
"""Run restart and recovery fault gates and emit exact-merge Evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.workspace import backup_crypto  # noqa: E402
from scripts.release_evidence import stamp_release_report  # noqa: E402


SCENARIOS: dict[str, tuple[str, ...]] = {
    "process-component-resume": (
        "tests/test_backup_object_set_contracts.py::test_object_set_restore_resumes_across_real_process_exits",
    ),
    "process-pause-resume": (
        "tests/test_backup_object_set_contracts.py::test_recovery_job_control_survives_real_process_exits",
    ),
    "disk-exhaustion": (
        "tests/test_backup_recovery_preflight.py::test_preflight_service_maps_capacity_block_to_stable_conflict",
    ),
    "lease-conflict": (
        "tests/test_backup_recovery_lease.py::test_renew_fails_closed_on_cas_conflict",
    ),
    "cache-corruption": (
        "tests/test_backup_component_cache.py::test_cache_corruption_is_scrubbed_and_never_returned",
    ),
    "remote-mutation": (
        "tests/test_backup_object_set_contracts.py::test_object_set_fetch_fails_closed_and_resumes_partial_members",
    ),
    "partial-federated-commit": (
        "tests/test_backup_recovery_job.py::test_abort_prepared_rolls_back_but_uncertain_commit_requires_recovery",
    ),
}

CHECK_SCENARIOS = {
    "componentTransferResumesAfterRealProcessExit": "process-component-resume",
    "pauseResumeSurvivesRealProcessExit": "process-pause-resume",
    "diskExhaustionBlocksBeforeMutation": "disk-exhaustion",
    "leaseConflictFailsClosed": "lease-conflict",
    "cacheCorruptionIsScrubbedAndRefetched": "cache-corruption",
    "remoteMutationFailsClosed": "remote-mutation",
    "partialFederatedCommitRequiresRecovery": "partial-federated-commit",
}


def run_scenarios(
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    *,
    helper_available: bool | None = None,
) -> dict[str, dict[str, object]]:
    available = backup_crypto.helper_path() is not None if helper_available is None else helper_available
    results: dict[str, dict[str, object]] = {}
    for name, node_ids in SCENARIOS.items():
        if name == "process-component-resume" and not available:
            results[name] = {
                "nodeIds": list(node_ids),
                "exitCode": 2,
                "error": "required production prerequisite missing: real Age backup-crypto helper",
            }
            continue
        completed = run([sys.executable, "-m", "pytest", "--no-cov", "-q", *node_ids], cwd=ROOT, check=False)
        results[name] = {"nodeIds": list(node_ids), "exitCode": completed.returncode}
    return results


def build_report(results: dict[str, dict[str, object]]) -> dict[str, object]:
    checks = {
        check: "PASS" if results[scenario]["exitCode"] == 0 else "FAIL"
        for check, scenario in CHECK_SCENARIOS.items()
    }
    return {
        "ok": all(value == "PASS" for value in checks.values()),
        "checks": checks,
        "checkProvenance": dict(CHECK_SCENARIOS),
        "scenarios": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    report = stamp_release_report(build_report(run_scenarios()), root=ROOT)
    output = args.out if args.out.is_absolute() else ROOT / args.out
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
