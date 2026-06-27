# AI Runtime Evaluation Harness

适用版本：v2.2.8。

这套 harness 对 DeepSeek Infra 的核心能力线做**自动化回归评测**（全部可离线执行，无需 API Key）。v2.2.7 已把 RAG / Tool Policy / Prompt Injection adversarial eval 整理成统一报告和 v2.2.6 baseline compare；v2.2.8 继续把 Agent Eval 从样例打分升级为稳定 JSONL 录制、归一化回放、`agent-latest` 报告和 report-only baseline 对比。Agent 指标低于建议阈值只 warning，结构错误才失败，硬门禁仍留到 v2.4。

| 指标族 | 含义 | 由谁产出 |
| --- | --- | --- |
| **Golden Questions** | 答案明确落在某个仓库文档里的标注问题集 | `golden/rag_questions.jsonl` |
| **RAG Recall@K** | 期望来源文档是否进入检索 top-K | `run_rag_eval.py`（真实离线检索） |
| **Citation Accuracy** | top 来源正确 **且** 期望关键词在检索片段里 grounded | `run_rag_eval.py` |
| **Prompt Regression** | 关键词覆盖率是否跌破阈值（回归保护） | RAG / Agent runner |
| **Tool Call Accuracy** | 实际工具调用集合与期望计划是否一致（精确 / F1） | `run_agent_eval.py` |
| **Agent Success Rate** | 任务最终答案是否满足成功标准且未失败 | `run_agent_eval.py` |
| **Tool Policy Pass Rate** | 安全闸门对 SSRF / 路径越界 / 密钥外泄 / 越权等判定是否符合预期 | `run_tool_eval.py`（离线重放真实闸门） |
| **Injection Defense Pass Rate** | 注入指令被检出 / 清洗、良性内容不误伤的比例 | `run_tool_eval.py` |
| **Block / Bypass / False Positive Rate** | 小型对抗注入语料库的拦截率、绕过率、误伤率 | `run_injection_adversarial.py`（soft gate） |
| **Latency Benchmark** | 平均 / P50 / P95 延迟 | 全部 runner |
| **Cost Benchmark** | 平均 token 与 USD 成本（按模型定价或录制估算） | `run_agent_eval.py` |

## 目录

```
evals/
  golden/
    rag_questions.jsonl                         # {id, question, expected_source, expected_keywords}
    agent_tasks.jsonl                           # {id, task, expected_tools, expected_keywords}
    agent_predictions.sample.jsonl              # 旧版示例 predictions
    agent_predictions.v2.2.8.sample.jsonl       # v2.2.8 稳定录制 / 回放样例
    tool_policy_cases.jsonl                     # 安全闸门 / 注入防御标注用例
    injection_adversarial.jsonl                 # 对抗注入小语料
  schemas/
    agent_prediction.schema.json                # Agent prediction JSONL 结构规范
  runners/
    run_offline_eval_suite.py
    compare_eval_baseline.py
    run_rag_eval.py
    run_agent_eval.py
    run_tool_eval.py
    run_injection_adversarial.py
  baselines/
    v2.2.6.json                                 # RAG / Tool Policy / Injection 回归基线
    agent-v2.2.8.json                           # Agent Eval report-only 基线
  reports/
    latest.json                                 # 统一离线报告（入库）
    latest.md                                   # PR 审查摘要（入库）
    agent-latest.json                           # Agent Eval report-only 报告（入库）
    agent-latest.md                             # Agent Eval Markdown 摘要（入库）
    <suite>-<timestamp>.json                    # 单项 runner 本地产物（gitignore）
```

评分核心是纯函数库 `deepseek_infra/infra/evaluation/harness.py`（无 I/O、可单测）；Agent 录制去噪在 `deepseek_infra/infra/evaluation/agent_recording.py`，runner 只做编排与报告。

## 运行

```bash
# 统一离线评测套件：运行 RAG / Tool Policy / Prompt Injection adversarial eval，
# 写出 evals/reports/latest.json 与 latest.md。
python evals/runners/run_offline_eval_suite.py --out evals/reports/latest.json --markdown evals/reports/latest.md

# 可选把 Agent Eval 聚合进统一报告。Agent 仍是 report-only，不影响主线硬门禁。
python evals/runners/run_offline_eval_suite.py --include-agent --out evals/reports/latest.json --markdown evals/reports/latest.md

# 与 v2.2.6 baseline 比较，输出 PASS / WARNING / FAIL。
python evals/runners/compare_eval_baseline.py --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json

# Agent 录制回放：结构错误失败，指标低于建议阈值只 WARNING。
python evals/runners/run_agent_eval.py --report-dir evals/reports --report-only

# RAG 召回 / 引用准确率：临时本地 RAG 索引，不动真实 .local-rag。
PYTHONHASHSEED=0 python evals/runners/run_rag_eval.py

# 安全闸门 / 注入防御：离线重放真实 ToolPolicy、sanitizer 与 taint scanner。
python evals/runners/run_tool_eval.py

# 对抗注入小语料：v2.2.6 起 soft gate；加 --strict 可升级为硬失败。
python evals/runners/run_injection_adversarial.py --no-report
```

常用参数：`--golden`、`--k`（RAG）、`--predictions`（Agent）、`--json`（机器可读）、`--no-report`（不落盘）、`--include-agent`（suite 可选聚合 Agent）。

CI 口径：

- PR 必跑 `python evals/runners/run_offline_eval_suite.py --out evals/reports/latest.json --markdown evals/reports/latest.md`，生成统一 JSON / Markdown 报告。
- PR 必跑 `python evals/runners/compare_eval_baseline.py --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json`，无退化为 PASS，轻微退化为 WARNING，严重退化为 FAIL。
- CI 跑 `python evals/runners/run_agent_eval.py --report-dir evals/reports --report-only`，生成 Agent replay 报告；指标 warning 不会阻断 CI。
- CI 上传 `offline-eval-report` artifact，包含 `latest.json`、`latest.md`、`agent-latest.json` 与 `agent-latest.md`。
- `run_injection_adversarial.py` 保持 v2.2.6 起的 soft gate 语义：未达阈值只 warning；加 `--strict` 可升级为硬失败（v2.3 路径）。

## 输出示例

当前 golden 基线（`run_rag_eval.py` 会把 `PYTHONHASHSEED` 钉成 `0`，所以逐次可复现）：

```
=== Eval Report · rag ===
Cases: 6
RAG Recall@5: 1.000
RAG MRR: 1.000
Citation Accuracy: 0.833
Keyword Coverage: 1.000
Prompt Regression Pass: 1.000
Avg Latency: 39.4ms
P95 Latency: 49.5ms
```

```
=== Agent Eval Report ===
Status: PASS
Cases: 6
Tool Call Accuracy: 1.000
Tool Call F1: 1.000
Agent Success Rate: 1.000
Prompt Regression Pass: 1.000
Avg Latency: 4050.0ms
P95 Latency: 6775.0ms
Avg Tokens: 4610.0
Avg Cost: $0.002683
Baseline Compare: PASS
```

```
=== Eval Report · tool-policy ===
Cases: 26
Tool Policy Pass Rate: 1.000
Prompt Injection Defense Pass: 1.000
Avg Latency: 0.0ms
P95 Latency: 0.1ms
```

```
=== Eval Report · injection-adversarial ===
Cases: 30
Injection Block Rate: 1.000
False Positive Rate: 0.000
Bypass Rate: 0.000
Soft Gate: PASS
```

`run_tool_eval.py` 在判定不符时退出码为 1 并逐条列出错判用例，可直接当回归门禁；新增攻击样本只需往 `tool_policy_cases.jsonl` 追加一行。

`run_injection_adversarial.py` 自 v2.2.6 起接入版本化阈值 soft gate（`blockRate>=0.85`、`falsePositiveRate<=0.10`、`bypassRate<=0.15`）：未达标打印 `SOFT GATE: WARNING` 但仍 `exit 0`，CI 不中断；加 `--strict` 把未达标升级为 `exit 1`，是 v2.3 硬门禁的毕业路径。

## 录制真实 predictions

`agent_predictions.v2.2.8.sample.jsonl` 是稳定录制示例。真实运行录制每条 prediction 时保持这些核心字段：

```json
{"id":"agent_001","task":"Summarize uploaded docs and cite sources","model":"deepseek-v4-pro","tools":["file_search","file_read"],"final":"answer text","status":"succeeded","latencyMs":4020,"usage":{"inputTokens":3200,"outputTokens":900,"estimatedCostUsd":0.0031},"trace":{"agentCount":4,"retryCount":0,"toolErrorCount":0}}
```

`agent_recording.py` 会剔除 `runId` / `traceId` / `spanId` / timestamp 等非确定字段，规范化工具名、usage、latency 和 trace 摘要。最终答案只参与关键词覆盖和成功状态判断，不做全文精确匹配。完整说明见 [docs/AGENT_EVAL.md](../docs/AGENT_EVAL.md)。
