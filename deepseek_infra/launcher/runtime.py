"""Subprocess wrapper that runs the HTTP server for the GUI launcher.

The launcher spawns the existing ``python -m deepseek_infra.app`` entry point
in a child process so users can change API keys / host / port and restart the
service without having to restart the launcher window (the backend ``settings``
dataclass is frozen at import time, so an in-process restart would not pick up
the new environment).

When the application is bundled with PyInstaller (``sys.frozen`` is set), the
same one-file executable is re-invoked with ``--server`` so the bundled exe can
act as both launcher and server.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from deepseek_infra.core.config import settings
from deepseek_infra.core.utils import hidden_subprocess_kwargs
from deepseek_infra.launcher.credentials import LauncherCredentials

logger = logging.getLogger("deepseek_infra.launcher.runtime")
REDACTED_TOKEN_VALUE = "[redacted]"

StatusCallback = Callable[[str], None]
LogCallback = Callable[[str], None]


class LauncherRuntime:
    def __init__(self, on_log: LogCallback, on_status: StatusCallback) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._on_log = on_log
        self._on_status = on_status
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        proc = self._process
        return proc is not None and proc.poll() is None

    def start(self, credentials: LauncherCredentials) -> None:
        with self._lock:
            if self.is_running():
                return
            env = build_env(credentials)
            cmd = server_command()
            self._on_status("starting")
            try:
                self._process = subprocess.Popen(
                    cmd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=str(settings.root),
                    bufsize=1,
                    **hidden_subprocess_kwargs(),
                )
            except OSError as exc:
                self._on_log(f"failed to spawn server: {exc}")
                self._on_status("stopped")
                self._process = None
                return
        threading.Thread(target=self._read_loop, name="launcher-log-reader", daemon=True).start()
        self._on_status("running")

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            proc = self._process
            if proc is None:
                self._on_status("stopped")
                return
            if proc.poll() is not None:
                self._process = None
                self._on_status("stopped")
                return
            self._on_status("stopping")
            try:
                proc.terminate()
            except OSError as exc:
                self._on_log(f"terminate failed: {exc}")
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError as exc:
                self._on_log(f"kill failed: {exc}")
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                pass
        with self._lock:
            self._process = None
        self._on_status("stopped")

    def _read_loop(self) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self._on_log(line.rstrip("\n"))
        finally:
            self._on_status("stopped")
            with self._lock:
                if self._process is proc:
                    self._process = None


def build_env(credentials: LauncherCredentials) -> dict[str, str]:
    env = dict(os.environ)
    if credentials.deepseek_api_key:
        env["DEEPSEEK_API_KEY"] = credentials.deepseek_api_key
    else:
        env.pop("DEEPSEEK_API_KEY", None)
    if credentials.tavily_api_key:
        env["TAVILY_API_KEY"] = credentials.tavily_api_key
    else:
        env.pop("TAVILY_API_KEY", None)
    env["HOST"] = credentials.host
    env["PORT"] = str(int(credentials.port))
    env["OCR_ENABLED"] = "1" if credentials.ocr_enabled else "0"
    if credentials.auth_disabled:
        env["AUTH_DISABLED"] = "1"
        env.pop("AUTH_TOKEN", None)
    else:
        env.pop("AUTH_DISABLED", None)
        if settings.auth.token:
            env["AUTH_TOKEN"] = settings.auth.token
        else:
            env.pop("AUTH_TOKEN", None)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def launcher_url_from_log(value: object, auth_token: str) -> str:
    if not isinstance(value, str) or not value:
        return ""
    return _restore_redacted_token(value, auth_token)


def _restore_redacted_token(url: str, auth_token: str) -> str:
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    restored: list[tuple[str, str]] = []
    changed = False
    for key, value in query:
        if key == "token" and value == REDACTED_TOKEN_VALUE:
            if not auth_token:
                return ""
            restored.append((key, auth_token))
            changed = True
            continue
        restored.append((key, value))
    if not changed:
        return url
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(restored), parsed.fragment))


def server_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--server"]
    return [sys.executable, "-m", "deepseek_infra.app"]


def project_root() -> Path:
    return settings.root
