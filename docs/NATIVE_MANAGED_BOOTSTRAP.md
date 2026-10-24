# Default native managed bootstrap

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: implementation in progress, NOT_READY. This is part of the complete
Rust/Go product migration in ADR-0049, including server, desktop, Android,
background recovery and all retained features. It does not qualify that migration.

## Default entry and trust

When no private worker/control deployment settings are present, ProductionPlan
selects managed bootstrap automatically. All existing launcher modes use Run,
including the configuration UI. Explicit operator transport or authority settings
keep the existing deployment path; partial settings are not silently replaced.
There is no Python/Node fallback, offline signer command or seeded Go database.

The Go supervisor holds a kernel lifetime lock in go-control/native-profile.
A fresh profile accepts only local authentication and Go launcher configuration
alongside its own metadata. Legacy data, a foreign database, redirected paths,
partial identity files and missing retained state fail closed. A profile ID and
public signer pin are published exclusively; they are never overwritten.

Rust initializes its encrypted Ed25519 signer bundle, passphrase, transport CA,
worker TLS identity and Go-control client TLS identity. Private files are created
in an exclusively created directory with user-private permissions and published
as a directory after syncing complete contents. The parent supplies deployment
scope, not a signing seed. Go reads only the public Rust transport CA PEM and the
ControlSignerBootstrap.GetBinding response over verified TLS plus a bounded
service bearer. The response binds schema, fleet, environment, expected writer
fence, public signing key and derived key ID. Reopening also requires the retained
public pin. Discovery does not sign, admit an action or mutate any journal.

Go creates only its own per-launch TLS server identity, which is distinct from
Rust authority/federation keys. Rust clients authenticate to that server with
Rust-held client keys. Role filters keep worker custody and worker server keys
with the worker, Rust client keys with the Rust worker/gateway, and the Go server
key with deepseekd. The Windows private directories have protected user/System
DACLs; Unix private directories and files use 0700/0600.

The supervisor starts the worker first and resolves its authenticated public
binding before starting Go and the gateway. A read-only SQLite preflight includes
retained WAL when predicting the next writer fencing token. Only OpenControl
claims the writer, and lifecycle startup refuses to serve if its actual token
differs. The prediction is never an ownership proof or a writer claim.

## Android resources and data

Android keeps its original `Context.getFilesDir()` as the authoritative data root.
Bundled `static/`, `skills/` and golden eval inputs are installed under the sibling
`Context.getDir("native-assets", MODE_PRIVATE)` directory; runtime logs use the
separate `native-logs` app directory. Neither is placed inside a fresh data root.
The JVM supplies canonical paths so Android's `/data/user/0` alias cannot redirect
native custody. It does not create, migrate, erase or replace a control database.

The optional `DEEPSEEK_INFRA_BUNDLE_ROOT` selects packaged Skill definitions,
Packs and golden eval inputs. Go requires an existing canonical absolute directory
when it is configured and forwards it to native children. Rust retains
`DEEPSEEK_INFRA_ROOT` for writable stores, custom Skills, runs, user eval cases and
mutation fences. Existing explicit Skill-directory overrides retain precedence;
unset or empty bundle configuration retains the previous resource layout.

Separating resources does not admit legacy or foreign data. A nonempty data root
without valid native provenance still fails closed and requires the existing
signed migration path. Actual packaged device tests must prove resource reads,
custom Skill persistence and unchanged golden bytes across restart; host tests
and an APK build alone do not qualify Android. The full migration remains
`NOT_READY`, including all original coverage floors and platform requirements.

## Recovery boundaries

An existing Go database or public pin never triggers Rust custody creation.
Missing keys, a lost Go database, inconsistent pins or abandoned partial custody
stay failures. Existing encrypted custody is validated and retained. The startup
path does not reset effect journals, synthesize recovery receipts, reissue an
operation under a new epoch, or reinterpret EFFECT_UNKNOWN as unapplied.

The existing one-hour maximum service bearer lifetime remains unchanged. Managed
launch currently issues a 55-minute transport credential. Automatic safe renewal
for long-running background execution is still required; do not extend or ignore
the expiry as a substitute. Go transport files are removed only after the owned
process tree settles. Persisted Rust custody is retained.

## Required acceptance and remaining work

The required empty-profile process gate must ultimately show all three real
native binaries, private authenticated transport, four actual controlled provider
requests, an awaiting-plan boundary retained across a whole-launcher restart,
advancing Go writer fencing and identical original recovery identities. A Go
fixture process test is useful regression evidence but does not qualify the
actual Rust process, a device, or a production provider.

Fresh native-origin domain genesis is not implemented by this bootstrap slice.
The existing Python-source migration attestation and per-domain signed cutover
requirements remain enforced. In particular, an empty inventory does not create
fake Python provenance. Current default public Agent mutations can therefore
still return a domain-authority refusal after transport and custody initialize.
A separate native-origin proof path, complete recovery of initialization failures,
credential renewal and exact-source desktop/Android/background acceptance remain
necessary before product readiness can change from NOT_READY.

All native compilation and execution remain on GitHub Actions for this host.
Collect source-matched reports only, preserve original 80% Rust / 95% Go and
Python coverage floors, and distinguish workflow_dispatch source validation,
push workflows and a future PR merge candidate. No caches, images or volumes are
downloaded. Tests of this slice do not remove any existing required gate.

Reference APIs: [rcgen 0.13.2](https://docs.rs/rcgen/0.13.2/rcgen/)
and [Go X.509](https://pkg.go.dev/crypto/x509).


The first cloud execution of this source (d1422808) passed all 101 top-level
config/lifecycle/launch tests, including authenticated fixture discovery and a
whole-supervisor restart that retained the public pin and advanced the real Go
writer fence. It did not qualify the Rust process. Its focused preview gate
correctly exposed that the older shared Python-path denylist omitted core Python
capability directories such as .memory. Both preview and writer now reject those
paths before creation. The original .memory assertion is retained and an actual
writer/no-created-files regression covers representative capability directories.
Six Clippy needless-borrow errors in the new environment lookup are also removed
without suppressions or lint changes. Cloud execution of this descendant is
required; the earlier successes are not evidence for its exact revision.


## Android private public-value publication

Android app storage can deny hard links, while the minimum supported API 24
kernel cannot be assumed to implement `renameat2`. The three small public values
under `go-control/native-profile` (`profile-id`, `signer-public-pin`, and
`signer-public-preparation`) use the versioned `.values-v1` layout on Android.
Other platforms retain their exclusive regular-file publication. Existing regular
profile files remain readable without conversion.

A publisher writes an exclusive 0600 record named
`<scope>.<payload-sha256>.<random-128-bit-suffix>`, syncs and closes the record,
and syncs the private pool and owned directory entries before creating the
relative head with exclusive `symlinkat`. It syncs the head directory before
acknowledging publication. An existing file, directory, symlink or damaged head
is never replaced. A same-value replay only reads the retained value. Records
survive ambiguous errors and process death; they are never unconditionally
removed after a head has referenced them.

The shared Go reader recognizes only these three exact paths, a single relative
`.values-v1/<record>` target, matching scope/hash, a private current-owner directory
and regular current-owner 0600-or-stricter record without extra hard links. Descriptor-relative
opens refuse nested symlinks, unsafe modes, nonregular/oversized records, absolute
or escaping references, wrong-scope hashes, and missing records. A damaged head
is invalid; it never becomes `not found`. Rust custody and general metadata readers
retain their existing symlink denial. A content hash is an integrity check, not
proof of native authority: the original signed Rust origin, public binding and
Go writer admission remain mandatory.

Before the first head exists, a bounded pool of at most 255 private profile-id
stages may be left by interrupted pre-admission publication. The bytes remain
non-authoritative and are preserved. Such stages cannot admit a fresh profile
when Rust custody, a Go database, Python state, unknown metadata or a pin stage
is retained. Referenced records remain immutable. The relative layout permits a
complete data-root move without changing its identity. Authentication token
creation and the Rust mirror's regular-file handoff are separate paths and are
not changed by this metadata format.

Cloud regressions exercise real hard-link denial plus unavailable `renameat2`,
six actual SIGKILL boundaries, concurrent publication, unsafe/foreign pointers,
regular-file compatibility, relative-layout restoration, and original signed
intent prediction. A deliberate old hard-link implementation must fail the
positive kernel regression. These host results do not qualify packaged Android:
all original API 24/26/28/30/34 device gates and original coverage floors still
apply. Product readiness remains NOT_READY.

The exclusion and durability rules follow the Linux
[symlinkat contract](https://man7.org/linux/man-pages/man2/symlink.2.html)
and [file/directory fsync contract](https://man7.org/linux/man-pages/man2/fsync.2.html).

A visible Android head is not by itself an acknowledged durable publication.
An I/O error after head creation preserves that head and its immutable record;
it must not mint a replacement identity. Before admitting native effects, the
launcher reopens and validates each retained public metadata value, then syncs
its held record, pool, metadata directory and owned root ancestors. Replaying
the same value, including a concurrent publication winner, requires the same
barrier. A failed barrier retains the original bytes and prevents Rust-state
creation. The read-only Go writer preview performs no sync or repair.

Host regressions deny actual `fsync` and descriptor reads with per-child kernel
filters, exercise real permission failures, and restore I/O before admitting
the original identity. A temporary copy with only the launcher barrier removed
must fail the admission regression. Successful instrumented child probes emit
their own coverage counters; killed children are not represented by synthetic
counters. These checks do not prove physical power-loss recovery or Windows
directory durability, and they do not replace the Android device gates.
