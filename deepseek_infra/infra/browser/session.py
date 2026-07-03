"""Browser session registry and schema helpers."""

from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deepseek_infra.core import config
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.workspace.schema import new_id, utc_now, validate_project_id, validate_workspace_id

BROWSER_SESSION_STATUSES = {"idle", "running", "closed", "failed"}
_sessions: dict[str, "BrowserSession"] = {}
_lock = threading.RLock()


@dataclass(slots=True)
class BrowserSession:
    browser_session_id: str
    project_id: str = ""
    status: str = "idle"
    current_url: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    headless: bool = True
    engine: str = "playwright"
    profile_dir: str = ""
    controller_kind: str = "unstarted"
    last_access: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "browserSessionId": self.browser_session_id,
            "projectId": self.project_id,
            "status": self.status,
            "currentUrl": self.current_url,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "headless": self.headless,
            "engine": self.engine,
            "controller": self.controller_kind,
        }

    def touch(self, *, status: str | None = None, current_url: str | None = None, controller_kind: str | None = None) -> None:
        if status is not None:
            if status not in BROWSER_SESSION_STATUSES:
                raise AppError("Invalid browser session status", code=ErrorCode.INVALID_PAYLOAD, status=400)
            self.status = status
        if current_url is not None:
            self.current_url = str(current_url or "")
        if controller_kind is not None:
            self.controller_kind = str(controller_kind or "")
        self.updated_at = utc_now()
        self.last_access = time.time()


def create_session(*, project_id: str = "", headless: bool | None = None, engine: str = "playwright") -> BrowserSession:
    close_expired_sessions()
    safe_project_id = validate_project_id(project_id) if project_id else ""
    session_id = new_id("browser")
    profiles_dir().mkdir(parents=True, exist_ok=True)
    session = BrowserSession(
        browser_session_id=session_id,
        project_id=safe_project_id,
        headless=config.BROWSER_HEADLESS if headless is None else bool(headless),
        engine=str(engine or "playwright"),
        profile_dir=str(profiles_dir() / session_id),
    )
    with _lock:
        _sessions[session_id] = session
    return session


def get_session(session_id: str) -> BrowserSession:
    close_expired_sessions()
    safe_id = validate_browser_session_id(session_id)
    with _lock:
        session = _sessions.get(safe_id)
    if session is None or session.status == "closed":
        raise AppError("Browser session not found", code=ErrorCode.NOT_FOUND, status=404)
    session.touch()
    return session


def list_sessions() -> list[dict[str, Any]]:
    close_expired_sessions()
    with _lock:
        return [session.to_dict() for session in sorted(_sessions.values(), key=lambda item: item.updated_at, reverse=True)]


def close_session(session_id: str, *, remove_profile: bool = True) -> BrowserSession:
    safe_id = validate_browser_session_id(session_id)
    with _lock:
        session = _sessions.get(safe_id)
    if session is None:
        raise AppError("Browser session not found", code=ErrorCode.NOT_FOUND, status=404)
    try:
        from deepseek_infra.infra.browser.controller import close_controller

        close_controller(safe_id)
    finally:
        session.touch(status="closed")
        if remove_profile:
            try:
                shutil.rmtree(Path(session.profile_dir), ignore_errors=True)
            except OSError:
                pass
    return session


def close_expired_sessions(now: float | None = None) -> int:
    ttl = max(30, int(config.BROWSER_SESSION_TTL_SECONDS or 1_800))
    cutoff = (now if now is not None else time.time()) - ttl
    with _lock:
        expired = [session.browser_session_id for session in _sessions.values() if session.status not in {"closed", "failed"} and session.last_access < cutoff]
    count = 0
    for session_id in expired:
        try:
            close_session(session_id)
            count += 1
        except AppError:
            continue
    return count


def mark_failed(session: BrowserSession, message: str = "") -> None:
    session.touch(status="failed")
    if message:
        session.controller_kind = f"failed:{message[:120]}"


def validate_browser_session_id(value: str) -> str:
    return validate_workspace_id(value, label="browser session id")


def profiles_dir() -> Path:
    return config.BROWSER_PROFILES_DIR


def reset_sessions_for_tests() -> None:
    with _lock:
        ids = list(_sessions)
    for session_id in ids:
        try:
            close_session(session_id)
        except AppError:
            pass
    with _lock:
        _sessions.clear()
