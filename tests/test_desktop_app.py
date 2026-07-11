from __future__ import annotations

import sys
import io
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import deepseek_infra.desktop_app as desktop_app


def test_desktop_app_starts_webview_and_shuts_down() -> None:
    handle = SimpleNamespace(computer_url="http://127.0.0.1:8000/?token=abc")

    with (
        patch.object(desktop_app, "prepare_and_start", return_value=handle) as prepare,
        patch.object(desktop_app, "wait_for_server_ready") as wait_ready,
        patch.object(desktop_app, "open_app_window") as open_window,
        patch.object(desktop_app, "shutdown_handle") as shutdown,
    ):
        assert desktop_app.main() == 0

    prepare.assert_called_once_with(host="127.0.0.1", serve=True)
    wait_ready.assert_called_once_with("http://127.0.0.1:8000/?token=abc&desktop=1")
    open_window.assert_called_once_with("http://127.0.0.1:8000/?token=abc&desktop=1")
    shutdown.assert_called_once_with(handle)


def test_desktop_app_shuts_down_after_window_error() -> None:
    handle = SimpleNamespace(computer_url="http://127.0.0.1:8000/?token=abc")

    with (
        patch.object(desktop_app, "prepare_and_start", return_value=handle),
        patch.object(desktop_app, "open_app_window", side_effect=RuntimeError("boom")),
        patch.object(desktop_app, "show_startup_error") as show_error,
        patch.object(desktop_app, "shutdown_handle") as shutdown,
    ):
        assert desktop_app.main() == 1

    show_error.assert_called_once()
    shutdown.assert_called_once_with(handle)


def test_desktop_app_shuts_down_when_server_never_becomes_ready() -> None:
    handle = SimpleNamespace(computer_url="http://127.0.0.1:8000/?token=abc")

    with (
        patch.object(desktop_app, "prepare_and_start", return_value=handle),
        patch.object(desktop_app, "wait_for_server_ready", side_effect=RuntimeError("not ready")),
        patch.object(desktop_app, "open_app_window") as open_window,
        patch.object(desktop_app, "show_startup_error") as show_error,
        patch.object(desktop_app, "shutdown_handle") as shutdown,
    ):
        assert desktop_app.main() == 1

    open_window.assert_not_called()
    show_error.assert_called_once()
    shutdown.assert_called_once_with(handle)


def test_open_app_window_uses_pywebview() -> None:
    fake_webview = ModuleType("webview")
    calls: list[tuple[Any, ...]] = []

    def create_window(*args: object, **kwargs: object) -> None:
        calls.append(("create_window", args, kwargs))

    def start(*args: object, **kwargs: object) -> None:
        calls.append(("start", args, kwargs))

    fake_webview.create_window = create_window  # type: ignore[attr-defined]
    fake_webview.start = start  # type: ignore[attr-defined]

    with patch.dict(sys.modules, {"webview": fake_webview}):
        desktop_app.open_app_window("http://127.0.0.1:8000/")

    assert calls[0][0] == "create_window"
    assert calls[0][1][0] == "DeepSeek Infra"
    assert calls[0][1][1] == "http://127.0.0.1:8000/"
    assert calls[1] == ("start", (), {"debug": False, "private_mode": False})


def test_webview_entry_url_marks_desktop_handshake() -> None:
    assert desktop_app.webview_entry_url("http://127.0.0.1:8000/?token=abc") == "http://127.0.0.1:8000/?token=abc&desktop=1"
    assert desktop_app.webview_entry_url("http://127.0.0.1:8000/?token=abc&desktop=1") == "http://127.0.0.1:8000/?token=abc&desktop=1"


def test_open_app_window_reports_missing_dependency() -> None:
    with patch.dict(sys.modules, {"webview": None}):
        try:
            desktop_app.open_app_window("http://127.0.0.1:8000/")
        except RuntimeError as exc:
            assert "dependency is missing" in str(exc)
        else:
            raise AssertionError("missing webview should fail")


def test_wait_for_server_ready_retries_status_and_network_errors() -> None:
    responses = [RuntimeError("offline"), SimpleNamespace(status=503), SimpleNamespace(status=204)]

    class Response:
        def __init__(self, value: Any) -> None:
            self.value = value
            self.status = getattr(value, "status", 0)

        def __enter__(self) -> "Response":
            if isinstance(self.value, BaseException):
                raise self.value
            return self

        def __exit__(self, *args: object) -> None:
            return None

    with (
        patch.object(desktop_app, "urlopen", side_effect=lambda *_args, **_kwargs: Response(responses.pop(0))),
        patch.object(desktop_app.time, "sleep"),
    ):
        desktop_app.wait_for_server_ready("http://127.0.0.1:8000/", timeout_seconds=10)
    assert responses == []


def test_wait_for_server_ready_timeout_reports_last_error() -> None:
    clock = iter([0.0, 0.1, 0.2, 0.3])
    with (
        patch.object(desktop_app.time, "monotonic", side_effect=lambda: next(clock)),
        patch.object(desktop_app, "urlopen", side_effect=OSError("refused")),
        patch.object(desktop_app.time, "sleep"),
    ):
        try:
            desktop_app.wait_for_server_ready("http://127.0.0.1:8000/", timeout_seconds=0.15)
        except RuntimeError as exc:
            assert "refused" in str(exc)
        else:
            raise AssertionError("timeout should fail")


def test_show_startup_error_uses_stderr() -> None:
    stream = io.StringIO()
    with patch.object(desktop_app.sys, "stderr", stream):
        desktop_app.show_startup_error(RuntimeError("boom"))
    assert "failed to start: boom" in stream.getvalue()
