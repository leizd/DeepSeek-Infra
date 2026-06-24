"""Capability scoping and user-consent gates for the MCP Tool Hub.

MCP clients never reach tool executors directly. Every ``tools/call`` runs
through the same Tool Policy Engine as LLM tool calls, scoped to the capability
profile configured for the hub (``MCP_CAPABILITY``, default ``full``). The MCP
spec stresses explicit user consent before tool execution; tools whose metadata
says ``requires_confirm`` therefore honor the global confirmation gate, and a
client can pre-approve them per call via ``params._meta.approvedTools``.
"""

from __future__ import annotations

from typing import Any

from deepseek_infra.core.config import MCP_CAPABILITY
from deepseek_infra.infra.tool_runtime.tool_policy import CAPABILITY_PROFILES, ToolPolicy, all_tool_names


def hub_capability() -> str:
    """The capability profile granted to MCP clients (unknown profile -> full)."""
    capability = str(MCP_CAPABILITY or "full").strip() or "full"
    return capability if capability in CAPABILITY_PROFILES else "full"


def allowed_tool_names() -> list[str]:
    """Tool names exposed by the hub under the configured capability."""
    capability = hub_capability()
    if capability == "full":
        names = list(all_tool_names())
        # Include external MCP bridged tools when capability is full.
        try:
            from deepseek_infra.infra.mcp.bridge import external_mcp_registry
            names.extend(p.bridged_name for p in external_mcp_registry.list_profiles())
        except Exception:
            pass
        return names
    return list(CAPABILITY_PROFILES.get(capability, ()))


def connection_policy(approvals: set[str] | None = None) -> ToolPolicy:
    """One policy per ``tools/call``: capability slice + per-call pre-approvals."""
    return ToolPolicy(
        capability=hub_capability(),
        approvals=approvals or set(),
        scope="mcp",
    )


def approvals_from_meta(meta: Any) -> set[str]:
    """Extract ``approvedTools`` from a request's ``params._meta`` block."""
    if not isinstance(meta, dict):
        return set()
    approved = meta.get("approvedTools")
    if not isinstance(approved, list):
        return set()
    return {str(item).strip() for item in approved if str(item or "").strip()}
