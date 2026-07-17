# A2A 互操作 — 独立进程 Peer

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


适用版本：DeepSeek Infra v2.3.0。

本页记录 DeepSeek Infra 的 `A2AClient` 与一个**独立进程 A2A peer** 的互操作验证结果。

> **诚实标注**：本次验证的是独立进程 interop，不是第三方生态 A2A 实现。Peer 使用 Python 标准库（`http.server`）构建，运行在独立端口，有自己的 Agent Card、JSON-RPC endpoint 和 task store。它验证 DeepSeek Infra 的 `A2AClient` 能与一个遵循 A2A JSON-RPC + SSE contract 的外部 server 真正互通。第三方生态 A2A 实现仍待实机。

## 互操作 Peer

| 字段 | 值 |
| --- | --- |
| Peer 实现 | Python 标准库 `http.server`（`ThreadingHTTPServer`），无第三方依赖 |
| Peer 脚本 | `examples/a2a_interop_peer.py` |
| 协议 | A2A JSON-RPC 2.0 over `POST /a2a/agents/interop-peer` |
| Agent Card | `GET /.well-known/agent-card.json`（protocolVersion `0.3.0`） |
| 流式响应 | `text/event-stream`（SSE） |
| 验证日期 | 2026-06-27 |
| 验证 commit | `6edcda5` |
| OS | Windows 11 |

## 复现步骤

### 1. 启动 A2A peer（独立进程，端口 8002）

```bash
python examples/a2a_interop_peer.py --port 8002 --delay 2.0
```

`--delay` 控制 progress chunk 与 answer chunk 之间的间隔（秒），用于测试 cancel。

### 2. 用 A2AClient 验证

```python
from deepseek_infra.infra.agent_runtime.a2a import A2AClient

client = A2AClient("http://127.0.0.1:8002/a2a/agents/interop-peer", timeout_seconds=30)

# Agent Card 发现
import urllib.request, json
card = json.loads(urllib.request.urlopen("http://127.0.0.1:8002/.well-known/agent-card.json").read())

# message/send
task = client.send_message("Hello from DeepSeek Infra")

# tasks/get
task = client.get_task(task["id"])

# message/stream
for event in client.message_stream("Stream test"):
    print(event)

# tasks/cancel
task = client.send_message("Cancel me")
client.cancel_task(task["id"])
```

## 验证结果（2026-06-27 · commit `6edcda5`）

| # | 检查项 | 状态 | 证据 |
| --- | --- | --- | --- |
| 1 | Agent Card discovery | ✅ PASS | `name=A2A Interop Peer, protocolVersion=0.3.0, url=…/a2a/agents/interop-peer` |
| 2 | `message/send` | ✅ PASS | 返回 `kind=task`，初始 `state=working` |
| 3 | `tasks/get` | ✅ PASS | 提交后 0.3s `state=working artifacts=0`；2.5s 后 `state=completed artifacts=1` |
| 4 | `message/stream` | ✅ PASS | 5 个 SSE 事件：`task → artifact-update → status-update → artifact-update → status-update(final)`；2 个 artifact chunks；`final_state=completed` |
| 5 | `tasks/cancel` | ✅ PASS | 提交后立即 cancel → `state=canceling`（cancel 请求在 2s delay 内到达） |
| 6 | `tasks/list` | ✅ PASS | 返回 3 个 task（前面创建的） |

### message/stream 事件序列

```
event 1: kind=task          (初始 Task，state=working)
event 2: kind=artifact-update (chunk 0, "progress", final=false)
event 3: kind=status-update   (state=working, final=false)
event 4: kind=artifact-update (chunk 1, "answer", final=true)
event 5: kind=status-update   (state=completed, final=true)
```

## 验证覆盖的 A2A 方法

| 方法 | DeepSeek Infra 侧 | Peer 侧 | 验证结果 |
| --- | --- | --- | --- |
| `message/send` | `A2AClient.send_message()` | `_create_task()` + 后台线程 | ✅ |
| `message/stream` | `A2AClient.message_stream()` | `_stream_events()` SSE | ✅ |
| `tasks/get` | `A2AClient.get_task()` | `_handle_rpc()` | ✅ |
| `tasks/cancel` | `A2AClient.cancel_task()` | `_handle_rpc()` + cancel event | ✅ |
| `tasks/list` | `A2AClient._rpc("tasks/list", {})` | `_handle_rpc()` | ✅ |
| `tasks/resubscribe` | `A2AClient.resubscribe()` | `_stream_events()` | ✅ 合约已覆盖（contract test） |
| Agent Card | `GET /.well-known/agent-card.json` | `_agent_card()` | ✅ |

## 诚实标注

- 本次验证的 peer 是**独立进程**（有自己的 HTTP server、端口、task store），不是同进程 mock。它验证了 `A2AClient` 的 JSON-RPC 请求格式、SSE 事件解析、task 生命周期和 cancel 行为与一个外部 A2A server 真正互通。
- 它**不是**第三方生态 A2A 实现（如 Google A2A reference、CrewAI 等）。第三方生态实现仍标为待实机。
- Peer 的 task 处理是确定性模拟（echo + artifact chunks），不调用 LLM，不需要 API key。
- `tasks/resubscribe` 的合约由 `tests/test_a2a_compat_contract.py` 覆盖；本次 interop 侧重 `message/send` / `message/stream` / `tasks/get` / `tasks/cancel` 的端到端验证。
