# Native Go-to-Rust worker execution: dependency plan

Status: transport-authentication choice awaiting confirmation. No production
authority is enabled by this plan. Full 5.0 scope remains the accepted specification
in `docs/specs/5.0-native-rust-go-runtime.md`.

## Verified starting point (2026-09-07, 2302574)

- `proto/action/v1/action.proto` exposes admission, effect query and signed epoch
  installation, but no storage execution RPC. Storage request messages exist in
  `proto/storage/v1/storage.proto` without an execution service.
- `go/internal/worker/client.go` dials plaintext loopback. The Rust worker main
  restricts its plaintext listener to loopback, but does not authenticate the Go
  service identity. Loopback placement is not caller authentication.
- The signed install request binds action/epoch, not a particular storage operation.
  `control-mutation-request-v1` is a frozen propose-mutation envelope, not a storage
  execution grant. Neither format should be repurposed by changing its semantics.
- The optional Rust S3 library now binds durable placement/condition and verifies
  bytes during reconciliation. Its gRPC commands still do not dispatch these effects.
- Go `TransitionCutover` rejects production-authoritative transitions with
  `ErrCutoverNotAuthorized`. Removing that rejection is not an authorization protocol.
- ADR-0049 requires Federation and other security private keys to stay in Rust.
  Go signing helper functions currently have only test call sites; they must not
  become the production signing path.

## Authentication decision to confirm

Proposed first boundary: server-authenticated TLS with the server private key in
Rust, plus an explicitly provisioned Go service credential sent only over that TLS
channel. Go must validate a configured trust root and server identity, without
ambient trust, skip-verification, plaintext fallback or credential logging.

This authenticates a service, not an action or lease. Credential lifetime, rotation,
revocation and role scope must be explicit in the implementation contract. A stolen
bearer credential can impersonate that service; short validity and a narrow role
reduce exposure but do not remove that property. A mutually authenticated certificate
design would need Rust-backed client signing to preserve the private-key boundary;
loading a client private key into Go is not silently accepted as an exception.

Authentication implementation is paused pending confirmation, per the security
skill. No certificate, production credential, new listener or auth behavior has
been created or changed.

## Ordered implementation slices

### 1. Authenticate the existing real Go-to-Rust channel

Acceptance:
- Explicit credentials work against the actual Rust binary; missing/wrong/duplicate
  credentials and invalid server identity fail before worker state changes.
- Partial configuration, expired credentials and invalid trust material fail closed;
  no fallback exposes credentials over plaintext. Private keys remain Rust-local.
- Test logs/errors/debug surfaces exclude credential values. Existing shadow-only
  compatibility remains explicitly non-production, not a permanent execution fallback.

Verification: Go worker unit tests, real Go-to-Rust TLS process tests, Rust worker
tests/Clippy and default-feature build. These are transport tests, not MinIO evidence.

### 2. Add a typed operation-specific execution contract

Depends on slice 1 and its confirmed identity/lifetime model.

Acceptance:
- Additive Protobuf commands bind action/epoch, operation identity, placement/key,
  exact condition, payload digest/length and authority/lease identity. Existing frozen
  messages and descriptor baseline remain unchanged.
- Rust validates authenticated identity and signed operation authority against the
  exact command, with durable replay protection before dispatch. Go owns claim/epoch
  decisions; Rust owns signing keys and its effect journal.
- Canonical signing input is explicitly specified; deterministic Protobuf encoding
  is not assumed to be a portable cryptographic canonicalization rule.

Verification: descriptor compatibility/drift gate, Go/Rust shared positive/negative
vectors and real cross-process substitution/replay tests. Decompose signer and
admission implementation into separately verified increments before dispatch wiring.

### 3. Wire bounded storage execution and control reconciliation

Depends on slice 2 and a qualified Go authority/lease path.

Acceptance:
- Go durable claim -> typed command -> Rust durable intent -> actual provider effect
  -> conditional byte verification -> bound result -> Go journal reconciliation.
- Payload bytes stay in Rust; Go never opens the Rust database. Service auth alone
  never supplies missing operation authority, lease state or production cutover proof.
- Cancellation, lost ACK and fence loss retain uncertain effects; no blind replacement
  effect or late successful Go commit after authority loss.

Verification: real Three-MinIO execution, actual controller/worker termination at
commit windows, durable takeover and measured provider observations. No manually
seeded effect journal or synthetic success is accepted as execution evidence.

### 4. Qualify production ownership

Depends on complete per-domain parity and cutover implementation, not just slice 3.

Acceptance: complete backup/restore/repair/rebalance and native federation workflows,
two-Fleet/four-MinIO takeover, measured zero-Python/unique-writer invariants, performance
SLOs, every production target/distributable and exact-head CI Evidence Assembly.

The original `tasks/plan.md` and `tasks/todo.md` recovery plans remain untouched.

## Implementation sources checked

- Workspace Tonic 0.12.3 `ServerTlsConfig` and `tls` feature source in the pinned
  Cargo registry. Its TLS configuration is not caller authentication by itself.
- [gRPC Go 1.83.0 per-RPC credentials](https://pkg.go.dev/google.golang.org/grpc@v1.83.0/credentials#PerRPCCredentials)
  provide a transport-security requirement for service metadata.
- [Go TLS configuration](https://pkg.go.dev/crypto/tls#Config) provides explicit
  trust roots and server-name validation. No `InsecureSkipVerify` path is proposed.

## Implementation Status (2026-09-07)

1. **Protobuf & Wire Schema**:
   - Added `ExecuteStorageMutation` and `QueryStorageEffect` RPCs, `StorageMutationRequest`, `StorageMutationResponse`, `QueryStorageEffectRequest`, `StorageMutationStatus`, `StorageConditionType`, and `StoragePrecondition` in `proto/action/v1/action.proto`.
   - Regenerated descriptor and Go stubs; verified contract parity via `native_codegen.py --check` and `native_runtime_contract.py --check`.

2. **Rust Worker Service (`deepseek-worker`)**:
   - `TransportAuthenticator` trait with `ProductionFailClosedAuthenticator` (fails closed with `SERVICE_AUTHENTICATION_UNAVAILABLE` by default) and `StaticTokenAuthenticator` (Bearer token for test/qualification).
   - `execute_storage_mutation`: Authenticates caller, validates fence and epoch against local authority, checks payload digest and expected length, verifies target identity, maps preconditions (`CreateOnly`, `IfMatch`), executes durable intent via `Worker::execute_storage_put`, and journals effect.
   - `query_storage_effect`: Authenticates caller, validates fence, queries SQLite effect record, and triggers idempotent byte-level reconciliation (`reconcile_storage_mutation`) when uncertain.
   - All tests in `grpc_service.rs` and `authorized_storage.rs` pass; Clippy clean with zero warnings under `-D warnings`.

3. **Go Control Plane & Client (`go/internal/action`, `go/internal/worker`)**:
   - `worker.Client`: `ExecuteStorageMutation` and `QueryStorageEffect` with outgoing metadata auth, fence validation, operation ID checking, and sentinel error mapping.
   - `action.Coordinator`: Enforces fail-closed production cutover (`ErrCutoverNotAuthorized`), validates Go writer lease before claim and after dispatch, manages transition lifecycle (`PENDING -> CLAIMED -> EXECUTING -> SUCCEEDED / FAILED_BEFORE_EFFECT / EFFECT_UNKNOWN`), and reconciles uncertain effects.
   - Test coverage: `internal/action` at 99.5%, `internal/worker` at 97.9%, overall Go module at 95.6% (meeting the 95.0% CI gate).

