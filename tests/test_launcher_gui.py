from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sys

import deepseek_infra.launcher.gui as gui
from deepseek_infra.launcher.credentials import DEFAULT_HOST, DEFAULT_PORT, LAN_HOST, LauncherCredentials


class FakeVar:
    def __init__(self, value: Any = "") -> None:
        self.value = value

    def get(self) -> Any:
        return self.value

    def set(self, value: Any) -> None:
        self.value = value


class FakeWidget:
    created: list["FakeWidget"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = dict(kwargs)
        self.config: dict[str, Any] = dict(kwargs)
        self.content = ""
        FakeWidget.created.append(self)

    def pack(self, *args: Any, **kwargs: Any) -> None:
        self.pack_args = (args, kwargs)

    def grid(self, *args: Any, **kwargs: Any) -> None:
        self.grid_args = (args, kwargs)

    def configure(self, **kwargs: Any) -> None:
        self.config.update(kwargs)

    def columnconfigure(self, *args: Any, **kwargs: Any) -> None:
        self.column_args = (args, kwargs)

    def bind(self, *args: Any, **kwargs: Any) -> None:
        self.bind_args = (args, kwargs)

    def insert(self, _index: str, text: str) -> None:
        self.content += text

    def delete(self, _start: str, _end: str) -> None:
        self.content = ""

    def see(self, index: str) -> None:
        self.seen = index

    def index(self, _index: str) -> str:
        line_count = max(1, self.content.count("\n") + 1)
        return f"{line_count}.0"


class FakeRoot(FakeWidget):
    def __init__(self) -> None:
        super().__init__()
        self.after_calls: list[tuple[int, Any]] = []
        self.clipboard: list[str] = []
        self.destroyed = False

    def title(self, value: str) -> None:
        self.window_title = value

    def geometry(self, value: str) -> None:
        self.window_geometry = value

    def minsize(self, width: int, height: int) -> None:
        self.window_minsize = (width, height)

    def protocol(self, name: str, callback: Any) -> None:
        self.protocol_handler = (name, callback)

    def after(self, delay_ms: int, callback: Any) -> None:
        self.after_calls.append((delay_ms, callback))

    def clipboard_clear(self) -> None:
        self.clipboard.clear()

    def clipboard_append(self, value: str) -> None:
        self.clipboard.append(value)

    def destroy(self) -> None:
        self.destroyed = True

    def mainloop(self) -> None:
        self.mainloop_called = True


@dataclass
class FakeRuntime:
    on_log: Any
    on_status: Any
    running: bool = False
    started_with: LauncherCredentials | None = None
    stop_calls: int = 0

    def start(self, creds: LauncherCredentials) -> None:
        self.started_with = creds
        self.running = True

    def stop(self) -> None:
        self.stop_calls += 1
        self.running = False

    def is_running(self) -> bool:
        return self.running


@dataclass
class FakeMessageBox:
    yesno: bool = True
    errors: list[tuple[str, str]] = field(default_factory=list)
    infos: list[tuple[str, str]] = field(default_factory=list)
    prompts: list[tuple[str, str]] = field(default_factory=list)

    def showerror(self, title: str, message: str) -> None:
        self.errors.append((title, message))

    def showinfo(self, title: str, message: str) -> None:
        self.infos.append((title, message))

    def askyesno(self, title: str, message: str) -> bool:
        self.prompts.append((title, message))
        return self.yesno


class FakeThread:
    def __init__(self, *, target: Any, name: str, daemon: bool) -> None:
        self.target = target
        self.name = name
        self.daemon = daemon

    def start(self) -> None:
        self.target()


@pytest.fixture()
def launcher_window(monkeypatch: pytest.MonkeyPatch) -> tuple[gui.LauncherWindow, FakeRoot, FakeMessageBox, list[LauncherCredentials]]:
    FakeWidget.created.clear()
    saved: list[LauncherCredentials] = []
    messagebox = FakeMessageBox()

    monkeypatch.setattr(gui.tk, "StringVar", FakeVar)
    monkeypatch.setattr(gui.tk, "BooleanVar", FakeVar)
    monkeypatch.setattr(gui.ctk, "CTkFont", lambda **kwargs: ("font", kwargs))
    for widget_name in ("CTkFrame", "CTkLabel", "CTkEntry", "CTkCheckBox", "CTkButton", "CTkTextbox"):
        monkeypatch.setattr(gui.ctk, widget_name, FakeWidget)
    monkeypatch.setattr(gui, "LauncherRuntime", FakeRuntime)
    monkeypatch.setattr(gui, "messagebox", messagebox)
    monkeypatch.setattr(gui.credentials_store, "load", lambda: LauncherCredentials(deepseek_api_key="sk-loaded", tavily_api_key="tvly-loaded", port=8123))
    monkeypatch.setattr(gui.credentials_store, "save", lambda creds: saved.append(creds))
    monkeypatch.setattr(gui.credentials_store, "clear", lambda: saved.append(LauncherCredentials()))
    monkeypatch.setattr(gui, "local_ip", lambda: "192.168.1.42")
    monkeypatch.setattr(gui.threading, "Thread", FakeThread)

    root = FakeRoot()
    window = gui.LauncherWindow(root)
    return window, root, messagebox, saved


def test_launcher_window_builds_headless_and_loads_persisted_credentials(
    launcher_window: tuple[gui.LauncherWindow, FakeRoot, FakeMessageBox, list[LauncherCredentials]],
) -> None:
    window, root, _messagebox, _saved = launcher_window

    assert root.window_geometry == "860x740"
    assert window.deepseek_var.get() == "sk-loaded"
    assert window.tavily_var.get() == "tvly-loaded"
    assert window.host_var.get() == DEFAULT_HOST
    assert window.port_var.get() == "8123"
    assert root.after_calls[0][0] == gui.POLL_INTERVAL_MS


def test_launcher_current_credentials_validates_port_and_lan(
    launcher_window: tuple[gui.LauncherWindow, FakeRoot, FakeMessageBox, list[LauncherCredentials]],
) -> None:
    window, _root, messagebox, _saved = launcher_window

    window.port_var.set("not-a-port")
    assert window._current_credentials() is None
    assert messagebox.errors

    window.port_var.set("70000")
    assert window._current_credentials() is None

    window.port_var.set("9000")
    window.allow_lan_var.set(True)
    creds = window._current_credentials()

    assert creds is not None
    assert creds.host == LAN_HOST
    assert creds.port == 9000
    assert window.host_var.get() == LAN_HOST


def test_launcher_start_stop_save_clear_and_browser_actions(
    launcher_window: tuple[gui.LauncherWindow, FakeRoot, FakeMessageBox, list[LauncherCredentials]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _root, messagebox, saved = launcher_window
    opened: list[str] = []

    monkeypatch.setattr(gui, "port_in_use", lambda _host, _port: False)
    monkeypatch.setattr(gui.webbrowser, "open", lambda url, new=0: opened.append(f"{new}:{url}"))

    window.deepseek_var.set("")
    window.port_var.set("8124")
    window.auth_disabled_var.set(True)
    window._on_start()

    assert messagebox.prompts
    runtime = cast(FakeRuntime, window.runtime)
    assert runtime.started_with is not None
    assert saved[-1].port == 8124
    assert window.computer_url_var.get() == "http://127.0.0.1:8124/"
    assert window.phone_url_var.get() == "http://192.168.1.42:8124/"
    assert window.start_button.config["state"] == "disabled"

    window._on_open_browser()
    assert opened == ["2:http://127.0.0.1:8124/"]

    window._on_stop()
    assert runtime.stop_calls == 1

    window._on_save()
    assert messagebox.infos

    window._on_clear()
    assert window.deepseek_var.get() == ""
    assert window.port_var.get() == str(DEFAULT_PORT)


def test_launcher_events_logs_urls_copy_and_close(
    launcher_window: tuple[gui.LauncherWindow, FakeRoot, FakeMessageBox, list[LauncherCredentials]],
) -> None:
    window, root, messagebox, _saved = launcher_window

    window._event_queue.put(("status", "running"))
    window._event_queue.put(("log", 'server_started {"computer_url": "http://127.0.0.1:9001/?token=%5Bredacted%5D", "phone_url": "http://10.0.0.5:9001/?token=%5Bredacted%5D"}'))
    window._drain_events()

    assert "token=" in window.computer_url_var.get()
    assert window.open_button.config["state"] == "normal"
    assert "server_started" in window.log_widget.content

    window._copy_url(window.computer_url_var)
    assert root.clipboard == [window.computer_url_var.get()]
    assert root.after_calls[-1][0] == 1500

    window._apply_status("stopped")
    assert window.start_button.config["state"] == "normal"
    assert window.open_button.config["state"] == "disabled"

    runtime = cast(FakeRuntime, window.runtime)
    runtime.running = True
    messagebox.yesno = False
    window._on_close()
    assert not root.destroyed

    messagebox.yesno = True
    window._on_close()
    assert root.destroyed
    assert runtime.stop_calls >= 1


def test_launcher_misc_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert gui.port_in_use("127.0.0.1", 9) is False

    monkeypatch.setattr(gui.ctk, "set_appearance_mode", lambda _mode: None)
    monkeypatch.setattr(gui.ctk, "set_default_color_theme", lambda _theme: None)
    monkeypatch.setattr(gui.ctk, "CTk", FakeRoot)
    monkeypatch.setattr(gui, "_enable_windows_dpi_awareness", lambda: 0.0)
    monkeypatch.setattr(gui, "LauncherWindow", lambda root: root)

    gui.main()


def test_launcher_load_save_and_start_failures_are_nonfatal(
    launcher_window: tuple[gui.LauncherWindow, FakeRoot, FakeMessageBox, list[LauncherCredentials]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _root, messagebox, _saved = launcher_window
    monkeypatch.setattr(gui.credentials_store, "load", lambda: (_ for _ in ()).throw(OSError("corrupt credentials")))
    window._load_persisted()

    window.deepseek_var.set("")
    messagebox.yesno = False
    window._on_start()
    assert cast(FakeRuntime, window.runtime).started_with is None

    window.deepseek_var.set("sk")
    monkeypatch.setattr(gui, "port_in_use", lambda _host, _port: True)
    window._on_start()
    assert cast(FakeRuntime, window.runtime).started_with is None

    messagebox.yesno = True
    monkeypatch.setattr(gui.credentials_store, "save", lambda _creds: (_ for _ in ()).throw(OSError("disk full")))
    window._on_start()
    assert cast(FakeRuntime, window.runtime).started_with is not None

    window._on_save()
    assert messagebox.errors[-1][1] == "disk full"


def test_launcher_empty_clear_open_copy_and_invalid_logs_are_noops(
    launcher_window: tuple[gui.LauncherWindow, FakeRoot, FakeMessageBox, list[LauncherCredentials]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, root, messagebox, saved = launcher_window
    opened: list[str] = []
    monkeypatch.setattr(gui.webbrowser, "open", lambda url, new=0: opened.append(url))
    window.computer_url_var.set("")
    window._on_open_browser()
    window._copy_url(window.computer_url_var)
    assert opened == [] and root.clipboard == []

    messagebox.yesno = False
    window._on_clear()
    assert saved == []
    window._try_update_urls_from_log("server_started without-json")
    window._try_update_urls_from_log("server_started {bad json")
    assert window.open_button.config["state"] == "disabled"


def test_launcher_status_and_url_fallback_branches(
    launcher_window: tuple[gui.LauncherWindow, FakeRoot, FakeMessageBox, list[LauncherCredentials]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window, _root, _messagebox, _saved = launcher_window
    for status in ("starting", "stopping", "custom"):
        window._apply_status(status)
        assert window.status_var.get()
    window._restore_status_text(True)
    window._restore_status_text(False)

    monkeypatch.setattr(gui, "local_ip", lambda: (_ for _ in ()).throw(OSError("no network")))
    monkeypatch.setattr(gui, "settings", SimpleNamespace(auth=SimpleNamespace(token="token")))
    window._update_urls(LauncherCredentials(port=8123, auth_disabled=False))
    assert "token=" in window.computer_url_var.get()
    assert "127.0.0.1" in window.phone_url_var.get()


def test_port_probe_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class Probe:
        def __enter__(self) -> "Probe":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def settimeout(self, value: float) -> None:
            assert value == 0.2

        def connect(self, address: tuple[str, int]) -> None:
            assert address == ("127.0.0.1", 8123)

    monkeypatch.setattr(gui.socket, "socket", lambda *_args, **_kwargs: Probe())
    assert gui.port_in_use("127.0.0.1", 8123) is True


def test_windows_dpi_awareness_uses_legacy_fallbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Shcore:
        def SetProcessDpiAwareness(self, level: int) -> None:
            calls.append(f"shcore:{level}")
            if level == 2:
                raise OSError("unsupported")

    class User32:
        def GetDpiForSystem(self) -> int:
            raise AttributeError

        def GetDC(self, value: int) -> int:
            return 7

        def ReleaseDC(self, hdc: int, value: int) -> None:
            calls.append(f"release:{hdc}:{value}")

        def SetProcessDPIAware(self) -> None:
            calls.append("legacy")

    windll = SimpleNamespace(
        shcore=Shcore(),
        user32=User32(),
        gdi32=SimpleNamespace(GetDeviceCaps=lambda hdc, index: 144),
    )
    monkeypatch.setattr(sys, "platform", "win32")
    import ctypes

    monkeypatch.setattr(ctypes, "windll", windll, raising=False)
    assert gui._enable_windows_dpi_awareness() == 2.0
    assert calls[:2] == ["shcore:2", "shcore:1"]


def test_windows_dpi_awareness_non_windows_and_total_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert gui._enable_windows_dpi_awareness() == 0.0
    monkeypatch.setattr(sys, "platform", "win32")
    import ctypes

    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(), raising=False)
    assert gui._enable_windows_dpi_awareness() == 0.0
