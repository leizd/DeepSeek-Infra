# Implementation Plan: 4.5.0 Production Recovery Orchestration & DR Readiness

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Overview

Implement the approved specification in reversible vertical slices while keeping object-set-v1, Receipt v4, Commit v4, randomized Age, and legacy restore compatibility frozen. The existing `restoreId` session becomes the durable Recovery Job; no parallel job identity is introduced.

Specification: `docs/specs/4.5.0-production-recovery-orchestration.md`
Decision record: `docs/adr/ADR-0044-production-recovery-orchestration.md`

## Dependency graph

```text
Frozen compatibility fixtures
        |
Scheduler priority + FD budget
        |-------------------------------|
Remote parallel publish          Per-component restore state
        |                               |
Provider checksum capability     Lazy HEAD + parallel download
        |                               |
Prepared Object Set              Verified ciphertext cache
                                        |
                              Verified Projection Plan
                                        |
                              Network/crypto pipeline
                                        |
                              Renewable hold leases
                                        |
                              Recovery Job controls
                                        |
                                  Disk preflight
                                        |
                         Readiness + isolated Recovery Drill
                                        |
                             Real MinIO/Age/fault Evidence
```

## Architecture decisions

- Extend the existing restore session and routes additively; `restoreId` remains the authority.
- Migrate old `componentFetchIndex` on read, but write digest-keyed `componentStates` for object sets.
- Make cache and provider checksum optimizations fail-safe fallbacks, never trust roots.
- Keep whole-snapshot health in Scrub/Drill records; a selective preflight reports only Projection recoverability live.
- Gate polished UI work until transport, cache, pipeline, and safety Gates A-D pass.

## Phase 0 — Contract foundation

### Task 1: Freeze compatibility and version-development surfaces

**Acceptance criteria:**
- Protocol fixture tests prove object-set-v1, Receipt v4, Commit v4, and legacy Whole-Age behavior are unchanged.
- Development version surfaces become 4.5.0 only after the compatibility fixtures are green.

**Verification:** `python -m pytest tests/test_backup_object_set_contracts.py --no-cov` and `python scripts/check_release_version.py`.

**Files likely touched:** protocol tests, `VERSION`, version surfaces, release draft.
**Dependencies:** None.
**Scope:** Medium; split version/docs from behavioral fixtures if it exceeds five files.

## Phase 1 — Gate A: bounded transport

### Task 2: Enforce scheduler priority and FD budgets

**Acceptance criteria:**
- Stable ascending priority is observed before task admission.
- Worker, byte, and declared FD maxima are never exceeded; cancellation drains admitted work.

**Verification:** focused RED/GREEN tests in `tests/test_backup_component_transport.py`; `ruff` and `mypy` on changed modules.

**Files likely touched:** `backup_component_transport.py`, its tests.
**Dependencies:** Task 1.
**Scope:** Small.

### Task 3: Parallelize remote object-set upload

**Acceptance criteria:**
- S3/ObjectStore Components overlap under the shared scheduler and retain the journal barrier.
- Completion order leaves Receipt, ObjectSetDigest, and Commit bytes unchanged.

**Verification:** MemoryTargetStore overlap/failure tests plus existing publish contracts.

**Files likely touched:** `backup_publish.py`, publish tests, object-set contract tests.
**Dependencies:** Task 2.
**Scope:** Medium.

### Task 4: Persist per-component restore state

**Acceptance criteria:**
- Digest-keyed queued/downloading/partial/verified/failed states survive process exit.
- Existing `componentFetchIndex` sessions migrate safely; changed source identity or bad partial length restarts from zero.

**Verification:** session migration and subprocess-style restart contracts.

**Files likely touched:** a focused recovery-state module, `backup_remote_restore.py`, tests.
**Dependencies:** Task 2.
**Scope:** Medium.

### Task 5: Defer Payload HEAD until Projection closure

**Acceptance criteria:**
- Session creation validates Commit/Receipt/inventory and HEADs Controls only.
- A 5000-Component receipt with three required Components produces HEAD/GET only for Controls plus those three Payloads.

**Verification:** counting-store tests assert zero unselected Payload HEAD and GET.

**Files likely touched:** `backup_remote_restore.py`, object-set tests.
**Dependencies:** Task 4.
**Scope:** Medium.

### Task 6: Parallelize selective Component download

**Acceptance criteria:**
- Required Components overlap under worker/byte/FD limits and complete in arbitrary order.
- Every completed ciphertext is size/SHA verified before state becomes `verified`.

**Verification:** deterministic concurrency, failure drain, resume, and digest-order tests.

**Files likely touched:** `backup_remote_restore.py`, scheduler integration tests, object-set tests.
**Dependencies:** Tasks 4-5.
**Scope:** Medium.

### Checkpoint A

- Focused transport/restore suites green.
- `ruff check .` and `mypy .` green.
- Gate A behavioral evidence is local PASS; real MinIO status remains pending.

## Phase 2 — Gate B: verified ciphertext cache

### Task 7: Add verified encrypted Component cache

**Acceptance criteria:**
- Hits require exact size and streaming SHA-256; misses use fsync plus atomic rename.
- Corrupt entries are removed/refetched and never change restore output.

**Verification:** cache unit tests with corruption, crash partials, and no-secret metadata assertions.

**Files likely touched:** new cache module, its tests, `.gitignore`, `tests/conftest.py`; update `AGENTS.md` in a separate docs commit if needed.
**Dependencies:** Task 6.
**Scope:** Medium.

### Task 8: Add cache pins and quota GC

**Acceptance criteria:**
- Active, paused, and recovery-required jobs pin required digests.
- LRU quota GC evicts verified unpinned entries only; default quota is 20 GiB.

**Verification:** deterministic clock/quota tests and concurrent pin/GC contracts.

**Files likely touched:** cache module, recovery state integration, tests.
**Dependencies:** Task 7.
**Scope:** Medium.

### Checkpoint B

- Warm in-memory/filesystem restore produces zero remote Payload GET.
- Corruption and eviction contracts green.

## Phase 3 — Gate C: plan reuse and pipeline

### Task 9: Persist and validate the verified Projection Plan

**Acceptance criteria:**
- Atomic `verified-plan.json` binds selection, chain/ObjectSet, Control digests, and planner schema.
- A valid plan is reused; any mismatch fails closed into explicit replanning.

**Verification:** binding-tamper and restart tests.

**Files likely touched:** a plan module, `backup_remote_restore.py`, tests.
**Dependencies:** Task 6.
**Scope:** Medium.

### Task 10: Reuse Control metadata during materialize

**Acceptance criteria:**
- Valid Preview-to-Materialize flow does not decrypt/decode Controls again.
- Existing no-preview and legacy Whole-Age paths continue to work.

**Verification:** state-based decode-count and byte-identical materialization tests.

**Files likely touched:** `backup_remote_restore.py`, object-set tests.
**Dependencies:** Task 9.
**Scope:** Small.

### Task 11: Pipeline network and crypto with prompt plaintext scrubbing

**Acceptance criteria:**
- A bounded crypto queue overlaps verified downloads with decrypt/verify/extract.
- Each plaintext Component ZIP is scrubbed immediately after use and on all failures.

**Verification:** overlap, queue-bound, plaintext-lifetime, and injected-decrypt-failure tests.

**Files likely touched:** recovery pipeline module, remote restore integration, tests.
**Dependencies:** Tasks 8 and 10.
**Scope:** Medium.

### Checkpoint C

- Preview plan reuse and pipeline contracts green.
- Actual scratch inspection confirms no retained plaintext Component ZIP.

## Phase 4 — Gate D: protection and job controls

### Task 12: Make recovery holds renewable leases

**Acceptance criteria:**
- CAS renewal increments generation and extends expiry for all protected phases.
- Pause and recovery-required retain renewal; safe terminal phases release.

**Verification:** fake-clock, ETag-conflict, long-duration, retention interaction, and restart tests.

**Files likely touched:** recovery lease module, remote restore/retention integration, tests.
**Dependencies:** Task 4.
**Scope:** Medium.

### Task 13: Add durable pause, resume, and phase-aware abort

**Acceptance criteria:**
- Pause stops new admission after checkpoints; resume validates partial state.
- Abort cleans safely before commit, aborts prepared transactions, and enters recovery-required on uncertain/partial commit.

**Verification:** state-machine/API tests plus process restart between request and convergence.

**Files likely touched:** recovery job module, two route modules, tests, typed frontend client.
**Dependencies:** Tasks 8, 11-12.
**Scope:** Medium; split core and route/client slices.

### Task 14: Add disk/dependency Recovery preflight

**Acceptance criteria:**
- Report covers closure, cache, network, scratch, safety backup, free disk, and health.
- Insufficient capacity blocks before mutation with a stable error code.

**Verification:** pure arithmetic boundaries, disk-probe failure, and route contract tests.

**Files likely touched:** preflight module, route/client, tests, `docs/API.md`.
**Dependencies:** Tasks 8-10 and 12.
**Scope:** Medium; keep docs/client separate if needed.

### Checkpoint D

- Active/paused/recovery-required protection verified.
- Pause/resume/abort and insufficient-disk preflight pass across restart.
- Review before any UI work.

## Phase 5 — Gate E: cost and provider integrity

### Task 15: Introduce Prepared Object Sets

**Acceptance criteria:**
- Exact prepared Component bytes determine Adaptive Full and feed Age directly.
- Threshold crossing can stop component preparation early; every plaintext path is scrubbed.

**Verification:** compression-call counts, early-abort-before-Age, cleanup, and output compatibility tests.

**Files likely touched:** `backup_object_set.py`, `backup_scheduled.py`, `backup_executor.py`, tests.
**Dependencies:** Task 1.
**Scope:** Medium; split preparation from executor integration.

### Task 16: Add authoritative provider-checksum capability

**Acceptance criteria:**
- Capability probe distinguishes proven full-object SHA-256 from fallback.
- Single/multipart ETag is never accepted; missing/inconsistent checksum forces readback.

**Verification:** fake S3 response matrix and store contract tests.

**Files likely touched:** `backup_target_store.py`, `backup_target_s3.py`, tests.
**Dependencies:** Task 1.
**Scope:** Medium.

### Task 17: Use provider integrity mode in parallel publish

**Acceptance criteria:**
- Proven checksum avoids Payload full readback.
- Fallback preserves current streaming full SHA-256 readback and atomic publish barrier.

**Verification:** counting-store tests for zero/nonzero readback and corrupted claims.

**Files likely touched:** `backup_publish.py`, publish tests.
**Dependencies:** Tasks 3 and 16.
**Scope:** Small.

### Checkpoint E

- No object-set double compression.
- Provider checksum and fallback contracts green without wire-format changes.

## Phase 6 — Gate F: DR readiness

### Task 18: Persist recovery telemetry and readiness inputs

**Acceptance criteria:**
- Bounded-cardinality counters/histograms answer job phase, transport/cache, lease, and stage-throughput questions.
- No secret, logical metadata, digest, path, or error string becomes a metric label.

**Verification:** telemetry snapshot tests and a redaction/cardinality review.

**Files likely touched:** recovery telemetry module, observability integration, tests.
**Dependencies:** Tasks 11-14 and 17.
**Scope:** Medium.

### Task 19: Expose DR readiness

**Acceptance criteria:**
- Status reports actual RPO and latest committed/Scrub/Drill health.
- RTO is explicitly estimated from recent successful stage throughput and reports unavailable when evidence is insufficient.

**Verification:** pure aggregation and authenticated route/client tests.

**Files likely touched:** readiness module, route/client, tests, `docs/API.md`.
**Dependencies:** Task 18.
**Scope:** Medium.

### Task 20: Add isolated manual Recovery Drill

**Acceptance criteria:**
- Drill uses production fetch/decrypt/verify/materialize paths but cannot invoke live commit.
- Drill root and plaintext are destroyed; result records chain/components/bytes/duration and releases safe holds/pins.

**Verification:** sentinel live Workspace remains byte-identical under success and injected failure.

**Files likely touched:** drill module, route/client, tests, `docs/API.md`.
**Dependencies:** Tasks 12-14 and 18.
**Scope:** Medium.

### Checkpoint F

- Readiness, RPO, estimated-RTO, and isolated Drill contracts green.
- Security review confirms drill has no live-commit capability.

## Phase 7 — Gate G: real Evidence and release

### Task 21: Add real MinIO/Age cold and warm recovery Evidence

**Acceptance criteria:**
- Cold selective restore proves parallel required-only transfer.
- Warm restore proves zero remote Payload GET and corrupt-cache refetch.

**Verification:** dedicated CI job with real MinIO and real Rust Age helper; version-derived Evidence artifact.

**Files likely touched:** E2E runner/test, CI workflow, Evidence contract.
**Dependencies:** Checkpoints A-F.
**Scope:** Medium; separate CI from E2E code.

### Task 22: Add subprocess pause/resume and fault-injection Evidence

**Acceptance criteria:**
- Real process exit resumes per-Component transfer and paused job.
- Faults cover disk exhaustion, lease conflict, cache corruption, remote mutation, and partial federated commit.

**Verification:** dedicated CI artifacts with only executed checks marked PASS.

**Files likely touched:** E2E runner/tests, CI workflow, Evidence contract.
**Dependencies:** Task 21.
**Scope:** Medium.

### Task 23: Complete release surfaces and full gates

**Acceptance criteria:**
- Changelog, API/security/architecture/status/release docs describe implemented behavior only.
- Full Python/frontend/offline eval/security/release gates pass; CI-only evidence is attached and exact-merge.

**Verification:** commands in the specification and `python scripts/preflight_release.py --version 4.5.0`.

**Files likely touched:** release documentation/evidence indices and version-derived generated release artifacts.
**Dependencies:** Tasks 21-22.
**Scope:** Medium; documentation and generated Evidence commits remain separate.

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Parallel workers corrupt a shared session journal | Lost/incorrect resume state | Single atomic state coordinator; workers return results and never concurrently rewrite JSON. |
| Lazy HEAD weakens whole-snapshot health claims | False healthy status | Report Projection recoverability separately; source full health only from Scrub/Drill. |
| Cache becomes an implicit trust root | Corrupt restore | Verify size/SHA on every hit against Control/Receipt commitments. |
| Lease renewal races retention | Ancestors collected during recovery | CAS generation, renewal margin, fail-closed protection degradation, fake-clock concurrency tests. |
| Plan reuse accepts stale selection/chain | Wrong scope restored | Bind selection, chain/ObjectSet, Control digests, and planner schema; mismatch replans. |
| Pipeline retains plaintext on error | Data exposure | `finally` scrubbing and injected failures at every stage. |
| Provider claims checksum but returns weak semantics | Undetected corruption | Active probe plus strict full-object SHA-256 equality; otherwise full readback. |
| DR status overstates readiness/RTO | Unsafe operational decision | Explicit health provenance; RTO labelled estimate/unavailable, never SLA. |
| Large milestone accumulates unreviewable diff | Review/rollback risk | One task per atomic commit, checkpoints after each Gate, no task over five files without splitting. |

## Review checkpoint

Approved by the user on 2026-08-13. Implementation may proceed in the listed slices. Any material change to frozen invariants, job identity, cache trust, lease lifecycle, or API compatibility updates the spec/ADR first and returns for review.
