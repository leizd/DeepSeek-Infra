"""Cross-process workspace mutation gate and durable restore fence.

The lock serializes writers across the HTTP server, CLI processes, workers, and
restore recovery.  The fence is separate and durable: a process crash releases
the OS lock, but ordinary mutations remain blocked until restore recovery
reconciles the recorded transaction.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode

_PROCESS_LOCK = threading.RLock()
_GATE_STATE = threading.local()


def _root(root: Path | None = None) -> Path:
    return (root or config.ROOT).resolve()


def lock_path(root: Path | None = None) -> Path:
    return _root(root) / ".workspace-mutation.lock"


def fence_path(root: Path | None = None) -> Path:
    return _root(root) / ".workspace-restore-fence.json"


def generation_path(root: Path | None = None) -> Path:
    return _root(root) / ".workspace-generation"


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def workspace_root_for_path(path: Path) -> Path:
    """Resolve a configured contributor path back to the shared data root."""

    candidate = path.resolve(strict=False)
    configured = (
        config.PROJECTS_DIR,
        config.FILE_CACHE_DIR,
        config.GENERATED_DIR,
        config.MEMORY_DIR,
        config.MEDIA_DIR,
        config.SKILLS_DIR,
        config.AUTOMATION_DIR,
        config.REMINDERS_DIR,
        config.AGENT_RUNS_DIR,
        config.A2A_TASKS_DIR,
        config.TRACE_DIR,
        config.SCHEDULER_DIR,
    )
    for directory in configured:
        resolved = directory.resolve(strict=False)
        if candidate == resolved or resolved in candidate.parents:
            return resolved.parent
    runtime_root = config.ROOT.resolve()
    return runtime_root if candidate == runtime_root or runtime_root in candidate.parents else candidate.parent


def _lock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    import fcntl

    getattr(fcntl, "flock")(handle.fileno(), getattr(fcntl, "LOCK_EX"))


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    getattr(fcntl, "flock")(handle.fileno(), getattr(fcntl, "LOCK_UN"))


@contextmanager
def exclusive_gate(root: Path | None = None) -> Iterator[None]:
    """Hold the workspace mutation lock in this and every peer process."""

    target = lock_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        pass
    else:
        try:
            os.write(descriptor, b"0")
        finally:
            os.close(descriptor)
    with _PROCESS_LOCK:
        depth = int(getattr(_GATE_STATE, "depth", 0))
        active_path = getattr(_GATE_STATE, "path", None)
        if depth:
            if active_path != target:
                raise RuntimeError("Nested workspace mutation gates must use the same root")
            _GATE_STATE.depth = depth + 1
            try:
                yield
            finally:
                _GATE_STATE.depth = depth
            return
        with target.open("r+b") as handle:
            _lock_file(handle)
            _GATE_STATE.depth = 1
            _GATE_STATE.path = target
            try:
                yield
            finally:
                _GATE_STATE.depth = 0
                _GATE_STATE.path = None
                _unlock_file(handle)


def read_fence(root: Path | None = None) -> dict[str, Any] | None:
    target = fence_path(root)
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError(
            "Workspace restore fence is unreadable; recovery is required",
            code=ErrorCode.INVALID_REQUEST,
            status=423,
        ) from exc
    return value if isinstance(value, dict) else None


def assert_mutation_allowed(owner_restore_id: str | None = None, root: Path | None = None) -> None:
    fence = read_fence(root)
    if fence is None:
        return
    if owner_restore_id and owner_restore_id == fence.get("restoreId"):
        return
    raise AppError(
        "Workspace writes are fenced while restore is in progress",
        code=ErrorCode.INVALID_REQUEST,
        status=423,
    )


def write_fence(value: dict[str, Any], root: Path | None = None) -> None:
    target = fence_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        if read_fence(root) != value:
            raise OSError("restore fence read-back mismatch")
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Cleanup is best-effort after the durable replace.  A filesystem
            # cleanup failure must not turn an already committed fence write
            # into a reported transaction failure.
            pass


def clear_fence(restore_id: str, root: Path | None = None) -> bool:
    target = fence_path(root)
    current = read_fence(root)
    if current is None:
        return False
    if current.get("restoreId") != restore_id:
        raise AppError("Restore fence belongs to another transaction", code=ErrorCode.INVALID_REQUEST, status=409)
    target.unlink(missing_ok=True)
    return True


def read_generation(root: Path | None = None) -> int:
    try:
        return max(0, int(generation_path(root).read_text(encoding="ascii").strip()))
    except (FileNotFoundError, OSError, ValueError):
        return 0


def bump_generation(root: Path | None = None) -> int:
    target = generation_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    value = read_generation(root) + 1
    temporary = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with temporary.open("w", encoding="ascii", newline="\n") as handle:
            handle.write(str(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # os.replace() already made the generation durable.  Antivirus or
            # another process may transiently deny removal of a leftover temp.
            pass
    return value


@contextmanager
def mutation_scope(owner_restore_id: str | None = None, root: Path | None = None) -> Iterator[None]:
    """Fence-aware scope for an ordinary or restore-owned workspace mutation."""

    # Fail fast while a long restore owns the OS lock.  The check is repeated
    # under the lock below to close the race with a newly-created fence.
    assert_mutation_allowed(owner_restore_id, root)
    with exclusive_gate(root):
        assert_mutation_allowed(owner_restore_id, root)
        # Advance before touching data as well as after it.  If a process dies
        # mid-write, a concurrent backup still observes a changed generation
        # instead of accepting a package assembled across that crash boundary.
        bump_generation(root)
        try:
            yield
        finally:
            bump_generation(root)
