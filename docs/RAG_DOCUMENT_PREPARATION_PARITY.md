# RAG Document Preparation Parity

DeepSeek Infra 3.7.0 adds an optional Rust delegate for deterministic preparation of text that Python has already parsed. The stable development line remains 3.x. The published `v4.0.0-rc.1` is a historical architecture preview, not the active release target.

## Ownership boundary

Python continues to own uploads, path and filename security, MIME detection, PDF/Office/HTML/image/archive parsing, OCR, caches, embeddings, persistence, SQLite and vector indexes, ingestion scheduling, authorization, workspaces, projects, deletion/update transactions, retrieval, context assembly, and citations. Rust cannot read files, receive filesystem paths or raw file bytes, write an index, compute embeddings, or execute a query.

Rust receives only:

```text
already parsed text + allowlisted non-sensitive metadata + chunk configuration
-> normalized document and chunk descriptors, or a stable error code
```

The endpoint is `POST /rag/documents/prepare`. It is independently controlled by `DEEPSEEK_RUST_RAG_DOCUMENT_PREP`, which defaults to `0`; it does not change the existing `DEEPSEEK_RUST_RAG` hot-path flag. Default Compose remains Python-only.

## Python compatibility contract

The implementation reproduces the established Python behavior:

- CRLF and CR become LF, NUL characters are removed, trailing whitespace is removed per line, and the full text is stripped.
- The default chunk window is 6,000 Unicode characters with 400 characters of overlap.
- A newline boundary in the latter half of a window is preferred; empty stripped chunks are omitted.
- `start` and `end` are Python string character indexes, not UTF-8 byte offsets. CJK, emoji, non-BMP characters, combining sequences, and mixed-language text are parity cases.
- Chunk hashes use the existing BLAKE2b-96 lineage hash. The document hash uses the existing ordered `index:chunkHash\0` rule.
- Chunk IDs are deterministic: `documentId:index:chunkHash`. The Python-owned persisted file/index ID remains unchanged.
- Only `displayName`, `sourceType`, and `kind` metadata can cross the boundary; unknown fields are dropped and path/credential-like fields are rejected.

Stable validation categories include `invalid_request`, `invalid_document_id`, `invalid_text`, `invalid_metadata`, `invalid_chunk_size`, `invalid_chunk_overlap`, `chunk_overlap_too_large`, `document_too_large`, `request_too_large`, and `nesting_limit_exceeded`. Natural-language error wording is not a parity requirement.

## Defensive adoption and fallback

Python computes the local preparation first. A Rust result is adopted only when it is safely JSON serializable and exactly matches Python after Python verifies the document ID, metadata, chunk count, contiguous indexes, unique IDs, character offsets, chunk text, overlap behavior, content hashes, document hash, and absence of sensitive fields. A malformed response, timeout, connection or HTTP failure, empty/non-object body, injected field, changed metadata, duplicate ID, offset mismatch, hash mismatch, or semantic difference uses the already-computed Python result.

Deterministic user/configuration errors remain their stable error category and are not disguised as backend fallback. Diagnostics contain only a document-ID hash, counts, chunk configuration, runtime, fallback state/reason, and latency; document/chunk text, paths, credentials, and private metadata are excluded.

## Shared corpus and gates

The shared fixture at `fixtures/rag/document_preparation_cases.json` contains 125 deterministic cases. Run the live-sidecar gate with:

```bash
python scripts/check_rag_document_preparation_parity.py \
  --base-url http://127.0.0.1:8787 \
  --strict \
  --report docs/evidence/rag-document-preparation-parity-v4.0.1.json
```

The report contains only redacted summaries and fingerprints. The independent `rag-document-preparation-parity` CI job uploads it even when the comparison fails. Existing Gateway, MCP, RAG, Policy, Docker, hybrid E2E, release, and security gates remain in place.

The informational five-profile benchmark is:

```bash
python benchmarks/bench_rag_document_preparation.py \
  --base-url http://127.0.0.1:8787 \
  --out docs/evidence/rag-document-preparation-benchmark-v3.7.0.json
```

It reports Python latency, Rust sidecar round-trip latency, serialization overhead, input character count, and chunk count. It does not justify or change the default-disabled setting.

## Hybrid E2E proof

The existing hybrid runtime smoke calls the real Python file ingestion function with an offline text fixture. Python parses the bytes, Rust prepares the parsed text, and Python persists and reads the chunks. The probe verifies that the Rust payload contains no path, bytes, or credential fields. It then stops the sidecar, ingests the same document through Python fallback, and requires the same semantic chunk fingerprint.

## Non-goals

This milestone does not add Rust file I/O, PDF/Office/HTML parsing, OCR, archive extraction, embeddings, vector databases, SQLite ownership, ingestion scheduling, chunk persistence, document transactions, retrieval/context assembly, a default-on Rust flag, a default Rust deployment, a new 4.0 RC, stable 4.0.0, a tag, or a GitHub Release.
