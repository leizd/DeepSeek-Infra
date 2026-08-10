# Implementation Plan: 4.4.13 Projected Recovery & Production Remote Restore

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

## Overview

4.4.12 made incremental chain restores fast and correct. 4.4.13 lets a user restore only a selected Contributor / Project instead of the whole Workspace, and replaces the MinIO E2E's assembled stub with a real production Backup → Age → S3 → Receipt → Restore → Federated Commit chain.

The restore is frozen into an explicit `selection` whose `selectionDigest` is durable and immutable across retries. Cross-file `parent-range` dependencies automatically enter a read-only Support set that is materialized in scratch space but never written to the final tree. Even for a partial restore, the full F0→I1→…→In logical Merkle chain is still verified layer by layer. Because this release keeps the whole-age-object network model, every preview/API surface reports `networkSelective: false` and never claims network-level selective fetch.

## Architecture Decisions

- Freeze `selectionDigest` at session creation; a resume whose digest differs returns `409 restore-selection-mismatch`. Selection is durable frozen state, like a backup run plan.
- Scope projection granularity by contributor capability: `projects → project`, every other contributor → `contributor`. No arbitrary glob / path / JSON-subtree selection.
- Keep `restoreOutputSet` and `restoreDependencySet` strictly separate. Support files are verified but never enter the prepared final mutation list.
- Metadata plane stays full (logical PUT/DELETE chain + every Merkle root); only payload-byte materialization is projected.
- Extract selectively: read only `manifest.json`, `delta/operations.json` and `payload/packs/index.json`, then extract only required Full entries, Packs and standalone blobs.
- Parse the pack index without hashing; verify each pack's size/SHA256 only on first use. A full restore naturally verifies every pack.
- Thread selection into the federated transaction: project-scoped staging inside `projects`, `requiresFrontendApply`/`requiresExternalMcp` derived from selection, `serverTransactionDigest` includes `selectionDigest`.
- Safety backup stays always full; restore may be partial, rollback stays complete.
- Release remote ancestor holds at terminal states; retain them during `recovery-required`.
- Base adaptive-full decisions on packed-container physical bytes instead of raw logical payload bytes.
- Add an index-maintenance migration path that rebuilds and atomically swaps the index DB without a full `VACUUM` on the scheduler path.
- The production remote restore E2E runs through the real executor and the real Rust Age helper against MinIO, including a new-process restart and full federated commit/complete.

## Dependency Graph

```text
Projection core (selection/digest/closure)
        │
Metadata-first extraction ──→ selective extraction ──→ projection-aware materializer
        │                                              │
Selection freeze in remote session ──→ preview endpoint ─┘
        │
Projected federated restore (prepare/commit/rollback)
        │
Frontend/MCP derivation + hold lifecycle
        │
Adaptive-full cost model + index maintenance migration
        │
Real Age MinIO production E2E ──→ evidence + release surfaces
```

## Task List

### Phase 0: Release and projection foundation

- [x] Task 1: Prepare the 4.4.13 version surface and ADR-0042.
  - Acceptance: every canonical release surface resolves to 4.4.13; the ADR records the projection/digest/whole-age-object decisions.
  - Verification: `python scripts/check_release_version.py --strict-branch`.
  - Files: `VERSION`, release surfaces, `docs/adr/`, `docs/releases/`.

### Phase 1: Projection core

- [ ] Task 2: Add the pure projection module.
  - Acceptance: contributor/project granularity validation, canonical `selection_digest`, and full-logical-chain dependency closure (output set, support set, needed packs/blobs) computed from chain metadata; byte report with `networkSelective: false`.
  - Verification: offline unit contracts (digest stability, cross-file parent closure, support/output separation).
  - Files: new `backup_projection.py` + tests.

### Phase 2: Durable selection freeze and preview

- [ ] Task 3: Freeze selection in the remote restore session (schema v3) and reject retries that change it.
  - Acceptance: `create_restore_from_target` persists `selection` + `selectionDigest`; resume with a different selection returns `409 restore-selection-mismatch`.
  - Verification: session-contract tests.
  - Files: `backup_remote_restore.py`, `backup_governance.py` routes, tests.
- [ ] Task 4: Add the from-target preview endpoint.
  - Acceptance: fetch + decrypt + metadata-only extract + projection plan + accurate byte report (`ciphertextDownloadBytes` = whole chain), reusing the session so confirm does not re-download.
  - Verification: preview-contract tests and route coverage.
  - Files: preview function, routes, frontend API client.

### Phase 3: Selective materialization

- [ ] Task 5: Split extraction into metadata-only and selective entry extraction.
  - Acceptance: path/dup/compression validations preserved; only required Full entries, Packs and standalone blobs are extracted.
  - Verification: extraction-contract tests.
  - Files: extraction helpers, restore session, tests.
- [ ] Task 6: Lazy pack verification.
  - Acceptance: `parse_pack_index` (no file I/O) + `verify_pack` on first use; unused packs are not opened or hashed.
  - Verification: open-handle-count and corruption tests.
  - Files: `backup_pack.py`, `PackHandleCache`, tests.
- [ ] Task 7: Projection-aware chain materializer.
  - Acceptance: `projection=None` is byte-identical to 4.4.12; with projection, full logical chain verification is retained, only outputs reach the workspace, support files never do, and selected file SHAs verify.
  - Verification: projected byte-for-byte, unselected-isolation, support-never-written tests.
  - Files: `backup_incremental_restore.py`, tests.

### Phase 4: Projected federated restore

- [ ] Task 8: Projected `prepare_restore`, commit and rollback.
  - Acceptance: only selected contributors/projects are staged and swapped; `serverTransactionDigest` includes `selectionDigest`; project-scoped commit/rollback via the always-full safety backup.
  - Verification: transaction/rollback and retry-digest contracts.
  - Files: `backups.py`, contributor apply, tests.
- [ ] Task 9: Derive frontend/external-MCP participation and complete the hold lifecycle.
  - Acceptance: `requiresFrontendApply`/`requiresExternalMcp` derive from selection; holds release at complete/abort/failed-before-transaction and are retained at `recovery-required`.
  - Verification: gating and hold-lifecycle contracts.
  - Files: `backup_remote_restore.py`, frontend, tests.

### Phase 5: Cost model and maintenance

- [ ] Task 10: Base adaptive full on packed-container physical cost.
  - Acceptance: the executor decision uses physical delta bytes vs estimated full archive bytes, with evidence recorded.
  - Verification: adaptive-ratio contracts.
  - Files: `backup_scheduled.py`, `backup_executor.py`, tests.
- [ ] Task 11: Add the index-maintenance migration path.
  - Acceptance: rebuild → copy live state → verify head/root → fsync → atomic swap, keeping the old DB until success; no full `VACUUM` on the scheduler path.
  - Verification: migration and fail-safe contracts.
  - Files: `backup_incremental.py`, tests.

### Phase 6: Production remote restore E2E and release

- [ ] Task 12: Add the real Age + MinIO production restore E2E and CI wiring.
  - Acceptance: real executor F0/I1, receipts/catalog, slot commit, new-process restart, real Age decrypt, projection, federated commit/complete; workspace equals the I1 snapshot byte-for-byte.
  - Verification: dedicated CI job with the Rust helper built and evidence emitted.
  - Files: E2E test/script, CI workflow, evidence producer.
- [ ] Task 13: Update release-facing documentation, evidence contracts and run the full gates.
  - Verification: full Python/frontend/Rust/docs gates, coverage ≥ 95%, exact-merge evidence.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Projection closure misses a cross-file parent | Corrupt or missing restore | Backward dependency walk over the whole logical chain; support files verified before use |
| Selection changes mid-restore | Wrong scope restored | Immutable `selectionDigest` + 409 mismatch on resume |
| Selective materialization weakens integrity | Unverified partial restore | Metadata plane stays full: every Merkle root verified; only payload bytes are projected |
| Selective extraction drops validation | Malicious archive entry restored | Keep all path/dup/compression validations from `_safe_extract_and_verify` |
| Support files leak into the workspace | Mutated unselected state | MaterializedNode `role` tracking; only outputs enter the final mutation list |
| Real Age E2E is flaky in CI | CI noise | Pinned MinIO image, health checks, localhost only, real Rust helper built once |
| Packed-cost adaptive decision regresses | Wasted full backups | Evidence records container bytes basis; ratio contracts unchanged in semantics |

## Open Questions

None. The supplied release specification defines the compatibility, security, and non-goal boundaries (network-level selective fetch is explicitly deferred to 4.4.14+).
