"""Packed delta and persistent snapshot state release contracts."""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.workspace import backup_chunk_engine, backup_incremental, backup_incremental_restore, backup_pack


def _scan(path: Path) -> backup_chunk_engine.FileChunkScan:
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return backup_chunk_engine.FileChunkScan(
        size=len(data),
        sha256=digest,
        protocol=backup_incremental.CURRENT_CDC_PROTOCOL,
        chunks=({"offset": 0, "length": len(data), "sha256": digest},),
        engine="rust",
    )


def _file(path: str, *, sha: str, size: int = 4) -> backup_incremental.FileRecord:
    return backup_incremental.FileRecord("local", path, size, sha * 64)


def _commit_index(
    backup_id: str,
    files: list[backup_incremental.FileRecord],
    *,
    parent: str | None = None,
    chunks: list[backup_incremental.ChunkRecord] | None = None,
) -> None:
    backup_incremental.commit_snapshot_index(
        target_id="target",
        policy_id="policy",
        backup_id=backup_id,
        parent_backup_id=parent,
        base_backup_id="F0",
        chain_depth=0 if parent is None else 1,
        root_digest=backup_incremental.snapshot_root(files),
        files=files,
        chunks=chunks or [],
        logical_bytes=sum(item.size for item in files),
    )


def test_snapshot_index_stores_full_checkpoint_then_only_file_operations(tmp_settings: Path) -> None:
    del tmp_settings
    first = _file("payload/local/a.bin", sha="a")
    unchanged = _file("payload/local/b.bin", sha="b")
    changed = _file("payload/local/b.bin", sha="c")
    added = _file("payload/local/c.bin", sha="d")
    _commit_index("F0", [first, unchanged])
    _commit_index("I1", [first, changed, added], parent="F0")

    with backup_incremental._connect() as connection:
        counts = {
            str(row["backup_id"]): int(row["count"])
            for row in connection.execute(
                "SELECT backup_id, COUNT(*) AS count FROM snapshot_file_ops GROUP BY backup_id"
            )
        }
        assert counts == {"F0": 2, "I1": 2}
        assert connection.execute("SELECT COUNT(*) FROM current_effective_files").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM snapshot_files").fetchone()[0] == 0
        head = connection.execute("SELECT backup_id, root_digest FROM current_effective_heads").fetchone()
        assert head is not None and tuple(head) == ("I1", backup_incremental.snapshot_root([first, changed, added]))

    assert backup_incremental.load_snapshot_files("target", "policy", "F0") == [first, unchanged]
    assert backup_incremental.load_snapshot_files("target", "policy", "I1") == [first, changed, added]
    metrics = backup_incremental.index_metrics("target", "policy")
    assert metrics["snapshotFileOps"] == 2
    assert metrics["effectiveFiles"] == 3
    assert metrics["fileVersions"] == 4
    assert metrics["chunkMaps"] == 0
    assert int(metrics["dbBytes"]) > 0
    assert 0.0 <= float(metrics["freePageRatio"]) <= 1.0
    compacted = backup_incremental.maintain_snapshot_index(
        "target",
        "policy",
        minimum_db_bytes=-1,
        minimum_free_page_ratio=-1.0,
        maximum_pages=1,
    )
    assert compacted["status"] == "compacted"
    assert compacted["after"]["effectiveFiles"] == 3
    assert backup_incremental.load_snapshot_files("target", "policy", "I1") == [first, changed, added]


def test_file_versions_are_shared_across_renames(tmp_settings: Path) -> None:
    del tmp_settings
    original = _file("payload/local/original.bin", sha="e")
    renamed = _file("payload/local/renamed.bin", sha="e")
    _commit_index("F0", [original])
    _commit_index("I1", [renamed], parent="F0")

    with backup_incremental._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM file_versions").fetchone()[0] == 1
        operations = connection.execute(
            "SELECT op, logical_path FROM snapshot_file_ops WHERE backup_id = 'I1' ORDER BY op, logical_path"
        ).fetchall()
    assert [tuple(row) for row in operations] == [
        ("DELETE", original.logical_path),
        ("PUT", renamed.logical_path),
    ]


def test_effective_head_mismatch_forces_full(tmp_settings: Path) -> None:
    del tmp_settings
    file = _file("payload/local/a.bin", sha="f")
    _commit_index("F0", [file])
    with backup_incremental._connect() as connection:
        connection.execute("UPDATE current_effective_heads SET backup_id = 'ghost'")
        connection.commit()

    assert not backup_incremental.index_is_healthy("target", "policy")
    selected = backup_incremental.select_snapshot_plan(
        policy={"incremental": {"mode": "file-delta"}},
        target_id="target",
        policy_id="policy",
        index_available=True,
    )
    assert selected[0] == "full" and selected[6] == "chunk-index-rebuild-failed"


def test_incremental_v5_container_does_not_force_full_from_v4_parent(tmp_settings: Path) -> None:
    del tmp_settings
    file = _file("payload/local/a.bin", sha="1")
    backup_incremental.record_committed_snapshot(
        target_id="target",
        policy_id="policy",
        backup_id="I4",
        parent_backup_id=None,
        base_backup_id="I4",
        chain_depth=0,
        root_digest=backup_incremental.snapshot_root([file]),
        files=[file],
        chunk_protocol=backup_incremental.CURRENT_CDC_PROTOCOL,
    )
    selected = backup_incremental.select_snapshot_plan(
        policy={"incremental": {"mode": "file-delta", "maxChainDepth": 8, "fullIntervalDays": 7}},
        target_id="target",
        policy_id="policy",
        index_available=True,
    )
    assert selected[:4] == ("incremental", "I4", "I4", 1)
    assert selected[6] is None


def test_pack_writer_aligns_ranges_rolls_packs_and_self_verifies(tmp_path: Path) -> None:
    writer = backup_pack.PackWriter(
        tmp_path,
        target_pack_size=32,
        max_pack_size=40,
        alignment=8,
    )
    refs = []
    for value in (b"a" * 10, b"b" * 10, b"c" * 10):
        refs.append(
            writer.append(
                io.BytesIO(value),
                expected_length=len(value),
                expected_sha256=hashlib.sha256(value).hexdigest(),
            )
        )
    index = writer.finalize()

    assert refs == [
        {"kind": "pack-range", "blobId": "blob_000000"},
        {"kind": "pack-range", "blobId": "blob_000001"},
        {"kind": "pack-range", "blobId": "blob_000002"},
    ]
    assert [item["size"] for item in index["packs"]] == [26, 10]
    assert [index["entries"][ref["blobId"]]["offset"] for ref in refs] == [0, 16, 0]
    for pack in index["packs"]:
        path = tmp_path / str(pack["path"])
        assert path.stat().st_size == int(pack["size"])
        assert hashlib.sha256(path.read_bytes()).hexdigest() == pack["sha256"]


def test_pack_writer_rejects_truncated_or_corrupt_sources(tmp_path: Path) -> None:
    writer = backup_pack.PackWriter(tmp_path)
    with pytest.raises(AppError, match="length mismatch"):
        writer.append(io.BytesIO(b"short"), expected_length=6, expected_sha256=hashlib.sha256(b"short").hexdigest())
    with pytest.raises(AppError, match="checksum mismatch"):
        writer.append(io.BytesIO(b"content"), expected_length=7, expected_sha256="0" * 64)
    writer.abort()


def test_pack_ranges_restore_whole_and_cdc_payloads(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    parent_root = tmp_path / "parent"
    output_root = tmp_path / "output"
    parent_root.mkdir()
    output_root.mkdir()
    writer = backup_pack.PackWriter(package_root)
    first = b"hello "
    second = b"packed world"
    first_ref = writer.append(
        io.BytesIO(first), expected_length=len(first), expected_sha256=hashlib.sha256(first).hexdigest()
    )
    second_ref = writer.append(
        io.BytesIO(second), expected_length=len(second), expected_sha256=hashlib.sha256(second).hexdigest()
    )
    writer.finalize()

    with backup_incremental_restore.PackHandleCache(package_root) as cache:
        whole_target = output_root / "whole.bin"
        backup_incremental_restore._materialize_put(
            package_root,
            parent_root,
            whole_target,
            {
                "path": "whole.bin",
                "storage": "whole",
                "size": len(first),
                "sha256": hashlib.sha256(first).hexdigest(),
                "payloadRef": first_ref,
            },
            chunk_protocol=backup_incremental.CURRENT_CDC_PROTOCOL,
            payload_cache=cache,
        )
        cdc_target = output_root / "cdc.bin"
        combined = first + second
        backup_incremental_restore._materialize_cdc(
            parent_root,
            package_root,
            cdc_target,
            {
                "path": "cdc.bin",
                "chunks": [
                    {
                        "source": "payload",
                        "length": len(first),
                        "sha256": hashlib.sha256(first).hexdigest(),
                        "payloadRef": first_ref,
                    },
                    {
                        "source": "payload",
                        "length": len(second),
                        "sha256": hashlib.sha256(second).hexdigest(),
                        "payloadRef": second_ref,
                    },
                ],
            },
            chunk_protocol=backup_incremental.CURRENT_CDC_PROTOCOL,
            payload_cache=cache,
        )
        assert cache.open_handle_count == 1
    assert whole_target.read_bytes() == first
    assert cdc_target.read_bytes() == combined


def test_pack_and_blob_corruption_fail_closed(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    value = b"verified payload"
    digest = hashlib.sha256(value).hexdigest()
    writer = backup_pack.PackWriter(package_root)
    ref = writer.append(io.BytesIO(value), expected_length=len(value), expected_sha256=digest)
    index = writer.finalize()
    pack_path = package_root / str(index["packs"][0]["path"])
    pack_path.write_bytes(b"X" + pack_path.read_bytes()[1:])
    with pytest.raises(AppError, match="pack failed checksum"):
        backup_incremental_restore.PackHandleCache(package_root)

    index["packs"][0]["sha256"] = hashlib.sha256(pack_path.read_bytes()).hexdigest()
    (package_root / backup_pack.PACK_INDEX_PATH).write_text(
        json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with backup_incremental_restore.PackHandleCache(package_root) as cache:
        source = backup_incremental_restore.PackRangePayloadSource(cache, str(ref["blobId"]))
        with pytest.raises(AppError, match="checksum mismatch"):
            source.copy_to(io.BytesIO(), expected_sha256=digest, expected_length=len(value))


def test_pack_reader_lru_keeps_at_most_four_handles(tmp_path: Path) -> None:
    writer = backup_pack.PackWriter(tmp_path, target_pack_size=4, max_pack_size=8, alignment=8)
    values = [bytes([index]) * 4 for index in range(5)]
    refs = [
        writer.append(io.BytesIO(value), expected_length=4, expected_sha256=hashlib.sha256(value).hexdigest())
        for value in values
    ]
    writer.finalize()
    with backup_incremental_restore.PackHandleCache(tmp_path, max_handles=4) as cache:
        for ref, value in zip(refs, values, strict=True):
            output = io.BytesIO()
            backup_incremental_restore.PackRangePayloadSource(cache, str(ref["blobId"])).copy_to(
                output,
                expected_sha256=hashlib.sha256(value).hexdigest(),
                expected_length=4,
            )
            assert output.getvalue() == value
            assert cache.open_handle_count <= 4
        assert cache.open_handle_count == 4


@pytest.mark.slow
def test_hundred_thousand_tiny_payloads_avoid_entry_explosion(tmp_path: Path) -> None:
    writer = backup_pack.PackWriter(tmp_path)
    for index in range(100_000):
        value = index.to_bytes(4, "big")
        writer.append(
            io.BytesIO(value),
            expected_length=4,
            expected_sha256=hashlib.sha256(value).hexdigest(),
        )
    packed = writer.finalize()
    physical_files = [path for path in (tmp_path / "payload" / "packs").iterdir() if path.is_file()]
    assert len(packed["entries"]) == 100_000
    assert len(packed["packs"]) == 1
    assert len(physical_files) == 2  # one pack plus index.json
    assert len(packed["entries"]) - len(packed["packs"]) == 99_999


@pytest.mark.slow
def test_hundred_thousand_file_index_growth_tracks_one_change(tmp_settings: Path) -> None:
    del tmp_settings
    files = [
        backup_incremental.FileRecord("local", f"payload/local/{index:06d}.bin", 4, f"{index:064x}")
        for index in range(100_000)
    ]
    _commit_index("F0", files)
    full_db_bytes = backup_incremental.INDEX_DB.stat().st_size
    changed = [*files[:-1], backup_incremental.FileRecord("local", files[-1].logical_path, 4, "f" * 64)]
    _commit_index("I1", changed, parent="F0")
    delta_db_growth = max(0, backup_incremental.INDEX_DB.stat().st_size - full_db_bytes)

    with backup_incremental._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM snapshot_file_ops WHERE backup_id = 'I1'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM current_effective_files").fetchone()[0] == 100_000
        assert connection.execute("SELECT COUNT(*) FROM snapshot_files").fetchone()[0] == 0
    assert delta_db_growth < max(4096, full_db_bytes // 100)


def test_scan_budget_uses_estimated_working_set_not_logical_file_size() -> None:
    ten_mib = 10 * 1024 * 1024
    assert backup_chunk_engine.scan_working_set_bytes(50 * 1024 * 1024 * 1024) == ten_mib
    assert backup_chunk_engine.scan_working_set_bytes(4096) == 4096
    assert backup_chunk_engine.effective_scan_workers(workers=8, max_in_flight_bytes=25 * 1024 * 1024) == 2


def test_batch_scan_receives_worker_and_working_set_limits(tmp_path: Path) -> None:
    paths = [tmp_path / f"{index}.bin" for index in range(3)]
    for path in paths:
        path.write_bytes(path.name.encode())

    class ConfigurableBatch:
        name = "configured-native"

        def __init__(self) -> None:
            self.config: tuple[int, int] | None = None

        def configure_batch(self, *, workers: int, max_in_flight_bytes: int) -> None:
            self.config = (workers, max_in_flight_bytes)

        def scan_files(self, selected: list[Path], *, protocol: str) -> dict[Path, backup_chunk_engine.FileChunkScan]:
            assert protocol == backup_incremental.CURRENT_CDC_PROTOCOL
            return {path: _scan(path) for path in selected}

        def scan_file(self, path: Path, *, protocol: str) -> backup_chunk_engine.FileChunkScan:
            assert protocol == backup_incremental.CURRENT_CDC_PROTOCOL
            return _scan(path)

    engine = ConfigurableBatch()
    _, telemetry = backup_chunk_engine.scan_files_bounded(
        paths,
        workers=8,
        max_in_flight_bytes=25 * 1024 * 1024,
        engine=engine,
    )
    assert engine.config == (2, 25 * 1024 * 1024)
    assert telemetry["workers"] == 2
    assert telemetry["scanWorkingSetBytes"] == 10 * 1024 * 1024


class _FakeStdout:
    def __init__(self) -> None:
        self.responses: list[str] = []
        self.closed = False
        self.condition = threading.Condition()

    def readline(self) -> str:
        deadline = time.monotonic() + 2
        with self.condition:
            while not self.responses and not self.closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ""
                self.condition.wait(remaining)
            return self.responses.pop(0) if self.responses else ""

    def append(self, response: str) -> None:
        with self.condition:
            self.responses.append(response)
            self.condition.notify_all()

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class _FakeStdin:
    def __init__(self, stdout: _FakeStdout) -> None:
        self.stdout = stdout
        self.pending = ""
        self.closed = False

    def write(self, value: str) -> int:
        self.pending += value
        return len(value)

    def flush(self) -> None:
        lines = self.pending.splitlines()
        self.pending = ""
        for line in reversed(lines):
            request = json.loads(line)
            data = Path(request["path"]).read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            self.stdout.append(
                json.dumps(
                    {
                        "id": request["id"],
                        "size": len(data),
                        "sha256": digest,
                        "protocol": request["protocol"],
                        "chunks": [{"offset": 0, "length": len(data), "sha256": digest}],
                    }
                )
                + "\n"
            )

    def close(self) -> None:
        self.closed = True
        self.stdout.close()


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = _FakeStdout()
        self.stdin = _FakeStdin(self.stdout)
        self.stderr = io.StringIO()
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = -1

    def kill(self) -> None:
        self.returncode = -9


def test_native_batch_reuses_one_streaming_process(tmp_path: Path, monkeypatch: Any) -> None:
    first = tmp_path / "a.bin"
    second = tmp_path / "b.bin"
    first.write_bytes(b"a")
    second.write_bytes(b"bb")
    helper = tmp_path / "deepseek-backup"
    helper.write_bytes(b"helper")
    processes: list[_FakeProcess] = []
    commands: list[list[str]] = []

    def popen(command: list[str], **kwargs: Any) -> _FakeProcess:
        del kwargs
        commands.append(command)
        process = _FakeProcess()
        processes.append(process)
        return process

    monkeypatch.setattr(backup_chunk_engine.subprocess, "Popen", popen)
    engine = backup_chunk_engine.RustChunkEngine(helper)
    engine.configure_batch(workers=2, max_in_flight_bytes=32 * 1024 * 1024)
    assert set(engine.scan_files([first, second])) == {first, second}
    assert set(engine.scan_files([second])) == {second}
    assert commands == [[str(helper), "scan-batch", "--workers", "2"]]
    engine.close()
    assert processes[0].stdin.closed
