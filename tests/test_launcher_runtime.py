"""Tests for the GUI launcher's subprocess runtime helpers."""

from __future__ import annotations

import sys

import pytest

import deepseek_infra.launcher.runtime as runtime_module
from deepseek_infra.launcher.credentials import LAN_HOST, LauncherCredentials
from deepseek_infra.launcher.runtime import (
    LauncherRuntime,
    build_env,
    launcher_url_from_log,
    server_command,
    settings as runtime_settings,
)


def test_build_env_carries_keys_and_settings() -> None:
    creds = LauncherCredentials(
        deepseek_api_key="sk-d",
        tavily_api_key="tvly-t",
        host=LAN_HOST,
        port=9123,
        allow_lan=True,
        ocr_enabled=True,
        auth_disabled=False,
    )
    env = build_env(creds)
    assert env["DEEPSEEK_API_KEY"] == "sk-d"
    assert env["TAVILY_API_KEY"] == "tvly-t"
    assert env["HOST"] == LAN_HOST
    assert env["PORT"] == "9123"
    assert env["OCR_ENABLED"] == "1"
    assert env["AUTH_TOKEN"] == runtime_settings.auth.token
    assert "AUTH_DISABLED" not in env
    assert env.get("PYTHONIOENCODING") == "utf-8"
    assert env.get("PYTHONUTF8") == "1"


def test_build_env_strips_empty_keys_from_parent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "inherited")
    monkeypatch.setenv("TAVILY_API_KEY", "inherited")
    monkeypatch.setenv("AUTH_DISABLED", "1")
    creds = LauncherCredentials(deepseek_api_key="", tavily_api_key="")
    env = build_env(creds)
    assert "DEEPSEEK_API_KEY" not in env
    assert "TAVILY_API_KEY" not in env
    assert "AUTH_DISABLED" not in env


def test_build_env_keeps_auth_disabled_when_requested() -> None:
    creds = LauncherCredentials(deepseek_api_key="sk", auth_disabled=True)
    env = build_env(creds)
    assert env["AUTH_DISABLED"] == "1"
    assert "AUTH_TOKEN" not in env


def test_build_env_passes_ocr_disabled_explicitly() -> None:
    creds = LauncherCredentials(deepseek_api_key="sk", ocr_enabled=False)
    env = build_env(creds)
    assert env["OCR_ENABLED"] == "0"


def test_server_command_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    cmd = server_command()
    assert cmd == [sys.executable, "-m", "deepseek_infra.app"]


def test_server_command_frozen_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cmd = server_command()
    assert cmd == [sys.executable, "--server"]


def test_launcher_url_from_log_restores_redacted_token() -> None:
    url = launcher_url_from_log("http://127.0.0.1:8011/?token=%5Bredacted%5D", "actual-token")

    assert url == "http://127.0.0.1:8011/?token=actual-token"


def test_launcher_url_from_log_keeps_plain_urls_and_ignores_unrestorable_token() -> None:
    assert launcher_url_from_log("http://127.0.0.1:8011/", "actual-token") == "http://127.0.0.1:8011/"
    assert launcher_url_from_log("http://127.0.0.1:8011/?token=%5Bredacted%5D", "") == ""


def test_launcher_runtime_starts_server_without_console_window(monkeypatch: pytest.MonkeyPatch) -> None:
    startupinfo = object()
    launched: dict[str, object] = {}

    class FakeProcess:
        stdout = None

        def poll(self) -> None:
            return None

    class FakeThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

    def fake_popen(cmd: list[str], **kwargs: object) -> FakeProcess:
        launched["cmd"] = cmd
        launched.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(runtime_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runtime_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(runtime_module, "hidden_subprocess_kwargs", lambda: {"creationflags": 123, "startupinfo": startupinfo})

    runtime = LauncherRuntime(on_log=lambda _line: None, on_status=lambda _status: None)
    runtime.start(LauncherCredentials(deepseek_api_key="sk"))

    assert launched["creationflags"] == 123
    assert launched["startupinfo"] is startupinfo


def test_build_env_without_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    original = runtime_module.settings
    monkeypatch.setattr(runtime_module, "settings", SimpleNamespace(auth=SimpleNamespace(enabled=True, token="")))
    env = build_env(LauncherCredentials())
    assert "AUTH_TOKEN" not in env
    monkeypatch.setattr(runtime_module, "settings", original)
