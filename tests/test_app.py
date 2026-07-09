from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, call, patch

import pytest

import deepseek_infra.app as app_module


def test_startup_structured_log_redacts_token_urls() -> None:
    stdout = SimpleNamespace(isatty=lambda: False)

    with patch.object(app_module.sys, "stdout", stdout), patch.object(app_module.logger, "info") as info:
        app_module.log_server_started(
            "http://127.0.0.1:8000/?token=computer-secret",
            "http://192.168.1.2:8000/?token=phone-secret",
        )

    extra = info.call_args.kwargs["extra"]
    serialized = json.dumps(extra, ensure_ascii=False)
    assert "computer-secret" not in serialized
    assert "phone-secret" not in serialized
    assert "%5Bredacted%5D" in serialized


def test_startup_log_handles_windowed_stdout() -> None:
    with patch.object(app_module.sys, "stdout", None), patch.object(app_module.logger, "info") as info:
        app_module.log_server_started(
            "http://127.0.0.1:8000/",
            "http://192.168.1.2:8000/",
        )

    info.assert_called_once()


def test_cleanup_runtime_caches_runs_both_cleaners_and_swallows_errors() -> None:
    with (
        patch.object(app_module, "cleanup_file_cache", side_effect=RuntimeError("file boom")) as file_cleanup,
        patch.object(app_module, "cleanup_search_cache") as search_cleanup,
        patch.object(app_module, "mark_orphan_runs_on_startup") as orphan_cleanup,
        patch.object(app_module.logger, "exception") as log_exception,
    ):
        app_module.cleanup_runtime_caches()

    file_cleanup.assert_called_once_with()
    search_cleanup.assert_called_once_with()
    orphan_cleanup.assert_called_once_with()
    log_exception.assert_called_once()


def test_startup_dependency_check_fails_fast_for_incompatible_multipart() -> None:
    with patch.object(app_module, "multipart_module", None):
        try:
            app_module.ensure_startup_dependencies()
        except SystemExit as exc:
            assert "Multipart parser dependency" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected SystemExit")


def test_periodic_cache_cleanup_starts_daemon_thread() -> None:
    with patch.object(app_module.threading, "Thread") as thread_cls:
        stop_event = app_module.start_periodic_cache_cleanup(interval_seconds=999)

    assert hasattr(stop_event, "set")
    thread = thread_cls.call_args.kwargs
    assert thread["name"] == "deepseek-cache-cleanup"
    assert thread["daemon"] is True
    thread_cls.return_value.start.assert_called_once_with()


def test_register_mimetypes_adds_expected_types() -> None:
    with patch.object(app_module.mimetypes, "add_type") as add_type:
        app_module.register_mimetypes()

    suffixes = {call.args[1] for call in add_type.call_args_list}
    assert ".js" in suffixes
    assert ".css" in suffixes
    assert ".woff2" in suffixes
    assert ".webmanifest" in suffixes


def test_compute_urls_without_auth() -> None:
    with (
        patch.object(
            app_module,
            "settings",
            SimpleNamespace(auth=SimpleNamespace(enabled=False, token="")),
        ),
        patch.object(app_module, "local_ip", return_value="192.168.1.5"),
    ):
        computer_url, phone_url = app_module.compute_urls("127.0.0.1", 8000)

    assert computer_url == "http://127.0.0.1:8000"
    assert phone_url == "http://192.168.1.5:8000"


def test_compute_urls_with_auth() -> None:
    with (
        patch.object(
            app_module,
            "settings",
            SimpleNamespace(auth=SimpleNamespace(enabled=True, token="sekrit")),
        ),
        patch.object(app_module, "local_ip", return_value="192.168.1.5"),
        patch.object(app_module, "url_with_token", side_effect=lambda url, token: f"{url}?token={token}") as url_token,
    ):
        computer_url, phone_url = app_module.compute_urls("127.0.0.1", 8000)

    assert "token=sekrit" in computer_url
    assert "token=sekrit" in phone_url
    url_token.assert_has_calls(
        [
            call("http://127.0.0.1:8000/", "sekrit"),
            call("http://192.168.1.5:8000/", "sekrit"),
        ]
    )


def test_log_server_started_prints_to_tty() -> None:
    stdout = SimpleNamespace(isatty=lambda: True)
    with (
        patch.object(app_module.sys, "stdout", stdout),
        patch.object(app_module.logger, "info"),
        patch("builtins.print") as mock_print,
    ):
        app_module.log_server_started("http://127.0.0.1:8000/", "http://192.168.1.5:8000/")

    mock_print.assert_any_call("Computer: http://127.0.0.1:8000/", flush=True)
    mock_print.assert_any_call("Phone: http://192.168.1.5:8000/", flush=True)


def test_shutdown_handle_stops_server_and_cleanup() -> None:
    server = MagicMock()
    stop_event = MagicMock()
    handle = app_module.ServerHandle(
        server=server,
        port=8000,
        host="127.0.0.1",
        computer_url="http://127.0.0.1:8000/",
        phone_url="http://192.168.1.5:8000/",
        stop_cache_cleanup=stop_event,
    )
    app_module.shutdown_handle(handle)

    stop_event.set.assert_called_once_with()
    server.shutdown.assert_called_once_with()
    server.server_close.assert_called_once_with()


def test_cleanup_runtime_caches_succeeds_without_errors() -> None:
    with (
        patch.object(app_module, "cleanup_file_cache") as file_cleanup,
        patch.object(app_module, "cleanup_search_cache") as search_cleanup,
        patch.object(app_module, "mark_orphan_runs_on_startup") as orphan_cleanup,
        patch.object(app_module.logger, "exception") as log_exception,
    ):
        app_module.cleanup_runtime_caches()

    file_cleanup.assert_called_once_with()
    search_cleanup.assert_called_once_with()
    orphan_cleanup.assert_called_once_with()
    log_exception.assert_not_called()


def test_ensure_startup_dependencies_passes_with_supported_multipart() -> None:
    with (
        patch.object(app_module, "multipart_module", MagicMock()),
        patch.object(app_module, "supported_multipart_module", return_value=True),
    ):
        app_module.ensure_startup_dependencies()


@pytest.fixture
def _prepare_and_start_patches():
    server = MagicMock()
    stop_event = threading.Event()
    with (
        patch.object(app_module, "STATIC_DIR", MagicMock(exists=MagicMock(return_value=True))),
        patch.object(app_module, "configure_logging"),
        patch.object(app_module, "ensure_startup_dependencies"),
        patch.object(app_module, "register_mimetypes"),
        patch.object(app_module, "cleanup_runtime_caches"),
        patch.object(app_module, "resume_orphaned_runs"),
        patch.object(app_module, "recover_scheduler_orphans", return_value=0),
        patch.object(app_module, "start_periodic_cache_cleanup", return_value=stop_event),
        patch.object(app_module, "create_server", return_value=(server, 8001)),
        patch.object(
            app_module,
            "settings",
            SimpleNamespace(auth=SimpleNamespace(enabled=False, token="")),
        ),
        patch.object(app_module, "local_ip", return_value="192.168.1.5"),
    ):
        yield server, stop_event


def test_prepare_and_start_embedded_serve_false(_prepare_and_start_patches) -> None:
    server, stop_event = _prepare_and_start_patches
    server = cast(MagicMock, server)
    on_started = MagicMock()

    handle = app_module.prepare_and_start(
        host="127.0.0.1",
        port=8000,
        serve=False,
        on_started=on_started,
    )

    assert handle.server is server
    assert handle.port == 8001
    assert handle.host == "127.0.0.1"
    assert handle.stop_cache_cleanup is stop_event
    on_started.assert_called_once_with(handle)
    server.serve_forever.assert_not_called()


def test_prepare_and_start_starts_server_in_daemon_thread(_prepare_and_start_patches) -> None:
    server, stop_event = _prepare_and_start_patches
    server = cast(MagicMock, server)

    handle = app_module.prepare_and_start(host="127.0.0.1", port=8000, serve=True)

    assert handle.server is server
    assert handle.stop_cache_cleanup is stop_event
    server.serve_forever.assert_called_once()


def test_prepare_and_start_on_started_failure_is_logged(_prepare_and_start_patches) -> None:
    server, stop_event = _prepare_and_start_patches

    def _on_started_fail(handle: app_module.ServerHandle) -> None:
        raise RuntimeError("boom")

    with patch.object(app_module.logger, "exception") as log_exception:
        handle = app_module.prepare_and_start(
            host="127.0.0.1",
            port=8000,
            serve=False,
            on_started=_on_started_fail,
        )

    assert handle is not None
    log_exception.assert_called_once()


def test_prepare_and_start_exits_when_static_dir_missing() -> None:
    with patch.object(app_module, "STATIC_DIR", MagicMock(exists=MagicMock(return_value=False))):
        try:
            app_module.prepare_and_start(host="127.0.0.1", port=8000, serve=False)
        except SystemExit as exc:
            assert "Missing static directory" in str(exc)
        else:
            raise AssertionError("expected SystemExit")


def test_prepare_and_start_logs_resume_orphan_exception(_prepare_and_start_patches) -> None:
    server, stop_event = _prepare_and_start_patches
    with patch.object(app_module, "resume_orphaned_runs", side_effect=RuntimeError("boom")), patch.object(app_module.logger, "exception") as log_exception:
        handle = app_module.prepare_and_start(host="127.0.0.1", port=8000, serve=False)
    assert handle is not None
    log_exception.assert_any_call("agent_run_auto_resume_failed")


def test_prepare_and_start_logs_scheduler_orphan_exception(_prepare_and_start_patches) -> None:
    server, stop_event = _prepare_and_start_patches
    with patch.object(app_module, "recover_scheduler_orphans", side_effect=RuntimeError("boom")), patch.object(app_module.logger, "exception") as log_exception:
        handle = app_module.prepare_and_start(host="127.0.0.1", port=8000, serve=False)
    assert handle is not None
    log_exception.assert_any_call("scheduler_orphan_recovery_failed")


def test_prepare_and_start_logs_recovered_orphans(_prepare_and_start_patches) -> None:
    server, stop_event = _prepare_and_start_patches
    with patch.object(app_module, "recover_scheduler_orphans", return_value=3), patch.object(app_module.logger, "info") as log_info:
        handle = app_module.prepare_and_start(host="127.0.0.1", port=8000, serve=False)
    assert handle is not None
    log_info.assert_any_call("scheduler_recovered_orphans count=%d", 3)


def test_shutdown_handle_swallows_server_close_oserror() -> None:
    server = MagicMock()
    server.server_close.side_effect = OSError("boom")
    stop_event = MagicMock()
    handle = app_module.ServerHandle(
        server=server,
        port=8000,
        host="127.0.0.1",
        computer_url="http://127.0.0.1:8000/",
        phone_url="http://192.168.1.5:8000/",
        stop_cache_cleanup=stop_event,
    )
    app_module.shutdown_handle(handle)

    stop_event.set.assert_called_once_with()
    server.shutdown.assert_called_once_with()
    server.server_close.assert_called_once_with()


def test_cleanup_runtime_caches_swallows_orphan_mark_error() -> None:
    with (
        patch.object(app_module, "cleanup_file_cache") as file_cleanup,
        patch.object(app_module, "cleanup_search_cache") as search_cleanup,
        patch.object(app_module, "mark_orphan_runs_on_startup", side_effect=RuntimeError("boom")) as orphan_cleanup,
        patch.object(app_module.logger, "exception") as log_exception,
    ):
        app_module.cleanup_runtime_caches()

    file_cleanup.assert_called_once_with()
    search_cleanup.assert_called_once_with()
    orphan_cleanup.assert_called_once_with()
    log_exception.assert_called_once()


def test_periodic_cache_cleanup_invokes_cleanup_when_running() -> None:
    stop_event = MagicMock()
    stop_event.wait.side_effect = [False, True]
    with (
        patch.object(app_module.threading, "Event", return_value=stop_event),
        patch.object(app_module.threading, "Thread") as thread_cls,
        patch.object(app_module, "cleanup_runtime_caches") as cleanup,
    ):
        returned = app_module.start_periodic_cache_cleanup(interval_seconds=0.001)
        target = thread_cls.call_args.kwargs["target"]
        target()

    assert returned is stop_event
    cleanup.assert_called_once()
    stop_event.wait.assert_called_with(0.001)


def test_main_runs_server_forever_and_stops_cleanup() -> None:
    server = MagicMock()
    stop_event = threading.Event()
    handle = app_module.ServerHandle(
        server=server,
        port=8000,
        host="127.0.0.1",
        computer_url="http://127.0.0.1:8000/",
        phone_url="http://192.168.1.5:8000/",
        stop_cache_cleanup=stop_event,
    )
    with (
        patch.object(app_module, "prepare_and_start", return_value=handle),
        patch.object(app_module, "log_server_started") as log_started,
    ):
        app_module.main()

    log_started.assert_called_once_with(handle.computer_url, handle.phone_url)
    server.serve_forever.assert_called_once()
    assert stop_event.is_set()
