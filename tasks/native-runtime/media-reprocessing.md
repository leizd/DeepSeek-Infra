# Standalone native media reprocessing

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


This is one unfinished part of the complete server, desktop, Android and
background Rust/Go migration. Overall readiness remains **NOT_READY**. Source
implementation, source generation, a scoped runtime probe, full source CI and a
PR merge candidate are separate evidence scopes.

## Contract and ownership

The existing public `POST /api/media/{id}/process` retains its media identity,
source path and creation timestamp. It uses PROCESS kind3 and its own
`media-process-<32 lowercase hex>` operation. Go admits the typed force/OCR
options and log-safe correlation identifier, owns its lease and immutable control
row, and reconciles only that original operation. Rust owns the preparation,
library publication, source read, segment replacement and local index effects.
No CREATE staging, new source publication, new action/epoch during recovery,
shared SQLite writer or production Python fallback is introduced.

The signed processed result binds the original fence, exact typed record and
segment bytes, private preparation digest and index count. A present empty
segment array has zero serialized bytes and the SHA-256 of empty bytes. The
separate retained-result RPC must verify both payloads and the count before a
public response. A processor error with a retained failed record is APPLIED with
the original public error; it is never a no-effect REFUSED result.
Go also requires the APPLIED unsigned receipt digest to equal its original
dispatch preparation digest. Even a valid signature cannot substitute another
private preparation or index count after that permit.

Rust snapshots exact original segment file bytes/presence, the selected index
column codecs and their actual vector rows, and any source bytes used by the
original processor. The source is read-only input. Audio/video and webpages with
imported text do not acquire a new source-file dependency. Processing cannot
recreate a deleted source. The index filter retains the original rule: delete
only collection `media`, the media identity and, when nonempty, its project.
Other projects and collections remain untouched.
The original stored chunk column remains a signed INTEGER in the private
snapshot, including negative legacy values; it is not narrowed to the unsigned
enumeration used for newly generated chunks.

Force first clears the selected index and segments. Success then replaces the
segments and index; a known processing failure retains the failed record and
the original stale count metadata while leaving the force-cleared stores empty.
A non-force processor failure preserves the old segments and index. Disabled
indexing does not gain permission to modify an old index. Configuration captured
in the preparation is not reinterpreted during recovery.

Each SQLite phase uses a FULL transaction and its original preparation marker.
Recovery compares actual stored rows/vectors and exact segment bytes. A replay
of an earlier clear phase cannot erase final rows or segments. Only the exact
original processing/final library witness permits continuing local effects.
A still-old library, conflicting bytes, an unconfirmed effect or a missing
provider result remains unresolved; none authorizes another business request.
Segment replacement uses a temporary file in the same directory, explicit file
and applicable directory synchronization and read-back confirmation. The
[pinned tempfile contract](https://docs.rs/tempfile/3.27.0/tempfile/struct.NamedTempFile.html#method.persist)
requires synchronization in addition to replacement. Physical power loss and
Windows/Android durability still require their own platform evidence.

## Evidence boundaries

- Protocol source `7c1a6b12` and source-generation run41175785006 passed. Four
  reviewed Go/descriptor/manifest outputs were received without tools, binaries
  or caches, and the historical PATCH bytes/digest were preserved.
  Signed-row contract `6e5d9b72` and source-generation run41179849284 also passed,
  including signed64-bit boundary round trips and the same historical digest.
- Parent `c4aae0c0` passed97 scoped Go/race families,47 Rust media tests, six
  actual JSON processing creations and the known failed PDF case. Its actual
  standalone force probe failed. The historical observer did not retain that
  failure's numeric status; no status is inferred as an observation.
- Original full source CI at `4688a38c` passed37 jobs and50 verified Evidence
  artifacts. Default Go is16247/17098=95.022810%, Android Go16566/17436=95.010323%,
  Rust91757/111671=82.167259%, and Python branch coverage83517/87385=95.573611%.
  Both complete Go race corpora were independently checked. This older source
  does not qualify this implementation or any PR merge candidate.
- This implementation adds eight mandatory Go test families, expanding the
  scoped inventory from97 to105 without removing an old test. Rust tests cover
  six media kinds, force/non-force failures, exact before codecs, project and
  collection scope, source/segment/index conflicts, original local phases and
  lost terminal commit. These tests remain unqualified until this source runs
  in the cloud. Synthetic fault phases are not actual SIGKILL evidence.
- The real HTTP observer now retains numeric force failures before assertions,
  independently reads every ready record and force result/segments, and requires
  a whole-launcher restart with unchanged records, segments, index and sources.
  Existing35 multipart cases and56 earlier restart observations stay required.

## Current exact-source evidence and external crash probe

Source `020065e4948f49b21beac2f33ed1e65f9069456f` passed both independent
105-family Go/race corpora (605 tests/subtests and all four packages each),
53 Rust media tests including all six standalone PROCESS families, and both
qualified targeted retained-source EIO cases. Its complete original15-crate
Rust workflow passed1672 tests, with only the existing disposable-Redis ignore;
independently summed LCOV is94389/114522=82.419971708493%, above the original80%
floor. Its actual six JSON processing creations, six standalone force200
responses, independent public reads and complete launcher processing restart
passed while preserving the existing35 multipart/56 earlier read observations.
The full source CI is [run41195790266](https://github.com/leizd/DeepSeek-Infra/actions/runs/41195790266);
it remains a separate qualification and is not a PR merge-candidate check.

The external PROCESS recovery observer adds24 cloud-only cases: all six media
kinds on SQLite and SQLiteVec, at both the intermediate processing-library and
final-library publication. A calibrated official tracer delays the actual
post-rename sync return for five seconds without substituting a return value.
The observer kills the real native process group, then requires autonomous
settlement of the same original action/epoch, claim, plan and signed outcome;
no second public write is sent. Source bytes/inode/mtime, foreign records and
segments, unrelated exact index columns and namespace metadata must survive.
SQLiteVec checks actual vector bytes and scalar bindings. Public record/segment
reads must match the independent original ASGI oracle, and a further complete
restart must preserve files, index and settled receipt without reprocessing.
These new actual crash cases remain **CLOUD_PENDING** until this exact candidate
runs. Offline observer tests do not qualify a native crash or SQLiteVec HTTP.

This probe does not yet cover every clear/segment/index commit boundary,
physical power loss, Windows/Android durability, all configuration/provider/OCR
paths or the entire processing pipeline. Those requirements remain open.

## Remaining acceptance

Exact-source compilation,105 Go/race families, all Rust tests and original
coverage floors, actual force/restart and process-kill reconciliation are still
required. Native SQLiteVec HTTP/configuration/disabled-index behavior, binary
PDF/OCR, hosted OCR credential overrides and telemetry, embedding providers,
original errors/capacity behavior and backup interactions remain required.
Caller OCR credential overrides are explicitly unavailable until a separate
Rust-custody transport exists; keys are neither silently ignored for requested
OCR nor put in immutable Go intents.

The full production ownership/cutover, server/desktop/Android/background feature
matrix, MCP test engine, zero-Python workloads, platform/provider recovery,
security, performance, exact source CI/Evidence and release requirements remain
open. The user has retained the original MCP test-engine requirement without a
Python exception. Builds/tests/images remain in GitHub Actions; the local dirty
workspace is preserved. No main merge, release, production data operation or PR
outside the scheduled daily09:00 batch is part of this change.
