from __future__ import annotations

from pathlib import Path
from collections.abc import Iterator

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.browser import controller, session


@pytest.fixture(autouse=True)
def reset_browser_sessions() -> Iterator[None]:
    session.reset_sessions_for_tests()
    controller._controllers.clear()
    yield
    session.reset_sessions_for_tests()
    controller._controllers.clear()


def test_unknown_and_already_closed_sessions_are_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session.config, "BROWSER_PROFILES_DIR", tmp_path)
    unknown = session.close_session("browser-unknown")
    assert unknown.status == "closed"

    created = session.create_session(engine="static")
    closed = session.close_session(created.browser_session_id)
    assert closed.status == "closed"
    assert session.close_session(created.browser_session_id) is closed
    with pytest.raises(AppError, match="not found"):
        session.get_session(created.browser_session_id)


def test_session_validation_failure_and_mark_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session.config, "BROWSER_PROFILES_DIR", tmp_path)
    created = session.create_session(headless=False, engine="static")
    with pytest.raises(AppError, match="Invalid browser session status"):
        created.touch(status="unknown")

    session.mark_failed(created, "browser process crashed")
    assert created.status == "failed"
    assert created.controller_kind == "failed:browser process crashed"


def test_expired_session_cleanup_continues_after_close_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session.config, "BROWSER_PROFILES_DIR", tmp_path)
    monkeypatch.setattr(session.config, "BROWSER_SESSION_TTL_SECONDS", 30)
    first = session.create_session(engine="static")
    second = session.create_session(engine="static")
    first.last_access = 0
    second.last_access = 0
    original = session.close_session

    def close_with_one_error(session_id: str, *, remove_profile: bool = True) -> session.BrowserSession:
        if session_id == first.browser_session_id:
            raise AppError("close failed")
        return original(session_id, remove_profile=remove_profile)

    monkeypatch.setattr(session, "close_session", close_with_one_error)
    assert session.close_expired_sessions(now=1000) == 1
    assert second.status == "closed"


def test_controller_registry_reuses_and_closes_controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session.config, "BROWSER_PROFILES_DIR", tmp_path)
    created = session.create_session(engine="static")
    first = controller.controller_for(created)
    second = controller.controller_for(created)
    assert first is second
    assert created.controller_kind == "static_fallback"

    closed: list[bool] = []
    first.close = lambda: closed.append(True)  # type: ignore[method-assign]
    controller.close_controller(created.browser_session_id)
    controller.close_controller(created.browser_session_id)
    assert closed == [True]
