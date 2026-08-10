# Implementation Plan: 4.4.12 Packed Delta Payloads & Persistent Snapshot State

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Overview

4.4.12 makes incremental-backup metadata and local staging scale with the changed set. The local SQLite index stores immutable content-addressed file versions, per-snapshot PUT/DELETE operations, and one current effective view instead of copying the complete workspace into every incremental snapshot. New `incremental-v5` packages stream small whole-file payloads and every unmatched CDC chunk into immutable 64 MiB packfiles. Restore reads verified pack ranges while retaining v2-v4 compatibility. Native scanning moves from one captured serial batch to a persistent, bounded Rust worker pool.

## Architecture Decisions

- Keep `fastcdc-gear-v3`; v5 changes only child payload layout, so a v4 parent remains incremental-compatible.
- Store the current view generation in one `current_effective_heads` row. Stamping the latest backup id onto every effective-file row would rewrite the entire workspace and defeat the release objective.
- Keep legacy materialized tables readable during additive migration, but stop production writes from growing them. Historical state is reconstructed from one Full plus at most the configured delta depth.
- Derive `file_version_id` from size, file SHA-256, and nullable chunk-map id. Paths refer to versions; equal content shares versions across copies and renames.
- Packs are snapshot-local and live only inside the encrypted archive. Target size is 64 MiB, maximum size 72 MiB, alignment 8 bytes; whole files above 16 MiB remain standalone.
- Treat decrypted pack metadata as untrusted. Validate relative paths, object shapes, bounds, pack digest, blob digest, file digest, and snapshot Merkle root.
- Use a persistent JSONL helper process with a bounded Rust worker pool. Python budgets the estimated scan working set rather than logical file size.
- Compaction never runs a full `VACUUM` on the scheduler path. Incremental vacuum is eligible only above 256 MiB with more than 30% free pages.

## Dependency Graph

```text
Rust streaming/budget contract
        │
Index v3 schema + migration ──→ effective parent lookup
        │
PackWriter contract ──────────→ incremental-v5 builder
                                      │
                                      └──→ pack-range restore + validation
Index v3 ──→ GC/metrics/compaction
Builder + restore + S3 adapter ──→ real HTTP S3 E2E
All slices ──→ release docs/evidence contract/CI
```

## Task List

### Phase 1: Release and native scanning foundation

- [x] Task 1: Prepare the 4.4.12 version surface and ADR.
  - Acceptance: every canonical release surface resolves to 4.4.12; the ADR records the O(delta) head-pointer decision and pack trust boundary.
  - Verification: `python scripts/check_release_version.py --require-release-note` after the release note exists.
  - Files: `VERSION`, release surfaces, `docs/adr/`, `docs/releases/`.
- [x] Task 2: Bound and stream native batch scanning.
  - Acceptance: one reusable `Popen` helper streams JSONL results; worker count and estimated working set are bounded; item failure falls back per file.
  - Verification: focused Python contracts plus `cargo test -p deepseek-backup` in Rust-capable CI.
  - Files: `backup_chunk_engine.py`, Rust helper, focused tests.

### Checkpoint: Native foundation

- [x] Python focused tests pass.
- [x] Rust format/check pass locally; Rust tests remain delegated to CI because local MSVC is unavailable.

### Phase 2: Persistent snapshot state

- [x] Task 3: Add immutable file versions and delta snapshot operations.
  - Acceptance: Full stores all PUTs; Incremental stores only changed/deleted paths; equal content shares file versions.
  - Verification: row-growth, rename sharing, and historical reconstruction tests.
  - Files: `backup_incremental.py`, executor integration, focused tests.
- [x] Task 4: Maintain one atomic current effective view and migrate legacy indexes.
  - Acceptance: head, lineage, ops, versions, maps, and current view commit in one transaction; head mismatch marks stale and forces Full; legacy v2 state migrates deterministically.
  - Verification: crash/rollback, migration, current-head invariant, and Full-rebuild tests.
  - Files: index module, executor, fixtures/tests.

### Checkpoint: Index v3

- [x] Existing 4.4.8-4.4.11 index contracts pass.
- [x] Synthetic 100k-file state proves one changed file creates one I1 operation and less than 1% DB growth; the schema property extends independently of workspace size.

### Phase 3: Packed incremental container

- [x] Task 5: Implement the immutable PackWriter and index.
  - Acceptance: aligned ranges, deterministic ids, 64/72 MiB bounds, per-pack and per-entry SHA-256, no per-blob staging files.
  - Verification: boundary, alignment, corruption, and entry-count tests.
  - Files: new pack module and focused tests.
- [x] Task 6: Emit `incremental-v5` from the scheduled builder.
  - Acceptance: CDC payloads always pack; whole payloads at most 16 MiB pack; larger whole payloads are typed standalone refs; parent reuse is unchanged.
  - Verification: builder archive inspection and privacy-safe packing metrics.
  - Files: builder, package model, builder contracts.
- [x] Task 7: Restore and validate pack ranges.
  - Acceptance: bounded four-handle cache, pack digest once per pack, blob digest per range, file/Merkle checks, v2-v4 unchanged.
  - Verification: byte-for-byte mixed restore and fail-closed tamper tests.
  - Files: restore module, archive validator, restore contracts.

### Checkpoint: Container v5

- [x] Incremental v2-v5 restore compatibility passes.
- [x] 100k logical blobs produce one pack plus one index in the deterministic scale contract.

### Phase 4: Maintenance, scale, and real S3

- [x] Task 8: Add GC, index metrics, and incremental compaction.
  - Acceptance: live versions/maps survive; unreferenced state is removed; metrics contain no path/hash; scheduler path never full-vacuums.
  - Verification: GC/compaction/effective-view tests.
  - Files: index module, executor/retention integration, tests.
- [x] Task 9: Add scale benchmark contracts and a real HTTP MinIO E2E job.
  - Acceptance: packed Full→Incremental→multipart restart→restore works byte-for-byte over HTTP S3; 100k logical changes avoid entry explosion.
  - Verification: local synthetic contract plus dedicated CI service job.
  - Files: integration test/script, CI workflow, evidence assembly inputs.

### Phase 5: Release and review

- [x] Task 10: Update all release-facing documentation and evidence contracts.
- [ ] Task 11: Run multi-axis review, security checks, full Python/frontend/Rust/docs gates, publish the branch and converge CI.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Head/view divergence after crash | Incorrect parent lookup | One `BEGIN IMMEDIATE` commit and explicit head/root invariant; stale→Full on mismatch |
| Malicious pack offsets or paths | Read outside package or corrupted restore | Canonical relative-path validation, integer/bounds limits, layered SHA verification |
| Persistent helper protocol desynchronization | Wrong result assigned to a file | Request ids, exact response cardinality, call-level lock, process reset on malformed output |
| Worker pool exceeds memory | Scheduler pressure | Estimated per-worker working set and bounded worker count derived from configured budget |
| Legacy migration expands locks | Scheduler stall | One additive transaction on local rebuildable cache; failure marks stale and forces Full |
| Pack optimization adds complexity without scale gain | Maintenance cost | Before/after entry-count and index-row-growth contracts; revert neutral optimizations |
| HTTP S3 test becomes flaky | CI noise | Pinned service image, health checks, localhost only, deterministic fixture and retry bounds |

## Open Questions

None. The supplied release specification defines the compatibility, security, and non-goal boundaries.
