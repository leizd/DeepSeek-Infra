# ADR-0042: Projected recovery and production remote restore

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

- Status: Approved
- Date: 2026-08-10
- Target: `4.4.13`
- Approver: leizd

## Context

4.4.12 made incremental chain restores fast and correct, but every restore — local or remote — materialized the complete snapshot. `prepare_restore()` staged every contributor and created a full safety backup; remote restore still decrypted and extracted the entire Age-protected archive and downloaded every ancestor ciphertext object. A user wanting only `projects/proj-a` still had to download and materialize the whole chain.

The MinIO E2E validated real S3, boto3, multipart resume and the pack materializer, but its Age layer was a byte-reversal stub. The executor, writer-lease/slot commit, receipt/catalog and `create_restore_from_target` → federated-complete path were not exercised end to end, so the "Production Remote Restore" claim was not yet executable.

## Decision

1. Freeze every remote restore into an explicit `selection` (`contributors` + `projectIds`) at session creation. Persist a canonical `selectionDigest`; any resume whose digest differs fails with `409 restore-selection-mismatch`. Selection is durable frozen state, exactly like a backup run plan.
2. Scope projection granularity by contributor capability: `projects → project`, every other contributor (including `frontend` and `stateless-mcp`) → `contributor`. No arbitrary glob, path or JSON-subtree selection in this release.
3. Separate `restoreOutputSet` (final writes) from `restoreDependencySet` (read-only support). Cross-file `parent-range` / `parent-file` / CDC-parent dependencies close backward through the chain into the support set; support files are materialized and verified in scratch space but never enter the prepared final mutation list.
4. Keep the metadata plane full: apply the complete logical PUT/DELETE chain and verify every Merkle `rootDigest`. Only payload-byte materialization is projected.
5. Keep the whole-age-object network model. Report `networkSelective: false` and `networkSelectivityReason: "whole-age-object"` in every preview/API surface. Do not claim network-level selective fetch.
6. Extract selectively: read only `manifest.json`, `delta/operations.json` and `payload/packs/index.json`, then extract only required Full entries, required Packs and required standalone blobs. Parse the pack index without hashing; verify a pack's size/SHA256 only on first use.
7. Thread selection into the federated transaction: `prepare_restore` stages only selected contributors (project-scoped staging inside the `projects` contributor), `requiresFrontendApply`/`requiresExternalMcp` derive from selection, and `serverTransactionDigest` includes `selectionDigest`.
8. Keep the safety backup always full. Restore may be partial; rollback stays complete.
9. Release remote ancestor holds at complete/abort/failed-before-transaction; retain them during `recovery-required`. TTL remains the backstop.
10. Base adaptive-full decisions on packed-container physical bytes (packs + index + standalone + operations + ZIP overhead) rather than raw logical payload bytes.
11. Add a production remote restore E2E driven through the real executor and the real Rust Age helper against MinIO, including a new-process restart and full federated commit/complete.

## Compatibility invariants

- `selectionDigest` is a digest of canonicalized selection only; it never depends on file hashes or remote metadata.
- `projection=None` restores remain byte-identical to 4.4.12 full restore semantics.
- Incremental v2-v5 restore compatibility is unchanged; `fastcdc-gear-v3` remains the current chunk protocol.
- Pack index schema stays v1; compact per-pack indexes (v2) are deferred to 4.4.14.
- No independent encrypted pack objects, cloud chunk CAS, convergent/deterministic Age, plaintext chunk hashes, cross-policy/target reuse, WAL shipping or WebDAV.

## Security and failure model

The selection digest freeze prevents a restore session from silently switching scope mid-flight (409 on mismatch). Support-only files cannot reach the final workspace because the materializer tracks `role` per node and only commits outputs. Full logical-chain Merkle verification still fails closed on any tampered ancestor even when payload materialization is projected. Whole-age-object ciphertext digests remain the outer integrity layer, and no plaintext content hash or pack index is written to remote metadata.
