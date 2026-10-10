from __future__ import annotations

import json
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import deepseek_infra.infra.data.memory as memory
import deepseek_infra.infra.data.projects as projects
import deepseek_infra.infra.tool_runtime.tools as tools
from deepseek_infra.core.errors import AppError, ErrorCode


class ToolServiceTests(unittest.TestCase):
    def test_python_eval_computes_safe_math_expression(self) -> None:
        result = tools.python_eval("factorial(6)")

        self.assertEqual(result["result"], "720")

    def test_python_eval_rejects_unsafe_expression(self) -> None:
        with self.assertRaises(AppError) as cm:
            tools.python_eval("__import__('os').system('whoami')")

        self.assertEqual(cm.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_fetch_url_blocks_local_targets(self) -> None:
        with self.assertRaises(AppError) as cm:
            tools.fetch_url("http://127.0.0.1:8000/")

        self.assertEqual(cm.exception.code, ErrorCode.FORBIDDEN)

    def test_suggest_memory_tool_invokes_callback_without_saving(self) -> None:
        suggestions: list[dict[str, object]] = []
        tool_call = {
            "id": "call_1",
            "function": {"name": "suggest_memory", "arguments": json.dumps({"content": "Prefers concise answers", "category": "preference"})},
        }

        results = tools.execute_tool_calls(tool_calls=[tool_call], memory_suggestion_callback=suggestions.append, default_memory_scope="seek:study")

        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["scope"], "seek:study")
        self.assertIn("Prefers concise", results[0]["content"])

    def test_web_search_tool_invokes_callback(self) -> None:
        received: dict[str, str] = {}

        def callback(query: str, intent: str) -> dict[str, object]:
            received["query"] = query
            received["intent"] = intent
            return {"query": query, "results": [{"title": "T", "url": "https://example.com", "snippet": "s"}]}

        result = tools.execute_tool_call(
            {
                "id": "call_1",
                "function": {"name": "web_search", "arguments": json.dumps({"query": "OnePlus 16 specs", "intent": "fresh"})},
            },
            web_search_callback=callback,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(received, {"query": "OnePlus 16 specs", "intent": "fresh"})
        self.assertEqual(result["result"]["results"][0]["url"], "https://example.com")

    def test_web_search_tool_requires_callback(self) -> None:
        result = tools.execute_tool_call(
            {
                "id": "call_1",
                "function": {"name": "web_search", "arguments": json.dumps({"query": "docs", "intent": "general"})},
            }
        )

        self.assertFalse(result["ok"])
        self.assertIn("not enabled", result["error"])

    def test_artifact_tool_output_is_compact_for_model(self) -> None:
        output = {
            "ok": True,
            "tool": "create_pptx",
            "result": {
                "fileId": "a" * 32,
                "filename": "deck.pptx",
                "slideCount": 8,
                "downloadUrl": "/api/download?id=" + "a" * 32,
                "title": "Deck",
                "outline": [{"page": 1, "title": "Intro", "layout": "quote", "bullets": ["very long duplicate content"]}],
                "note": "long instruction that should not be sent back to the model",
            },
        }

        compact = tools.stable_tool_output_for_model(output)

        self.assertEqual(compact["result"]["slideCount"], 8)
        self.assertNotIn("note", compact["result"])
        self.assertNotIn("bullets", compact["result"]["outline"][0])

    def test_new_tool_definitions_are_available(self) -> None:
        names = {tool["function"]["name"] for tool in tools.available_tool_definitions()}

        self.assertIn("create_reminder", names)
        self.assertIn("list_reminders", names)
        self.assertIn("recall_memory", names)
        self.assertIn("forget_memory", names)
        self.assertIn("list_project_files", names)
        self.assertIn("read_file_chunk", names)
        self.assertIn("data_transform", names)
        self.assertIn("generate_chart", names)
        self.assertIn("create_mindmap", names)
        self.assertIn("compare_search_results", names)


def test_search_files_reads_temporary_and_project_indexes(tmp_settings: Path) -> None:
    file_cache = tmp_settings / ".file-cache"
    project_files = tmp_settings / ".projects" / "proj_1" / "files"
    file_cache.mkdir(parents=True)
    project_files.mkdir(parents=True)
    (file_cache / "a.json").write_text(
        json.dumps(
            {
                "id": "a" * 32,
                "name": "react-notes.txt",
                "kind": "text",
                "chunks": [{"index": 0, "text": "useMemo 可以缓存计算结果", "lineStart": 1, "lineEnd": 1}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (project_files / "b.json").write_text(
        json.dumps(
            {
                "id": "b" * 32,
                "name": "project-guide.txt",
                "kind": "text",
                "chunks": [{"index": 0, "text": "项目空间里的 useMemo 笔记", "lineStart": 2, "lineEnd": 3}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = tools.search_files("useMemo", limit=10)

    assert result["searchedFiles"] == 2
    matches = result["matches"]
    assert len(matches) == 2
    assert {match["projectId"] for match in matches} == {"", "proj_1"}


def test_reminder_tools_create_and_list(tmp_settings: Path) -> None:
    due_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    created = tools.execute_tool_call(
        {
            "id": "call_reminder",
            "function": {"name": "create_reminder", "arguments": json.dumps({"title": "Standup", "content": "Prepare notes", "dueAt": due_at})},
        }
    )
    listed = tools.execute_tool_call(
        {
            "id": "call_list",
            "function": {"name": "list_reminders", "arguments": json.dumps({"status": "active"})},
        }
    )

    assert created["ok"] is True
    assert created["result"]["title"] == "Standup"
    assert listed["ok"] is True
    assert listed["result"]["count"] == 1
    assert listed["result"]["reminders"][0]["content"] == "Prepare notes"


def test_memory_tools_recall_and_forget_current_scope(tmp_settings: Path) -> None:
    memory.upsert_memory("Project alpha uses SQLite", category="project", scope="project:alpha")
    memory.upsert_memory("Project beta uses Postgres", category="project", scope="project:beta")
    memory.upsert_memory("Prefers concise answers", category="preference", scope="global")

    recalled = tools.recall_memory_tool("SQLite", default_scope="project:alpha")
    forgotten = tools.forget_memory_tool("SQLite", default_scope="project:alpha")

    assert "project:alpha" in recalled["scopes"]
    recalled_contents = {item["content"] for item in recalled["memories"]}
    assert "Project alpha uses SQLite" in recalled_contents
    assert "Project beta uses Postgres" not in recalled_contents
    assert forgotten["deleted"] == 1
    assert {item["content"] for item in memory.load_memories()} == {"Project beta uses Postgres", "Prefers concise answers"}


def test_project_file_tools_list_and_read_chunk(tmp_settings: Path) -> None:
    project = projects.create_project("Docs")
    added = projects.add_project_files(project["id"], [{"filename": "guide.txt", "content_type": "text/plain", "data": b"project library content"}])

    listed = tools.list_project_files_tool(project["id"])
    chunk = tools.read_file_chunk_tool(added[0]["fileId"], chunk_index=1, project_id=project["id"])

    assert listed["count"] == 1
    assert listed["projects"][0]["files"][0]["name"] == "guide.txt"
    assert chunk["file"]["projectId"] == project["id"]
    assert "project library content" in chunk["chunk"]["text"]


def test_data_transform_operations_are_whitelisted() -> None:
    regex_result = tools.data_transform("extract_regex", "a@example.com b@example.com", pattern=r"([\w.-]+)@example\.com")
    json_result = tools.data_transform("json_path", '{"items":[{"name":"alpha"}]}', path="$.items[0].name")
    csv_result = tools.data_transform("csv_summary", "name,value\nA,2\nB,4")
    number_result = tools.data_transform("number_summary", "1, 2, 3, 4")

    assert regex_result["count"] == 2
    assert regex_result["matches"][0]["groups"] == ["a"]
    assert json_result["value"] == "alpha"
    assert csv_result["numericColumns"][0]["mean"] == 3
    assert number_result["median"] == 2.5


def test_generate_chart_returns_markdown_table() -> None:
    result = tools.generate_chart("bar", "Revenue", [{"label": "A", "value": 2}, {"label": "B", "value": "4"}])

    assert result["type"] == "bar"
    assert result["title"] == "Revenue"
    assert "| label | value |" in result["markdownTable"]
    assert "| B | 4.0 |" in result["markdownTable"]


def test_compare_search_results_limits_queries_and_deduplicates() -> None:
    calls: list[tuple[str, str]] = []

    def callback(query: str, intent: str) -> dict[str, object]:
        calls.append((query, intent))
        return {
            "query": query,
            "results": [
                {"title": f"{query} A", "url": "https://example.com/a/", "snippet": "same"},
                {"title": f"{query} B", "url": f"https://example.com/{len(calls)}", "snippet": "unique"},
            ],
        }

    result = tools.compare_search_results([" q1 ", "q2", "q2", "q3", "q4", "q5"], "compare", callback)

    assert [query for query, _ in calls] == ["q1", "q2"]
    assert all(intent == "compare" for _, intent in calls)
    assert result["queries"] == ["q1", "q2"]
    assert len(result["results"]) == 3


def test_execute_tool_calls_preserves_order_and_keeps_side_effects_serial() -> None:
    events: list[str] = []

    def call(tool_id: str, name: str) -> dict[str, object]:
        return {"id": tool_id, "function": {"name": name, "arguments": "{}"}}

    def fake_execute_tool_call(tool_call: dict[str, object], **_: object) -> dict[str, object]:
        tool_id = str(tool_call["id"])
        name = tools.tool_call_name(tool_call)
        events.append(f"start:{tool_id}")
        if tool_id == "slow":
            time.sleep(0.05)
        events.append(f"end:{tool_id}")
        return {"ok": True, "tool": name, "result": {"id": tool_id}}

    tool_calls = [
        call("slow", "python_eval"),
        call("fast", "data_transform"),
        call("serial", "suggest_memory"),
        call("after", "generate_chart"),
    ]

    with patch.object(tools, "execute_tool_call", side_effect=fake_execute_tool_call):
        results = tools.execute_tool_calls(tool_calls)

    assert [result["tool_call_id"] for result in results] == ["slow", "fast", "serial", "after"]
    assert events.index("end:slow") < events.index("start:serial")
    assert events.index("end:fast") < events.index("start:serial")


def test_execute_tool_calls_converts_unfinished_outputs_to_cancelled_when_cancel_fires_mid_batch() -> None:
    """v1.2.8：并行 batch 已经启动后中途 cancel，未拿到 result 的 slot 应统一变成
    cancelled_output 错误体，而不是退化到通用的 "Tool did not run"——cancel 语义在
    所有路径上保持一致。"""
    cancel_event = threading.Event()

    def fake_execute_tool_call(tool_call: dict[str, object], **_: object) -> dict[str, object]:
        name = tools.tool_call_name(tool_call)
        if name == "data_transform":
            # 第一个 worker 触发 cancel 后立即返回；as_completed 把它推出来时，
            # 主线程的 is_cancelled() 检查会让循环 break，剩下 slot 留 None。
            cancel_event.set()
            return {"ok": True, "tool": name, "result": {}}
        # 让其他 worker 慢一点完成，确保 data_transform 一定是第一个被 as_completed 收上来的
        time.sleep(0.05)
        return {"ok": True, "tool": name, "result": {}}

    tool_calls = [
        {"id": "fast", "function": {"name": "data_transform", "arguments": "{}"}},
        {"id": "slow1", "function": {"name": "python_eval", "arguments": "{}"}},
        {"id": "slow2", "function": {"name": "generate_chart", "arguments": "{}"}},
    ]

    with patch.object(tools, "execute_tool_call", side_effect=fake_execute_tool_call):
        results = tools.execute_tool_calls(tool_calls, cancel_event=cancel_event)

    assert [result["tool_call_id"] for result in results] == ["fast", "slow1", "slow2"]
    cancelled = []
    for result in results:
        payload = json.loads(result["content"])
        if not payload["ok"]:
            cancelled.append(payload)
            assert "cancel" in payload["error"].lower(), (
                f"cancel 期间所有未完成 slot 都该是 cancelled，但拿到 {payload['error']!r}"
            )
    # 至少 slow1 / slow2 这两个 slot 要被 cancel；fast 在 cancel 之前就 return 了，
    # 它的真实 result 是否被 outputs 接收取决于 as_completed 调度，但即便被丢弃，
    # 走 None → is_cancelled() 兜底也会被替换成 cancelled，不会泄成 "Tool did not run"。
    assert len(cancelled) >= 2


def test_fetch_url_extracts_and_caches_public_page(tmp_settings: Path) -> None:
    page = b"<html><head><title>Doc</title></head><body><main><h1>Hello</h1><p>Readable page text.</p></main></body></html>"
    connections: list[FakeHTTPConnection] = []

    def fake_connection(target: tools.PublicUrlTarget, timeout: float) -> FakeHTTPConnection:
        connection = FakeHTTPConnection(FakeHTTPResponse(page))
        connection.target = target
        connections.append(connection)
        return connection

    with (
        patch.object(tools, "resolve_public_host", return_value=["93.184.216.34"]) as resolve_host,
        patch.object(tools, "public_http_connection", side_effect=fake_connection) as open_public,
    ):
        first = tools.fetch_url("https://example.com/path?x=1#fragment")
        second = tools.fetch_url("https://example.com/path?x=1#fragment")

    assert resolve_host.call_count == 2
    assert open_public.call_count == 1
    assert connections[0].target is not None
    assert connections[0].target.address == "93.184.216.34"
    assert connections[0].target.host_header == "example.com"
    assert first["url"] == "https://example.com/path?x=1"
    assert "Readable page text" in first["text"]
    assert second["cached"] is True


def test_fetch_url_revalidates_redirect_targets(tmp_settings: Path) -> None:
    real_resolve_public_host = tools.resolve_public_host

    def resolve_host(host: str, port: int | None) -> list[str]:
        if host == "example.com":
            return ["93.184.216.34"]
        return real_resolve_public_host(host, port)

    def fake_connection(target: tools.PublicUrlTarget, timeout: float) -> FakeHTTPConnection:
        return FakeHTTPConnection(FakeHTTPResponse(b"", status=302, headers={"Location": "http://127.0.0.1/admin"}))

    with (
        patch.object(tools, "resolve_public_host", side_effect=resolve_host),
        patch.object(tools, "public_http_connection", side_effect=fake_connection),
    ):
        with pytest.raises(AppError) as cm:
            tools.fetch_url("https://example.com/path")

    assert cm.value.code == ErrorCode.FORBIDDEN


class FakeHTTPResponse:
    def __init__(self, data: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.data = data
        self.status = status
        self.headers = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}

    def getheader(self, name: str, default: str = "") -> str:
        return self.headers.get(name, default)

    def read(self, size: int = -1) -> bytes:
        if size >= 0:
            return self.data[:size]
        return self.data


class FakeHTTPConnection:
    def __init__(self, response: FakeHTTPResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.target: tools.PublicUrlTarget | None = None
        self.closed = False

    def request(self, method: str, url: str, headers: dict[str, str]) -> None:
        self.requests.append((method, url, headers))

    def getresponse(self) -> FakeHTTPResponse:
        return self.response

    def close(self) -> None:
        self.closed = True
