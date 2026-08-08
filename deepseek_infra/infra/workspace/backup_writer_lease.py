"""Target-side writer leases (4.4.5).

The SQLite run lease only protects the local scheduler database; it cannot
constrain side effects on a target disk that several workers may see. Every
target therefore carries ``.target-lock/writer.json``: a single-writer lease
acquired with ``O_EXCL``, renewed by the owning run's heartbeat, preemptible
only when expired *and* by a strictly higher fencing token, and asserted at
every visible mutation — commit markers, catalog appends, pin/unpin, scrub
records, retention moves, trash restores and catalog rebuilds.

Targets whose filesystem cannot honor exclusive-create and atomic-rename
semantics are rejected explicitly with ``unsupported-atomic-target`` instead of
being trusted silently.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode

TARGET_WRITER_LEASE_SECONDS = 300


def _retry_permission(operation: Callable[[], Any], *, attempts: int = 10, sleep_seconds: float = 0.05) -> Any:
    """Windows holds delete/replace locks briefly; retry transient PermissionErrors."""
    last: PermissionError | None = None
    for _ in range(attempts):
        try:
            return operation()
        except PermissionError as exc:
            last = exc
            time.sleep(sleep_seconds)
    if last is not None:
        raise last
    raise AppError("unreachable retry state", code=ErrorCode.INTERNAL, status=500)


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(tz=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def probe_atomic_target(root: Path) -> None:
    """Raise ``unsupported-atomic-target`` (503) when O_EXCL/rename semantics are missing."""
    lock_dir = root / ".target-lock"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        probe = lock_dir / f".probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        renamed = probe.with_name(probe.name + ".renamed")
        try:
            fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            os.close(fd)
            try:
                os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                pass
            else:
                raise AppError("unsupported-atomic-target: exclusive create is not honored", code=ErrorCode.INVALID_REQUEST, status=503)
            os.replace(probe, renamed)
            if probe.exists() or not renamed.is_file():
                raise AppError("unsupported-atomic-target: atomic rename is not honored", code=ErrorCode.INVALID_REQUEST, status=503)
        finally:
            probe.unlink(missing_ok=True)
            renamed.unlink(missing_ok=True)
    except AppError:
        raise
    except OSError as exc:
        raise AppError(f"unsupported-atomic-target: {exc}", code=ErrorCode.INVALID_REQUEST, status=503) from exc


class TargetWriterLease:
    """Single-writer lease on one target root.

    ``acquire`` creates ``writer.json`` exclusively or preempts an expired
    lease held by a lower fencing token. ``assert_owned`` re-reads the lease
    file and raises 409 whenever ownership, fencing or freshness is lost, so a
    writer that was preempted mid-operation cannot mutate the target again.
    """

    def __init__(
        self,
        root: Path,
        *,
        target_id: str,
        owner_run_id: str,
        owner_instance_id: str,
        fencing_token: int,
        lease_seconds: int = TARGET_WRITER_LEASE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = root
        self.target_id = target_id
        self.owner_run_id = owner_run_id
        self.owner_instance_id = owner_instance_id
        self.fencing_token = int(fencing_token)
        self.lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self.acquired = False

    @property
    def path(self) -> Path:
        return self.root / ".target-lock" / "writer.json"

    def _now(self) -> datetime:
        return self._clock().astimezone(timezone.utc)

    def _payload(self, acquired_at: datetime) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "targetId": self.target_id,
            "ownerRunId": self.owner_run_id,
            "ownerInstanceId": self.owner_instance_id,
            "fencingToken": self.fencing_token,
            "acquiredAt": _utc_iso(acquired_at),
            "expiresAt": _utc_iso(acquired_at + timedelta(seconds=self.lease_seconds)),
        }

    def _read(self) -> dict[str, Any] | None:
        try:
            data = _retry_permission(lambda: self.path.read_text(encoding="utf-8"))
            data = json.loads(data)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write(self, payload: dict[str, Any]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _retry_permission(lambda: os.replace(tmp, self.path))

    def _expired(self, payload: dict[str, Any], now: datetime) -> bool:
        return str(payload.get("expiresAt") or "") < _utc_iso(now)

    def acquire(self) -> None:
        probe_atomic_target(self.root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Retry the exclusive-create path after preemption or a vanished lease so two
        # waiters cannot both believe they own writer.json after a concurrent release.
        for _ in range(8):
            now = self._now()
            payload = self._payload(now)
            content = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            try:
                fd = _retry_permission(lambda: os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
            except FileExistsError:
                existing = self._read()
                if existing is not None and not self._expired(existing, now):
                    raise AppError("Target writer is busy with another run", code=ErrorCode.INVALID_REQUEST, status=423)
                if existing is not None and int(existing.get("fencingToken") or 0) >= self.fencing_token:
                    raise AppError("Target writer is held by a newer or equal fencing token", code=ErrorCode.INVALID_REQUEST, status=423)
                if existing is None:
                    # Holder released between O_EXCL and read — retry exclusive create.
                    continue
                # Expired + lower token: publish our claim then confirm we still own it.
                self._write(payload)
                confirmed = self._read()
                if (
                    confirmed is not None
                    and str(confirmed.get("ownerRunId") or "") == self.owner_run_id
                    and int(confirmed.get("fencingToken") or -1) == self.fencing_token
                    and not self._expired(confirmed, self._now())
                ):
                    self.acquired = True
                    return
                continue
            except PermissionError:
                raise AppError("Target writer is busy with another run", code=ErrorCode.INVALID_REQUEST, status=423) from None
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return
        raise AppError("Target writer is busy with another run", code=ErrorCode.INVALID_REQUEST, status=423)

    def renew(self) -> None:
        existing = self._read()
        self._assert_payload_owned(existing)
        self._write(self._payload(self._now()))
        self.assert_owned()

    def _assert_payload_owned(self, existing: dict[str, Any] | None) -> None:
        if existing is None:
            raise AppError("Target writer lease lost: writer.json is missing or unreadable", code=ErrorCode.INVALID_REQUEST, status=409)
        if str(existing.get("ownerRunId") or "") != self.owner_run_id or str(existing.get("ownerInstanceId") or "") != self.owner_instance_id or int(existing.get("fencingToken") or -1) != self.fencing_token:
            raise AppError("Target writer lease lost to another writer", code=ErrorCode.INVALID_REQUEST, status=409)

    def assert_owned(self) -> None:
        existing = self._read()
        self._assert_payload_owned(existing)
        if self._expired(existing or {}, self._now()):
            raise AppError("Target writer lease expired", code=ErrorCode.INVALID_REQUEST, status=409)

    def release(self) -> None:
        try:
            existing = self._read()
            if existing is not None and str(existing.get("ownerRunId") or "") == self.owner_run_id and int(existing.get("fencingToken") or -1) == self.fencing_token:
                try:
                    _retry_permission(lambda: self.path.unlink(missing_ok=True), attempts=5)
                except PermissionError:
                    pass
        finally:
            self.acquired = False

    def __enter__(self) -> TargetWriterLease:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
