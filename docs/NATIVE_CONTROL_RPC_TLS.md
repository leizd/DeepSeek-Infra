# Native control RPC transport

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

This is a production lifecycle transport change, not whole-product acceptance.
Launcher-specific environment isolation, Rust client endpoint selection, Android
worker/bootstrap integration, both device API levels, ARM64 execution and all
original CI/Evidence gates still require qualification. Overall **NOT_READY**.
