# Continue.dev MCP Evidence

- Client: Continue.dev
- Version: 2.5.8
- Commit: 2e2782e
- Status: PASS
- Generated: 2026-06-28T10:00:00Z
- OS: Windows
- Python: 3.13.5
- CI: false

## Checks

| Check | Result |
| --- | --- |
| configLoaded | PASS |
| mcpInitialize | PASS |
| toolsList | PASS |
| lowRiskToolCall | PASS |
| policyDenial | PASS |
| promptInjectionClean | PASS |

## Transport

- Type: streamable-http
- MCP URL: http://127.0.0.1:8000/mcp
- Auth: disabled

## Steps

1. **server.healthz**: pass — status=ok
2. **config.loaded**: pass — Continue.dev MCP config loaded with streamable-http transport
3. **mcp.initialize**: pass — protocol=2025-06-18 server=deepseek-infra
4. **mcp.tools_list**: pass — 17 tools exposed
5. **mcp.tools_call**: pass — data_transform count=4
6. **mcp.policy_denial**: pass — fetch_url localhost probe blocked by Tool Policy

## Summary

Continue.dev successfully connects to DeepSeek Infra's MCP endpoint, lists all 17 local tools, executes `data_transform` (low-risk tool call), and correctly denies `fetch_url` SSRF probe through the Tool Policy gate. System prompt remains uncontaminated by tool results.

See [docs/integrations/continue-dev.md](../integrations/continue-dev.md) for full configuration guide and verification runbook.
