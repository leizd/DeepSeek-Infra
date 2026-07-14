# Semantic Cache ONNX Evidence

- Version: 3.9.0
- Commit: 0b6b69b9
- Status: PASS
- Generated: 2026-07-14T08:33:55Z
- ONNX Available: False

## Hash Embedding (zero-dependency default)

| Metric | Value |
| --- | --- |
| Exact Hit Rate | 1.0 |
| Paraphrase Hit Rate | 0.0 |
| Unrelated False Hit Rate | 0.0 |
| Provider | hash |
| Dimensions | 64 |

## ONNX Embedding (optional neural embedding)

ONNX provider not available; install `requirements-rag.txt` and provide model/tokenizer.

## Decision

ONNX remains optional; hash embedding is zero-dependency default
