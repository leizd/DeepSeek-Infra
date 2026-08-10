# ADR-0041: Packed delta payloads and persistent snapshot state

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

- Status: Approved
- Date: 2026-08-10
- Target: `4.4.12`
- Approver: leizd

## Context

4.4.11 made chunk maps immutable and reusable, but each incremental snapshot still materialized the complete file and chunk-reference view in SQLite. A workspace with hundreds of thousands of files therefore grew local index state with `snapshot count × workspace size`. Unmatched delta blobs were also staged as one file and one ZIP entry per logical blob, producing excessive filesystem and archive metadata work after deduplication became effective.

The native batch helper removed repeated process startup, but its captured serial batch bypassed Python's byte-budget scheduling and did not stream completed results.

## Decision

1. Store immutable content-addressed `file_versions`, per-snapshot `snapshot_file_ops`, one `current_effective_files` materialization, and one `current_effective_heads` generation row per target/policy.
2. Keep the generation in the single head row instead of stamping it onto every effective-file row. This preserves the invariant that the current view belongs to the latest committed snapshot without turning every incremental commit into an O(workspace) update.
3. Migrate existing materialized index rows additively. Keep legacy readers compatible during the release, but production commits use Index v3 and historical views reconstruct from a Full plus bounded deltas.
4. Introduce `incremental-v5`. Every unmatched CDC chunk and whole-file payload at most 16 MiB is streamed into snapshot-local immutable packs. Packs target 64 MiB, never exceed 72 MiB, and align entries to 8 bytes. Larger whole files remain standalone.
5. Keep pack metadata inside the encrypted archive. A pack never references another snapshot, target, or policy.
6. Restore validates package paths and types, Pack SHA-256, range bounds, Blob SHA-256, File SHA-256, and Snapshot Merkle root. At most four pack handles remain open.
7. Run native scan batches through one reusable JSONL process and a bounded Rust worker pool. Budget estimated working set, not logical file length.
8. Run only thresholded incremental vacuum outside correctness-critical work. Never run full `VACUUM` in a scheduler commit.

## Compatibility invariants

- `fastcdc-gear-v3` remains the current chunk protocol.
- Incremental v2, v3, and v4 remain restorable.
- A 4.4.11 parent can have a v5 incremental child without forcing a Full.
- Parent dependencies remain limited to the immediate parent.
- Packfiles do not introduce cloud chunk objects, convergent encryption, cross-policy/target reuse, WAL shipping, or WebDAV.

## Security and failure model

The encrypted object digest is the outer integrity layer. Pack digest, blob-range digest, file digest, and Snapshot Merkle root form nested fail-closed layers after decryption. Pack index input is untrusted and cannot select absolute paths, traversal paths, negative or Boolean offsets, overlapping out-of-bounds ranges, undeclared files, or mismatched lengths/digests. No plaintext content hash or Pack Index is written to remote metadata.

Index state remains a rebuildable performance cache. Any migration, head, root, or atomic-commit inconsistency writes a durable stale marker and forces a new Full; it never changes an already committed encrypted backup.

## Consequences

- Incremental index growth becomes proportional to changed and deleted paths while the latest-parent lookup remains materialized and fast.
- Thousands of logical payload blobs become a small bounded set of pack and ZIP entries.
- Historical local reads perform bounded delta replay rather than one direct materialized-view query.
- The implementation adds a pack reader/writer and native helper lifecycle that require explicit corruption, restart, and resource-bound tests.

## Rejected alternatives

- Updating `backup_id` on every current-effective row: rejected because it recreates O(workspace) writes.
- Cross-snapshot packs or cloud Chunk CAS: rejected because they expand the retention dependency graph and leak new metadata surfaces.
- Dynamic pack sizing: rejected until fixed-size measurements demonstrate a need.
- Full `VACUUM` after each backup: rejected because it can block the scheduler for large databases.
- CDC v4: rejected because payload packing does not change content-defined chunk boundaries.
