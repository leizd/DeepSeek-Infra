# Offline Eval Report

- Version: 2.3.3
- Git SHA: 1001084 (dirty)
- Generated: 2026-06-27T10:51:29Z
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

## Dataset Sizes

- RAG: 6 cases
- Tool Policy: 26 cases
- Injection adversarial: 30 cases

## Regression Compare

```bash
python evals/runners/compare_eval_baseline.py --baseline evals/baselines/v2.2.6.json --current evals/reports/latest.json
```
