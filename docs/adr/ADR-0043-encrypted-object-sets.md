# ADR-0043: Encrypted object sets and true selective fetch

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

- Status: Approved
- Date: 2026-08-11
- Target: `4.4.14`
- Approver: leizd

## Context

4.4.13 projected payload materialization only after every Whole-Age ancestor had already been downloaded. Full snapshots also bypassed projection, project selection was validated against F0 instead of the verified final state, and adaptive deltas first retained the complete plaintext ZIP in memory. Network cost therefore remained proportional to the whole lineage, and a large adaptive candidate could exhaust RAM before the policy chose Full.

## Decision

1. Full and Incremental restores use one member/metadata/projection/materialization pipeline. Project selection is validated only after applying and Merkle-verifying the complete target chain.
2. Adaptive candidates stream into a bounded, fsynced temporary archive. `DeltaCostExceeded` aborts ZIP production before Incremental Age encryption and resolves the frozen run plan to Full.
3. New lineages use `storageProtocol=object-set-v1` and begin with a forced Full checkpoint. Legacy `whole-age-v1` receipts and restore paths remain supported.
4. Every snapshot contains one independently randomized Age-encrypted Control and independently randomized payload components. Full payloads are grouped by Contributor/Project recovery boundary and then by an approximately 64 MiB plaintext target; incremental pack/standalone payloads map to components without cross-snapshot references.
5. Public object keys use ciphertext SHA-256 only. Receipt v4 exposes a role-blind sorted inventory of ciphertext digest/size pairs plus `controlObjectDigest` and `objectSetDigest`. Commit v4 binds those commitments and the Receipt digest.
6. Plaintext paths, Project/Contributor identity, plaintext/Chunk/Pack hashes, component roles and the Full Payload Map exist only inside the authenticated encrypted Control.
7. Restore holds every committed lineage member, fetches Controls first, verifies the logical chain, computes Output/Support/required-component closure, and permits payload GET only for that required set.
8. Durable spool and restore sessions reuse exact ciphertext and per-component multipart/fetch progress across process exits. Retention marks every recoverable member; incomplete transaction objects become collectible only after orphan grace.
9. Pack Index v2 stores compact `[packOrdinal, offset, length]` rows and relies on the operation/file layer for Blob SHA commitments instead of duplicating them.

## Security and privacy

Object sets deliberately reveal more coarse metadata than Whole-Age: the remote target can observe ciphertext component count and individual ciphertext sizes. It still cannot observe logical path, Project, Contributor, component role, plaintext SHA, Chunk SHA or plaintext equality. Components use fresh Age randomness; deterministic/convergent encryption and plaintext-derived object keys are forbidden. Privacy padding is deferred.

Missing committed members, foreign Control references, digest/size disagreement, unsafe component paths and overlapping payload entries fail closed. Active restore holds protect the entire exact object set. A failed Receipt/Commit never makes an uploaded object visible, and GC never deletes it immediately; orphan grace and committed/held-set marking arbitrate reclamation.

## Compatibility and non-goals

Whole-Age Full and Incremental v2-v5 backups remain restorable. This release does not add cross-Backup/Policy/Target CAS, global remote deduplication, deterministic Age, random Range decryption of Age ciphertext, historical references beyond the immediate parent, WAL incrementals, WebDAV GA, padding, prefetch, parallel download or a local encrypted component cache.
