"""4.6.1 fail-closed index coverage and GC authority contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from deepseek_infra.infra.workspace import (
    backup_control,
    backup_object_index,
    backup_retirement,
    backup_targets,
    backup_tiering,
)


def test_rebuild_with_malformed_receipt_never_complete(tmp_settings: Path) -> None:
    tid = "target_idx_bad"
    root = tmp_settings / tid
    receipts = root / "receipts"
    receipts.mkdir(parents=True)
    good = {
        "policyId": "p",
        "backupId": "b1",
        "objects": [{"digest": "aa" * 32, "size": 3}],
    }
    (receipts / "b1.json").write_text(json.dumps(good), encoding="utf-8")
    (receipts / "bad.json").write_text("{not-json", encoding="utf-8")
    backup_targets.register_filesystem_target(tid, path=root)
    target = SimpleNamespace(target_id=tid, root=root, store=None)
    with patch(
        "deepseek_infra.infra.workspace.backup_retirement._receipt_has_valid_retirement_marker",
        return_value=False,
    ):
        result = backup_object_index.rebuild_index_from_target(target)
    assert result["coverageState"] == "incomplete"
    assert int(result.get("parseFailures") or 0) >= 1
    allowed, reason = backup_object_index.gc_allowed(tid)
    assert allowed is False
    assert "parse" in reason or "incomplete" in reason or "count" in reason


def test_unreadable_receipt_blocks_payload_gc(tmp_settings: Path) -> None:
    tid = "target_scan_bad"
    root = tmp_settings / tid
    receipts = root / "receipts"
    receipts.mkdir(parents=True)
    # Retiring point receipt is fine; sibling is corrupt.
    (receipts / "drop.json").write_text(
        json.dumps({"policyId": "p", "backupId": "drop", "objects": [{"path": "objects/sha256/aa/x.age"}]}),
        encoding="utf-8",
    )
    (receipts / "live.json").write_text("{broken", encoding="utf-8")
    backup_targets.register_filesystem_target(tid, path=root)
    target = SimpleNamespace(target_id=tid, root=root, store=None)
    with pytest.raises(backup_retirement.GcReferenceScanIndeterminate):
        backup_retirement._payload_key_is_retained(target, "objects/sha256/aa/x.age", retiring_backup_id="drop")


def test_stale_complete_index_never_authorizes_gc(tmp_settings: Path) -> None:
    tid = "target_stale"
    root = tmp_settings / tid
    root.mkdir(parents=True)
    backup_targets.register_filesystem_target(tid, path=root)
    # Manually mark complete without matching mutation generation.
    backup_control.set_target_index_coverage(
        tid,
        state="complete",
        formal_receipt_count=1,
        enumerated_receipts=1,
        parsed_receipts=1,
        indexed_receipts=1,
        parse_failures=0,
        read_failures=0,
        source_receipt_mutation_generation=0,
    )
    backup_control.put_recovery_object_ref(
        target_id=tid,
        policy_id="p",
        backup_id="b",
        object_key="objects/sha256/aa/x.age",
        ref_state="live",
        size_bytes=1,
        physical=True,
    )
    # Mutation after "complete" dirties / advances generation.
    gen = backup_control.bump_target_receipt_mutation(tid)
    assert gen >= 1
    allowed, reason = backup_object_index.gc_allowed(tid)
    assert allowed is False
    assert "stale" in reason or "incomplete" in reason or "formal-receipt" in reason


def test_fresh_complete_index_uses_sql_live_ref(tmp_settings: Path) -> None:
    tid = "target_fresh"
    root = tmp_settings / tid
    receipts = root / "receipts"
    receipts.mkdir(parents=True)
    (receipts / "keep.json").write_text(
        json.dumps(
            {
                "policyId": "p",
                "backupId": "keep",
                "objects": [{"digest": "bb" * 32, "size": 2, "path": "objects/sha256/bb/y.age"}],
            }
        ),
        encoding="utf-8",
    )
    backup_targets.register_filesystem_target(tid, path=root)
    target = SimpleNamespace(target_id=tid, root=root, store=None)
    with patch(
        "deepseek_infra.infra.workspace.backup_retirement._receipt_has_valid_retirement_marker",
        return_value=False,
    ):
        rebuilt = backup_object_index.rebuild_index_from_target(target)
    assert rebuilt["coverageState"] == "complete"
    allowed, reason = backup_object_index.gc_allowed(tid)
    assert allowed is True
    assert reason == "ok"
    assert backup_retirement._payload_key_is_retained(
        target, backup_object_index.canonical_object_key("bb" * 32), retiring_backup_id="drop"
    )


def test_note_formal_receipt_mutation_invalidates_coverage(tmp_settings: Path) -> None:
    tid = "target_mut"
    backup_control.set_target_index_coverage(
        tid,
        state="complete",
        formal_receipt_count=1,
        enumerated_receipts=1,
        parsed_receipts=1,
        indexed_receipts=1,
        source_receipt_mutation_generation=0,
    )
    assert backup_object_index.gc_allowed(tid)[0] is True or backup_control.get_target_receipt_mutation_generation(tid) == 0
    # Align mutation gen then complete
    gen0 = backup_control.get_target_receipt_mutation_generation(tid)
    backup_control.set_target_index_coverage(
        tid,
        state="complete",
        formal_receipt_count=1,
        enumerated_receipts=1,
        parsed_receipts=1,
        indexed_receipts=1,
        parse_failures=0,
        read_failures=0,
        source_receipt_mutation_generation=gen0,
    )
    assert backup_object_index.gc_allowed(tid)[0] is True
    backup_control.note_formal_receipt_mutation(tid)
    allowed, reason = backup_object_index.gc_allowed(tid)
    assert allowed is False
    cov = backup_control.get_target_index_coverage(tid)
    assert cov is not None
    assert cov["state"] == "incomplete"


def test_retirement_job_gc_reconciliation_on_bad_receipt(tmp_settings: Path) -> None:
    tid = "target_ret_recon"
    root = tmp_settings / tid
    root.mkdir(parents=True)
    backup_targets.register_filesystem_target(tid, path=root)
    policy_id = "p"
    backup_id = "drop"
    receipt = {
        "policyId": policy_id,
        "backupId": backup_id,
        "targetId": tid,
        "objectSetDigest": "d" * 64,
        "objects": [{"path": "objects/sha256/aa/x.age", "digest": "aa" * 32, "size": 1}],
    }
    commit = {
        "backupId": backup_id,
        "policyId": policy_id,
        "targetId": tid,
        "objectSetDigest": "d" * 64,
        "committedAt": "2026-01-01T00:00:00Z",
        "commitHash": "c" * 64,
    }
    job = backup_retirement.create_copy_retirement_job(policy_id, backup_id, tid)
    fake_target = SimpleNamespace(target_id=tid, root=root, store=None)
    with patch(
        "deepseek_infra.infra.workspace.backup_publish.resolve_target",
        return_value=fake_target,
    ), patch.object(
        backup_retirement,
        "_read_formal_metadata",
        return_value=(json.dumps(receipt).encode("utf-8"), receipt, commit),
    ), patch(
        "deepseek_infra.infra.workspace.backup_replication.simulate_copy_removal",
        return_value={"policySafe": True},
    ), patch.object(
        backup_retirement, "has_active_copy_dependency", return_value=False
    ), patch.object(
        backup_retirement, "_write_retirement_marker", return_value=None
    ), patch(
        "deepseek_infra.infra.workspace.backup_dr_ledger.list_logical_recovery_copies",
        return_value=[{"targetId": tid, "committedAt": "t"}],
    ), patch(
        "deepseek_infra.infra.workspace.backup_dr_ledger.record_logical_recovery_copy",
        return_value=None,
    ), patch.object(
        backup_retirement,
        "_payload_key_is_retained",
        side_effect=backup_retirement.GcReferenceScanIndeterminate("receipt-parse-failure:live.json"),
    ):
        result = backup_retirement.execute_copy_retirement_job(str(job["jobId"]))
    assert result["phase"] == "gc-reconciliation-required", result
    assert "gc-scan-indeterminate" in str(result.get("error") or "")


def test_migration_transfer_failure_never_converges(tmp_settings: Path) -> None:
    src = "target_mfail_s"
    dst = "target_mfail_d"
    backup_targets.register_filesystem_target(src, path=tmp_settings / src, region="r1", failure_domain="fd1")
    backup_targets.register_filesystem_target(dst, path=tmp_settings / dst, region="r2", failure_domain="fd2")
    backup_targets.set_target_storage_tier(dst, storage_tier="warm")
    job = backup_control.create_chain_migration_job(
        {
            "policyId": "pol",
            "anchorBackupId": "I1",
            "desiredTier": "warm",
            "destTargetId": dst,
            "phase": "planned",
            "unit": {"memberBackupIds": ["I1"], "closureComplete": True},
            "members": [
                {"backupId": "I1", "sourceTargetId": src, "destTargetId": dst, "state": "planned", "noop": False}
            ],
        }
    )
    with patch.object(
        backup_tiering,
        "execute_tier_migration",
        return_value={"status": "failed", "error": "boom", "intentPhase": "failed-terminal"},
    ):
        result = backup_tiering.execute_chain_migration(str(job["migrationId"]))
    assert result["phase"] == "failed-terminal"
    assert result.get("error")


def test_schema_version_is_at_least_5(tmp_settings: Path) -> None:
    assert backup_control.schema_version() >= 5
