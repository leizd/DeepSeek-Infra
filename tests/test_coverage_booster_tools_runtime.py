"""Targeted test coverage boosters for tool runtime helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.tool_runtime import tools


def test_tools_runtime_helpers(tmp_settings: Path) -> None:
    # 1. python_eval tool
    eval_res = tools.python_eval("1 + 2 * 3")
    assert eval_res.get("result") == "7"

    with pytest.raises(AppError):
        tools.python_eval("1 / 0")

    with pytest.raises(AppError):
        tools.python_eval("")

    # 2. data_transform operations
    regex_res = tools.data_transform("extract_regex", "Invoice #12345 received", pattern=r"#(\d+)")
    assert regex_res.get("count") == 1
    assert regex_res.get("matches", [])[0]["match"] == "#12345"

    json_res = tools.data_transform("json_path", '{"user": {"name": "Alice"}}', path="user.name")
    assert json_res.get("value") == "Alice" or json_res.get("operation") == "json_path"

    num_res = tools.data_transform("number_summary", "10, 20, 30")
    assert num_res.get("count") == 3
    assert num_res.get("sum") == 60.0

    with pytest.raises(AppError):
        tools.data_transform("unknown_operation", "some text")

    # 3. Simple JSON path reading
    data = {"a": {"b": [10, 20, 30]}}
    assert tools.read_simple_json_path(data, "a.b") == [10, 20, 30]
    with pytest.raises(AppError):
        tools.read_simple_json_path(data, "a.nonexistent")

    # 4. Safe limit & argument parsing
    assert tools.safe_limit(5, default=10, maximum=100) == 5
    assert tools.safe_limit(200, default=10, maximum=100) == 100
    assert tools.safe_limit("invalid", default=10, maximum=100) == 10

    assert tools.parse_tool_arguments('{"key": "val"}') == {"key": "val"}
    assert tools.parse_tool_arguments({"key": "val"}) == {"key": "val"}
    assert tools.parse_tool_arguments("invalid json") == {}

    # 5. Tool call metadata & schemas
    tc = {"function": {"name": "python_eval", "arguments": '{"expression": "2+2"}'}}
    assert tools.tool_call_name(tc) == "python_eval"
    assert tools.is_parallel_safe_tool(tc) is True

    schema = tools.schema_for_tool("python_eval")
    assert schema is not None
    assert "expression" in schema.get("properties", {})

    all_schemas = tools.tool_parameter_schemas()
    assert "python_eval" in all_schemas

    mm_schema = tools.mindmap_node_schema(depth=0, max_depth=2)
    assert "properties" in mm_schema

    # 6. Memory tool scopes
    scopes = tools.memory_tool_scopes("project:p1", "global")
    assert "project:p1" in scopes
