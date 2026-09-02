# Implementation Plan: 4.8.1 Native Runtime Contract Freeze

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

## Overview

4.8.1 freezes the merged 4.8.0 Python production semantics as immutable native
compatibility corpora and introduces the contract/tooling foundations for a Rust
data/security plane and Go control plane. It adds no business capability and
changes no production mutation owner.

This plan intentionally does not replace `tasks/plan.md`; that file is the
completed 4.8.0 release plan.

## Architecture decisions

- ADR-0049 is Accepted. 4.8.1 keeps Python production authority while native
  components remain non-authoritative.
- Public/storage/federation/evidence behavior is frozen before native cutover.
- Internal Go/Rust boundaries use versioned Protobuf/gRPC.
- `actionId + executionEpoch` and fail-closed `EFFECT_UNKNOWN` are foundational,
  not later hardening.
- Go shadow state and Python authoritative state never share a writable store.
- Generated code is reproducible; hand-edited generated bindings are rejected.

## Dependency graph

```text
spec + ADR + ownership matrix
        |
        +--> pinned native toolchain --> proto source --> generated bindings
        |                                      |              |
        |                                      |              +--> Go lifecycle
        |                                      |              +--> Rust worker
        |                                      |
        +--> canonical fixture inventory ------+--> replay harnesses
                                                       |
                                                       +--> native CI/drift gates
                                                       |
                                                       +--> release evidence/runbook
```

## Task list

### Phase 0: Decision and machine-readable ownership

#### Task 1: Review and accept the native runtime architecture

**Acceptance criteria:**

- The specification covers objective, commands, structure, style, testing,
  boundaries, version gates, and exact 5.0 success criteria.
- ADR-0049 explicitly reconciles/supersedes ADR-0040 at staged 4.9.x gates.
- A fresh-context adversarial review has no unresolved substantive finding.

**Verification:** Markdown link/encoding checks and human approval.

**Dependencies:** None.

**Files:** `docs/specs/5.0-native-rust-go-runtime.md`,
`docs/adr/ADR-0049-native-runtime-ownership-inversion.md`,
`docs/NATIVE_RUNTIME_MIGRATION_ROADMAP.md`, this plan.

**Scope:** Medium (4 files).

#### Task 2: Add the machine-readable ownership contract

**Acceptance criteria:**

- Every production domain has exactly one final owner and one migration phase.
- Frozen contracts and forbidden ownership combinations are machine-readable.
- A contract test rejects duplicate owners, missing domains, shared stores, and
  any production Python owner in the 5.0 target.

**Verification:** focused ownership-contract pytest, then Ruff and Mypy.

**Dependencies:** Task 1.

**Files:** `release/native_runtime_ownership_v1.json`, JSON schema/helper,
focused test, ADR link (at most 4 files).

**Scope:** Medium.

### Checkpoint A: Architecture boundary

- ADR accepted and docs checks pass.
- Ownership contract test passes.
- Existing 4.8.0 plan/todo remain byte-identical.

### Phase 1: Reproducible protocol foundation

#### Task 3: Pin and verify the native code-generation toolchain

**Acceptance criteria:**

- Go 1.27.x, protoc 36.x, Go/Rust plugins, and checksums are locked.
- Bootstrap/check logic is Windows/Linux compatible and never mutates on
  `--check`.
- Missing, wrong, or tampered tools fail with a stable diagnostic.

**Verification:** focused toolchain-lock tests and dry-run/check commands.

**Dependencies:** Task 2.

**Files:** toolchain lock, bootstrap/check script, focused tests, docs (4 files).

**Scope:** Medium.

#### Task 4: Define common and action v1 contracts

**Acceptance criteria:**

- Common IDs/digests/schema metadata and action/effect messages are additive v1.
- `action_id` and non-zero `execution_epoch` are mandatory by validation.
- Effect states include explicit UNKNOWN and never default to NOT_APPLIED.

**Verification:** descriptor/schema tests fail RED before sources, then pass;
breaking/drift check passes.

**Dependencies:** Task 3.

**Files:** common proto, action proto, validation contract, focused tests (4 files).

**Scope:** Medium.

#### Task 5: Define storage and federation v1 contracts

**Acceptance criteria:**

- Commands/results reference, but do not redefine, frozen storage/federation
  wire objects.
- Request/result bindings include source/destination, object-set digest, action
  fence, effect/receipt/commit/proof digests, and reconciliation identity.
- Private key material and provider credentials have no message field.

**Verification:** descriptor security tests and frozen-field-number tests.

**Dependencies:** Task 4.

**Files:** storage proto, federation proto, focused test, contract notes (4 files).

**Scope:** Medium.

#### Task 6: Define control, evidence, and agent v1 contracts

**Acceptance criteria:**

- Control and Agent messages express desired/actual state and fenced commands,
  not byte movement.
- Evidence messages bind existing proof envelope/type/digest without changing it.
- No message permits a shadow controller to request a production mutation.

**Verification:** descriptor ownership/negative tests.

**Dependencies:** Task 4.

**Files:** three proto files, focused test (4 files).

**Scope:** Medium.

#### Task 7: Generate and drift-gate Go/Rust bindings

**Acceptance criteria:**

- Clean-checkout generation is deterministic on Windows and CI Linux.
- Generated Go and Rust bindings compile against locked runtimes.
- CI/check mode fails on source/generated descriptor drift.

**Verification:** generate, clean diff, Go compile/test, Cargo compile/test.

**Dependencies:** Tasks 4-6.

**Files:** generation config/script plus generated roots/manifests; split commits if
generated output exceeds review size.

**Scope:** Medium per generated package; execute as multiple atomic increments.

### Checkpoint B: Protocol foundation

- Descriptor compatibility and generated drift gates pass.
- Go/Rust bindings compile under pinned toolchains.
- No production route, store, or effect owner has changed.

### Phase 2: Native process foundations

#### Task 8: Initialize the read-only Go control process

**Acceptance criteria:**

- `deepseekd` starts, validates config, exposes internal health/readiness, and
  shuts down through context cancellation.
- The 4.8.1 binary has no production store write or mutation RPC capability.
- Config errors and lifecycle transitions have deterministic tests.

**Verification:** RED/GREEN Go unit tests, `go vet`, `go test`, `go test -race`.

**Dependencies:** Task 7.

**Files:** Go module, command, config/lifecycle package, tests (split to <=5 files).

**Scope:** Medium per increment.

#### Task 9: Add isolated Go shadow decision envelopes

**Acceptance criteria:**

- Shadow inputs produce deterministic digests and immutable comparison output.
- Shadow mode has no production credential/store/effect dependency.
- Attempted mutation in 4.8.1 is mechanically rejected.

**Verification:** failing mutation-denial test first, deterministic replay/race tests.

**Dependencies:** Task 8.

**Files:** shadow package, isolated store/output, tests, docs (<=5 files).

**Scope:** Medium.

#### Task 10: Add the Rust protocol and worker admission foundation

**Acceptance criteria:**

- Rust consumes generated contracts through a dedicated protocol crate.
- Worker admission rejects empty IDs, zero/stale epochs, unsupported versions,
  and invalid digest bindings before side effects.
- Unknown outcomes remain typed UNKNOWN and cannot be retried as NOT_APPLIED.

**Verification:** RED/GREEN Rust tests, fmt, clippy `-D warnings`, test.

**Dependencies:** Task 7.

**Files:** workspace manifest, protocol crate, worker crate, focused tests
(split into atomic <=5-file increments).

**Scope:** Medium per increment.

### Checkpoint C: Non-authoritative native processes

- Go shadow and Rust worker foundations compile and pass race/clippy tests.
- Mutation-denial and stale-epoch tests are green.
- Production Python authority remains unchanged.

### Phase 3: Canonical corpus and replay

#### Task 11: Freeze the corpus manifest and extraction rules

**Acceptance criteria:**

- Every public/storage/federation/evidence/state surface has an owner, source,
  normalization rule, sensitivity classification, and expected digest.
- Extraction is deterministic, redacts secrets, and cannot mutate production
  or runtime stores.
- Fixtures record the exact 4.8.0 merge/head provenance.

**Verification:** manifest schema and deterministic extraction tests.

**Dependencies:** Task 2.

**Files:** corpus manifest/schema, extractor, tests, README (4 files).

**Scope:** Medium.

#### Task 12: Freeze REST, SSE, MCP, and A2A corpora

**Acceptance criteria:**

- Valid, invalid, ordering, streaming/cancellation, and stable-error cases exist.
- Corpus derives from real 4.8.0 implementations and existing compatibility
  smokes; no fabricated success path.
- Dynamic fields have explicit normalization and raw canonical inputs remain.

**Verification:** Python oracle replay and fixture digest test.

**Dependencies:** Task 11.

**Files:** one corpus family per atomic increment, extractor/test updates.

**Scope:** Medium per family.

#### Task 13: Freeze storage, federation, evidence, and state corpora

**Acceptance criteria:**

- Existing authenticated bytes/digests and legal/illegal transitions are covered.
- Receipt v4, Commit v4, object-set-v1, randomized Age, Control Authority,
  Federation, and Evidence semantics remain exact.
- Sensitive/provider fields are redacted without weakening binding assertions.

**Verification:** existing contract validators plus new immutable corpus digests.

**Dependencies:** Task 11.

**Files:** one corpus family per atomic increment, extractor/test updates.

**Scope:** Medium per family.

#### Task 14: Replay canonical corpora through Rust

**Acceptance criteria:**

- Rust replays every eventual Rust-owned corpus with exact normalized results.
- Diagnostics identify corpus/case/field/digest without emitting secrets/payloads.
- Unsupported behavior fails the gate; it does not silently call Python.

**Verification:** Rust unit/integration replay and cross-language parity script.

**Dependencies:** Tasks 10, 12, 13.

**Files:** one replay adapter/test per corpus family.

**Scope:** Medium per family.

#### Task 15: Replay canonical corpora through Go

**Acceptance criteria:**

- Go replays every eventual Go-owned decision/state corpus deterministically.
- Results match Python normalized semantics and stable error categories.
- Replay is read-only and cannot call mutation handlers.

**Verification:** Go replay/race tests and cross-language parity script.

**Dependencies:** Tasks 9, 12, 13.

**Files:** one replay adapter/test per domain.

**Scope:** Medium per domain.

### Checkpoint D: Contract freeze

- Python/Rust/Go parity reports are clean for all assigned corpora.
- Corpus hashes and provenance are immutable and reproducible.
- Existing Python, frontend, Rust, eval, and release gates remain green.

### Phase 4: CI, operations, and release closure

#### Task 16: Add native CI lanes and protocol drift gates

**Acceptance criteria:**

- Go format/vet/staticcheck/test/race/coverage/vulnerability gates run.
- Rust existing gates include new crates and parity tests.
- Generated-code drift and Rust-Go corpus compatibility fail independently with
  bounded diagnostics.

**Verification:** local workflow contract tests and exact PR-head Actions run.

**Dependencies:** Tasks 7, 14, 15.

**Files:** workflow plus focused workflow-contract tests/scripts (<=5 files per
increment).

**Scope:** Medium per CI lane.

#### Task 17: Document operation, rollback, and ownership transfer

**Acceptance criteria:**

- Runbook covers shadow safety, process failure, version mismatch, unknown
  effect, corpus correction, and rollback without shared writers.
- Operators can prove which runtime owns every state/effect at any point.
- Rollback never interprets uncertainty as no effect.

**Verification:** docs link/command checks and adversarial scenario review.

**Dependencies:** Tasks 9, 10, 16.

**Files:** runbook, ownership doc links, docs tests (<=4 files).

**Scope:** Medium.

#### Task 18: Qualify and release 4.8.1

**Acceptance criteria:**

- Version surfaces and changelog describe only contract/foundation changes.
- Full repository and native gates pass on the exact head with no skips.
- Evidence proves no production owner or frozen contract changed.

**Verification:** repository release sequence, exact-head CI, Evidence Assembly,
and RC Readiness. Local results alone cannot mark release PASS.

**Dependencies:** Tasks 1-17.

**Files:** version/changelog/release evidence files split by existing convention.

**Scope:** Medium per increment.

## Risks and mitigations

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Canonical corpus accidentally normalizes away behavior | High | Retain raw input/output, explicit normalization schema, digest both |
| Proto default enum treats unknown as safe | Critical | zero=UNSPECIFIED; UNKNOWN explicit; boundary validation; negative tests |
| Go/Rust generated runtime version skew | High | pinned toolchain/runtime locks and clean regeneration drift gate |
| Shadow process mutates production | Critical | separate credentials/store, no mutation service in 4.8.1, denial tests |
| ADR-0040/0049 ambiguity | High | staged supersession table and per-gate authority evidence |
| Existing 4.8.0 planning artifacts overwritten | Medium | isolated `tasks/native-runtime/` paths and byte-identity checkpoint |
| Windows local machine lacks Go/protoc | Medium | checksum-pinned bootstrap; CI Linux remains independent evidence |
| Fixture or Evidence contains secrets | Critical | sensitivity manifest, allowlist serialization, scanner tests |
| Migration becomes permanent hybrid | High | zero-usage gates and fallback-removal 5.0 exit criterion |

## Explicit non-goals for 4.8.1

- No new product capability.
- No public listener cutover.
- No Go production mutation or authoritative database.
- No production storage bytes moved by a new Rust worker.
- No frozen wire revision.
- No Python fallback removal.
- No release PASS from local tests alone.
