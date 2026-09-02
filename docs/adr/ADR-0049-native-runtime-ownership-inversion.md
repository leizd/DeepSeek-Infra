# ADR-0049: Native Rust/Go Runtime Ownership Inversion

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

- Status: Accepted
- Date: 2026-09-02
- Applies to: 4.8.1 migration foundation through 5.0.0
- Machine contract: [`release/native_runtime_ownership_v1.json`](../../release/native_runtime_ownership_v1.json)
- Specification: [Native Rust/Go Runtime Ownership Inversion](../specs/5.0-native-rust-go-runtime.md)

## Context

ADR-0040 intentionally made the complete 4.x line Python-first: Rust delegates
were optional, default deployment was Python-only, streaming and real MCP tool
execution stayed in Python, and Python fallback was retained. That was the right
decision while the native paths were partial.

By 4.8.0 the repository has seven real Rust crates and production-grade backup,
resilience, Evidence, and signed federation semantics, but production authority
still resides in the Python package. Continuing to add Python-owned control and
data paths increases migration cost and leaves the most sensitive boundaries
split between an authoritative Python runtime and optional native helpers.

The requested end state is now explicit: production ownership must move entirely
to Rust and Go while repository Python remains only for offline reference,
evaluation, migration, benchmark, and release tooling.

## Decision

1. **Invert ownership.** Rust owns the data/security plane and Go owns the
   control plane. Production Python is a migration source, not the 5.0 runtime.
2. **Keep one public port.** Rust is the eventual public HTTP/SSE listener and
   internally proxies low-frequency `/api/*` control calls to Go.
3. **Use process boundaries.** Go and Rust communicate through versioned gRPC and
   Protobuf. C FFI/cgo is not the primary server, desktop, or worker architecture.
4. **Bind every effect.** `actionId + executionEpoch` is mandatory from Go claim
   through Rust effect, provider metadata, proof, and Go reconciliation. Stale
   epochs are rejected and unknown remote outcomes remain `EFFECT_UNKNOWN`.
5. **Assign one durable owner.** Go alone writes control-plane state; Rust alone
   writes transfer/effect/checkpoint state. No table or SQLite database has
   simultaneous Python/Go/Rust write ownership.
6. **Preserve private-key isolation.** Federation and other security private keys
   stay in Rust custody. Go requests a signature over a validated digest and
   receives only the signature and public binding.
7. **Freeze wire semantics.** The migration does not upgrade object-set, Receipt,
   Commit, FastCDC, Age, Control Authority, Federation, or Evidence formats.
8. **Freeze before cutover.** 4.8.1 captures Python 4.8.0 canonical behavior and
   establishes replayable Go/Rust compatibility oracles before business migration.
9. **Cut over incrementally.** Each domain passes shadow and parity gates before
   it becomes authoritative. Unknown effects or parity divergence stop cutover.
10. **Make fallback temporary.** Python compatibility can support rollback during
    4.9.x, but it is disabled by default in 4.9.4 and absent from 5.0 production.
11. **Preserve feature coverage.** Server, desktop, Android, document/OCR/media,
    stateless MCP, and background behavior must be ported or formally deprecated;
    no production feature disappears implicitly.
12. **Define 5.0 by evidence.** Language-local tests are insufficient. Exact-head
    native process, provider-backed, crash-recovery, zero-Python image, parity,
    security, coverage, fuzz, and performance evidence gates the release.

## Ownership summary

| Concern | Authoritative owner |
| --- | --- |
| Public HTTP/SSE, Gateway, MCP protocol, RAG hot path | Rust |
| Tool sandbox, storage/crypto/transfer/proof/signing | Rust |
| Config, lifecycle, `/api/*`, desired/actual state | Go |
| Scheduler, leases/fencing, Action Journal, Agent DAG | Go |
| Resilience, DR, federation control lifecycle | Go |
| Browser UI | TypeScript client |
| Offline oracle/eval/migration/release tooling | Python, never production |

## Relationship to ADR-0040

ADR-0040 remains operationally binding for 4.8.1: Python keeps production
authority and all native components remain non-authoritative. Once this ADR is
accepted, its staged cutover decision supersedes ADR-0040 decisions 1-5 as each
4.9.x ownership gate is individually met:

- 4.9.0 introduces read-only Go shadow control.
- 4.9.1 transfers payload-byte movement to Rust.
- 4.9.2 makes the Rust listener authoritative.
- 4.9.3 makes Go control domains authoritative one at a time.
- 4.9.4 disables Python production authority by default while retaining an
  explicit rollback runtime.
- 5.0 removes the production Python runtime and fallback.

Public and frozen wire compatibility remains binding throughout. A cutover gate
that has not passed leaves the prior owner authoritative; it does not permit
dual writers.

## Consequences

- The repository temporarily carries Python reference logic and native
  replacements, increasing test cost until each domain reaches zero usage.
- Process isolation gives independent crash recovery, profiling, upgrades, and
  allocator ownership at the cost of explicit RPC/version/timeout handling.
- Go/Rust boundaries become durable contracts and require generation, compatibility,
  breaking-change, and fuzz gates.
- Shadow evaluation needs isolated state and explicit telemetry; it cannot write
  the authoritative store or cause external effects.
- Rollback after a data-owner cutover is a controlled ownership transfer using
  exported state and fences, not concurrent writes or automatic fallback.
- The source-language distribution in the repository is not a release metric.
  Production ownership and measured zero usage are the release metrics.

## Frozen compatibility surface

`object-set-v1`, Receipt v4, Commit v4, FastCDC v3, randomized Age,
`control-authority-v1`, AuthorityCheckpoint v1, `dr-readiness-proof-v1`,
`evidence-proof-v2`, `predictive-planning-proof-v1`, and the signed Federation
4.8.0 wire semantics remain unchanged.

## Rejected alternatives

- A big-bang rewrite or direct translation of every `.py` file.
- Python, Go, and Rust writing the same SQLite database or table.
- Go-to-Rust cgo/static-library calls as the main runtime architecture.
- Continuing ad-hoc JSON RPC for new cross-language boundaries.
- Changing frozen storage/federation formats to make the rewrite easier.
- Reimplementing FastCDC/crypto hot paths in Go or controller state machines in
  Rust solely to increase one language's share.
- Deleting the Python oracle before native parity is production-proven.
- Keeping Python fallback indefinitely after 5.0.
- Removing all Python from the repository for language-purity optics.
