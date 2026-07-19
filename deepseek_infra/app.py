"""Application entry point for logging, cache cleanup, and HTTP server startup."""

from __future__ import annotations

import logging
import mimetypes
import sys
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

from deepseek_infra.core.config import DEFAULT_HOST, DEFAULT_PORT, STATIC_DIR, configure_logging, settings
from deepseek_infra.infra.rag.files import cleanup_file_cache
from deepseek_infra.infra.agent_runtime.agent_runs import mark_orphan_runs_on_startup, resume_orphaned_runs
from deepseek_infra.infra.gateway.scheduler import recover_orphans as recover_scheduler_orphans
from deepseek_infra.web.server import MULTIPART_IMPORT_ERROR, create_server, multipart_module, supported_multipart_module
from deepseek_infra.core.utils import local_ip, url_with_token
from deepseek_infra.infra.tool_runtime.search import cleanup_search_cache
from deepseek_infra.web.server import redact_sensitive_query

logger = logging.getLogger("deepseek_infra")
CACHE_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60


class ServerLifecycle(Protocol):
    def serve_forever(self, poll_interval: float | None = None) -> None: ...
    def shutdown(self) -> None: ...
    def server_close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ServerHandle:
    """Bundle of objects needed to interact with a running ASGI server."""

    server: ServerLifecycle
    port: int
    host: str
    computer_url: str
    phone_url: str
    stop_cache_cleanup: threading.Event


def main() -> None:
    handle = prepare_and_start(serve=False)
    log_server_started(handle.computer_url, handle.phone_url)
    try:
        handle.server.serve_forever()
    finally:
        handle.stop_cache_cleanup.set()


def prepare_and_start(
    host: str | None = None,
    port: int | None = None,
    serve: bool = True,
    on_started: Callable[[ServerHandle], None] | None = None,
) -> ServerHandle:
    """Prepare runtime dependencies and bind the FastAPI/ASGI server.

    When ``serve=True`` (the default for embedded callers) the server is started
    in a daemon background thread so the caller can keep doing other work and
    later call :func:`shutdown_handle` to stop it. The CLI entry point passes
    ``serve=False`` because it wants to call ``serve_forever`` on the main
    thread itself for clean Ctrl+C semantics.
    """
    configure_logging()
    if not STATIC_DIR.exists():
        raise SystemExit("Missing static directory")
    react_index = STATIC_DIR / "ui" / "index.html"
    if not react_index.is_file():
        raise RuntimeError("React frontend build is missing. Run scripts/build_frontend.py.")
    ensure_startup_dependencies()
    register_mimetypes()
    cleanup_runtime_caches()
    # One-time at startup (not in the periodic loop): opt-in resume of orphaned
    # Agent runs. No-op unless AGENT_RUNTIME_AUTO_RESUME is set.
    try:
        resume_orphaned_runs()
    except Exception:
        logger.exception("agent_run_auto_resume_failed")
    # Background recovery: reconcile request-queue rows left mid-flight by a crash
    # (mark stale running/queued as failed and dead-letter them). Best-effort.
    try:
        recovered = recover_scheduler_orphans()
        if recovered:
            logger.info("scheduler_recovered_orphans count=%d", recovered)
    except Exception:
        logger.exception("scheduler_orphan_recovery_failed")
    stop_event = start_periodic_cache_cleanup()

    bind_host = host or settings.default_host or DEFAULT_HOST
    bind_port = port or settings.default_port or DEFAULT_PORT
    server, actual_port = create_server(bind_port, host=bind_host)
    computer_url, phone_url = compute_urls(bind_host, actual_port)
    handle = ServerHandle(
        server=server,
        port=actual_port,
        host=bind_host,
        computer_url=computer_url,
        phone_url=phone_url,
        stop_cache_cleanup=stop_event,
    )

    if on_started is not None:
        try:
            on_started(handle)
        except Exception:
            logger.exception("on_started_callback_failed")

    if serve:
        thread = threading.Thread(
            target=server.serve_forever,
            name="deepseek-asgi-server",
            daemon=True,
        )
        thread.start()
    return handle


def shutdown_handle(handle: ServerHandle) -> None:
    """Stop the HTTP server and the cache cleanup thread."""
    handle.stop_cache_cleanup.set()
    try:
        handle.server.shutdown()
    finally:
        try:
            handle.server.server_close()
        except OSError:
            pass


def ensure_startup_dependencies() -> None:
    if multipart_module is None or not supported_multipart_module(multipart_module):
        raise SystemExit(MULTIPART_IMPORT_ERROR)


def register_mimetypes() -> None:
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("text/css", ".css")
    mimetypes.add_type("font/woff2", ".woff2")
    mimetypes.add_type("image/png", ".png")
    mimetypes.add_type("image/svg+xml", ".svg")
    mimetypes.add_type("image/x-icon", ".ico")
    mimetypes.add_type("application/manifest+json", ".webmanifest")


def cleanup_runtime_caches() -> None:
    for name, cleanup in (("file_cache", cleanup_file_cache), ("search_cache", cleanup_search_cache)):
        try:
            cleanup()
        except Exception:
            logger.exception("cache_cleanup_failed", extra={"cache": name})
    try:
        mark_orphan_runs_on_startup()
    except Exception:
        logger.exception("agent_run_orphan_mark_failed")


def start_periodic_cache_cleanup(interval_seconds: float = CACHE_CLEANUP_INTERVAL_SECONDS) -> threading.Event:
    stop_event = threading.Event()

    def loop() -> None:
        while not stop_event.wait(interval_seconds):
            cleanup_runtime_caches()

    thread = threading.Thread(target=loop, name="deepseek-cache-cleanup", daemon=True)
    thread.start()
    return stop_event


def compute_urls(host: str, port: int) -> tuple[str, str]:
    ip = local_ip()
    computer_url = f"http://127.0.0.1:{port}"
    phone_url = f"http://{ip}:{port}"
    if settings.auth.enabled:
        computer_url = url_with_token(computer_url + "/", settings.auth.token)
        phone_url = url_with_token(phone_url + "/", settings.auth.token)
    return computer_url, phone_url


def log_server_started(computer_url: str, phone_url: str) -> None:
    logger.info(
        "server_started",
        extra={
            "computer_url": redact_sensitive_query(computer_url),
            "phone_url": redact_sensitive_query(phone_url),
        },
    )
    stdout = getattr(sys, "stdout", None)
    if stdout is not None and stdout.isatty():
        print(f"Computer: {computer_url}", flush=True)
        print(f"Phone: {phone_url}", flush=True)


if __name__ == "__main__":
    main()
