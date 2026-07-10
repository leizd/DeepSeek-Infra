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
