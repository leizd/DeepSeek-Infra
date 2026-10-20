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
