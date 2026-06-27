# Eval Reports

适用版本：v2.2.8。

v2.2.7 把 RAG、Tool Policy 和 Prompt Injection adversarial eval 从分散 CLI 输出升级为一份可归档、可比较、可上传到 CI artifact 的离线评测报告。v2.2.8 在这个证据链旁边补齐 Agent Eval 的稳定录制回放：`agent-latest.json` / `agent-latest.md` 作为 report-only artifact 输出，和 `agent-v2.2.8` baseline 做 warning 级对比。目标仍然不是扩大硬门禁面，而是让每次 PR 都能看到当前分数、版本信息、数据集规模、阈值和退化判断。

## 本地复跑

```bash
python evals/runners/run_offline_eval_suite.py \
  --out evals/reports/latest.json \
  --markdown evals/reports/latest.md

python evals/runners/compare_eval_baseline.py \
  --baseline evals/baselines/v2.2.6.json \
  --current evals/reports/latest.json

python evals/runners/run_agent_eval.py \
  --report-dir evals/reports \
  --report-only
```

也可以用封装脚本一次完成刷新、对比和 Agent report-only 报告：

```bash
python scripts/update_eval_report.py
```

需要把 Agent Eval 聚合进统一 suite 摘要时：

```bash
python evals/runners/run_offline_eval_suite.py \
  --include-agent \
  --out evals/reports/latest.json \
  --markdown evals/reports/latest.md
```

## 报告内容

`evals/reports/latest.json` 是机器可读证据，包含：

- `version` / `gitSha` / `gitDirty` / `generatedAt`：报告归属的代码状态。
- `rag.recallAt5`、`rag.citationAccuracy`、`rag.mrr`：离线 RAG 检索和引用指标。
- `toolPolicy.passRate`、`toolPolicy.injectionDefensePassRate`：真实 ToolPolicy / sanitizer / taint 用例通过率。
- `injection.blockRate`、`injection.falsePositiveRate`、`injection.bypassRate`、`injection.softGate`：对抗注入 soft gate 指标。
- 可选 `agent`：当使用 `--include-agent` 时，记录 Agent replay report-only 指标与 baseline warning 状态。

`evals/reports/agent-latest.json` 是 Agent Eval 专用报告，包含：

- `agent.toolCallAccuracy`、`agent.toolCallF1`：工具调用集合评分。
- `agent.agentSuccessRate`、`agent.promptRegressionPassRate`：任务成功率与关键词回归。
- `agent.avgLatencyMs`、`agent.p95LatencyMs`、`agent.avgTokens`、`agent.avgCostUsd`：录制运行的延迟与成本摘要。
- `baselineCompare`：对照 `evals/baselines/agent-v2.2.8.json` 的 PASS / WARNING；不会因为指标退化变成 hard fail。

`latest.md` 和 `agent-latest.md` 是给 PR 审查看的 Markdown 摘要；CI 会把四份文件作为 `offline-eval-report` artifact 上传。

## 回归比较

`evals/baselines/v2.2.6.json` 固化 v2.2.6 稳定离线评测基线。`compare_eval_baseline.py` 使用以下规则：

| Metric | Warning | Fail |
| --- | --- | --- |
| RAG Recall@5 | 有下降但不超过 0.02 | 下降超过 0.02 |
| Citation Accuracy | 有下降但不超过 0.02 | 下降超过 0.02 |
| Tool Policy Pass Rate | 无 warning 档 | 任何下降 |
| Injection Bypass Rate | 上升但不超过 0.05 | 上升超过 0.05 |
| Injection False Positive Rate | 上升但不超过 0.05 | 上升超过 0.05 |

`WARNING` 会保留 CI 绿色但提醒审查；`FAIL` 返回非零退出码并阻断 eval job。

Agent baseline 当前只做 report-only warning：

| Metric | Warning |
| --- | --- |
| Tool Call Accuracy / F1 | 低于 baseline |
| Agent Success Rate | 低于 baseline |
| Prompt Regression Pass Rate | 低于 baseline |
| Avg / P95 Latency | 高于 baseline |
| Avg Tokens / Avg Cost | 高于 baseline |

## 更新 baseline

只在明确发布新的稳定版本时更新 baseline。推荐流程：

1. 先合入修复，确保 `latest.json` 的退化比较为 `PASS`。
2. 发布版本时把 `evals/reports/latest.json` 复制为新的 `evals/baselines/vX.Y.Z.json`。
3. Agent 录制样本稳定后，更新 `evals/baselines/agent-vX.Y.Z.json`，但在 v2.4 之前保持 warning-only。
4. 更新 CI 的 `--baseline` 参数和本文档中的 baseline 版本。

不要把带时间戳的 `evals/reports/<suite>-*.json` 提交进仓库；它们仍是本地产物。仓库只跟踪 `latest.json` / `latest.md`、`agent-latest.json` / `agent-latest.md` 和版本化 baseline。
