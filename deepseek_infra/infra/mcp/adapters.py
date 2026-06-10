"""Bridge MCP ``tools/call`` requests into the policy-gated local tool runtime.

The adapter builds the same tool-call envelope the LLM loop uses and funnels it
through ``execute_tool_call`` with a capability-scoped :class:`ToolPolicy`, so
MCP clients get exactly the same schema validation, SSRF / path / sensitive
guards, confirmation gates and result sanitization as the model does. Results
come back as MCP ``content`` parts plus ``structuredContent``.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from deepseek_infra.core.config import TAVILY_API_KEY
from deepseek_infra.infra.mcp.permissions import approvals_from_meta, connection_policy
from deepseek_infra.infra.tool_runtime.search import search_single_round
from deepseek_infra.infra.tool_runtime.tools import execute_tool_call, stable_tool_output_for_model

MAX_MCP_RESULT_CHARS = 24_000


def _hub_web_search_callback() -> Callable[[str, str], dict[str, Any]] | None:
    """A web_search callback for MCP calls when a server-side Tavily key exists."""
    if not TAVILY_API_KEY:
        return None

    def perform(query: str, intent: str) -> dict[str, Any]:
        return search_single_round(
            query,
            intent=intent,
            round_index=1,
            tavily_api_key=TAVILY_API_KEY,
            use_cache=True,
        )

    return perform


def call_hub_tool(name: str, arguments: dict[str, Any] | None, *, meta: Any = None) -> dict[str, Any]:
    """Execute one MCP tool call and shape the result per the MCP spec.

    Returns ``{"content": [...], "isError": bool, "structuredContent": {...}}``.
    Policy denials and tool failures are reported as ``isError`` tool results
    (per spec they are *tool* errors, not protocol errors).
    """
    tool_call = {
        "id": "mcp_call",
        "type": "function",
        "function": {
            "name": str(name or ""),
            "arguments": json.dumps(arguments if isinstance(arguments, dict) else {}, ensure_ascii=False),
        },
    }
    output = execute_tool_call(
        tool_call,
        web_search_callback=_hub_web_search_callback(),
        policy=connection_policy(approvals_from_meta(meta)),
    )
    stable = stable_tool_output_for_model(output)
    text = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))[:MAX_MCP_RESULT_CHARS]
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": stable,
        "isError": output.get("ok") is not True,
    }
