# Agent Eval Replay

适用版本：v2.5.2。

Agent Eval 是离线回放硬门禁：真实多 Agent 运行可以在线生成 prediction，但 CI 只读取已经录制好的 JSONL，与 `evals/golden/agent_tasks.jsonl` 按 `id` join 后评分。v2.4.0 起 `run_agent_eval.py --strict` 进入 CI，Tool Call Accuracy、Agent Success Rate、Prompt Regression 和 baseline warning 都会阻断回归。

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
  --strict
```

输出：

- `evals/reports/agent-latest.json`
- `evals/reports/agent-latest.md`

状态语义：

- `PASS`：结构有效，指标达到建议阈值，baseline 对比无 warning。
- `WARNING`：结构有效，但 Agent Success、Tool Call Accuracy、Prompt Regression 或 baseline 对比有退化；CI strict 模式下会返回 `exit 1`。
- `FAIL`：JSONL 无法解析、golden id 缺失、prediction schema/normalization 失败。

## Suite 集成

统一 suite 默认不跑 Agent Eval：

```bash
python evals/runners/run_offline_eval_suite.py
```

需要完整报告时显式加入；CI 使用 strict 模式：

```bash
python evals/runners/run_offline_eval_suite.py --include-agent --strict
```

`--include-agent` 会把 Agent 指标写入 `latest.json/latest.md`；`--strict` 下 Agent WARNING 会让 suite 状态变成 FAIL。

## Baseline

`evals/baselines/agent-v2.2.8.json` 是当前 Agent replay baseline。v2.4.0 的 `compare_eval_baseline.py --strict --agent-baseline evals/baselines/agent-v2.2.8.json` 会把 Agent Success Rate 纳入 baseline regression gate；独立 `run_agent_eval.py --strict` 也会对 Tool Call Accuracy >= 0.90、Agent Success Rate >= 0.85、Prompt Regression Pass Rate >= 0.90 做硬门禁。

## v2.4.0

v2.4.0 把 Agent Eval 从 report-only 升级为 CI hard gate。发版前用 `python scripts/preflight_release.py --version 2.4.0` 校验 `agent-latest.json` 可解析、版本同步且状态为 PASS；用 `python scripts/smoke_release.py --offline` 一键跑 doctor + strict offline eval suite + security corpus + Agent Eval + baseline compare。详见 [docs/RELEASE_READINESS.md](RELEASE_READINESS.md)。

## v2.2.9

v2.2.9 是 v2.2.x 收官版，主题是发布前体检与运行时诊断，**不改变 Agent Eval 的录制格式、normalizer 或 report-only 语义**。`agent-latest.json` 仍是 report-only artifact，baseline 仍是 `agent-v2.2.8.json`。发版前用 `python scripts/preflight_release.py --version 2.2.9` 校验 `agent-latest.json` 可解析且版本同步；用 `python scripts/smoke_release.py --offline` 一键跑 doctor + offline eval + Agent Eval。详见 [docs/RELEASE_READINESS.md](RELEASE_READINESS.md)。
