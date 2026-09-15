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
all passed.

**SSE streaming is now wired too (2026-09-14, uncommitted).** `chat_stream.rs`
owns upstream SSE decoding (`decode_event`/`decode_chunk`) and downstream OpenAI
SSE encoding (`StreamChunkEncoder`), and `chat_completions` now branches on
`stream`. `request_preparation` no longer refuses `stream: true` — it normalizes
it to a boolean and forwards it, because transport selection is not a
preparation-layer concern. Verified locally:

- `cargo test -p deepseek-gateway -j 1` -> 96 lib + 4 `chat_execution` +
  6 `chat_stream` real-boundary tests, all passed.
- Byte-level parity: `tasks/native-runtime/sse_parity_probe.py` (extracts the
  real `_sse`/`openai_chat_stream` via `ast`) vs `examples/sse_parity_probe.rs`
  over the same two upstream scripts -> identical MD5
  `b9129475b6bae8b1239f4529e0a50932`, 12 frames, no differences.
- `cargo clippy -p deepseek-gateway --all-targets --all-features -- -D warnings`
  -> only the pre-existing `control_proxy.rs:20` `result_large_err` (file
  byte-identical to HEAD; local rustc 1.97.1 vs the declared 1.85).
- See `docs/GATEWAY_SSE_PARITY.md` for the frame contract and the explicit
  non-goals.

Still unwired and each failing closed with its own code: tool-call rounds,
`/api/chat` NDJSON (including `system_note`/`search`/`memory_suggestion`),
semantic cache/memory/context-compression, model router, scheduler leases and
budget ledger. `release/native_runtime_5_0_evidence_v1.json` stays `NOT_READY`.

Wire Go `ExecuteClaimedStorageAction` to sign `control-storage-operation-grant-v1`
from the live claim (no payload bytes in the grant), persist grants in the Rust
worker sqlite journal, then slice 3 provider-backed dispatch.

**Tool-round layer 1 is now implemented and byte-verified (2026-09-14, uncommitted).**
`rust/crates/deepseek-gateway/src/tool_rounds.rs` mirrors the oracle's round
*bookkeeping* only: `ToolCallAccumulator` (streamed `tool_calls` delta merge and
finalization), `normalize_tool_calls_lenient`, `decide_round` (the round/budget
branch), `append_tool_exchange` (message assembly), and
`force_final_answer_without_tools`, plus `tool_names` / `select_tool_calls` /
`tool_call_note` and the three constants.

The public route **keeps refusing** a `tool_calls` turn with
`NATIVE_CHAT_TOOL_ROUNDS_NOT_READY`. Layers 2 (tool execution: the 17 branches
plus `browser_*`) and 3 (policy/sandbox) are not implemented, and wiring layer 1
alone would replace the oracle's terminating tool loop with a permanently
failing one that still answers `200` — the forbidden silent behavior change.
This slice exists to make that refusal precise and to make later enablement a
wiring change rather than a rewrite.

Verified locally:

- Byte-level parity: `tasks/native-runtime/tool_round_parity_probe.py` (extracts
  the real `append_tool_exchange`, `merge_stream_tool_call_deltas`,
  `finalized_stream_tool_calls`, `normalize_tool_calls`, `tool_names`,
  `force_final_answer_without_tools` via `ast`; stubs only the layer-2
  `execute_tool_calls` and the transport `raise_if_cancelled`) vs
  `examples/tool_round_parity_probe.rs` over the same 12 input scripts ->
  **identical MD5 `ca9b072a4826fc470e3ccdc6e436bc58`**, 36 keys, no differences.
- `cargo test -p deepseek-gateway --lib tool_rounds -j 1` -> 24 tests, all pass.
- `cargo fmt --check` clean.

Three real divergences were found and fixed **by the probe**, not by reasoning:

1. an index-less delta lands at the *slot count*, not the highest index plus one
   (oracle: `["first", "third", "five"]`; the original unit test asserted
   `["first", "five", "third"]` and was wrong);
2. `str(item.get("id") or f"call_{i+1}")` stringifies truthy non-strings, so
   `id: 123` becomes `"123"` while `id: 0` / `id: ""` fall back — the first
   implementation read only string ids and silently renumbered them;
3. non-string `arguments` use Python's **default** JSON separators (`{"a": 1}`),
   not `serde_json`'s compact form (`{"a":1}`). This lands verbatim in the
   upstream body and therefore changes the prompt prefix and DeepSeek's prefix
   caching.

Known bounded limitation: this workspace compiles `serde_json` **without**
`preserve_order`, so an `arguments` *object* whose keys are not already sorted
serializes in Rust's sorted order rather than the caller's insertion order.
Enabling `preserve_order` workspace-wide would silently reorder every other Rust
response (only `deepseek-proof` opts in), so it was deliberately not done;
resolving it belongs with argument canonicalization. Recorded in
`docs/GATEWAY_TOOL_ROUND_PARITY.md`.

Next concrete action for this line: implement layer 2 (`execute_tool_call`'s 17
branches + `browser_*`) and layer 3 (`ToolPolicy.evaluate`/`sanitize_result`),
then wire `tool_rounds` into `chat_execution`/`chat_stream` and delete the
`ToolRoundsUnwired` refusal.

**Tool-policy pure core ported and byte-verified (2026-09-15, uncommitted).**
`rust/crates/deepseek-policy/src/tool_policy.rs` mirrors the side-effect-free half
of `deepseek_infra/infra/tool_runtime/tool_policy.py`: the SSRF guard
(`evaluate_url_safety`), the path-escape guard (`evaluate_path_safety`), the
recursive network-argument guard, the secret-exfiltration guard
(`arguments_contain_secret`), the prompt-injection sanitizers
(`sanitize_external_text` / `sanitize_tool_result` /
`sanitize_tool_result_for_external`), `validate_arguments`, `_max_risk`, and the
`ToolMetadata` / capability-profile tables.

This is the gate the oracle applies **before** a tool runs, so it is a
prerequisite for layer 2 (tool execution): porting execution first would mean
running model-chosen side effects with no SSRF, path-escape, secret-exfil, or
injection guard — strictly weaker than the Python being replaced.

Still **not** ported: `ToolPolicy.evaluate` (reads config + audit state) and the
audit writers. Until those land, nothing may execute a tool on this module alone,
and the route keeps refusing tool rounds with
`NATIVE_CHAT_TOOL_ROUNDS_NOT_READY`.

Verified locally:

- Byte-level parity: `tasks/native-runtime/tool_policy_parity_probe.py` slices the
  contiguous pure region of the oracle (lines 59–563) and `exec`s it (so the
  definitions being compared are the oracle's own, including the import-time
  derivations) vs `deepseek-policy/examples/tool_policy_parity_probe.rs` over the
  same corpus -> **identical MD5 `26c7723c89a4fb59c7ef9e412f1b4b97`**, 158 keys,
  no differences.
- `cargo test -p deepseek-policy --lib tool_policy` -> 30 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean. `cargo fmt` applied.
- `cargo check -p deepseek-gateway --all-targets` -> still compiles.

Two defects the probe caught, both from **guessing** the IP classifier instead of
reading CPython's tables (the first pass was wrong in both directions):

1. false negative — `1:0:0:2::3` was allowed; Python's `is_reserved` covers
   `::/8` (and much more: `4000::/3`, `e000::/4`, …), so the oracle blocks it.
2. false positive — `192.88.99.1` was blocked; that range is in none of Python's
   tables, so the oracle allows it.

Derived facts, now encoded and tested:

- IPv4 `is_global` = `not in 100.64.0.0/10 and not is_private`, so `not is_global`
  adds only the shared range; `is_reserved` is `240.0.0.0/4` (already private).
  Blocking set = 14 ranges.
- IPv6 `is_global` is literally `not is_private`, so `not is_global` adds nothing.
  Blocking set = `_private_networks` ∪ `_reserved_networks` ∪ multicast = 23 ranges.
- IPv4-mapped IPv6 delegates **every** predicate to the underlying IPv4 address,
  so `::ffff:1.2.3.4` is *allowed* while `::ffff:0:1` is blocked — even though
  `_private_networks` lists `::ffff:0.0.0.0/96`. Rust's `to_ipv4_mapped()` matches
  CPython's `ipv4_mapped` exactly.
- `fec0::/10` (deprecated site-local) is allowed by the oracle; `fe00::/9` stops
  at `fe7f::`. Mirroring that hole is correctness, not a bug to "fix".

Message-parity surfaces that look like formatting but are not: the blocked-IP
reason embeds Python's `str(ip)` (so IPv4-mapped must render dotted, not
`::ffff:0:1`), and the enum violation embeds Python's `repr` of the list
(`['x', 'y']`).

Dependency change is minimal: `regex 1.13.0` was already in `Cargo.lock`
transitively, so it is pinned exactly and promoted to a direct dep of
`deepseek-policy`; the lockfile gains one edge and no new crate version.

**Real finding, deliberately not fixed here.** The crate's pre-existing generic
guards (`url_guard.rs` / `path_guard.rs`, behind the gateway's `/policy/*` routes)
are **weaker than the oracle** and are a different model: no
`.local`/`.localhost`/`.internal` suffix check, no trailing-dot strip, URL
credentials are **stripped and allowed** (the oracle denies them), and
multicast/reserved/CGNAT/non-global IPv4 plus the IPv6 reserved ranges are not
checked at all. Tightening them changes a registered route's behavior, so it
deserves its own slice with its own evidence. Exposure is latent — the Rust
gateway is not the production authority — but it should not ship as-is.

Next concrete action for this line: port `ToolPolicy.evaluate` + the audit log
(layer 3b), then implement layer 2 execution against this gate, then wire the
round loop and delete the `ToolRoundsUnwired` refusal.

**Tool-policy engine + audit layer ported and byte-verified (2026-09-15 二轮，uncommitted).**
`deepseek-policy::tool_policy` now also carries the decision engine and the audit
log, completing the policy gate:

- `ToolPolicy` + `ToolPolicyConfig` with the oracle's own defaults,
  `ToolPolicy::new` / `permissive()`, `evaluate`, `_record` semantics
  (counters + `blocked_tools`), `mark_tainted` / `is_tainted`,
  `sanitize_result` (scrubs and taints the turn on a hit), `denial_output`,
  `diagnostics`.
- `ToolPolicyDecision` — deliberately **not** named `PolicyDecision`, because this
  crate already exports a different `PolicyDecision` (the
  `Capability`/`RiskLevel` model behind `/policy/*`). Sharing the name would make
  importing the wrong one an easy, security-relevant mistake.
- `is_sensitive_memory` (extracted from `infra/data/memory.py`, not re-written).
- Audit: `AuditSink` trait with `NullAuditSink` / `InMemoryAuditSink` /
  `JsonlAuditSink`, `build_audit_entry`, `build_external_audit_entry`,
  `normalized_args_hash`, `read_recent_audit`, and a hand-rolled
  `utc_isoformat_seconds` (no date dependency).

**Not ported:** `tool_policy_status` (reads the audit-path global and the config
object) and the `deepseek_infra.core.config` env reader — a config-layer concern,
not a policy one. Nothing may execute a tool until layer 2 exists and is wired;
the route still refuses tool rounds with `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY`.

Verified locally:

- Byte-level parity across **both** regions of the same oracle file: the
  contiguous constants+guards+engine slice (lines 59–901, `exec`ed verbatim with
  only the config globals and audit path rebound) plus the audit functions lifted
  individually and driven against a **real temporary JSONL file** (so the writer
  that ships is the writer measured, not a re-implementation of its entry dict) ->
  **identical MD5 `d51462e06a0e6ccd03db7ed05ab77d71`**, 197 keys, no differences,
  re-confirmed after `cargo fmt`.
- `cargo test -p deepseek-policy` -> 77 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean.
- Dependency: `sha2 0.10.9` was already in `Cargo.lock`; the lockfile gains one
  edge and no new crate version.

Design points worth keeping:

- **The decision order is the contract.** `evaluate` returns on the first failing
  check, so reordering any pair changes which reason a call reports when it fails
  several (unknown → capability → schema → SSRF → path → sensitive → secret →
  confirm → taint → allow).
- **`fetch_url` bypasses the recursive guard** and calls `evaluate_url_safety` on
  `args["url"]` directly; every other network tool goes through
  `evaluate_network_argument_safety`, which **prefixes the offending key**. So the
  same private host yields `ssrf_blocked:private or local ip is not allowed: …`
  for `fetch_url` but `ssrf_blocked:host: …` for `web_search`. My first unit-test
  expectation used the unprefixed form for `web_search` and was wrong; the probe
  settled it. (Third time this pattern has caught me — measure, don't infer.)
- **`denial_output` does not check the action.** Called on an allow it still
  returns a denial-shaped payload with `code: "forbidden"` and
  `error: "… blocked by tool policy (allow)"`. That looks like a bug and is not;
  it has an explicit test so nobody "fixes" it.
- **Best-effort audit is the contract.** The oracle swallows every write error so
  an unwritable log can never break a tool call. The port keeps that but records
  the failure in `last_error()` so it stays observable instead of vanishing.
  Splitting the write behind `AuditSink` is also what keeps `evaluate`
  deterministic enough to compare byte-for-byte, and lets shadow runs capture
  decisions without touching the authoritative log.
- The audit entry is `{"ts", "scope", **decision.to_dict()}` with `sort_keys=True`.
  `ts` is the only non-deterministic field, so the probe injects a fixed clock and
  masks it on both sides; its *shape* is pinned by unit tests with hand-checked
  anchors (epoch, day boundary, Unix 1e9).

Next concrete action for this line: port `tool_policy_status` + the config reader,
then implement layer 2 execution against this gate, then wire the round loop and
delete the `ToolRoundsUnwired` refusal.

**Status endpoint ported, and the `/policy/url` gate aligned to the oracle (2026-09-15 三轮，uncommitted).**

Step 1 of the planned sequence (`tool_policy_status` + config) is done:
`ToolPolicySettings` (the five knobs, config defaults), `ToolAuditPaths::under(root)`
(mirroring `tool_audit_dir = root / ".tool-audit"`), `tool_policy_status`, and
`render_path_like_python` for the `auditLogPath` field. `ToolPolicyConfig::default()`
now reads its four strictness fields *through* `ToolPolicySettings::default()`, so
the engine and the status payload cannot drift apart (asserted by a test).

**The scoping of step 2 turned up something that reordered the work.** The oracle's
own Rust delegation is the risk:

```
execute_tool_call -> _evaluate_rust_policy (tools.py)
                  -> rust_core.policy_client.check_url / check_path
                  -> POST /policy/url, /policy/path (gateway)
                  -> url_guard::validate_url_access  <-- weaker than Python
```

`DEEPSEEK_RUST_POLICY` defaults to **false** (`infra/rust_core/config.py`), so
Python still decides. But flipping it would have moved SSRF decisions onto the
guard flagged in the previous round: `.local` / `.internal` hosts, trailing-dot
localhost, credential-bearing URLs, multicast, reserved, CGNAT and the whole IPv6
reserved set would all have started passing. Wiring execution onto that gate first
would have been the wrong order — the gate had to be correct before anything was
allowed to depend on it.

So `url_guard::validate_url_access` now **delegates to
`tool_policy::evaluate_url_safety`** and maps the oracle's denial reason onto the
crate's codes. The bridge contract is unaffected: `policy_client._parse_response`
requires the `allowed` bool plus non-empty string `code`/`reason`/`decision_id`/
`capability`/`risk_level`, and treats `code` as **opaque** — nothing branches on it.

Two deliberate consequences:

- The oracle reports one `private or local ip is not allowed: …` verdict, so
  loopback, link-local, reserved and multicast now all return
  `PRIVATE_NETWORK_BLOCKED` from this route. `codes::LINK_LOCAL_BLOCKED` is no
  longer emitted *by this guard*. Mirroring the oracle means mirroring its
  collapsing, not inventing a finer taxonomy.
- `UrlPolicy` can only **tighten**. The oracle accepts http(s) only, so listing
  another scheme cannot reintroduce it — there is a test for exactly that.

`path_guard` is deliberately **not** touched: `validate_workspace_path` is
root-containment over a `{root, requested}` pair, while the oracle's
`evaluate_path_safety` is an argument-key scan. Complementary, not
interchangeable; merging them would change what the route means.

Verified locally:

- Byte-level parity: **identical MD5 `bae3a9e5eb30cdd80a7a28b31e1f433b`**, 257
  keys, no differences. The URL corpus is checked **twice** — `url::<label>` (the
  guard) and `guard::<label>` (the route), so the route cannot silently drift from
  the guard it delegates to.
- `cargo test -p deepseek-policy` -> 82 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.
- `cargo check -p deepseek-gateway --all-targets` -> still compiles.

**NOT done, and the honest state of the remaining two steps:**

- **Step 2 (layer 2 tool execution) is not started.** `execute_tool_call` dispatches
  to 17 local branches plus `browser_*`, and those depend on the `search`, `rag`,
  `data` (projects/reminders/memory), `media` (presentations, mindmaps, documents,
  slides) and `browser` packages — several thousand lines with their own
  side-effect and sandbox semantics. It is a multi-slice effort, not one commit.
  A sensible first slice is the **dispatch skeleton + the branches with no external
  package** (e.g. `python_eval`'s sandbox envelope, `data_transform`,
  `list_reminders`), each behind the gate just aligned, with the package-backed
  branches added one at a time.
- **Step 3 (wire the round loop, delete `ToolRoundsUnwired`) is not started** and
  is correctly blocked on step 2 — wiring it now would replace the oracle's
  terminating tool loop with a permanently failing one that still answers `200`.
- `DEEPSEEK_RUST_POLICY` remains **off**, deliberately. Enabling it is an explicit
  cutover that needs `path_guard` aligned and the failure-mode policy reviewed.

**Layer 2 slice 1: the executor seam, with one branch ported (2026-09-15 四轮，uncommitted).**

`rust/crates/deepseek-policy/src/tool_dispatch.rs` ports the *seam* of
`execute_tool_call` in `infra/tool_runtime/tools.py`:

- the envelope contract (success `{"ok": true, "tool", "result"}` + `sanitize_result`;
  the `AppError` and catch-all error arms; the `Unsupported tool:` fallback);
- the normalization the branches rely on — `tool_call_name`,
  `parse_tool_arguments`, `safe_limit`, `is_parallel_safe_tool`, `SERIAL_TOOL_NAMES`;
- the **complete branch inventory** (`Branch`, 18 entries) with `branch_for`
  routing, `is_ported`, and `blocker()` naming the package each unported branch
  waits on — a test asserts no branch is silently missing;
- `generate_chart` + `chart_markdown_table`, the one branch that needs no package.

**Nothing is wired.** `DispatchOutcome::Unported` deliberately carries **no
envelope** and `to_output()` returns `None` for it, so a caller cannot report
success (or even a tidy error) for a tool that was never implemented. 17 of 18
branches remain unported; `python_eval` in particular needs a real sandbox
because the oracle shells out to a Python interpreter, which the migrated runtime
must not do.

Verified locally:

- Byte-level parity: **identical MD5 `9d491ef3f97c9f079ad9d7761a815ec4`**, 49 keys,
  no differences, re-confirmed after the clippy fixes.
- `cargo test -p deepseek-policy` -> 102 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.

Three orderings the probe pinned, and the bugs they caught:

1. **Parse before gate.** My first draft gated the raw `arguments` value. The
   model sends arguments as a JSON *string*, and the guards read fields inside it —
   so gating the raw string left every argument guard looking at an empty object
   and the SSRF/path checks **silently passed**. The oracle parses first; there is
   now a test that fails if the order is reversed
   (`dispatch("fetch_url", "{\"url\": \"http://169.254.169.254/\"}")` must be
   Denied with `risk = "critical"`).
2. **Gate before branch.** A denial short-circuits; the probe records branch
   invocations, and every denied case reports `branches: []`.
3. **The unknown-tool fallback is a no-policy path.** With a policy attached an
   unregistered name is denied as `unknown_tool` first, so `Unsupported tool:` is
   only reachable without one. The Rust probe example's first version gated every
   case, which made the `no-policy` case deny where the oracle reached the
   fallback — the diff exposed it.

Two behaviours the corpus settled, both from **guessing instead of measuring**
(that is now four times on this project):

- `data[:12]` is applied **before** the point filter, so the cap counts raw items,
  not usable points. My first test asserted 12 points for a 26-item input; the
  real answer is 7.
- `int("7.9")` raises in Python (falls back to the default) while `int(7.9)`
  truncates to 7 — the string and number paths had to be handled separately.

Also reproduced: `python_float_str` for `str(float)` (`1.0` not `1`; signed
zero-padded exponents outside `1e-4..1e16`), because those values are interpolated
into the model-facing markdown table.

Next concrete action for this line: port `execute_tool_calls` (the parallel batch
+ cancellation) and the remaining branches one at a time, each behind the gate,
starting with the ones whose packages are smallest. Only after enough branches
exist does wiring the round loop (and deleting `ToolRoundsUnwired`) become safe.
