"""Fail-closed bootstrap, explicit mode, formal-truth attestation, evidence proof."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_authority_provider,
    backup_control,
    backup_control_authority,
    backup_control_recovery,
    evidence_proof,
)


@pytest.fixture
def control_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "control"
    root.mkdir()
    db = root / "control.sqlite3"
    monkeypatch.setattr(backup_control, "CONTROL_DIR", root)
    monkeypatch.setattr(backup_control, "CONTROL_DB", db)
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    backup_authority_provider.reset_authority_replica_provider()
    backup_control_recovery.clear_formal_truth_attestations()
    # Explicit replicated for these fail-closed tests (override conftest local-only).
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MODE, "replicated")
    return db


def test_replicated_zero_replicas_requires_configuration(control_db: Path) -> None:
    control_db.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(control_db) + suffix).unlink(missing_ok=True)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.STATE_AUTHORITY_CONFIGURATION_REQUIRED
    assert verdict["allowWorkers"] is False


def test_local_only_mode_allows_genesis(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MODE, "local-only")
    backup_authority_provider.reset_authority_replica_provider()
    control_db.unlink(missing_ok=True)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.RECOVERY_ACTIVE
    assert verdict["reason"] == "genesis-local-only"


def test_bootstrap_exception_fails_closed(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_authority_provider.reset_authority_replica_provider()
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)

    def boom(**_k: Any) -> Any:
        raise RuntimeError("unexpected-bootstrap-bug")

    monkeypatch.setattr(backup_authority_provider, "install_provider_from_bootstrap", boom)
    control_db.unlink(missing_ok=True)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.STATE_AUTHORITY_BOOTSTRAP_FAILED
    assert verdict["allowWorkers"] is False
    assert "unexpected" in str(verdict.get("reason") or "").casefold()


def test_coverage_alone_cannot_activate(control_db: Path) -> None:
    # local-only for DB creation path via monkeypatch
    import os

    os.environ[backup_authority_provider.ENV_AUTHORITY_MODE] = "local-only"
    backup_control.create_policy({"policyId": "p", "policyRevision": 1, "enabled": True})
    backup_control.upsert_target({"targetId": "t1", "kind": "filesystem", "root": "/tmp"})
    backup_control.set_target_index_coverage(
        "t1",
        state="complete",
        formal_receipt_count=1,
        source_receipt_mutation_generation=0,
        reason="forged",
    )
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH,
        reason="unit",
    )
    with pytest.raises(AppError, match="attestation-missing"):
        backup_control_recovery.activate_control_after_formal_truth()


def test_attestation_then_activate(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MODE, "local-only")
    backup_control.create_policy({"policyId": "p", "policyRevision": 1, "enabled": True})
    backup_control.upsert_target({"targetId": "t1", "kind": "filesystem", "root": "/tmp"})
    backup_control.set_target_index_coverage(
        "t1",
        state="complete",
        formal_receipt_count=1,
        source_receipt_mutation_generation=0,
        reason="unit",
    )
    backup_control_recovery.record_formal_truth_validation(
        target_id="t1",
        status="VALID",
        authenticated_commit_count=1,
        index_coverage_complete=True,
        lineage_valid=True,
        retirement_reconciled=True,
    )
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH,
        reason="unit",
    )
    out = backup_control_recovery.activate_control_after_formal_truth()
    assert out["status"] == "active"


def test_bootstrap_apperror_fails_closed(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_authority_provider.reset_authority_replica_provider()
    control_db.unlink(missing_ok=True)

    def boom(**_k: Any) -> Any:
        raise AppError("parse-failed", code=ErrorCode.INVALID_REQUEST, status=400)

    monkeypatch.setattr(backup_authority_provider, "install_provider_from_bootstrap", boom)
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.STATE_AUTHORITY_BOOTSTRAP_FAILED


def test_authority_verify_reads_replicas(control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MODE, "local-only")
    root = tmp_path / "auth"
    root.mkdir()
    a = backup_control_authority.build_authority_checkpoint(
        generation=1,
        previous_digest=None,
        policies=[],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=8,
    )
    backup_control_authority.write_authority_checkpoint_bundle(root, a)
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.schema_version()
    verify = backup_control_recovery.authority_verify()
    assert "replicas" in verify
    assert verify.get("overall") in {
        "HEALTHY",
        "DEGRADED",
        "UNAVAILABLE",
        "DIVERGENT",
        "DURABILITY_UNSATISFIED",
    }
    audit = backup_control_recovery.authority_audit_once()
    assert audit.get("destructiveRepair") is False


def test_evidence_proof_invalid_and_resolve_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"schema":"nope","checks":{}}', encoding="utf-8")
    with pytest.raises(ValueError, match="schema-mismatch"):
        evidence_proof.load_evidence_proof(bad)
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must-be-object"):
        evidence_proof.load_evidence_proof(bad)
    bad.write_text('{"schema":"evidence-proof-v1"}', encoding="utf-8")
    with pytest.raises(ValueError, match="checks-required"):
        evidence_proof.load_evidence_proof(bad)
    monkeypatch.setenv(evidence_proof.ENV_EVIDENCE_PROOF_PATH, str(tmp_path / "p.json"))
    assert evidence_proof.resolve_proof_path() == tmp_path / "p.json"
    monkeypatch.delenv(evidence_proof.ENV_EVIDENCE_PROOF_PATH, raising=False)
    assert evidence_proof.resolve_proof_path() is None
    # Convention path under artifacts/
    art = Path("artifacts")
    art.mkdir(exist_ok=True)
    conv = art / "evidence-proof-scen.json"
    conv.write_text(
        json.dumps({"schema": evidence_proof.EVIDENCE_PROOF_SCHEMA, "scenario": "scen", "checks": {}}),
        encoding="utf-8",
    )
    try:
        assert evidence_proof.resolve_proof_path(scenario="scen") == conv
    finally:
        conv.unlink(missing_ok=True)


def test_verdict_local_healthy_active_and_pending(
    control_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MODE, "local-only")
    backup_authority_provider.reset_authority_replica_provider()
    # Create healthy DB via schema
    backup_control.schema_version()
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.RECOVERY_ACTIVE, reason="ok"
    )
    verdict = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict["verdict"] == backup_control_recovery.RECOVERY_ACTIVE
    assert verdict["allowWorkers"] is True
    assert backup_control_recovery.workers_allowed_by_verdict(verdict) is True

    # Non-active recovery state blocks workers
    backup_control_recovery.enter_control_recovery_required(reason="unit")
    verdict2 = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict2["allowWorkers"] is False

    # Pending outbox with anchors
    root = tmp_path / "a"
    root.mkdir()
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.RECOVERY_ACTIVE, reason="ok"
    )
    backup_control_authority.configure_authority_anchor_roots([root])
    ckpt = backup_control_authority.snapshot_authority_from_control_db()
    backup_control_authority._enqueue_authority_outbox(kind="p", checkpoint=ckpt)

    def _ready() -> dict[str, Any]:
        return {"pending": 1, "status": "pending"}

    monkeypatch.setattr(backup_control, "ensure_control_authority_ready", _ready)
    verdict3 = backup_control_recovery.resolve_startup_authority_verdict()
    assert verdict3["verdict"] == backup_control_recovery.RECOVERY_REQUIRED
    assert verdict3["reason"] == "pending-authority-outbox"


def test_authority_mode_from_bootstrap_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    boot = tmp_path / "b.json"
    boot.write_text(json.dumps({"controlAuthority": {"mode": "local-only", "replicas": []}}), encoding="utf-8")
    monkeypatch.delenv(backup_authority_provider.ENV_AUTHORITY_MODE, raising=False)
    assert (
        backup_authority_provider.authority_mode(env={}, bootstrap_path=boot)
        == backup_authority_provider.MODE_LOCAL_ONLY
    )
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert (
        backup_authority_provider.authority_mode(env={}, bootstrap_path=bad)
        == backup_authority_provider.MODE_REPLICATED
    )


def test_list_formal_truth_validations(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MODE, "local-only")
    backup_control_recovery.clear_formal_truth_attestations()
    backup_control_recovery.record_formal_truth_validation(
        target_id="t1", status="VALID", index_coverage_complete=True
    )
    items = backup_control_recovery.list_formal_truth_validations()
    assert len(items) == 1
    assert items[0]["targetId"] == "t1"
    assert backup_control_recovery.get_formal_truth_validation("missing") is None


def test_evidence_proof_merge_fail_paths(tmp_path: Path) -> None:
    assert evidence_proof.proof_check_status({"checks": {"c": "not-dict"}}, "c") == "FAIL"
    assert evidence_proof.proof_check_status({"checks": {}}, "missing") == "FAIL"
    # Non-zero exit fails all required
    merged = evidence_proof.merge_checks_from_proof(
        checks={"a": "PASS"},
        check_to_scenario={},
        scenario_results={"s": {"exitCode": 1}},
        required_proof_checks={"s": ("a",)},
    )
    assert merged["a"] == "FAIL"
    # Corrupt proof file after path is set
    bad = tmp_path / "c.json"
    bad.write_text("{", encoding="utf-8")
    merged2 = evidence_proof.merge_checks_from_proof(
        checks={"a": "PASS"},
        check_to_scenario={},
        scenario_results={"s": {"exitCode": 0, "proofPath": str(bad)}},
        required_proof_checks={"s": ("a",)},
    )
    assert merged2["a"] == "FAIL"
    assert evidence_proof.resolve_proof_path(env={}, scenario="nope") is None


def test_evidence_proof_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "proof.json"
    evidence_proof.write_evidence_proof(
        path,
        scenario="demo",
        checks={
            "realPreDisasterBackupIsActuallyRestored": {
                "status": "PASS",
                "evidence": {"beforeSha256": "a", "afterSha256": "a"},
            }
        },
    )
    loaded = evidence_proof.load_evidence_proof(path)
    assert loaded["schema"] == evidence_proof.EVIDENCE_PROOF_SCHEMA
    assert (
        evidence_proof.proof_check_status(loaded, "realPreDisasterBackupIsActuallyRestored")
        == "PASS"
    )
    merged = evidence_proof.merge_checks_from_proof(
        checks={"realPreDisasterBackupIsActuallyRestored": "PASS", "other": "PASS"},
        check_to_scenario={},
        scenario_results={
            "demo": {"exitCode": 0, "proofPath": str(path)},
        },
        required_proof_checks={"demo": ("realPreDisasterBackupIsActuallyRestored",)},
    )
    assert merged["realPreDisasterBackupIsActuallyRestored"] == "PASS"
    # Missing proof fails required checks
    merged2 = evidence_proof.merge_checks_from_proof(
        checks={"realPreDisasterBackupIsActuallyRestored": "PASS"},
        check_to_scenario={},
        scenario_results={"demo": {"exitCode": 0, "proofPath": None}},
        required_proof_checks={"demo": ("realPreDisasterBackupIsActuallyRestored",)},
    )
    assert merged2["realPreDisasterBackupIsActuallyRestored"] == "FAIL"
