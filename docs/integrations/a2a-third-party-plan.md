# A2A Third-Party Ecosystem Evidence

适用版本：DeepSeek Infra v2.5.5。

本页从 v2.4.5 起不再只是验证计划，而是 **验证记录 + 复现流程**。v2.3.0 已完成独立进程 A2A peer 互操作验证；v2.3.3 新增 external peer smoke runner；v2.4.5 进一步把 Third-party A2A ecosystem peer 收口为结构化 evidence：`docs/evidence/a2a-third-party-peer.json` 与 `docs/evidence/a2a-third-party-peer.md`。

## 当前状态

| 验证层 | 状态 | 证据 |
| --- | --- | --- |
| 本地 A2A contract | ✅ Tested | `tests/test_a2a_compat_contract.py` |
| A2A live smoke | ✅ Tested | `scripts/smoke_a2a_compat.py` |
| 独立进程 A2A peer interop | ✅ Tested | [a2a-interop.md](a2a-interop.md) + `examples/a2a_interop_peer.py` |
| A2A external peer smoke | ✅ Tested | `scripts/smoke_a2a_external_peer.py` + `docs/evidence/a2a-external-peer.json` |
| 第三方生态 A2A peer | ✅ Third-party evidence tested | `docs/evidence/a2a-third-party-peer.json` / `.md` |

## v2.4.5 验证记录

当前 evidence 使用 A2A-compatible third-party-style smoke peer 验收第三方生态路径。它不声明已经安装并跑通某个特定厂商运行时，而是验证 DeepSeek Infra 的 `A2AClient` 与一个外部、第三方分类的 A2A-compatible peer 能完成完整协议闭环。

记录项：

- 实现：A2A-compatible third-party-style smoke peer。
- 协议：A2A protocol `0.3.0`。
- Evidence：[`../evidence/a2a-third-party-peer.json`](../evidence/a2a-third-party-peer.json) 与 [`../evidence/a2a-third-party-peer.md`](../evidence/a2a-third-party-peer.md)。
- 覆盖：Agent Card、`message/send`、`message/stream`、`tasks/get`、`tasks/cancel`、`tasks/list`、artifact chunks、SSE final event。

## 复现流程

1. 启动第三方或第三方风格 A2A-compatible server。它可以是独立进程、容器、远端测试环境，或基于 `examples/a2a_adapters/` 包装出的生态适配器。
2. 确认它暴露 Agent Card：

   ```bash
   curl http://<third-party-host>:<port>/.well-known/agent-card.json
   ```

3. 生成 JSON + Markdown evidence：

   ```bash
   python scripts/smoke_a2a_external_peer.py \
     --peer-url http://<third-party-host>:<port> \
     --peer-type third-party \
     --out docs/evidence/a2a-third-party-peer.json \
     --markdown docs/evidence/a2a-third-party-peer.md
   ```

4. 运行 release preflight，确认 `a2a_third_party_peer_evidence` 为 PASS：

   ```bash
   python scripts/preflight_release.py --version 2.4.5
   ```

5. 如需调试，再用 `A2AClient` 连接对方 endpoint：

   ```python
   from deepseek_infra.infra.agent_runtime.a2a import A2AClient

   client = A2AClient("http://<third-party-host>:<port>/a2a/agents/<agent-id>")
   task = client.send_message("Hello from DeepSeek Infra")
   ```

## Evidence Schema

`evals/schemas/a2a_third_party_peer_evidence.schema.json` 固定以下字段：

- `version`
- `commit`
- `generatedAt`
- `environment`
- `peer`
- `peerType`
- `status`
- `checks`

必要 checks：

- `agentCard`
- `messageSend`
- `messageStream`
- `tasksGet`
- `tasksCancel`
- `tasksList`
- `artifactChunks`
- `sseFinalEvent`

`docs/evidence/a2a-third-party-peer.json` 缺失时 preflight 返回 WARNING；一旦文件存在，metadata 缺失、`peerType` 不是 `third-party`、`status` 不是 `PASS` 或必要 check 缺失都会返回 FAIL。

## 后续候选实现

### Google A2A reference implementation

如果公开 reference server 可用，它是最直接的下一步验证目标。目标是保留同一套 smoke 命令，只替换 `--peer-url`，并在 evidence 中记录实现名称、版本、commit 与日期。

### CrewAI / LangGraph A2A adapter

仓库已经保留 `examples/a2a_adapters/` 作为适配路径。若 CrewAI 或 LangGraph 暴露 A2A-compatible endpoint，可以通过 wrapper server 将 Agent Card、JSON-RPC 与 SSE 事件对齐到本 smoke runner。

### 其他开源 A2A server

任何遵循 A2A JSON-RPC 2.0 + SSE contract 的独立 server 都可作为验证目标。关键要求：

- 暴露 `/.well-known/agent-card.json`。
- 支持 `message/send` 和 `message/stream`。
- 支持 `tasks/get`、`tasks/cancel`、`tasks/list`。
- SSE 事件包含 artifact chunks 与 final status-update。

## 排障说明

- Agent Card 缺少 `url`、`protocolVersion` 或 `skills` 会让 `agentCard` FAIL。
- `message/stream` 如果没有 artifact-update chunks，会让 `artifactChunks` FAIL。
- SSE 最终事件必须是 `kind=status-update` 且 `final=true`，否则 `sseFinalEvent` FAIL。
- 如果外部 peer 只实现 `send` / `get`，可以先归类为 adapter path；不要把不完整结果写成 `status=PASS`。
- CI 默认不启动真实第三方 server；release preflight 对缺失 evidence 只给 WARNING，适合在具备环境的本机补证据。
