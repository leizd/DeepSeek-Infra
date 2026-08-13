"""Verified local ciphertext cache for independently encrypted Components."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

CACHE_DIR = config.ROOT / ".backup-component-cache"
READ_CHUNK_BYTES = 1024 * 1024
DEFAULT_QUOTA_BYTES = 20 * 1024 * 1024 * 1024
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _canonical_digest(digest: str) -> str:
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
                payload = {"digests": canonical, "schemaVersion": 1}
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
                pattern = re.compile(r"^[0-9a-f]{64}\.age$")
                candidates: list[tuple[int, str, Path, int]] = []
                before = 0
                object_root = self.root / "sha256"
                if object_root.is_dir():
                    for path in object_root.glob("*/*.age"):
                        if not pattern.fullmatch(path.name):
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
