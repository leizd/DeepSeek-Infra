# Native control RPC transport

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

The `deepseekd` executable reads the following deployment settings together:

| Setting | Meaning |
| --- | --- |
| `DEEPSEEKD_INTERNAL_RPC_LISTEN` | Separate literal loopback IP and port, for example `127.0.0.1:8091` |
| `DEEPSEEKD_INTERNAL_RPC_TLS_CA_FILE` | PEM trust roots for allowed client transport certificates |
| `DEEPSEEKD_INTERNAL_RPC_TLS_CERT_FILE` | Server transport certificate chain |
| `DEEPSEEKD_INTERNAL_RPC_TLS_KEY_FILE` | Matching server transport private key |
| `DEEPSEEKD_INTERNAL_BEARER` | Existing internal service bearer, at least 32 characters |

All four RPC settings are required if any is supplied. DNS names, public IPs,
wildcard addresses, invalid ports and missing authentication are refused. Port
zero is supported for isolated callers that obtain `Server.InternalRPCAddr()`.
Material and binding errors abort startup before opening the durable Go store.
Ordinary certificate file symlinks remain usable for mounted deployment secrets.

The private listener requires TLS 1.3 and a client certificate chained to its
configured roots. Existing versioned Protobuf services and the loopback/bearer
interceptor remain in place. The client must also verify the server certificate
using its own pinned roots and server name. Transport credentials do not replace
service authentication or authorize an ownership transition.

Public HTTP views continue at `DEEPSEEKD_LISTEN`. When the private listener is
configured, gRPC requests on cleartext HTTP/2 are explicitly refused. With no RPC
settings, the existing authenticated shared-listener qualification behavior is
retained. New production peers must select the private endpoint and TLS client
configuration rather than infer an RPC address from the public HTTP proxy URL.

Both listeners share one process supervisor and one authoritative Go writer.
Cancellation, either listener failing, or loss of the writer lease stops both
listeners before releasing the store. Startup failures close only listeners
owned by that attempt. Restart continues the stored fencing history.

These transport certificates are separate from Rust-held Ed25519 authority and
federation signing keys. No custody bundle or signing private key is read by this
implementation. Existing domain cutover proofs, immutable recovery identities,
`EFFECT_UNKNOWN` behavior and authoritative-writer rules remain unchanged.

The native launcher forwards the server settings only to `deepseekd`. Rust
gateway and worker children instead receive the following complete client set:

| Setting | Meaning |
| --- | --- |
| `DEEPSEEK_GO_CONTROL_RPC_URL` | HTTPS literal loopback address with the same fixed port as the server |
| `DEEPSEEK_GO_CONTROL_RPC_TLS_CA_FILE` | Pinned trust roots for the Go server |
| `DEEPSEEK_GO_CONTROL_RPC_TLS_CERT_FILE` | Rust client transport certificate chain |
| `DEEPSEEK_GO_CONTROL_RPC_TLS_KEY_FILE` | Matching Rust client transport private key |
| `DEEPSEEK_GO_CONTROL_RPC_TLS_SERVER_NAME` | Expected server certificate identity |
| `DEEPSEEK_INTERNAL_BEARER` | Must match the existing Go internal service bearer |

When using the launcher, all nine transport settings must be provided together;
the paired port must be nonzero. Partial or explicitly empty configuration,
public addresses, cleartext URLs, user information, queries, paths and mismatched
bearers fail before child launch. The public `GO_CONTROL_ADDR` proxy address
remains HTTP on the separate public control port. Rust worker recovery and
dispatch clients, gateway Agent Run reads and the chat scheduler use the private
transport without replacing that HTTP proxy. Any explicit client setting
requires all five transport values, bounded nonempty certificate files and a
valid client identity; configuration or TLS authentication failure never retries
through the public HTTP endpoint.

Child environments keep Rust authority custody paths and S3 credentials with the
worker, and model/search API credentials with the gateway. The launcher parent
still constructs these environment entries; this change does not establish a
whole-product credential isolation proof. Transport identity files remain
separate from Ed25519 signing custody and require deployment provisioning.

The worker regression test performs its real recovery-preview RPC through an
isolated mutually authenticated Rust TLS fixture and refuses an untrusted server,
wrong server name, untrusted client, wrong bearer and cleartext. This fixture is
not a Rust-to-Go production-process acceptance result. Go lifecycle tests cover
the actual Go listener separately; the combined default bootstrap, all native
peer paths and exact-source cloud qualification remain required.

Android worker/bootstrap integration, API 24 SQLite compatibility, both device
API levels, ARM64 execution, full product workloads and original CI/Evidence
gates remain incomplete. Overall **NOT_READY**.
