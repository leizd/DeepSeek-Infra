# Semantic Cache Binary Embedding Storage

Applicable version: v4.0.0-rc.2.

DeepSeek Infra 3.10.0 keeps the existing semantic-cache JSON embedding contract and adds a compatible little-endian `f64` BLOB representation for the explicit Rust binary-ranking path. Python still owns SQLite, embedding generation, cache semantics, the complete authoritative ranking, parity, and fallback. Rust never opens the database, Rust-primary is not enabled, every Rust flag remains off by default, the vector transport still defaults to JSON, and default Compose remains Python-only.

## Schema and rollback compatibility

`semantic_cache_items` retains:

```sql
embedding TEXT NOT NULL
```

and adds:

```sql
embedding_blob BLOB
embedding_dimensions INTEGER NOT NULL DEFAULT 0
embedding_format TEXT NOT NULL DEFAULT ''
```

The only supported non-empty format is `f64le-v1`. `initialize_schema()` uses `PRAGMA table_info` and idempotent `ALTER TABLE ... ADD COLUMN` statements only. It does not select cache rows, parse JSON, hold a migration transaction, or rewrite existing records. An old JSON-only database therefore starts immediately, and an older application can still read rows written by 3.10.0 because the required JSON column is unchanged.

Cache namespaces remain `<SEMANTIC_CACHE_VERSION>:<embedding provider>:<dimensions>`. The stored dimension must be positive, within the current 4,096-dimension pipeline bound, equal to the query/pipeline dimension, and have exactly `dimensions * 8` BLOB bytes.

## Canonical dual-write

Every new insert or update follows one normalization pass:

```text
raw embedding
  -> round(float(value), 6)
  -> JSON TEXT and f64le BLOB from the same normalized values
```

The JSON spelling and numerical semantics remain the contract established before 3.10.0. The BLOB is only a second representation:

```text
dimension 0: IEEE-754 f64, little-endian, 8 bytes
dimension 1: IEEE-754 f64, little-endian, 8 bytes
...
```

Encoding rejects empty/over-limit dimensions, dimension mismatch, non-finite values, an unexpected host byte order, or a non-8-byte native double. Big-endian hosts explicitly byte-swap. Diagnostics and exceptions contain stable categories only, never vector contents.

## Mixed databases and corruption fallback

For explicit binary transport, each candidate is handled independently in original row order:

1. accept a BLOB only when `embedding_format == "f64le-v1"`;
2. require valid positive dimensions equal to the active query dimension;
3. require the exact byte length and finite decoded values;
4. use the BLOB and its lightweight `array("d")` parity view when valid;
5. otherwise decode that row's existing JSON embedding;
6. ignore the row safely when both representations are invalid.

Missing BLOBs, unknown formats, zero/mismatched dimensions, truncation, trailing bytes, over-size buffers, non-finite values, and buffer/memoryview failures do not fail the lookup when JSON remains valid. A mixed lookup copies valid BLOBs directly, temporarily encodes only legacy/fallback rows, and sends one combined request to `/rag/vectors/rank-binary`. It never retries `/rag/vectors/rank` after a binary assembly, connection, timeout, HTTP, response, or parity failure.

Lookup diagnostics are fixed and value-free:

```text
embeddingStorage = blob | mixed | json
blobCandidates
legacyCandidates
invalidBlobCandidates
```

The prompt-hash exact lookup uses a column list that excludes both embedding representations. It returns before query embedding generation, candidate BLOB loading, or any Rust call. TTL, model, scope, cache version, attachment exact-only behavior, quality score, response/usage decoding, hit count, and last-hit mutation remain unchanged.

## Direct `DSVRNK01` assembly and parity

`encode_rank_request_from_blobs()` validates dimensions, candidate count, scalar budget, total request length, buffer contiguity, per-candidate byte length, and—unless the caller supplies already validated cache buffers—finite IEEE-754 exponent bits. It encodes the query once with `array("d")`, allocates one final `bytearray`, writes the unchanged 16-byte header, then copies candidate memoryviews into their final offsets. It does not materialize candidate floats or create a candidate `list[list[float]]`.

The wire format and 3.9.0 limits are unchanged: `DSVRNK01`, little-endian `u32` dimensions/count, query then candidate-major `f64`, at most 4,096 dimensions, 50,000 candidates, 1,600,000 scalars, and 12,800,016 request bytes. Tests require byte-for-byte equality with the existing list encoder.

Python still scans every accepted candidate and preserves clamped dot-product cosine semantics, positive-best selection, first-match ties, zero-vector behavior, and `rel_tol=1e-9` / `abs_tol=1e-12`. A Rust index/similarity divergence uses the Python result.

## Explicit offline migration

The migration utility is separate from application startup:

```bash
# default and explicit inspection modes; neither writes
python scripts/migrate_semantic_cache_embeddings.py --dry-run
python scripts/migrate_semantic_cache_embeddings.py --database /path/to/cache.sqlite3 --dry-run

# explicit write, bounded transactions, then verification
python scripts/migrate_semantic_cache_embeddings.py \
  --database /path/to/cache.sqlite3 \
  --write \
  --batch-size 100 \
  --verify

# verification-only inspection is still dry-run unless --write is present
python scripts/migrate_semantic_cache_embeddings.py --verify
```

The tool requires an existing regular, non-symlink file with the SQLite header and the semantic-cache table. It scans by increasing `rowid`, commits each batch, skips already valid dual-format rows, repairs invalid/unknown BLOB metadata from valid JSON, reports invalid JSON without deletion, and can resume after interruption by rerunning the same command. Updates touch only `embedding_blob`, `embedding_dimensions`, and `embedding_format`; prompt, response, usage, timestamps, hit fields, namespace/scope, quality, query text, and the legacy JSON embedding remain byte-for-byte unchanged.

The JSON report contains `scanned`, `migrated`, `skipped`, `invalid`, `failed`, and `wouldMigrate`; `--verify` adds valid/legacy/invalid counts. It never prints embeddings.

## Verification and performance evidence

Unit and integration coverage includes dual-write equality, little-endian bytes, idempotent no-scan schema upgrade, old-version rollback reads, mixed rows, every corruption fallback, exact-match short-circuiting, direct request byte equality, no JSON decode on valid BLOBs, one-endpoint failure behavior, migration dry-run/batching/resume, and value-free diagnostics.

The hybrid Compose smoke creates an isolated fresh SQLite database, stores three dual-format cache rows, converts one to legacy JSON-only and corrupts another BLOB while keeping valid JSON, performs a real lookup through one binary endpoint with full Python parity, then stops the sidecar and proves the same Python result without any external model call.

The release benchmark reports `16 x 384`, `128 x 768`, `1000 x 1536`, and mixed BLOB/legacy scenarios across SQLite JSON fetch, JSON decode, list assembly, SQLite BLOB fetch, BLOB validation, direct assembly, warmed HTTP from lists/BLOBs, full shadow integrations, and direct Python ranking from JSON/BLOB arrays. It records database bytes plus `fetchUs`, `legacyDecodeUs`, `blobValidationUs`, `payloadAssemblyUs`, `transportUs`, `rustProcessingUs`, `pythonValidationUs`, and `totalUs`. Absolute public-runner latency is informational; strict gates cover semantics, zero errors/unexpected fallbacks, identical request bytes, value redaction, and faster large-scenario direct assembly than JSON decode plus list assembly in the same run.

## Non-goals

The rc.2 freeze does not delete `embedding TEXT`, run a startup backfill, let Rust read SQLite, move semantic-cache ownership to Rust, enable Rust-primary or sampled parity, select binary automatically, use `f32`, compress embeddings, change `DSVRNK01`, add a delegate, enable any Rust flag or sidecar deployment by default, remove Python fallback, promote directly to stable 4.0.0, tag a commit, or publish a GitHub Release.
