from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.web import server


def _decoded(events: Any) -> list[dict[str, Any]]:
    return [json.loads(item) for item in events]


def test_multipart_module_validation_reports_missing_and_uninspectable_api() -> None:
    assert "parse_options_header" in str(server.multipart_module_issue(SimpleNamespace()))
    candidate = SimpleNamespace(parse_options_header=lambda value: value, MultipartParser=MagicMock())
    with patch.object(server, "signature", side_effect=ValueError("opaque")):
        assert server.multipart_module_issue(candidate) == "MultipartParser signature could not be inspected"

    class Parser:
        def __init__(self, content_length: int) -> None:
            self.content_length = content_length

    candidate.MultipartParser = Parser
    assert "missing parameters" in str(server.multipart_module_issue(candidate))


@pytest.mark.parametrize(
    ("headers", "message", "code"),
    [
        ({"Content-Type": "application/json", "Content-Length": "1"}, "Expected multipart", ErrorCode.INVALID_PAYLOAD),
        ({"Content-Type": "multipart/form-data; boundary=x", "Content-Length": "0"}, "empty", ErrorCode.INVALID_PAYLOAD),
        ({"Content-Type": "multipart/form-data; boundary=x", "Content-Length": str(server.MAX_UPLOAD_BYTES + 1)}, "too large", ErrorCode.UPLOAD_TOO_LARGE),
    ],
)
def test_multipart_reader_rejects_invalid_envelopes(headers: dict[str, str], message: str, code: ErrorCode) -> None:
    request = SimpleNamespace(headers=headers)
    with pytest.raises(AppError, match=message) as caught:
        asyncio.run(server.read_multipart_form(cast(Any, request)))
    assert caught.value.code == code


def test_multipart_reader_reports_missing_or_shadowed_dependency() -> None:
    request = SimpleNamespace(headers={"Content-Type": "multipart/form-data; boundary=x", "Content-Length": "10"})
    with patch.object(server, "multipart_module", None), pytest.raises(AppError) as caught:
        asyncio.run(server.read_multipart_form(cast(Any, request)))
    assert caught.value.code == ErrorCode.INTERNAL


def test_cascade_stream_emits_reasoning_content_and_done() -> None:
    output = {
        "id": "id",
        "model": "model",
        "reasoning": "why",
        "content": "answer",
        "usage": {"total_tokens": 2},
        "memorySuggestions": [{"content": "remember"}],
    }
    events: list[dict[str, Any]] = []
    with patch.object(server, "call_deepseek_cascade", return_value=output):
        server.emit_cascade_as_stream({}, events.append)
    assert [event["type"] for event in events] == ["reasoning", "content", "done"]


@pytest.mark.parametrize("mode", ["agent", "cascade", "chat"])
def test_chat_event_stream_routes_each_runtime_mode(mode: str) -> None:
    payload: dict[str, Any] = {"agentMode": mode == "agent"}

    def emit(_payload: dict[str, Any], callback: Any, **_kwargs: Any) -> None:
        callback({"type": "content", "text": mode})

    with (
        patch.object(server, "stream_multi_agent", side_effect=emit),
        patch.object(server, "model_router_cascade_requested", return_value=mode == "cascade"),
        patch.object(server, "emit_cascade_as_stream", side_effect=lambda _payload, callback: callback({"type": "content", "text": mode})),
        patch.object(server, "stream_deepseek", side_effect=emit),
    ):
        events = _decoded(server.chat_event_stream(payload))
    assert events == [{"type": "content", "text": mode}]


def test_chat_event_stream_swallows_cancel_and_reports_internal_crash() -> None:
    with patch.object(server, "stream_deepseek", side_effect=server.RequestCancelled()):
        assert list(server.chat_event_stream({})) == []
    with patch.object(server, "stream_deepseek", side_effect=RuntimeError("worker crashed")):
        events = _decoded(server.chat_event_stream({}))
    assert events[-1]["code"] == ErrorCode.INTERNAL.value


def test_agent_run_event_stream_stops_at_terminal_cursor() -> None:
    with (
        patch.object(server, "agent_run_events_after", return_value=[{"index": 2, "type": "status"}]),
        patch.object(server, "load_agent_run", return_value={"status": "done", "nextIndex": 3}),
    ):
        assert _decoded(server.agent_run_event_stream("run_test", -1)) == [{"index": 2, "type": "status"}]


@pytest.mark.parametrize("action", ["list", "create", "delete"])
def test_reminder_action_dispatches_supported_operations(action: str) -> None:
    with (
        patch.object(server, "load_reminders", return_value=[{"id": "r"}]),
        patch.object(server, "create_reminder", return_value={"id": "r"}),
        patch.object(server, "delete_reminder", return_value=1),
    ):
        result = server.reminder_action({"action": action, "id": "r"})
    assert result
    with pytest.raises(AppError):
        server.reminder_action({"action": "unknown"})


def test_conversation_search_rejects_invalid_collection_and_truncates_fields() -> None:
    assert server.conversation_search({"query": ""}) == {"results": []}
    with pytest.raises(AppError):
        server.conversation_search({"query": "x", "conversations": {}})
    result = server.conversation_search(
        {
            "query": "needle",
            "conversations": [None, {"id": "c", "title": "needle title", "tags": [" needle ", ""], "messages": [None, {"id": "m", "role": "user", "content": "a needle b"}]}],
        }
    )
    assert len(result["results"][0]["matches"]) == 3


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/api/agent-runs/run_abc", ("run_abc", "")),
        ("/api/agent-runs/run_abc/resume", ("run_abc", "resume")),
    ],
)
def test_agent_action_path_parser(path: str, expected: tuple[str, str]) -> None:
    assert server.parse_agent_run_action(path) == expected
    with pytest.raises(AppError):
        server.parse_agent_run_action("/wrong")
    with pytest.raises(AppError):
        server.parse_agent_run_action("/api/agent-runs/run/x/extra")


def test_server_helpers_cover_cursors_forms_shares_and_error_translation() -> None:
    assert server.parse_event_cursor("bad") == -1
    assert server.format_upload_limit(0) == "1 MB"
    assert server.first_form_value({}, "missing") == ""
    assert server.first_form_value({"x": ["  value  "]}, "x") == "value"
    assert server.share_target_prompt(title="", text="", url="") == ""
    assert "Title: T" in server.share_target_prompt(title="T", text="body", url="https://x")
    assert server.pop_share_target_payload("") is None
    share_id = server.store_share_target_payload({"text": "x"})
    assert server.pop_share_target_payload(share_id) == {"text": "x"}
    assert server.pop_share_target_payload(share_id) is None
    assert server.translate_multipart_error(RuntimeError("plain")) is None
    error = RuntimeError("large")
    error.http_status = 413  # type: ignore[attr-defined]
    assert server.translate_multipart_error(error).code == ErrorCode.UPLOAD_TOO_LARGE  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("cached", "expected"),
    [
        ({"kind": "pdf"}, "application/pdf"),
        ({"kind": "image", "type": "image/png"}, "image/png"),
        ({"type": "text/plain"}, "text/plain; charset=utf-8"),
        ({"kind": "md"}, "text/plain; charset=utf-8"),
        ({"type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ({"type": "text/html"}, "application/octet-stream"),
    ],
)
def test_original_file_media_type_is_safe(cached: dict[str, Any], expected: str) -> None:
    assert server.original_file_media_type(cached) == expected


def test_sensitive_query_redacts_absolute_and_relative_tokens() -> None:
    value = "https://host/path?token=secret&x=1 /api/open?token=other"
    redacted = server.redact_sensitive_query(value)
    assert "secret" not in redacted and "other" not in redacted
    assert redacted.count("%5Bredacted%5D") == 2


def test_create_server_retries_ports_and_closes_failed_socket() -> None:
    failed_socket = MagicMock()
    good_socket = MagicMock()
    good_socket.getsockname.return_value = ("127.0.0.1", 8124)
    with (
        patch.object(server, "open_bind_socket", side_effect=[OSError("busy"), good_socket]),
        patch.object(server, "create_app", return_value=MagicMock()),
        patch.object(server, "FastAPIServer", side_effect=lambda app, sock, host, port: (app, sock, host, port)),
    ):
        created, port = server.create_server(8123)
    assert port == 8124 and cast(Any, created)[3] == 8124
    failed_socket.close.assert_not_called()


def test_create_server_reports_exhausted_port_range() -> None:
    with patch.object(server, "open_bind_socket", side_effect=OSError("busy")), pytest.raises(SystemExit, match="No available port"):
        server.create_server(8123)


def test_resolve_static_file_rejects_path_escape(tmp_path: Path) -> None:
    with patch.object(server, "STATIC_DIR", tmp_path):
        assert server.resolve_static_file("../secret") is None


def _route_endpoint(app: Any, path: str, method: str) -> Any:
    return next(route.endpoint for route in app.routes if getattr(route, "path", "") == path and method in getattr(route, "methods", set()))


def test_semantic_cache_endpoint_covers_status_clear_and_invalid_actions() -> None:
    endpoint = _route_endpoint(server.create_app(), "/api/semantic-cache", "POST")
    request = cast(Any, SimpleNamespace())
    with (
        patch.object(server, "require_api_auth"),
        patch.object(server, "read_json_body", AsyncMock(return_value={"action": "status"})),
        patch.object(server, "semantic_cache_status", return_value={"entries": 2}),
    ):
        response = asyncio.run(endpoint(request))
    assert response.status_code == 200 and b'"entries":2' in response.body

    with (
        patch.object(server, "require_api_auth"),
        patch.object(server, "read_json_body", AsyncMock(return_value={"action": "clear"})),
        patch.object(server, "clear_semantic_cache", return_value={"ok": True}),
    ):
        assert asyncio.run(endpoint(request)).status_code == 200
    with patch.object(server, "require_api_auth"), patch.object(server, "read_json_body", AsyncMock(return_value={"action": "bad"})), pytest.raises(AppError):
        asyncio.run(endpoint(request))


def test_agent_run_action_endpoint_covers_plan_rerun_resume_and_rejections() -> None:
    endpoint = _route_endpoint(server.create_app(), "/api/agent-runs/{run_id}/{action}", "POST")
    request = cast(Any, SimpleNamespace())
    base_payload = {"messages": [{"role": "user", "content": "q"}]}

    async def invoke(action: str, stored_status: str, body: dict[str, Any]) -> Any:
        stored = {"runId": "run_x", "status": stored_status, "requestPayload": base_payload, "events": []}
        with (
            patch.object(server, "require_api_auth"),
            patch.object(server, "read_json_body", AsyncMock(return_value=body)),
            patch.object(server, "load_agent_run", return_value=stored),
            patch.object(server, "preflight_deepseek_payload"),
            patch.object(server.agent_run_registry, "ensure_started", return_value=True),
            patch.object(server, "public_agent_run", side_effect=lambda value: value),
        ):
            return await endpoint(request, "run_x", action)

    assert asyncio.run(invoke("plan", "awaiting_plan", {"plan": []})).status_code == 200
    assert asyncio.run(invoke("rerun", "failed", {"agentId": "coder", "resynthesize": False})).status_code == 200
    assert asyncio.run(invoke("resume", "failed", {})).status_code == 200

    rejection_cases = [
        ("plan", "running", {}),
        ("plan", "awaiting_plan", {"plan": "bad"}),
        ("rerun", "running", {"agentId": "coder"}),
        ("rerun", "failed", {}),
        ("resume", "running", {}),
        ("resume", "awaiting_plan", {}),
        ("unknown", "failed", {}),
    ]
    for action, status, body in rejection_cases:
        with pytest.raises(AppError):
            asyncio.run(invoke(action, status, body))


class _Part332:
    def __init__(self, *, filename: str = "", name: str = "", size: int = 0, raw: bytes = b"", value: str = "") -> None:
        self.filename = filename
        self.name = name
        self.size = size
        self.raw = raw
        self.value = value
        self.content_type = "text/plain"
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _multipart_request332() -> Any:
    return cast(
        Any,
        SimpleNamespace(
            headers={"Content-Type": "multipart/form-data; boundary=x", "Content-Length": "10"},
            body=AsyncMock(return_value=b"payload"),
        ),
    )


def test_multipart_parser_file_field_and_cleanup_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    parts = [_Part332(filename="safe.txt", raw=b"ok", size=2), _Part332(name="title", value=" value ")]
    module = SimpleNamespace(
        parse_options_header=lambda _value: ("multipart/form-data", {"boundary": "x"}),
        MultipartParser=lambda *_args, **_kwargs: iter(parts),
    )
    monkeypatch.setattr(server, "multipart_module", module)
    monkeypatch.setattr(server, "supported_multipart_module", lambda _module: True)
    fields, uploads = asyncio.run(server.read_multipart_form(_multipart_request332()))
    assert fields == {"title": [" value "]}
    assert uploads[0]["filename"] == "safe.txt"
    assert all(part.closed for part in parts)

    monkeypatch.setattr(server, "MAX_MULTIPART_FILES", 0)
    with pytest.raises(AppError) as caught:
        asyncio.run(server.read_multipart_form(_multipart_request332()))
    assert caught.value.code == ErrorCode.UPLOAD_TOO_LARGE


def test_multipart_parser_size_and_translation_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    module = SimpleNamespace(parse_options_header=lambda _value: ("multipart/form-data", {"boundary": "x"}))
    monkeypatch.setattr(server, "multipart_module", module)
    monkeypatch.setattr(server, "supported_multipart_module", lambda _module: True)

    oversized_file = _Part332(filename="large.bin", size=server.MAX_UPLOAD_FILE_BYTES + 1)
    module.MultipartParser = lambda *_args, **_kwargs: iter([oversized_file])
    with pytest.raises(AppError) as caught:
        asyncio.run(server.read_multipart_form(_multipart_request332()))
    assert caught.value.code == ErrorCode.UPLOAD_TOO_LARGE and oversized_file.closed

    oversized_field = _Part332(name="text", size=server.MAX_MULTIPART_FIELD_BYTES + 1)
    module.MultipartParser = lambda *_args, **_kwargs: iter([oversized_field])
    with pytest.raises(AppError):
        asyncio.run(server.read_multipart_form(_multipart_request332()))
    assert oversized_field.closed

    class BrokenParser:
        def __iter__(self) -> Any:
            error = RuntimeError("bad upload")
            error.http_status = 400  # type: ignore[attr-defined]
            raise error

    module.MultipartParser = lambda *_args, **_kwargs: BrokenParser()
    with pytest.raises(AppError, match="Invalid multipart upload"):
        asyncio.run(server.read_multipart_form(_multipart_request332()))


def test_file_routes_reject_empty_all_failed_and_invalid_chunks() -> None:
    app = server.create_app()
    request = cast(Any, SimpleNamespace())
    file_text = _route_endpoint(app, "/api/file-text", "POST")
    with patch.object(server, "require_api_auth"), patch.object(server, "read_multipart_files", AsyncMock(return_value=([], True, ""))), pytest.raises(AppError):
        asyncio.run(file_text(request))

    upload = {"filename": "bad.bin", "content_type": "application/octet-stream", "data": b"bad"}
    with (
        patch.object(server, "require_api_auth"),
        patch.object(server, "read_multipart_files", AsyncMock(return_value=([upload], True, ""))),
        patch.object(server, "extract_uploaded_file", side_effect=AppError("bad file", code=ErrorCode.INVALID_PAYLOAD, status=422)),
        pytest.raises(AppError) as caught,
    ):
        asyncio.run(file_text(request))
    assert caught.value.status == 422

    file_chunk = _route_endpoint(app, "/api/file-chunk", "POST")
    with patch.object(server, "require_api_auth"), patch.object(server, "read_json_body", AsyncMock(return_value={"chunkIndex": "bad"})), pytest.raises(AppError):
        asyncio.run(file_chunk(request))
    with (
        patch.object(server, "require_api_auth"),
        patch.object(server, "read_json_body", AsyncMock(return_value={"fileId": "f", "chunkIndex": 2})),
        patch.object(server, "load_cached_file", return_value={"chunks": []}),
        pytest.raises(AppError),
    ):
        asyncio.run(file_chunk(request))
    with (
        patch.object(server, "require_api_auth"),
        patch.object(server, "read_json_body", AsyncMock(return_value={"fileId": "f", "chunkIndex": 1})),
        patch.object(server, "load_cached_file", return_value={"chunks": ["bad"]}),
        pytest.raises(AppError),
    ):
        asyncio.run(file_chunk(request))


def test_server_stream_static_share_and_redaction_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[dict[str, Any]] = []
    with patch.object(server, "call_deepseek_cascade", return_value={"id": "i", "model": "m", "content": "", "reasoning": ""}):
        server.emit_cascade_as_stream({}, events.append)
    assert [event["type"] for event in events] == ["done"]

    monkeypatch.setattr(server, "agent_run_events_after", lambda *_args: (_ for _ in ()).throw(BrokenPipeError()))
    assert list(server.agent_run_event_stream("run", -1)) == []
    with patch.object(server, "STATIC_DIR", tmp_path):
        assert server.resolve_static_file("") == tmp_path / "index.html"
        assert server.resolve_static_file("../") == tmp_path / "index.html"
    server._SHARE_TARGETS.clear()
    server._SHARE_TARGETS["old"] = (0, {"x": 1})
    server.cleanup_share_target_payloads(now=server.SHARE_TARGET_TTL_SECONDS + 1)
    assert "old" not in server._SHARE_TARGETS
    assert server.conversation_tags({"tags": "bad"}) == []
    assert server.conversation_search_matches({"messages": "bad"}, "missing") == []
    assert server.redact_sensitive_query("https://host/path?x=1 /api/open?x=2") == "https://host/path?x=1 /api/open?x=2"


def test_fastapi_server_close_and_create_server_failed_socket_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = server.FastAPIServer.__new__(server.FastAPIServer)
    instance._socket = cast(Any, SimpleNamespace(close=lambda: (_ for _ in ()).throw(OSError("already closed"))))
    instance.server_close()

    failed_socket = MagicMock()
    failed_socket.getsockname.return_value = ("127.0.0.1", 9000)
    monkeypatch.setattr(server, "open_bind_socket", MagicMock(side_effect=[failed_socket, OSError("busy")] + [OSError("busy")] * 18))
    monkeypatch.setattr(server, "create_app", lambda: MagicMock())
    monkeypatch.setattr(server, "FastAPIServer", MagicMock(side_effect=OSError("construct failed")))
    with pytest.raises(SystemExit):
        server.create_server(9000)
    failed_socket.close.assert_called_once()
