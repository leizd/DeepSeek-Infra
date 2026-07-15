# Pre-4.0 Quality Baseline

This document records the verified `3.10.0` baseline from which `4.0.0-rc.2` is frozen. `v4.0.0-rc.1` remains historical and is not an eligible stable promotion source.

## Frozen baseline

- Python remains authoritative and the default deployment remains Python-only.
- All Rust delegates and compact binary vector transport remain explicit opt-ins with direct Python fallback.
- JSON semantic-cache embeddings remain readable and new rows retain the JSON column while dual-writing the compatible `f64le-v1` BLOB.
- Python owns SQLite, files, OCR, embeddings, indexing, upstream HTTP, credentials, retries, Gateway streaming, MCP transport/session, and real tool execution.
- The complete parity corpora are Gateway 68, MCP 105, RAG 38, RAG document 125, and vector binary 110 valid plus 16 malformed.
- Rust workspace quality includes fmt, clippy with warnings denied, workspace tests, Docker smoke, and measured line coverage of at least 80%.
- Python CI continues to enforce 95%; the rc.2 freeze requires two complete runs at or above 95.20%.

Two complete Python runs each measured 95.2317% combined statement/branch coverage (2581 tests and 58 subtests per run). Rust 1.85 with `cargo-llvm-cov 0.6.21` measured 80.4329% line coverage (3716/4620) across 172 workspace tests. HIGH-risk Python coverage debt stayed exactly level with the verified 3.9/3.10 baseline at 201 missing statements and 198 missing branches. The machine-readable results live in `docs/evidence/python-coverage-stability-v4.0.0-rc.2.json` and `docs/evidence/rust-coverage-v4.0.0-rc.2.json`.

## Stable-release boundary

The rc.2 freeze proves the supported Python-first hybrid contract. It does not enable Rust by default, remove JSON embeddings, remove Python fallback, make ranking Rust-primary, tag a commit, publish a GitHub Release, or publish stable `4.0.0`. Stable promotion requires a separate PR after observation.
