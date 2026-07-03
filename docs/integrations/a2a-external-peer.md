# A2A 外部 Peer Smoke

适用版本：DeepSeek Infra v2.7.1。

本页说明如何在无 GUI、无 API key 的环境中复现 A2A external peer 兼容性证据。这里的 external peer 指独立进程或外部进程暴露的 A2A server；它不等同于真实第三方生态实现。

## 什么时候使用

- CI 或 release readiness 环境。
- 服务器环境，无法打开 GUI 客户端。
- 已经拿到一个外部 A2A endpoint，需要快速验收 Agent Card / JSON-RPC / SSE contract。
- 需要为 `docs/evidence/a2a-external-peer.json` 生成可归档 evidence。

## 默认路径：启动仓库内独立进程 peer

```bash
python scripts/smoke_a2a_external_peer.py \
  --out docs/evidence/a2a-external-peer.json
```

不传 `--peer-url` 时，脚本会启动 `examples/a2a_interop_peer.py` 作为独立进程 peer，并在临时本地端口上完成端到端验证。

## 验证真实外部 peer

```bash
python scripts/smoke_a2a_external_peer.py \
  --peer-url http://127.0.0.1:8002 \
  --peer-type independent-process \
  --out docs/evidence/a2a-external-peer.json \
  --markdown docs/evidence/a2a-external-peer.md \
  --json
```

`--peer-url` 可以是 peer root，也可以是 `/a2a/agents/<agent-id>` JSON-RPC endpoint。脚本会优先读取 `/.well-known/agent-card.json`，再使用 Agent Card 里的 `url` 作为 JSON-RPC endpoint。

## 覆盖范围

- `GET /.well-known/agent-card.json`
- `message/send`
- `message/stream`
- `tasks/get`
- `tasks/cancel`
- `tasks/list`
- artifact chunks 顺序和 final chunk
- SSE final `status-update`

## 证据

默认输出：

```text
docs/evidence/a2a-external-peer.json
```

核心字段：

```json
{
  "schemaVersion": "a2a-external-peer-evidence.v1",
  "version": "2.4.6",
  "commit": "abc1234",
  "generatedAt": "2026-06-28T00:00:00Z",
  "environment": {
    "os": "Windows",
    "python": "3.12.0",
    "ci": false
  },
  "peer": {
    "name": "A2A Interop Peer",
    "type": "independent-process"
  },
  "peerType": "independent-process",
  "checks": {
    "agentCard": "PASS",
    "messageSend": "PASS",
    "messageStream": "PASS",
    "tasksGet": "PASS",
    "tasksCancel": "PASS",
    "tasksList": "PASS",
    "artifactChunks": "PASS",
    "sseFinalEvent": "PASS"
  },
  "status": "PASS"
}
```

`scripts/preflight_release.py` 会把这份 evidence 作为硬检查。缺失、版本不一致、状态非 `PASS` 或关键 check 非 `PASS` 都会导致 preflight FAIL。

## 第三方生态说明

第三方生态实现使用分层标注：

- `docs/evidence/a2a-external-peer.json`：最低交付标准，必须 PASS。
- `docs/evidence/a2a-third-party-peer.json` / `.md`：v2.4.5 起的第三方生态展示标准，缺失时 preflight 只 WARNING；存在时必须 `peerType=third-party` 且八类 checks 全 PASS。

第三方或第三方风格 A2A-compatible peer 可用时，运行：

```bash
python scripts/smoke_a2a_external_peer.py \
  --peer-url http://<third-party-host>:<port> \
  --peer-type third-party \
  --out docs/evidence/a2a-third-party-peer.json \
  --markdown docs/evidence/a2a-third-party-peer.md
```

LangGraph / CrewAI / Google A2A reference 等具体实现通过同一 smoke 后，继续在 [a2a-third-party-plan.md](a2a-third-party-plan.md) 与 [../COMPATIBILITY.md](../COMPATIBILITY.md) 补充实现名称、版本、commit 与日期。
