"""Tool-dispatch parity probe, Python side.

Measures the *seam* of ``deepseek_infra/infra/tool_runtime/tools.py``: the
argument/limit/name normalization, the chart branch, and the envelope/ordering of
``execute_tool_call``.

What is extracted verbatim
--------------------------
``tool_call_name``, ``parse_tool_arguments``, ``safe_limit``,
``is_parallel_safe_tool``, ``generate_chart``, ``chart_markdown_table``,
``execute_tool_call``, ``SERIAL_TOOL_NAMES`` — plus the real ``ErrorCode`` /
``AppError`` by executing the self-contained ``core/errors.py``, and the real
``ToolPolicy`` / ``tool_metadata`` by reusing the tool-policy probe's namespace.

What is stubbed, and why
------------------------
- **The 18 branch functions.** Only ``generate_chart`` is pure enough to port
  without its packages, so the others are replaced by recorders. That is not a
  loss of fidelity for what is being measured here: it is what makes the *gate
  short-circuit* observable (a denied call must invoke **no** branch).
- **``_evaluate_rust_policy`` returns ``None``.** This is exactly what the real
  function does when ``DEEPSEEK_RUST_POLICY`` is false, which is the production
  default (``infra/rust_core/config.py``). The Rust-policy path is measured
  separately by the tool-policy probe.
- **``schema_for_tool`` returns ``None``.** The schema catalog derives from
  ``available_tool_definitions()``; schema-driven verdicts are covered by the
  tool-policy probe's ``validate::*`` keys instead of being duplicated here.

Nothing is written anywhere.

Usage::

    python tasks/native-runtime/tool_dispatch_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example tool_dispatch_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tool_policy_parity_probe as policy_probe  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "deepseek_infra" / "infra" / "tool_runtime" / "tools.py"
ERRORS = REPO / "deepseek_infra" / "core" / "errors.py"

EXTRACT_NAMES = (
    "tool_call_name",
    "parse_tool_arguments",
    "safe_limit",
    "is_parallel_safe_tool",
    "generate_chart",
    "chart_markdown_table",
    "execute_tool_call",
)

# Recorded branch invocations, so a gate short-circuit is observable.
CALLS: list[str] = []


def _extract_function(source: str, name: str) -> str | None:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node)
    return None


def _extract_assignment(source: str, name: str) -> object:
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise SystemExit(f"could not extract {name}")


def build_namespace() -> dict:
    tools_source = TOOLS.read_text(encoding="utf-8")
    namespace: dict = {}

    # The real error types — `errors.py` is self-contained.
    exec(compile(ERRORS.read_text(encoding="utf-8"), str(ERRORS), "exec"), namespace)  # noqa: S102

    from typing import Any, Callable  # noqa: E402
    import re  # noqa: E402
    import threading  # noqa: E402

    # The extracted functions reference these as module globals.
    namespace.update(
        {"Any": Any, "Callable": Callable, "json": json, "re": re, "threading": threading}
    )

    # The real policy objects, reused from the tool-policy probe. Audit is turned
    # off here: this probe measures dispatch, not the audit log, and the log path
    # is unset in that namespace.
    policy_ns = policy_probe.build_namespace()
    policy_ns["TOOL_POLICY_AUDIT_ENABLED"] = False
    for key in ("ToolPolicy", "tool_metadata", "PolicyDecision", "DENY"):
        namespace[key] = policy_ns[key]

    namespace["SERIAL_TOOL_NAMES"] = _extract_assignment(tools_source, "SERIAL_TOOL_NAMES")

    for name in EXTRACT_NAMES:
        found = _extract_function(tools_source, name)
        if found is None:
            raise SystemExit(f"could not extract {name}")
        exec(compile(found, str(TOOLS), "exec"), namespace)  # noqa: S102

    def recorder(branch: str):
        def stub(*_args: object, **_kwargs: object) -> dict:
            CALLS.append(branch)
            return {"stub": branch}

        return stub

    for branch in (
        "python_eval",
        "search_files",
        "fetch_url",
        "compare_search_results",
        "build_memory_suggestion",
        "create_reminder_tool",
        "list_reminders_tool",
        "recall_memory_tool",
        "forget_memory_tool",
        "list_project_files_tool",
        "read_file_chunk_tool",
        "data_transform",
        "create_mindmap",
        "create_presentation",
        "create_document",
        "browser_action_tool",
        "normalize_memory_scope",
        "call_external_mcp_tool",
    ):
        namespace[branch] = recorder(branch)

    # Mirrors the production default (`DEEPSEEK_RUST_POLICY` false) and the
    # schema source duplicating the tool-policy probe.
    namespace["rust_policy_enabled"] = lambda: False
    namespace["_evaluate_rust_policy"] = lambda *a, **k: None
    namespace["schema_for_tool"] = lambda name: None

    return namespace


# --- corpus ----------------------------------------------------------------------

PARSE_CASES: list[tuple[str, object]] = [
    ("object", {"a": 1}),
    ("object-string", '{"a": 1}'),
    ("nested-string", '{"a": {"b": [1, 2]}}'),
    ("not-json", "not json"),
    ("json-array", "[1, 2]"),
    ("json-scalar", "42"),
    ("blank", "   "),
    ("empty", ""),
    ("none", None),
    ("integer", 7),
]

LIMIT_CASES: list[tuple[str, object]] = [
    ("in-range", 3),
    ("zero", 0),
    ("negative", -9),
    ("above-max", 99),
    ("numeric-string", "7"),
    ("float-string", "7.9"),
    ("float", 3.9),
    ("unparseable", "abc"),
    ("none", None),
    ("list", [1]),
    ("bool-true", True),
]

NAME_CASES: list[tuple[str, dict]] = [
    ("function-name", {"function": {"name": " search_files "}}),
    ("blank-function-top-level", {"function": {"name": ""}, "name": "x"}),
    ("top-level-only", {"name": "y"}),
    ("missing", {}),
    ("function-not-a-dict", {"function": "nope", "name": "z"}),
]

PARALLEL_CASES: list[tuple[str, dict]] = [
    ("serial-tool", {"function": {"name": "web_search"}}),
    ("parallel-tool", {"function": {"name": "generate_chart"}}),
    ("nameless", {}),
]

CHART_CASES: list[tuple[str, dict]] = [
    ("line-with-title", {"type": "line", "title": "Revenue", "data": [{"label": "Q1", "value": 1}, {"label": "Q2", "value": 2.5}]}),
    ("defaults", {"data": [{"label": "a", "value": 1}]}),
    ("unknown-type", {"type": "radar", "data": [{"label": "a", "value": 1}]}),
    ("string-numbers", {"data": [{"label": "a", "value": "3"}]}),
    ("drops-and-caps", {"data": ["nope", {"value": 1}, {"label": "nv"}, {"label": "n", "value": None}, {"label": "bad", "value": "abc"}, {"label": "ok", "value": "3"}] + [{"label": f"p{i}", "value": i} for i in range(20)]}),
    ("pipe-in-label", {"data": [{"label": "a|b", "value": 1}]}),
    ("empty-list", {"data": []}),
    ("not-a-list", {"data": "nope"}),
    ("no-valid-points", {"data": [{"value": 1}]}),
    ("float-rendering", {"data": [{"label": "x", "value": 100}, {"label": "y", "value": -0.0}]}),
]

# (label, tool_call, policy mode) — `policy` is "none" or "permissive".
DISPATCH_CASES: list[tuple[str, dict, str]] = [
    ("unknown-no-policy", {"function": {"name": "not_a_tool", "arguments": "{}"}}, "none"),
    ("unknown-with-policy", {"function": {"name": "not_a_tool", "arguments": "{}"}}, "permissive"),
    ("chart-no-policy", {"function": {"name": "generate_chart", "arguments": '{"data": [{"label": "a", "value": 1}]}'}}, "none"),
    ("chart-with-policy", {"function": {"name": "generate_chart", "arguments": '{"data": [{"label": "a", "value": 1}]}'}}, "permissive"),
    ("chart-empty-args", {"function": {"name": "generate_chart", "arguments": "{}"}}, "none"),
    ("chart-arguments-as-object", {"function": {"name": "generate_chart", "arguments": {"data": [{"label": "a", "value": 2}]}}}, "none"),
    # Denied by the gate, so the branch must never be invoked.
    ("ssrf-denied-with-policy", {"function": {"name": "fetch_url", "arguments": '{"url": "http://169.254.169.254/"}'}}, "permissive"),
    ("path-denied-with-policy", {"function": {"name": "search_files", "arguments": '{"path": "../../etc/passwd"}'}}, "permissive"),
]


def _policy(namespace: dict, mode: str):
    if mode == "none":
        return None
    return namespace["ToolPolicy"].permissive()


def main() -> int:
    for path in (TOOLS, ERRORS):
        if not path.exists():
            print(f"missing {path}", file=sys.stderr)
            return 2

    namespace = build_namespace()
    out: dict = {}

    parse_tool_arguments = namespace["parse_tool_arguments"]
    safe_limit = namespace["safe_limit"]
    tool_call_name = namespace["tool_call_name"]
    is_parallel_safe_tool = namespace["is_parallel_safe_tool"]
    generate_chart = namespace["generate_chart"]
    chart_markdown_table = namespace["chart_markdown_table"]
    execute_tool_call = namespace["execute_tool_call"]
    AppError = namespace["AppError"]

    for label, value in PARSE_CASES:
        out[f"parse::{label}"] = parse_tool_arguments(value)

    for label, value in LIMIT_CASES:
        out[f"limit::{label}"] = safe_limit(value, default=5, maximum=10)

    for label, call in NAME_CASES:
        out[f"name::{label}"] = tool_call_name(call)

    for label, call in PARALLEL_CASES:
        out[f"parallel::{label}"] = is_parallel_safe_tool(call)

    for label, arguments in CHART_CASES:
        try:
            out[f"chart::{label}"] = {"ok": True, "result": generate_chart(
                str(arguments.get("type") or "bar"),
                str(arguments.get("title") or ""),
                arguments.get("data"),
            )}
        except AppError as exc:
            out[f"chart::{label}"] = {"ok": False, "error": str(exc), "code": exc.code.value}

    out["markdown::table"] = chart_markdown_table(
        [{"label": "a|b", "value": 1.0}, {"label": "c", "value": 2.5}]
    )
    out["serial::names"] = sorted(namespace["SERIAL_TOOL_NAMES"])

    for label, call, mode in DISPATCH_CASES:
        CALLS.clear()
        output = execute_tool_call(call, policy=_policy(namespace, mode))
        if output.get("ok") is True:
            outcome = "executed"
        elif output.get("code") == "forbidden":
            outcome = "denied"
        else:
            outcome = "unsupported"
        out[f"dispatch::{label}"] = {
            "outcome": outcome,
            "output": output,
            "branches": sorted(set(CALLS)),
        }

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
