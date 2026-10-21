# New native profile initialization

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


This implements the new-profile path of ADR-0049. Existing Python or Go stores
retain the signed export/import/promotion procedure, including attestation of an
empty Python inventory. A mode flag, empty SQLite file, missing database, or
successful TLS connection never establishes native origin.

An actually empty managed Rust directory receives an immutable v2 custody
manifest and a typed Ed25519 birth certificate. The certificate binds a fresh
incarnation, profile, fleet, signer, original initialization action/epoch 1 and
the digest of a fixed empty generation-one `control-authority-v1` checkpoint.
The seven custody files and passphrase remain private to Rust. A v1 manifest is
readable but never gains a birth certificate on upgrade.

Go reads this certificate over the authenticated private worker TLS service and
independently verifies its purpose, signature, fixed checkpoint and deployment
pin. Before exclusively creating the database it durably records the exact
certificate as a creation intent. A preexisting empty database without that
intent is refused. Schema 18 adds an immutable origin row; older schemas upgrade
with no origin row. Initial native cutover events identify Go as the original
owner, remain non-authoritative, and make no claim about a previous Python
inventory. Every legacy promotion check remains in force for legacy domains.

The existing Go authority-claim path installs the fixed checkpoint. Go then asks
Rust to persist a publication receipt for that exact birth certificate. The RPC
does not accept arbitrary documents, timestamps, epochs, digests to sign, or
private keys. Only after verification of the retained receipt does a fenced Go
transaction activate the twelve native domains and retain their provenance.
The control listener does not serve business requests before this completes.

The supervisor starts worker, control, then edge in that order. It waits for
the existing Go loopback health endpoint to return the actual expected writer
fence and owner before starting the public edge. A stale process, an unavailable
endpoint, or a different writer cannot satisfy this readiness observation.
This Go-only startup read does not grant authority or replace private Go/Rust
TLS RPC admission. A delayed real control subprocess is tested with an endpoint
that refuses readiness until explicitly released; the gateway must stay absent
while that endpoint is unavailable.

Managed startup also configures the edge's existing private TLS credential
delivery to that same Rust worker and enables the worker's Agent executor.
Both Rust processes use the profile's `.native-agent-artifacts` directory in
`python_disabled` mode. Provider fallback keys reach these Rust roles only;
Go receives neither the body-custody settings nor the credential-delivery
configuration. The request's sealed credential continues to override the
configured server fallback. This wiring does not relax grant or epoch checks.

The Rust publication receipt is immutable. Reads never mint certificates or
signatures. A lost response is recovered by reading the original receipt using
the successor's current writer fence, retaining the original birth action,
epoch, incarnation and checkpoint. A committed Rust publication with a missing
Go database is a loss of authoritative state, not permission to create an empty
replacement. Missing or inconsistent custody, intent or origin records fail
closed. Interrupted preparation may resume only the original retained intent.

Required qualification includes actual empty-profile launcher/Go/worker/edge
startup, four controlled provider requests through the public Agent API, whole
launcher restart at the awaiting-plan boundary, writer takeover, lost-response
recovery, and refusal of existing/foreign/lost state. This path is not release
qualification: full exact-source CI, original coverage floors, retained product
features, desktop/Android/background acceptance and Evidence Assembly remain
required. Readiness remains `NOT_READY` until the full migration is proved.
