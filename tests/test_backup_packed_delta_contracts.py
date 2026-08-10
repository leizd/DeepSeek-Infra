"""Packed delta and persistent snapshot state release contracts."""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from pathlib import Path
from typing import Any

from deepseek_infra.infra.workspace import backup_chunk_engine, backup_incremental


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
