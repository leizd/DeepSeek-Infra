"""Native desktop shell that embeds the local app in a WebView window."""

from __future__ import annotations

import logging
import sys
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

from deepseek_infra.app import ServerHandle, prepare_and_start, shutdown_handle

APP_TITLE = "DeepSeek Infra"
DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 820
MIN_WIDTH = 760
MIN_HEIGHT = 520
SERVER_READY_TIMEOUT_SECONDS = 8.0
SERVER_READY_POLL_SECONDS = 0.2
SERVER_READY_REQUEST_TIMEOUT_SECONDS = 0.75

logger = logging.getLogger("deepseek_infra.desktop_app")


def main() -> int:
    handle: ServerHandle | None = None
    try:
        handle = prepare_and_start(host="127.0.0.1", serve=True)
        url = webview_entry_url(handle.computer_url)
        wait_for_server_ready(url)
        open_app_window(url)
        return 0
    except Exception as exc:
        logger.exception("desktop_app_failed")
        show_startup_error(exc)
        return 1
    finally:
        if handle is not None:
            shutdown_handle(handle)


def webview_entry_url(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(key == "desktop" for key, _ in query):
        query.append(("desktop", "1"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))


def open_app_window(url: str) -> None:
    try:
        import webview
    except ModuleNotFoundError as exc:
        raise RuntimeError("Desktop WebView dependency is missing. Install pywebview or rebuild the app package.") from exc

    webview.create_window(
        APP_TITLE,
        url,
        width=DEFAULT_WIDTH,
        height=DEFAULT_HEIGHT,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
    )
    webview.start(debug=False, private_mode=False)


def wait_for_server_ready(url: str, timeout_seconds: float = SERVER_READY_TIMEOUT_SECONDS) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=SERVER_READY_REQUEST_TIMEOUT_SECONDS) as response:
                if 200 <= int(response.status) < 400:
                    return
                last_error = RuntimeError(f"HTTP {response.status}")
        except BaseException as exc:
            last_error = exc
        time.sleep(SERVER_READY_POLL_SECONDS)
    raise RuntimeError(f"Local server did not become ready: {last_error}")


def show_startup_error(exc: BaseException) -> None:
    if sys.stderr is not None:
        print(f"DeepSeek Infra failed to start: {exc}", file=sys.stderr)
        return

    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:
        return

    root: Any = None
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("DeepSeek Infra", f"DeepSeek Infra failed to start:\n{exc}")
    finally:
        if root is not None:
            root.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
