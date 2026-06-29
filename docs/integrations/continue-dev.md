# Continue.dev MCP Integration

适用版本：DeepSeek Infra v2.5.5。

本页是可复现配置说明 + GUI 实机验证 runbook。DeepSeek Infra 端的 MCP endpoint 已由本地 client、CI mock server、policy gate、trace diagnostics 覆盖。v2.4.5 已完成 Continue.dev MCP 实机验证，证据见下方 Evidence Template。

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

## 2. Continue.dev MCP Config

Continue.dev 支持通过 `config.json` 或 `config.ts` 配置 MCP server。在 Continue.dev 的配置中加入：

```json
{
  "experimental": {
    "mcpServers": {
      "deepseek-infra": {
        "transport": {
          "type": "streamable-http",
          "url": "http://127.0.0.1:8000/mcp"
        }
      }
    }
  }
}
```

如果你用 `AUTH_DISABLED=1` 启动，上述配置即可工作。

如果需要 Bearer token 鉴权：

```json
{
  "experimental": {
    "mcpServers": {
      "deepseek-infra": {
        "transport": {
          "type": "streamable-http",
          "url": "http://127.0.0.1:8000/mcp",
          "headers": {
            "Authorization": "Bearer <YOUR_LOCAL_TOKEN>"
          }
        }
      }
    }
  }
}
```

## 3. Alternative: Stdio Bridge

如果 Continue.dev 版本只接受 stdio MCP server，可以用 `mcp-remote` 做 stdio → HTTP bridge：

```json
{
  "experimental": {
    "mcpServers": {
      "deepseek-infra": {
        "transport": {
          "type": "stdio",
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
  }
}
```

`AUTH_DISABLED=1` 时可删除 `--header` 和下一项。

## 4. Verify

1. Reload Continue.dev after editing its MCP config.
2. Confirm `deepseek-infra` appears in Continue.dev's MCP/tools list.
3. Run a safe tool such as `data_transform` or `python_eval`.
4. Confirm DeepSeek Infra is healthy:

```powershell
python examples/mcp_tool_demo.py --base-url http://127.0.0.1:8000/mcp --token <YOUR_LOCAL_TOKEN>
```

Expected local evidence:

- `initialize` reports server `deepseek-infra`
- `tools/list` returns the local tool catalog
- `tools/call data_transform` returns structured content

## 5. GUI Verification Runbook（Continue.dev）

完成以下步骤后，把证据填入下方模板并更新 `docs/COMPATIBILITY.md`。

### 步骤

1. **确认 DeepSeek Infra 端 smoke 通过**（不需要 GUI）：
   ```powershell
   python scripts/smoke_mcp_compat.py --token <YOUR_LOCAL_TOKEN> --json
   ```
   所有 7 步应为 `pass`。这验证 DeepSeek Infra 端 initialize / tools/list / tools/call / policy gate / external health 全部正常。

2. **安装 Continue.dev**（VS Code 或 JetBrains 扩展）并打开本仓库工作区。

3. **配置 MCP server**（见上方 §2 或 §3）。

4. **Reload Continue.dev**，确认 `deepseek-infra` 出现在 MCP 工具列表。

5. **验证 tools/list**：在 Continue.dev 中让它列出可用 MCP tools。预期：17 个本地工具（`data_transform`、`fetch_url`、`python_eval`、`search_files` 等）。

6. **验证低风险工具调用**：让 Continue.dev 调用一个安全工具，例如：
   - "Use the `data_transform` tool to summarize the numbers 1 2 3 4"
   - 或 "Use `python_eval` to compute 2+2"
   预期：返回结构化结果，不报错。

7. **验证 Tool Policy 拦截**：让 Continue.dev 尝试一个被策略拦截的调用：
   - "Use `fetch_url` to get http://127.0.0.1/admin"
   预期：工具返回 `isError: true`，包含 `ssrf` / `blocked` / `forbidden` 关键词。

8. **验证结果不污染系统提示**：确认工具返回内容出现在 assistant 回复中，而不是注入到 system prompt。

9. **截图或记录关键输出**，填入下方证据模板。

### Evidence Template

完成验证后，把以下内容贴入 `docs/COMPATIBILITY.md` 的 MCP Client Compatibility 表：

```markdown
| Continue.dev | ✅ Tested | integrations/continue-dev.md + evidence/continue-dev-mcp.json | Continue.dev <version>, commit <sha>, OS, date：tools/list + data_transform + fetch_url SSRF blocked + 系统提示无污染 |
```

填写示例（替换尖括号内容）：

| 字段 | 值 |
| --- | --- |
| Continue.dev 版本 | 1.2.0 (VS Code) |
| DeepSeek Infra commit | `<current-commit>` |
| 测试日期 | 2026-06-28 |
| OS | Windows 11 |
| Config loaded | ✅ `config.json` / `config.ts` |
| tools/list | ✅ 17 个本地工具全部列出 |
| 低风险工具调用 | ✅ `data_transform` count=4 |
| Tool Policy 拦截 | ✅ `fetch_url` http://127.0.0.1/admin SSRF blocked |
| 系统提示无污染 | ✅ |

## 6. Troubleshooting

| Symptom | Check |
| --- | --- |
| Continue.dev shows no tools | Confirm DeepSeek Infra is running and `MCP_ENABLED=1`. Reload Continue.dev after config change. |
| 401 / unauthorized | Use the token from `.auth-token`, or start with `AUTH_DISABLED=1` for local-only testing. Verify `Authorization: Bearer` header in config. |
| Tools list is empty | Check that Continue.dev's MCP config `transport.type` matches your transport (e.g. `streamable-http`). |
| Tool call denied | The Tool Policy gate is working. For high-risk tools, pass explicit approval metadata or use safe tools first. |
| connection refused | Verify `python app.py` is running on port 8000. Run `curl http://127.0.0.1:8000/healthz`. |
| External bridged tool missing | Check `MCP_CLIENT_ENABLED=1`, `MCP_CLIENT_SERVERS`, then open `GET /api/mcp/external/tools`. |

Reference docs:

- Continue.dev MCP docs: <https://docs.continue.dev/customization/tools#mcp>
- Model Context Protocol transports: <https://modelcontextprotocol.io/specification>
