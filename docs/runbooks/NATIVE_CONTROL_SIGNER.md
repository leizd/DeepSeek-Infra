# Native control signer custody

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->


The authority-configured Rust worker now owns an independent Ed25519 control
signer. Authoritative Go storage execution obtains an epoch document and a
placement-bound PUT grant through the typed `action.v1.ControlSigner` gRPC
service, after recording its durable claim. Go receives public metadata and
signed documents; it does not read the encrypted bundle or passphrase.

This is a locally qualified native storage path. It does not promote a domain,
transfer existing Python stores, implement Fleet online signer provisioning, or
complete the 5.0 product/platform migration. Readiness remains `NOT_READY`.

## Provisioning and startup

Prepare an operator-controlled directory and a protected credential file. The
credential is the exact 16–1024 bytes in that file, with no NUL; a trailing newline
is part of the credential. Keep the directory and all ancestors inaccessible to
untrusted writers. On Windows, apply the deployment's restricted file ACLs; the
initializer sets mode `0600` on Unix but does not provision Windows ACLs.

Set these four environment variables for the native initializer:

| Variable | Value |
| --- | --- |
| `DEEPSEEK_WORKER_CONTROL_SIGNER_BUNDLE_FILE` | New encrypted bundle file path |
| `DEEPSEEK_WORKER_CONTROL_SIGNER_PASSPHRASE_FILE` | Existing protected credential file path |
| `DEEPSEEK_WORKER_AUTHORITY_FLEET_ID` | Deployment Fleet identifier |
| `DEEPSEEK_WORKER_AUTHORITY_ENVIRONMENT` | Deployment environment identifier |

Run `deepseek-control-signer-init` (`.exe` on Windows). It generates the random
key inside Rust and writes the existing Argon2id/AES-256-GCM encrypted envelope
with a control-specific public binding. It refuses an existing output file,
syncs the bundle before success, and prints only public binding metadata. The
worker image includes this initializer. Preserve the bundle and credential
through the deployment's protected backup procedure.

Pin the printed `signerPublicKey` as
`DEEPSEEK_WORKER_AUTHORITY_SIGNER_PUBLIC_KEY` in both Go and Rust. Keep Fleet,
environment, positive writer fencing token, and `DEEPSEEK_WORKER_STATE_ROOT`
consistent with the existing worker authority configuration. Configure the
worker TLS identity and the `go-control-plane` / `controller` service credential
as described in [the migration runbook](NATIVE_RUNTIME_MIGRATION.md). Configure
Go's TLS trust/server identity as well. The Go supervisor forwards the two file
paths to its Rust child; file contents do not become RPC arguments.

Both custody paths may be absent for a signer-unconfigured worker. Partial,
empty, invalid or nonmatching configuration, plaintext transport, bad key
binding, and an unavailable or foreign signature journal cause startup refusal.
An RPC without valid service authentication or the required caller identity is
denied. Unconfigured custody never supplies an in-memory signer.

## Execution and recovery

Only `INSTALL_EPOCH` and `STORAGE_PUT` issuance are supported. Rust constructs
the frozen canonical documents from bounded typed fields, validates the complete
signature/scope/timestamp contract, and commits its issuance receipt before
returning. The existing frozen document `mode=shadow` is preserved; actual Go
writer promotion and lease checks remain separate requirements. Signing an
epoch does not install it. The existing installation RPC must accept it before
a storage grant can be issued for that exact action/epoch.

For an unsigned request, authoritative Go execution signs after durable claim,
verifies every returned binding, rechecks ownership and writer authority, then
records dispatch and sends the original payload to Rust. Renewable action/resource
claims also retain their heartbeat during signing. Missing custody, changed
intent, bad TLS, or lease loss prevents provider dispatch. An already signed
request continues through the existing full verification path.

Rust alone writes `rust-worker/control-signatures.sqlite3`, separate from the
Go control database and worker authority/effect journals. Its immutable request
and nonce receipts survive process death. Exact retries reuse the original
signed bytes and timestamps; changed requests or nonce reuse are denied. A
receipt's expiry is never silently extended. If authority has expired, investigate
and renew through the control protocol; do not clear replay history or create a
new provider operation to bypass uncertainty.

Stop the worker and preserve every journal for diagnosis after an interrupted
initialization or integrity refusal. Foreign files and auxiliary symlinks are
refused. Signer/Fleet/environment rotation needs an explicit authenticated
migration; replacing a bundle or deleting a journal is not a supported rollback.
`EFFECT_UNKNOWN` remains blocked from blind retry.

## Qualification

`scripts/run_native_s3_e2e.py` provisions isolated providers, builds the default
worker plus initializer, and uses Rust-generated custody for actual Go-promoted
storage writes. It verifies independent provider bytes, TLS, refused unpromoted
dispatch, renewable-claim writes, Unicode object names, forced worker death,
receipt replay and unchanged object versions on three MinIO instances. Promotion
itself still uses an isolated offline administrative fixture. The leased action
reaches `VERIFYING`; complete production proof settlement, Fleet custody,
rotation, all-platform deployment and full ownership qualification remain open.
