# 4.0 Hybrid Runtime Support Policy

DeepSeek Infra 4.0 is a Python-first hybrid runtime.

## Supported contract

- Python is the default and authoritative runtime, and default Compose is Python-only.
- The Rust sidecar is an officially supported optional deployment component.
- All Rust delegates and binary vector transport are disabled by default.
- Python fallback is supported throughout the entire 4.x line and cannot be removed before 5.0.0.
- Python owns Gateway streaming, upstream HTTP, credentials, retries, MCP transport/session/real tool execution, file reading, OCR, embeddings, SQLite, indexes, retrieval, and business state.
- Rust may optionally prepare Gateway requests, prepare MCP protocol envelopes, evaluate Tool Policy, rank RAG vectors, prepare parsed RAG documents, and serve the explicit compact binary vector protocol.
- The 3.10 JSON/BLOB semantic-cache path remains explicit opt-in for binary transport. `embedding TEXT` remains part of the compatibility contract.
- Rust-primary ranking is not enabled.

The internal stable endpoint inventory is frozen in `release/4_0_protocol_contract.json`. Binary magic `DSVRNK01` and `DSVRSP01` is stable for 4.x.

## Stable release policy

`v4.0.0-rc.1` was superseded by rc.2 and remains only a historical architecture preview. Stable `4.0.0` was promoted from the validated rc.2 candidate. Patch release `4.0.1` hardens frontend CSP, credential lifetime, uploads, offline resources, navigation semantics, and browser testing. Patch release `4.0.2` adds an isolated React/TypeScript/Vite preview and typed stream-domain foundation while keeping the legacy frontend as the default. Neither patch changes runtime behavior, defaults, ownership, or the frozen protocol contract.

## Advisory work

Sidecar image-size optimization and persistent policy audit storage remain advisory. They are not represented as completed. Expanded parity corpora and the hybrid performance benchmark were observed during rc.2 qualification and remain part of stable evidence.
