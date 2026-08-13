#!/usr/bin/env python3
"""Run independently attributable object-set gates and emit exact-merge Evidence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release_evidence import stamp_release_report  # noqa: E402
from deepseek_infra.infra.workspace import backup_crypto  # noqa: E402


SCENARIOS: dict[str, tuple[str, ...]] = {
    "full-projection": (
        "tests/test_backup_remote_restore_projection_e2e.py::test_full_remote_restore_applies_project_projection",
        "tests/test_backup_object_set_contracts.py::test_object_set_preview_plans_and_fetches_only_selected_component",
    ),
    "incremental-projection": (
        "tests/test_backup_object_set_contracts.py::test_incremental_object_set_restores_post_baseline_project_and_support_component",
    ),
    "unselected-contributor": (
        "tests/test_backup_object_set_contracts.py::test_object_set_federated_restore_keeps_unselected_live_contributor",
    ),
    "adaptive-memory": (
        "tests/test_backup_executor.py::test_incremental_archive_is_spooled_to_disk_before_encryption",
        "tests/test_backup_executor.py::test_oversized_delta_aborts_before_incremental_encryption",
    ),
    "protocol-upgrade": (
        "tests/test_backup_object_set_contracts.py::test_whole_age_lineage_forces_object_set_full_upgrade",
    ),
    "independent-age": (
        "tests/test_backup_object_set_contracts.py::test_build_object_set_independently_encrypts_control_and_payloads",
    ),
    "receipt-v4": (
        "tests/test_backup_object_set_contracts.py::test_receipt_v4_commits_exact_ciphertext_set_without_roles",
        "tests/test_backup_object_set_contracts.py::test_publish_v4_commits_every_ciphertext_member",
    ),
    "fail-closed-members": (
        "tests/test_backup_object_set_contracts.py::test_object_set_restore_fails_closed_when_committed_component_is_missing",
        "tests/test_backup_object_set_contracts.py::test_object_set_control_cannot_select_foreign_component",
    ),
    "control-first": (
        "tests/test_backup_object_set_contracts.py::test_object_set_restore_fetches_control_before_any_payload",
    ),
    "exact-selective-get": (
        "tests/test_backup_object_set_contracts.py::test_object_set_selective_restore_issues_gets_for_required_payload_only",
    ),
    "real-process-restart": (
        "tests/test_backup_object_set_contracts.py::test_object_set_restore_resumes_across_real_process_exits",
    ),
    "object-lifecycle": (
        "tests/test_backup_object_set_contracts.py::test_object_set_retention_hold_protects_every_ciphertext_member",
        "tests/test_backup_object_set_contracts.py::test_object_set_orphan_components_are_collected_only_after_grace",
    ),
    "legacy-whole-age": (
        "tests/test_backup_remote_restore_projection_e2e.py::test_full_remote_restore_round_trip_still_works",
        "tests/test_backup_448_contracts.py::test_incremental_chain_materialize_e2e",
    ),
    "real-minio": (
        "tests/test_backup_production_remote_restore_e2e.py::test_production_remote_restore_full_chain",
    ),
}

CHECK_SCENARIOS = {
    "fullSnapshotProjectionIsSelective": "full-projection",
    "incrementalSnapshotProjectionIsSelective": "incremental-projection",
    "projectCreatedAfterBaselineIsRestorable": "incremental-projection",
    "unselectedDivergedContributorRemainsUntouched": "unselected-contributor",
    "adaptiveDeltaCostUsesBoundedMemory": "adaptive-memory",
    "oversizedDeltaAbortsBeforeEncryption": "adaptive-memory",
    "objectSetUpgradeForcesFull": "protocol-upgrade",
    "objectSetControlIsIndependentlyEncrypted": "independent-age",
    "payloadComponentsUseIndependentRandomAgeSessions": "independent-age",
    "receiptV4CommitsExactCiphertextSet": "receipt-v4",
    "missingCommittedComponentFailsClosed": "fail-closed-members",
    "foreignComponentFailsClosed": "fail-closed-members",
    "previewFetchesControlObjectsOnly": "control-first",
    "projectedRestoreFetchesRequiredComponentsOnly": "exact-selective-get",
    "unselectedComponentsReceiveZeroGetRequests": "exact-selective-get",
    "crossFileDependencyFetchesSupportComponent": "incremental-projection",
    "supportComponentNeverReachesWorkspace": "incremental-projection",
    "restoreFetchResumesAfterActualProcessExit": "real-process-restart",
    "federatedCommitResumesAfterActualProcessExit": "real-process-restart",
    "activeHoldsProtectEveryObjectSetMember": "object-lifecycle",
    "orphanComponentsCollectedAfterGrace": "object-lifecycle",
    "legacyWholeAgeV2ThroughV5RestoreCompatible": "legacy-whole-age",
    "realMinioObjectSetSelectiveRestoreE2E": "real-minio",
    "realMinioColdRestoreUsesParallelRequiredOnlyPayloadGets": "real-minio",
    "realMinioWarmCacheUsesZeroPayloadGets": "real-minio",
    "realMinioCorruptCacheRefetchesOnlyCorruptPayload": "real-minio",
}

CRYPTO_SCENARIOS = frozenset({"real-process-restart", "real-minio"})
S3_SCENARIOS = frozenset({"real-minio"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    results: dict[str, dict[str, object]] = {}
    for name, node_ids in SCENARIOS.items():
        missing: list[str] = []
        if name in CRYPTO_SCENARIOS and backup_crypto.helper_path() is None:
            missing.append("real Age backup-crypto helper")
        if name in S3_SCENARIOS and not os.environ.get("DEEPSEEK_TEST_S3_ENDPOINT"):
            missing.append("DEEPSEEK_TEST_S3_ENDPOINT")
        if missing:
            results[name] = {
                "nodeIds": list(node_ids),
                "exitCode": 2,
                "error": f"required production prerequisite missing: {', '.join(missing)}",
            }
            continue
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
            "checks": checks,
            "checkProvenance": dict(CHECK_SCENARIOS),
            "scenarios": results,
        },
        root=ROOT,
    )
    output = args.out if args.out.is_absolute() else ROOT / args.out
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
