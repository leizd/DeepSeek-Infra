"""Tool-catalog parity probe, Python side.

Covers `available_tool_definitions` / `tool_parameter_schemas` / `schema_for_tool` /
`agent_tool_definitions` from `infra/tool_runtime/tools.py`.

Unlike the other probes in this directory, this one **imports the module directly**
rather than AST-extracting the functions. Extraction would mean chasing a transitive
dependency chain (`available_tool_definitions` -> `browser_tool_definitions` /
`additional_tool_definitions` -> `mindmap_node_schema` / ...) and re-implementing the
module's own scaffolding; the module imports cleanly, so running the real thing is both
simpler and more faithful.

The strongest case is `asset::json`: the embedded Rust asset must be byte-identical to
`json.dumps(available_tool_definitions(), ensure_ascii=False, indent=2)`. That is what
proves the committed 40 KB file is still the oracle's own rendering rather than a
hand-edited copy.

Usage::

    python tasks/native-runtime/tool_catalog_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example tool_catalog_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

SCHEMA_CASES = [
    "web_search",
    "create_reminder",
    "generate_chart",
    "read_file_chunk",
    "browser_open_url",
    "  web_search  ",
    "no_such_tool",
    "",
]

INDEX_SAMPLE = ["web_search", "create_reminder", "generate_chart", "read_file_chunk"]


def main() -> int:
    from deepseek_infra.infra.tool_runtime.tools import (
        agent_tool_definitions,
        available_tool_definitions,
        schema_for_tool,
        tool_parameter_schemas,
    )

    tools = available_tool_definitions()
    out: dict = {}

    # The asset's exact bytes, as the oracle renders them.
    out["asset::json"] = json.dumps(tools, ensure_ascii=False, indent=2)

    # Declaration order is load-bearing: callers index into it.
    out["definitions::names"] = [
        str(tool.get("function", {}).get("name") or "") for tool in tools
    ]
    out["definitions::count"] = len(tools)

    index = tool_parameter_schemas()
    out["schemas::count"] = len(index)
    out["schemas::names"] = sorted(index)
    for name in INDEX_SAMPLE:
        out[f"schema::{name}"] = index.get(name)

    for case in SCHEMA_CASES:
        out[f"schema-for::{json.dumps(case, ensure_ascii=False)}"] = schema_for_tool(case)

    # With no external MCP profiles registered this is the local catalog unchanged, which
    # is the arm the Rust side passes `None` for.
    out["agent::names"] = [
        str(tool.get("function", {}).get("name") or "") for tool in agent_tool_definitions()
    ]

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
