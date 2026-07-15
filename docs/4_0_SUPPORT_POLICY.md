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

## Candidate and stable release policy

`v4.0.0-rc.1` is superseded by rc.2 and remains only a historical architecture preview. `4.0.0-rc.2` must complete its observation period before stable `4.0.0` can be proposed through a separate promotion PR. This freeze PR does not create a tag or GitHub Release.

## Advisory work

Sidecar image-size optimization and persistent policy audit storage remain advisory. They are not represented as completed. Expanding parity corpora and recording the hybrid performance benchmark are observed in rc.2.
