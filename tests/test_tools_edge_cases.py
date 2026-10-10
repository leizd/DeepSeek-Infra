"""Edge-case tests for tool_runtime/tools.py to raise coverage."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.tool_runtime import tool_policy as tool_policy_module
import deepseek_infra.infra.tool_runtime.tools as tools


def test_execute_tool_call_invalid_tool_name() -> None:
    result = tools.execute_tool_call({"function": {"name": "unknown_tool", "arguments": {}}, "id": "c1"})
    assert not result["ok"]
    assert result["code"] == ErrorCode.INVALID_PAYLOAD.value


def test_execute_tool_call_missing_capability() -> None:
    policy = tool_policy_module.ToolPolicy(allowed_tools=("ReadFile",))
    result = tools.execute_tool_call(
        {"function": {"name": "fetch_url", "arguments": {"url": "https://example.com"}}, "id": "c1"},
        policy=policy,
    )
    assert not result["ok"]


def test_execute_tool_call_policy_schema_deny() -> None:
    policy = tool_policy_module.ToolPolicy(enforce_schema=True)
    result = tools.execute_tool_call(
        {"function": {"name": "python_eval", "arguments": {}}, "id": "c1"},
        policy=policy,
    )
    assert not result["ok"]


def test_execute_tool_call_browser_action_error() -> None:
    with patch("deepseek_infra.infra.browser.actions.execute_browser_action", return_value={"ok": False, "error": "boom"}):
        result = tools.execute_tool_call(
            {"function": {"name": "browser_read_page", "arguments": {"sessionId": "s1"}}, "id": "c1"}
        )
    assert not result["ok"]


def test_execute_tool_call_python_eval() -> None:
    result = tools.execute_tool_call({"function": {"name": "python_eval", "arguments": {"expression": "2+2"}}, "id": "c1"})
    assert result["ok"]
    assert result["result"]["result"] == "4"


def test_python_eval_empty_and_too_long() -> None:
    with pytest.raises(AppError) as cm:
        tools.python_eval("")
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD
    with pytest.raises(AppError) as cm:
        tools.python_eval("1" * 1001)
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD


def test_python_eval_timeout() -> None:
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("python", 8)):
        with pytest.raises(AppError) as cm:
            tools.python_eval("1")
    assert cm.value.code == ErrorCode.UPSTREAM_TIMEOUT


def test_execute_tool_call_web_search_requires_callback() -> None:
    result = tools.execute_tool_call({"function": {"name": "web_search", "arguments": {"query": "x", "intent": "general"}}, "id": "c1"})
    assert not result["ok"]


def test_execute_tool_call_compare_search_requires_callback() -> None:
    result = tools.execute_tool_call({"function": {"name": "compare_search_results", "arguments": {"queries": ["a"], "intent": "compare"}}, "id": "c1"})
    assert not result["ok"]


def test_execute_tool_call_create_reminder_and_list(tmp_settings: Path) -> None:
    due = "2099-01-01T00:00:00Z"
    created = tools.execute_tool_call({"function": {"name": "create_reminder", "arguments": {"title": "T", "content": "C", "dueAt": due}}, "id": "c1"})
    assert created["ok"]
    listed = tools.execute_tool_call({"function": {"name": "list_reminders", "arguments": {"status": "active"}}, "id": "c2"})
    assert listed["ok"]
    assert listed["result"]["count"] >= 1


def test_list_reminders_invalid_status() -> None:
    result = tools.list_reminders_tool("invalid")
    assert result["status"] == "active"


def test_memory_tool_scopes() -> None:
    assert tools.memory_tool_scopes("project:alpha", "global") == ["project:alpha"]
    assert tools.memory_tool_scopes("global", "global") == ["global"]
    assert set(tools.memory_tool_scopes("", "project:alpha")) == {"global", "project:alpha"}


def test_forget_memory_empty() -> None:
    with pytest.raises(AppError) as cm:
        tools.forget_memory_tool("", default_scope="global")
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD


def test_project_document_for_tool_skips_non_dict() -> None:
    result = tools.project_document_for_tool({"name": "x", "fileId": "a" * 32})
    assert result["name"] == "x"


def test_list_project_files_empty() -> None:
    result = tools.list_project_files_tool("")
    assert "projects" in result


def test_read_file_chunk_missing(tmp_settings: Path) -> None:
    file_id = "a" * 32
    cached_path = tmp_settings / ".file-cache" / f"{file_id}.json"
    cached_path.parent.mkdir(parents=True)
    cached_path.write_text(json.dumps({"id": file_id, "name": "x.txt", "kind": "text", "chunks": []}), encoding="utf-8")
    with pytest.raises(AppError) as cm:
        tools.read_file_chunk_tool(file_id, chunk_index=1, project_id="")
    assert cm.value.code == ErrorCode.NOT_FOUND


def test_data_transform_unsupported() -> None:
    with pytest.raises(AppError) as cm:
        tools.data_transform("noop", "input")
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD


def test_transform_extract_regex_invalid() -> None:
    with pytest.raises(AppError) as cm:
        tools.transform_extract_regex("text", "[invalid")
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD

    with pytest.raises(AppError) as cm:
        tools.transform_extract_regex("text", "")
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD


def test_transform_json_path_errors() -> None:
    with pytest.raises(AppError) as cm:
        tools.transform_json_path("not json", "$")
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD

    with pytest.raises(AppError) as cm:
        tools.transform_json_path('{"a":1}', "$.a[0]")
    assert cm.value.code == ErrorCode.NOT_FOUND

    with pytest.raises(AppError) as cm:
        tools.transform_json_path('{"a":1}', "$.b")
    assert cm.value.code == ErrorCode.NOT_FOUND

    with pytest.raises(AppError) as cm:
        tools.transform_json_path('{"a":1}', "$.a[999]")
    assert cm.value.code == ErrorCode.NOT_FOUND


def test_compact_json_value_truncates() -> None:
    big = {"x": "y" * 5000}
    result = tools.compact_json_value(big)
    assert isinstance(result, str)
    assert len(result) <= 4000


def test_transform_csv_summary_edge_cases() -> None:
    assert tools.transform_csv_summary("", ",")["rows"] == 0
    result = tools.transform_csv_summary("a,b\n1", ",")
    assert result["numericColumns"][0]["count"] == 1


def test_number_summary_empty() -> None:
    result = tools.transform_number_summary("hello")
    assert result["count"] == 0


def test_generate_chart_edge_cases() -> None:
    with pytest.raises(AppError) as cm:
        tools.generate_chart("bar", "T", [])
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD

    with pytest.raises(AppError) as cm:
        tools.generate_chart("bar", "T", [{"label": "A", "value": None}, {"label": "B", "value": "x"}])
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD

    result = tools.generate_chart("pie", "T", [{"label": "A", "value": 1}, {"label": "", "value": 2}])
    assert result["type"] == "pie"


def test_compare_search_results_invalid() -> None:
    with pytest.raises(AppError) as cm:
        tools.compare_search_results("not a list", "general", lambda q, i: {"results": []})
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD

    with pytest.raises(AppError) as cm:
        tools.compare_search_results(["", "   "], "general", lambda q, i: {"results": []})
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD


def test_compare_search_results_dedup_and_callback(tmp_settings: Path) -> None:
    calls: list[tuple[str, str]] = []

    def cb(query: str, intent: str) -> dict[str, Any]:
        calls.append((query, intent))
        return {"results": [{"url": "https://example.com/a", "title": "A"}, {"url": "https://example.com/a", "title": "A2"}]}

    result = tools.compare_search_results(["q1"], "fresh", cb)
    assert result["queries"] == ["q1"]
    assert result["intent"] == "fresh"
    assert len(result["results"]) == 1


def test_search_files_empty_query() -> None:
    with pytest.raises(AppError) as cm:
        tools.search_files("")
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD


def test_compact_snippet_long_text() -> None:
    text = "a" * 2000
    snippet = tools.compact_snippet(text, "a")
    assert len(snippet) <= 700


def test_fetch_url_too_large(tmp_settings: Path) -> None:
    huge = b"x" * (tools.MAX_FETCH_BYTES + 10)

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "text/html"}

        def read(self, size: int = -1) -> bytes:
            return huge

        def getheader(self, name: str, default: str = "") -> str:
            return self.headers.get(name, default)

    class FakeConnection:
        def request(self, *args: object, **kwargs: object) -> None:
            pass

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            pass


    with (
        patch.object(tools, "resolve_public_host", return_value=["93.184.216.34"]),
        patch.object(tools, "public_http_connection", return_value=FakeConnection()),
    ):
        with pytest.raises(AppError) as cm:
            tools.fetch_url("https://example.com/huge")
    assert cm.value.code == ErrorCode.UPLOAD_TOO_LARGE


def test_validate_public_url() -> None:
    with pytest.raises(AppError) as cm:
        tools.validate_public_url("http://127.0.0.1")
    assert cm.value.code == ErrorCode.FORBIDDEN


def test_resolve_public_url_rejects_local_and_invalid() -> None:
    with pytest.raises(AppError) as cm:
        tools.resolve_public_url("http://localhost:8000")
    assert cm.value.code == ErrorCode.FORBIDDEN

    with pytest.raises(AppError) as cm:
        tools.resolve_public_url("ftp://example.com")
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD

    with pytest.raises(AppError) as cm:
        tools.resolve_public_url("https://user:pass@example.com")
    assert cm.value.code == ErrorCode.INVALID_PAYLOAD


def test_fetch_url_cache_roundtrip(tmp_settings: Path) -> None:
    url = "https://cache.example.com/page"
    tools.save_fetch_url_cache(url, {"text": "cached"})
    cached = tools.load_fetch_url_cache(url)
    assert cached is not None
    assert cached["text"] == "cached"
    stale = tools.load_fetch_url_cache("https://not.cached/")
    assert stale is None


def test_parse_tool_arguments() -> None:
    assert tools.parse_tool_arguments({"a": 1}) == {"a": 1}
    assert tools.parse_tool_arguments('{"a": 1}') == {"a": 1}
    assert tools.parse_tool_arguments("not json") == {}
    assert tools.parse_tool_arguments(None) == {}


def test_safe_limit() -> None:
    assert tools.safe_limit(5, default=1, maximum=10) == 5
    assert tools.safe_limit(100, default=1, maximum=10) == 10
    assert tools.safe_limit("abc", default=3, maximum=10) == 3


def test_tool_call_name_and_parallel_safety() -> None:
    assert tools.tool_call_name({"function": {"name": "x"}, "id": "c"}) == "x"
    assert tools.is_parallel_safe_tool({"function": {"name": "python_eval"}, "id": "c"}) is True
    assert tools.is_parallel_safe_tool({"function": {"name": "suggest_memory"}, "id": "c"}) is False


def test_agent_tool_definitions_external_mcp_best_effort() -> None:
    with patch("deepseek_infra.infra.mcp.bridge.external_mcp_registry", side_effect=Exception("no mcp")):
        tools.agent_tool_definitions()


def test_compact_artifact_outputs() -> None:
    pptx = {
        "ok": True,
        "tool": "create_pptx",
        "result": {
            "fileId": "a" * 32,
            "filename": "deck.pptx",
            "slideCount": 2,
            "downloadUrl": "/dl",
            "outline": [{"page": 1, "title": "Intro", "layout": "quote", "extra": "x"}],
        },
    }
    compact = tools.stable_tool_output_for_model(pptx)
    assert "slideCount" in compact["result"]

    doc = {
        "ok": True,
        "tool": "create_document",
        "result": {
            "fileId": "a" * 32,
            "filename": "doc.docx",
            "format": "docx",
            "sectionCount": 1,
            "outline": [{"index": 1, "heading": "H", "hasTable": True}],
        },
    }
    compact = tools.stable_tool_output_for_model(doc)
    assert "sectionCount" in compact["result"]

    mm = {
        "ok": True,
        "tool": "create_mindmap",
        "result": {
            "fileId": "a" * 32,
            "filename": "map.svg",
            "format": "svg",
            "nodeCount": 2,
            "outline": [{"label": "Root", "children": [{"label": "Child", "children": [{}]}]}],
        },
    }
    compact = tools.stable_tool_output_for_model(mm)
    assert "nodeCount" in compact["result"]


def test_create_mindmap_tool() -> None:
    with patch("deepseek_infra.infra.tool_runtime.tools.create_mindmap", return_value={"downloadUrl": "/dl"}) as mock:
        result = tools.execute_tool_call({
            "function": {
                "name": "create_mindmap",
                "arguments": {
                    "title": "M",
                    "subtitle": "",
                    "nodes": [{"label": "Root", "children": []}],
                },
            },
            "id": "c1",
        })
    assert result["ok"]
    mock.assert_called_once()


def test_create_pptx_tool(tmp_settings: Path) -> None:
    with patch("deepseek_infra.infra.tool_runtime.tools.create_presentation", return_value={"downloadUrl": "/dl"}) as mock:
        result = tools.execute_tool_call({
            "function": {
                "name": "create_pptx",
                "arguments": {
                    "title": "T",
                    "subtitle": "",
                    "slides": [{"title": "S", "bullets": ["b"], "layout": "auto"}],
                },
            },
            "id": "c1",
        })
    assert result["ok"]
    mock.assert_called_once()


def test_create_document_tool(tmp_settings: Path) -> None:
    with patch("deepseek_infra.infra.tool_runtime.tools.create_document", return_value={"downloadUrl": "/dl"}) as mock:
        result = tools.execute_tool_call({
            "function": {
                "name": "create_document",
                "arguments": {
                    "format": "docx",
                    "title": "T",
                    "subtitle": "",
                    "sections": [{"heading": "H", "body": [], "bullets": [], "table": {"headers": [], "rows": []}}],
                },
            },
            "id": "c1",
        })
    assert result["ok"]
    mock.assert_called_once()
