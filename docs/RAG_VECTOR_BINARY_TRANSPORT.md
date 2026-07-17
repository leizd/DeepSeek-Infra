# RAG Vector Ranking Compact Binary Transport

Applicable version: v4.0.3.

DeepSeek Infra 3.9.0 added the explicit, default-disabled compact binary HTTP contract for the existing Rust semantic-cache vector-ranking delegate. Version 3.10.0 keeps that wire format unchanged and allows Python to assemble the same request directly from validated SQLite `f64le-v1` embedding BLOBs. It does not add a delegate, make Rust authoritative, sample or remove Python parity, or change the default JSON contract. Python remains the default runtime, computes the full authoritative ranking, rejects any Rust divergence, and falls back directly to Python on every binary failure.

## Enabling the transport

Both switches are required:

```bash
DEEPSEEK_RUST_RAG=1
DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT=binary
```

`DEEPSEEK_RUST_RAG_VECTOR_TRANSPORT` accepts only `json` and `binary`. Its default is `json`; an invalid value fails closed to `json` and records only a bounded `transportConfigInvalid=true` diagnostic. There is deliberately no `auto` mode. A binary failure never causes a second JSON sidecar request; the same business request falls back directly to the Python ranking.

## Request contract

`POST /rag/vectors/rank-binary` requires:

```text
Content-Type: application/vnd.deepseek.vector-rank.v1+octet-stream
```

All integers and IEEE-754 values are little-endian. Vectors use `f64`, preserving the existing Python/JSON numerical contract.

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 8 | ASCII magic `DSVRNK01` |
| 8 | 4 | dimensions, unsigned `u32` |
| 12 | 4 | candidate count, unsigned `u32` |
| 16 | `8 × d` | query, `d` finite `f64` values |
| following | `8 × n × d` | candidate-major finite `f64` values |

The exact request size is `16 + 8 × dimensions × (candidate_count + 1)`. Trailing bytes are rejected. The current binary limits are 4,096 dimensions, 50,000 candidates, 1,600,000 total scalars, and 12,800,016 request bytes. Checked addition/multiplication runs before allocation or scalar scanning; length validation completes before vector values are inspected. Empty dimensions/candidates, non-finite values, over-limit bodies, length mismatches, and arithmetic overflow all fail without ranking or echoing input.

Stable error codes are `invalid_content_type`, `invalid_binary_magic`, `invalid_binary_header`, `invalid_dimensions`, `invalid_candidate_count`, `payload_length_mismatch`, `payload_too_large`, `non_finite_vector`, `arithmetic_overflow`, and `ranking_failed`. Errors use the existing structured JSON model and a non-2xx status. Clients classify errors by stable code/category, never natural-language text.

## Success response

The success Content-Type is the same binary media type and the response is exactly 24 bytes:

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 8 | ASCII magic `DSVRSP01` |
| 8 | 4 | best index, unsigned `u32`; `0xffffffff` means no positive match |
| 12 | 4 | reserved, must be zero |
| 16 | 8 | finite `f64` similarity in `[0, 1]` |

Python rejects an empty response, wrong Content-Type, wrong size/magic/reserved field, out-of-range index, or non-finite/out-of-range similarity. It then compares the index exactly and the similarity with the production tolerance (`rel=1e-9`, `abs=1e-12`) against the full Python scan. First-match tie behavior is unchanged.

## Python encoding and diagnostics

The list encoder in `deepseek_infra/infra/rust_core/vector_binary.py` uses only `array.array("d")`, `memoryview`-compatible bytes, `struct`, and `sys.byteorder`. It performs one bulk scalar encoding and explicitly byte-swaps on big-endian hosts; NumPy is not a runtime dependency and there is no per-float `struct.pack` loop.

Version 3.10.0 adds `encode_rank_request_from_blobs()`. It validates dimensions, candidate count, scalar count, exact BLOB lengths, finite values, and total payload size before one final body allocation. The query is encoded once; validated candidate buffers are copied into their final offsets with `memoryview`, without building a candidate `list[list[float]]` or parsing candidate floats during assembly. On big-endian hosts, SQLite's canonical little-endian BLOB bytes are already in wire order. Contract tests require this body to be byte-for-byte identical to the list encoder.

## Semantic-cache storage input

The storage contract is documented in [SEMANTIC_CACHE_BINARY_EMBEDDINGS.md](SEMANTIC_CACHE_BINARY_EMBEDDINGS.md). New cache rows retain the existing six-decimal JSON text and dual-write the same normalized values as a contiguous little-endian `f64` BLOB. Python owns SQLite and validates every BLOB before delegation; Rust never opens the database.

For a mixed candidate set, valid BLOB rows are copied directly, while only legacy or invalid-BLOB rows decode their existing JSON and are temporarily encoded to the same little-endian buffer form. The lookup still sends one binary request at most. A BLOB failure falls back to the same row's JSON representation; a binary backend/protocol/parity failure falls back directly to Python and never invokes the JSON Rust endpoint. Full Python parity preferentially scans the decoded `array("d")` values and retains positive-best, first-match tie, zero-vector, and `rel=1e-9`/`abs=1e-12` semantics.

Fixed storage diagnostics are `embeddingStorage=blob|mixed|json`, `blobCandidates`, `legacyCandidates`, and `invalidBlobCandidates`. They expose counts only and never vector values.

Delegate diagnostics retain the six layered timings and add bounded transport metadata:

```text
pythonPreparationUs
serializationUs
transportUs
rustProcessingUs
pythonValidationUs
totalDelegateUs
transportEncoding = json | binary | python
requestPayloadBytes
responsePayloadBytes
```

No diagnostic, exception, log, metric, parity artifact, or benchmark report contains query/candidate values. Metrics reuse `component="rag_vector_rank"`; the additional `vector_rank_transport_total{encoding,outcome}` counter permits only `json|binary` and the fixed outcome allowlist. Dimensions and counts are never labels.

## Parity and evidence

Run a release sidecar and the strict corpus:

```bash
cargo build --release --locked --manifest-path rust/Cargo.toml -p deepseek-gateway
python scripts/check_rag_vector_binary_parity.py \
  --base-url http://127.0.0.1:8787 \
  --strict \
  --report docs/evidence/rag-vector-binary-parity-v4.0.3.json
```

The current [4.0.3 parity evidence](evidence/rag-vector-binary-parity-v4.0.3.json) contains 110 deterministic valid cases and 16 malformed protocol cases. Python, JSON Rust, and binary Rust select the same best index; similarities meet the production tolerance; ties remain first-match; malformed cases retain stable categories; all binary successes are 24 bytes; and no vectors are stored. The original [3.10.0 evidence](evidence/rag-vector-binary-parity-v3.10.0.json) remains historical.

Equivalent dense six-decimal payload sizes from the parity run are:

| Scenario | JSON request | Binary request | Reduction |
| --- | ---: | ---: | ---: |
| 16 × 384 | 61,181 bytes | 52,240 bytes | 14.6% |
| 128 × 768 | 929,336 bytes | 792,592 bytes | 14.7% |
| 1000 × 1536 | 14,397,632 bytes | 12,300,304 bytes | 14.6% |

The release benchmark uses a separately generated but equivalent deterministic dataset; exact JSON byte counts differ slightly while the reduction remains about 14.6%.

## Failure and ownership boundaries

Connection failure, timeout, HTTP error/404, invalid Content-Type, empty/malformed response, invalid index/similarity, or parity divergence all return to the Python result. The binary branch makes one sidecar request and never retries the JSON endpoint. The hybrid E2E creates a fresh Python-owned semantic-cache database, proves dual-write rows, adds one legacy JSON-only row and one corrupt-BLOB/valid-JSON row, performs a real mixed lookup with exactly one binary request and no JSON retry, then stops the sidecar and proves the same result through Python fallback.

Rust still does not own embedding generation, files, OCR, SQLite, vector indexes, cache persistence, retrieval, authorization, or any upstream HTTP. The transport evidence does not justify enabling Rust or binary by default, weakening full parity, or moving any ownership boundary.
