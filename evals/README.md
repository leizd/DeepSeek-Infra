# AI Runtime Evaluation Harness

适用版本：v2.2.6。

这套 harness 对 DeepSeek Infra 的核心能力线做**自动化回归评测**（全部可离线执行，无需 API Key）。v2.2.6 CI 必过项包含稳定、无需录制输入的 `run_rag_eval.py` 与 `run_tool_eval.py`；`run_injection_adversarial.py` 输出对抗注入指标并对照版本化阈值做 **soft gate**（未达标只 warning，`--strict` 升级为硬失败）；`run_agent_eval.py` 继续提供离线样例打分，等录制数据完全稳定后再加入必过门禁。

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
| **Block / Bypass / False Positive Rate** | 小型对抗注入语料库的拦截率、绕过率、误伤率 | `run_injection_adversarial.py`（soft gate） |
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
    injection_adversarial.jsonl     # 对抗注入小语料（中文 / 英文 / Base64 / Markdown hidden / 多轮）
  runners/
    run_rag_eval.py
    run_agent_eval.py
    run_tool_eval.py
    run_injection_adversarial.py
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

# 对抗注入小语料：输出 block_rate / false_positive_rate / bypass_rate。
# v2.2.6 起 soft gate：未达标只 warning、仍 exit 0；加 --strict 升级为硬失败。
python evals/runners/run_injection_adversarial.py
```

常用参数：`--golden`、`--k`（RAG）、`--predictions`（agent）、`--json`（机器可读）、
`--no-report`（不落盘）。

CI 口径：

- PR 必跑 `python evals/runners/run_rag_eval.py`，失败时 CI 红。
- PR 必跑 `python evals/runners/run_tool_eval.py`，覆盖 Tool Policy Pass Rate 与 Prompt Injection Defense Pass Rate，失败时 CI 红。
- PR 跑 `python evals/runners/run_injection_adversarial.py --no-report`，v2.2.6 起为 soft gate：未达阈值只 warning，不阻断 CI；加 `--strict` 可升级为硬失败（v2.3 路径）。
- `python evals/runners/run_agent_eval.py` 目前作为离线样例回归，不属于 v2.2.6 必过项。

## 输出示例

当前 golden 基线（`run_rag_eval.py` 会把 `PYTHONHASHSEED` 钉成 `0` 再执行，所以**逐次可复现**——
检索在近似打平的文档上对 BM25 浮点和取整时本会随哈希种子轻微抖动，钉种子后消除）：

```
=== Eval Report · rag ===
Cases: 6
RAG Recall@5: 1.000
RAG MRR: 1.000
Citation Accuracy: 1.000
Keyword Coverage: 1.000
Prompt Regression Pass: 1.000
Avg Latency: 39.4ms
P95 Latency: 49.5ms
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

```
=== Eval Report · injection-adversarial ===
Cases: 30
Injection Block Rate: 1.000
False Positive Rate: 0.000
Bypass Rate: 0.000
Avg Latency: 0.0ms
P95 Latency: 0.1ms

Soft Gate: PASS (all thresholds met)
  - blockRate: 1.000 >= 0.85 [PASS]
  - falsePositiveRate: 0.000 <= 0.1 [PASS]
  - bypassRate: 0.000 <= 0.15 [PASS]
```

`run_tool_eval.py` 在判定不符时退出码为 1 并逐条列出错判用例，可直接当回归门禁；
新增攻击样本只需往 `tool_policy_cases.jsonl` 追加一行。

`run_injection_adversarial.py` 自 v2.2.6 起接入版本化阈值 soft gate（`blockRate>=0.85`、
`falsePositiveRate<=0.10`、`bypassRate<=0.15`）：未达标打印 `SOFT GATE: WARNING` 但仍
`exit 0`，CI 不中断；加 `--strict` 把未达标升级为 `exit 1`，是 v2.3 硬门禁的毕业路径。

## 录制真实 predictions

`agent_predictions.sample.jsonl` 是示例。要把它变成真正的回归门禁，用真实运行录制每条
`{id, answer, tools:[...], usage:{prompt_tokens,completion_tokens}, model, latencyMs, failed?}`，
覆盖 `--predictions` 即可。RAG runner 本身就是 live 的（对仓库文档做真实检索），可直接进 CI。
