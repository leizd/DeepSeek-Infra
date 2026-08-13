"""Verified local ciphertext cache for independently encrypted Components."""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

CACHE_DIR = config.ROOT / ".backup-component-cache"
READ_CHUNK_BYTES = 1024 * 1024
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
