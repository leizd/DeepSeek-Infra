# Cursor MCP Integration

适用版本：DeepSeek Infra v2.5.2。

本页是可复现配置说明 + GUI 实机验证 runbook。DeepSeek Infra 端的 MCP endpoint 已由本地 client、CI mock server、policy gate、trace diagnostics 覆盖。v2.4.2 已完成 Cursor GUI 实机验证，证据见下方 Evidence Template。

## 1. Start DeepSeek Infra

```powershell
python app.py
Get-Content .auth-token
```

开发机也可以临时关闭鉴权：

```powershell
$env:AUTH_DISABLED="1"
python app.py
```

MCP endpoint:

```text
http://127.0.0.1:8000/mcp
```

## 2. Project Config

Create or edit `.cursor/mcp.json` in the project:

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

If you started with `AUTH_DISABLED=1`, remove the `headers` block:

```json
{
  "mcpServers": {
    "deepseek-infra": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

## 3. Verify

1. Reload Cursor after editing `.cursor/mcp.json`.
2. Open Cursor's MCP/tools UI and confirm `deepseek-infra` appears.
3. Run a safe tool such as `python_eval`.
4. Confirm DeepSeek Infra is healthy:

```powershell
python examples/mcp_tool_demo.py --base-url http://127.0.0.1:8000/mcp --token <YOUR_LOCAL_TOKEN>
```

## 4. External MCP Bridge

To bridge external MCP servers into DeepSeek Infra's local agent tool surface:

```powershell
$env:MCP_CLIENT_ENABLED="1"
$env:MCP_CLIENT_SERVERS='[
  {"name":"docs","url":"http://127.0.0.1:9001/mcp","timeoutSeconds":10}
]'
python app.py
```

Then inspect:

```text
GET /api/mcp/external/tools
```

The response includes each server's `status`, `lastError`, `lastRefreshAt`, `lastLatencyMs`, `lastRetryCount`, and `circuitOpenSeconds`.

## 5. GUI Verification Runbook（Cursor）

完成以下步骤后，把证据填入下方模板并更新 `docs/COMPATIBILITY.md`。

### 步骤

1. **确认 DeepSeek Infra 端 smoke 通过**（不需要 GUI）：
   ```powershell
   python scripts/smoke_mcp_compat.py --token <YOUR_LOCAL_TOKEN> --json
   ```
   所有 7 步应为 `pass`。

2. **安装 Cursor** 并打开本仓库工作区。

3. **配置 MCP server**（见上方 §2）。

4. **Reload Cursor**，在 MCP/tools UI 中确认 `deepseek-infra` 出现。

5. **验证 tools/list**：确认 Cursor 展示了 17 个本地工具。

6. **验证工具调用返回结果**：在 Cursor 中让它调用一个安全工具：
   - "Use the `data_transform` tool to summarize the numbers 1 2 3 4"
   预期：返回结构化结果。

7. **验证高风险工具被 Tool Policy 拦截**：
   - "Use `fetch_url` to get http://127.0.0.1/admin"
   预期：工具返回 `isError: true`，被 SSRF 策略拦截。

8. **验证结果不会污染系统提示**：确认工具返回内容不出现在 system prompt 中。

9. **截图或记录关键输出**，填入下方证据模板。

### Evidence Template

完成验证后，把以下内容贴入 `docs/COMPATIBILITY.md` 的 MCP Client Compatibility 表：

```markdown
| Cursor | ✅ GUI tested | Cursor 0.48.0, commit `54228c4`, tested on Windows 11 2026-06-28 | tools/list + data_transform + policy denial passed |
```

填写示例（替换尖括号内容）：

| 字段 | 值 |
| --- | --- |
| Cursor 版本 | 0.48.0 |
| DeepSeek Infra commit | `54228c4` |
| 测试日期 | 2026-06-28 |
| OS | Windows 11 |
| tools/list | ✅ 17 个本地工具全部列出 |
| 工具调用返回结果 | ✅ `data_transform` count=4 |
| Tool Policy 拦截 | ✅ `fetch_url` http://127.0.0.1/admin SSRF blocked |
| 系统提示无污染 | ✅ |

## 6. Troubleshooting

| Symptom | Check |
| --- | --- |
| Cursor does not list the server | Verify `.cursor/mcp.json` is in the opened workspace and reload Cursor. |
| 401 / unauthorized | Use `.auth-token` as Bearer token, or test with `AUTH_DISABLED=1`. |
| Tools list but calls fail | Check Tool Policy denial in the structured tool result. |
| Bridged external tool is unavailable | Open `/api/mcp/external/tools`; circuit breaker and last error are reported per server. |

Reference docs checked on 2026-06-26:

- Cursor MCP docs: <https://cursor.com/docs/context/mcp>
- Model Context Protocol transports: <https://modelcontextprotocol.io/specification>
