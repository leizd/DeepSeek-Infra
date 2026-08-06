# Offline Eval Report

- Version: 4.4.3
- Git SHA: b6d3a030cd5a96cc2da5031da1af62d2b70176a9 (dirty)
- Generated: 2026-08-05T11:19:37Z
- Overall: PASS

| Suite | Metric | Value | Status |
| --- | --- | ---: | --- |
| RAG | Recall@5 | 1.0000 | PASS |
| RAG | Citation Accuracy | 0.8333 | PASS |
| RAG | MRR | 0.8750 | PASS |
| Tool Policy | Pass Rate | 1.0000 | PASS |
| Tool Policy | Injection Defense Pass Rate | 1.0000 | PASS |
| Injection | Block Rate | 1.0000 | PASS |
| Injection | False Positive Rate | 0.0000 | PASS |
| Injection | Bypass Rate | 0.0000 | PASS |

## Dataset Sizes

- RAG: 6 cases
- Tool Policy: 26 cases
- Injection adversarial: 30 cases

## Regression Compare

```bash
python evals/runners/compare_eval_baseline.py --strict --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json --agent-baseline evals/baselines/agent-v2.2.8.json --out evals/reports/baseline-compare-latest.json
```
