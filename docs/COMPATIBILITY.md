# Compatibility Matrix（兼容性矩阵）

适用版本：v2.8.0。

这页只记录已经可复现的互操作结果，不把"协议上应该兼容"写成"实机已验证"。v2.3.0 的重点是把 v2.2.x 已完成的 MCP / A2A / 安全评测能力真正拿到外部实现里验一遍：MCP 客户端与官方 MCP Python SDK 的 Streamable HTTP transport 真正互通（SSE 响应解析修复）、A2A 客户端与独立进程 peer 端到端验证、Prompt Injection 对抗评测从 soft gate 毕业为 CI 硬门禁。v2.4.2 已完成 Claude Desktop / Cursor 的 GUI 实机验证并填入证据；v2.4.3 将 Edge Router 从 runbook-only 推进为结构化 smoke evidence；v2.4.5 将 Third-party A2A ecosystem peer 推进为 third-party-style structured evidence；v2.4.5 将 Continue.dev 从 Not tested 推进为结构化 MCP evidence；v2.4.6 将 Other OpenAI-compatible SDKs 从 Not tested 推进为结构化 SDK smoke evidence。

## Compatibility Smoke Pack / 兼容性冒烟测试包

先启动本地服务。开发机上最少可用：

```powershell
$env:AUTH_DISABLED="1"
python app.py
```

如果启用了本地鉴权，请把 `.auth-token` 里的值传给 `--token`，或设置 `DEEPSEEK_INFRA_TOKEN` / `AUTH_TOKEN`。

```powershell
python scripts/smoke_mcp_compat.py --token <local-token>
python scripts/smoke_a2a_compat.py --token <local-token>
python examples/edge_router_smoke.py --token <local-token>
python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md
```

真实第三方 MCP server 冒烟入口：

```powershell
python scripts/smoke_mcp_compat.py --token <local-token> --external-server-url http://127.0.0.1:9001/mcp
```

建议把命令、commit、时间、客户端/第三方 server 版本和关键输出一起贴入本页矩阵。默认 A2A smoke 不强制要求 artifact chunk，因为没有 `DEEPSEEK_API_KEY` 时任务可能以 `failed` 终态收束；离线 contract 测试会固定 artifact chunk 行为。需要在有上游 Key 的环境强制验 artifact，可加 `--strict-artifacts`。

### v2.2.5 冒烟证据

| 路径 | 状态 | 命令 | 覆盖范围 |
| --- | --- | --- | --- |
| MCP local smoke | ✅ Runner 已添加 | `python scripts/smoke_mcp_compat.py` | `/healthz`、`initialize`、`tools/list`、`tools/call`、policy gate、`/api/mcp/external/tools` |
| MCP real external server smoke | 🟡 入口就绪 | `python scripts/smoke_mcp_compat.py --external-server-url <url>` | 第三方 server 的 `initialize` / `tools/list`；本仓库未记录实机通过 |
| A2A live smoke | ✅ Runner 已添加 | `python scripts/smoke_a2a_compat.py` | Agent Card、agents list、`message/send`、`message/stream`、`tasks/resubscribe`、`tasks/cancel` |
| A2A external peer smoke | ✅ 已测试 | `python scripts/smoke_a2a_external_peer.py` + [integrations/a2a-external-peer.md](integrations/a2a-external-peer.md) | 独立进程 external peer：Agent Card + send + stream + get + cancel + list + artifact chunks + SSE final event。 |
| A2A contract regression | ✅ 已测试 | `pytest tests/test_a2a_compat_contract.py` | artifact chunks、SSE final status、resubscribe cursor、cancel lifecycle |
| Edge Router smoke | ✅ 冒烟证据已添加 | `python examples/edge_router_smoke.py --require-ollama --out docs/evidence/edge-router-smoke.json --markdown docs/evidence/edge-router-smoke.md` | `/api/edge/status`、`/v1/models`、Ollama-compatible local call、fallback readiness |

### 故障排查

| 症状 | 首选检查 | 可能的修复 |
| --- | --- | --- |
| `401 / unauthorized` | `cat .auth-token` 或环境变量 token | 传递 `--token`，设置 `DEEPSEEK_INFRA_TOKEN`，或以 `AUTH_DISABLED=1` 运行纯本地模式 |
| `connection refused` | `curl http://127.0.0.1:8000/healthz` | 启动 `python app.py`；验证 `DEFAULT_PORT` |
| MCP 工具调用失败但 initialize 正常 | 冒烟输出中的 `mcp.policy_gate` / `structuredContent` | 检查 `MCP_CAPABILITY`、Tool Policy 拒绝或工具参数 |
| A2A stream 无 artifact chunk | 冒烟输出中的最终状态 | 配置 `DEEPSEEK_API_KEY`，或仅视为端点冒烟并依赖 contract 测试 |
| 真实外部 MCP server 工具列表为空 | 第三方 server 日志 | 确认 server 使用 Streamable HTTP JSON-RPC 且支持 `tools/list` |

## MCP 客户端兼容性

| 客户端 / 路径 | 状态 | 证据 | 备注 |
| --- | --- | --- | --- |
| `examples/mcp_tool_demo.py` | ✅ 已测试 | `python examples/mcp_tool_demo.py` | 本地 Python MCP client，覆盖 `initialize` / `tools/list` / `tools/call`。 |
| MCP local smoke runner | ✅ Runner 已添加 | `python scripts/smoke_mcp_compat.py` | 覆盖本地 health、握手、目录、工具执行、policy gate 和外部 health API。 |
| Headless MCP bridge | ✅ 已测试 | `python scripts/smoke_mcp_headless_bridge.py` + [integrations/headless-mcp-client.md](integrations/headless-mcp-client.md) | 无 GUI 环境下验证 stdio bridge → Streamable HTTP、`tools/list`、`data_transform` 调用与 `fetch_url` policy denial。 |
| MCP test suite (`tests/test_mcp.py`) | ✅ 已测试 | CI + local pytest | 覆盖握手、目录、能力切片、工具执行、错误码、loopback client、外部 server profile、policy gate、结果清洗、trace diagnostics。 |
| `curl` JSON-RPC | ✅ 已测试 | `POST /mcp` | 适合排查 token、协议响应和工具目录。 |
| Claude Desktop | ✅ GUI tested / 已测试 | [integrations/claude-desktop.md](integrations/claude-desktop.md) | Claude Desktop 0.9.0, commit `54228c4`, Windows 11, 2026-06-28：tools/list + `data_transform` + `fetch_url` SSRF blocked + 系统提示无污染 |
| Cursor | ✅ GUI tested / 已测试 | [integrations/cursor.md](integrations/cursor.md) | Cursor 0.48.0, commit `54228c4`, Windows 11, 2026-06-28：tools/list + `data_transform` + `fetch_url` SSRF blocked + 系统提示无污染 |
| Continue.dev | ✅ Tested / 已测试 | [integrations/continue-dev.md](integrations/continue-dev.md) + [evidence/continue-dev-mcp.json](evidence/continue-dev-mcp.json) | Continue.dev 1.2.0, commit `2e2782e`, Windows 11, 2026-06-28：tools/list + `data_transform` + `fetch_url` SSRF blocked + 系统提示无污染 |

## MCP External Server Bridge / MCP 外部 Server 桥接

v2.2.1 起，外部 MCP server 的工具会以 `mcp__<server>__<tool>` 桥接进本地 Agent 工具面；v2.2.2 起，Agent 调用和 `/mcp tools/call` 都共享 executor 内部 ToolPolicy 闸门，远端 `isError=true` 会映射为本地 `upstream_tool_error`。

| 场景 | 状态 | 证据 | 备注 |
| --- | --- | --- | --- |
| Local mock external MCP server | ✅ 已测试 | `tests/test_mcp.py` | `MCPClient` 消费外部 `tools/list`，生成 `mcp__<server>__<tool>` profiles。 |
| External tool policy gate | ✅ 已测试 | `tests/test_mcp.py` + `scripts/smoke_mcp_compat.py` | 高风险/敏感参数进入 Tool Policy，拒绝时不会触达外部 server。 |
| External server unavailable | ✅ 已测试 | `tests/test_mcp.py` | 外部 server 失败不影响本地 MCP tools。 |
| Timeout / retry stats | ✅ 已测试 | `test_client_retries_retryable_transport_failures` | `MCPClient.last_stats` 记录 attempts、retry count、latency、timeout/error type。 |
| Circuit breaker | ✅ 已测试 | `test_external_mcp_registry_reports_health_and_opens_circuit` | 连续失败后进入短期 `circuit_open`，`/api/mcp/external/tools` 返回健康态。 |
| Trace diagnostics | ✅ 已测试 | `test_external_mcp_call_records_trace_diagnostics` | `mcp_external` span 记录 latency、attempts、retryCount、timeout、errorType。 |
| Real third-party Streamable HTTP MCP server | ✅ 官方 MCP SDK 互操作已测试 | `scripts/smoke_mcp_compat.py --external-server-url <url>` + [integrations/external-mcp-server.md](integrations/external-mcp-server.md) | 官方 `mcp` Python SDK v1.28.1 FastMCP `streamable-http` partner（`echo` / `word_count`），commit `6edcda5`，2026-06-27 验证：initialize / tools/list / tools/call / 桥接 `mcp__interop-partner__echo` / health API / 外部 server 挂掉时本地工具不受影响。SSE 响应解析为 v2.3.0 关键修复。 |

## 当前 MCP MVP 验收

| 验收项 | v2.3.0 结果 |
| --- | --- |
| 本地 MCP server | ✅ `POST /mcp` + examples + CI + smoke runner |
| 本地 mock external MCP server | ✅ CI |
| Claude Desktop | ✅ GUI tested / 已测试（v2.4.2）：tools/list + 低风险工具调用 + Tool Policy 拦截 + 系统提示无污染 |
| Cursor | ✅ GUI tested / 已测试（v2.4.2）：tools/list + 低风险工具调用 + Tool Policy 拦截 + 系统提示无污染 |
| 一个真实外部 MCP server | ✅ 官方 MCP SDK v1.28.1 partner 实测通过（SSE 解析 + 桥接 + health + policy gate） |
| 外部 server 挂掉 | ✅ health + local tools unaffected |
| schema/响应异常 | ✅ invalid JSON / malformed tool catalog mapped to upstream failure |
| 工具超时/重试 | ✅ client stats + trace diagnostics |
| 危险参数拦截 | ✅ Tool Policy gate |

## Health API

`GET /api/mcp/external/tools` 返回：

- `servers[]`: `available`、`status`、`timeoutSeconds`、`consecutiveFailures`、`lastError`、`lastErrorType`、`lastRefreshAt`、`lastLatencyMs`、`lastRetryCount`、`circuitOpenSeconds`
- `tools[]`: `server`、`tool`、`bridgedName`、`risk`、`network`、`filesystem`、`requiresApproval`

## OpenAI API 兼容性

| 客户端 | 状态 | 证据 |
| --- | --- | --- |
| OpenAI Python SDK (`openai>=1.0`) | ✅ 已测试 | `examples/openai_compatible_client.py` |
| `curl` | ✅ 已测试 | README examples |
| Ollama as provider | ✅ 已测试 | `OLLAMA_ENABLED=1` exposes `ollama/<tag>` through `/v1/models` |
| Edge Router smoke evidence | ✅ 已测试 | [EDGE_ROUTER_RUNBOOK.md](EDGE_ROUTER_RUNBOOK.md) + `examples/edge_router_smoke.py` + [evidence/edge-router-smoke.json](evidence/edge-router-smoke.json) |
| Other OpenAI-compatible SDKs | ✅ SDK smoke tested / SDK 冒烟已测试 | [evidence/openai-compatible-sdks.json](evidence/openai-compatible-sdks.json) / [openai-compatible-sdks.md](evidence/openai-compatible-sdks.md) | LangChain (ChatOpenAI)、LiteLLM、LlamaIndex (OpenAILike) 均已通过 models list、chat completion 与 streaming 验证。 |

## A2A 互操作兼容性

| 对端 | 状态 | 证据 |
| --- | --- | --- |
| Local A2A test suite (`tests/test_a2a.py`) | ✅ 已测试 | 14 个用例：artifact chunks、`tasks/resubscribe`、canceling、loopback client、metrics |
| A2A compatibility contract (`tests/test_a2a_compat_contract.py`) | ✅ 已测试 | Agent Card、`message/send`、`message/stream`、artifact chunks、`tasks/resubscribe`、`tasks/cancel` |
| A2A live smoke runner | ✅ Runner 已添加 | `python scripts/smoke_a2a_compat.py` | 针对运行中的本地服务器的端点级冒烟；artifact chunk 可通过 `--strict-artifacts` 严格化 |
| A2A external peer smoke runner | ✅ 已测试 | `python scripts/smoke_a2a_external_peer.py` + `docs/evidence/a2a-external-peer.json` | Agent Card / `message/send` / `message/stream` / `tasks/get` / `tasks/cancel` / `tasks/list` / artifact chunks / SSE final event。 |
| Local Agent Card discovery | ✅ 已测试 | `GET /.well-known/agent-card.json` |
| Local external A2A peer loopback | ✅ 已测试 | `examples/a2a_peer_demo.py` against `http://127.0.0.1:8001/a2a/agents/reasoner` |
| Third-party A2A ecosystem peer | ✅ Third-party evidence tested / 第三方证据已测试 | [evidence/a2a-third-party-peer.json](evidence/a2a-third-party-peer.json) / [a2a-third-party-peer.md](evidence/a2a-third-party-peer.md) + [integrations/a2a-third-party-plan.md](integrations/a2a-third-party-plan.md) | A2A-compatible third-party-style smoke peer, protocol `0.3.0`, commit `8a44088`, Windows 11, 2026-06-28：Agent Card + send + stream + get + cancel + list + artifact chunks + SSE final event。 |

## A2A MVP Acceptance / A2A MVP 验收

| 验收项 | v2.3.0 结果 |
| --- | --- |
| Artifact streaming chunks | ✅ `artifactId` / `chunkIndex` / `append` / `final` in `artifact-update` SSE events |
| `tasks/resubscribe` | ✅ 通过 `taskId` 和 `afterChunkIndex` 重连 |
| Local external peer loopback | ✅ `A2AClient.message_stream()` + `examples/a2a_peer_demo.py` |
| Independent-process A2A interop | ✅ `examples/a2a_interop_peer.py` — Agent Card / send / stream / get / cancel / list 全通过 |
| A2A trace / metrics | ✅ `a2a_task` / `a2a_peer_call` spans + `ai_a2a_*` Prometheus metrics |
| Cancellation lifecycle | ✅ `cancelRequestedAt`、`canceling -> canceled`、`discardedResult` trace diagnostics |
| Compatibility smoke entry | ✅ `scripts/smoke_a2a_compat.py` + `tests/test_a2a_compat_contract.py` |
