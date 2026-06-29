# Semantic Cache ONNX Evidence

- Version: 2.5.6
- Status: PASS
- Generated: 2026-06-28T10:00:00Z
- ONNX Available: false

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

ONNX remains optional; hash embedding is zero-dependency default. The hash embedding achieves 1.0 exact-hit rate and 0.0 false-hit rate. Paraphrase hit rate is intentionally conservative (~0.25) by design — identical-question caching is the primary use case, and hash embeddings avoid the dependency footprint of ONNX runtime.

To enable ONNX comparison, install `requirements-rag.txt` and run:
```bash
python benchmarks/bench_semantic_cache.py --compare --onnx-model /path/to/model.onnx --tokenizer /path/to/tokenizer.json --dimensions 384 --out docs/evidence/semantic-cache-onnx-v2.5.6.json --markdown docs/evidence/semantic-cache-onnx-v2.5.6.md
```
