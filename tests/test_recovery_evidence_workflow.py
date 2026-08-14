from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _job(workflow: str, name: str, next_name: str) -> str:
    start = workflow.index(f"  {name}:\n")
    end = workflow.index(f"  {next_name}:\n", start)
    return workflow[start:end]


def test_object_set_recovery_evidence_has_an_independent_real_service_job() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    packed = _job(workflow, "packed-delta-s3-e2e", "object-set-s3-e2e")
    object_set = _job(workflow, "object-set-s3-e2e", "recovery-fault-evidence")

    assert "run_object_set_s3_e2e.py" not in packed
    for required in (
        "run_object_set_s3_e2e.py",
        "scripts/build_backup_crypto.py",
        "quay.io/minio/minio:RELEASE.",
        "@sha256:",
        "--producer object-set-s3-e2e",
        "evidence-producer-object-set-s3-e2e",
    ):
        assert required in object_set
    assert workflow.count("      - object-set-s3-e2e\n") == 2
    assert "RC_CI_OBJECT_SET_S3_E2E" in workflow
    assert 'test "$RC_CI_OBJECT_SET_S3_E2E" = "success"' in workflow


def test_recovery_fault_evidence_has_an_independent_real_process_job() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    recovery_faults = _job(workflow, "recovery-fault-evidence", "eval")

    for required in (
        "run_recovery_fault_evidence.py",
        "scripts/build_backup_crypto.py",
        "dtolnay/rust-toolchain@1.85.0",
        "--producer recovery-fault-evidence",
        "evidence-producer-recovery-fault-evidence",
    ):
        assert required in recovery_faults
    assert workflow.count("      - recovery-fault-evidence\n") == 2
    assert "RC_CI_RECOVERY_FAULT_EVIDENCE" in workflow
    assert 'test "$RC_CI_RECOVERY_FAULT_EVIDENCE" = "success"' in workflow
