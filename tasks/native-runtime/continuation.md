# Native runtime continuation record

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

This file is the session handoff. Historical plans, checkboxes, VERSION, and
`release/native_runtime_5_0_evidence_v1.json` are not completion evidence.
The capability matrix is [`migration-matrix.md`](migration-matrix.md).

## Current git

| Field | Value |
| --- | --- |
| Branch | `codex/native-runtime-5.0.0-continue` (created from `native-runtime-5.0.0-recovered`) |
| Recovered HEAD | `451ba5ec23ad07e783b68f2a1f8f87ed5f9b8f05` |
| VERSION | `4.8.0` |
| Evidence file | `NOT_READY` (must stay fail-closed until exact-head CI generates it) |
| Production authority | Python (`release/native_runtime_ownership_v1.json`) |

Do not reset, clean, or discard the recovered uncommitted tree. Coverage
profiles (`go/cov`, `go/shadow_cov`, `go/store_cov`, `*.out`) are local
artifacts and must not be committed.

## Evidence classes (do not collapse)

| Class | Meaning |
| --- | --- |
| Documented target | Spec/ADR/roadmap/plan text |
| Implemented, unwired | Native code exists; production entry still Python or fail-closed |
| Wired, unverified | Native entry exists; missing real-process/provider/CI evidence |
| Locally verified | This workspace ran the matching command |
| CI / release verified | Exact-head CI, Evidence Assembly, readiness validator |

## Recovered uncommitted work (this session)

Two in-progress slices were already present. They are not treated as correct
until tests in this session pass.

1. **Go schema v7 verification lifecycle** — `VERIFYING` / `ASSESSING_EFFECT`
   with an immutable boundary; leased execution/recovery persist VERIFYING
   instead of SUCCEEDED; resources stay held.
2. **Go→Rust TLS transport** — Rust-owned server key, Go CA + server-name
   verify, short-lived bearer over TLS only, no plaintext credential fallback.

## What this session implements next

Signed storage-operation grant (worker-execution-plan slice 2): canonical JSON
grant, shared v31 corpus, RPC admission before dispatch.

Not in this slice: durable grant sqlite journal, payload-bytes-in-Rust, MinIO
kill/takeover, production cutover, public edge parity, or readiness PASS.

## Status after this session

| Item | Class | Command / evidence |
| --- | --- | --- |
| Schema v7 + verification primitives | locally verified | `go test ./internal/store -count=1 -timeout=360s` exit 0 (49.937s); `go test ./internal/action -count=1` exit 0 |
| TLS unit + real-process tests | locally verified | worker/protocol tests exit 0; `cargo test -p deepseek-worker --lib --bins --test grpc_service --test tls_transport` exit 0; `TestRustWorkerTLSRealBoundary` exit 0 (0.16s) |
| Clippy worker | locally verified | `cargo clippy -p deepseek-worker --all-targets -- -D warnings` exit 0 |
| Full Go module (no race) | locally verified | `go test ./... -count=1 -timeout=600s` exit 0 (then worker re-run after OPERATION_INVALID mapping) |
| native-go TLS CI wiring | implemented, unverified | `.github/workflows/ci.yml` — needs exact-head CI |
| Storage operation grant v31 + RPC admission | locally verified | `go test ./internal/store -run StorageOperationGrant`; `cargo test -p deepseek-worker --test frozen_storage_operation_grant_v31 --test grpc_service`; `python scripts/native_runtime_contract.py --check` (42 corpora / 31 versions); TLS process test exit 0 |
| Full Go `-race` | not verified this session | store historically hits 600s aggregate timeout |
| Production cutover | documented target | `ErrCutoverNotAuthorized` still enforced |

## Running processes

None started by this continuation unless a later section records a PID.

## Blockers that remain after this slice

1. Durable worker sqlite grant journal (in-memory replay only today) and Go
   coordinator attaching live grants instead of qualification JSON.
2. Outcome/risk verifiers and compensation (not journal primitives).
3. Provider-backed Three-MinIO / two-Fleet kill-and-takeover.
4. Rust edge chat/MCP/A2A/catalog parity; authenticated `/api/*` proxy.
   Non-stream `/v1/chat/completions` now executes natively (see below); SSE,
   tool rounds, MCP and A2A remain fail-closed.
5. Oracle normalization differences - **RESOLVED 2026-09-14** (commit `df7dfa13`).
   The oracle silently dropped blank-content turns, `null` content, non-object
   entries, `tool` turns missing `tool_call_id`, and caller-supplied `system`
   turns. It now refuses the first four (same `ErrorCode` values as Rust) and
   *keeps* `system` turns. The `system` case is the important correction: a
   second measurement on the full assembly path
   (`tasks/native-runtime/oracle_layering_probe.py`) showed the caller's
   instruction never reached the upstream body at all, because
   `normalize_chat_messages` dropped it while `build_deepseek_request` builds
   the authoritative prefix separately from `payload["systemPrompt"]`. Keeping
   the turn is therefore not a capability addition - it stops silent data loss.
   Rust behavior unchanged, as decided.
5. Go production cutover authorization protocol.
6. Default launchers/images still start Python (`launch.py`, `docker-compose.yml`).
7. Exact-head CI and Evidence Assembly.

## Next explicit action

The Python oracle now fails closed on unrepresentable turns instead of silently
dropping them (commit `df7dfa13`, 2026-09-14). Verified: 101 passed across
`test_deepseek_client_failure_paths.py`, `test_gateway_request_preparation.py`
and `test_rust_gateway_request_parity_contract.py`; the only pre-existing failure
was the test that encoded the bug itself. The four measured parity differences
are now closed, with the two layers proven to agree.

Native edge chat has moved from fail-closed to a wired non-stream path
(`6ea4dde3` + `chat_execution.rs`, 2026-09-14). Verified locally:
`cargo fmt -p deepseek-gateway -- --check` clean; `cargo test -p deepseek-gateway
-j 1` -> 77 lib + 4 `chat_execution` real-upstream tests + 2 boundary tests,
all passed. Still unwired and each failing closed with its own code: SSE
streaming, tool-call rounds, semantic cache/memory/context-compression, model
router, scheduler leases and budget ledger. `release/native_runtime_5_0_evidence_v1.json`
stays `NOT_READY`.

Wire Go `ExecuteClaimedStorageAction` to sign `control-storage-operation-grant-v1`
from the live claim (no payload bytes in the grant), persist grants in the Rust
worker sqlite journal, then slice 3 provider-backed dispatch.
