"""Durable pause/resume/abort state machine for recovery sessions."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace import backup_recovery_state, backup_unattended

TERMINAL_PHASES = frozenset({"complete", "aborted", "rolled-back", "failed"})
UNCERTAIN_TRANSACTION_PHASES = frozenset({"commit-intent", "frontend-committed", "backend-committed", "committing", "recovery-required"})
CONTROLLED_STOP_PHASES = frozenset({"paused", "aborted", "rolled-back", "recovery-required"})
_LOCKS_GUARD = threading.Lock()
_SESSION_LOCKS: dict[str, threading.RLock] = {}


class RecoveryJobStopped(RuntimeError):
    """Internal checkpoint signal used to drain in-flight recovery work."""

    def __init__(self, phase: str) -> None:
        super().__init__(f"Recovery job stopped in phase {phase}")
        self.phase = phase


@contextmanager
def session_lock(session_path: Path) -> Iterator[None]:
    """Serialize recovery session updates across threads and processes."""
    key = str(session_path.resolve())
    with _LOCKS_GUARD:
        thread_lock = _SESSION_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        lock_path = session_path.with_name(".remote-fetch.lock")
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


def request_pause(session: dict[str, Any]) -> None:
    if str(session.get("phase") or "") in TERMINAL_PHASES:
        raise AppError("Terminal recovery job cannot be paused", code=ErrorCode.INVALID_REQUEST, status=409)
    session["pauseRequested"] = True


def request_abort(session: dict[str, Any]) -> None:
    if str(session.get("phase") or "") in TERMINAL_PHASES:
        raise AppError("Terminal recovery job cannot be aborted", code=ErrorCode.INVALID_REQUEST, status=409)
    session["abortRequested"] = True


def _transaction_phase(session: dict[str, Any]) -> str:
    path = Path(str(session.get("transactionPath") or ""))
    if not path.is_file():
        return ""
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "recovery-required"
    return str(payload.get("phase") or "") if isinstance(payload, dict) else "recovery-required"


def _scrub_plaintext_scratch(session: dict[str, Any]) -> None:
    root = Path(str(session.get("scratchRoot") or ""))
    if not root.is_dir():
        return
    for path in root.glob("payload-decrypted-*.zip"):
        backup_unattended.scrub_plaintext_file(path)


def converge(
    session: dict[str, Any],
    *,
    abort_prepared: Callable[[], None] | None = None,
    release: Callable[[], None] | None = None,
) -> str:
    phase = str(session.get("phase") or "")
    if session.get("abortRequested"):
        abort_from_phase = str(session.get("abortFromPhase") or phase)
        session["phase"] = "aborting"
        _scrub_plaintext_scratch(session)
        transaction_phase = _transaction_phase(session)
        if transaction_phase in UNCERTAIN_TRANSACTION_PHASES or abort_from_phase in {"committing", "recovery-required"}:
            session["phase"] = "recovery-required"
            return "recovery-required"
        if transaction_phase:
            if abort_prepared is None:
                session["phase"] = "recovery-required"
                return "recovery-required"
            abort_prepared()
            session["phase"] = "rolled-back"
        else:
            session["phase"] = "aborted"
        session.pop("abortRequested", None)
        session.pop("pauseRequested", None)
        session.pop("abortFromPhase", None)
        if release is not None:
            release()
        return str(session["phase"])
    if session.get("pauseRequested") and phase != "paused":
        session["pausedFromPhase"] = phase
        session["phase"] = "paused"
        return "paused"
    return phase


def resume(session: dict[str, Any]) -> str:
    if str(session.get("phase") or "") != "paused":
        raise AppError("Recovery job is not paused", code=ErrorCode.INVALID_REQUEST, status=409)
    backup_recovery_state.ensure_component_states(session)
    target_phase = str(session.get("pausedFromPhase") or "fetching-selected-components")
    session["phase"] = target_phase
    session.pop("pauseRequested", None)
    session.pop("pausedFromPhase", None)
    return target_phase
