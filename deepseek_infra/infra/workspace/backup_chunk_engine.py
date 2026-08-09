"""Pluggable one-pass file hashing and versioned CDC chunking."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol, runtime_checkable

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_incremental


@dataclass(frozen=True, slots=True)
class FileChunkScan:
    size: int
    sha256: str
    protocol: str
    chunks: tuple[dict[str, Any], ...]
    engine: str


@runtime_checkable
class BackupChunkEngine(Protocol):
    name: str

    def scan_file(self, path: Path, *, protocol: str = backup_incremental.CURRENT_CDC_PROTOCOL) -> FileChunkScan: ...


class _HashingReader:
    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        data = self._handle.read(size)
        self.digest.update(data)
        return data


class PythonChunkEngine:
    name = "python"

    def scan_file(self, path: Path, *, protocol: str = backup_incremental.CURRENT_CDC_PROTOCOL) -> FileChunkScan:
        size = path.stat().st_size
        with path.open("rb") as raw:
            reader = _HashingReader(raw)
            chunks = backup_incremental.chunk_stream(reader, file_size=size, protocol=protocol)  # type: ignore[arg-type]
        return FileChunkScan(size=size, sha256=reader.digest.hexdigest(), protocol=protocol, chunks=tuple(chunks), engine=self.name)


def native_helper_path() -> Path | None:
    executable = "deepseek-backup.exe" if os.name == "nt" else "deepseek-backup"
    explicit = os.environ.get("DEEPSEEK_BACKUP_CHUNK_HELPER", "").strip()
    repository = Path(__file__).resolve().parents[3]
    bundle_root = str(getattr(sys, "_MEIPASS", ""))
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if bundle_root:
        candidates.append(Path(bundle_root) / "bin" / executable)
    candidates.extend(
        [
            repository / "bin" / executable,
            repository / "rust" / "target" / "release" / executable,
            repository / "rust" / "target" / "debug" / executable,
        ]
    )
    discovered = shutil.which(executable)
    if discovered:
        candidates.append(Path(discovered))
    return next((item.resolve() for item in candidates if item.is_file()), None)


class RustChunkEngine:
    name = "rust"

    def __init__(self, executable: Path) -> None:
        self.executable = executable

    def scan_file(self, path: Path, *, protocol: str = backup_incremental.CURRENT_CDC_PROTOCOL) -> FileChunkScan:
        result = subprocess.run(
            [str(self.executable), "scan", "--protocol", protocol, str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if result.returncode != 0:
            raise AppError("native backup chunk helper failed", code=ErrorCode.INTERNAL, status=500)
        try:
            payload = json.loads(result.stdout)
            chunks = tuple(dict(item) for item in payload["chunks"])
            return FileChunkScan(
                size=int(payload["size"]),
                sha256=str(payload["sha256"]),
                protocol=str(payload["protocol"]),
                chunks=chunks,
                engine=self.name,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AppError("native backup chunk helper returned invalid output", code=ErrorCode.INTERNAL, status=500) from exc


class FallbackChunkEngine:
    """Prefer Rust but fail safely to the reference engine before publication."""

    name = "rust-with-python-fallback"

    def __init__(self, native: RustChunkEngine | None, reference: PythonChunkEngine | None = None) -> None:
        self.native = native
        self.reference = reference or PythonChunkEngine()

    def scan_file(self, path: Path, *, protocol: str = backup_incremental.CURRENT_CDC_PROTOCOL) -> FileChunkScan:
        if self.native is not None:
            try:
                return self.native.scan_file(path, protocol=protocol)
            except (AppError, OSError, subprocess.SubprocessError):
                pass
        return self.reference.scan_file(path, protocol=protocol)


def default_chunk_engine() -> BackupChunkEngine:
    helper = native_helper_path()
    return FallbackChunkEngine(RustChunkEngine(helper) if helper is not None else None)


class _ByteBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = max(1, maximum)
        self.available = self.maximum
        self.condition = threading.Condition()

    def acquire(self, requested: int, cancel_event: threading.Event | None) -> int:
        amount = min(self.maximum, max(1, requested))
        with self.condition:
            while amount > self.available:
                if cancel_event is not None and cancel_event.is_set():
                    raise AppError("Scheduled backup cancelled", code=ErrorCode.INVALID_REQUEST, status=499)
                self.condition.wait(timeout=0.1)
            self.available -= amount
        return amount

    def release(self, amount: int) -> None:
        with self.condition:
            self.available += amount
            self.condition.notify_all()


def scan_files_bounded(
    paths: list[Path],
    *,
    workers: int,
    max_in_flight_bytes: int,
    protocol: str = backup_incremental.CURRENT_CDC_PROTOCOL,
    engine: BackupChunkEngine | None = None,
    cancel_event: threading.Event | None = None,
    checkpoint: Any | None = None,
) -> tuple[dict[Path, FileChunkScan], dict[str, Any]]:
    """Scan independent files concurrently under byte and descriptor bounds."""
    selected = engine or default_chunk_engine()
    budget = _ByteBudget(max_in_flight_bytes)
    started = time.monotonic()

    def scan_one(path: Path) -> FileChunkScan:
        if cancel_event is not None and cancel_event.is_set():
            raise AppError("Scheduled backup cancelled", code=ErrorCode.INVALID_REQUEST, status=499)
        held = budget.acquire(path.stat().st_size, cancel_event)
        try:
            if checkpoint is not None:
                checkpoint()
            return selected.scan_file(path, protocol=protocol)
        finally:
            budget.release(held)

    results: dict[Path, FileChunkScan] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="backup-cdc") as executor:
        futures = {executor.submit(scan_one, path): path for path in paths}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    elapsed = max(0.000001, time.monotonic() - started)
    logical = sum(item.size for item in results.values())
    return results, {
        "engine": selected.name,
        "files": len(results),
        "logicalBytes": logical,
        "scanSeconds": round(elapsed, 6),
        "throughputBytesPerSecond": int(logical / elapsed),
        "workers": max(1, workers),
        "maxInFlightBytes": max_in_flight_bytes,
    }
