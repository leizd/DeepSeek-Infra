"""Target-side writer leases (4.4.6).

The SQLite run lease only protects the local scheduler database; it cannot
constrain side effects on a target that several workers may see. Every
filesystem target carries ``.target-lock/writer.json`` acquired with
``O_EXCL``; remote targets use ``control/writer.json`` with conditional puts
(``If-None-Match: *`` / ``If-Match``). Leases are renewed by the owning run's
heartbeat, preemptible only when expired *and* by a strictly higher fencing
token, and asserted at every visible mutation.

Targets that cannot honor exclusive-create / conditional-write semantics are
rejected explicitly with ``unsupported-atomic-target`` /
``unsupported-conditional-target`` instead of being trusted silently.
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
CLOCK_SKEW_SAFETY_SECONDS = 30


def _retry_permission(operation: Callable[[], Any], *, attempts: int = 10, sleep_seconds: float = 0.05) -> Any:
    """Windows holds delete/replace locks briefly; retry transient PermissionErrors."""
    last: PermissionError | None = None
    for _ in range(attempts):
        try:
            return operation()
        except PermissionError as exc:  # pragma: no cover - platform-specific lock races
            last = exc
            time.sleep(sleep_seconds)
    if last is not None:  # pragma: no cover
        raise last
    raise AppError("unreachable retry state", code=ErrorCode.INTERNAL, status=500)  # pragma: no cover


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
    """Single-writer lease on one target root or remote store.

    ``acquire`` creates ``writer.json`` exclusively or preempts an expired
    lease held by a lower fencing token. ``assert_owned`` re-reads the lease
    and raises 409 whenever ownership, fencing or freshness is lost, so a
    writer that was preempted mid-operation cannot mutate the target again.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        store: Any | None = None,
        target_id: str,
        owner_run_id: str,
        owner_instance_id: str,
        fencing_token: int,
        lease_seconds: int = TARGET_WRITER_LEASE_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if root is None and store is None:
            raise AppError("writer lease requires root or store", code=ErrorCode.INTERNAL, status=500)
        self.root = root
        self.store = store
        self.target_id = target_id
        self.owner_run_id = owner_run_id
        self.owner_instance_id = owner_instance_id
        self.fencing_token = int(fencing_token)
        self.lease_seconds = lease_seconds
        self._clock = clock or (lambda: datetime.now(tz=timezone.utc))
        self.acquired = False
        self._etag: str | None = None
        self._server_skew = timedelta(0)

    @property
    def path(self) -> Path:
        if self.root is None:
            raise AppError("remote writer lease has no local path", code=ErrorCode.INTERNAL, status=500)
        return self.root / ".target-lock" / "writer.json"

    def _now(self) -> datetime:
        return (self._clock().astimezone(timezone.utc) + self._server_skew)

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
        if self.store is not None and self.root is None:
            from deepseek_infra.infra.workspace.backup_target_store import read_json, writer_lease_key

            data = read_json(self.store, writer_lease_key())
            meta = self.store.stat(writer_lease_key())
            self._etag = meta.etag if meta is not None else None
            return data
        try:
            data = _retry_permission(lambda: self.path.read_text(encoding="utf-8"))
            data = json.loads(data)
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write(self, payload: dict[str, Any]) -> None:
        if self.store is not None and self.root is None:
            from deepseek_infra.infra.workspace.backup_target_store import put_json_if_match, writer_lease_key

            if self._etag is None:
                raise AppError("Target writer lease lost: missing etag", code=ErrorCode.INVALID_REQUEST, status=409)
            result = put_json_if_match(self.store, writer_lease_key(), payload, expected_etag=self._etag)
            self._etag = result.etag
            self._note_server_date(result.server_date)
            return
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _retry_permission(lambda: os.replace(tmp, self.path))

    def _note_server_date(self, server_date: str | None) -> None:
        if not server_date:
            return
        try:
            from email.utils import parsedate_to_datetime

            server = parsedate_to_datetime(server_date).astimezone(timezone.utc)
        except (TypeError, ValueError, IndexError):
            try:
                server = datetime.fromisoformat(server_date.replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                return
        local = self._clock().astimezone(timezone.utc)
        skew = server - local
        # Keep a safety margin so we never trust a lease longer than the server believes.
        if skew > timedelta(0):
            self._server_skew = skew
        else:
            self._server_skew = skew - timedelta(seconds=CLOCK_SKEW_SAFETY_SECONDS)

    def _expired(self, payload: dict[str, Any], now: datetime) -> bool:
        return str(payload.get("expiresAt") or "") < _utc_iso(now)

    def acquire(self) -> None:
        if self.store is not None and self.root is None:
            self._acquire_store()
            return
        assert self.root is not None
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

    def _acquire_store(self) -> None:
        from deepseek_infra.infra.workspace.backup_target_store import put_json_if_absent, put_json_if_match, read_json, writer_lease_key

        assert self.store is not None
        caps = self.store.capabilities()
        if not caps.scheduled_backup_ready:
            raise AppError("unsupported-conditional-target: writer lease requires conditional writes", code=ErrorCode.INVALID_REQUEST, status=503)
        key = writer_lease_key()
        for _ in range(8):
            now = self._now()
            payload = self._payload(now)
            try:
                result = put_json_if_absent(self.store, key, payload)
                self._etag = result.etag
                self._note_server_date(result.server_date)
                self.acquired = True
                return
            except AppError as exc:
                if exc.status not in {409, 412}:
                    raise
            existing = read_json(self.store, key)
            meta = self.store.stat(key)
            if existing is not None and not self._expired(existing, now):
                raise AppError("Target writer is busy with another run", code=ErrorCode.INVALID_REQUEST, status=423)
            if existing is not None and int(existing.get("fencingToken") or 0) >= self.fencing_token:
                raise AppError("Target writer is held by a newer or equal fencing token", code=ErrorCode.INVALID_REQUEST, status=423)
            if existing is None or meta is None:
                continue
            try:
                result = put_json_if_match(self.store, key, payload, expected_etag=meta.etag)
            except AppError as exc:
                if exc.status in {409, 412}:
                    continue
                raise
            self._etag = result.etag
            self._note_server_date(result.server_date)
            confirmed = read_json(self.store, key)
            if (
                confirmed is not None
                and str(confirmed.get("ownerRunId") or "") == self.owner_run_id
                and int(confirmed.get("fencingToken") or -1) == self.fencing_token
                and not self._expired(confirmed, self._now())
            ):
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
                if self.store is not None and self.root is None:
                    from deepseek_infra.infra.workspace.backup_target_store import writer_lease_key

                    try:
                        self.store.delete_if_match(writer_lease_key(), expected_etag=self._etag)
                    except AppError:
                        pass
                else:
                    try:
                        _retry_permission(lambda: self.path.unlink(missing_ok=True), attempts=5)
                    except PermissionError:
                        pass
        finally:
            self.acquired = False
            self._etag = None

    def __enter__(self) -> TargetWriterLease:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()
