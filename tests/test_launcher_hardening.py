"""Tests for launcher runtime and mobile modules to raise coverage for 3.1.5."""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import deepseek_infra.launcher.credentials as credentials_module
from deepseek_infra.launcher.credentials import (
    LauncherCredentials,
    clear,
    config_path,
    load,
    save,
)
from deepseek_infra.launcher.mobile import (
    DEFAULT_MOBILE_HOST,
    DEFAULT_PORT,
    LAN_HOST,
    configure_environment,
    is_mobile_environment,
    open_mobile_browser,
    parse_args,
    parse_port,
    print_mobile_banner,
)
from deepseek_infra.launcher.runtime import (
    LauncherRuntime,
    build_env,
    launcher_url_from_log,
    project_root,
    server_command,
)


# --- mobile.py ---


def test_is_mobile_environment_detects_android_env() -> None:
    assert is_mobile_environment({"ANDROID_ARGUMENT": "1"}) is True
    assert is_mobile_environment({"TERMUX_VERSION": "0.118"}) is True
    assert is_mobile_environment({"PYDROID_PACKAGE": "com.pyroid"}) is True


def test_is_mobile_environment_detects_android_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(platform, "platform", lambda: "Android-13")
    assert is_mobile_environment({}) is True


def test_is_mobile_environment_false() -> None:
    assert is_mobile_environment({}) is False


def test_parse_port_valid() -> None:
    assert parse_port("8080") == 8080
    assert parse_port("1") == 1
    assert parse_port("65535") == 65535


def test_parse_port_invalid() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_port("abc")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_port("0")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_port("65536")


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.host == DEFAULT_MOBILE_HOST
    assert args.port == DEFAULT_PORT
    assert args.lan is False


def test_parse_args_lan() -> None:
    args = parse_args(["--lan", "--port", "9000"])
    assert args.lan is True
    assert args.port == 9000


def test_configure_environment_defaults() -> None:
    host, port = configure_environment(parse_args([]))
    assert host == DEFAULT_MOBILE_HOST
    assert port == DEFAULT_PORT
    assert os.environ["HOST"] == DEFAULT_MOBILE_HOST
    assert os.environ["PORT"] == str(DEFAULT_PORT)


def test_configure_environment_lan() -> None:
    host, port = configure_environment(parse_args(["--lan"]))
    assert host == LAN_HOST


def test_configure_environment_with_keys() -> None:
    configure_environment(parse_args(["--api-key", "ds-key", "--tavily-api-key", "tv-key", "--auth-disabled"]))
    assert os.environ["DEEPSEEK_API_KEY"] == "ds-key"
    assert os.environ["TAVILY_API_KEY"] == "tv-key"
    assert os.environ["AUTH_DISABLED"] == "1"


def test_open_mobile_browser_termux(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("shutil.which", return_value="termux-open-url"):
        with patch("subprocess.Popen") as popen_mock:
            assert open_mobile_browser("http://x") is True
            popen_mock.assert_called_once()


def test_open_mobile_browser_webbrowser(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("shutil.which", return_value=None):
        with patch("webbrowser.open", return_value=True) as wb_mock:
            assert open_mobile_browser("http://x") is True
            wb_mock.assert_called_once_with("http://x", new=2)


def test_open_mobile_browser_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("shutil.which", return_value=None):
        with patch("webbrowser.open", side_effect=Exception("no browser")):
            assert open_mobile_browser("http://x") is False


def test_print_mobile_banner(capsys: pytest.CaptureFixture) -> None:
    print_mobile_banner("http://computer", "http://phone", True)
    captured = capsys.readouterr()
    assert "DeepSeek Infra is running on this phone" in captured.out


# --- runtime.py ---


def test_server_command() -> None:
    cmd = server_command()
    assert cmd[0] == sys.executable


def test_server_command_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert server_command() == [sys.executable, "--server"]


def test_project_root() -> None:
    root = project_root()
    assert (root / "pyproject.toml").exists()
    assert root.name in ("deepseek", "DeepSeek-Infra")


def test_build_env_sets_keys() -> None:
    creds = LauncherCredentials(
        deepseek_api_key="ds-key", tavily_api_key="tv-key", host="0.0.0.0", port=9000, ocr_enabled=True
    )
    env = build_env(creds)
    assert env["DEEPSEEK_API_KEY"] == "ds-key"
    assert env["TAVILY_API_KEY"] == "tv-key"
    assert env["HOST"] == "0.0.0.0"
    assert env["PORT"] == "9000"
    assert env["OCR_ENABLED"] == "1"


def test_build_env_clears_keys() -> None:
    os.environ["DEEPSEEK_API_KEY"] = "old"
    os.environ["TAVILY_API_KEY"] = "old"
    creds = LauncherCredentials()
    env = build_env(creds)
    assert "DEEPSEEK_API_KEY" not in env
    assert "TAVILY_API_KEY" not in env


def test_build_env_auth_disabled() -> None:
    creds = LauncherCredentials(auth_disabled=True)
    env = build_env(creds)
    assert env["AUTH_DISABLED"] == "1"
    assert "AUTH_TOKEN" not in env


def test_launcher_url_from_log() -> None:
    assert launcher_url_from_log("http://x?token=[redacted]", "abc") == "http://x?token=abc"
    assert launcher_url_from_log("http://x?token=[redacted]", "") == ""
    assert launcher_url_from_log(123, "abc") == ""
    assert launcher_url_from_log("http://x?other=1", "abc") == "http://x?other=1"


def test_launcher_runtime_is_running_not_started() -> None:
    log_cb: list[str] = []
    status_cb: list[str] = []
    runtime = LauncherRuntime(lambda x: log_cb.append(x), lambda x: status_cb.append(x))
    assert runtime.is_running() is False


def test_launcher_runtime_stop_when_not_running() -> None:
    log_cb: list[str] = []
    status_cb: list[str] = []
    runtime = LauncherRuntime(lambda x: log_cb.append(x), lambda x: status_cb.append(x))
    runtime.stop()
    assert status_cb == ["stopped"]


def test_launcher_runtime_start_already_running() -> None:
    log_cb: list[str] = []
    status_cb: list[str] = []
    runtime = LauncherRuntime(lambda x: log_cb.append(x), lambda x: status_cb.append(x))
    runtime._process = MagicMock()
    runtime._process.poll.return_value = None
    runtime.start(LauncherCredentials())
    assert runtime.is_running() is True


def test_launcher_runtime_start_spawn_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    log_cb: list[str] = []
    status_cb: list[str] = []
    runtime = LauncherRuntime(lambda x: log_cb.append(x), lambda x: status_cb.append(x))
    with patch("subprocess.Popen", side_effect=OSError("no python")):
        runtime.start(LauncherCredentials())
    assert runtime.is_running() is False
    assert any("failed to spawn" in msg for msg in log_cb)


def test_launcher_runtime_stop_and_kill() -> None:
    log_cb: list[str] = []
    status_cb: list[str] = []
    runtime = LauncherRuntime(lambda x: log_cb.append(x), lambda x: status_cb.append(x))
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired("", 5), None]
    runtime._process = proc
    runtime.stop(timeout=0.01)
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert "stopped" in status_cb


def test_launcher_runtime_read_loop_handles_none() -> None:
    runtime = LauncherRuntime(lambda x: None, lambda x: None)
    runtime._process = None
    runtime._read_loop()


# --- credentials.py ---


@pytest.fixture
def tmp_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(credentials_module, "settings", SimpleNamespace(root=tmp_path))
    return tmp_path



def test_credentials_config_path(tmp_root: Path) -> None:
    assert config_path() == tmp_root / ".launcher-config.json"


def test_credentials_save_load_roundtrip(tmp_root: Path) -> None:
    creds = LauncherCredentials(
        deepseek_api_key="ds-key",
        tavily_api_key="tv-key",
        host="0.0.0.0",
        port=1234,
        allow_lan=True,
        ocr_enabled=True,
        auth_disabled=True,
    )
    save(creds)
    loaded = load()
    assert loaded == creds


def test_credentials_load_missing_file(tmp_root: Path) -> None:
    assert load() == LauncherCredentials()


def test_credentials_load_invalid_json(tmp_root: Path) -> None:
    config_path().write_text("not json", encoding="utf-8")
    assert load() == LauncherCredentials()


def test_credentials_load_non_dict_envelope(tmp_root: Path) -> None:
    config_path().write_text("[1, 2, 3]", encoding="utf-8")
    assert load() == LauncherCredentials()


def test_credentials_load_missing_data_key(tmp_root: Path) -> None:
    config_path().write_text('{"version": 1}', encoding="utf-8")
    assert load() == LauncherCredentials()


def test_credentials_load_tampered_mac(tmp_root: Path) -> None:
    creds = LauncherCredentials(deepseek_api_key="secret")
    save(creds)
    envelope = json.loads(config_path().read_text(encoding="utf-8"))
    envelope["data"]["mac"] = base64.b64encode(b"wrong").decode("ascii")
    config_path().write_text(json.dumps(envelope), encoding="utf-8")
    assert load() == LauncherCredentials()


def test_credentials_load_invalid_b64(tmp_root: Path) -> None:
    config_path().write_text('{"version": 1, "data": "not-b64!!!"}', encoding="utf-8")
    assert load() == LauncherCredentials()


def test_credentials_load_invalid_plaintext_utf8(tmp_root: Path) -> None:
    from deepseek_infra.launcher.credentials import _decrypt, _encrypt

    envelope = _encrypt(b"\xff\xfe")
    assert _decrypt(envelope) is None


def test_credentials_restrict_permissions_non_windows(tmp_root: Path) -> None:
    from deepseek_infra.launcher.credentials import _restrict_permissions

    path = tmp_root / "config.json"
    path.write_text("{}", encoding="utf-8")
    with patch("os.name", "posix"):
        with patch("pathlib.Path.chmod") as chmod_mock:
            _restrict_permissions(path)
            chmod_mock.assert_called_once_with(0o600)


def test_credentials_restrict_permissions_oserror_ignored(tmp_root: Path) -> None:
    from deepseek_infra.launcher.credentials import _restrict_permissions

    path = tmp_root / "config.json"
    path.write_text("{}", encoding="utf-8")
    with patch("os.name", "posix"):
        with patch("pathlib.Path.chmod", side_effect=OSError("no perm")):
            _restrict_permissions(path)  # should not raise



def test_credentials_from_dict_defaults(tmp_root: Path) -> None:
    from deepseek_infra.launcher.credentials import _from_dict

    assert _from_dict({}) == LauncherCredentials()


def test_credentials_from_dict_port_clamping(tmp_root: Path) -> None:
    from deepseek_infra.launcher.credentials import _from_dict

    assert _from_dict({"port": 0}).port == 8000
    assert _from_dict({"port": 65536}).port == 8000
    assert _from_dict({"port": "abc"}).port == 8000


def test_credentials_from_dict_lan_inference(tmp_root: Path) -> None:
    from deepseek_infra.launcher.credentials import _from_dict

    parsed = _from_dict({"host": "127.0.0.1", "allow_lan": True})
    assert parsed.host == "0.0.0.0"
    assert parsed.allow_lan is True


def test_credentials_with_updates() -> None:
    creds = LauncherCredentials()
    updated = creds.with_updates(port=9999, ocr_enabled=True)
    assert updated.port == 9999
    assert updated.ocr_enabled is True
    assert creds.port == 8000


def test_credentials_clear(tmp_root: Path) -> None:
    save(LauncherCredentials(deepseek_api_key="x"))
    assert config_path().exists()
    clear()
    assert not config_path().exists()
    clear()  # idempotent


# --- runtime.py branches ---


def test_runtime_stop_process_already_exited() -> None:
    log_cb: list[str] = []
    status_cb: list[str] = []
    runtime = LauncherRuntime(lambda x: log_cb.append(x), lambda x: status_cb.append(x))
    proc = MagicMock()
    proc.poll.return_value = 0
    runtime._process = proc
    runtime.stop(timeout=0.01)
    assert runtime._process is None
    assert "stopped" in status_cb


def test_runtime_stop_terminate_oserror() -> None:
    log_cb: list[str] = []
    status_cb: list[str] = []
    runtime = LauncherRuntime(lambda x: log_cb.append(x), lambda x: status_cb.append(x))
    proc = MagicMock()
    proc.poll.return_value = None
    proc.terminate.side_effect = OSError("term")
    proc.wait.return_value = None
    runtime._process = proc
    runtime.stop(timeout=0.01)
    assert "terminate failed" in log_cb[-1]
    assert "stopped" in status_cb


def test_runtime_stop_kill_oserror() -> None:
    log_cb: list[str] = []
    status_cb: list[str] = []
    runtime = LauncherRuntime(lambda x: log_cb.append(x), lambda x: status_cb.append(x))
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = subprocess.TimeoutExpired("", 5)
    proc.kill.side_effect = OSError("kill")
    runtime._process = proc
    runtime.stop(timeout=0.01)
    assert "kill failed" in log_cb[-1]


def test_runtime_stop_kill_wait_timeout() -> None:
    log_cb: list[str] = []
    status_cb: list[str] = []
    runtime = LauncherRuntime(lambda x: log_cb.append(x), lambda x: status_cb.append(x))
    proc = MagicMock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired("", 5), subprocess.TimeoutExpired("", 5)]
    runtime._process = proc
    runtime.stop(timeout=0.01)
    proc.kill.assert_called_once()


def test_runtime_read_loop_processes_lines() -> None:
    log_cb: list[str] = []
    status_cb: list[str] = []
    runtime = LauncherRuntime(lambda x: log_cb.append(x), lambda x: status_cb.append(x))
    proc = MagicMock()
    proc.stdout = ["line1\n", "line2\n"]
    proc.__bool__ = lambda self: True
    runtime._process = proc
    runtime._read_loop()
    assert log_cb == ["line1", "line2"]
    assert "stopped" in status_cb
    assert runtime._process is None


# --- mobile.py branches ---


def test_configure_environment_prompt_for_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.launcher.mobile import configure_environment

    args = MagicMock()
    args.lan = False
    args.host = "127.0.0.1"
    args.port = 8000
    args.api_key = ""
    args.tavily_api_key = ""
    args.auth_disabled = False
    args.ocr = False
    args.no_prompt = False
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "typed-key")
    os.environ.pop("DEEPSEEK_API_KEY", None)
    configure_environment(args)
    assert os.environ["DEEPSEEK_API_KEY"] == "typed-key"


def test_print_mobile_banner_not_opened(capsys: pytest.CaptureFixture) -> None:
    from deepseek_infra.launcher.mobile import print_mobile_banner

    print_mobile_banner("http://computer", "http://phone", False)
    captured = capsys.readouterr()
    assert "Copy the local URL" in captured.out


def test_mobile_main(monkeypatch: pytest.MonkeyPatch) -> None:
    from deepseek_infra.launcher.mobile import main

    fake_handle = MagicMock()
    fake_handle.computer_url = "http://computer"
    fake_handle.phone_url = "http://phone"
    fake_server = MagicMock()
    fake_server.serve_forever.side_effect = KeyboardInterrupt
    fake_handle.server = fake_server
    with patch("deepseek_infra.app.prepare_and_start", return_value=fake_handle) as prep:
        with patch("deepseek_infra.app.shutdown_handle") as shutdown:
            with patch("deepseek_infra.launcher.mobile.open_mobile_browser", return_value=False):
                result = main(["--no-open", "--no-prompt", "--port", "8000"])
    assert result == 0
    prep.assert_called_once()
    shutdown.assert_called_once()

