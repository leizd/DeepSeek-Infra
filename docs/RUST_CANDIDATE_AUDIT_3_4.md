# Rust Candidate Audit for 3.4.0

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


Applicable version: v3.4.0.

## Decision

3.4.0 moves semantic-cache batch vector ranking into the existing `deepseek-rag` Rust crate and exposes it through `POST /rag/vectors/rank`. The Python semantic-cache boundary delegates only when `DEEPSEEK_RUST_RAG=1`; otherwise it uses the original Python implementation. Unreachable, non-200, malformed, non-finite, or out-of-range responses also fall back to Python.

This is an incremental hot-path migration, not a runtime-default change. ADR-0040 remains authoritative: Python FastAPI is the default runtime and all Rust delegates remain opt-in.

## Candidate matrix

| Candidate | CPU potential | Boundary stability | Migration risk | 3.4.0 decision |
| --- | --- | --- | --- | --- |
| Semantic-cache batch vector ranking | High for larger candidate sets or dimensions | High: vectors in, best index and similarity out | Low | **Implemented** in `deepseek-rag::vector` |
| Local RAG dense-vector scoring | High | Medium: currently coupled to SQLite and optional `sqlite-vec` candidate selection | Medium | Keep database access in Python; reuse the new pure vector primitive before expanding |
| BM25 and lexical ranking | Medium to high for large corpora | High; deterministic parity corpus already exists | Medium | Existing Rust chunk scoring already covers the opt-in lexical hot path; expand only with new corpus-scale evidence |
| Evaluation metric aggregation | Low runtime value | High | Low | Defer: offline evaluation is not a user-facing latency bottleneck |
| OCR cleanup and formula heuristics | Medium | Low: many Unicode and regex edge cases | High | Defer until a versioned parity corpus exists |
| Gateway scheduler and backpressure | Potentially high | Low: thread coordination and mutable queue state | High | Keep Python-owned for now |
| Streaming chat transport | Potentially high | Architecture-owned | High | Do not migrate in 3.4.0; ADR-0040 assigns 4.0 streaming to Python |
| MCP real tool execution | Not primarily CPU-bound | Architecture-owned and side-effectful | High | Do not migrate; ADR-0040 keeps execution in Python |

## Ownership boundary

Rust owns:

- clamped dot-product similarity for normalized embeddings;
- stable first-candidate tie behavior;
- batch selection of the best positive-similarity candidate;
- the `/rag/vectors/rank` request and response contract.

Python retains:

- SQLite access and candidate ordering;
- cache version, model, and scope filtering;
- TTL and attachment exact-match rules;
- exact prompt-hash selection;
- hit thresholds, response decoding, hit mutation, and diagnostics;
- fallback execution and release defaults.

## Verification contract

- Rust unit tests cover clamping, empty vectors, zero similarity, and stable ties.
- Rust Gateway tests cover the HTTP endpoint.
- Python client tests reject booleans, out-of-range indexes, invalid similarity types, and values outside `[0, 1]`.
- Semantic-cache integration tests cover the Rust result and sidecar-failure Python fallback.
- `scripts/smoke_rust_sidecar.py` checks the endpoint without an API key or network dependency.

## Performance caution

The sidecar call serializes vectors as JSON, so small candidate sets may be faster on the in-process Python path. The opt-in design lets deployments evaluate their own cache size and embedding dimensions before enabling it. A future default-on proposal requires measured end-to-end latency and throughput evidence, not only a faster inner loop.
