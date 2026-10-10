from __future__ import annotations

from deepseek_infra.launcher import mobile


def test_mobile_environment_detects_android_markers() -> None:
    assert mobile.is_mobile_environment({"ANDROID_ROOT": "/system"}) is True
    assert mobile.is_mobile_environment({"TERMUX_VERSION": "0.118"}) is True
    assert mobile.is_mobile_environment({}) is False


def test_mobile_configure_environment_sets_local_defaults(monkeypatch) -> None:
    for key in [
        "DEEPSEEK_API_KEY",
        "HOST",
        "OCR_ENABLED",
        "PORT",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "TAVILY_API_KEY",
    ]:
        monkeypatch.delenv(key, raising=False)
    args = mobile.parse_args(["--port", "8123", "--api-key", "sk-phone", "--no-prompt", "--no-open"])

    host, port = mobile.configure_environment(args)

    assert host == "127.0.0.1"
    assert port == 8123
    assert mobile.os.environ["HOST"] == "127.0.0.1"
    assert mobile.os.environ["PORT"] == "8123"
    assert mobile.os.environ["DEEPSEEK_API_KEY"] == "sk-phone"
    assert mobile.os.environ["OCR_ENABLED"] == "0"
    assert mobile.os.environ["PYTHONIOENCODING"] == "utf-8"
    assert mobile.os.environ["PYTHONUTF8"] == "1"


def test_mobile_configure_environment_supports_lan_auth_and_ocr(monkeypatch) -> None:
    monkeypatch.delenv("AUTH_DISABLED", raising=False)
    args = mobile.parse_args(["--lan", "--auth-disabled", "--ocr", "--tavily-api-key", "tvly-phone", "--no-prompt"])

    host, _ = mobile.configure_environment(args)

    assert host == "0.0.0.0"
    assert mobile.os.environ["HOST"] == "0.0.0.0"
    assert mobile.os.environ["AUTH_DISABLED"] == "1"
    assert mobile.os.environ["OCR_ENABLED"] == "1"
    assert mobile.os.environ["TAVILY_API_KEY"] == "tvly-phone"


def test_mobile_parse_port_rejects_invalid_values() -> None:
    for value in ["0", "65536", "abc"]:
        try:
            mobile.parse_port(value)
        except Exception as exc:
            assert exc.__class__.__name__ == "ArgumentTypeError"
        else:
            raise AssertionError(f"expected parse error for {value}")


def test_mobile_configure_environment_interactive_getpass(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(mobile.sys.stdin, "isatty", lambda: True)

    # Empty key entered
    monkeypatch.setattr(mobile.getpass, "getpass", lambda prompt: "   ")
    args_empty = mobile.parse_args(["--port", "8123"])
    mobile.configure_environment(args_empty)
    assert "DEEPSEEK_API_KEY" not in mobile.os.environ

    # Key entered
    monkeypatch.setattr(mobile.getpass, "getpass", lambda prompt: "sk-interactive")
    args_key = mobile.parse_args(["--port", "8123"])
    mobile.configure_environment(args_key)
    assert mobile.os.environ["DEEPSEEK_API_KEY"] == "sk-interactive"


def test_open_mobile_browser(monkeypatch) -> None:
    # 1. termux-open-url found
    monkeypatch.setattr(mobile.shutil, "which", lambda cmd: "/usr/bin/termux-open-url" if cmd == "termux-open-url" else None)
    monkeypatch.setattr(mobile.subprocess, "Popen", lambda *a, **kw: None)
    assert mobile.open_mobile_browser("http://127.0.0.1:8123") is True

    # 2. termux-open-url not found -> webbrowser fallback
    monkeypatch.setattr(mobile.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(mobile.webbrowser, "open", lambda url, new=2: True)
    assert mobile.open_mobile_browser("http://127.0.0.1:8123") is True

