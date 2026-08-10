"""Packed delta and persistent snapshot state release contracts."""

from __future__ import annotations

import hashlib
import io
import json
import queue
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

        def scan_file(
            self,
            path: Path,
            *,
            protocol: str = backup_incremental.CURRENT_CDC_PROTOCOL,
        ) -> backup_chunk_engine.FileChunkScan:
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


def test_native_batch_response_timeout_resets_process(tmp_path: Path, monkeypatch: Any) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"a")
    helper = tmp_path / "deepseek-backup"
    helper.write_bytes(b"helper")
    process = _FakeProcess()
    process.stdin.flush = lambda: None  # type: ignore[method-assign]
    monkeypatch.setattr(backup_chunk_engine.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(backup_chunk_engine, "NATIVE_BATCH_RESPONSE_TIMEOUT_SECONDS", 0.01)

    engine = backup_chunk_engine.RustChunkEngine(helper)
    with pytest.raises(AppError, match="invalid output"):
        engine.scan_files([source])
    assert process.stdin.closed
    assert engine._batch_process is None


def _write_pack_fixture(root: Path, *, content: bytes = b"abcdefgh") -> dict[str, Any]:
    pack_path = root / "payload" / "packs" / "0000.pack"
    pack_path.parent.mkdir(parents=True, exist_ok=True)
    pack_path.write_bytes(content)
    index: dict[str, Any] = {
        "schemaVersion": 1,
        "packs": [
            {
                "path": "payload/packs/0000.pack",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "entries": {
            "blob_000000": {
                "pack": "payload/packs/0000.pack",
                "offset": 0,
                "length": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        },
    }
    index_path = root / backup_pack.PACK_INDEX_PATH
    index_path.write_text(json.dumps(index), encoding="utf-8")
    return index


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"kind": "pack-range"},
        {"kind": "standalone"},
        {"kind": "unknown", "path": "payload/files/000000"},
    ],
)
def test_payload_reference_parser_rejects_incomplete_typed_values(value: Any) -> None:
    with pytest.raises(AppError, match="reference is missing or invalid"):
        backup_pack.parse_payload_ref(value)
    assert backup_pack.parse_payload_ref("payload/files/legacy") == ("standalone", "payload/files/legacy")
    assert backup_pack.parse_payload_ref({"kind": "pack-range", "blobId": "blob"}) == ("pack-range", "blob")
    assert backup_pack.parse_payload_ref({"kind": "standalone", "path": "payload/files/blob"}) == (
        "standalone",
        "payload/files/blob",
    )
    assert not backup_pack._is_sha256("g" * 64)


@pytest.mark.parametrize(
    "case",
    [
        "invalid-json",
        "invalid-schema",
        "invalid-containers",
        "invalid-pack-record",
        "invalid-pack-path",
        "duplicate-pack",
        "invalid-pack-size",
        "invalid-pack-digest",
        "missing-pack",
        "invalid-entry-record",
        "invalid-entry-pack",
        "invalid-entry-offset",
        "invalid-entry-length",
        "invalid-entry-digest",
        "overlap",
    ],
)
def test_pack_index_rejects_malformed_or_unverifiable_state(tmp_path: Path, case: str) -> None:
    index = _write_pack_fixture(tmp_path)
    index_path = tmp_path / backup_pack.PACK_INDEX_PATH
    if case == "invalid-json":
        index_path.write_text("{", encoding="utf-8")
    elif case == "invalid-schema":
        index["schemaVersion"] = 2
    elif case == "invalid-containers":
        index["packs"] = {}
    elif case == "invalid-pack-record":
        index["packs"] = [None]
    elif case == "invalid-pack-path":
        index["packs"][0]["path"] = "../0000.pack"
    elif case == "duplicate-pack":
        index["packs"].append(dict(index["packs"][0]))
    elif case == "invalid-pack-size":
        index["packs"][0]["size"] = True
    elif case == "invalid-pack-digest":
        index["packs"][0]["sha256"] = "not-a-digest"
    elif case == "missing-pack":
        (tmp_path / "payload" / "packs" / "0000.pack").unlink()
    elif case == "invalid-entry-record":
        index["entries"] = {"blob": None}
    elif case == "invalid-entry-pack":
        index["entries"]["blob_000000"]["pack"] = "payload/packs/missing.pack"
    elif case == "invalid-entry-offset":
        index["entries"]["blob_000000"]["offset"] = 1
    elif case == "invalid-entry-length":
        index["entries"]["blob_000000"]["length"] = True
    elif case == "invalid-entry-digest":
        index["entries"]["blob_000000"]["sha256"] = "bad"
    elif case == "overlap":
        index["entries"]["blob_000001"] = {
            "pack": "payload/packs/0000.pack",
            "offset": 0,
            "length": 1,
            "sha256": hashlib.sha256(b"a").hexdigest(),
        }
    if case != "invalid-json":
        index_path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(AppError):
        backup_pack.load_pack_index(tmp_path)


@pytest.mark.parametrize(
    ("target", "maximum", "alignment"),
    [(0, 1, 1), (2, 1, 1), (1, 1, 0)],
)
def test_pack_writer_validates_limits(tmp_path: Path, target: int, maximum: int, alignment: int) -> None:
    with pytest.raises(ValueError, match="invalid pack writer limits"):
        backup_pack.PackWriter(tmp_path, target_pack_size=target, max_pack_size=maximum, alignment=alignment)


def test_pack_writer_empty_finalize_idempotency_and_state_guards(tmp_path: Path) -> None:
    writer = backup_pack.PackWriter(tmp_path, target_pack_size=8, max_pack_size=8, alignment=8)
    assert writer._aligned_offset() == 0
    writer._finish_pack()
    index = writer.finalize()
    assert index == {"schemaVersion": 1, "packs": [], "entries": {}}
    assert writer.finalize() is index
    assert writer.delta_files()[0]["path"] == backup_pack.PACK_INDEX_PATH
    with pytest.raises(AppError, match="already finalized"):
        writer.append(io.BytesIO(), expected_length=0, expected_sha256=hashlib.sha256(b"").hexdigest())


@pytest.mark.parametrize("length", [-1, 9])
def test_pack_writer_rejects_out_of_range_blob_lengths(tmp_path: Path, length: int) -> None:
    writer = backup_pack.PackWriter(tmp_path, target_pack_size=8, max_pack_size=8)
    with pytest.raises(AppError, match="exceeds the maximum pack size"):
        writer.append(io.BytesIO(), expected_length=length, expected_sha256=hashlib.sha256(b"").hexdigest())


def test_pack_writer_bounds_overreading_sources_and_tolerates_abort_cleanup_error(tmp_path: Path, monkeypatch: Any) -> None:
    class OverreadingSource:
        def read(self, _size: int) -> bytes:
            return b"abcdef"

    writer = backup_pack.PackWriter(tmp_path, target_pack_size=8, max_pack_size=8)
    ref = writer.append(OverreadingSource(), expected_length=2, expected_sha256=hashlib.sha256(b"ab").hexdigest())  # type: ignore[arg-type]
    assert ref["kind"] == "pack-range"
    monkeypatch.setattr(Path, "unlink", lambda self, **kwargs: (_ for _ in ()).throw(OSError("busy")))
    writer.abort()
    assert writer._handle is None


def test_payload_sources_reject_missing_invalid_and_mismatched_ranges(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"data").hexdigest()
    with pytest.raises(AppError, match="range is missing"):
        backup_incremental_restore.FilePayloadSource(tmp_path / "missing").copy_to(
            io.BytesIO(), expected_sha256=digest, expected_length=4
        )
    source_path = tmp_path / "source.bin"
    source_path.write_bytes(b"data")
    with pytest.raises(AppError, match="length mismatch"):
        backup_incremental_restore.FilePayloadSource(source_path, offset=2).copy_to(
            io.BytesIO(), expected_sha256=digest, expected_length=4
        )
    with pytest.raises(AppError, match="payload path is invalid"):
        backup_incremental_restore._standalone_path(tmp_path, "../escape")
    with pytest.raises(AppError, match="pack index is missing"):
        backup_incremental_restore._payload_source(
            tmp_path, {"kind": "pack-range", "blobId": "blob"}, None
        )


def test_pack_handle_and_range_source_reject_unknown_or_tampered_metadata(tmp_path: Path) -> None:
    index = _write_pack_fixture(tmp_path)
    cache = backup_incremental_restore.PackHandleCache(tmp_path)
    with pytest.raises(AppError, match="blob is missing"):
        cache.entry("missing")
    with pytest.raises(AppError, match="pack path is invalid"):
        cache.handle("../escape.pack")
    with pytest.raises(AppError, match="metadata mismatch"):
        backup_incremental_restore.PackRangePayloadSource(cache, "blob_000000").copy_to(
            io.BytesIO(), expected_sha256="0" * 64, expected_length=8
        )
    assert cache.handle(str(index["packs"][0]["path"])) is cache.handle(str(index["packs"][0]["path"]))
    cache.close()
    assert cache.open_handle_count == 0


def test_verified_range_and_materialized_file_fail_closed_on_short_or_corrupt_data(
    tmp_path: Path, monkeypatch: Any
) -> None:
    with pytest.raises(AppError, match="length mismatch"):
        backup_incremental_restore._copy_verified_range(
            io.BytesIO(b"x"), io.BytesIO(), offset=0, length=2, expected_sha256="", label="short"
        )
    with pytest.raises(AppError, match="checksum mismatch"):
        backup_incremental_restore._copy_verified_range(
            io.BytesIO(b"xx"), io.BytesIO(), offset=0, length=2, expected_sha256="0" * 64, label="bad"
        )
    package = tmp_path / "package"
    parent = tmp_path / "parent"
    target = tmp_path / "target.bin"
    payload = package / "payload" / "files" / "000000"
    payload.parent.mkdir(parents=True)
    payload.write_bytes(b"data")
    parent.mkdir()
    monkeypatch.setattr(backup_incremental_restore, "_sha256_file", lambda path: "0" * 64)
    with pytest.raises(AppError, match="failed checksum after restore"):
        backup_incremental_restore._materialize_put(
            package,
            parent,
            target,
            {
                "path": "target.bin",
                "storage": "whole",
                "size": 4,
                "sha256": hashlib.sha256(b"data").hexdigest(),
                "payloadRef": "payload/files/000000",
            },
            chunk_protocol=backup_incremental.CURRENT_CDC_PROTOCOL,
            payload_cache=None,
        )


def test_materialize_chain_removes_directory_tombstones_before_final_validation(tmp_path: Path) -> None:
    full = tmp_path / "full"
    delta = tmp_path / "delta"
    output = tmp_path / "output"
    logical_path = "payload/local/folder/value.bin"
    data = b"value"
    digest = hashlib.sha256(data).hexdigest()
    full_file = full / logical_path
    full_file.parent.mkdir(parents=True)
    full_file.write_bytes(data)
    record = backup_incremental.FileRecord("local", logical_path, len(data), digest)
    (full / "manifest.json").write_text(
        json.dumps(
            {
                "snapshotKind": "full",
                "files": [
                    {
                        "contributorId": record.contributor_id,
                        "path": record.logical_path,
                        "size": record.size,
                        "sha256": record.sha256,
                    }
                ],
                "snapshot": {"rootDigest": backup_incremental.snapshot_root([record])},
            }
        ),
        encoding="utf-8",
    )
    (delta / "delta").mkdir(parents=True)
    (delta / "manifest.json").write_text(json.dumps({"snapshotKind": "incremental"}), encoding="utf-8")
    (delta / "delta" / "operations.json").write_text(
        json.dumps(
            {
                "parentRootDigest": backup_incremental.snapshot_root([record]),
                "put": [],
                "delete": [{"contributorId": "local", "path": "payload/local/folder"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AppError, match="missing declared file"):
        backup_incremental_restore.materialize_chain([full, delta], output)
    assert not (output / "payload" / "local" / "folder").exists()


def test_chunk_stream_defensive_coverage_checks_fail_closed() -> None:
    with pytest.raises(AppError, match="non-covering ranges"):
        backup_incremental.chunk_stream(io.BytesIO(b"short"), file_size=6)


def test_immutable_file_versions_and_snapshot_state_detect_corruption(tmp_settings: Path, monkeypatch: Any) -> None:
    del tmp_settings
    file = _file("payload/local/a.bin", sha="a")
    with backup_incremental._connect() as connection:
        connection.execute(
            "INSERT INTO file_versions (file_version_id, size, sha256, chunk_map_id) VALUES ('fixed', 1, ?, NULL)",
            ("b" * 64,),
        )
        monkeypatch.setattr(backup_incremental, "file_version_id", lambda **kwargs: "fixed")
        with pytest.raises(AppError, match="Immutable file version conflicts"):
            backup_incremental._store_file_version(connection, file, map_id=None)
        with pytest.raises(AppError, match="Immutable file version conflicts"):
            backup_incremental._store_file_versions(connection, [(file, None)])
        with pytest.raises(AppError, match="missing file version"):
            backup_incremental._records_for_version_state(connection, {("local", "missing"): "absent"})


def test_historical_snapshot_state_replays_put_delete_and_legacy_fallback(tmp_settings: Path) -> None:
    del tmp_settings
    first = _file("payload/local/a.bin", sha="a")
    second = _file("payload/local/b.bin", sha="b")
    _commit_index("F0", [first, second])
    _commit_index("I1", [second], parent="F0")
    with backup_incremental._connect() as connection:
        connection.execute("DELETE FROM current_effective_heads")
        connection.execute("DELETE FROM current_effective_files")
        state = backup_incremental._load_snapshot_version_state(connection, "target", "policy", "I1")
        records = backup_incremental._records_for_version_state(connection, state)
        assert records == [second]

        connection.execute("UPDATE snapshot_lineages SET parent_backup_id = 'F0' WHERE backup_id = 'F0'")
        with pytest.raises(AppError, match="chain cycle"):
            backup_incremental._load_snapshot_version_state(connection, "target", "policy", "F0")


def test_snapshot_state_rejects_put_without_version_and_recovers_legacy_rows(tmp_settings: Path) -> None:
    del tmp_settings
    file = _file("payload/local/a.bin", sha="c")
    _commit_index("F0", [file])
    with backup_incremental._connect() as connection:
        connection.execute("DELETE FROM current_effective_heads")
        connection.execute("DELETE FROM current_effective_files")
        connection.execute("UPDATE snapshot_file_ops SET file_version_id = NULL WHERE backup_id = 'F0'")
        with pytest.raises(AppError, match="PUT is missing a file version"):
            backup_incremental._load_snapshot_version_state(connection, "target", "policy", "F0")

        connection.execute("DELETE FROM snapshot_file_ops")
        connection.execute(
            "INSERT INTO snapshot_files (target_id, policy_id, backup_id, contributor_id, logical_path, size, sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("target", "policy", "F0", file.contributor_id, file.logical_path, file.size, file.sha256),
        )
        state = backup_incremental._load_snapshot_version_state(connection, "target", "policy", "F0")
        assert backup_incremental._records_for_version_state(connection, state) == [file]
        connection.execute("DELETE FROM snapshot_lineages")
        assert backup_incremental._load_snapshot_version_state(connection, "target", "policy", "F0")


def test_current_head_compatibility_and_stale_parent_commit_are_fail_closed(tmp_settings: Path) -> None:
    del tmp_settings
    file = _file("payload/local/a.bin", sha="d")
    with backup_incremental._connect() as connection:
        assert backup_incremental._current_head_matches_latest(connection, "target", "policy")
    _commit_index("F0", [file])
    with backup_incremental._connect() as connection:
        connection.execute("DELETE FROM current_effective_heads")
        assert not backup_incremental._current_head_matches_latest(connection, "target", "policy")
        connection.execute(
            "INSERT INTO snapshot_files (target_id, policy_id, backup_id, contributor_id, logical_path, size, sha256) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("target", "policy", "F0", file.contributor_id, file.logical_path, file.size, file.sha256),
        )
        assert backup_incremental._current_head_matches_latest(connection, "target", "policy")
        connection.execute(
            "INSERT INTO current_effective_heads (target_id, policy_id, backup_id, root_digest) VALUES ('target', 'policy', 'wrong', 'wrong')"
        )
    with pytest.raises(AppError, match="does not match the committed parent"):
        _commit_index("I1", [file], parent="F0")


def test_chunk_state_can_be_attached_after_snapshot_commit_and_queried_historically(tmp_settings: Path) -> None:
    del tmp_settings
    data = b"data"
    digest = hashlib.sha256(data).hexdigest()
    file = backup_incremental.FileRecord("local", "payload/local/a.bin", len(data), digest)
    chunk = backup_incremental.ChunkRecord("local", file.logical_path, 0, 0, len(data), digest)
    _commit_index("F0", [file])
    backup_incremental.record_snapshot_chunks(
        target_id="target", policy_id="policy", backup_id="F0", chunks=[chunk]
    )
    _commit_index("I1", [file], parent="F0")
    with backup_incremental._connect() as connection:
        connection.execute("DELETE FROM current_effective_heads")
        connection.execute("DELETE FROM current_effective_files")
    assert backup_incremental.load_snapshot_chunks("target", "policy", "F0") == [chunk]
    refs = backup_incremental.load_snapshot_chunk_refs("target", "policy", "F0")
    assert refs[(file.contributor_id, file.logical_path)]
    assert backup_incremental.lookup_parent_file_by_digest(
        "target", "policy", "F0", sha256=digest, size=len(data), exclude_path="different"
    ) == file
    matches, metrics = backup_incremental.lookup_parent_chunks_accelerated(
        "target", "policy", "F0", [(digest, len(data))]
    )
    assert matches[(digest, len(data))].logical_path == file.logical_path
    assert metrics["exactHits"] == 1


def test_record_snapshot_chunks_rejects_unknown_file_and_migrates_legacy_rows(tmp_settings: Path) -> None:
    del tmp_settings
    digest = hashlib.sha256(b"data").hexdigest()
    unknown = backup_incremental.ChunkRecord("local", "payload/local/unknown.bin", 0, 0, 4, digest)
    _commit_index("F0", [_file("payload/local/known.bin", sha="e")])
    with pytest.raises(AppError, match="unknown file"):
        backup_incremental.record_snapshot_chunks(
            target_id="target", policy_id="policy", backup_id="F0", chunks=[unknown]
        )

    with backup_incremental._connect() as connection:
        connection.execute("DELETE FROM snapshot_lineages")
        connection.execute(
            "INSERT INTO snapshot_files (target_id, policy_id, backup_id, contributor_id, logical_path, size, sha256) VALUES ('target', 'policy', 'legacy', 'local', ?, 4, ?)",
            (unknown.logical_path, digest),
        )
    backup_incremental.record_snapshot_chunks(
        target_id="target", policy_id="policy", backup_id="legacy", chunks=[unknown]
    )
    assert backup_incremental.load_snapshot_chunks("target", "policy", "legacy") == [unknown]


def test_snapshot_index_maintenance_not_needed_preserves_metrics(tmp_settings: Path) -> None:
    del tmp_settings
    result = backup_incremental.maintain_snapshot_index(
        "target", "policy", minimum_db_bytes=10**12, minimum_free_page_ratio=1.0
    )
    assert result["status"] == "not-needed"
    assert result["before"] == result["after"]


def test_native_batch_lifecycle_handles_reconfiguration_missing_pipes_and_failed_shutdown(
    tmp_path: Path, monkeypatch: Any
) -> None:
    helper = tmp_path / "deepseek-backup"
    helper.write_bytes(b"helper")
    engine = backup_chunk_engine.RustChunkEngine(helper)
    closed: list[bool] = []
    engine._batch_process = _FakeProcess()  # type: ignore[assignment]
    engine._batch_process_workers = 1
    monkeypatch.setattr(engine, "_close_batch_process", lambda: closed.append(True))
    engine.configure_batch(workers=2, max_in_flight_bytes=64 * 1024 * 1024)
    assert closed == [True]

    class MissingPipes:
        stdin = None
        stdout = None
        killed = False

        def kill(self) -> None:
            self.killed = True

    missing = MissingPipes()
    monkeypatch.setattr(backup_chunk_engine.subprocess, "Popen", lambda *args, **kwargs: missing)
    engine = backup_chunk_engine.RustChunkEngine(helper)
    with pytest.raises(AppError, match="no streaming pipes"):
        engine._start_batch_process()
    assert missing.killed

    class Unstoppable(_FakeProcess):
        killed = False

        def wait(self, timeout: float | None = None) -> int:
            raise OSError("wait failed")

        def terminate(self) -> None:
            raise OSError("terminate failed")

        def kill(self) -> None:
            self.killed = True

    unstoppable = Unstoppable()
    engine._batch_process = unstoppable  # type: ignore[assignment]
    engine._close_batch_process()
    assert unstoppable.killed


def test_native_batch_empty_input_and_cancelled_byte_budget(tmp_path: Path) -> None:
    helper = tmp_path / "deepseek-backup"
    helper.write_bytes(b"helper")
    assert backup_chunk_engine.RustChunkEngine(helper).scan_files([]) == {}
    budget = backup_chunk_engine._ByteBudget(1)
    assert budget.acquire(1, None) == 1
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(AppError, match="cancelled"):
        budget.acquire(1, cancelled)
    budget.release(1)


def test_native_batch_write_failure_is_reported_after_response_drain(tmp_path: Path, monkeypatch: Any) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"a")
    helper = tmp_path / "deepseek-backup"
    helper.write_bytes(b"helper")
    process = _FakeProcess()
    digest = hashlib.sha256(b"a").hexdigest()
    process.stdout.append(
        json.dumps(
            {
                "id": 0,
                "size": 1,
                "sha256": digest,
                "protocol": backup_incremental.CURRENT_CDC_PROTOCOL,
                "chunks": [{"offset": 0, "length": 1, "sha256": digest}],
            }
        )
        + "\n"
    )

    class FailingStdin:
        def write(self, value: str) -> int:
            del value
            raise OSError("write failed")

        def flush(self) -> None:
            return None

        def close(self) -> None:
            raise OSError("close failed")

    process.stdin = FailingStdin()  # type: ignore[assignment]
    monkeypatch.setattr(backup_chunk_engine.subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(AppError, match="invalid output"):
        backup_chunk_engine.RustChunkEngine(helper).scan_files([source])


def test_native_batch_bounded_queue_stops_reader_after_timeout(tmp_path: Path, monkeypatch: Any) -> None:
    source = tmp_path / "a.bin"
    source.write_bytes(b"a")
    helper = tmp_path / "deepseek-backup"
    helper.write_bytes(b"helper")
    process = _FakeProcess()
    process.stdin.flush = lambda: None  # type: ignore[method-assign]

    class AlwaysFullQueue(queue.Queue[Any]):
        def put(self, item: Any, block: bool = True, timeout: float | None = None) -> None:
            del item, block, timeout
            raise backup_chunk_engine.queue.Full

    monkeypatch.setattr(backup_chunk_engine.queue, "Queue", AlwaysFullQueue)
    monkeypatch.setattr(backup_chunk_engine.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(backup_chunk_engine, "NATIVE_BATCH_RESPONSE_TIMEOUT_SECONDS", 0.01)
    process.stdout.append("{}\n")
    with pytest.raises(AppError, match="invalid output"):
        backup_chunk_engine.RustChunkEngine(helper).scan_files([source])
