# Filesystem target registration

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

The development workflow preserves all original 43 Go test families and adds 15
public/store/metadata families, the retained CPython oracle and two Rust complete
containment tests. Fixture signatures qualify only the relevant Go admission and
transport tests. Physical Rust observations, source-dispatch workflows, platform
workflows and any future PR candidate must each be qualified for their exact
source; a success in one is not a success in the others. Original coverage floors
remain unchanged.
