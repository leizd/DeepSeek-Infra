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

Current measured coverage values, test counts, exact commit, and PASS status live in `docs/evidence/python-coverage-stability-v4.0.0-rc.2.json` and `docs/evidence/rust-coverage-v4.0.0-rc.2.json`. They are populated only by complete executable runs.

## Stable-release boundary

The rc.2 freeze proves the supported Python-first hybrid contract. It does not enable Rust by default, remove JSON embeddings, remove Python fallback, make ranking Rust-primary, tag a commit, publish a GitHub Release, or publish stable `4.0.0`. Stable promotion requires a separate PR after observation.
