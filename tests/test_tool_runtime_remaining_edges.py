from __future__ import annotations

import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.mcp import bridge, executor
from deepseek_infra.infra.tool_runtime import tools


def test_agent_tool_definitions_isolates_registry_refresh_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge.external_mcp_registry, "refresh", lambda: (_ for _ in ()).throw(RuntimeError("offline")))

    definitions = tools.agent_tool_definitions()

    assert definitions


def test_schema_for_external_tool_isolates_registry_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge.external_mcp_registry, "get_profile", lambda _name: (_ for _ in ()).throw(RuntimeError("offline")))

    assert tools.schema_for_tool("mcp__offline__echo") is None


def test_execute_tool_call_dispatches_external_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(executor, "call_external_mcp_tool", lambda name, arguments, policy, **kwargs: {"ok": True, "tool": name, "result": arguments})

    result = tools.execute_tool_call({"function": {"name": "mcp__remote__echo", "arguments": {"message": "hello"}}})

    assert result["ok"] is True
    assert result["result"] == {"message": "hello"}


@pytest.mark.parametrize(
    ("name", "arguments", "target"),
    [
        ("search_files", {"query": "needle", "limit": 3}, "search_files"),
        ("compare_search_results", {"queries": ["a"], "intent": "compare"}, "compare_search_results"),
        ("recall_memory", {"query": "memory"}, "recall_memory_tool"),
        ("forget_memory", {"query": "memory"}, "forget_memory_tool"),
        ("list_project_files", {"projectId": "project"}, "list_project_files_tool"),
        ("read_file_chunk", {"fileId": "file", "chunkIndex": 2, "projectId": "project"}, "read_file_chunk_tool"),
        ("generate_chart", {"type": "line", "title": "Trend", "data": [{"label": "A", "value": 1}]}, "generate_chart"),
    ],
)
def test_execute_tool_call_dispatches_remaining_helpers(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    arguments: dict[str, Any],
    target: str,
) -> None:
    monkeypatch.setattr(tools, target, lambda *_args, **_kwargs: {"dispatched": target})

    result = tools.execute_tool_call(
        {"function": {"name": name, "arguments": arguments}},
        web_search_callback=lambda _query, _intent: {"results": []},
    )

    assert result["ok"] is True
    assert result["result"]["dispatched"] == target


def test_rust_policy_unknown_tool_and_fail_closed_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "rust_policy_enabled", lambda: True)
    monkeypatch.setattr(tools, "rust_policy_fallback_enabled", lambda: False)
    monkeypatch.setattr(tools, "tool_metadata", lambda _name: None)
    assert tools._evaluate_rust_policy("unknown", {}, None) is None

    metadata = SimpleNamespace(network=False, filesystem=True, capability="general", risk="high")
    monkeypatch.setattr(tools, "tool_metadata", lambda _name: metadata)
    monkeypatch.setattr(
        tools,
        "rust_check_path",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False, allowed=False, reason="offline"),
    )
    denied = tools._evaluate_rust_policy("read_file_chunk", {"path": "../secret"}, None)
    assert denied is not None
    assert denied["ok"] is False

    metadata.filesystem = False
    monkeypatch.setattr(
        tools,
        "rust_check_capability",
        lambda *_args, **_kwargs: SimpleNamespace(ok=False, allowed=False, reason="offline"),
    )
    denied = tools._evaluate_rust_policy("python_eval", {}, None)
    assert denied is not None
    assert denied["ok"] is False


def test_compact_artifact_and_mindmap_guard_paths() -> None:
    failed = {"ok": False, "tool": "create_pptx", "error": "failed"}
    invalid_result = {"ok": True, "tool": "create_document", "result": "invalid"}

    assert tools.compact_artifact_tool_output(failed) is failed
    assert tools.compact_artifact_tool_output(invalid_result) is invalid_result
    assert tools._compact_mindmap_outline([{"label": "too deep"}], depth=4) == []
    assert tools._compact_mindmap_outline([None, {"label": "kept"}]) == [{"label": "kept"}]


def test_python_eval_rejects_empty_subprocess_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=""))

    with pytest.raises(AppError) as exc_info:
        tools.python_eval("1 + 1")

    assert exc_info.value.code == ErrorCode.INTERNAL


def test_list_reminders_notified_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "load_reminders", lambda: [{"id": "a", "notified": False}, {"id": "b", "notified": True}])

    result = tools.list_reminders_tool("notified")

    assert [item["id"] for item in result["reminders"]] == ["b"]


def test_project_and_chunk_error_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "read_project", lambda _project_id: None)
    with pytest.raises(AppError) as exc_info:
        tools.list_project_files_tool("missing")
    assert exc_info.value.code == ErrorCode.NOT_FOUND

    monkeypatch.setattr(tools, "list_projects", lambda: [{"id": "project", "name": "Project", "documents": [None, {"name": "ok"}]}])
    assert tools.list_project_files_tool()["count"] == 1

    monkeypatch.setattr(tools, "load_cached_file", lambda *_args, **_kwargs: {"chunks": ["invalid"]})
    with pytest.raises(AppError) as exc_info:
        tools.read_file_chunk_tool("file", chunk_index=1)
    assert exc_info.value.code == ErrorCode.NOT_FOUND


def test_transform_guards_and_non_dict_inputs() -> None:
    result = tools.transform_extract_regex("x" * 120, "x")
    assert result["count"] == 100
    assert tools.read_simple_json_path({"a": 1}, "$") == {"a": 1}
    assert tools.read_simple_json_path({"a": 1}, "$a") == 1
    with pytest.raises(AppError):
        tools.read_simple_json_path({"a": 1}, "$.a!")

    chart = tools.generate_chart("bar", "Title", [None, {"label": "A", "value": 1}])
    assert chart["data"] == [{"label": "A", "value": 1.0}]

    compared = tools.compare_search_results(
        ["query"],
        "general",
        lambda _query, _intent: {"results": [None, {"url": "https://example.com", "title": "Example"}]},
    )
    assert len(compared["results"]) == 1


def test_search_result_and_cached_file_malformed_inputs(tmp_path: Path) -> None:
    assert tools.search_result_key("http://[") == "http://["
    malformed = tmp_path / "broken.json"
    malformed.write_text("{", encoding="utf-8")
    assert tools.read_cached_file(malformed) is None


def test_public_url_parsing_and_resolution_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError):
        tools.resolve_public_url("http://[")
    with pytest.raises(AppError):
        tools.resolve_public_url("https://example.com:bad")

    calls: list[tuple[str, int | None]] = []

    def record_resolution(host: str, port: int | None) -> list[str]:
        calls.append((host, port))
        return ["93.184.216.34"]

    monkeypatch.setattr(tools, "resolve_public_host", record_resolution)
    tools.ensure_public_host("EXAMPLE.COM.")
    assert calls == [("example.com", None)]
    assert tools.normalize_url_host("bad%zone") == "bad%zone"
    with pytest.raises(AppError):
        tools.normalize_url_host("\ud800")
    assert tools.format_host_header("2001:db8::1", 443) == "[2001:db8::1]:443"


def test_public_host_and_address_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools.socket, "getaddrinfo", lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.gaierror("dns")))
    with pytest.raises(AppError) as exc_info:
        tools.resolve_public_host("example.invalid", 443)
    assert exc_info.value.code == ErrorCode.UPSTREAM_FAILURE

    monkeypatch.setattr(tools.socket, "getaddrinfo", lambda *_args, **_kwargs: [])
    with pytest.raises(AppError):
        tools.resolve_public_host("example.invalid", 443)

    with pytest.raises(AppError):
        tools.ensure_public_address("not-an-ip")


def test_policy_redaction_targets_hide_credentials_and_query(monkeypatch: pytest.MonkeyPatch) -> None:
    assert "secret" not in tools._redact_policy_text("Authorization: Bearer secret-token").lower()
    assert tools._redact_policy_target("", is_url=True) == ""
    assert tools._redact_policy_target("C:/workspace/report.txt", is_url=False).endswith("/report.txt")
    assert tools._redact_policy_target("https://example.com/path?token=secret", is_url=True) == "https://example.com/path"
    assert tools._redact_policy_target("https://[2001:db8::1]:8443/path", is_url=True) == "https://[2001:db8::1]:8443/path"
    assert tools._redact_policy_target("not a url", is_url=True) == "<invalid-url>"
    monkeypatch.setattr(tools, "urlsplit", lambda _value: (_ for _ in ()).throw(ValueError("bad")))
    assert tools._redact_policy_target("https://example.com", is_url=True) == "<invalid-url>"


def test_search_files_skips_corrupt_cache_and_merges_keyword_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError):
        tools.search_files("")
    bad = tmp_path / "bad.json"
    good = tmp_path / "good.json"
    monkeypatch.setattr(tools, "iter_cached_file_paths", lambda: [(bad, ""), (good, "proj")])
    monkeypatch.setattr(
        tools,
        "read_cached_file",
        lambda path: None
        if path == bad
        else {"id": "file", "name": "notes", "kind": "text", "chunks": [None, {"index": 0, "text": "needle text", "lineStart": 1, "lineEnd": 2}]},
    )
    monkeypatch.setattr(tools.local_rag, "index_file_payload", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tools.local_rag, "search_files_index", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(tools, "local_text_vector", lambda _text: [1.0])
    monkeypatch.setattr(tools, "cosine_similarity", lambda *_args: 1.0)
    result = tools.search_files("needle", limit=2)
    assert result["searchedFiles"] == 1
    assert result["matches"][0]["projectId"] == "proj"


def test_iter_cached_file_paths_and_read_array_cache(tmp_settings: Path) -> None:
    global_cache = tools.FILE_CACHE_DIR
    project_cache = tools.PROJECTS_DIR / "proj_x" / "files"
    global_cache.mkdir(parents=True)
    project_cache.mkdir(parents=True)
    (global_cache / "global.json").write_text("{}", encoding="utf-8")
    (project_cache / "project.json").write_text("{}", encoding="utf-8")
    paths = tools.iter_cached_file_paths()
    assert (global_cache / "global.json", "") in paths
    assert (project_cache / "project.json", "proj_x") in paths
    array = global_cache / "array.json"
    array.write_text("[]", encoding="utf-8")
    assert tools.read_cached_file(array) is None


def test_locked_http_connections_use_pinned_address(monkeypatch: pytest.MonkeyPatch) -> None:
    target = tools.PublicUrlTarget(
        url="http://example.com/",
        scheme="http",
        host="example.com",
        port=80,
        address="93.184.216.34",
        request_target="/",
        host_header="example.com",
    )
    calls: list[tuple[object, ...]] = []
    fake_socket = SimpleNamespace()

    def create_connection(*args: object) -> object:
        calls.append(args)
        return fake_socket

    monkeypatch.setattr(tools.socket, "create_connection", create_connection)
    http = tools.LockedHTTPConnection(target, 2)
    http.connect()
    assert calls[0][0] == ("93.184.216.34", 80)
    assert http.sock is fake_socket
    assert isinstance(tools.public_http_connection(target, 2), tools.LockedHTTPConnection)

    context = SimpleNamespace(wrap_socket=lambda sock, *, server_hostname: ("tls", sock, server_hostname))
    monkeypatch.setattr(tools.ssl, "create_default_context", lambda: context)
    secure_target = tools.PublicUrlTarget(
        url="https://example.com/",
        scheme="https",
        host="example.com",
        port=443,
        address="93.184.216.34",
        request_target="/",
        host_header="example.com",
    )
    https = tools.LockedHTTPSConnection(secure_target, 2)
    https.connect()
    assert https.sock == ("tls", fake_socket, "example.com")
    assert isinstance(tools.public_http_connection(secure_target, 2), tools.LockedHTTPSConnection)


class _Response332:
    def __init__(self, status: int, *, location: str = "", body: bytes = b"ok", content_type: str = "text/plain") -> None:
        self.status = status
        self.location = location
        self.body = body
        self.content_type = content_type

    def getheader(self, name: str, default: str = "") -> str:
        if name == "Location":
            return self.location
        if name == "Content-Type":
            return self.content_type
        return default

    def read(self, _limit: int) -> bytes:
        return self.body


class _Connection332:
    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.closed = False

    def request(self, *_args: object, **_kwargs: object) -> None:
        if self.error:
            raise self.error

    def getresponse(self) -> object:
        return self.response

    def close(self) -> None:
        self.closed = True


def test_fetch_public_url_redirect_http_error_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    target = tools.PublicUrlTarget("https://example.com/start", "https", "example.com", 443, "example.com", "/start", "93.184.216.34")
    redirected = tools.PublicUrlTarget("https://example.com/final", "https", "example.com", 443, "example.com", "/final", "93.184.216.34")
    connections = [_Connection332(_Response332(302, location="/final")), _Connection332(_Response332(200, body=b"done"))]
    monkeypatch.setattr(tools, "public_http_connection", lambda *_args: connections.pop(0))
    monkeypatch.setattr(tools, "resolve_public_url", lambda _url: redirected)
    assert tools.fetch_public_url(target) == (b"done", "text/plain", "https://example.com/final")

    failed = _Connection332(_Response332(503))
    monkeypatch.setattr(tools, "public_http_connection", lambda *_args: failed)
    with pytest.raises(AppError) as caught:
        tools.fetch_public_url(target)
    assert caught.value.code == ErrorCode.UPSTREAM_FAILURE and failed.closed

    timed_out = _Connection332(error=TimeoutError("timed out"))
    monkeypatch.setattr(tools, "public_http_connection", lambda *_args: timed_out)
    with pytest.raises(AppError) as caught:
        tools.fetch_public_url(target)
    assert caught.value.code == ErrorCode.UPSTREAM_TIMEOUT and timed_out.closed


def test_fetch_url_cache_and_readable_text_fallbacks(tmp_settings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = "https://example.com/page"
    path = tools.fetch_url_cache_path(url)
    path.parent.mkdir(parents=True)
    path.write_text("[]", encoding="utf-8")
    assert tools.load_fetch_url_cache(url) is None
    path.write_text('{"fetchedAt":0,"result":{}}', encoding="utf-8")
    assert tools.load_fetch_url_cache(url) is None
    path.write_text('{"fetchedAt":999999999999,"result":"bad"}', encoding="utf-8")
    assert tools.load_fetch_url_cache(url) is None
    tools.save_fetch_url_cache(url, {"content": "ok"})
    assert tools.load_fetch_url_cache(url) == {"content": "ok"}
    monkeypatch.setitem(__import__("sys").modules, "trafilatura", SimpleNamespace(extract=lambda *_args, **_kwargs: " extracted \n\n\n text "))
    assert tools.extract_readable_text(b"raw", "text/plain") == "extracted \n\n text"
    monkeypatch.setitem(__import__("sys").modules, "trafilatura", SimpleNamespace(extract=lambda *_args, **_kwargs: ""))
    assert "Title" in tools.extract_readable_text(b"<html><title>Title</title></html>", "text/html")
