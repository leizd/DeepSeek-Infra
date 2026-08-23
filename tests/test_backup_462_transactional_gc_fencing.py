"""Transactional Metadata Fencing & Two-Phase Ciphertext GC (release patch)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_object_index,
    backup_retirement,
)
from deepseek_infra.infra.workspace.backup_target_store import object_key


@pytest.fixture
def control_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "control"
    root.mkdir()
    db = root / "control.sqlite3"
    monkeypatch.setattr(backup_control, "CONTROL_DIR", root)
    monkeypatch.setattr(backup_control, "CONTROL_DB", db)
    return db


def test_schema_v6_and_migrations(control_db: Path) -> None:
    assert backup_control.CONTROL_SCHEMA_VERSION == 6
    assert backup_control.schema_version() == 6
    migrations = backup_control.list_schema_migrations()
    assert migrations[-1]["version"] == 6
    assert "metadata-fences" in migrations[-1]["description"]


def test_formal_receipt_mutation_fail_closed_requires_target(control_db: Path) -> None:
    with pytest.raises(AppError) as exc:
        backup_control.note_formal_receipt_mutation(None)
    assert "blocked-control-authority" in str(exc.value)


def test_formal_metadata_mutation_dirties_coverage_and_bumps_generation(control_db: Path) -> None:
    backup_control.set_target_index_coverage(
        "t1",
        state="complete",
        formal_receipt_count=1,
        enumerated_receipts=1,
        parsed_receipts=1,
        indexed_receipts=1,
        source_receipt_mutation_generation=0,
    )
    assert backup_control.get_target_receipt_mutation_generation("t1") == 0
    with backup_control.begin_formal_metadata_mutation("t1", operation_id="op-1", kind="test"):
        gen = backup_control.get_target_receipt_mutation_generation("t1")
        cov = backup_control.get_target_index_coverage("t1")
        assert gen == 1
        assert cov is not None
        assert cov["state"] == "incomplete"
        assert cov["reason"] == "formal-receipt-mutation"
    # Fence released after exit.
    with backup_control.begin_formal_metadata_mutation("t1", operation_id="op-2", kind="test"):
        assert backup_control.get_target_receipt_mutation_generation("t1") == 2


def test_formal_and_destructive_gates_are_mutually_exclusive(control_db: Path) -> None:
    with backup_control.begin_formal_metadata_mutation("t1", operation_id="pub-1"):
        with pytest.raises(AppError) as exc:
            with backup_control.begin_destructive_metadata_fence("t1", operation_id="gc-1"):
                pass
        assert "blocked-metadata-gate" in str(exc.value)


def test_index_rebuild_never_exposes_empty_complete_generation(
    control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target_root = tmp_path / "target"
    receipts = target_root / "receipts"
    receipts.mkdir(parents=True)
    receipt = {
        "backupId": "b1",
        "policyId": "p1",
        "objects": [{"digest": "a" * 64, "size": 10}],
    }
    (receipts / "b1.json").write_text(json.dumps(receipt), encoding="utf-8")

    class _T:
        target_id = "t-rebuild"
        root = target_root
        store = None

    # Seed complete coverage + live ref, then rebuild must invalidate before clear.
    backup_control.set_target_index_coverage(
        "t-rebuild",
        state="complete",
        formal_receipt_count=1,
        enumerated_receipts=1,
        parsed_receipts=1,
        indexed_receipts=1,
        source_receipt_mutation_generation=0,
    )
    backup_object_index.index_receipt_objects(
        target_id="t-rebuild",
        policy_id="p1",
        backup_id="b1",
        receipt=receipt,
        ref_state="live",
    )

    observed: list[str] = []
    real_begin = backup_control.begin_index_rebuild_clear

    def _wrap(tid: str, **kwargs: Any) -> int:
        pinned = real_begin(tid, **kwargs)
        cov = backup_control.get_target_index_coverage(tid)
        assert cov is not None
        observed.append(str(cov["state"]))
        assert cov["state"] == "building"
        # Index rows already cleared in same transaction — GC must not see complete.
        allowed, reason = backup_control.index_coverage_allows_gc(tid)
        assert not allowed
        observed.append(reason)
        return pinned

    monkeypatch.setattr(backup_control, "begin_index_rebuild_clear", _wrap)
    monkeypatch.setattr(
        backup_retirement,
        "_receipt_has_valid_retirement_marker",
        lambda *a, **k: False,
    )
    result = backup_object_index.rebuild_index_from_target(_T())
    assert result["coverageState"] == "complete"
    assert observed[0] == "building"
    assert backup_control.object_has_live_ref("t-rebuild", object_key("a" * 64))


def test_gc_intent_binds_generation_and_cancels_on_mutation(control_db: Path, tmp_path: Path) -> None:
    target_root = tmp_path / "tgt"
    obj_key = object_key("b" * 64)
    path = target_root.joinpath(*obj_key.split("/"))
    path.parent.mkdir(parents=True)
    path.write_bytes(b"ciphertext")

    gen0 = backup_control.get_target_receipt_mutation_generation("t-gc")
    intent = backup_control.create_ciphertext_gc_intent(
        target_id="t-gc",
        object_key=obj_key,
        expected_receipt_mutation_generation=gen0,
        expected_etag=None,
        size_bytes=10,
        owner_id="job-1",
    )
    assert intent["state"] == backup_control.GC_INTENT_VALIDATED

    # Concurrent formal mutation advances generation → sweep must cancel.
    with backup_control.begin_formal_metadata_mutation("t-gc", operation_id="pub"):
        pass
    live_gen = backup_control.get_target_receipt_mutation_generation("t-gc")
    assert live_gen != gen0
    backup_control.update_ciphertext_gc_intent(
        intent["intentId"],
        state=backup_control.GC_INTENT_CANCELLED,
        error="receipt-mutation-generation-changed",
    )
    updated = backup_control.get_ciphertext_gc_intent(intent["intentId"])
    assert updated is not None
    assert updated["state"] == "cancelled"
    assert path.is_file()


def test_two_phase_gc_reclaims_unreferenced_only(control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target_root = tmp_path / "tgt2"
    dig_keep = "c" * 64
    dig_drop = "d" * 64
    key_keep = object_key(dig_keep)
    key_drop = object_key(dig_drop)
    for key, body in ((key_keep, b"keep"), (key_drop, b"drop")):
        p = target_root.joinpath(*key.split("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)

    receipts = target_root / "receipts"
    receipts.mkdir(parents=True)
    retiring = {
        "backupId": "old",
        "policyId": "p1",
        "objects": [
            {"digest": dig_keep, "size": 4},
            {"digest": dig_drop, "size": 4},
        ],
    }
    live = {
        "backupId": "new",
        "policyId": "p1",
        "objects": [{"digest": dig_keep, "size": 4}],
    }
    (receipts / "old.json").write_text(json.dumps(retiring), encoding="utf-8")
    (receipts / "new.json").write_text(json.dumps(live), encoding="utf-8")

    class _T:
        target_id = "t-gc2"
        root = target_root
        store = None

    monkeypatch.setattr(
        backup_retirement,
        "_receipt_has_valid_retirement_marker",
        lambda target, raw, receipt: str(receipt.get("backupId")) == "old",
    )
    reclaimed = backup_retirement._reclaim_unreferenced_payloads(
        _T(),
        receipt=retiring,
        retiring_backup_id="old",
        owner_id="job-gc2",
    )
    assert reclaimed >= 4
    assert target_root.joinpath(*key_keep.split("/")).is_file()
    assert not target_root.joinpath(*key_drop.split("/")).is_file()
    intents = backup_control.list_ciphertext_gc_intents("t-gc2")
    assert any(i["state"] == "reclaimed" for i in intents)


def test_control_authority_failure_blocks_formal_mutation(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Any:
        raise AppError("storage control authority failed integrity check: bad", code=ErrorCode.INTERNAL, status=500)

    monkeypatch.setattr(backup_control, "_connect", _boom)
    with pytest.raises(AppError):
        with backup_control.begin_formal_metadata_mutation("t1", operation_id="x"):
            pass


def test_gc_delete_cas_mismatch_never_marks_reclaimed(control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target_root = tmp_path / "tgt3"
    dig = "e" * 64
    key = object_key(dig)
    path = target_root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True)
    path.write_bytes(b"payload")

    class _T:
        target_id = "t-cas"
        root = target_root
        store = None

    receipt = {"backupId": "gone", "policyId": "p1", "objects": [{"digest": dig, "size": 7}]}
    (target_root / "receipts").mkdir(parents=True)
    (target_root / "receipts" / "gone.json").write_text(json.dumps(receipt), encoding="utf-8")

    monkeypatch.setattr(
        backup_retirement,
        "_payload_key_is_retained",
        lambda *a, **k: False,
    )

    def _fail_cas(target: Any, object_key_arg: str, *, expected_etag: str | None) -> int:
        raise AppError(
            f"retirement-payload-delete-cas-mismatch:{object_key_arg}",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )

    monkeypatch.setattr(backup_retirement, "_delete_payload_if_match", _fail_cas)
    with pytest.raises(AppError) as exc:
        backup_retirement._reclaim_unreferenced_payloads(
            _T(),
            receipt=receipt,
            retiring_backup_id="gone",
            owner_id="job-cas",
        )
    assert "cas-mismatch" in str(exc.value)
    assert path.is_file()
    intents = backup_control.list_ciphertext_gc_intents("t-cas")
    assert intents
    assert all(i["state"] != "reclaimed" for i in intents)
