# Compact media PATCH result contract

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

The retained v1 receipt and its 2 MiB signing limit stay frozen. A schema-v2
applied PATCH uses the separate `deepseek-infra:media-patch-outcome-v2` domain
(including its terminating NUL) and a `MediaPatchedResult`; it never contains an
inline record or CREATE/PROCESS/DELETE result. Refusals retain the v1 contract.
The admitted PATCH intent must explicitly select `max_outcome_schema_version=2`;
this capability is included in its canonical request digest and durable intent.
Zero retains legacy behavior. Historical intents are never upgraded on read.

Rust retains the exact deterministic typed public record before the library's
last-1000 retention step. The record-preparation digest binds a typed
`MediaPatchPreparation` containing the normalized original claim (`CLAIMED`),
record SHA-256 and byte length. The dispatch preparation is separately the hash
of the complete unsigned planned outcome. Go must match that dispatch hash to
the recorded permit before settling an applied v2 PATCH. These two digests are
different commitments and cannot substitute for each other.

Authenticated `ReadPatchResult` serves only the original APPLIED operation,
fence, Rust journal receipt and retained record. Its first message carries the
exact signed manifest; later messages carry contiguous offsets and at most
256 KiB of deterministic `MediaObject` bytes. The receiver must verify the
complete signed length, SHA-256, manifest, canonical typed data and EOF before
writing the HTTP success response. No record-size product limit is added by the
stream contract. Transport chunks do not themselves establish bounded total
record parsing memory; that still requires separate measurement and work.

The implementation keeps backing records under a PATCH-specific directory whose
fixed path component is SHA-256 of the complete operation identity. CREATE's
separate 32-hex nonce path validator is unchanged. Backing records are immutable
receipt custody, survive terminal settlement and are checked before publication,
reconciliation and each result read. Missing or corrupt data never authorizes
reconstruction from the current library or another execution attempt.
On Unix, newly created custody directories and their links through the existing
worker state directory are synchronized before PREPARED, followed by the
immutable file and operation-directory synchronization. This source rule and
process-restart tests do not establish power-loss recovery on every platform;
Windows directory durability and platform crash qualification remain open.

The controlled HTTP parent qualification and current bulk candidate are recorded
in [capacity compatibility](NATIVE_MEDIA_CAPACITY_COMPATIBILITY.md). The protocol
alone is not runtime qualification. Bulk library snapshots,
bounded normalization, old unsignable APPLYING recovery, processing/index crash
cases, storage growth, disk-full/lease recovery and exact-source original
coverage, CI, browser, desktop and Android gates remain required. Production
readiness remains `NOT_READY`.
