# AI Runtime Evaluation Harness

适用版本：v2.1.7。

这套 harness 对 DeepSeek Infra 的三条核心能力线做**自动化回归评测**（全部可离线执行，无需 API Key）：

| 指标族 | 含义 | 由谁产出 |
| --- | --- | --- |
| **Golden Questions** | 答案明确落在某个仓库文档里的标注问题集 | `golden/rag_questions.jsonl` |
| **RAG Recall@K** | 期望来源文档是否进入检索 top-K | `run_rag_eval.py`（真实离线检索）|
| **Citation Accuracy** | top 来源正确 **且** 期望关键词在检索片段里 grounded | `run_rag_eval.py` |
| **Prompt Regression** | 关键词覆盖率是否跌破阈值（回归保护）| RAG / Agent runner |
| **Tool Call Accuracy** | 实际工具调用集合与期望计划是否一致（精确/ F1）| `run_agent_eval.py` |
| **Agent Success Rate** | 任务最终答案是否满足成功标准且未失败 | `run_agent_eval.py` |
| **Tool Policy Pass Rate** | 安全闸门对 SSRF / 路径越界 / 密钥外泄 / 越权等判定是否符合预期 | `run_tool_eval.py`（离线重放真实闸门）|
| **Injection Defense Pass Rate** | 注入指令被检出 / 清洗、良性内容不误伤的比例 | `run_tool_eval.py` |
| **Latency Benchmark** | 平均 / P50 / P95 延迟 | 全部 runner |
| **Cost Benchmark** | 平均 token 与 USD 成本（按模型定价）| `run_agent_eval.py` |

## 目录

```
evals/
  golden/
    rag_questions.jsonl              # {id, question, expected_source, expected_keywords}
    agent_tasks.jsonl               # {id, task, expected_tools, expected_keywords}
    agent_predictions.sample.jsonl  # 录制的 agent 输出，离线打分用
    tool_policy_cases.jsonl         # 安全闸门 / 注入防御标注用例（policy | sanitize | taint）
  runners/
    run_rag_eval.py
    run_agent_eval.py
    run_tool_eval.py
  reports/                          # 生成的 JSON 报告（.gitignore）
```

评分核心是纯函数库 `deepseek_infra/infra/evaluation/harness.py`（无 I/O、可单测，见
`tests/test_eval_harness.py`）；runner 只做编排与报告。

## 运行

```bash
# RAG 召回 / 引用准确率：把每个 expected_source 文档索引进一个临时本地 RAG 索引
# （hash embedding + BM25，离线、无需 API Key，不动你真实的 .local-rag），逐题检索打分。
python evals/runners/run_rag_eval.py

# Agent / 工具调用：把录制的 predictions 与 golden 任务按 id 关联打分。
python evals/runners/run_agent_eval.py

# 安全闸门 / 注入防御：把标注用例喂给真实的 ToolPolicy.evaluate、
# sanitize_tool_result 与 context_taint.scan_text，离线断言判定结果。
python evals/runners/run_tool_eval.py
```

常用参数：`--golden`、`--k`（RAG）、`--predictions`（agent）、`--json`（机器可读）、
`--no-report`（不落盘）。

## 输出示例

当前 golden 基线（`run_rag_eval.py` 会把 `PYTHONHASHSEED` 钉成 `0` 再执行，所以**逐次可复现**——
检索在近似打平的文档上对 BM25 浮点和取整时本会随哈希种子轻微抖动，钉种子后消除）：

```
=== Eval Report · rag ===
Cases: 6
RAG Recall@5: 1.000
RAG MRR: 0.917
Citation Accuracy: 0.833
Keyword Coverage: 1.000
Prompt Regression Pass: 1.000
Avg Latency: 20.4ms
P95 Latency: 21.3ms
```

```
=== Eval Report · agent ===
Cases: 6
Tool Call Accuracy: 0.833
Tool Call F1: 0.833
Agent Success Rate: 0.833
Prompt Regression Pass: 0.833
Avg Latency: 4.05s
P95 Latency: 6.78s
Avg Token Cost: 4.6k
Avg Cost: $0.003121
```

```
=== Eval Report · tool-policy ===
Cases: 26
Tool Policy Pass Rate: 1.000
Prompt Injection Defense Pass: 1.000
Avg Latency: 0.0ms
P95 Latency: 0.1ms
```

`run_tool_eval.py` 在判定不符时退出码为 1 并逐条列出错判用例，可直接当回归门禁；
新增攻击样本只需往 `tool_policy_cases.jsonl` 追加一行。

## 录制真实 predictions

`agent_predictions.sample.jsonl` 是示例。要把它变成真正的回归门禁，用真实运行录制每条
`{id, answer, tools:[...], usage:{prompt_tokens,completion_tokens}, model, latencyMs, failed?}`，
覆盖 `--predictions` 即可。RAG runner 本身就是 live 的（对仓库文档做真实检索），可直接进 CI。
