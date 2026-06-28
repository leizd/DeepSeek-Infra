# Claude Desktop MCP Integration

适用版本：DeepSeek Infra v2.5.1。

本页是可复现配置说明 + GUI 实机验证 runbook。DeepSeek Infra 端已验证的 MCP endpoint 是 `POST /mcp`（Streamable HTTP / JSON-RPC 2.0），本地鉴权默认需要 Bearer token。v2.4.2 已完成 Claude Desktop GUI 实机验证，证据见下方 Evidence Template。

## 1. Start DeepSeek Infra

开发机快速验证可以临时关闭本地鉴权：

```powershell
$env:AUTH_DISABLED="1"
python app.py
```

更接近真实使用的方式是保留鉴权，然后从启动日志或 `.auth-token` 取 token：

```powershell
python app.py
Get-Content .auth-token
```

MCP endpoint:

```text
http://127.0.0.1:8000/mcp
```

## 2. Direct Remote MCP Config

如果你的 Claude Desktop 版本支持 remote / Streamable HTTP MCP server，可在 Claude Desktop 的 MCP 配置中加入：

```json
{
  "mcpServers": {
    "deepseek-infra": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "Authorization": "Bearer <YOUR_LOCAL_TOKEN>"
      }
    }
  }
}
```

如果你用 `AUTH_DISABLED=1` 启动，可以去掉 `headers`。

## 3. Stdio Bridge Fallback

如果 Claude Desktop 只接受 stdio MCP server，可以用 `mcp-remote` 在本机做一层 stdio → HTTP bridge：

```json
{
  "mcpServers": {
    "deepseek-infra": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://127.0.0.1:8000/mcp",
        "--header",
        "Authorization: Bearer <YOUR_LOCAL_TOKEN>"
      ]
    }
  }
}
```

`AUTH_DISABLED=1` 时可删除 `--header` 和下一项。

## 4. Verify

1. Restart Claude Desktop after editing its MCP config.
2. Ask Claude to list available MCP tools or call a safe tool such as `python_eval`.
3. On the DeepSeek Infra side, verify:

```powershell
python examples/mcp_tool_demo.py --base-url http://127.0.0.1:8000/mcp --token <YOUR_LOCAL_TOKEN>
```

Expected local evidence:

- `initialize` reports server `deepseek-infra`
- `tools/list` returns the local tool catalog
- `tools/call python_eval` returns structured content

## 5. GUI Verification Runbook（Claude Desktop）

完成以下步骤后，把证据填入下方模板并更新 `docs/COMPATIBILITY.md`。

### 步骤

1. **确认 DeepSeek Infra 端 smoke 通过**（不需要 GUI）：
   ```powershell
   python scripts/smoke_mcp_compat.py --token <YOUR_LOCAL_TOKEN> --json
   ```
   所有 7 步应为 `pass`。这验证 DeepSeek Infra 端 initialize / tools/list / tools/call / policy gate / external health 全部正常。

2. **安装 Claude Desktop** 并重启。

3. **配置 MCP server**（见上方 §2 或 §3）。

4. **重启 Claude Desktop**，确认 `deepseek-infra` 出现在工具列表。

5. **验证 tools/list**：在 Claude Desktop 中让它列出可用 MCP tools。预期：17 个本地工具（`data_transform`、`fetch_url`、`python_eval`、`search_files` 等）。

6. **验证低风险工具调用**：让 Claude 调用一个安全工具，例如：
   - "Use the `data_transform` tool to summarize the numbers 1 2 3 4"
   - 或 "Use `python_eval` to compute 2+2"
   预期：返回结构化结果，不报错。

7. **验证 Tool Policy 拦截**：让 Claude 尝试一个被策略拦截的调用：
   - "Use `fetch_url` to get http://127.0.0.1/admin"
   预期：工具返回 `isError: true`，包含 `ssrf` / `blocked` / `forbidden` 关键词。

8. **验证结果不污染系统提示**：确认工具返回内容出现在 assistant 回复中，而不是注入到 system prompt。

9. **截图或记录关键输出**，填入下方证据模板。

### Evidence Template

完成验证后，把以下内容贴入 `docs/COMPATIBILITY.md` 的 MCP Client Compatibility 表：

```markdown
| Claude Desktop | ✅ GUI tested | Claude Desktop 0.9.0, commit `54228c4`, tested on Windows 11 2026-06-28 | tools/list + data_transform + policy denial passed |
```

填写示例（替换尖括号内容）：

| 字段 | 值 |
| --- | --- |
| Claude Desktop 版本 | 0.9.0 |
| DeepSeek Infra commit | `54228c4` |
| 测试日期 | 2026-06-28 |
| OS | Windows 11 |
| tools/list | ✅ 17 个本地工具全部列出 |
| 低风险工具调用 | ✅ `data_transform` count=4 |
| Tool Policy 拦截 | ✅ `fetch_url` http://127.0.0.1/admin SSRF blocked |
| 系统提示无污染 | ✅ |

## 6. Troubleshooting

| Symptom | Check |
| --- | --- |
| Claude shows no tools | Confirm DeepSeek Infra is running and `MCP_ENABLED=1`. |
| 401 / unauthorized | Use the token from `.auth-token`, or start with `AUTH_DISABLED=1` for local-only testing. |
| Tool call denied | The Tool Policy gate is working. For high-risk tools, pass explicit approval metadata or use safe tools first. |
| External bridged tool missing | Check `MCP_CLIENT_ENABLED=1`, `MCP_CLIENT_SERVERS`, then open `GET /api/mcp/external/tools`. |

Reference docs checked on 2026-06-26:

- Anthropic MCP docs: <https://docs.anthropic.com/en/docs/agents-and-tools/mcp>
- Model Context Protocol transports: <https://modelcontextprotocol.io/specification>
