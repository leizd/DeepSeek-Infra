"""Console launcher for running DeepSeek Infra directly on a phone.

The desktop launcher depends on Tk/customtkinter. Android Python environments
such as Termux or Pydroid usually do not provide a desktop GUI stack, so this
module keeps the startup path standard-library only until the environment is
configured and the HTTP server is ready to import.
"""

from __future__ import annotations

import argparse
import getpass
import os
import platform
import shutil
import subprocess
import sys
import webbrowser
from typing import Mapping

DEFAULT_MOBILE_HOST = "127.0.0.1"
LAN_HOST = "0.0.0.0"
DEFAULT_PORT = 8000
MOBILE_ENV_KEYS = (
    "ANDROID_ARGUMENT",
    "ANDROID_DATA",
    "ANDROID_ROOT",
    "PYDROID_PACKAGE",
    "TERMUX_VERSION",
)


def is_mobile_environment(env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    if any(values.get(key) for key in MOBILE_ENV_KEYS):
        return True
    return "android" in platform.platform().lower()


def parse_port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run DeepSeek Infra on this phone.")
    parser.add_argument("--host", default=os.environ.get("HOST") or DEFAULT_MOBILE_HOST, help="Bind host. Defaults to 127.0.0.1.")
    parser.add_argument("--port", type=parse_port, default=parse_port(os.environ.get("PORT") or str(DEFAULT_PORT)), help="Bind port.")
    parser.add_argument("--lan", action="store_true", help="Bind to 0.0.0.0 so other devices can access this phone.")
    parser.add_argument("--api-key", default="", help="DeepSeek API key for this run.")
    parser.add_argument("--tavily-api-key", default="", help="Tavily API key for search in this run.")
    parser.add_argument("--auth-disabled", action="store_true", help="Disable local token auth for this run.")
    parser.add_argument("--ocr", action="store_true", help="Enable OCR if the phone environment has OCR dependencies installed.")
    parser.add_argument("--no-open", action="store_true", help="Print the URL without trying to open the browser.")
    parser.add_argument("--no-prompt", action="store_true", help="Do not prompt for missing API keys.")
    return parser.parse_args(argv)


def configure_environment(args: argparse.Namespace) -> tuple[str, int]:
    host = LAN_HOST if args.lan else str(args.host or DEFAULT_MOBILE_HOST).strip() or DEFAULT_MOBILE_HOST
    port = int(args.port)
    os.environ["HOST"] = host
    os.environ["PORT"] = str(port)
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    os.environ["OCR_ENABLED"] = "1" if args.ocr else os.environ.get("OCR_ENABLED", "0")

    if args.api_key:
        os.environ["DEEPSEEK_API_KEY"] = args.api_key.strip()
    elif not args.no_prompt and not os.environ.get("DEEPSEEK_API_KEY") and sys.stdin.isatty():
        key = getpass.getpass("DeepSeek API Key (Enter to skip and fill it in web settings): ").strip()
        if key:
            os.environ["DEEPSEEK_API_KEY"] = key

    if args.tavily_api_key:
        os.environ["TAVILY_API_KEY"] = args.tavily_api_key.strip()

    if args.auth_disabled:
        os.environ["AUTH_DISABLED"] = "1"

    return host, port


def open_mobile_browser(url: str) -> bool:
    opener = shutil.which("termux-open-url")
    if opener:
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def print_mobile_banner(computer_url: str, phone_url: str, opened: bool) -> None:
    print("", flush=True)
    print("DeepSeek Infra is running on this phone.", flush=True)
    print(f"Open on this phone: {computer_url}", flush=True)
    print(f"LAN URL: {phone_url}", flush=True)
    if opened:
        print("Browser open requested. Keep this terminal session running.", flush=True)
    else:
        print("Copy the local URL into your phone browser. Keep this terminal session running.", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    print("", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    host, port = configure_environment(args)

    from deepseek_infra.app import prepare_and_start, shutdown_handle

    handle = prepare_and_start(host=host, port=port, serve=False)
    opened = False if args.no_open else open_mobile_browser(handle.computer_url)
    print_mobile_banner(handle.computer_url, handle.phone_url, opened)
    try:
        handle.server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nStopping DeepSeek Infra...", flush=True)
    finally:
        shutdown_handle(handle)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
