"""Verified local ciphertext cache for independently encrypted Components."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

CACHE_DIR = config.ROOT / ".backup-component-cache"
READ_CHUNK_BYTES = 1024 * 1024
DEFAULT_QUOTA_BYTES = 20 * 1024 * 1024 * 1024
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _canonical_digest(digest: str) -> str:
    if not isinstance(digest, str):
        raise ValueError("component digest must be canonical sha256")
    if digest.startswith("sha256:"):
        cleaned = digest[7:]
        if len(cleaned) == 64 and all(char in "0123456789abcdef" for char in cleaned.lower()):
            return cleaned.lower()
        return hashlib.sha256(digest.encode("utf-8")).hexdigest()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("component digest must be canonical sha256")
    return digest


def _digest_file(path: Path) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(READ_CHUNK_BYTES):
                size += len(chunk)
                hasher.update(chunk)
    except OSError:
        return 0, ""
    return size, hasher.hexdigest()


@contextmanager
def _process_lock(root: Path, digest: str) -> Iterator[None]:
    lock_path = root / ".locks" / f"{digest}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            getattr(msvcrt, "locking")(handle.fileno(), getattr(msvcrt, "LK_LOCK"), 1)
        else:
            import fcntl

            getattr(fcntl, "flock")(handle.fileno(), getattr(fcntl, "LOCK_EX"))
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                getattr(msvcrt, "locking")(handle.fileno(), getattr(msvcrt, "LK_UNLCK"), 1)
            else:
                import fcntl

                getattr(fcntl, "flock")(handle.fileno(), getattr(fcntl, "LOCK_UN"))


class ComponentCache:
    """Content-addressed cache that re-verifies every returned ciphertext."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else CACHE_DIR

    def _lock_for(self, digest: str) -> threading.RLock:
        key = f"{self.root.resolve()}:{digest}"
        with _LOCKS_GUARD:
            return _LOCKS.setdefault(key, threading.RLock())

    def path_for(self, digest: str) -> Path:
        canonical = _canonical_digest(digest)
        return self.root / "sha256" / canonical[:2] / f"{canonical}.age"

    def partial_path(self, digest: str) -> Path:
        canonical = _canonical_digest(digest)
        return self.path_for(canonical).with_name(f".{canonical}.partial")

    def _pin_path(self, owner_id: str) -> Path:
        owner_hash = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
        return self.root / "pins" / f"{owner_hash}.json"

    def pin(self, owner_id: str, digests: list[str]) -> Path:
        if not owner_id:
            raise ValueError("owner_id must be non-empty")
        canonical = sorted({_canonical_digest(digest) for digest in digests})
        with self._lock_for("cache-control"):
            with _process_lock(self.root, "cache-control"):
                path = self._pin_path(owner_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                payload = {
                    "digests": canonical,
                    "schemaVersion": 1,
                }
                tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
                with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, path)
                return path

    def unpin(self, owner_id: str) -> None:
        if not owner_id:
            return
        with self._lock_for("cache-control"):
            with _process_lock(self.root, "cache-control"):
                self._pin_path(owner_id).unlink(missing_ok=True)

    def reconcile_pins(self) -> dict[str, Any]:
        """Remove cache pins for terminal or deleted recovery sessions."""
        from deepseek_infra.infra.workspace import backup_recovery_keeper

        active_sessions = backup_recovery_keeper.scan_durable_recovery_sessions()
        hash_to_session: dict[str, dict[str, Any]] = {}
        for r_id, session in active_sessions.items():
            h = hashlib.sha256(r_id.encode("utf-8")).hexdigest()
            hash_to_session[h] = session

        terminal_phases = {"complete", "aborted", "failed"}
        retained = 0
        removed = 0
        removed_owners: list[str] = []

        with self._lock_for("cache-control"):
            with _process_lock(self.root, "cache-control"):
                pin_dir = self.root / "pins"
                if not pin_dir.is_dir():
                    return {"reconciled": 0, "retained": 0, "removed": 0, "unpinned": 0, "removedOwners": []}
                for path in sorted(pin_dir.glob("*.json")):
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        values = payload.get("digests") if isinstance(payload, dict) and payload.get("schemaVersion") == 1 else None
                        if not isinstance(values, list) or any(
                            not isinstance(item, str)
                            or len(item) != 64
                            or any(char not in "0123456789abcdef" for char in item)
                            for item in values
                        ):
                            path.unlink(missing_ok=True)
                            removed += 1
                            continue
                    except (OSError, json.JSONDecodeError):
                        path.unlink(missing_ok=True)
                        removed += 1
                        continue

                    owner_hash = path.stem
                    active_session = hash_to_session.get(owner_hash)
                    if active_session is not None:
                        phase = str(active_session.get("phase") or "")
                        if phase in terminal_phases:
                            path.unlink(missing_ok=True)
                            removed += 1
                            removed_owners.append(owner_hash)
                        else:
                            retained += 1
                    else:
                        # Check if this owner_hash was created from a manual pin
                        is_manual = False
                        for d in values or []:
                            if hashlib.sha256(f"manual_{d}".encode("utf-8")).hexdigest() == owner_hash:
                                is_manual = True
                                break
                        if is_manual:
                            retained += 1
                        else:
                            path.unlink(missing_ok=True)
                            removed += 1
                            removed_owners.append(owner_hash)

        return {
            "reconciled": retained + removed,
            "retained": retained,
            "removed": removed,
            "unpinned": removed,
            "removedOwners": removed_owners,
        }

    def _pinned_digests_unlocked(self) -> set[str]:
        pinned: set[str] = set()
        pin_dir = self.root / "pins"
        if not pin_dir.is_dir():
            return pinned
        for path in sorted(pin_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AppError("Component cache pin metadata is invalid", code=ErrorCode.INTERNAL, status=500) from exc
            raw = payload.get("digests") if isinstance(payload, dict) and payload.get("schemaVersion") == 1 else None
            if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
                raise AppError("Component cache pin metadata is invalid", code=ErrorCode.INTERNAL, status=500)
            try:
                pinned.update(_canonical_digest(item) for item in raw)
            except ValueError as exc:
                raise AppError("Component cache pin metadata is invalid", code=ErrorCode.INTERNAL, status=500) from exc
        return pinned

    def pinned_digests(self) -> set[str]:
        with self._lock_for("cache-control"):
            with _process_lock(self.root, "cache-control"):
                return self._pinned_digests_unlocked()

    def gc(self, *, quota_bytes: int = DEFAULT_QUOTA_BYTES) -> dict[str, int]:
        if quota_bytes < 0:
            raise ValueError("quota_bytes must be non-negative")
        with self._lock_for("cache-control"):
            with _process_lock(self.root, "cache-control"):
                sha_dir = self.root / "sha256"
                if not sha_dir.is_dir():
                    return {"beforeBytes": 0, "afterBytes": 0, "evicted": 0, "freedBytes": 0}
                candidates: list[tuple[int, str, Path, int]] = []
                before = 0
                for path in sha_dir.glob("*/*"):
                    if not path.name.endswith(".age") or len(path.name) != 68 or any(char not in "0123456789abcdef" for char in path.name[:64]):
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    before += stat.st_size
                    candidates.append((stat.st_mtime_ns, path.name, path, stat.st_size))
                pinned = self._pinned_digests_unlocked()
                after = before
                freed = 0
                evicted = 0
                for _mtime, name, path, size in sorted(candidates):
                    if after <= quota_bytes:
                        break
                    digest = name[:-4]
                    if digest in pinned:
                        continue
                    try:
                        path.unlink()
                    except OSError:
                        continue
                    after -= size
                    freed += size
                    evicted += 1
                report = {"beforeBytes": before, "afterBytes": after, "evicted": evicted, "freedBytes": freed}
                if after > quota_bytes:
                    report["overQuotaBytes"] = after - quota_bytes
                return report

    def get(self, digest: str, expected_bytes: int) -> Path | None:
        canonical = _canonical_digest(digest)
        with self._lock_for(canonical):
            path = self.path_for(canonical)
            size, observed_digest = _digest_file(path)
            if size == expected_bytes and observed_digest == canonical:
                try:
                    os.utime(path, None)
                except OSError:
                    pass
                return path
            if path.exists():
                path.unlink(missing_ok=True)
            return None

    def inspect(self, digest: str, expected_bytes: int) -> bool:
        """Verify a cache entry without touching LRU state or deleting it."""
        canonical = _canonical_digest(digest)
        if expected_bytes < 0:
            raise ValueError("expected_bytes must be non-negative")
        with self._lock_for(canonical):
            size, observed_digest = _digest_file(self.path_for(canonical))
        return size == expected_bytes and observed_digest == canonical

    def fetch(
        self,
        digest: str,
        expected_bytes: int,
        read_from: Callable[[int], Iterator[bytes]],
        *,
        progress: Callable[[int], None] | None = None,
    ) -> Path:
        canonical = _canonical_digest(digest)
        if expected_bytes < 0:
            raise ValueError("expected_bytes must be non-negative")
        with self._lock_for(canonical):
            with _process_lock(self.root, canonical):
                hit = self.get(canonical, expected_bytes)
                if hit is not None:
                    return hit
                path = self.path_for(canonical)
                partial = self.partial_path(canonical)
                path.parent.mkdir(parents=True, exist_ok=True)
                offset = partial.stat().st_size if partial.is_file() else 0
                if offset > expected_bytes:
                    partial.unlink(missing_ok=True)
                    offset = 0
                hasher = hashlib.sha256()
                if offset:
                    with partial.open("rb") as existing:
                        while chunk := existing.read(READ_CHUNK_BYTES):
                            hasher.update(chunk)
                if offset == expected_bytes and hasher.hexdigest() == canonical:
                    os.replace(partial, path)
                    return path
                mode = "ab" if offset else "wb"
                position = offset
                with partial.open(mode) as handle:
                    try:
                        for raw_piece in read_from(offset):
                            if position >= expected_bytes:
                                break
                            piece = raw_piece[: expected_bytes - position]
                            if not piece:
                                continue
                            handle.write(piece)
                            hasher.update(piece)
                            position += len(piece)
                            if progress is not None:
                                handle.flush()
                                os.fsync(handle.fileno())
                                progress(position)
                    finally:
                        handle.flush()
                        os.fsync(handle.fileno())
                if position < expected_bytes:
                    raise AppError("Component cache download was truncated", code=ErrorCode.INTERNAL, status=500)
                if position != expected_bytes or hasher.hexdigest() != canonical:
                    partial.unlink(missing_ok=True)
                    raise AppError("Component cache digest mismatch", code=ErrorCode.INTERNAL, status=500)
                os.replace(partial, path)
                return path

    def is_pinned(self, digest: str) -> bool:
        canonical = _canonical_digest(digest)
        with self._lock_for("cache-control"):
            with _process_lock(self.root, "cache-control"):
                return canonical in self._pinned_digests_unlocked()


_DEFAULT_CACHE: ComponentCache | None = None
_DEFAULT_CACHE_LOCK = threading.Lock()


def get_default_cache() -> ComponentCache:
    global _DEFAULT_CACHE
    with _DEFAULT_CACHE_LOCK:
        if _DEFAULT_CACHE is None or _DEFAULT_CACHE.root != CACHE_DIR:
            _DEFAULT_CACHE = ComponentCache(CACHE_DIR)
        return _DEFAULT_CACHE


def pin(digest_or_owner: str, digests: list[str] | None = None, *, owner_id: str | None = None) -> Path:
    cache = get_default_cache()
    if digests is not None:
        return cache.pin(digest_or_owner, digests)
    if owner_id is not None:
        return cache.pin(owner_id, [digest_or_owner])
    d = _canonical_digest(digest_or_owner)
    return cache.pin(f"manual_{d}", [digest_or_owner])


def unpin(owner_id: str) -> None:
    get_default_cache().unpin(owner_id)


def is_pinned(digest: str) -> bool:
    return get_default_cache().is_pinned(digest)


def reconcile_pins() -> dict[str, Any]:
    return get_default_cache().reconcile_pins()
