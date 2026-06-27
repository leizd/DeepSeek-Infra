# Headless MCP Client Compatibility

适用版本：DeepSeek Infra v2.3.3。

Headless MCP verification is the no-GUI compatibility path for CI, servers, and machines that do not have Claude Desktop or Cursor installed. It proves that the MCP client path those apps commonly rely on can cross a stdio bridge into DeepSeek Infra's Streamable HTTP endpoint.

It does **not** claim Claude Desktop or Cursor GUI verification. Those rows stay 🟡 until a human runs the GUI runbooks in [claude-desktop.md](claude-desktop.md) and [cursor.md](cursor.md).

## What It Verifies

- Local DeepSeek Infra can start without an API key.
- A stdio bridge can forward JSON-RPC messages to `POST /mcp`.
- MCP `initialize` succeeds.
- MCP `tools/list` exposes expected local tools.
- MCP `tools/call` can run `data_transform`.
- Tool Policy blocks an SSRF probe through `fetch_url`.
- The evidence JSON records the result without storing any Bearer token.

## Run

```bash
python scripts/smoke_mcp_headless_bridge.py --json
```

By default the script starts an embedded server on an ephemeral localhost port with `AUTH_DISABLED=1`, then launches its built-in stdio-to-HTTP bridge. To check an already-running server:

```bash
python scripts/smoke_mcp_headless_bridge.py \
  --mcp-url http://127.0.0.1:8000/mcp \
  --token <local-token> \
  --json
```

The committed release evidence lives at:

```text
docs/evidence/headless-mcp-bridge.json
```

Refresh it before release:

```bash
python scripts/smoke_mcp_headless_bridge.py \
  --out docs/evidence/headless-mcp-bridge.json
```

## Client Config Generator

Generate copyable configs instead of hand-assembling JSON:

```bash
python scripts/generate_mcp_client_config.py --client cursor --auth-disabled
python scripts/generate_mcp_client_config.py --client claude --token <local-token>
python scripts/generate_mcp_client_config.py --client claude --stdio-bridge --token <local-token>
```

The generator emits:

- Claude Desktop direct HTTP config.
- Claude Desktop stdio bridge config using `npx -y mcp-remote`.
- Cursor `.cursor/mcp.json` direct HTTP config.

When `--auth-disabled` is set, no `Authorization` header is emitted. When `--token` is passed, the output includes a copyable `Bearer` header.

## Preflight Contract

`scripts/preflight_release.py` checks `docs/evidence/headless-mcp-bridge.json` as a hard release item:

- Missing evidence: `FAIL`.
- Wrong version: `FAIL`.
- Evidence status not `PASS`: `FAIL`.
- Missing `bridge.start`, `mcp.initialize`, `mcp.tools_list`, `mcp.tools_call`, or `mcp.policy_denial`: `FAIL`.

Claude Desktop / Cursor GUI evidence remains a separate warning-only check until the GUI runbooks are completed.
