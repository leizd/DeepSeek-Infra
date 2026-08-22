"""Fail-closed index coverage and GC authority contracts."""

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


def test_retirement_reclaim_with_clean_receipts(tmp_settings: Path) -> None:
    """Full reclaim path when receipt truth is determinate."""
    tid = "target_reclaim_ok"
    root = tmp_settings / tid
    receipts = root / "receipts"
    receipts.mkdir(parents=True)
    payload = root / "objects" / "sha256" / "aa"
    payload.mkdir(parents=True)
    blob = payload / "x.age"
    blob.write_bytes(b"xx")
    policy_id = "p"
    backup_id = "drop"
    receipt = {
        "policyId": policy_id,
        "backupId": backup_id,
        "targetId": tid,
        "objectSetDigest": "d" * 64,
        "objects": [{"digest": "aa" * 32, "size": 2, "path": "objects/sha256/aa/x.age"}],
    }
    commit = {
        "backupId": backup_id,
        "policyId": policy_id,
        "targetId": tid,
        "objectSetDigest": "d" * 64,
        "committedAt": "2026-01-01T00:00:00Z",
        "commitHash": "c" * 64,
    }
    (receipts / f"{backup_id}.json").write_text(json.dumps(receipt), encoding="utf-8")
    backup_targets.register_filesystem_target(tid, path=root)
    fake = SimpleNamespace(target_id=tid, root=root, store=None)
    job = backup_retirement.create_copy_retirement_job(policy_id, backup_id, tid)
    with patch(
        "deepseek_infra.infra.workspace.backup_publish.resolve_target", return_value=fake
    ), patch.object(
        backup_retirement,
        "_read_formal_metadata",
        return_value=(json.dumps(receipt).encode(), receipt, commit),
    ), patch(
        "deepseek_infra.infra.workspace.backup_replication.simulate_copy_removal",
        return_value={"policySafe": True, "protectedByHold": False},
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
        backup_retirement, "_payload_key_is_retained", return_value=False
    ):
        result = backup_retirement.execute_copy_retirement_job(str(job["jobId"]))
    assert result["phase"] == "reclaimed", result
    assert not blob.is_file()


def test_apply_retirement_indexes_missing_refs(tmp_settings: Path) -> None:
    tid = "target_apply_ret"
    receipt = {
        "policyId": "p",
        "backupId": "b",
        "objects": [{"digest": "11" * 32, "size": 4, "path": "objects/sha256/11/q.age"}],
    }
    # No prior refs → apply_retirement indexes then retires.
    changed = backup_object_index.apply_retirement_to_index(
        target_id=tid, policy_id="p", backup_id="b", receipt=receipt
    )
    assert changed >= 1
    # Second apply is idempotent (already retired)
    changed2 = backup_object_index.apply_retirement_to_index(
        target_id=tid, policy_id="p", backup_id="b", receipt=receipt
    )
    assert changed2 == 0


def test_reconcile_inventory_and_store_retained_scan(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore

    tid = "target_inv"
    store = MemoryTargetStore()
    store.put_if_absent("objects/sha256/ff/a.age", b"abc")
    store.put_if_absent("receipts/r.json", b"{}")
    target = SimpleNamespace(target_id=tid, root=None, store=store)
    page = backup_object_index.reconcile_inventory_page(target, prefix="objects/", limit=50)
    assert page["examined"] >= 1
    assert isinstance(page["orphans"], list)
    # no store
    empty = backup_object_index.reconcile_inventory_page(
        SimpleNamespace(target_id=tid, root=None, store=None)
    )
    assert empty["examined"] == 0

    # Store retained scan success + fail-closed
    good = {
        "backupId": "keep",
        "policyId": "p",
        "objects": [{"digest": "aa" * 32}],
    }
    store2 = MemoryTargetStore()
    store2.put_if_absent("receipts/keep.json", json.dumps(good).encode())
    store2.put_if_absent("receipts/drop.json", json.dumps({"backupId": "drop", "policyId": "p"}).encode())
    rt = SimpleNamespace(root=None, store=store2, target_id="t-ret-store")
    keys = backup_retirement._retained_payload_keys(rt, retiring_backup_id="drop")
    assert any("aa" * 32 in k or "sha256" in k for k in keys)
    store2.put_if_absent("receipts/broken.json", b"{")
    with pytest.raises(backup_retirement.GcReferenceScanIndeterminate):
        backup_retirement._retained_payload_keys(rt, retiring_backup_id="drop")


def test_control_schema_v5_helpers_are_exercised(tmp_settings: Path) -> None:
    """Dense hits for schema v5 coverage evidence and mutation APIs."""
    tid = "target_v5_dense"
    assert backup_control.get_target_receipt_mutation_generation(tid) == 0
    assert backup_control.get_target_index_coverage(tid) is None
    # Dirty via note
    assert backup_control.note_formal_receipt_mutation(tid) == 1
    cov = backup_control.get_target_index_coverage(tid)
    assert cov is not None
    assert cov["state"] == "incomplete"
    assert backup_control.get_target_receipt_mutation_generation(tid) == 1
    # Complete claim with auto-fill defaults at current mutation gen
    backup_control.set_target_index_coverage(tid, state="complete", formal_receipt_count=4)
    allowed, reason = backup_control.index_coverage_allows_gc(tid)
    assert allowed is True, reason
    # Stale after next mutation
    backup_control.note_formal_receipt_mutation(tid)
    assert backup_control.index_coverage_allows_gc(tid)[0] is False
    # Explicit incomplete reason
    backup_control.set_target_index_coverage(
        tid, state="building", reason="rebuild-started", formal_receipt_count=0
    )
    assert backup_control.index_coverage_allows_gc(tid)[0] is False
    backup_control.set_target_index_coverage(
        tid, state="scanning", reason="rebuild-scanning", formal_receipt_count=0
    )
    assert "scanning" in str(backup_control.get_target_index_coverage(tid) or {})
    # Empty target_id note
    assert backup_control.note_formal_receipt_mutation("") is None
    # put_recovery_object_ref + gc candidates blocked when incomplete
    backup_control.put_recovery_object_ref(
        target_id=tid,
        policy_id="p",
        backup_id="b",
        object_key="objects/sha256/ee/z.age",
        ref_state="retired",
        size_bytes=1,
        physical=True,
    )
    assert backup_object_index.gc_candidate_keys(tid) == []
    # Fresh complete unlocks candidates
    gen = backup_control.get_target_receipt_mutation_generation(tid)
    backup_control.set_target_index_coverage(
        tid,
        state="complete",
        formal_receipt_count=1,
        enumerated_receipts=1,
        parsed_receipts=1,
        indexed_receipts=1,
        parse_failures=0,
        read_failures=0,
        source_receipt_mutation_generation=gen,
    )
    assert "objects/sha256/ee/z.age" in backup_object_index.gc_candidate_keys(tid)
    # retained_payload_keys_from_index signals authoritative mode
    assert backup_object_index.retained_payload_keys_from_index(tid, retiring_backup_id="x") is not None


def test_rebuild_from_store_with_bad_and_good_receipts(tmp_settings: Path) -> None:
    from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore

    tid = "target_store_rebuild"
    store = MemoryTargetStore()
    good = json.dumps({"policyId": "p", "backupId": "g1", "objects": [{"digest": "dd" * 32, "size": 1}]}).encode()
    store.put_if_absent("receipts/g1.json", good)
    store.put_if_absent("receipts/bad.json", b"{nope")
    target = SimpleNamespace(target_id=tid, root=None, store=store)
    with patch(
        "deepseek_infra.infra.workspace.backup_retirement._receipt_has_valid_retirement_marker",
        return_value=False,
    ):
        result = backup_object_index.rebuild_index_from_target(target)
    assert result["coverageState"] == "incomplete"
    assert int(result.get("parseFailures") or 0) >= 1
    assert backup_object_index.gc_allowed(tid)[0] is False


def test_coverage_reject_reasons_are_explicit(tmp_settings: Path) -> None:
    tid = "target_reasons"
    assert backup_control.index_coverage_allows_gc(tid) == (False, "object-reference-index-missing")
    backup_control.set_target_index_coverage(
        tid,
        state="complete",
        formal_receipt_count=2,
        enumerated_receipts=2,
        parsed_receipts=2,
        indexed_receipts=1,
        parse_failures=0,
        read_failures=0,
        source_receipt_mutation_generation=0,
    )
    allowed, reason = backup_control.index_coverage_allows_gc(tid)
    assert allowed is False
    assert "count-mismatch" in reason
    backup_control.set_target_index_coverage(
        tid,
        state="complete",
        formal_receipt_count=1,
        enumerated_receipts=1,
        parsed_receipts=1,
        indexed_receipts=1,
        parse_failures=1,
        read_failures=0,
        source_receipt_mutation_generation=0,
    )
    assert "parse-failures" in backup_control.index_coverage_allows_gc(tid)[1]
    backup_control.set_target_index_coverage(
        tid,
        state="complete",
        formal_receipt_count=1,
        enumerated_receipts=1,
        parsed_receipts=1,
        indexed_receipts=1,
        parse_failures=0,
        read_failures=1,
        source_receipt_mutation_generation=0,
    )
    assert "read-failures" in backup_control.index_coverage_allows_gc(tid)[1]


def test_mutation_and_coverage_helpers_cover_edges(tmp_settings: Path) -> None:
    assert backup_control.note_formal_receipt_mutation(None) is None
    assert backup_control.note_formal_receipt_mutation("  ") is None
    tid = "target_edges"
    # First bump creates incomplete coverage row
    g1 = backup_control.bump_target_receipt_mutation(tid)
    assert g1 == 1
    cov = backup_control.get_target_index_coverage(tid)
    assert cov is not None and cov["state"] == "incomplete"
    g2 = backup_control.bump_target_receipt_mutation(tid)
    assert g2 == 2
    # Incomplete reason path
    backup_control.set_target_index_coverage(tid, state="incomplete", reason="manual-dirty")
    allowed, reason = backup_control.index_coverage_allows_gc(tid)
    assert allowed is False
    assert "manual-dirty" in reason or "incomplete" in reason
    # process_pending classifies reconciliation as waiting
    jobs = [
        {"jobId": "j1", "phase": "requested"},
        {"jobId": "j2", "phase": "requested"},
    ]
    with patch.object(backup_retirement, "list_copy_retirement_jobs", return_value=jobs), patch.object(
        backup_retirement,
        "execute_copy_retirement_job",
        side_effect=[{"phase": "gc-reconciliation-required"}, {"phase": "reclaimed"}],
    ):
        summary = backup_retirement.process_pending_retirements(limit=5)
    assert summary["waiting"] >= 1
    assert summary["reclaimed"] >= 1


def test_rebuild_read_failure_leaves_incomplete(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tid = "target_read_fail"
    root = tmp_settings / tid
    receipts = root / "receipts"
    receipts.mkdir(parents=True)
    good_path = receipts / "ok.json"
    good_path.write_text(
        json.dumps({"policyId": "p", "backupId": "ok", "objects": [{"digest": "cc" * 32, "size": 1}]}),
        encoding="utf-8",
    )
    bad_path = receipts / "badread.json"
    bad_path.write_text("{}", encoding="utf-8")
    backup_targets.register_filesystem_target(tid, path=root)
    target = SimpleNamespace(target_id=tid, root=root, store=None)
    real_read = Path.read_bytes

    def flaky_read(self: Path) -> bytes:
        if self.name == "badread.json":
            raise OSError("boom")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read)
    with patch(
        "deepseek_infra.infra.workspace.backup_retirement._receipt_has_valid_retirement_marker",
        return_value=False,
    ):
        result = backup_object_index.rebuild_index_from_target(target)
    assert result["coverageState"] == "incomplete"
    assert int(result.get("readFailures") or 0) >= 1
