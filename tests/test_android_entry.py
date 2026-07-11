from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import deepseek_infra.android_entry as android_entry


def test_dependency_versions_reports_android_server_runtime() -> None:
    versions = android_entry.dependency_versions()

    assert set(versions) == {"fastapi", "pydantic", "uvicorn"}
    assert all(versions.values())


def test_configure_android_environment_sets_private_runtime_paths(tmp_path: Path) -> None:
    with patch.dict(os.environ, {}, clear=True):
        payload = android_entry.configure_android_environment(
            str(tmp_path),
            port=8123,
            api_key="sk-local",
            tavily_api_key="tvly-local",
            auth_disabled=True,
        )

        assert payload["root"] == str(tmp_path.resolve())
        assert payload["port"] == "8123"
        assert os.environ["DEEPSEEK_MOBILE_ROOT"] == str(tmp_path.resolve())
        assert os.environ["DEEPSEEK_MOBILE_STATIC_DIR"].endswith("static")
        assert os.environ["HOST"] == "127.0.0.1"
        assert os.environ["PORT"] == "8123"
        assert os.environ["DEEPSEEK_ANDROID_APP"] == "1"
        assert os.environ["OCR_ENABLED"] == "1"
        assert os.environ["DEEPSEEK_API_KEY"] == "sk-local"
        assert os.environ["TAVILY_API_KEY"] == "tvly-local"
        assert os.environ["AUTH_DISABLED"] == "1"


def test_start_json_returns_existing_handle_without_restarting(tmp_path: Path, monkeypatch) -> None:
    handle = SimpleNamespace(
        computer_url="http://127.0.0.1:8000/?token=abc",
        phone_url="http://127.0.0.1:8000/?token=abc",
        host="127.0.0.1",
        port=8000,
    )
    monkeypatch.setattr(android_entry, "_HANDLE", handle)

    payload = json.loads(android_entry.start_json(str(tmp_path)))

    assert payload == {
        "url": "http://127.0.0.1:8000/?token=abc",
        "phoneUrl": "http://127.0.0.1:8000/?token=abc",
        "host": "127.0.0.1",
        "port": 8000,
    }


def test_android_environment_defaults_and_clears_auth_disabled(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"AUTH_DISABLED": "1"}, clear=True):
        payload = android_entry.configure_android_environment(str(tmp_path), port=0)
        assert payload["port"] == str(android_entry.DEFAULT_ANDROID_PORT)
        assert "DEEPSEEK_API_KEY" not in os.environ
        assert "TAVILY_API_KEY" not in os.environ
        assert "AUTH_DISABLED" not in os.environ


def test_android_start_and_stop_lifecycle(tmp_path: Path, monkeypatch) -> None:
    handle = SimpleNamespace(computer_url="http://local", phone_url="http://phone", host="127.0.0.1", port=8123)
    monkeypatch.setattr(android_entry, "_HANDLE", None)
    import deepseek_infra.app as app

    starts: list[tuple[str, int, bool]] = []
    stopped: list[object] = []

    def prepare(*, host: str, port: int, serve: bool) -> object:
        starts.append((host, port, serve))
        return handle

    monkeypatch.setattr(app, "prepare_and_start", prepare)
    monkeypatch.setattr(app, "shutdown_handle", lambda value: stopped.append(value))
    with patch.dict(os.environ, {}, clear=False):
        assert android_entry.start(str(tmp_path), 8123)["port"] == 8123
        assert starts == [("127.0.0.1", 8123, True)]
        android_entry.stop()
        android_entry.stop()
    assert stopped == [handle]
