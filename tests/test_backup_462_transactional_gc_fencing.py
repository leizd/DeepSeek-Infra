"""Transactional Metadata Fencing & Two-Phase Ciphertext GC (release patch)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_object_index,
    backup_retirement,
)
from deepseek_infra.infra.workspace.backup_target_store import object_key
from deepseek_infra.infra.workspace.backup_target_store import ListPage, MemoryTargetStore, ObjectMeta


@pytest.fixture
def control_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "control"
    root.mkdir()
    db = root / "control.sqlite3"
    monkeypatch.setattr(backup_control, "CONTROL_DIR", root)
    monkeypatch.setattr(backup_control, "CONTROL_DB", db)
    return db


def test_schema_v6_and_migrations(control_db: Path) -> None:
    assert backup_control.CONTROL_SCHEMA_VERSION >= 6
    assert backup_control.schema_version() == backup_control.CONTROL_SCHEMA_VERSION
    migrations = backup_control.list_schema_migrations()
    assert any(item["version"] == 6 and "metadata-fences" in item["description"] for item in migrations)


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


def test_gc_storage_helpers_cover_filesystem_store_and_missing(tmp_path: Path) -> None:
    digest = "f" * 64
    key = object_key(digest)

    filesystem_target = SimpleNamespace(root=tmp_path / "filesystem", store=None)
    assert backup_retirement._object_etag_and_size(filesystem_target, key) == (None, 0)
    path = filesystem_target.root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True)
    path.write_bytes(b"filesystem")
    filesystem_etag, filesystem_size = backup_retirement._object_etag_and_size(filesystem_target, key)
    assert filesystem_etag is not None
    assert filesystem_size == len(b"filesystem")
    with pytest.raises(AppError, match="cas-mismatch"):
        backup_retirement._delete_payload_if_match(filesystem_target, key, expected_etag="stale")
    assert backup_retirement._delete_payload_if_match(
        filesystem_target,
        key,
        expected_etag=filesystem_etag,
    ) == len(b"filesystem")
    assert backup_retirement._delete_payload_if_match(filesystem_target, key, expected_etag=filesystem_etag) == 0

    store = MemoryTargetStore()
    store.put_if_absent(key, b"remote")
    store_target = SimpleNamespace(root=None, store=store)
    store_etag, store_size = backup_retirement._object_etag_and_size(store_target, key)
    assert store_etag is not None
    assert store_size == len(b"remote")
    assert backup_retirement._delete_payload_if_match(store_target, key, expected_etag=store_etag) == len(b"remote")
    assert backup_retirement._delete_payload_if_match(store_target, key, expected_etag=store_etag) == 0

    rejecting_store = SimpleNamespace(
        stat=lambda _key: SimpleNamespace(etag="etag", size=7),
        delete_if_match=lambda _key, *, expected_etag: False,
    )
    with pytest.raises(AppError, match="cas-mismatch"):
        backup_retirement._delete_payload_if_match(
            SimpleNamespace(root=None, store=rejecting_store),
            key,
            expected_etag="etag",
        )

    empty_target = SimpleNamespace(root=None, store=None)
    assert backup_retirement._object_etag_and_size(empty_target, key) == (None, 0)
    assert backup_retirement._delete_payload_if_match(empty_target, key, expected_etag=None) == 0


def test_gc_intent_validation_filters_and_expired_gate_takeover(control_db: Path) -> None:
    with pytest.raises(AppError, match="target_id and object_key required"):
        backup_control.create_ciphertext_gc_intent(
            target_id="",
            object_key="",
            expected_receipt_mutation_generation=0,
        )
    with pytest.raises(AppError, match="target_id required for destructive fence"):
        with backup_control.begin_destructive_metadata_fence("", operation_id="gc"):
            pass
    with pytest.raises(AppError, match="target-id-required-for-formal-receipt"):
        with backup_control.begin_formal_metadata_mutation("", operation_id="publish"):
            pass
    with pytest.raises(AppError, match="target_id required for index rebuild"):
        backup_control.begin_index_rebuild_clear("")

    assert backup_control.get_ciphertext_gc_intent("missing") is None
    assert backup_control.update_ciphertext_gc_intent("missing", state=backup_control.GC_INTENT_CANCELLED) is None
    first = backup_control.create_ciphertext_gc_intent(
        target_id="t-query",
        object_key="objects/a",
        expected_receipt_mutation_generation=2,
        expected_etag="etag-a",
        size_bytes=-1,
        owner_id="owner-a",
        intent_id="intent-a",
    )
    second = backup_control.create_ciphertext_gc_intent(
        target_id="t-query",
        object_key="objects/b",
        expected_receipt_mutation_generation=2,
        intent_id="intent-b",
    )
    assert first["sizeBytes"] == 0
    assert first["expectedEtag"] == "etag-a"
    assert first["ownerId"] == "owner-a"
    assert second["expectedEtag"] is None
    cancelled = backup_control.update_ciphertext_gc_intent(
        first["intentId"],
        state=backup_control.GC_INTENT_CANCELLED,
        error="test-cancel",
    )
    assert cancelled is not None
    assert cancelled["error"] == "test-cancel"
    assert [item["intentId"] for item in backup_control.list_ciphertext_gc_intents(states=[backup_control.GC_INTENT_CANCELLED])] == [
        "intent-a"
    ]
    assert len(backup_control.list_ciphertext_gc_intents("t-query", limit=1)) == 1
    assert len(backup_control.list_ciphertext_gc_intents()) == 2

    backup_control.schema_version()
    with backup_control._connect() as conn:
        conn.execute(
            """
            INSERT INTO target_metadata_gates(target_id, owner_id, mode, fencing_token, lease_until, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("t-expired", "old-owner", backup_control.METADATA_GATE_FORMAL, 7, "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00Z"),
        )
    with backup_control.begin_destructive_metadata_fence("t-expired", operation_id="new-owner") as guard:
        assert guard["fencingToken"] == 8
        assert guard["ownerId"] == "new-owner"
        assert backup_control.metadata_gate_is_current(
            "t-expired",
            owner_id="new-owner",
            fencing_token=8,
            mode=backup_control.METADATA_GATE_DESTRUCTIVE,
        )
        renewed = backup_control.renew_target_metadata_gate(
            "t-expired",
            owner_id="new-owner",
            fencing_token=8,
            mode=backup_control.METADATA_GATE_DESTRUCTIVE,
        )
        assert renewed["fencingToken"] == 8
        assert renewed["leaseUntil"]

    with backup_control._connect() as conn:
        backup_control._begin_immediate(conn)
        first_token = backup_control._acquire_metadata_gate(
            conn,
            target_id="t-renew",
            owner_id="same-owner",
            mode=backup_control.METADATA_GATE_DESTRUCTIVE,
        )
        renewed_token = backup_control._acquire_metadata_gate(
            conn,
            target_id="t-renew",
            owner_id="same-owner",
            mode=backup_control.METADATA_GATE_DESTRUCTIVE,
        )
        backup_control._release_metadata_gate(
            conn,
            target_id="t-renew",
            owner_id="same-owner",
            fencing_token=renewed_token,
        )
        conn.execute("COMMIT")
    assert renewed_token == first_token


def test_expired_metadata_fence_never_deletes_after_takeover(
    control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale GC holder must not DELETE after lease expiry + token takeover."""
    from contextlib import contextmanager

    target_root = tmp_path / "tgt-fence-takeover"
    dig = "a1" + ("b" * 62)
    key = object_key(dig)
    path = target_root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True)
    path.write_bytes(b"must-survive-stale-gc")
    (target_root / "receipts").mkdir(parents=True)
    receipt = {"backupId": "gone", "policyId": "p1", "objects": [{"digest": dig, "size": 20}]}
    (target_root / "receipts" / "gone.json").write_text(json.dumps(receipt), encoding="utf-8")

    class _T:
        target_id = "t-fence-takeover"
        root = target_root
        store = None

    monkeypatch.setattr(backup_retirement, "_payload_key_is_retained", lambda *a, **k: False)

    real_begin = backup_control.begin_destructive_metadata_fence
    delete_calls: list[str] = []

    @contextmanager
    def _stale_after_takeover(target_id: str, **kwargs: Any):
        with real_begin(target_id, **kwargs) as guard:
            with backup_control._connect() as conn:
                conn.execute(
                    """
                    UPDATE target_metadata_gates
                    SET lease_until = ?, updated_at = ?
                    WHERE target_id = ?
                    """,
                    ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00Z", target_id),
                )
            with real_begin(target_id, operation_id="publisher-takeover") as newer:
                assert int(newer["fencingToken"]) > int(guard["fencingToken"])
            yield guard

    real_delete = backup_retirement._delete_payload_if_match

    def _track_delete(target: Any, object_key_arg: str, *, expected_etag: str | None) -> int:
        delete_calls.append(object_key_arg)
        return real_delete(target, object_key_arg, expected_etag=expected_etag)

    monkeypatch.setattr(backup_control, "begin_destructive_metadata_fence", _stale_after_takeover)
    monkeypatch.setattr(backup_retirement, "_delete_payload_if_match", _track_delete)

    reclaimed = backup_retirement._reclaim_unreferenced_payloads(
        _T(),
        receipt=receipt,
        retiring_backup_id="gone",
        owner_id="gc-stale",
    )
    assert reclaimed == 0
    assert delete_calls == []
    assert path.is_file()
    intents = backup_control.list_ciphertext_gc_intents("t-fence-takeover")
    assert intents
    assert all(i["state"] != "reclaimed" for i in intents)
    assert any(i.get("error") == "metadata-fence-lost" for i in intents)


def test_metadata_fence_renewal_failure_aborts_destructive_gc(
    control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renewal failure mid critical section must abort DELETE (leave garbage)."""
    target_root = tmp_path / "tgt-fence-renew"
    dig = "c2" + ("d" * 62)
    key = object_key(dig)
    path = target_root.joinpath(*key.split("/"))
    path.parent.mkdir(parents=True)
    path.write_bytes(b"must-survive-renew-fail")
    (target_root / "receipts").mkdir(parents=True)
    receipt = {"backupId": "old", "policyId": "p1", "objects": [{"digest": dig, "size": 22}]}
    (target_root / "receipts" / "old.json").write_text(json.dumps(receipt), encoding="utf-8")

    class _T:
        target_id = "t-fence-renew"
        root = target_root
        store = None

    monkeypatch.setattr(backup_retirement, "_payload_key_is_retained", lambda *a, **k: False)

    def _renew_fails(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise AppError(
            "blocked-metadata-gate:lease-expired:t-fence-renew",
            code=ErrorCode.INVALID_REQUEST,
            status=409,
        )

    delete_calls: list[str] = []
    real_delete = backup_retirement._delete_payload_if_match

    def _track_delete(target: Any, object_key_arg: str, *, expected_etag: str | None) -> int:
        delete_calls.append(object_key_arg)
        return real_delete(target, object_key_arg, expected_etag=expected_etag)

    monkeypatch.setattr(backup_control, "renew_target_metadata_gate", _renew_fails)
    monkeypatch.setattr(backup_retirement, "_delete_payload_if_match", _track_delete)

    reclaimed = backup_retirement._reclaim_unreferenced_payloads(
        _T(),
        receipt=receipt,
        retiring_backup_id="old",
        owner_id="gc-renew",
    )
    assert reclaimed == 0
    assert delete_calls == []
    assert path.is_file()
    intents = backup_control.list_ciphertext_gc_intents("t-fence-renew")
    assert any(i.get("error") == "metadata-fence-lost" for i in intents)


def test_assert_metadata_gate_rejects_stale_token_after_takeover(control_db: Path) -> None:
    with backup_control.begin_destructive_metadata_fence("t-assert", operation_id="gc-a") as stale:
        token_a = int(stale["fencingToken"])
    with backup_control._connect() as conn:
        conn.execute(
            """
            INSERT INTO target_metadata_gates(target_id, owner_id, mode, fencing_token, lease_until, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "t-assert",
                "gc-a",
                backup_control.METADATA_GATE_DESTRUCTIVE,
                token_a,
                "2000-01-01T00:00:00+00:00",
                "2000-01-01T00:00:00Z",
            ),
        )
    with backup_control.begin_destructive_metadata_fence("t-assert", operation_id="gc-b") as fresh:
        assert int(fresh["fencingToken"]) == token_a + 1
        assert backup_control.metadata_gate_is_current(
            "t-assert",
            owner_id="gc-b",
            fencing_token=int(fresh["fencingToken"]),
            mode=backup_control.METADATA_GATE_DESTRUCTIVE,
        )
        assert not backup_control.metadata_gate_is_current(
            "t-assert",
            owner_id="gc-a",
            fencing_token=token_a,
            mode=backup_control.METADATA_GATE_DESTRUCTIVE,
        )
        with pytest.raises(AppError, match="stale-token|owner-mismatch"):
            backup_control.assert_metadata_gate_current(
                "t-assert",
                owner_id="gc-a",
                fencing_token=token_a,
                mode=backup_control.METADATA_GATE_DESTRUCTIVE,
            )
        with pytest.raises(AppError, match="stale-token|owner-mismatch|lease-expired"):
            backup_control.renew_target_metadata_gate(
                "t-assert",
                owner_id="gc-a",
                fencing_token=token_a,
                mode=backup_control.METADATA_GATE_DESTRUCTIVE,
            )


def test_reconcile_interrupted_gc_intents_handles_every_terminal_outcome(
    control_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = "t-reconcile"
    current_generation = backup_control.get_target_receipt_mutation_generation(target_id)
    cases: dict[str, tuple[int, str]] = {
        "changed": (current_generation + 1, "changed-etag"),
        "gone": (current_generation, "gone-etag"),
        "etag": (current_generation, "old-etag"),
        "live": (current_generation, "live-etag"),
        "cas": (current_generation, "cas-etag"),
        "deleted": (current_generation, "deleted-etag"),
    }
    for name, (generation, etag) in cases.items():
        backup_control.create_ciphertext_gc_intent(
            target_id=target_id,
            object_key=f"objects/{name}",
            expected_receipt_mutation_generation=generation,
            expected_etag=etag,
            size_bytes=7,
            intent_id=f"intent-{name}",
        )

    observed_etags = {
        "objects/gone": None,
        "objects/etag": "new-etag",
        "objects/live": "live-etag",
        "objects/cas": "cas-etag",
        "objects/deleted": "deleted-etag",
    }

    def _stat(_target: Any, key: str) -> tuple[str | None, int]:
        return observed_etags.get(key, "changed-etag"), 7

    deleted: list[str] = []

    def _delete(_target: Any, key: str, *, expected_etag: str | None) -> int:
        assert expected_etag is not None
        if key == "objects/cas":
            raise AppError("cas-mismatch", code=ErrorCode.INVALID_REQUEST, status=409)
        deleted.append(key)
        return 7

    monkeypatch.setattr(backup_retirement, "_object_etag_and_size", _stat)
    monkeypatch.setattr(
        backup_retirement,
        "_payload_key_is_retained",
        lambda _target, key, *, retiring_backup_id: key == "objects/live",
    )
    monkeypatch.setattr(backup_retirement, "_delete_payload_if_match", _delete)

    result = backup_retirement.reconcile_interrupted_gc_intents(
        SimpleNamespace(target_id=target_id),
        limit=20,
    )
    assert result == {"examined": 6, "reclaimed": 2, "cancelled": 4}
    assert deleted == ["objects/deleted"]
    states = {item["objectKey"]: item["state"] for item in backup_control.list_ciphertext_gc_intents(target_id)}
    assert states == {
        "objects/changed": backup_control.GC_INTENT_CANCELLED,
        "objects/gone": backup_control.GC_INTENT_RECLAIMED,
        "objects/etag": backup_control.GC_INTENT_CANCELLED,
        "objects/live": backup_control.GC_INTENT_CANCELLED,
        "objects/cas": backup_control.GC_INTENT_CANCELLED,
        "objects/deleted": backup_control.GC_INTENT_RECLAIMED,
    }
    assert backup_retirement.reconcile_interrupted_gc_intents(SimpleNamespace(target_id="")) == {
        "examined": 0,
        "reclaimed": 0,
        "cancelled": 0,
    }


def test_two_phase_gc_cancels_all_intents_when_receipt_generation_changes(
    control_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = "t-generation-change"
    digest = "1" * 64
    key = object_key(digest)
    target = SimpleNamespace(target_id=target_id, root=None, store=object())

    class _MutatingFence:
        def __enter__(self) -> dict[str, Any]:
            backup_control.note_formal_receipt_mutation(target_id)
            return {
                "targetId": target_id,
                "ownerId": "gc-owner",
                "mode": backup_control.METADATA_GATE_DESTRUCTIVE,
                "fencingToken": 1,
            }

        def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            return None

    monkeypatch.setattr(backup_retirement, "_payload_key_is_retained", lambda *args, **kwargs: False)
    monkeypatch.setattr(backup_retirement, "_object_etag_and_size", lambda *args, **kwargs: ("etag", 9))
    monkeypatch.setattr(backup_control, "begin_destructive_metadata_fence", lambda *args, **kwargs: _MutatingFence())
    monkeypatch.setattr(
        backup_control,
        "renew_target_metadata_gate",
        lambda *args, **kwargs: {"fencingToken": 1, "leaseUntil": "2099-01-01T00:00:00Z"},
    )
    monkeypatch.setattr(backup_control, "assert_metadata_gate_current", lambda *args, **kwargs: None)

    assert backup_retirement._reclaim_unreferenced_payloads(
        target,
        receipt={"objects": [{"path": key, "size": 9}]},
        retiring_backup_id="retiring",
        owner_id="gc-owner",
    ) == 0
    intent = backup_control.list_ciphertext_gc_intents(target_id)[0]
    assert intent["objectKey"] == key
    assert intent["state"] == backup_control.GC_INTENT_CANCELLED
    assert intent["error"] == "receipt-mutation-generation-changed"


def test_two_phase_gc_revalidation_handles_live_absent_and_etag_changed(
    control_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = "t-revalidation"
    digests = {
        "gone": "2" * 64,
        "etag": "3" * 64,
        "live": "4" * 64,
    }
    keys = {name: object_key(digest) for name, digest in digests.items()}
    target = SimpleNamespace(target_id=target_id, root=None, store=object())
    retain_calls: dict[str, int] = {}
    stat_calls: dict[str, int] = {}

    def _retained(_target: Any, key: str, *, retiring_backup_id: str) -> bool:
        assert retiring_backup_id == "retiring"
        retain_calls[key] = retain_calls.get(key, 0) + 1
        return key == keys["live"] and retain_calls[key] > 1

    def _stat(_target: Any, key: str) -> tuple[str | None, int]:
        stat_calls[key] = stat_calls.get(key, 0) + 1
        if key == keys["gone"] and stat_calls[key] > 1:
            return None, 0
        if key == keys["etag"] and stat_calls[key] > 1:
            return "new-etag", 9
        return "old-etag" if key == keys["etag"] else f"etag-{key[-1]}", 9

    monkeypatch.setattr(backup_retirement, "_payload_key_is_retained", _retained)
    monkeypatch.setattr(backup_retirement, "_object_etag_and_size", _stat)
    monkeypatch.setattr(
        backup_retirement,
        "_delete_payload_if_match",
        lambda *args, **kwargs: pytest.fail("revalidation outcomes must not reach DELETE"),
    )

    with pytest.raises(AppError, match="cas-mismatch"):
        backup_retirement._reclaim_unreferenced_payloads(
            target,
            receipt={"objects": [{"path": key, "size": 9} for key in keys.values()]},
            retiring_backup_id="retiring",
            owner_id="gc-owner",
        )
    intents = {item["objectKey"]: item for item in backup_control.list_ciphertext_gc_intents(target_id)}
    assert intents[keys["gone"]]["state"] == backup_control.GC_INTENT_RECLAIMED
    assert intents[keys["live"]]["error"] == "live-ref-appeared"
    assert intents[keys["etag"]]["error"] == "etag-mismatch"


def test_two_phase_gc_propagates_non_cas_delete_errors(
    control_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = "t-delete-error"
    digest = "5" * 64
    key = object_key(digest)
    target = SimpleNamespace(target_id=target_id, root=None, store=object())
    monkeypatch.setattr(backup_retirement, "_payload_key_is_retained", lambda *args, **kwargs: False)
    monkeypatch.setattr(backup_retirement, "_object_etag_and_size", lambda *args, **kwargs: ("etag", 9))

    def _fail_delete(*args: Any, **kwargs: Any) -> int:
        raise AppError("storage unavailable", code=ErrorCode.INTERNAL, status=500)

    monkeypatch.setattr(backup_retirement, "_delete_payload_if_match", _fail_delete)
    with pytest.raises(AppError, match="storage unavailable"):
        backup_retirement._reclaim_unreferenced_payloads(
            target,
            receipt={"objects": [{"path": key, "size": 9}]},
            retiring_backup_id="retiring",
            owner_id="gc-owner",
        )
    intent = backup_control.list_ciphertext_gc_intents(target_id)[0]
    assert intent["state"] == backup_control.GC_INTENT_DELETING


def test_two_phase_gc_requires_target_identity(control_db: Path) -> None:
    with pytest.raises(AppError, match="target_id required for payload GC"):
        backup_retirement._reclaim_unreferenced_payloads(
            SimpleNamespace(target_id="", root=None, store=None),
            receipt={"objects": []},
            retiring_backup_id="retiring",
            owner_id="gc-owner",
        )


def test_index_coverage_rejects_stale_mutation_generation(control_db: Path) -> None:
    backup_control.set_target_index_coverage(
        "t-stale",
        state="complete",
        formal_receipt_count=1,
        enumerated_receipts=1,
        parsed_receipts=1,
        indexed_receipts=1,
        source_receipt_mutation_generation=99,
    )
    assert backup_control.index_coverage_allows_gc("t-stale") == (False, "object-reference-index-stale")


def test_payload_retention_prefers_authoritative_index_and_over_retains_partial_index(
    control_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = SimpleNamespace(target_id="t-index", root=None, store=None)
    index_mode: set[str] | None = set()
    live = True

    def _index(*args: Any, **kwargs: Any) -> set[str] | None:
        return index_mode

    def _live(*args: Any, **kwargs: Any) -> bool:
        return live

    monkeypatch.setattr(backup_object_index, "retained_payload_keys_from_index", _index)
    monkeypatch.setattr(backup_object_index, "object_is_live_referenced", _live)
    assert backup_retirement._payload_key_is_retained(target, "objects/live", retiring_backup_id="old")

    live = False
    assert not backup_retirement._payload_key_is_retained(target, "objects/free", retiring_backup_id="old")

    index_mode = None
    live = True
    assert backup_retirement._payload_key_is_retained(target, "objects/partial", retiring_backup_id="old")


def test_payload_retention_falls_back_to_filesystem_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "a-old.json").write_text(
        json.dumps({"backupId": "old", "objects": [{"path": "objects/retiring"}]}),
        encoding="utf-8",
    )
    (receipts / "b-retired.json").write_text(
        json.dumps({"backupId": "retired", "objects": [{"path": "objects/retired"}]}),
        encoding="utf-8",
    )
    (receipts / "c-live.json").write_text(
        json.dumps({"backupId": "live", "objects": [{"path": "objects/live"}]}),
        encoding="utf-8",
    )
    target = SimpleNamespace(target_id="", root=tmp_path, store=None)
    monkeypatch.setattr(
        backup_retirement,
        "_receipt_has_valid_retirement_marker",
        lambda _target, _raw, receipt: receipt.get("backupId") == "retired",
    )
    assert backup_retirement._payload_key_is_retained(target, "objects/live", retiring_backup_id="old")
    assert not backup_retirement._payload_key_is_retained(target, "objects/free", retiring_backup_id="old")

    (receipts / "d-invalid.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(backup_retirement.GcReferenceScanIndeterminate, match="receipt-parse-failure"):
        backup_retirement._payload_key_is_retained(target, "objects/free", retiring_backup_id="old")


def test_payload_retention_falls_back_to_store_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryTargetStore()
    store.put_if_absent(
        "receipts/a-old.json",
        json.dumps({"backupId": "old", "objects": [{"path": "objects/retiring"}]}).encode(),
    )
    store.put_if_absent(
        "receipts/b-retired.json",
        json.dumps({"backupId": "retired", "objects": [{"path": "objects/retired"}]}).encode(),
    )
    store.put_if_absent(
        "receipts/c-live.json",
        json.dumps({"backupId": "live", "objects": [{"path": "objects/live"}]}).encode(),
    )
    target = SimpleNamespace(target_id="", root=None, store=store)
    monkeypatch.setattr(
        backup_retirement,
        "_receipt_has_valid_retirement_marker",
        lambda _target, _raw, receipt: receipt.get("backupId") == "retired",
    )
    assert backup_retirement._payload_key_is_retained(target, "objects/live", retiring_backup_id="old")
    assert not backup_retirement._payload_key_is_retained(target, "objects/free", retiring_backup_id="old")


def test_payload_retention_store_scan_fails_closed_on_missing_or_invalid_receipt() -> None:
    meta = ObjectMeta(key="receipts/missing.json", size=1, etag="etag")

    class _MissingStore:
        def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 1000) -> ListPage:
            return ListPage(objects=(meta,))

        def get_bytes(self, key: str) -> bytes | None:
            return None

    target = SimpleNamespace(target_id="", root=None, store=_MissingStore())
    with pytest.raises(backup_retirement.GcReferenceScanIndeterminate, match="receipt-read-failure"):
        backup_retirement._payload_key_is_retained(target, "objects/free", retiring_backup_id="old")

    class _BrokenStore(_MissingStore):
        def get_bytes(self, key: str) -> bytes | None:
            return b"not-json"

    target.store = _BrokenStore()
    with pytest.raises(backup_retirement.GcReferenceScanIndeterminate, match="receipt-parse-failure"):
        backup_retirement._payload_key_is_retained(target, "objects/free", retiring_backup_id="old")

    class _FailingStore(_MissingStore):
        def get_bytes(self, key: str) -> bytes:
            raise OSError("read failed")

    target.store = _FailingStore()
    with pytest.raises(backup_retirement.GcReferenceScanIndeterminate, match="receipt-read-failure"):
        backup_retirement._payload_key_is_retained(target, "objects/free", retiring_backup_id="old")


def test_retained_payload_set_uses_index_or_store_fallback(
    control_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_object_index, "retained_payload_keys_from_index", lambda *args, **kwargs: set())
    assert backup_retirement._retained_payload_keys(
        SimpleNamespace(target_id="t-index", root=None, store=None),
        retiring_backup_id="old",
    ) == set()

    monkeypatch.setattr(backup_object_index, "retained_payload_keys_from_index", lambda *args, **kwargs: None)
    store = MemoryTargetStore()
    store.put_if_absent(
        "receipts/a-old.json",
        json.dumps({"backupId": "old", "objects": [{"path": "objects/retiring"}]}).encode(),
    )
    store.put_if_absent(
        "receipts/b-retired.json",
        json.dumps({"backupId": "retired", "objects": [{"path": "objects/retired"}]}).encode(),
    )
    store.put_if_absent(
        "receipts/c-live.json",
        json.dumps({"backupId": "live", "objects": [{"path": "objects/live"}]}).encode(),
    )
    monkeypatch.setattr(
        backup_retirement,
        "_receipt_has_valid_retirement_marker",
        lambda _target, _raw, receipt: receipt.get("backupId") == "retired",
    )
    assert backup_retirement._retained_payload_keys(
        SimpleNamespace(target_id="", root=None, store=store),
        retiring_backup_id="old",
    ) == {"objects/live"}

    meta = ObjectMeta(key="receipts/broken.json", size=1, etag="etag")

    class _BrokenStore:
        def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 1000) -> ListPage:
            return ListPage(objects=(meta,))

        def get_bytes(self, key: str) -> bytes | None:
            return b"not-json"

    with pytest.raises(backup_retirement.GcReferenceScanIndeterminate, match="receipt-parse-failure"):
        backup_retirement._retained_payload_keys(
            SimpleNamespace(target_id="", root=None, store=_BrokenStore()),
            retiring_backup_id="old",
        )

    class _MissingStore(_BrokenStore):
        def get_bytes(self, key: str) -> None:
            return None

    with pytest.raises(backup_retirement.GcReferenceScanIndeterminate, match="receipt-read-failure"):
        backup_retirement._retained_payload_keys(
            SimpleNamespace(target_id="", root=None, store=_MissingStore()),
            retiring_backup_id="old",
        )

    class _FailingStore(_BrokenStore):
        def get_bytes(self, key: str) -> bytes:
            raise OSError("read failed")

    with pytest.raises(backup_retirement.GcReferenceScanIndeterminate, match="receipt-read-failure"):
        backup_retirement._retained_payload_keys(
            SimpleNamespace(target_id="", root=None, store=_FailingStore()),
            retiring_backup_id="old",
        )


def test_retained_payload_set_paginates_and_fails_closed_on_filesystem_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_meta = ObjectMeta(key="receipts/old.json", size=1, etag="etag-old")
    second_meta = ObjectMeta(key="receipts/live.json", size=1, etag="etag-live")

    class _PagedStore:
        def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 1000) -> ListPage:
            if cursor is None:
                return ListPage(objects=(first_meta,), cursor="next")
            return ListPage(objects=(second_meta,))

        def get_bytes(self, key: str) -> bytes:
            backup_id = "old" if key.endswith("old.json") else "live"
            return json.dumps({"backupId": backup_id, "objects": [{"path": f"objects/{backup_id}"}]}).encode()

    monkeypatch.setattr(backup_retirement, "_receipt_has_valid_retirement_marker", lambda *args, **kwargs: False)
    assert backup_retirement._retained_payload_keys(
        SimpleNamespace(target_id="", root=None, store=_PagedStore()),
        retiring_backup_id="old",
    ) == {"objects/live"}

    receipts = tmp_path / "receipts"
    receipts.mkdir()
    broken = receipts / "broken.json"
    broken.write_text("{}", encoding="utf-8")
    original_read_bytes = Path.read_bytes

    def _read_bytes(path: Path) -> bytes:
        if path == broken:
            raise OSError("read failed")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)
    with pytest.raises(backup_retirement.GcReferenceScanIndeterminate, match="receipt-read-failure"):
        backup_retirement._retained_payload_keys(
            SimpleNamespace(target_id="", root=tmp_path, store=None),
            retiring_backup_id="old",
        )


def test_retirement_marker_reads_filesystem_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = {"policyId": "policy", "backupId": "backup"}
    marker_path = tmp_path.joinpath(*backup_retirement.retirement_marker_key("policy", "backup").split("/"))
    marker_path.parent.mkdir(parents=True)
    marker_path.write_text(json.dumps({"targetId": "target"}), encoding="utf-8")
    monkeypatch.setattr(backup_retirement, "_read_formal_metadata", lambda *args, **kwargs: ({}, {}, {}))
    monkeypatch.setattr(backup_retirement, "retirement_marker_valid", lambda *args, **kwargs: True)
    assert backup_retirement._receipt_has_valid_retirement_marker(
        SimpleNamespace(target_id="target", root=tmp_path, store=None),
        b"receipt",
        receipt,
    )


def test_store_index_rebuild_counts_read_and_parse_failures(
    control_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metas = tuple(
        ObjectMeta(key=f"receipts/{name}.json", size=1, etag=f"etag-{name}")
        for name in ("raised", "missing", "invalid", "valid")
    )

    class _Store:
        def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 1000) -> ListPage:
            return ListPage(objects=metas)

        def get_bytes(self, key: str) -> bytes | None:
            if key.endswith("raised.json"):
                raise OSError("read failed")
            if key.endswith("missing.json"):
                return None
            if key.endswith("invalid.json"):
                return b"not-json"
            return json.dumps(
                {
                    "backupId": "valid",
                    "policyId": "policy",
                    "objects": [{"path": "objects/live", "digest": "6" * 64, "size": 7}],
                }
            ).encode()

    monkeypatch.setattr(backup_retirement, "_receipt_has_valid_retirement_marker", lambda *args, **kwargs: False)
    result = backup_object_index.rebuild_index_from_target(
        SimpleNamespace(target_id="t-store-rebuild", root=None, store=_Store())
    )
    assert result["coverageState"] == "incomplete"
    assert result["scannedReceipts"] == 4
    assert result["parsedReceipts"] == 1
    assert result["indexedReceipts"] == 1
    assert result["readFailures"] == 2
    assert result["parseFailures"] == 1
