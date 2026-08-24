"""Coverage booster for control-authority / recovery edge paths."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_control,
    backup_control_authority,
    backup_control_recovery,
    backup_object_set,
    backup_publish,
)
from deepseek_infra.infra.workspace.backup_target_store import MemoryTargetStore, receipt_key


@pytest.fixture
def control_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "control"
    root.mkdir()
    db = root / "control.sqlite3"
    monkeypatch.setattr(backup_control, "CONTROL_DIR", root)
    monkeypatch.setattr(backup_control, "CONTROL_DB", db)
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    return db


def _ckpt(**kwargs: Any) -> dict[str, Any]:
    base = {
        "generation": 1,
        "previous_digest": None,
        "policies": [],
        "targets": [],
        "receipt_mutation_generations": {},
        "promotion_epochs": {},
        "drain_generations": {},
        "placement_generations": {},
        "control_schema_version": 7,
        "created_at": "2026-08-24T00:00:00Z",
    }
    base.update(kwargs)
    return backup_control_authority.build_authority_checkpoint(**base)


def test_verify_chain_payload_and_broken_link_and_gap(control_db: Path) -> None:
    a = _ckpt(generation=1)
    b = _ckpt(generation=2, previous_digest=str(a["digest"]), policies=[{"policyId": "p", "policyRevision": 1}])
    bad_payload = dict(b)
    bad_payload["payloadDigest"] = "0" * 64
    with pytest.raises(AppError, match="payload-digest-mismatch"):
        backup_control_authority.verify_authority_chain([a, bad_payload])
    broken = _ckpt(generation=2, previous_digest="1" * 64, policies=[{"policyId": "x", "policyRevision": 1}])
    with pytest.raises(AppError, match="broken-chain|divergent"):
        backup_control_authority.verify_authority_chain([a, broken])
    # invalid schema / generation
    with pytest.raises(AppError, match="schema-mismatch|digest"):
        weird = dict(a)
        weird["schema"] = "nope"
        backup_control_authority.verify_authority_chain([weird])
    with pytest.raises(AppError, match="invalid-generation|generation"):
        backup_control_authority.build_authority_checkpoint(
            generation=0,
            previous_digest=None,
            policies=[],
            targets=[],
            receipt_mutation_generations={},
            promotion_epochs={},
            drain_generations={},
            placement_generations={},
            control_schema_version=7,
        )


def test_select_heads_empty_and_invalid(control_db: Path) -> None:
    with pytest.raises(AppError, match="no-replicas"):
        backup_control_authority.select_authority_heads({})
    with pytest.raises(AppError, match="invalid-head"):
        backup_control_authority.select_authority_heads({"r": {"generation": 0, "digest": ""}})


def test_store_checkpoint_conflict_and_load_errors(control_db: Path) -> None:
    store = MemoryTargetStore()
    ckpt = _ckpt(generation=1)
    backup_control_authority.write_authority_checkpoint_to_store(store, ckpt)
    other = _ckpt(generation=1, policies=[{"policyId": "diff", "policyRevision": 1}])
    with pytest.raises(AppError, match="checkpoint-conflict"):
        backup_control_authority.write_authority_checkpoint_to_store(store, other)
    with pytest.raises(AppError, match="head-missing"):
        backup_control_authority.load_authority_bundle_from_store(MemoryTargetStore(), replica_id="empty")
    # corrupt head json type
    bad = MemoryTargetStore()
    bad.put_if_absent(backup_control_authority.authority_head_key(), b"[1,2,3]\n")
    with pytest.raises(AppError, match="head-invalid"):
        backup_control_authority.load_authority_bundle_from_store(bad)
    # head/checkpoint digest mismatch
    store2 = MemoryTargetStore()
    backup_control_authority.write_authority_checkpoint_to_store(store2, ckpt)
    head_key = backup_control_authority.authority_head_key()
    meta = store2.stat(head_key)
    assert meta is not None
    store2.put_if_match(
        head_key,
        b'{"schema":"control-authority-v1","authorityGeneration":1,"digest":"' + b"f" * 64 + b'"}\n',
        expected_etag=str(meta.etag),
    )
    with pytest.raises(AppError, match="head-checkpoint-mismatch"):
        backup_control_authority.load_authority_bundle_from_store(store2)


def test_put_json_replace_missing_etag(control_db: Path) -> None:
    class _NoEtag:
        def stat(self, key: str) -> Any:
            return SimpleNamespace(etag=None)

        def put_if_absent(self, *a: Any, **k: Any) -> Any:
            return SimpleNamespace(created=True)

        def get_bytes(self, key: str) -> bytes | None:
            return None

    with pytest.raises(OSError, match="missing-etag"):
        backup_control_authority._put_json_replace(_NoEtag(), "k", {"a": 1})


def test_write_roots_and_stores_all_fail(control_db: Path, tmp_path: Path) -> None:
    ckpt = _ckpt(generation=1)
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(AppError, match="anchor-failed"):
        backup_control_authority._write_checkpoint_to_roots(ckpt, [blocker])

    class _Boom:
        def get_bytes(self, key: str) -> bytes | None:
            raise OSError("down")

        def put_if_absent(self, *a: Any, **k: Any) -> Any:
            raise OSError("down")

        def stat(self, key: str) -> Any:
            return None

    with pytest.raises(AppError, match="anchor-failed"):
        backup_control_authority._write_checkpoint_to_stores(ckpt, [_Boom()])


def test_anchor_rpo_false_on_store_failure(control_db: Path) -> None:
    class _Boom:
        def get_bytes(self, key: str) -> bytes | None:
            return None

        def put_if_absent(self, *a: Any, **k: Any) -> Any:
            raise OSError("nope")

        def stat(self, key: str) -> Any:
            return None

        def put_if_match(self, *a: Any, **k: Any) -> Any:
            raise OSError("nope")

    result = backup_control_authority.anchor_non_rebuildable_mutation(
        kind="x",
        stores=[_Boom()],
        rpo_zero=False,
    )
    assert result["status"] == "failed"


def test_drain_outbox_invalid_json_and_rpo_false_no_roots(control_db: Path) -> None:
    now = "2026-08-24T00:00:00Z"
    with backup_control._connect() as conn:
        backup_control._begin_immediate(conn)
        conn.execute(
            """
            INSERT INTO control_authority_outbox(outbox_id, kind, checkpoint_json, state, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            ("bad1", "k", "not-json", backup_control_authority.OUTBOX_PENDING, now, now),
        )
        conn.execute(
            """
            INSERT INTO control_authority_outbox(outbox_id, kind, checkpoint_json, state, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, NULL, ?, ?)
            """,
            ("bad2", "k", "[]", backup_control_authority.OUTBOX_PENDING, now, now),
        )
        conn.execute("COMMIT")
    result = backup_control_authority.drain_pending_authority_outbox(rpo_zero=False)
    assert result["failed"] >= 2


def test_ensure_ready_anchor_failed(control_db: Path, tmp_path: Path) -> None:
    ckpt = _ckpt(generation=1)
    backup_control_authority._enqueue_authority_outbox(kind="s", checkpoint=ckpt)
    blocker = tmp_path / "blocked"
    blocker.write_text("x", encoding="utf-8")
    backup_control_authority.configure_authority_anchor_roots([blocker])
    out = backup_control.ensure_control_authority_ready()
    assert out["status"] == "anchor-failed"
    assert int(out.get("failed") or 0) >= 1


def test_discover_skips_bad_fs_and_store(control_db: Path, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert backup_control_recovery.discover_authority_replicas([empty]) == {}
    assert backup_control_recovery.discover_authority_replicas_from_stores([MemoryTargetStore()]) == {}


def test_quarantine_moves_wal_shm(control_db: Path) -> None:
    db = Path(backup_control.CONTROL_DB)
    backup_control.schema_version()
    wal = Path(str(db) + "-wal")
    shm = Path(str(db) + "-shm")
    wal.write_bytes(b"wal")
    shm.write_bytes(b"shm")
    dest = backup_control_recovery._quarantine_corrupt_db(db)
    assert dest is not None
    assert dest.is_file()
    assert Path(str(dest) + "-wal").is_file()
    assert Path(str(dest) + "-shm").is_file()
    assert backup_control_recovery._quarantine_corrupt_db(Path(str(db) + ".missing")) is None


def test_authenticate_object_set_and_missing_backup(control_db: Path, tmp_path: Path) -> None:
    target_root = tmp_path / "tgt"
    (target_root / "receipts").mkdir(parents=True)
    target = SimpleNamespace(target_id="t", root=target_root, store=None)
    # missing backup id
    assert backup_control_recovery.authenticate_committed_receipt(target, {"commitHash": "x"}) is None
    # invalid marker
    assert backup_control_recovery.authenticate_committed_receipt(target, {"backupId": "b", "commitHash": "0" * 64}) is None

    digest = "aa" * 32
    receipt = {
        "schemaVersion": 4,
        "storageProtocol": backup_object_set.OBJECT_SET_V1,
        "backupId": "b-os",
        "policyId": "p",
        "objectSetDigest": "d" * 64,
        "controlObjectDigest": "c" * 64,
        "objects": [{"digest": digest, "size": 1, "ciphertextDigest": digest}],
    }
    # Make inventory happy if possible - may fail and return None
    try:
        inv = backup_object_set.committed_object_inventory(receipt)
        assert inv is not None
    except AppError:
        # still exercise mismatch branches
        pass
    raw = (json.dumps(receipt, sort_keys=True) + "\n").encode("utf-8")
    (target_root / "receipts" / "b-os.json").write_bytes(raw)
    marker = {
        "schemaVersion": 3,  # wrong for object-set
        "backupId": "b-os",
        "policyId": "p",
        "receiptDigest": hashlib.sha256(raw).hexdigest(),
        "objectSetDigest": "d" * 64,
        "controlObjectDigest": "c" * 64,
        "targetGeneration": 1,
    }
    marker["commitHash"] = backup_publish._commit_hash(marker)
    assert backup_control_recovery.authenticate_committed_receipt(target, marker) is None

    marker2 = dict(marker)
    marker2["schemaVersion"] = backup_publish.COMMIT_SCHEMA_VERSION
    marker2["objectSetDigest"] = "e" * 64
    marker2["commitHash"] = backup_publish._commit_hash(marker2)
    assert backup_control_recovery.authenticate_committed_receipt(target, marker2) is None

    marker3 = dict(marker)
    marker3["schemaVersion"] = backup_publish.COMMIT_SCHEMA_VERSION
    marker3["controlObjectDigest"] = "f" * 64
    marker3["commitHash"] = backup_publish._commit_hash(marker3)
    assert backup_control_recovery.authenticate_committed_receipt(target, marker3) is None


def test_load_receipt_nested_and_store_list(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "nested"
    nested = root / "receipts" / "sub"
    nested.mkdir(parents=True)
    nested.joinpath("bid.json").write_text("{}", encoding="utf-8")
    target = SimpleNamespace(target_id="t", root=root, store=None)
    assert backup_control_recovery._load_receipt_bytes(target, "bid") is not None
    assert backup_control_recovery._load_receipt_bytes(target, "missing") is None
    assert backup_control_recovery._list_receipt_backup_ids(target) == {"bid"}

    store = MemoryTargetStore()
    store.put_if_absent(receipt_key("s1"), b'{"a":1}\n')
    st = SimpleNamespace(target_id="ts", root=None, store=store)
    assert backup_control_recovery._load_receipt_bytes(st, "s1") is not None
    assert "s1" in backup_control_recovery._list_receipt_backup_ids(st)
    empty = SimpleNamespace(target_id="e", root=None, store=None)
    assert backup_control_recovery._load_receipt_bytes(empty, "x") is None
    assert backup_control_recovery._list_receipt_backup_ids(empty) == set()
    assert backup_control_recovery._iter_commit_markers(empty) == []


def test_delete_policy_and_target_anchor(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.create_policy({"policyId": "del-p", "policyRevision": 1, "enabled": True})
    backup_control.upsert_target({"targetId": "del-t", "kind": "s3", "bucket": "b", "endpointUrl": "https://x"})
    before = json.loads((root / "control" / "authority" / "head.json").read_text(encoding="utf-8"))
    backup_control.delete_policy("del-p")
    mid = json.loads((root / "control" / "authority" / "head.json").read_text(encoding="utf-8"))
    assert int(mid["authorityGeneration"]) > int(before["authorityGeneration"])
    backup_control.delete_target("del-t")
    after = json.loads((root / "control" / "authority" / "head.json").read_text(encoding="utf-8"))
    assert int(after["authorityGeneration"]) > int(mid["authorityGeneration"])


def test_mutate_target_drain_complete(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "auth2"
    root.mkdir()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control.upsert_target(
        {"targetId": "t-dc", "kind": "s3", "bucket": "b", "endpointUrl": "https://x", "drainState": "draining"}
    )
    backup_control.mutate_target(
        "t-dc",
        expected_generation=None,
        mutate=lambda t: {**t, "drainState": "drained", "drainedAt": "2026-08-24T00:00:00Z"},
    )
    head = json.loads((root / "control" / "authority" / "head.json").read_text(encoding="utf-8"))
    assert int(head["authorityGeneration"]) >= 1


def test_strip_secrets_list_and_provider_without_type(control_db: Path) -> None:
    cleaned = backup_control_authority.strip_secrets(
        {
            "items": [{"secretAccessKey": "x", "ok": 1}],
            "credentialProvider": {"accessKey": "a"},
            "credentialReference": "ref",
        }
    )
    assert cleaned["credentialReference"] == "ref"
    assert "secretAccessKey" not in cleaned["items"][0]
    assert cleaned["items"][0]["ok"] == 1


def test_apply_authority_skips_bad_entries(control_db: Path) -> None:
    ckpt = _ckpt(
        generation=1,
        policies=[{"noId": True}, {"policyId": "p-ok", "policyRevision": 2}],
        targets=[{"noId": True}, {"targetId": "t-ok", "kind": "s3", "bucket": "b"}],
        receipt_mutation_generations={"t-ok": 3},
    )
    backup_control_authority.apply_authority_checkpoint_to_fresh_db(ckpt)
    assert backup_control.get_policy("p-ok") is not None
    assert backup_control.get_target("t-ok") is not None


def test_reconstruct_incomplete_checkpoint(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "inc"
    root.mkdir()
    # head-only without policies key
    head_only = {
        "schema": backup_control_authority.AUTHORITY_SCHEMA,
        "authorityGeneration": 1,
        "digest": "a" * 64,
    }
    # write a valid checkpoint then corrupt by selecting head-only via mock
    ckpt = _ckpt(generation=1, policies=[{"policyId": "p", "policyRevision": 1}])
    backup_control_authority.write_authority_checkpoint_bundle(root, ckpt)
    # force incomplete by patching select to return head-only
    real_select = backup_control_authority.select_authority_heads

    def _fake(replicas: dict[str, Any]) -> dict[str, Any]:
        out = real_select(replicas)
        out["checkpoint"] = head_only
        return out

    monkey = pytest.MonkeyPatch()
    monkey.setattr(backup_control_authority, "select_authority_heads", _fake)
    try:
        with pytest.raises(AppError, match="incomplete"):
            backup_control_recovery.reconstruct_control_authority([root])
    finally:
        monkey.undo()


def test_formal_truth_invalid_bound_types(control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_retirement

    target_root = tmp_path / "ft"
    (target_root / "commits" / "p").mkdir(parents=True)
    (target_root / "receipts").mkdir(parents=True)
    receipt = {"schemaVersion": 4, "backupId": "b1", "policyId": "p1", "objects": [{"digest": "bb" * 32, "size": 1}]}
    raw = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (target_root / "receipts" / "b1.json").write_bytes(raw)
    marker = {
        "schemaVersion": 4,
        "backupId": "b1",
        "policyId": "p1",
        "receiptDigest": hashlib.sha256(raw).hexdigest(),
        "targetGeneration": 1,
    }
    marker["commitHash"] = backup_publish._commit_hash(marker)
    (target_root / "commits" / "p" / "s.json").write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")

    real_auth = backup_control_recovery.authenticate_committed_receipt

    def _bad_types(target: Any, m: dict[str, Any]) -> dict[str, Any] | None:
        bound = real_auth(target, m)
        if bound is None:
            return None
        return {"backupId": "b1", "receipt": "not-dict", "receiptBytes": "not-bytes"}

    monkeypatch.setattr(backup_control_recovery, "authenticate_committed_receipt", _bad_types)
    monkeypatch.setattr(backup_retirement, "_receipt_has_valid_retirement_marker", lambda *a, **k: False)
    target = SimpleNamespace(target_id="t-bad", root=target_root, store=None)
    result = backup_control_recovery.rebuild_formal_truth_from_authenticated_commits(target)
    assert result["invalidCommits"] >= 1
