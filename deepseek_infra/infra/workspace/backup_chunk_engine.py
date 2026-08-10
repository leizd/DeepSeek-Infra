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
from dataclasses import dataclass, replace
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
    fallback_reason: str | None = None


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


class NativeBatchItemErrors(AppError):
    """Native batch completed, but a bounded subset needs Python fallback."""

    def __init__(self, partial: dict[Path, FileChunkScan], failed: list[Path]) -> None:
        super().__init__("native backup batch helper reported item failures", code=ErrorCode.INTERNAL, status=500)
        self.partial = partial
        self.failed = failed


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

    def _decode(self, payload: dict[str, Any]) -> FileChunkScan:
        try:
            chunks = tuple(dict(item) for item in payload["chunks"])
            return FileChunkScan(
                size=int(payload["size"]),
                sha256=str(payload["sha256"]),
                protocol=str(payload["protocol"]),
                chunks=chunks,
                engine=self.name,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError("native backup chunk helper returned invalid output", code=ErrorCode.INTERNAL, status=500) from exc

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
            if not isinstance(payload, dict):
                raise TypeError("native output is not an object")
            return self._decode(payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise AppError("native backup chunk helper returned invalid output", code=ErrorCode.INTERNAL, status=500) from exc

    def scan_files(self, paths: list[Path], *, protocol: str = backup_incremental.CURRENT_CDC_PROTOCOL) -> dict[Path, FileChunkScan]:
        """Scan many files through one persistent JSONL helper process."""
        requests = [json.dumps({"id": index, "path": str(path), "protocol": protocol}) for index, path in enumerate(paths)]
        try:
            result = subprocess.run(
                [str(self.executable), "scan-batch"],
                input="\n".join(requests) + "\n",
                check=False,
                capture_output=True,
                text=True,
                timeout=3600,
            )
        except subprocess.SubprocessError as exc:
            raise AppError("native backup batch helper failed", code=ErrorCode.INTERNAL, status=500) from exc
        if result.returncode != 0:
            raise AppError("native backup batch helper failed", code=ErrorCode.INTERNAL, status=500)
        decoded: dict[int, FileChunkScan] = {}
        failed: set[int] = set()
        seen: set[int] = set()
        try:
            for line in result.stdout.splitlines():
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ValueError("native batch item is not an object")
                item_id = int(payload["id"])
                if item_id < 0 or item_id >= len(paths) or item_id in seen:
                    raise ValueError("native batch item id is invalid")
                seen.add(item_id)
                if "error" in payload:
                    failed.add(item_id)
                else:
                    decoded[item_id] = self._decode(payload)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AppError("native backup batch helper returned invalid output", code=ErrorCode.INTERNAL, status=500) from exc
        if seen != set(range(len(paths))):
            raise AppError("native backup batch helper returned incomplete output", code=ErrorCode.INTERNAL, status=500)
        if failed:
            raise NativeBatchItemErrors(
                {path: decoded[index] for index, path in enumerate(paths) if index in decoded},
                [path for index, path in enumerate(paths) if index in failed],
            )
        return {path: decoded[index] for index, path in enumerate(paths)}


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
                return replace(self.reference.scan_file(path, protocol=protocol), fallback_reason="native-error")
        return replace(self.reference.scan_file(path, protocol=protocol), fallback_reason="native-unavailable")

    def scan_files(self, paths: list[Path], *, protocol: str = backup_incremental.CURRENT_CDC_PROTOCOL) -> dict[Path, FileChunkScan]:
        if self.native is not None:
            native_batch = getattr(self.native, "scan_files", None)
            if callable(native_batch):
                try:
                    return dict(native_batch(paths, protocol=protocol))
                except NativeBatchItemErrors as exc:
                    results = dict(exc.partial)
                    results.update(
                        {
                            path: replace(self.reference.scan_file(path, protocol=protocol), fallback_reason="native-error")
                            for path in exc.failed
                        }
                    )
                    return results
                except (AppError, OSError, subprocess.SubprocessError):
                    reason = "native-error"
            else:
                return {path: self.scan_file(path, protocol=protocol) for path in paths}
        else:
            reason = "native-unavailable"
        return {path: replace(self.reference.scan_file(path, protocol=protocol), fallback_reason=reason) for path in paths}


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
    batch_scan = getattr(selected, "scan_files", None)
    if callable(batch_scan) and len(paths) > 1:
        for path in paths:
            if cancel_event is not None and cancel_event.is_set():
                raise AppError("Scheduled backup cancelled", code=ErrorCode.INVALID_REQUEST, status=499)
            if checkpoint is not None:
                checkpoint()
        results = dict(batch_scan(paths, protocol=protocol))
    else:
        with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="backup-cdc") as executor:
            futures = {executor.submit(scan_one, path): path for path in paths}
            for future in as_completed(futures):
                results[futures[future]] = future.result()
    elapsed = max(0.000001, time.monotonic() - started)
    logical = sum(item.size for item in results.values())
    rust_files = sum(1 for item in results.values() if item.engine == "rust")
    python_fallback_files = sum(1 for item in results.values() if item.engine == "python" and item.fallback_reason is not None)
    fallback_reasons: dict[str, int] = {}
    for item in results.values():
        if item.fallback_reason:
            fallback_reasons[item.fallback_reason] = fallback_reasons.get(item.fallback_reason, 0) + 1
    return results, {
        "engine": {
            "preferred": "rust" if isinstance(selected, FallbackChunkEngine) and selected.native is not None else selected.name,
            "rustFiles": rust_files,
            "pythonFallbackFiles": python_fallback_files,
            "fallbackReasons": fallback_reasons,
            "degraded": bool(results) and python_fallback_files / len(results) > 0.10,
        },
        "files": len(results),
        "logicalBytes": logical,
        "scanSeconds": round(elapsed, 6),
        "throughputBytesPerSecond": int(logical / elapsed),
        "workers": max(1, workers),
        "maxInFlightBytes": max_in_flight_bytes,
    }
