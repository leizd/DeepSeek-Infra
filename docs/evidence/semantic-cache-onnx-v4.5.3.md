# Semantic Cache ONNX Evidence

- Version: 4.5.3
- Commit: 6f2cc6c
- Status: PASS
- Generated: 2026-08-17T00:39:33Z
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
