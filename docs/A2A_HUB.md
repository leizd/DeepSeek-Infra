# Native A2A JSON-RPC mesh (`POST /a2a`)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **wired for discovery, Go-owned durable task lifecycle, native chat execution, and SSE.**
Exact-head CI has not run.

`deepseek-gateway::a2a_hub` and `a2a_stream` implement these A2A methods:

- `GET /.well-known/agent-card.json` — orchestrator Agent Card (protocol 0.3.0)
- `GET /a2a/agents` — orchestrator + researcher/coder/reasoner/critic
- `POST /a2a` and `POST /a2a/agents/{id}`
  - `message/send`, `tasks/get`, `tasks/cancel`, `tasks/list`
  - `agent/getAuthenticatedExtendedCard`
  - `message/stream`, `tasks/resubscribe` (SSE)

`message/send` creates a task. Tests may inject a completion function. The
HTTP default is `a2a_runner::run_native_a2a`: capability-scoped tools + the
native chat tool loop. A missing `DEEPSEEK_API_KEY` fails the task with
`A2A upstream is not configured (DEEPSEEK_API_KEY)` rather than inventing an
answer.

The SSE body starts with the public task snapshot, emits `artifact-update`
events for progress and the answer, then ends with a `status-update` whose
`final` is true for completed, failed, or canceled tasks. It does not emit an
OpenAI `[DONE]` marker. `tasks/resubscribe` accepts `id` and `afterChunkIndex`;
only artifact updates with a greater index are replayed, followed by the
current terminal status. The initial snapshot still contains the task history
and stored chunks, matching Python. JSON-RPC IDs are retained on every event.

Disconnecting drops the stream subscription, without canceling the task.
Cancellation is best-effort at the upstream boundary: an accepted cancellation
discards a late answer. Task start, cancel, and completion mutate the same
locked record, preventing a stale completion from overwriting cancellation.
Production authentication applies to both stream methods; `A2A_ENABLED=false`
rejects RPC requests before submission. Go's public A2A flags advertise streaming.

## Durable control boundary

The native gateway uses `a2a_control` and `proto/agent/v1/a2a.proto` when the
control connection is configured. Go alone writes `a2a.sqlite3` in an isolated
directory; the gateway never writes that database or Python's `.a2a` files.
SQLite commits task state, history, and chunks atomically with full synchronous
WAL writes. An OS file lock admits only one controller process. A second
controller cannot recover the first controller's live tasks. Foreign schemas,
corrupt records, and nonregular database/lock/sidecar entries are rejected.

Configure `deepseekd` with all five settings (partial configuration is fatal):

| Setting | Meaning |
| --- | --- |
| `DEEPSEEKD_A2A_LISTEN` | Private gRPC listener, for example `127.0.0.1:8091` |
| `DEEPSEEKD_A2A_STORE` | Isolated Go-owned task directory; Python state paths are rejected |
| `DEEPSEEKD_A2A_TLS_CERT` | Server certificate PEM |
| `DEEPSEEKD_A2A_TLS_KEY` | Server private key PEM |
| `DEEPSEEKD_A2A_TLS_CA` | CA PEM used to verify client certificates |

Configure the Rust edge with `DEEPSEEK_A2A_CONTROL_URL` (HTTPS),
`DEEPSEEK_A2A_TLS_SERVER_NAME`, `DEEPSEEK_A2A_TLS_CA`,
`DEEPSEEK_A2A_TLS_CERT`, and `DEEPSEEK_A2A_TLS_KEY`. The transport requires mTLS;
there is no plaintext or local-task fallback on connection/configuration errors.
In `python_disabled` mode, an absent control configuration returns HTTP 503
`NATIVE_A2A_CONTROL_NOT_CONFIGURED`. The old process-local hub remains available
for qualification while Python still owns the default production entry.

Go installs epoch 1 for an immutable submission identity. A claim issues an
execution token, persists only its digest, and starts a 45-second lease. The
Rust executor renews every five seconds. Finish requires the exact Go-issued
task/epoch/token and a live lease. Get/List/public JSON never expose the token.
Cancellation discards a late answer. A controller restart preserves completed
snapshots and marks unfinished tasks failed with the Python restart message.
An executor crash expires its lease and fails the task; neither path reruns it.
This does not assert that a failed upstream operation had no external effects.

SSE uses the same cursor/event encoder for both implementations. The durable
path reads the Go snapshot every 250 ms while subscribed. Gateway and controller
restarts preserve the completed snapshot and resume cursor. Python mutations in
the declared `a2a_task_lifecycle` domain are mechanically denied under
`go_authoritative` and `python_disabled`, before submission, cancellation,
in-memory updates, or persistence.

## Remaining gaps

The default service/desktop/Android entry still needs native cutover. Migration
of existing Python task files, peer-client calls, full coercion/error parity,
A2A trace/disconnect metrics, retention/eviction policy, and broader acceptance
remain. The optional Go service is isolated qualification inside the current
shadow daemon, not a completed production control-plane cutover. Whole-Go
statement coverage is **95.003059% (4658/4903)**, just above the unchanged
**95.0%** local gate; exact-head CI remains outstanding. The gate reads raw
profile counts, so rounded Go output cannot admit a result below the threshold.

## Verification

- `a2a_hub::tests::agent_cards_cover_orchestrator_and_workers`
- `message_send_runs_injected_runner`
- `GET /.well-known/agent-card.json` returns the orchestrator card
- `tests/a2a_stream.rs`: completion, failure, cancellation after work starts,
  disconnect/resubscribe, cursor coercion, in-band errors, and real loopback
  HTTP through the production router and native runner (including auth/disable).
- `terminal_streams_match_python_oracle_for_all_resume_cursors`: 12 semantic
  event comparisons against the actual Python SSE generator. Fixtures are
  regenerated/checked by `python tasks/native-runtime/a2a_stream_oracle.py`
  (`--write` to regenerate). This compares decoded JSON, not object key order.
- `go/internal/a2a`: single-writer exclusion, claim fencing, expiry, cancellation,
  durable idempotency, corrupt-store recovery rollback, write-failure atomicity,
  foreign schema rejection, real mTLS RPC, leaf/issuer certificate expiry (including an already-open real mTLS connection),
  damaged SQLite page rollback, competing SQLite locks, and lifecycle tests.
- `tasks/native-runtime/a2a_restart_e2e.py` starts actual Go/Rust binaries and a
  controlled loopback HTTP provider, kills both native processes while idle and
  executing, waits for a real 45-second lease to expire, and asserts no repeated
  provider request. `artifacts/a2a-control-restart-proof.json` records the nine
  passing checks, killed PIDs/exit codes, and binary hashes. This is local A2A
  process evidence, not MinIO/provider-storage or complete zero-Python evidence.

- `a2a_message_oracle.py` derives 31 message/coercion cases from the unmodified
  Python text extractor. Rust rendering and Go admission use the same fixture.
  This covers falsy values, booleans, numbers, nested values, kind/type fallback,
  and Python's control-character whitespace. The real process harness also
  verifies a boolean message, nested context ID, explicit null message fields,
  and a metadata integer greater than JavaScript's exact-integer range.
  Full arbitrary dict-order and error parity remain separate work.
