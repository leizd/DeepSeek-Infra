# Headless MCP Client 兼容性

适用版本：DeepSeek Infra v2.9.0。

Headless MCP 验证是面向 CI、服务器以及未安装 Claude Desktop 或 Cursor 的机器的无 GUI 兼容性路径。它证明这些应用通常依赖的 MCP client 路径可以通过 stdio bridge 接入 DeepSeek Infra 的 Streamable HTTP endpoint。

它**不**意味着 Claude Desktop 或 Cursor 的 GUI 验证。在做完 [claude-desktop.md](claude-desktop.md) 和 [cursor.md](cursor.md) 的 GUI runbook 之前，这两行保持 🟡。

## 验证内容

- 本地 DeepSeek Infra 可以在没有 API key 的情况下启动。
- stdio bridge 可以将 JSON-RPC 消息转发到 `POST /mcp`。
- MCP `initialize` 成功。
- MCP `tools/list` 暴露预期的本地工具。
- MCP `tools/call` 可以运行 `data_transform`。
- Tool Policy 拦截通过 `fetch_url` 发起的 SSRF 探测。
- 证据 JSON 记录结果，不存储任何 Bearer token。

## 运行

```bash
python scripts/smoke_mcp_headless_bridge.py --json
```

默认情况下，脚本会在一个临时 localhost 端口上启动内嵌服务器（设置 `AUTH_DISABLED=1`），然后启动内置的 stdio-to-HTTP bridge。要检查已在运行的服务器：

```bash
python scripts/smoke_mcp_headless_bridge.py \
  --mcp-url http://127.0.0.1:8000/mcp \
  --token <local-token> \
  --json
```

已提交的发布证据位于：

```text
docs/evidence/headless-mcp-bridge.json
```

发布前刷新：

```bash
python scripts/smoke_mcp_headless_bridge.py \
  --out docs/evidence/headless-mcp-bridge.json
```

## Client Config Generator / 客户端配置生成器

生成可复制的配置，无需手动拼装 JSON：

```bash
python scripts/generate_mcp_client_config.py --client cursor --auth-disabled
python scripts/generate_mcp_client_config.py --client claude --token <local-token>
python scripts/generate_mcp_client_config.py --client claude --stdio-bridge --token <local-token>
```

生成器输出：

- Claude Desktop 直连 HTTP 配置。
- Claude Desktop 通过 `npx -y mcp-remote` 的 stdio bridge 配置。
- Cursor `.cursor/mcp.json` 直连 HTTP 配置。

设置 `--auth-disabled` 时，不输出 `Authorization` 头。传入 `--token` 时，输出包含可复制的 `Bearer` 头。

## 发布前检查合约

`scripts/preflight_release.py` 将 `docs/evidence/headless-mcp-bridge.json` 作为硬性发布项进行检查：

- 缺少证据：`FAIL`。
- 版本不对：`FAIL`。
- 证据状态不是 `PASS`：`FAIL`。
- 缺少 `bridge.start`、`mcp.initialize`、`mcp.tools_list`、`mcp.tools_call` 或 `mcp.policy_denial`：`FAIL`。

在 GUI runbook 完成之前，Claude Desktop / Cursor 的 GUI 证据仅作为单独的警告检查项。
