# Filesystem target registration

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


Status: **NOT_READY**. This snapshot implements the public existing-marker
filesystem registration path. Its new native tests require cloud qualification;
the complete target lifecycle and whole-product migration remain unfinished.

`POST /api/workspace/backup-targets` requires the existing authentication, host
and Go-authoritative target admissions. Go normalizes retained metadata, obtains
Rust filesystem observations over the production TLS/Protobuf client, and commits
the target, operator result and immutable observation binding in one transaction.
Rust owns the marker read and signing custody; Go receives public keys only.

Public registration requests complete containment explicitly. Rust resolves the
13 built-in contributor/backup locations individually, including links outside
the runtime root, and signs the completed-check flag. A worker without that flag
cannot satisfy a public request. The original basic read mode remains available
for its original purpose and does not imply complete containment.

Go captures a digest of the target registry before the reads. Every competing
filesystem target needs its own signed canonical-path resolution bound to the
same operation, epoch, writer and deployment. Both directions of overlap are
refused. A changed registry snapshot refuses publication. A moved identity also
retains the existing signed old-location existence check and clone refusal.

The Go-only `_nativeTargetRegistrationV1` event context retains the original
normalized document, exact request-body SHA-256, admission scope and typed receipt
bytes. It is removed from public registration, list and activation responses.
Ordinary metadata writes cannot replace or remove it. Historical validation uses
the target events preceding the registration event, so later target deletion or
metadata updates do not rewrite the earlier admission snapshot. No SQLite writer
is shared and this context is not a cross-language JSON bridge.

A profile with a signed native birth starts with an empty scheduler health
history. It can list newly registered targets before its first observation.
Imported profiles still need their authenticated scheduler health transfer.
The list rejects scheduler rows that have no matching source evidence.

Clients may retain the returned `X-DeepSeek-Operation-Id` and reuse it with the
identical request bytes. Go returns the originally committed document after a
lost response, deletion or writer restart, without reading an expired filesystem
receipt or issuing another write. A different body under that operation is a
conflict. Concurrent identical requests recover the same committed result.

The offline metadata oracle executes the retained HTTP conversion function and
`backup_targets._register`, replacing authentication and filesystem effects only
inside the oracle. Canonical comparison requires CPython 3.12 / Unicode 15.0.0;
developer runs on another interpreter are explicitly unqualified. Its 74 cases
cover ordered containers, duplicate keys, integer precision, numeric conversion,
Unicode strings, cost booleans and strict QoS. Frozen Go Unicode classifications
are checked against every Python code point and the official Unicode 15 data.
Python is not required by this production registration path.

Remaining work includes Rust marker initialization with effect journaling and
unknown-result recovery, S3 registration, register-new/adopt/probe/drain lifecycle
parity, malformed/nonstandard marker JSON compatibility, Windows path behavior,
actual public Go-to-Rust process/browser qualification, and the original complete
Go/Rust/platform coverage gates. Marker absence currently returns an explicit
503; S3 registration remains unavailable and the retained WebDAV reservation is
501. These refusals are unfinished functionality, not migration completion.

The development workflow preserves 58 Go test families and checks five more
public admission, relocation, replay and native-profile cases. It runs the retained
CPython oracle, the Rust containment cases, the full Go coverage gate and a
Chromium test that registers existing targets, moves one, and reopens the list
and original operation after launcher restart. The Go fixture signatures qualify
the admission and transport tests. The physical tests exercise Rust filesystem
reads and separately verify public browser behavior. Workflow results identify
the tested source and event. The original coverage floors still apply.
