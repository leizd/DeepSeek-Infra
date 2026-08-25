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


def test_authority_verify_divergent_and_durability(
    control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MODE, "local-only")
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MIN_DURABLE, "3")
    r1 = tmp_path / "r1"
    r2 = tmp_path / "r2"
    r1.mkdir()
    r2.mkdir()
    a = backup_control_authority.build_authority_checkpoint(
        generation=1,
        previous_digest=None,
        policies=[{"policyId": "p", "policyRevision": 1}],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=8,
    )
    b = backup_control_authority.build_authority_checkpoint(
        generation=1,
        previous_digest=None,
        policies=[{"policyId": "q", "policyRevision": 1}],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=8,
    )
    backup_control_authority.write_authority_checkpoint_bundle(r1, a)
    backup_control_authority.write_authority_checkpoint_bundle(r2, b)
    backup_control_authority.configure_authority_anchor_roots([r1, r2])
    backup_control.schema_version()
    verify = backup_control_recovery.authority_verify()
    # Valid local chains that fork at the same generation → overall DIVERGENT (Gate L).
    assert verify.get("overall") == "DIVERGENT"
    assert "cross-replica-divergent" in list(verify.get("issues") or [])
    replicas = list(verify.get("replicas") or [])
    assert len(replicas) >= 2
    assert all(bool(item.get("divergent")) for item in replicas)


def test_activate_rejects_invalid_attestation_counts(
    control_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        index_coverage_complete=True,
        invalid_commit_count=2,
        lineage_valid=True,
        retirement_reconciled=True,
    )
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH,
        reason="unit",
    )
    with pytest.raises(AppError, match="invalid-commits"):
        backup_control_recovery.activate_control_after_formal_truth()


def test_activate_rejects_lineage_invalid_and_stale_authority(
    control_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH,
        reason="unit",
    )
    backup_control_recovery.record_formal_truth_validation(
        target_id="t1",
        status="VALID",
        index_coverage_complete=True,
        invalid_commit_count=0,
        lineage_valid=False,
        retirement_reconciled=True,
    )
    with pytest.raises(AppError, match="lineage-invalid"):
        backup_control_recovery.activate_control_after_formal_truth()

    backup_control_recovery.record_formal_truth_validation(
        target_id="t1",
        status="VALID",
        index_coverage_complete=True,
        invalid_commit_count=0,
        lineage_valid=True,
        retirement_reconciled=True,
        authority_generation=999999,
    )
    with pytest.raises(AppError, match="stale-authority"):
        backup_control_recovery.activate_control_after_formal_truth()


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
    digest = "a" * 64
    other = "b" * 64
    restore_ev = {
        "backupId": "b1",
        "targetId": "t1",
        "restoreId": "r1",
        "preBackupWorkspaceDigest": digest,
        "corruptedWorkspaceDigest": other,
        "postRestoreWorkspaceDigest": digest,
        "restorePhase": "complete",
    }
    evidence_proof.write_evidence_proof(
        path,
        scenario="demo",
        checks={
            "realPreDisasterBackupIsActuallyRestored": {
                "status": "PASS",
                "evidence": restore_ev,
            }
        },
    )
    loaded = evidence_proof.load_evidence_proof(path)
    assert loaded["schema"] == evidence_proof.EVIDENCE_PROOF_SCHEMA
    assert (
        evidence_proof.proof_check_status(loaded, "realPreDisasterBackupIsActuallyRestored")
        == "PASS"
    )
    # Bare status=PASS without required fields must FAIL semantic validation.
    bare = {
        "schema": evidence_proof.EVIDENCE_PROOF_SCHEMA,
        "scenario": "demo",
        "checks": {
            "realPreDisasterBackupIsActuallyRestored": {
                "status": "PASS",
                "evidence": {"note": "not-enough"},
            }
        },
    }
    assert evidence_proof.proof_check_status(bare, "realPreDisasterBackupIsActuallyRestored") == "FAIL"
    assert evidence_proof.proof_check_status(
        {
            "checks": {
                "freshProcessAAndBHaveDifferentPids": {
                    "status": "PASS",
                    "evidence": {"pidA": 1, "pidB": 1},
                }
            }
        },
        "freshProcessAAndBHaveDifferentPids",
    ) == "FAIL"
    assert evidence_proof.proof_check_status(
        {
            "checks": {
                "processAExitedBySigkill": {"status": "PASS", "evidence": {"returncode": 0}},
            }
        },
        "processAExitedBySigkill",
    ) == "FAIL"
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


def _sha(n: int = 0xAA) -> str:
    return f"{n:02x}" * 32


def test_evidence_proof_v2_validators_cover_all_branches(tmp_path: Path) -> None:
    digest = _sha(0x11)
    other = _sha(0x22)
    third = _sha(0x33)
    # load scenario mismatch + invalid sha256 + restore relations
    path = tmp_path / "p.json"
    evidence_proof.write_evidence_proof(path, scenario="s1", checks={})
    with pytest.raises(ValueError, match="scenario-mismatch"):
        evidence_proof.load_evidence_proof(path, expected_scenario="other")
    assert evidence_proof.validate_restore_proof(
        {
            "backupId": "b",
            "targetId": "t",
            "restoreId": "r",
            "preBackupWorkspaceDigest": "not-a-digest",
            "corruptedWorkspaceDigest": other,
            "postRestoreWorkspaceDigest": digest,
        },
        "x",
    )
    assert "restore-digest-mismatch" in evidence_proof.validate_restore_proof(
        {
            "backupId": "b",
            "targetId": "t",
            "restoreId": "r",
            "preBackupWorkspaceDigest": digest,
            "corruptedWorkspaceDigest": other,
            "postRestoreWorkspaceDigest": third,
            "restorePhase": "complete",
        },
        "x",
    )
    assert "workspace-was-not-corrupted" in evidence_proof.validate_restore_proof(
        {
            "backupId": "b",
            "targetId": "t",
            "restoreId": "r",
            "preBackupWorkspaceDigest": digest,
            "corruptedWorkspaceDigest": digest,
            "postRestoreWorkspaceDigest": digest,
        },
        "x",
    )
    assert any(
        e.startswith("restore-phase-incomplete")
        for e in evidence_proof.validate_restore_proof(
            {
                "backupId": "b",
                "targetId": "t",
                "restoreId": "r",
                "preBackupWorkspaceDigest": digest,
                "corruptedWorkspaceDigest": other,
                "postRestoreWorkspaceDigest": digest,
                "restorePhase": "fetching",
            },
            "x",
        )
    )
    # backup commit binding
    assert evidence_proof.validate_backup_commit_proof(
        {
            "backupId": "b3",
            "commitKey": "commits/c.json",
            "receiptKey": "receipts/b3.json",
            "receiptDigest": digest,
            "objectSetDigest": other,
            "computedReceiptSha256": third,
        },
        "x",
    )
    assert not evidence_proof.validate_backup_commit_proof(
        {
            "backupId": "b3",
            "commitKey": "commits/c.json",
            "receiptKey": "receipts/b3.json",
            "receiptDigest": digest,
            "objectSetDigest": other,
            "computedReceiptSha256": digest,
        },
        "x",
    )
    # pids
    assert "invalid-pid-types" in evidence_proof.validate_distinct_pid_proof(
        {"pidA": "x", "pidB": "1"}, "x"
    )
    assert "non-positive-pid" in evidence_proof.validate_distinct_pid_proof(
        {"pidA": 0, "pidB": 2}, "x"
    )
    assert not evidence_proof.validate_distinct_pid_proof({"pidA": 1, "pidB": 2}, "x")
    # sigkill / epoch / endpoints
    assert "invalid-returncode" in evidence_proof.validate_sigkill_proof(
        {"returncode": "nope"}, "x"
    )
    assert not evidence_proof.validate_sigkill_proof({"returncode": -9}, "x")
    assert "boot-epoch-not-increased" in evidence_proof.validate_epoch_increase_proof(
        {"epochA": 3, "epochB": 3}, "x"
    )
    assert "invalid-epoch-types" in evidence_proof.validate_epoch_increase_proof(
        {"epochA": "a", "epochB": "b"}, "x"
    )
    assert not evidence_proof.validate_epoch_increase_proof({"epochA": 1, "epochB": 2}, "x")
    assert "need-three-endpoints" in evidence_proof.validate_minio_endpoints_proof(
        {"endpoints": ["a", "b"]}, "x"
    )
    assert "endpoints-not-distinct" in evidence_proof.validate_minio_endpoints_proof(
        {"endpoints": ["http://a", "http://a/", "http://a"]}, "x"
    )
    assert not evidence_proof.validate_minio_endpoints_proof(
        {"endpoints": ["http://a", "http://b", "http://c"]}, "x"
    )
    assert evidence_proof.validate_pass_with_schema_only({}, "x") == ["empty-evidence"]
    assert not evidence_proof.validate_pass_with_schema_only(
        {"schema": evidence_proof.EVIDENCE_PROOF_SCHEMA}, "x"
    )
    # validate_check edges
    assert evidence_proof.validate_check("any", {"status": "FAIL", "evidence": {}}) 
    assert evidence_proof.validate_check("any", {"status": "PASS", "evidence": "nope"})
    assert evidence_proof.validate_check("unknownCheck", {"status": "PASS", "evidence": {}})
    assert not evidence_proof.validate_check(
        "unknownCheck", {"status": "PASS", "evidence": {"ok": True}}
    )
    # non-semantic mode
    assert (
        evidence_proof.proof_check_status(
            {"checks": {"c": {"status": "PASS", "evidence": {}}}},
            "c",
            semantic=False,
        )
        == "PASS"
    )
    assert (
        evidence_proof.proof_check_status(
            {"checks": {"c": {"status": "FAIL"}}},
            "c",
            semantic=False,
        )
        == "FAIL"
    )
    # missing proof path with exit 0 → FAIL
    missing = evidence_proof.merge_checks_from_proof(
        checks={"a": "PASS"},
        check_to_scenario={},
        scenario_results={"s": {"exitCode": 0, "proofPath": None}},
        required_proof_checks={"s": ("a",)},
    )
    assert missing["a"] == "FAIL"
    # resolve via explicit env path
    env_path = tmp_path / "env-proof.json"
    evidence_proof.write_evidence_proof(env_path, scenario="env-scen", checks={})
    assert (
        evidence_proof.resolve_proof_path(
            env={evidence_proof.ENV_EVIDENCE_PROOF_PATH: str(env_path)}
        )
        == env_path
    )


def test_activate_rejects_coverage_retirement_and_stale_mutation(
    control_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    backup_control_recovery.set_control_recovery_state(
        recovery_state=backup_control_recovery.STATE_RECOVERING_FORMAL_TRUTH,
        reason="unit",
    )
    backup_control_recovery.record_formal_truth_validation(
        target_id="t1",
        status="VALID",
        index_coverage_complete=False,
        lineage_valid=True,
        retirement_reconciled=True,
    )
    with pytest.raises(AppError, match="coverage-incomplete"):
        backup_control_recovery.activate_control_after_formal_truth()

    backup_control_recovery.record_formal_truth_validation(
        target_id="t1",
        status="VALID",
        index_coverage_complete=True,
        lineage_valid=True,
        retirement_reconciled=False,
    )
    with pytest.raises(AppError, match="retirement-unreconciled"):
        backup_control_recovery.activate_control_after_formal_truth()

    backup_control_recovery.record_formal_truth_validation(
        target_id="t1",
        status="VALID",
        index_coverage_complete=True,
        lineage_valid=True,
        retirement_reconciled=True,
        source_receipt_mutation_generation=999,
    )
    with pytest.raises(AppError, match="stale-mutation"):
        backup_control_recovery.activate_control_after_formal_truth()

    # Missing mutation row is generation 0 (same as get_target_receipt_mutation_generation).
    backup_control_recovery.record_formal_truth_validation(
        target_id="t1",
        status="VALID",
        index_coverage_complete=True,
        lineage_valid=True,
        retirement_reconciled=True,
        source_receipt_mutation_generation=0,
    )
    out = backup_control_recovery.activate_control_after_formal_truth()
    assert out["status"] == "active"


def test_authority_verify_durability_unsatisfied(
    control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(backup_authority_provider.ENV_AUTHORITY_MODE, "local-only")
    r1 = tmp_path / "only"
    r1.mkdir()
    a = backup_control_authority.build_authority_checkpoint(
        generation=1,
        previous_digest=None,
        policies=[{"policyId": "p", "policyRevision": 1}],
        targets=[],
        receipt_mutation_generations={},
        promotion_epochs={},
        drain_generations={},
        placement_generations={},
        control_schema_version=8,
    )
    backup_control_authority.write_authority_checkpoint_bundle(r1, a)
    backup_control_authority.configure_authority_anchor_roots([r1])
    backup_control.schema_version()
    monkeypatch.setattr(
        backup_control_recovery,
        "authority_health_snapshot",
        lambda: {
            "configuredReplicaCount": 3,
            "resolvedReplicaCount": 1,
            "minDurableReplicas": 3,
            "formalTruth": {"incompleteTargets": 0},
            "unresolvedMutationCount": 0,
            "mode": "local-only",
        },
    )
    verify = backup_control_recovery.authority_verify()
    assert verify.get("overall") == "DURABILITY_UNSATISFIED"
    assert "durability-unsatisfied" in list(verify.get("issues") or [])
