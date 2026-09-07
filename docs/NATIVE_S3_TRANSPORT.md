# Native S3 transport increment

This is a Rust byte-transport library, not production storage authority. The worker's
`StorageNotAuthoritative` / `TransferNotAuthoritative` barriers remain unchanged.
Python is allowed to provision isolated test providers; test payloads must be generated,
uploaded, downloaded and hashed by Rust. No filesystem or fake-S3 fallback is allowed.

## Contract

- Explicit trusted target endpoint, bucket, prefix, region and credentials. No ambient
  credential discovery, proxy, redirect, automatic HTTP retry or unsigned request.
  TLS is required except an explicitly enabled numeric loopback MinIO endpoint.
  HTTP/1-only with no idle pooling also avoids hyper-util's independent pooled-connection
  cancellation retry. Connection pooling and HTTP/2 are deferred pending an explicitly
  non-retrying lower-level connector; this increment makes no throughput/SLO claim.
- Exact canonical object keys only; reject ambiguous paths rather than normalizing them.
  This is intentionally not yet a replacement for every legacy S3 key/configuration.
- Single PUT is bounded to 8 MiB, uses provider `If-None-Match: *` or a strong exact
  `If-Match` ETag, SHA-256 payload checksum, and `sha256`, `action-id`, `execution-epoch`
  metadata. There is no unconditional write, delete, or multipart API in this increment.
- A local validation rejection has no remote effect. A provider precondition rejection
  is explicit. Every other write error is `EffectUnknown`, including a missing/invalid
  success ETag. Never infer absence from a timeout or automatically retry a mutation.
- Successful PUT returns a provider observation, **not** an EffectProof, receipt, commit,
  durable job completion, current lease assertion, or exactly-once guarantee. Cancellation
  after sending is also uncertain; future effect-journal callers must durably record intent
  before polling the write future and reconcile cancelled/interrupted requests.
- GET is consumed incrementally into a caller-owned staging sink with backpressure.
  The sink must be fresh/empty and positioned at offset zero. Success verifies the
  transferred stream and `flush`, not arbitrary existing sink contents or fsync durability.
  Expected length and SHA-256 must match before returning success. The sink may contain
  partial/unverified data on any error; it must not be published or treated as restored.
  Metadata checksum claims are not provider checksums and do not establish byte integrity.
  Whole-object GET/HEAD requires HTTP 200 without Content-Range. Error response bodies
  are dropped before the SDK can buffer them; PUT classification retains raw per-request
  status provenance and only a single-attempt HTTP 412 means `PreconditionRejected`.
- The public error and transport/credential `Debug` surfaces do not expose credentials,
  endpoints, object keys or provider response bodies. Dependency debug logging can expose
  connection endpoints; do not enable dependency debug/trace logs where these are sensitive.
  Target configuration is operator-controlled, not a public URL fetch API.

## Pinned implementation sources

Rust MSRV remains 1.85. `object_store` 0.12.4 supplies S3 SigV4 and conditional PUT;
the transport uses an explicit hardened reqwest connector instead of its default client.
Version 0.13.2 declares MSRV 1.85 but its cloud token cache uses let-chains, which
fail on 1.85 with E0658; the real AWS-feature build, not package metadata, is the gate.

- [Apache object_store 0.12.4 S3 API](https://docs.rs/object_store/0.12.4/object_store/aws/struct.AmazonS3.html)
- [Conditional write modes](https://docs.rs/object_store/0.12.4/object_store/enum.PutMode.html)
- [Retry configuration](https://docs.rs/object_store/0.12.4/object_store/struct.RetryConfig.html)
- [S3 conditional writes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html)

## Placement identity and conditional byte observations

`target_identity()` is a SHA-256 placement fingerprint with domain separator
`deepseek-infra:s3-target-v1` followed by a NUL byte. The fields are canonical
endpoint origin (without the optional final slash), region, bucket and prefix,
in that order. Each UTF-8 field is preceded by its unsigned 64-bit big-endian byte
length. Credentials are deliberately excluded so renewal does not change placement;
the object key and write condition must be bound separately by the worker journal.
This fingerprint is not expected-bucket-owner verification or authority proof.

`download_observation_verified()` performs a GET with the observed strong ETag as
If-Match and the observed version ID when available. Before consuming the body it
compares the GET's full operation metadata with the prior observation, including
length, version and all six action/digest fields. It then hashes the complete
bounded-length stream. Same-byte overwrites can retain the ETag while changing
metadata, so the ETag check alone is insufficient. Staging obligations remain the
same as `download_verified()`, and any error leaves data unverified.
The pinned [GetOptions contract](https://docs.rs/object_store/0.12.4/object_store/struct.GetOptions.html)
supplies the conditional request, not a positive worker effect verdict.

The real-MinIO suite passed six storage cases after adding a same-ETag metadata
replacement, a changed-ETag conditional rejection, and correct/incorrect checksum
observations. The five existing worker provider cases also passed at that revision.
The worker integration now journals the placement/condition before dispatch and uses
this conditional read for UNKNOWN recovery; see `NATIVE_WORKER_AUTHORITY_STORE.md`.
Neither those primitives nor the optional worker path complete production reconciliation.

## Remaining production gates

Multipart create/ListParts/resume/conditional completion/abort, expected bucket owner,
provider full-object checksum observations, credential renewal/role providers, legacy
noncanonical key compatibility, signed admission and live fencing, durable data-plane
effect journal, SIGKILL takeover, Receipt/Commit publication and real native federation
are still required. This library must not be advertised as the completed 4.9.1 migration.

## Verification

```text
cargo test --locked --manifest-path rust/Cargo.toml -p deepseek-storage --features s3 --lib --test s3_transport
python scripts/run_native_s3_e2e.py
```

On Windows use `--toolchain 1.85.0-x86_64-pc-windows-gnu` on the runner if needed.
The existing real-storage harness provisions five independent MinIO processes/containers;
these tests use its three source endpoints. Python only creates isolated buckets and
starts/stops the providers; Rust owns every test payload and digest. A missing provider
fails the runner. `s3-e2e` is opt-in so ordinary offline Cargo tests do not silently skip
or impersonate provider tests. CI runs both paths on 1.85 and makes Evidence Assembly
depend on `native-s3-transport` succeeding.

Local 2026-09-06: five real-provider tests passed with zero skipped tests, using the
official Windows MinIO RELEASE.2025-09-07T16-13-09Z binary, SHA-256
`af709e6ba68488404e85acdd22a3030d0f5e56a108d4b27d744f18ceb50861b4`.
The tests cover 8 MiB byte/metadata round trips across three providers, exact Unicode,
space and percent-encoded-literal keys, create/CAS conflicts and replacement, staging
integrity and sink failures, absent objects/buckets, and a TCP relay that drops a real
MinIO 200 ACK after commitment. Reconciliation independently reads and hashes that
committed object while the interrupted write remains `EffectUnknown`.

Windows MinIO rejects keys containing `?` with `XMinioInvalidObjectName`. The provider
suite executes an explicit rejection assertion there; Linux executes the exact-key
round trip. Neither platform rewrites the key or skips the case. This is transport
development evidence, not provider-certified parity for every S3 implementation, a
release Evidence PASS, a controller/worker SIGKILL proof, or native Federation evidence.
