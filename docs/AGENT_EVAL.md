# Agent Eval Replay

适用版本：v2.3.3。

Agent Eval 仍然是离线回放：真实多 Agent 运行可以在线生成 prediction，但 CI 只读取已经录制好的 JSONL，与 `evals/golden/agent_tasks.jsonl` 按 `id` join 后评分。v2.2.8 的目标是先稳定录制格式和报告，不把 Agent Success 或 latency 退化升级为 hard gate。

## 录制格式

推荐每行 prediction 使用 `evals/schemas/agent_prediction.schema.json` 描述的稳定字段：

```json
{
  "id": "agent_001",
  "task": "Summarize uploaded docs and cite sources",
  "model": "deepseek-v4-pro",
  "tools": ["search_files", "read_file_chunk"],
  "final": "answer text with expected keywords",
  "status": "succeeded",
  "latencyMs": 4020,
  "usage": {
    "inputTokens": 3200,
    "outputTokens": 900,
    "estimatedCostUsd": 0.0031
  },
  "trace": {
    "agentCount": 4,
    "retryCount": 0,
    "toolErrorCount": 0
  }
}
```

示例文件是 `evals/golden/agent_predictions.v2.2.8.sample.jsonl`。旧格式的 `answer`、`prompt_tokens`、`completion_tokens` 仍可被读取，但新录制建议使用 `final`、`inputTokens`、`outputTokens`。

## Normalizer

`deepseek_infra/infra/evaluation/agent_recording.py` 会在评分前归一化 prediction：

- 删除 `runId`、`traceId`、`spanId`、`eventId`、`timestamp`、`createdAt`、`startedAt`、`completedAt` 等非确定字段。
- 将 `tools` / `toolCalls` / `tool_calls` 统一成去重后的工具名列表。
- 将 `final` / `answer` / `content` 统一成最终答案文本。
- 将 `inputTokens` / `outputTokens` 映射到 harness 使用的 token 字段。
- 保留 `latencyMs`、`usage` 和 `trace.agentCount/retryCount/toolErrorCount`，但这些指标先只进入报告和 baseline warning。

最终答案不做全文精确匹配，只用 golden 里的 `expected_keywords` 做覆盖率判断。

## 本地回放

```bash
python evals/runners/run_agent_eval.py \
  --golden evals/golden/agent_tasks.jsonl \
  --predictions evals/golden/agent_predictions.v2.2.8.sample.jsonl \
  --report-dir evals/reports \
  --report-only
```

输出：

- `evals/reports/agent-latest.json`
- `evals/reports/agent-latest.md`

状态语义：

- `PASS`：结构有效，指标达到建议阈值，baseline 对比无 warning。
- `WARNING`：结构有效，但 Agent Success、Tool Call Accuracy、Prompt Regression 或 baseline 对比有退化。
- `FAIL`：JSONL 无法解析、golden id 缺失、prediction schema/normalization 失败。

## Suite 集成

统一 suite 默认不跑 Agent Eval：

```bash
python evals/runners/run_offline_eval_suite.py
```

需要完整报告时显式加入：

```bash
python evals/runners/run_offline_eval_suite.py --include-agent
```

`--include-agent` 只把 Agent 指标作为 report-only 区块写入 `latest.json/latest.md`；Agent WARNING 不会影响 RAG / Tool Policy / Injection 的硬门禁状态。

## Baseline

`evals/baselines/agent-v2.2.8.json` 是当前 Agent replay baseline。2.2.8 中 baseline compare 只输出 `PASS` / `WARNING`，不阻断 CI。只有当录制格式、样本和 normalizer 稳定到足以承受真实输出波动后，才应在 v2.4 之类的后续版本把 Agent Eval 升级为 hard gate。

## v2.2.9

v2.2.9 是 v2.2.x 收官版，主题是发布前体检与运行时诊断，**不改变 Agent Eval 的录制格式、normalizer 或 report-only 语义**。`agent-latest.json` 仍是 report-only artifact，baseline 仍是 `agent-v2.2.8.json`。发版前用 `python scripts/preflight_release.py --version 2.2.9` 校验 `agent-latest.json` 可解析且版本同步；用 `python scripts/smoke_release.py --offline` 一键跑 doctor + offline eval + Agent Eval。详见 [docs/RELEASE_READINESS.md](RELEASE_READINESS.md)。
