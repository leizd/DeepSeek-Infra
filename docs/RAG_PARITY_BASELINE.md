# Rust/Python RAG Parity Baseline

This document records the deterministic RAG hot-path parity baseline introduced in 3.2.4. It compares one shared fixture against a pure Python reference contract and the live Rust sidecar HTTP routes.

> **Scope**: query normalization, lexical chunk scoring and ordering, citation formatting, and index validation. This baseline does not call embeddings, a vector database, an external model, or the public internet.

## Baseline result

| Contract | Cases | Required comparison | 3.2.4 result |
| --- | ---: | --- | ---: |
| Query normalization | 12 | Python = Rust = explicit expected value/error | 12/12 |
| Chunk ranking | 10 | Top-K IDs, full order, tie-break, and scores within `1e-6` | 10/10 |
| Citation formatting | 8 | Python = Rust = explicit expected value/error | 8/8 |
| Index validation | 8 | Accept/reject result and stable error category | 8/8 |
| **Overall** | **38** | All cases deterministic and offline | **38/38** |

The shared corpus is [fixtures/rag/parity_cases.json](../fixtures/rag/parity_cases.json). Expected values live only in this implementation-independent fixture; neither the Python nor Rust result is used as the other's golden answer.

## Covered behavior

Normalization cases include English, repeated whitespace, newlines, pure CJK, mixed English/CJK, full-width punctuation, ASCII case, version numbers, symbols, emoji, empty input, and whitespace-only input. Empty queries use the stable `empty_query` category.

Ranking cases cover exact phrases, partial token overlap, title and source matches, short-chunk weighting, CJK exact matching, no-match ordering, equal-score ID tie-breaks, duplicate content with distinct IDs, and Top-K truncation. IDs and order must match exactly; floating-point scores use an absolute tolerance of `1e-6`.

Citation cases preserve the existing runtime format:

```text
docs/cache.md:L12-L18
docs/cache.md:L12
docs/cache.md
```

Invalid ranges use `invalid_line_range`. This milestone intentionally preserves the established colon separator instead of changing the public citation contract during a test-only release.

Index validation compares these stable categories:

- `duplicate_chunk_id`
- `empty_chunk_id`
- `empty_chunk_source`
- `empty_chunk_text`
- `invalid_line_range`
- `invalid_metadata`

Valid metadata and a JSON round-trip case are also required to pass.

## Running locally

Start the Rust Gateway sidecar:

```bash
cargo run --manifest-path rust/Cargo.toml -p deepseek-gateway
```

Run the strict corpus:

```bash
python scripts/check_rag_parity.py \
  --base-url http://127.0.0.1:8787 \
  --strict \
  --report docs/evidence/rag-parity-v4.0.0.json
```

Any mismatch prints the case ID, expected result, Python result, Rust result, and first ranking divergence when applicable. `--strict` exits nonzero on any mismatch.

## CI gate

The independent `ci / rag-parity` job builds and starts only the Rust sidecar image, runs the strict 38-case corpus, uploads `docs/evidence/rag-parity-v4.0.0.json`, prints sidecar logs on failure, and always removes the container.

The job does not enable `DEEPSEEK_RUST_RAG` in the default Compose deployment. Rust RAG remains opt-in, Python remains the default runtime, and the Python coverage gate is 95%.

## Boundaries

The Python reference in `deepseek_infra/infra/rag/local_rag.py` models the same deterministic lexical hot path exposed by the Rust sidecar. The wider Python retrieval pipeline can additionally blend embeddings, vector distance, and BM25 candidate-corpus scores; those layers remain covered by the offline RAG evaluation suite rather than this parity corpus.

Performance thresholds, embedding parity, vector database parity, and default-on decisions remain pre-4.0 work.
