# Native MCP JSON-RPC hub (`POST /mcp`)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **wired for local tools.** Exact-head CI has not run.

`deepseek-gateway::mcp_hub` implements the Streamable-HTTP JSON-RPC methods
the Python Tool Hub supports:

- `initialize` / `notifications/initialized` / `ping`
- `tools/list` / `tools/call` (policy-gated `dispatch`)
- `resources/list` / `resources/read`
- `prompts/list` / `prompts/get`

`tools/call` builds the same envelope the chat loop uses and runs
`ToolRoundExecutor::execute_call_sync`. Results are MCP `content` +
`structuredContent` with `isError` when `ok` is not true.

## Honest gaps

- External `mcp__*` bridging is **not** a silent success: the hub returns a
  tool error (`external MCP bridging is not wired on the native hub`).
- `runtime://capabilities` is a reduced document (capability name only, not
  the full Python `tool_policy_status` blob).
- Production HTTP is still Python-authoritative; this is the native gateway's
  opt-in `/mcp`.

## Verification

- `mcp_hub::tests::tools_call_runs_python_eval` — `2+2` → `4`
- `mcp_initialize_and_tools_call_are_native` on `POST /mcp`
- `mcp_tools("researcher")` is the three research tools
