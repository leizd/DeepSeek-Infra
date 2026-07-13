# Offline Eval Report

- Version: 4.0.0-rc.1
- Git SHA: 3be60b68 (dirty)
- Generated: 2026-07-13T01:21:05Z
- Overall: PASS

| Suite | Metric | Value | Status |
| --- | --- | ---: | --- |
| RAG | Recall@5 | 1.0000 | PASS |
| RAG | Citation Accuracy | 0.8333 | PASS |
| RAG | MRR | 0.9167 | PASS |
| Tool Policy | Pass Rate | 1.0000 | PASS |
| Tool Policy | Injection Defense Pass Rate | 1.0000 | PASS |
| Injection | Block Rate | 1.0000 | PASS |
| Injection | False Positive Rate | 0.0000 | PASS |
| Injection | Bypass Rate | 0.0000 | PASS |
| Agent | Tool Call Accuracy | 1.0000 | PASS |
| Agent | Agent Success Rate | 1.0000 | PASS |
| Agent | Prompt Regression Pass | 1.0000 | PASS |

## Dataset Sizes

- RAG: 6 cases
- Tool Policy: 26 cases
- Injection adversarial: 30 cases
- Agent replay: 6 cases

## Regression Compare

```bash
python evals/runners/compare_eval_baseline.py --strict --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json --agent-baseline evals/baselines/agent-v2.2.8.json --out evals/reports/baseline-compare-latest.json
```
