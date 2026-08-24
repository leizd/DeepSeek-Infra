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


def test_allowed_secret_adjacent_and_invalid_generation_in_chain(control_db: Path) -> None:
    assert backup_control_authority._key_is_forbidden("credential_reference") is False
    assert backup_control_authority._key_is_forbidden("secretAccessKey") is True
    # gen < 1 inside verify (bypass build)
    bad = {
        "schema": backup_control_authority.AUTHORITY_SCHEMA,
        "authorityGeneration": 0,
        "previousDigest": None,
        "policies": [],
        "targets": [],
        "receiptMutationGenerations": {},
        "promotionEpochs": {},
        "drainGenerations": {},
        "placementGenerations": {},
        "controlSchemaVersion": 7,
        "createdAt": "2026-08-24T00:00:00Z",
    }
    bad["payloadDigest"] = backup_control_authority.compute_payload_digest(bad)
    bad["digest"] = backup_control_authority.compute_checkpoint_digest(bad)
    with pytest.raises(AppError, match="invalid-generation"):
        backup_control_authority.verify_authority_chain([bad])


def test_select_heads_without_checkpoint_blob(control_db: Path) -> None:
    chosen = backup_control_authority.select_authority_heads(
        {"r1": {"generation": 3, "digest": "a" * 64}}
    )
    assert chosen["generation"] == 3
    assert chosen["checkpoint"]["digest"] == "a" * 64


def test_put_absent_conflict_and_store_list_pagination(control_db: Path) -> None:
    class _ConflictStore:
        def stat(self, key: str) -> Any:
            return None

        def put_if_absent(self, *a: Any, **k: Any) -> Any:
            return SimpleNamespace(created=False)

        def get_bytes(self, key: str) -> bytes | None:
            return b"other"

    with pytest.raises(OSError, match="absent-conflict"):
        backup_control_authority._put_json_replace(_ConflictStore(), "k", {"a": 1})

    class _Page:
        def __init__(self, objects: list[Any], cursor: str | None) -> None:
            self.objects = objects
            self.cursor = cursor

    ckpt = _ckpt(generation=1)
    ckpt_raw = (backup_control_authority._canonical_json(ckpt) + "\n").encode()
    head = {
        "schema": backup_control_authority.AUTHORITY_SCHEMA,
        "authorityGeneration": 1,
        "digest": ckpt["digest"],
        "checkpointKey": backup_control_authority.authority_checkpoint_key(1),
    }
    head_raw = (backup_control_authority._canonical_json(head) + "\n").encode()

    class _PagedStore:
        def __init__(self) -> None:
            self.calls = 0

        def get_bytes(self, key: str) -> bytes | None:
            if key == backup_control_authority.authority_head_key():
                return head_raw
            if key == backup_control_authority.authority_checkpoint_key(1):
                return ckpt_raw
            if key.endswith("bad.json"):
                return b"not-json"
            if key.endswith("gone.json"):
                return None
            return None

        def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 200) -> Any:
            self.calls += 1
            if cursor is None:
                return _Page(
                    [
                        SimpleNamespace(key=f"{prefix}gone.json"),
                        SimpleNamespace(key=f"{prefix}bad.json"),
                        SimpleNamespace(key=backup_control_authority.authority_checkpoint_key(1)),
                    ],
                    cursor="next",
                )
            return _Page([], cursor=None)

    store = _PagedStore()
    bundle = backup_control_authority.load_authority_bundle_from_store(store)
    assert bundle["checkpoint"] is not None
    assert store.calls >= 2


def test_apply_skips_non_dict_entries(control_db: Path) -> None:
    ckpt = _ckpt(generation=1)
    ckpt["policies"] = ["bad", {"policyId": "p-nd", "policyRevision": 1}]
    ckpt["targets"] = ["bad", {"targetId": "t-nd", "kind": "s3"}]
    # recompute digests after mutation
    ckpt.pop("digest", None)
    ckpt.pop("payloadDigest", None)
    ckpt["payloadDigest"] = backup_control_authority.compute_payload_digest(ckpt)
    ckpt["digest"] = backup_control_authority.compute_checkpoint_digest(ckpt)
    backup_control_authority.apply_authority_checkpoint_to_fresh_db(ckpt)
    assert backup_control.get_policy("p-nd") is not None


def test_write_empty_roots_stores_return_empty(control_db: Path) -> None:
    ckpt = _ckpt(generation=1)
    assert backup_control_authority._write_checkpoint_to_roots(ckpt, []) == []
    assert backup_control_authority._write_checkpoint_to_stores(ckpt, []) == []


def test_drain_with_stores_and_no_durable(control_db: Path, tmp_path: Path) -> None:
    root = tmp_path / "ok"
    root.mkdir()
    ckpt = _ckpt(generation=1)
    backup_control_authority._enqueue_authority_outbox(kind="d", checkpoint=ckpt)
    store = MemoryTargetStore()
    backup_control_authority.configure_authority_anchor_roots([root])
    backup_control_authority.configure_authority_anchor_stores([store])
    out = backup_control_authority.drain_pending_authority_outbox(rpo_zero=True)
    assert out["drained"] == 1
    # second pending with empty write path: roots=[] stores=[] after clear mid-drain
    backup_control_authority._enqueue_authority_outbox(kind="d2", checkpoint=ckpt)
    backup_control_authority.configure_authority_anchor_roots(None)
    backup_control_authority.configure_authority_anchor_stores(None)
    with pytest.raises(AppError, match="no-roots"):
        backup_control_authority.drain_pending_authority_outbox(rpo_zero=True)


def test_drain_rpo_false_no_roots_continues(control_db: Path) -> None:
    ckpt = _ckpt(generation=1)
    backup_control_authority._enqueue_authority_outbox(kind="d3", checkpoint=ckpt)
    result = backup_control_authority.drain_pending_authority_outbox(rpo_zero=False)
    assert result["failed"] >= 1


def test_discover_store_tip_none(control_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = MemoryTargetStore()

    def _bundle(*a: Any, **k: Any) -> dict[str, Any]:
        return {"head": {"authorityGeneration": 1, "digest": "a" * 64}, "checkpoint": None, "history": []}

    monkeypatch.setattr(backup_control_authority, "load_authority_bundle_from_store", _bundle)
    assert backup_control_recovery.discover_authority_replicas_from_stores([store]) == {}


def test_iter_commits_store_errors_and_pagination(control_db: Path) -> None:
    class _Meta:
        def __init__(self, key: str) -> None:
            self.key = key

    class _Page:
        def __init__(self, objects: list[Any], cursor: str | None) -> None:
            self.objects = objects
            self.cursor = cursor

    class _Store:
        def __init__(self) -> None:
            self.n = 0

        def list_objects(self, prefix: str, *, cursor: str | None = None, limit: int = 200) -> Any:
            self.n += 1
            if cursor is None:
                return _Page([_Meta("commits/p/a.json"), _Meta("commits/p/b.json")], "c2")
            return _Page([_Meta("commits/p/c.json")], None)

        def get_bytes(self, key: str) -> bytes | None:
            if key.endswith("a.json"):
                raise RuntimeError("boom")
            if key.endswith("b.json"):
                return b"not-json"
            marker = {
                "schemaVersion": 4,
                "backupId": "bx",
                "policyId": "p",
                "receiptDigest": "0" * 64,
                "targetGeneration": 1,
                "commitHash": "x",
            }
            return (json.dumps(marker) + "\n").encode()

    target = SimpleNamespace(target_id="t", root=None, store=_Store())
    markers = backup_control_recovery._iter_commit_markers(target)
    assert len(markers) == 1


def test_load_receipt_oserror_and_store_exception(control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "r"
    (root / "receipts").mkdir(parents=True)
    path = root / "receipts" / "b.json"
    path.write_text("{}", encoding="utf-8")
    target = SimpleNamespace(target_id="t", root=root, store=None)

    def _boom(*a: Any, **k: Any) -> bytes:
        raise OSError("nope")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    assert backup_control_recovery._load_receipt_bytes(target, "b") is None

    class _Store:
        def get_bytes(self, key: str) -> bytes | None:
            raise RuntimeError("down")

    assert backup_control_recovery._load_receipt_bytes(SimpleNamespace(root=None, store=_Store()), "b") is None
    assert backup_control_recovery._list_receipt_backup_ids(SimpleNamespace(root=tmp_path / "nor", store=None)) == set()


def test_authenticate_object_set_inventory_ok_and_fail(control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_object_set as bos

    target_root = tmp_path / "os"
    (target_root / "receipts").mkdir(parents=True)
    target = SimpleNamespace(target_id="t", root=target_root, store=None)
    receipt = {
        "schemaVersion": 4,
        "storageProtocol": bos.OBJECT_SET_V1,
        "backupId": "bos1",
        "policyId": "p",
        "objectSetDigest": "d" * 64,
        "controlObjectDigest": "c" * 64,
        "objects": [],
    }
    raw = (json.dumps(receipt, sort_keys=True) + "\n").encode()
    (target_root / "receipts" / "bos1.json").write_bytes(raw)
    marker = {
        "schemaVersion": backup_publish.COMMIT_SCHEMA_VERSION,
        "backupId": "bos1",
        "policyId": "p",
        "receiptDigest": hashlib.sha256(raw).hexdigest(),
        "objectSetDigest": "d" * 64,
        "controlObjectDigest": "c" * 64,
        "targetGeneration": 1,
        "storageProtocol": bos.OBJECT_SET_V1,
    }
    marker["commitHash"] = backup_publish._commit_hash(marker)

    monkeypatch.setattr(bos, "committed_object_inventory", lambda r: [])
    assert backup_control_recovery.authenticate_committed_receipt(target, marker) is not None

    def _fail(r: Any) -> Any:
        raise AppError("bad inventory", code=__import__("deepseek_infra.core.errors", fromlist=["ErrorCode"]).ErrorCode.INVALID_REQUEST)

    monkeypatch.setattr(bos, "committed_object_inventory", _fail)
    assert backup_control_recovery.authenticate_committed_receipt(target, marker) is None


def test_formal_truth_missing_policy_and_retired(control_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.infra.workspace import backup_retirement

    target_root = tmp_path / "ret"
    (target_root / "commits" / "p").mkdir(parents=True)
    (target_root / "receipts").mkdir(parents=True)
    receipt = {"schemaVersion": 4, "backupId": "br", "policyId": "", "objects": [{"digest": "cc" * 32, "size": 1}]}
    raw = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    (target_root / "receipts" / "br.json").write_bytes(raw)
    marker = {
        "schemaVersion": 4,
        "backupId": "br",
        "policyId": "",
        "receiptDigest": hashlib.sha256(raw).hexdigest(),
        "targetGeneration": 1,
    }
    marker["commitHash"] = backup_publish._commit_hash(marker)
    (target_root / "commits" / "p" / "s.json").write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")

    # missing policy on both receipt and marker
    monkeypatch.setattr(backup_retirement, "_receipt_has_valid_retirement_marker", lambda *a, **k: False)
    target = SimpleNamespace(target_id="t-ret", root=target_root, store=None)
    result = backup_control_recovery.rebuild_formal_truth_from_authenticated_commits(target)
    assert result["invalidCommits"] >= 1

    # retired path
    receipt2 = {"schemaVersion": 4, "backupId": "br2", "policyId": "p2", "objects": [{"digest": "dd" * 32, "size": 1}]}
    raw2 = (json.dumps(receipt2, indent=2, sort_keys=True) + "\n").encode()
    (target_root / "receipts" / "br2.json").write_bytes(raw2)
    marker2 = {
        "schemaVersion": 4,
        "backupId": "br2",
        "policyId": "p2",
        "receiptDigest": hashlib.sha256(raw2).hexdigest(),
        "targetGeneration": 2,
        "parentBackupId": "br",
    }
    marker2["commitHash"] = backup_publish._commit_hash(marker2)
    (target_root / "commits" / "p" / "s2.json").write_text(json.dumps(marker2, indent=2) + "\n", encoding="utf-8")
    monkeypatch.setattr(backup_retirement, "_receipt_has_valid_retirement_marker", lambda *a, **k: True)
    result2 = backup_control_recovery.rebuild_formal_truth_from_authenticated_commits(target)
    assert result2["retired"] >= 1


def test_read_json_bytes_edges(control_db: Path) -> None:
    assert backup_control_recovery._read_json_bytes(None) is None
    assert backup_control_recovery._read_json_bytes(b"") is None
    assert backup_control_recovery._read_json_bytes(b"[1]") is None
    assert backup_control_recovery._read_json_bytes(b"{") is None
    assert backup_control_recovery._read_json_bytes(b'{"a":1}') == {"a": 1}


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
