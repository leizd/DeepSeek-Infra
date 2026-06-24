"""External MCP tool executor: policy-gated dispatch and audit.

External tool calls never go directly to ``MCPClient.call_tool()``. They pass
through the same :class:`ToolPolicy` gate as local tools, then the executor
resolves the bridged name, times the call, classifies errors, sanitizes results,
and writes the extended audit record.
"""

from __future__ import annotations

import time
from typing import Any

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.mcp.bridge import external_mcp_registry, parse_bridged_name
from deepseek_infra.infra.tool_runtime.tool_policy import (
    ToolPolicy,
    _normalized_args_hash,
    write_external_audit_entry,
)


def call_external_mcp_tool(
    bridged_name: str,
    arguments: dict[str, Any],
    policy: ToolPolicy | None,
) -> dict[str, Any]:
    """Execute one bridged external MCP tool call through the policy gate.

    1. Parse the bridged name → (server, original_tool)
    2. Resolve via registry → (MCPClient, original_tool_name)
    3. Call the external server (timed)
    4. Wrap in ``{ok, tool, result}``
    5. Sanitize via policy
    6. Write extended audit entry
    7. Return shaped output for the LLM
    """
    parsed = parse_bridged_name(bridged_name)
    if parsed is None:
        return {
            "ok": False,
            "tool": bridged_name or "unknown",
            "error": "Not a bridged external MCP tool",
            "code": "invalid_payload",
        }

    server, original_tool = parsed
    resolved = external_mcp_registry.resolve(bridged_name)
    if resolved is None:
        return {
            "ok": False,
            "tool": bridged_name,
            "error": f"External MCP server '{server}' is unavailable",
            "code": "upstream_failure",
        }

    client, tool_name = resolved
    profile = external_mcp_registry.get_profile(bridged_name)
    risk = profile.risk if profile is not None else "unknown"

    start = time.perf_counter()
    error_type: str | None = None

    try:
        result = client.call_tool(tool_name, arguments)
    except AppError as exc:
        error_type = _classify_error(exc)
        output = {"ok": False, "tool": bridged_name, "error": str(exc), "code": "upstream_failure"}
        _write_audit_for_call(
            server=server,
            tool=tool_name,
            bridged_tool=bridged_name,
            arguments=arguments,
            policy_verdict="allowed",  # policy already said yes — this is a transport failure
            risk=risk,
            latency_ms=int((time.perf_counter() - start) * 1000),
            error_type=error_type,
        )
        return output
    except Exception as exc:
        error_type = "unknown"
        output = {"ok": False, "tool": bridged_name, "error": str(exc), "code": "internal"}
        _write_audit_for_call(
            server=server,
            tool=tool_name,
            bridged_tool=bridged_name,
            arguments=arguments,
            policy_verdict="allowed",
            risk=risk,
            latency_ms=int((time.perf_counter() - start) * 1000),
            error_type=error_type,
        )
        return output

    latency_ms = int((time.perf_counter() - start) * 1000)

    output = {"ok": True, "tool": bridged_name, "result": result}

    if policy is not None:
        output = policy.sanitize_result(bridged_name, output)

    _write_audit_for_call(
        server=server,
        tool=tool_name,
        bridged_tool=bridged_name,
        arguments=arguments,
        policy_verdict="allowed",
        risk=risk,
        latency_ms=latency_ms,
        error_type=None,
    )

    return output


def _write_audit_for_call(
    *,
    server: str,
    tool: str,
    bridged_tool: str,
    arguments: dict[str, Any],
    policy_verdict: str,
    risk: str,
    latency_ms: int,
    error_type: str | None,
) -> None:
    write_external_audit_entry(
        scope="mcp_external",
        server=server,
        tool=tool,
        bridged_tool=bridged_tool,
        args_hash=_normalized_args_hash(arguments),
        policy_verdict=policy_verdict,
        risk=risk,
        latency_ms=latency_ms,
        error_type=error_type,
        protocol="mcp",
        direction="outbound",
    )


def _classify_error(exc: AppError) -> str:
    """Map an :class:`AppError` to an audit-friendly error-type label."""
    message = str(exc).lower()
    if "timeout" in message:
        return "timeout"
    if "unreachable" in message or "connection" in message:
        return "unreachable"
    if "invalid json" in message or "schema" in message:
        return "schema_error"
    if "http " in message or "http_" in message:
        return "http_error"
    if "protocol" in message:
        return "protocol_error"
    return "upstream_failure"
