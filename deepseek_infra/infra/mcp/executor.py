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
from deepseek_infra.infra.observability.observability import start_span
from deepseek_infra.infra.tool_runtime.tool_policy import (
    ToolPolicy,
    _normalized_args_hash,
    write_external_audit_entry,
)


def call_external_mcp_tool(
    bridged_name: str,
    arguments: dict[str, Any],
    policy: ToolPolicy | None,
    *,
    trace_id: str = "",
    parent_span_id: str = "",
) -> dict[str, Any]:
    """Execute one bridged external MCP tool call through the policy gate.

    1. Parse the bridged name → (server, original_tool)
    2. Require and run ``ToolPolicy.evaluate()``
    3. Resolve via registry → (MCPClient, original_tool_name)
    4. Call the external server (timed)
    5. Wrap in ``{ok, tool, result}``
    6. Sanitize via policy
    7. Write extended audit entry
    8. Return shaped output for the LLM
    """
    parsed = parse_bridged_name(bridged_name)
    if parsed is None:
        return {
            "ok": False,
            "tool": bridged_name or "unknown",
            "error": "Not a bridged external MCP tool",
            "code": "invalid_payload",
        }

    server, _original_tool = parsed
    if policy is None:
        return {
            "ok": False,
            "tool": bridged_name,
            "error": "External MCP tools require ToolPolicy",
            "code": "forbidden",
        }

    profile = external_mcp_registry.get_profile(bridged_name)
    schema = profile.input_schema if profile is not None else None
    decision = policy.evaluate(bridged_name, arguments, schema=schema)
    if not decision.allowed:
        return ToolPolicy.denial_output(decision)

    resolved = external_mcp_registry.resolve(bridged_name)
    if resolved is None:
        return {
            "ok": False,
            "tool": bridged_name,
            "error": f"External MCP server '{server}' is unavailable",
            "code": "upstream_failure",
        }

    client, tool_name = resolved
    risk = profile.risk if profile is not None else "unknown"

    start = time.perf_counter()
    error_type: str | None = None
    span = start_span(
        trace_id,
        name=f"mcp.external.{server}.{tool_name}",
        kind="mcp_external",
        input_data={
            "server": server,
            "tool": tool_name,
            "bridgedTool": bridged_name,
            "argsHash": _normalized_args_hash(arguments),
        },
        parent_span_id=parent_span_id,
    )

    try:
        result = client.call_tool(tool_name, arguments)
    except AppError as exc:
        external_mcp_registry.record_call_failure(server, client, exc)
        error_type = _classify_error(exc)
        latency_ms = int((time.perf_counter() - start) * 1000)
        output = {"ok": False, "tool": bridged_name, "error": str(exc), "code": "upstream_failure"}
        span.finish(
            status="error",
            output_data={"ok": False, "code": "upstream_failure"},
            diagnostics=_span_diagnostics(client, latency_ms=latency_ms, error_type=error_type),
            error=str(exc),
        )
        _write_audit_for_call(
            server=server,
            tool=tool_name,
            bridged_tool=bridged_name,
            arguments=arguments,
            policy_verdict=decision.policy_verdict,  # policy already said yes — this is a transport failure
            risk=risk,
            latency_ms=latency_ms,
            error_type=error_type,
        )
        return output
    except Exception as exc:
        external_mcp_registry.record_call_failure(server, client, exc)
        error_type = "unknown"
        latency_ms = int((time.perf_counter() - start) * 1000)
        output = {"ok": False, "tool": bridged_name, "error": str(exc), "code": "internal"}
        span.finish(
            status="error",
            output_data={"ok": False, "code": "internal"},
            diagnostics=_span_diagnostics(client, latency_ms=latency_ms, error_type=error_type),
            error=str(exc),
        )
        _write_audit_for_call(
            server=server,
            tool=tool_name,
            bridged_tool=bridged_name,
            arguments=arguments,
            policy_verdict=decision.policy_verdict,
            risk=risk,
            latency_ms=latency_ms,
            error_type=error_type,
        )
        return output

    latency_ms = int((time.perf_counter() - start) * 1000)
    external_mcp_registry.record_call_success(server, client)

    is_error = bool(result.get("isError")) if isinstance(result, dict) else False
    output = {"ok": not is_error, "tool": bridged_name, "result": result}
    if is_error:
        error_type = "tool_error"
        output["code"] = "upstream_tool_error"
        output["error"] = "External MCP tool returned isError=true"

    output = policy.sanitize_result(bridged_name, output)

    _write_audit_for_call(
        server=server,
        tool=tool_name,
        bridged_tool=bridged_name,
        arguments=arguments,
        policy_verdict=decision.policy_verdict,
        risk=risk,
        latency_ms=latency_ms,
        error_type=error_type,
    )
    span.finish(
        output_data={"ok": True, "tool": bridged_name},
        diagnostics=_span_diagnostics(client, latency_ms=latency_ms, error_type=None),
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


def _span_diagnostics(client: Any, *, latency_ms: int, error_type: str | None) -> dict[str, Any]:
    stats = getattr(client, "last_stats", None)
    return {
        "latencyMs": latency_ms,
        "transportLatencyMs": int(getattr(stats, "latency_ms", 0) or 0),
        "attempts": int(getattr(stats, "attempts", 0) or 0),
        "retryCount": int(getattr(stats, "retry_count", 0) or 0),
        "timeout": bool(getattr(stats, "timeout", False)),
        "errorType": error_type or "",
    }
