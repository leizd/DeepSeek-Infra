# Native media creation

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

This work retains the complete media migration requirement. JSON registration,
multipart uploads, processing, OCR, segments, citations, indexing, platform
packaging, backup/restore and recovery all remain required. The first creation
implementation is awaiting its own native cloud run; it is not a completed media
or whole-product migration. Production readiness remains `NOT_READY`.

## Authority and content

The exact Go `POST /api/media` handler checks public host, authentication, Go
authority and `python_disabled` before admission. JSON keeps its original
16,000,000-byte HTTP bound. A new random operation and original action/epoch bind
the generated `media_` identifier. Go retains only a typed SHA-256/length manifest
in its existing control journal. It never writes `.media` or Rust's staging.

The separate versioned `MediaCreationPayloads.Stage` RPC transfers typed Protobuf
content in chunks of at most 256 KiB over the pinned authenticated service TLS
channel. Rust obtains the current Go claim before receiving content and checks
it again before retaining any staged payload. Every chunk retains the original
operation and fence. Content, source and result files stay in Rust custody.
The 128 MiB bound is for the typed staging representation, not a replacement for
the original public JSON bound. Historical PATCH/DELETE/control message limits
and SQLite row bounds remain unchanged.

Rust normalizes the original six JSON media kinds and prepares source bytes,
the public record and library after-image before requesting Go's dispatch
permit. The shared library admission barrier covers PATCH, DELETE and CREATE.
Only a fresh original permit allows publication. A signed compact CREATE result
binds the original action/epoch, request, library before/after digests, exact
public-record bytes and exact Rust preparation. Go verifies it before settling.
The separate result read must match the signed record digest and length; large
records do not become oversized control receipts. Private keys stay in Rust.

## Retained effects

JSON webpage HTML uses `objects/<mediaId>/source.html`; imported text or
transcripts use `source.txt`. Creation time, metadata normalization, original
key order for collisions and JSON scalar types are retained. Registration does
not imply that processing or OCR has run.

A Rust journal row preserves the original preparation before publication. If
the library publication is confirmed but source persistence or reply delivery
fails, the original operation remains unresolved. Query can finish only that
bound source and settle its original signed result. It cannot mint another
action, replace an epoch, or retry an uncertain publication. A terminal receipt
is replayed without recreating or overwriting a later media-ID reuse.

Private content/preparation records are retained for audit and reconciliation.
They are not compiler caches. Their later retention, backup, controlled cleanup
and ownership handback need their own gates; no cleanup may invalidate an
unresolved operation or its original recovery evidence.

## Verification and remaining work

The focused cloud workflow requires the complete original selected Go media and
migration test set plus creation tests, with the race detector and exact test
inventory. Rust media unit/journal tests and a real default native-launcher HTTP
workload are separate requirements. The HTTP workload independently executes the
original ASGI route, checks all six JSON registration kinds, imported source
bytes, a record larger than 4 MiB, public reads and restart retention. Its
comparison distinguishes JSON `false`, integer zero and floating-point zero.
Offline Python is a reference observer only and is absent from production paths.

Multipart ingest and truthy `process` are still unfinished in this revision;
they are not accepted as successful processing. The original boundaries remain
50 MiB per media file, at most 20 uploads, 220,000,000 bytes per multipart request
and the original general per-file bound. These capabilities must be implemented,
including failed processing records with any retained effects, before media
creation can pass full parity. Deep/extreme JSON shapes, external OCR engines,
real-process CREATE crash/response-loss recovery, backup/restore, Windows,
Android and exact-source full CI/Evidence/release gates remain required.

The focused checks do not qualify the original Rust 80% and Go/Android-Go/Python
95% full coverage gates, any different source commit or any PR merge candidate.
Compilers, Go/Rust caches, Android SDK builds and Docker remain in GitHub Actions;
only reports and checked generated source return to the disk-constrained host.
