"""Chaquopy bridge used by the Android APK wrapper."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

DEFAULT_ANDROID_PORT = 8000
_LOCK = threading.RLock()
_HANDLE: Any | None = None


def dependency_versions() -> dict[str, str]:
    import fastapi
    import pydantic
    import uvicorn

    return {
        "fastapi": fastapi.__version__,
        "pydantic": pydantic.VERSION,
        "uvicorn": uvicorn.__version__,
    }


def configure_android_environment(
    root_dir: str,
    port: int = DEFAULT_ANDROID_PORT,
    api_key: str = "",
    tavily_api_key: str = "",
    auth_disabled: bool = False,
) -> dict[str, str]:
    root = Path(root_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    static_dir = Path(__file__).resolve().parents[1] / "static"

    os.environ["DEEPSEEK_MOBILE_ROOT"] = str(root)
    if static_dir.exists():
        os.environ["DEEPSEEK_MOBILE_STATIC_DIR"] = str(static_dir)
    os.environ["HOST"] = "127.0.0.1"
    os.environ["PORT"] = str(int(port) if port else DEFAULT_ANDROID_PORT)
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    os.environ["DEEPSEEK_ANDROID_APP"] = "1"
    os.environ.setdefault("OCR_ENABLED", "1")
    os.environ.setdefault("AUTH_ALLOWED_HOSTS", "127.0.0.1,localhost")

    if api_key:
        os.environ["DEEPSEEK_API_KEY"] = api_key.strip()
    if tavily_api_key:
        os.environ["TAVILY_API_KEY"] = tavily_api_key.strip()
    if auth_disabled:
        os.environ["AUTH_DISABLED"] = "1"
    else:
        os.environ.pop("AUTH_DISABLED", None)

    return {"root": str(root), "staticDir": str(static_dir), "port": os.environ["PORT"]}


def start(
    root_dir: str,
    port: int = DEFAULT_ANDROID_PORT,
    api_key: str = "",
    tavily_api_key: str = "",
    auth_disabled: bool = False,
) -> dict[str, Any]:
    global _HANDLE
    with _LOCK:
        if _HANDLE is not None:
            return _handle_payload(_HANDLE)

        configure_android_environment(root_dir, port, api_key, tavily_api_key, auth_disabled)

        from deepseek_infra.app import prepare_and_start

        _HANDLE = prepare_and_start(host="127.0.0.1", port=port, serve=True)
        return _handle_payload(_HANDLE)


def start_json(
    root_dir: str,
    port: int = DEFAULT_ANDROID_PORT,
    api_key: str = "",
    tavily_api_key: str = "",
    auth_disabled: bool = False,
) -> str:
    return json.dumps(
        start(root_dir, port, api_key, tavily_api_key, auth_disabled),
        ensure_ascii=False,
    )


def stop() -> None:
    global _HANDLE
    with _LOCK:
        handle = _HANDLE
        if handle is None:
            return
        _HANDLE = None

    from deepseek_infra.app import shutdown_handle

    shutdown_handle(handle)


def _handle_payload(handle: Any) -> dict[str, Any]:
    return {
        "url": handle.computer_url,
        "phoneUrl": handle.phone_url,
        "host": handle.host,
        "port": handle.port,
    }
