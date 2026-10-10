"""Effective snapshot deduplication and cross-file restore contracts."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import (
    backup_chunk_engine,
    backup_incremental,
    backup_incremental_restore,
    backup_target_s3,
)
from deepseek_infra.infra.workspace.backup_target_store import (
    MemoryTargetStore,
    MultipartUpload,
    ObjectMeta,
    probe_store_capabilities,
)


def _streaming_process(output: str = "") -> SimpleNamespace:
    state: dict[str, int | None] = {"returncode": None}

    def poll() -> int | None:
        return state["returncode"]

    def wait(timeout: float | None = None) -> int:
        del timeout
        state["returncode"] = 0
        return 0

    def terminate() -> None:
        state["returncode"] = -1

    def kill() -> None:
        state["returncode"] = -9

    return SimpleNamespace(
        stdin=io.StringIO(),
        stdout=io.StringIO(output),
        poll=poll,
        wait=wait,
        terminate=terminate,
        kill=kill,
    )


def _file(path: str, *, size: int = 4, sha: str = "a") -> backup_incremental.FileRecord:
    return backup_incremental.FileRecord("local", path, size, sha * 64)


def _chunk(path: str, *, size: int = 4, sha: str = "1") -> backup_incremental.ChunkRecord:
    return backup_incremental.ChunkRecord("local", path, 0, 0, size, sha * 64)


def _commit(
    backup_id: str,
    files: list[backup_incremental.FileRecord],
    chunks: list[backup_incremental.ChunkRecord],
    *,
    parent: str | None = None,
    depth: int = 0,
) -> None:
    backup_incremental.commit_snapshot_index(
        target_id="target",
        policy_id="policy",
        backup_id=backup_id,
        parent_backup_id=parent,
        base_backup_id="F0",
        chain_depth=depth,
        root_digest=backup_incremental.snapshot_root(files),
        files=files,
        chunks=chunks,
        chunk_protocol=backup_incremental.CURRENT_CDC_PROTOCOL,
        logical_bytes=sum(item.size for item in files),
    )


def test_effective_chunk_refs_survive_unchanged_incrementals(tmp_settings: Path) -> None:
    del tmp_settings
    a1 = _file("payload/local/a.bin", sha="a")
    a2 = _file("payload/local/a.bin", sha="c")
    b1 = _file("payload/local/b.bin", sha="b")
    _commit("F0", [a1, b1], [_chunk(a1.logical_path, sha="1"), _chunk(b1.logical_path, sha="2")])
    _commit("I1", [a2, b1], [_chunk(a2.logical_path, sha="3")], parent="F0", depth=1)
    _commit("I2", [a2, b1], [], parent="I1", depth=2)

    refs0 = backup_incremental.load_snapshot_chunk_refs("target", "policy", "F0")
    refs1 = backup_incremental.load_snapshot_chunk_refs("target", "policy", "I1")
    refs2 = backup_incremental.load_snapshot_chunk_refs("target", "policy", "I2")
    assert refs1[("local", b1.logical_path)] == refs0[("local", b1.logical_path)]
    assert refs2 == refs1
    found = backup_incremental.lookup_parent_chunks("target", "policy", "I2", [("2" * 64, 4)])
    assert found[("2" * 64, 4)].logical_path == b1.logical_path
    with backup_incremental._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM chunk_maps").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM snapshot_chunk_refs").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM snapshot_file_ops").fetchone()[0] == 3


def test_snapshot_index_conflict_rolls_back_and_forces_full(tmp_settings: Path) -> None:
    del tmp_settings
    file = _file("payload/local/a.bin")
    chunk = _chunk(file.logical_path)
    _commit("F0", [file], [chunk])
    map_id = backup_incremental.load_snapshot_chunk_refs("target", "policy", "F0")[("local", file.logical_path)]
    with backup_incremental._connect() as connection:
        connection.execute("UPDATE chunk_map_chunks SET chunk_sha256 = ? WHERE chunk_map_id = ?", ("f" * 64, map_id))
        connection.commit()

    with pytest.raises(AppError, match="Immutable chunk map conflicts"):
        _commit("I1", [file], [chunk], parent="F0", depth=1)
    assert backup_incremental.latest_committed_snapshot("target", "policy")["backup_id"] == "F0"  # type: ignore[index]
    assert not backup_incremental.index_is_healthy("target", "policy")
    selected = backup_incremental.select_snapshot_plan(
        policy={"incremental": {"mode": "file-delta"}},
        target_id="target",
        policy_id="policy",
        index_available=True,
    )
    assert selected[0] == "full" and selected[6] == "chunk-index-rebuild-failed"
    _commit("F1", [file], [chunk])
    assert backup_incremental.index_is_healthy("target", "policy")
    assert backup_incremental.latest_committed_snapshot("target", "policy")["backup_id"] == "F1"  # type: ignore[index]


def test_parent_lookup_is_exact_batched_and_immediate_parent_only(tmp_settings: Path) -> None:
    del tmp_settings
    first = _file("payload/local/a.bin", sha="a")
    second = _file("payload/local/b.bin", sha="b")
    shared = "7" * 64
    _commit("F0", [first, second], [_chunk(first.logical_path, sha="7"), _chunk(second.logical_path, sha="7")])
    candidates = [(shared, 4), *[(f"{index:064x}", 4) for index in range(1, 601)]]
    preferred = backup_incremental.lookup_parent_chunks(
        "target", "policy", "F0", candidates, preferred_file=("local", second.logical_path), batch_size=256
    )
    assert preferred[(shared, 4)].logical_path == second.logical_path
    described = backup_incremental.cdc_delta_for_file(
        contributor_id="local",
        logical_path="payload/local/copied.bin",
        file_size=4,
        parent_chunks=[],
        current_chunks=[_chunk("payload/local/copied.bin", sha="7")],
        parent_locations=preferred,
    )
    assert described == [
        {
            "length": 4,
            "sha256": shared,
            "source": "parent-range",
            "parentContributorId": "local",
            "parentPath": second.logical_path,
            "offset": 0,
        }
    ]
    renamed = backup_incremental.lookup_parent_file_by_digest(
        "target", "policy", "F0", sha256=second.sha256, size=second.size, exclude_path=second.logical_path
    )
    assert renamed is None
    copied = backup_incremental.lookup_parent_file_by_digest(
        "target", "policy", "F0", sha256=second.sha256, size=second.size, exclude_path="payload/local/copied.bin"
    )
    assert copied == second
    renamed_file = backup_incremental.FileRecord("local", "payload/local/renamed.bin", second.size, second.sha256)
    _commit("I0", [first, renamed_file], [], parent="F0", depth=1)
    renamed_refs = backup_incremental.load_snapshot_chunk_refs("target", "policy", "I0")
    assert renamed_refs[("local", renamed_file.logical_path)] == backup_incremental.load_snapshot_chunk_refs(
        "target", "policy", "F0"
    )[("local", second.logical_path)]
    _commit("I1", [], [], parent="I0", depth=2)
    assert backup_incremental.lookup_parent_chunks("target", "policy", "I1", [(shared, 4)]) == {}


def test_bloom_is_only_a_negative_accelerator(tmp_settings: Path) -> None:
    del tmp_settings
    file = _file("payload/local/a.bin")
    chunk = _chunk(file.logical_path)
    _commit("F0", [file], [chunk])
    missing = ("9" * 64, 4)
    with patch.object(backup_incremental, "lookup_parent_chunks", wraps=backup_incremental.lookup_parent_chunks) as exact:
        found, metrics = backup_incremental.lookup_parent_chunks_accelerated("target", "policy", "F0", [missing])
    assert found == {} and metrics["bloomNegatives"] == 1 and exact.call_count == 0

    bloom = backup_incremental.parent_chunk_bloom("target", "policy", "F0")
    corrupt = backup_incremental.ParentChunkBloom(bytearray(len(bloom.bits)), bloom.bit_count)
    backup_incremental._bloom_path("target", "policy", "F0").write_bytes(corrupt.to_bytes())
    with patch.object(backup_incremental, "lookup_parent_chunks", wraps=backup_incremental.lookup_parent_chunks) as exact:
        found, metrics = backup_incremental.lookup_parent_chunks_accelerated("target", "policy", "F0", [(chunk.chunk_sha256, 4)])
    assert found == {} and metrics["bloomNegatives"] == 1 and exact.call_count == 0

    positive = backup_incremental.ParentChunkBloom(bytearray([0xFF] * len(bloom.bits)), bloom.bit_count)
    backup_incremental._bloom_path("target", "policy", "F0").write_bytes(positive.to_bytes())
    with patch.object(backup_incremental, "lookup_parent_chunks", return_value={}) as exact:
        found, metrics = backup_incremental.lookup_parent_chunks_accelerated("target", "policy", "F0", [missing])
    assert found == {} and metrics["falsePositives"] == 1 and exact.call_count == 1


def test_index_failure_markers_and_bloom_corruption_are_fail_safe(tmp_settings: Path) -> None:
    del tmp_settings
    file = _file("payload/local/a.bin")
    valid = _chunk(file.logical_path)
    invalid_maps = (
        [backup_incremental.ChunkRecord("local", file.logical_path, 1, 0, 4, "1" * 64)],
        [backup_incremental.ChunkRecord("local", file.logical_path, 0, 0, 4, "short")],
        [backup_incremental.ChunkRecord("local", file.logical_path, 0, 0, 3, "1" * 64)],
    )
    for chunks in invalid_maps:
        with pytest.raises(AppError, match="non-contiguous|invalid digest|does not cover"):
            backup_incremental._validate_chunk_map(file, chunks)
    with pytest.raises(AppError, match="unknown file"):
        _commit("bad", [], [valid])

    invalid_blooms = (
        b"not-a-bloom",
        backup_incremental.BLOOM_MAGIC + b'{"bits":16,"hashes":0}\n00',
        backup_incremental.BLOOM_MAGIC + b"not-json\n00",
    )
    assert all(backup_incremental.ParentChunkBloom.from_bytes(raw) is None for raw in invalid_blooms)

    with (
        patch.object(Path, "write_text", side_effect=OSError("disk unavailable")),
        patch.object(backup_incremental, "_connect", side_effect=sqlite3.OperationalError("database unavailable")),
    ):
        backup_incremental.mark_index_stale("target", "policy", "failure")

    _commit("F0", [file], [valid])
    bloom_path = backup_incremental._bloom_path("target", "policy", "F0")
    bloom_path.unlink(missing_ok=True)
    with (
        patch.object(Path, "read_bytes", side_effect=OSError("cache unavailable")),
        patch.object(Path, "write_bytes", side_effect=OSError("cache unavailable")),
    ):
        rebuilt = backup_incremental.parent_chunk_bloom("target", "policy", "F0")
    assert rebuilt.might_contain(valid.chunk_sha256, valid.length)

    with patch.object(Path, "unlink", side_effect=OSError("cache locked")):
        _commit("F1", [file], [valid])
        collected = backup_incremental.garbage_collect_chunk_maps([("target", "policy", "F0")])
    assert collected["deletedSnapshotRefs"] == 1


def test_broken_legacy_index_migration_marks_every_scope_stale(tmp_settings: Path) -> None:
    del tmp_settings
    unknown = _file("payload/local/unknown.bin", sha="a")
    invalid = _file("payload/local/invalid.bin", sha="b")
    conflicting = _file("payload/local/conflicting.bin", sha="c")
    backup_incremental.record_committed_snapshot(
        target_id="target",
        policy_id="unknown",
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root([unknown]),
        files=[unknown],
        chunk_protocol="future-cdc",
    )
    for policy, file in (("invalid", invalid), ("conflicting", conflicting)):
        backup_incremental.record_committed_snapshot(
            target_id="target",
            policy_id=policy,
            backup_id="F0",
            parent_backup_id=None,
            base_backup_id="F0",
            chain_depth=0,
            root_digest=backup_incremental.snapshot_root([file]),
            files=[file],
            chunk_protocol=backup_incremental.CURRENT_CDC_PROTOCOL,
        )
    conflict_map_id = backup_incremental.chunk_map_id(
        protocol=backup_incremental.CURRENT_CDC_PROTOCOL,
        file_size=conflicting.size,
        file_sha256=conflicting.sha256,
    )
    with sqlite3.connect(backup_incremental.INDEX_DB) as connection:
        connection.execute(
            """
            INSERT INTO snapshot_lineages
            (target_id, policy_id, backup_id, parent_backup_id, base_backup_id, chain_depth,
             root_digest, committed_at, scope_digest, recipient_set_digest, schema_digest,
             chunk_protocol, full_committed_at, logical_bytes)
            VALUES ('target', 'orphan', 'I1', 'missing', 'missing', 1, ?,
                    '2026-01-01T00:00:00Z', '', '', '', ?, NULL, 0)
            """,
            (backup_incremental.snapshot_root([]), backup_incremental.CURRENT_CDC_PROTOCOL),
        )
        connection.executemany(
            """
            INSERT INTO snapshot_files
            (target_id, policy_id, backup_id, contributor_id, logical_path, size, sha256)
            VALUES ('target', ?, 'F0', 'local', ?, 4, ?)
            """,
            (
                ("unknown", unknown.logical_path, unknown.sha256),
                ("invalid", invalid.logical_path, invalid.sha256),
                ("conflicting", conflicting.logical_path, conflicting.sha256),
            ),
        )
        connection.execute("DELETE FROM index_meta WHERE key = ?", (backup_incremental.INDEX_SCHEMA_KEY,))
        connection.execute("DELETE FROM index_meta WHERE key = ?", (backup_incremental.STATE_SCHEMA_KEY,))
        connection.executemany(
            "INSERT INTO snapshot_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ("target", "unknown", "F0", "local", unknown.logical_path, 0, 0, 4, "1" * 64),
                ("target", "invalid", "F0", "local", invalid.logical_path, 0, 0, 3, "2" * 64),
                ("target", "conflicting", "F0", "local", conflicting.logical_path, 0, 0, 4, "3" * 64),
            ),
        )
        connection.execute(
            "INSERT INTO chunk_maps (chunk_map_id, protocol, file_size, file_sha256, chunk_count) VALUES (?, ?, ?, ?, ?)",
            (conflict_map_id, backup_incremental.CURRENT_CDC_PROTOCOL, 4, conflicting.sha256, 1),
        )
        connection.execute(
            "INSERT INTO chunk_map_chunks (chunk_map_id, ordinal, offset, length, chunk_sha256) VALUES (?, 0, 0, 4, ?)",
            (conflict_map_id, "f" * 64),
        )
        connection.commit()

    assert not backup_incremental.index_is_healthy("target", "unknown")
    for policy in ("invalid", "conflicting", "orphan"):
        assert not backup_incremental.index_is_healthy("target", policy)


def test_lineage_cycle_and_missing_parent_are_rejected(tmp_settings: Path) -> None:
    del tmp_settings
    _commit("F0", [], [])
    with backup_incremental._connect() as connection:
        connection.execute(
            "UPDATE snapshot_lineages SET parent_backup_id = 'F0' WHERE target_id = 'target' AND policy_id = 'policy' AND backup_id = 'F0'"
        )
        connection.commit()
    with pytest.raises(AppError, match="cycle"):
        backup_incremental.ancestor_chain("target", "policy", "F0")
    with pytest.raises(AppError, match="missing parent"):
        backup_incremental.ancestor_chain("target", "policy", "missing")

    cycle = {"a": {"backupId": "a", "parentBackupId": "a"}}
    with pytest.raises(AppError, match="cycle"):
        backup_incremental.resolve_lineage_from_receipts(cycle, "a")
    with pytest.raises(AppError, match="missing parent"):
        backup_incremental.resolve_lineage_from_receipts({}, "missing")


def _write_manifest(root: Path, files: list[backup_incremental.FileRecord], *, incremental: bool = False) -> None:
    manifest: dict[str, Any] = {
        "snapshotKind": "incremental" if incremental else "full",
        "files": [
            {"contributorId": item.contributor_id, "path": item.logical_path, "size": item.size, "sha256": item.sha256}
            for item in files
        ],
        "snapshot": {
            "format": "incremental-v4" if incremental else "full-v1",
            "chunkProtocol": backup_incremental.CURRENT_CDC_PROTOCOL,
            "rootDigest": backup_incremental.snapshot_root(files),
        },
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_delta_prepare_uses_immutable_parent_for_file_swaps(tmp_path: Path) -> None:
    f0 = tmp_path / "F0"
    i1 = tmp_path / "I1"
    a_path = "payload/local/a.bin"
    b_path = "payload/local/b.bin"
    a_data, b_data = b"AAAA", b"BBBB"
    (f0 / a_path).parent.mkdir(parents=True)
    (f0 / a_path).write_bytes(a_data)
    (f0 / b_path).write_bytes(b_data)
    f0_files = [
        backup_incremental.FileRecord("local", a_path, 4, hashlib.sha256(a_data).hexdigest()),
        backup_incremental.FileRecord("local", b_path, 4, hashlib.sha256(b_data).hexdigest()),
    ]
    final_files = [
        backup_incremental.FileRecord("local", a_path, 4, hashlib.sha256(b_data).hexdigest()),
        backup_incremental.FileRecord("local", b_path, 4, hashlib.sha256(a_data).hexdigest()),
    ]
    _write_manifest(f0, f0_files)
    _write_manifest(i1, final_files, incremental=True)
    (i1 / "delta").mkdir()
    operations = {
        "parentRootDigest": backup_incremental.snapshot_root(f0_files),
        "rootDigest": backup_incremental.snapshot_root(final_files),
        "delete": [],
        "put": [
            {"contributorId": "local", "path": a_path, "storage": "parent-file", "parentPath": b_path, "size": 4, "sha256": hashlib.sha256(b_data).hexdigest()},
            {"contributorId": "local", "path": b_path, "storage": "parent-file", "parentPath": a_path, "size": 4, "sha256": hashlib.sha256(a_data).hexdigest()},
        ],
    }
    (i1 / "delta" / "operations.json").write_text(json.dumps(operations), encoding="utf-8")
    output = tmp_path / "out"
    backup_incremental_restore.materialize_chain([f0, i1], output)
    assert (output / a_path).read_bytes() == b_data
    assert (output / b_path).read_bytes() == a_data


def test_parent_range_restore_avoids_rechunk_and_survives_parent_delete(tmp_path: Path) -> None:
    f0 = tmp_path / "F0"
    i1 = tmp_path / "I1"
    parent_path = "payload/local/source.bin"
    target_path = "payload/local/copied.bin"
    parent = b"abcdefgh"
    copied = parent[2:6]
    (f0 / parent_path).parent.mkdir(parents=True)
    (f0 / parent_path).write_bytes(parent)
    f0_files = [backup_incremental.FileRecord("local", parent_path, len(parent), hashlib.sha256(parent).hexdigest())]
    final_files = [backup_incremental.FileRecord("local", target_path, len(copied), hashlib.sha256(copied).hexdigest())]
    _write_manifest(f0, f0_files)
    _write_manifest(i1, final_files, incremental=True)
    (i1 / "delta").mkdir()
    operations = {
        "parentRootDigest": backup_incremental.snapshot_root(f0_files),
        "rootDigest": backup_incremental.snapshot_root(final_files),
        "delete": [{"contributorId": "local", "path": parent_path}],
        "put": [
            {
                "contributorId": "local",
                "path": target_path,
                "storage": "cdc",
                "size": len(copied),
                "sha256": hashlib.sha256(copied).hexdigest(),
                "chunks": [
                    {
                        "source": "parent-range",
                        "parentContributorId": "local",
                        "parentPath": parent_path,
                        "offset": 2,
                        "length": len(copied),
                        "sha256": hashlib.sha256(copied).hexdigest(),
                    }
                ],
            }
        ],
    }
    (i1 / "delta" / "operations.json").write_text(json.dumps(operations), encoding="utf-8")
    with patch.object(backup_incremental_restore, "_chunk_ranges_for", side_effect=AssertionError("v4 must not rechunk")):
        backup_incremental_restore.materialize_chain([f0, i1], tmp_path / "out")
    assert (tmp_path / "out" / target_path).read_bytes() == copied
    assert not (tmp_path / "out" / parent_path).exists()


def test_restore_rejects_invalid_ranges_and_keeps_legacy_parent_chunks(tmp_path: Path) -> None:
    parent_root = tmp_path / "parent"
    package_root = tmp_path / "package"
    logical_path = "payload/local/source.bin"
    parent_data = b"abcd"
    parent_path = parent_root / logical_path
    parent_path.parent.mkdir(parents=True)
    parent_path.write_bytes(parent_data)
    package_root.mkdir()

    assert backup_incremental_restore._chunk_protocol({"snapshot": {"format": "incremental-v2"}}) == backup_incremental.CDC_ALGORITHM_V2
    with pytest.raises(AppError, match="missing parent file"):
        backup_incremental_restore._parent_path(parent_root, "payload/local/missing.bin")

    base_put = {
        "path": logical_path,
        "size": len(parent_data),
        "sha256": hashlib.sha256(parent_data).hexdigest(),
    }
    for chunk in (
        {
            "source": "parent-range",
            "parentPath": logical_path,
            "offset": True,
            "length": len(parent_data),
            "sha256": hashlib.sha256(parent_data).hexdigest(),
        },
        {
            "source": "parent-range",
            "parentPath": logical_path,
            "offset": 2,
            "length": len(parent_data),
            "sha256": hashlib.sha256(parent_data).hexdigest(),
        },
    ):
        with pytest.raises(AppError, match="invalid parent range|exceeds its file"):
            backup_incremental_restore._materialize_cdc(
                parent_root,
                package_root,
                tmp_path / "invalid.bin",
                {**base_put, "chunks": [chunk]},
                chunk_protocol=backup_incremental.CURRENT_CDC_PROTOCOL,
            )

    legacy_put = {
        **base_put,
        "chunks": [
            {
                "source": "parent",
                "parentOrdinal": 0,
                "length": len(parent_data),
                "sha256": hashlib.sha256(parent_data).hexdigest(),
            }
        ],
    }
    legacy_target = tmp_path / "legacy.bin"
    backup_incremental_restore._materialize_cdc(
        parent_root,
        package_root,
        legacy_target,
        legacy_put,
        chunk_protocol=backup_incremental.CDC_ALGORITHM_V2,
    )
    assert legacy_target.read_bytes() == parent_data


def test_native_batch_helper_and_fallback_telemetry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"a")
    second.write_bytes(b"bb")
    helper = tmp_path / "deepseek-backup"
    helper.write_bytes(b"helper")
    calls: list[list[str]] = []

    def popen(args: list[str], **kwargs: Any) -> SimpleNamespace:
        del kwargs
        calls.append(args)
        responses = []
        for request_id, path in enumerate((first, second)):
            data = path.read_bytes()
            responses.append(
                json.dumps(
                    {
                        "id": request_id,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "protocol": backup_incremental.CURRENT_CDC_PROTOCOL,
                        "chunks": [{"offset": 0, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
                    }
                )
            )
        return _streaming_process("\n".join(responses) + "\n")

    monkeypatch.setattr(backup_chunk_engine.subprocess, "Popen", popen)
    native = backup_chunk_engine.RustChunkEngine(helper)
    scans = native.scan_files([first, second])
    native.close()
    assert set(scans) == {first, second} and calls == [[str(helper), "scan-batch", "--workers", "1"]]

    def partial_popen(args: list[str], **kwargs: Any) -> SimpleNamespace:
        del args, kwargs
        data = first.read_bytes()
        return _streaming_process(
            "\n".join(
                (
                    json.dumps(
                        {
                            "id": 0,
                            "size": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "protocol": backup_incremental.CURRENT_CDC_PROTOCOL,
                            "chunks": [{"offset": 0, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
                        }
                    ),
                    json.dumps({"id": 1, "error": "scan-failed"}),
                )
            )
            + "\n"
        )

    monkeypatch.setattr(backup_chunk_engine.subprocess, "Popen", partial_popen)
    partial = backup_chunk_engine.FallbackChunkEngine(backup_chunk_engine.RustChunkEngine(helper))
    _, partial_telemetry = backup_chunk_engine.scan_files_bounded(
        [first, second], workers=2, max_in_flight_bytes=1024, engine=partial
    )
    assert partial_telemetry["engine"] == {
        "preferred": "rust",
        "rustFiles": 1,
        "pythonFallbackFiles": 1,
        "fallbackReasons": {"native-error": 1},
        "degraded": True,
    }

    class BrokenBatch:
        def scan_file(self, path: Path, *, protocol: str) -> backup_chunk_engine.FileChunkScan:
            del path, protocol
            raise AppError("native error")

        def scan_files(self, paths: list[Path], *, protocol: str) -> dict[Path, backup_chunk_engine.FileChunkScan]:
            del paths, protocol
            raise AppError("native error")

    fallback = backup_chunk_engine.FallbackChunkEngine(BrokenBatch())  # type: ignore[arg-type]
    _, telemetry = backup_chunk_engine.scan_files_bounded(
        [first, second], workers=2, max_in_flight_bytes=1024, engine=fallback
    )
    assert telemetry["engine"] == {
        "preferred": "rust",
        "rustFiles": 0,
        "pythonFallbackFiles": 2,
        "fallbackReasons": {"native-error": 2},
        "degraded": True,
    }


def test_native_helper_protocol_failures_fall_back_or_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"a")
    second.write_bytes(b"bb")
    helper = tmp_path / "deepseek-backup"
    helper.write_bytes(b"helper")
    rust = backup_chunk_engine.RustChunkEngine(helper)

    with pytest.raises(AppError, match="invalid output"):
        rust._decode({})

    monkeypatch.setattr(backup_chunk_engine.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(AppError, match="helper failed"):
        rust.scan_file(first)

    def failed_popen(*args: Any, **kwargs: Any) -> SimpleNamespace:
        del args, kwargs
        raise backup_chunk_engine.subprocess.SubprocessError("failed")

    monkeypatch.setattr(backup_chunk_engine.subprocess, "Popen", failed_popen)
    with pytest.raises(AppError, match="batch helper failed"):
        rust.scan_files([first, second])

    monkeypatch.setattr(backup_chunk_engine.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="[]"))
    with pytest.raises(AppError, match="invalid output"):
        rust.scan_file(first)
    monkeypatch.setattr(backup_chunk_engine.subprocess, "Popen", lambda *args, **kwargs: _streaming_process("[]\n"))
    with pytest.raises(AppError, match="invalid output"):
        rust.scan_files([first, second])

    invalid_batch_outputs = (
        json.dumps({"id": 3, "error": "bad-id"}),
        json.dumps({"id": 0, "error": "failed"}),
        "not-json",
    )
    for output in invalid_batch_outputs:
        monkeypatch.setattr(backup_chunk_engine.subprocess, "Popen", lambda *args, _output=output, **kwargs: _streaming_process(_output + "\n"))
        with pytest.raises(AppError, match="invalid output|incomplete output"):
            rust.scan_files([first, second])

    def interrupted(*args: Any, **kwargs: Any) -> SimpleNamespace:
        del args, kwargs
        raise backup_chunk_engine.subprocess.SubprocessError("interrupted")

    monkeypatch.setattr(backup_chunk_engine.subprocess, "Popen", interrupted)
    with pytest.raises(AppError, match="batch helper failed"):
        rust.scan_files([first, second])

    class SingleFileNative:
        def scan_file(self, path: Path, *, protocol: str) -> backup_chunk_engine.FileChunkScan:
            scan = backup_chunk_engine.PythonChunkEngine().scan_file(path, protocol=protocol)
            return backup_chunk_engine.FileChunkScan(scan.size, scan.sha256, scan.protocol, scan.chunks, "rust")

    single = backup_chunk_engine.FallbackChunkEngine(SingleFileNative())  # type: ignore[arg-type]
    assert all(scan.engine == "rust" for scan in single.scan_files([first, second]).values())
    unavailable = backup_chunk_engine.FallbackChunkEngine(None)
    assert unavailable.scan_file(first).fallback_reason == "native-unavailable"
    assert unavailable.scan_files([first])[first].fallback_reason == "native-unavailable"


def test_bounded_scan_honours_batch_and_wait_cancellation(tmp_path: Path) -> None:
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(AppError, match="cancelled"):
        backup_chunk_engine.scan_files_bounded(
            [first, second], workers=1, max_in_flight_bytes=1, engine=backup_chunk_engine.FallbackChunkEngine(None), cancel_event=cancelled
        )

    budget = backup_chunk_engine._ByteBudget(1)
    budget.available = 0
    with pytest.raises(AppError, match="cancelled"):
        budget.acquire(2, cancelled)

    checkpoints: list[bool] = []
    backup_chunk_engine.scan_files_bounded(
        [first, second],
        workers=1,
        max_in_flight_bytes=2,
        engine=backup_chunk_engine.FallbackChunkEngine(None),
        checkpoint=lambda: checkpoints.append(True),
    )
    assert checkpoints == [True, True]


class _MultipartConflictClient:
    def __init__(self, *, size: int, sha256: str | None) -> None:
        self.size = size
        self.sha256 = sha256

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise RuntimeError("precondition failed")

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return {"ContentLength": self.size, "ETag": '"etag"', "Metadata": {"sha256": self.sha256} if self.sha256 else {}}


def test_multipart_convergence_requires_exact_digest_and_size() -> None:
    expected = "a" * 64
    upload = MultipartUpload(
        key="objects/value.age",
        upload_id="upload",
        checksum_sha256=expected,
        parts=[{"partNumber": 1, "etag": '"part"', "size": 10}],
        expected_size=10,
    )
    exact = backup_target_s3.S3TargetStore(bucket="b", client=_MultipartConflictClient(size=10, sha256=expected))
    assert exact.complete_multipart_if_absent(upload).created is False
    for client in (
        _MultipartConflictClient(size=10, sha256=None),
        _MultipartConflictClient(size=10, sha256="b" * 64),
        _MultipartConflictClient(size=11, sha256=expected),
    ):
        with pytest.raises(AppError, match="object-integrity-unproven"):
            backup_target_s3.S3TargetStore(bucket="b", client=client).complete_multipart_if_absent(upload)


def test_capability_probe_rejects_lost_multipart_metadata() -> None:
    class MetadataDroppingStore(MemoryTargetStore):
        def stat(self, key: str) -> ObjectMeta | None:
            meta = super().stat(key)
            if meta is not None and key.endswith(".mp.bin"):
                return ObjectMeta(key=meta.key, size=meta.size, etag=meta.etag, sha256=None)
            return meta

    probe = probe_store_capabilities(MetadataDroppingStore())
    assert probe["scheduledBackupReady"] is False
    assert probe["status"] == "object-integrity-unproven"
    assert probe["results"]["multipart-checksum"] == "FAIL"


def test_legacy_chunk_migration_and_reference_gc(tmp_settings: Path) -> None:
    del tmp_settings
    file = _file("payload/local/a.bin")
    backup_incremental.record_committed_snapshot(
        target_id="target",
        policy_id="policy",
        backup_id="F0",
        parent_backup_id=None,
        base_backup_id="F0",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root([file]),
        files=[file],
        chunk_protocol=backup_incremental.CURRENT_CDC_PROTOCOL,
    )
    with backup_incremental._connect() as connection:
        connection.execute(
            """
            INSERT INTO snapshot_files
            (target_id, policy_id, backup_id, contributor_id, logical_path, size, sha256)
            VALUES ('target', 'policy', 'F0', 'local', ?, ?, ?)
            """,
            (file.logical_path, file.size, file.sha256),
        )
        connection.execute("DELETE FROM index_meta WHERE key = ?", (backup_incremental.INDEX_SCHEMA_KEY,))
        connection.execute("DELETE FROM index_meta WHERE key = ?", (backup_incremental.STATE_SCHEMA_KEY,))
        connection.execute(
            "INSERT INTO snapshot_chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("target", "policy", "F0", "local", file.logical_path, 0, 0, 4, "1" * 64),
        )
        connection.commit()
    refs = backup_incremental.load_snapshot_chunk_refs("target", "policy", "F0")
    assert refs[("local", file.logical_path)] == backup_incremental.chunk_map_id(
        protocol=backup_incremental.CURRENT_CDC_PROTOCOL, file_size=4, file_sha256=file.sha256
    )
    with backup_incremental._connect() as connection:
        assert connection.execute("SELECT value FROM index_meta WHERE key = ?", (backup_incremental.STATE_SCHEMA_KEY,)).fetchone()[0] == "3"
        assert connection.execute("SELECT COUNT(*) FROM snapshot_file_ops WHERE backup_id = 'F0'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM current_effective_files").fetchone()[0] == 1
    _commit("I1", [file], [], parent="F0", depth=1)
    first_gc = backup_incremental.garbage_collect_chunk_maps([("target", "policy", "F0")])
    assert first_gc["deletedChunkMaps"] == 0
    second_gc = backup_incremental.garbage_collect_chunk_maps([("target", "policy", "I1")])
    assert second_gc["deletedChunkMaps"] == 1
    with sqlite3.connect(backup_incremental.INDEX_DB) as connection:
        assert connection.execute("SELECT COUNT(*) FROM snapshot_lineages").fetchone()[0] == 0
