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

**Layer 2 slice 2: `data_transform` branch, batch orchestration, shared Python-JSON (2026-09-15 五轮，uncommitted).**

- `tool_transform.rs` ports `data_transform` and its four pure operations
  (`extract_regex`, `json_path`, `csv_summary`, `number_summary`) plus helpers
  (`read_simple_json_path`, `compact_json_value`, `number_summary_payload`, and a
  hand-rolled `csv_read` for Python's default CSV dialect).
- `tool_batch.rs` ports `execute_tool_calls`: selection capped at 6, serial/parallel
  batching (exposed as `plan_batches` data), cancellation at the four points,
  None → cancelled / None → "did not run", and the `role: "tool"` message with
  compact-JSON content truncated to `MAX_TOOL_RESULT_CHARS`. `strip_volatile_tool_fields`
  and `stable_tool_output_for_model` ported; artifact-compaction deferred (those 3
  branches unported, path unreachable, a test pins the pass-through).
- `python_json.rs` owns `dumps_default_separators`/`dumps_compact`/`float_str`/`value_str`
  — the rendering rules `tool_rounds`/`tool_policy`/`tool_dispatch` each had a
  private copy of. `tool_policy::normalized_args_hash` and `tool_dispatch::python_float_str`
  now delegate to it (two duplicates removed; gateway's `tool_rounds` copy noted
  as a follow-up, out of this crate's boundary).

**Honest state of the remaining 15 branches**: `Branch::blocker()` still names each
one's package and a test asserts none is silent. They are blocked on real subsystems
(browser engine, RAG, data layer, media/doc generation, an HTTP client, a real
sandbox for `python_eval`). Wiring the round loop and deleting `ToolRoundsUnwired`
stays blocked on these.

Two parity substitutions recorded in docs:
- JSON-path splitter: Python uses a lookahead the `regex` crate lacks; a plain
  split on `.` is equivalent for every *acceptable* path (well-formed parts have
  digits-only indices, so no dot lives inside brackets; the two disagree only on
  paths that fail the fullmatch and raise "Unsupported JSON path" either way).
- Engine-specific diagnostics: `Invalid JSON: …` / `Invalid regex: …` embed the
  engine's own error text. The prefix is the oracle's and identical; the suffix is
  masked on both sides like the audit `ts`. Divergence on record in the doc and a
  unit test, not behind a green diff.

Verified locally:
- Byte-level parity: **identical MD5 `3f088f27bcf1dda772cf3fb18d318cf5`**, 80 keys,
  no differences (helpers, both ported branches, batch layer, volatile-strip).
- `cargo test -p deepseek-policy` -> 131 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.

**Layer 2 slice 3: the search family, callback injected (2026-09-15 六轮，uncommitted).**

The next-smallest dependency after `data_transform`: `web_search` and
`compare_search_results` need no package, only the per-request
`web_search_callback` the gateway owns.

- `tool_search.rs` ports both branch bodies (with their distinct "not enabled for
  this request" errors), `compare_search_results` (two cleaned queries, whitespace
  collapsed / de-duplicated / 500-char cap; one round each; results de-duplicated
  across rounds and capped at 20), and `search_result_key`.
- `ExecutorContext` carries the optional callback, mirroring the oracle's keyword
  arguments. `dispatch` now threads it through — the one signature change; tests
  and the probe example pass a default context, which makes the search branches
  take their "not enabled" path, and that path is compared directly.

**`search_result_key` is deliberately a different projection** from
`tool_policy`'s SSRF host extraction: the guard wants a hostname to classify
against the IP tables, this wants the raw netloc (lowercased, port and userinfo
included) so results differing only in case or fragment collapse to one key.

Two measured behaviours that corrected wrong guesses of mine (**sixth time on this
project that measuring beat reasoning**):

- `urlsplit` strips **leading** C0 controls and spaces but never trailing ones, so
  `"  HTTP://X  "` keys to `"http://x  /"`.
- An **empty** URL is not an empty key: `urlsplit("")` normalises to path `/`, so
  the key is `"/"` — which is why an empty-URL result is **kept**, not skipped.
  Only a non-object entry is dropped.

Verified locally:
- Byte-level parity: **identical MD5 `a6aa9b0ed707966a641940b47fdade55`**, 104 keys,
  no differences.
- `cargo test -p deepseek-policy` -> 141 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.

Branch status: **4 of 18 ported** (`generate_chart`, `data_transform`,
`web_search`, `compare_search_results`). 14 remain, each with `Branch::blocker()`
naming its package. Nothing is wired; the round loop stays blocked on them.

**Layer 2 / data layer slice A1: the workspace mutation gate (2026-09-15 七轮，uncommitted).**

Prerequisite chosen by the user (A1 over A2). `rust/crates/deepseek-policy/src/mutation_gate.rs`
ports `infra/workspace/mutation_gate.py` — the fence, the exclusive OS lock, and the
durable generation counter. Every memory/reminder write is wrapped in it, so no
data-layer branch could be faithful without it.

**It is not a mutex.** `mutation_scope` (1) asserts no restore owns the workspace
(423, checked twice to close the race with a newly-created fence), (2) takes an
exclusive OS lock for the whole mutation, (3) bumps the generation **before and
after**, fsync'd. The lock and the fence are deliberately separate: a crash
releases the lock, but mutations stay blocked until recovery reconciles the
transaction.

Shape differences, each with a reason: `root: &Path` instead of a `config.ROOT`
global; `LockFileEx` with the oracle's ten-attempts-one-second-apart retry policy
(plain `LockFileEx` would block **forever** where `msvcrt.LK_LOCK` raises); `flock`
on Unix; `Mutex` + thread-local depth instead of `RLock` (Rust's `Mutex` is not
reentrant); hand-written `extern "C"` because this workspace pins deps to what is
already in `Cargo.lock`.

Quirks reproduced rather than fixed: `fsync_directory` stays **best-effort** (the
directory open normally fails on Windows); the lock file is created with `b"0"`
only if absent; temp-file cleanup failure is ignored after a committed replace;
`write_fence` and `bump_generation` build temp names differently (suffix preserved
vs dropped); the unreadable-fence message is **fixed** because the oracle chains
the cause with `raise ... from exc` rather than interpolating it.

Errors: `GateKind` distinguishes the oracle's `AppError` / `RuntimeError` /
`OSError`, and **`code`/`status` are `Option`** — a `RuntimeError` has neither, and
inventing `internal`/500 would let a caller read a programming error as a routine
refusal. That was my first draft's mistake.

Three probe bugs this slice exposed (all mine):
1. `ast.get_source_segment` drops decorators, so `exclusive_gate`/`mutation_scope`
   came back as bare generators, not context managers.
2. `@contextmanager` is **lazy** — `mutation_scope()` alone asserts nothing and
   bumps nothing; the body only runs on `__enter__`. A probe that merely called it
   would have shown a green tick over no behaviour.
3. `_GATE_STATE` is a module-level global, so the nested-different-root check only
   fires within one module instance. Building a second namespace for the "other
   root" gave the inner gate its own thread-local state and — correctly — no error.
   The Rust behaviour was right; the probe was wrong.
4. `json!` reads `[...]` as an array literal, so a `.iter()` chain cannot follow it.

Verified locally:
- Byte-level parity: **identical MD5 `57e0ede25273693e03863bffe024aadb`**, 32 keys,
  no differences — paths, generation read/bump/clamp, fence write/read/clear, both
  refusal paths, the scope's double bump, nesting (same and different root),
  malformed fences, temp-file hygiene, lock-file content.
- `cargo test -p deepseek-policy` -> 155 tests, all pass (14 new), including a
  multi-threaded case asserting the generation ends at exactly `scopes * 2`.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  -> clean; `cargo fmt` applied.

Next: **slice B, the reminders pair** (`create_reminder`, `list_reminders`) — 138
lines, one JSON file, no retrieval, no RAG. `Branch::is_ported()` is unchanged for
every data-layer branch; nothing is wired.

**Data layer slice B: the reminders store and its branches (2026-09-15 八轮，uncommitted).**

`rust/crates/deepseek-policy/src/reminders.rs` ports
`infra/data/reminders.py` plus the `create_reminder_tool` / `list_reminders_tool`
wrappers: the JSON store, `parse_due_at`, `create_reminder`, `list_reminders`,
`delete_reminder`, `due_reminders`. First slice that exercises the slice-A1 gate
from a data path — the probe records the generation advancing **two per create** as
evidence the write really goes through the fence.

**Key order is part of the on-disk contract.** `serde_json` here has no
`preserve_order`, so object keys iterate sorted, while Python dicts keep insertion
order and the store file carries it. Writing from a plain `Value` would give
different bytes for equivalent JSON — and this repository's subject is a backup
system. Added `python_json::OrderedJson` to spell the order out, with a test pinning
the exact expected file text.

**Quirks reproduced, not fixed:** the temp file is
`REMINDERS_FILE.with_suffix(".tmp")`, which *replaces* the suffix
(`reminders.json` -> `reminders.tmp`), so two writers collide on one name; and reads
are silent (missing/unreadable/malformed/wrong-top-level-type all degrade to empty,
non-dict entries dropped).

**`parse_due_at`** reproduces the subset of `datetime.fromisoformat` this module
meets plus Python's `isoformat()`: `YYYY-MM-DD`, `YYYYMMDD`, `YYYY-Www-D`, `T`/`t`/
space separator, `HH` through `HH:MM:SS.ffffff`, compact time, and `Z`/`+HH`/`+HH:MM`/
`+HHMM` offsets. A **lowercase `z` is rejected** (only an uppercase trailing `Z` is
rewritten). Anything outside the measured set raises the oracle's own message rather
than being guessed at.

**Seventh "guessed instead of measured".** My first ISO-week implementation validated
the week by checking the resulting date's *year* matched the stated year. Wrong in
both directions; `date.fromisocalendar` settled it:

| Input | Oracle | My first version |
| --- | --- | --- |
| `2026-W01-1` | `2025-12-29` (week 1 starts in December) | rejected |
| `2026-W53-1` | `2026-12-28` (2026 has 53 ISO weeks) | rejected |
| `2025-W53-1` | error (2025 has 52) | (would have accepted) |

The rule is: validate against *how many ISO weeks that year actually has*, from the
distance between consecutive week-1 Mondays. The unit test asserted the wrong
expectation too and now carries the measured values.

**Non-determinism is injected.** `secrets.token_hex(8)` and `int(time.time()*1000)`
arrive through an `Entropy` trait; production uses the OS CSPRNG (`BCryptGenRandom` /
`/dev/urandom`) and **fails loudly rather than falling back** to a weaker source,
since `secrets` is explicitly the secure option and a reminder id reaches the model
in tool output.

Verified locally:
- Byte-level parity: **identical MD5 `a636cd4590cff26d7809e8866fb3e500`**, 74 keys,
  no differences — 41 date forms, 8 create shapes, the exact store bytes, generation
  and lock file, temp hygiene, 8 status variants over two store states, 5 tolerant-read
  shapes, delete outcomes.
- `cargo test -p deepseek-policy` -> 171 tests, all pass (16 new).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` ->
  clean; `cargo fmt` applied.

Next: slice C, the scorer (`query_tokens` / `score_chunk` / `utc_now_iso` /
`latest_user_query`) — pure, and shared with `search_files`, so one port unblocks two
branches. `due_reminders` is ported and unit-tested but not yet in a compared corpus
(calling it mutates the store past the probe's last observation).

**Data layer slice C: the retrieval scorer (2026-09-15 九轮，uncommitted).**

`core_utils.rs` ports `query_tokens`, `score_chunk`, `utc_now_iso` and
`latest_user_query` from `core/utils.py`. Shared by the memory branches **and** the
RAG `search_files` branch, so one port serves two.

**A measured defect in the oracle.** `query_tokens` ends with
`sorted(tokens, key=len, reverse=True)[:80]` over a **set**. Python's sort is stable,
so equal-length tokens keep the set's iteration order, which depends on
`PYTHONHASHSEED`; when more than 80 tokens survive, *which* 80 are kept changes every
run. Measured:

    100 equal-length tokens, seed 1 -> x70,x17,x04,x11,...
    100 equal-length tokens, seed 2 -> x78,x43,x17,x67,...
    weighted case: seed 1 -> score 360; seed 5 -> score 390

The score ranks memories, so this leaks into tool output. The port therefore orders
by **length descending, then lexicographically** — deterministic where the oracle is
not. That is a deliberate divergence: there is no single oracle behaviour to
preserve, and CPython's set order is impossible to reproduce by construction. It
narrows a varying result to a fixed one and weakens nothing.

The probe matches that reality instead of hiding it: token lists are compared
**sorted**; inputs where more than 80 tokens survive report **only the count** (the
subset itself differs run to run); and a unit test pins the determinism as a property
of this port.

Signature difference, documented: `utc_now_iso()` reads the clock and takes no
argument in the oracle; this port is `utc_now_iso(epoch_seconds)`, so the clock is
supplied and can be pinned. The probe compares the *rendering* for four epochs.

Details that are easy to conflate: the tokenizer's character classes need **two or
more** characters while the weight is `max(2, min(len, 10))`; CJK bigrams are added
**on top of** the run itself; and a `set` dedupes windows, so `"中" * 60` yields
exactly two tokens.

Also fixed: a unit test asserted `query_tokens("Rust   OWNERSHIP") == ["rust",
"ownership"]`, the wrong order — `ownership` is longer and comes first. Same mistake
class as the previous six; the ordering rule now has its own test.

Verified locally:
- Byte-level parity: **identical MD5 `2170fb900a543b67163f1417d8af2c15`**, 37 keys,
  no differences — 13 tokenizer inputs, 2 capped inputs, 10 scoring inputs with token
  lists, 4 epoch renderings, 8 `latest_user_query` payloads.
- `cargo test -p deepseek-policy` -> 180 tests, all pass (9 new).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` ->
  clean; `cargo fmt` applied.

Next: slice D, the memory triple (`suggest_memory`, `recall_memory`, `forget_memory`)
— store + scorer + the fingerprint/category/conflict/sensitive logic. Nothing is
wired; `Branch::is_ported()` is unchanged for every data-layer branch.

**Data layer slice D: the memory triple (2026-09-15 十轮，uncommitted).**

`memory.rs` ports `infra/data/memory.py` plus the `suggest_memory` /
`recall_memory` / `forget_memory` branches; `file_lock.rs` factors out the OS lock
that both this module and the mutation gate need (the platform split now lives in one
place). A memory write passes through **three** layers, each doing a different job: a
process-wide mutex, a cross-process file lock on `.memory/memories.lock`, and the
workspace mutation gate.

**The bug this slice found, and how.** The first version put `mutation_scope` around
the *delete* path only, because that was the path I was reading. The oracle puts it
inside `_save_memories_unlocked`, so **every** save is fenced — including the
migration save. The probe caught it as a generation counter off by exactly two:

    delete::no-write-generation   Python 6   Rust 4

Six means three scopes had run (migration + two deletes), four means two. Fixed by
moving the gate into `save_unlocked`, where the oracle has it.

**A truthiness detail.** `_save_memories_unlocked` normalises `source` through two
Python `or` chains. My first version stringified any number and fell back otherwise,
which is wrong at both ends: `0` and `false` are falsy and become `"manual"`, while a
non-zero number and `true` become `"5"` / `"True"`. Fixed with an explicit
`python_truthy` covering `""`, `[]` and `{}` too.

**One deliberate gap, stated everywhere it matters.** `retrieve_memories` adds a
vector-search bonus from `local_rag.search_memories_index`. `local_rag` is 2,676 lines
and belongs to the RAG slice, so the bonus arrives through an injectable `VectorHits`
provider defaulting to none. The oracle wraps the call in `try/except Exception` and
falls back to an empty map, so the default reproduces the oracle's **own degradation
path** and the probe compares that. But when the vector index is populated the
oracle's scores include a bonus this does not. **`recall_memory`'s ranking is verified
only where the vector index contributes nothing** — recorded in the docs, the matrix
and the module docs.

Also ported faithfully from the write path (it doubles as the migration): non-objects
and empty content dropped; `id` falls back `memoryId` -> `id` -> a **content-addressed**
`sha256(...)[:20]`; `confidence` default 0.9 clamped to [0,1]; `type` derived from
`type` -> `category` -> `"fact"`; timestamps through the injected clock; cap 400.
Reads stay silent on corruption.

Verified locally:
- Byte-level parity: **identical MD5 `4261dd06c31ec2de180601f8d80e5cca`**, 92 keys, no
  differences (text/scope/fingerprint/sensitive/category/conflict helpers, tool scopes,
  suggest, the loaded and migrated store bytes, tolerant reads, recall, forget, delete
  semantics with generation counters, conflict queries).
- `cargo test -p deepseek-policy` -> 206 tests, all pass (26 new).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` ->
  clean; `cargo fmt` applied.

Last data slice is E (`projects`, blocked on `rag/files.py`'s `load_cached_file`).
`suggest_memory` does not persist: it builds a suggestion and fires a callback, so
`upsert_memory`, `clear_memories` and `delete_memory_by_id` are not ported and are not
needed here. Nothing is wired.

**Data layer slice E1: the projects read path (2026-09-16，uncommitted).**

The last data domain, split in two because the measurement showed the halves have very
different dependencies. **E1 is done**: the projects store — `validate_project_id`, the
whole `normalize_*` family, `read_project`, `public_project`, `list_projects`.
**E2 is not started**: `load_cached_file` plus the two branch wrappers.

`read_project` re-normalises **six** collection fields on every read, so the normaliser
family is on the critical path even for a branch that only looks at `documents` — and
`normalize_skill_run` alone has **thirty fields**. That is why a ~45-line pair of
branches needs a store-sized slice.

**A real finding: the read path mints random ids.** `normalize_skill_run` and
`normalize_saved_items` generate `f"run-{secrets.token_hex(8)}"` / `f"saved-…"` whenever a
stored entry has none, and `read_project` calls them — so **reading the same malformed
project twice returns different values**. Measured: `run-d9d3e527ae4f29df` then
`run-5acb6344a2c0e2bf`. Not persisted (read never writes back), so it is a phantom id, but
it is observable through `public_project`, which `list_projects` returns to the model. The
port keeps the behaviour and takes the source through the shared `entropy::Entropy` trait.
That is why `Entropy` moved out of `reminders` into its own module — a second user appeared.

**`OrderedJson` had a real bug, exposed here.** Store records ported so far were flat, so
nested containers were being written **compactly** where Python's `indent=2` indents at
every level. A project record is not flat. Fixed by converting nested values into real
nodes — and this mattered beyond the probe, since a memory `source` object would have hit
the same bug. Residual limit stated rather than hidden: nested object **keys** come out
sorted, because `serde_json` here has no `preserve_order`.

**Two error-shape details.** `unique_strings(None)` **raises** in the oracle (`list(None)`
is a `TypeError`), so the port reproduces that and restores the `or []` guard at all six
call sites — which is what makes the raise unreachable from ported code. And that
`TypeError` has **no code**; this port reports `invalid_payload` with a matching message, a
documented mapping rather than an invented code, so the probe compares the message and
deliberately not the code.

Verified locally:
- Byte-level parity: **identical MD5 `787f519d69e6b4a295732891fa84777b`**, 76 keys, no
  differences (11 id shapes, 7 name shapes, 8 document shapes, 13 `_safe_int` shapes, 5
  `unique_strings` shapes incl. both raises, 9 skills shapes, 6 skill runs incl. one with
  all thirty fields, saved items and artifacts with generated ids, 5 tolerant reads,
  `require_project` hit and miss, `list_projects` ordering with an invalid dir and a loose
  file).
- `cargo test -p deepseek-policy -- --test-threads=1` -> 206 tests, all pass.
  **Note:** `mutation_gate::tests::concurrent_scopes_serialize_and_count_exactly` is flaky
  under the default parallel harness (passes in isolation and serially, twice). This crate
  already has a known class of process-level shared-state interactions; run the suite with
  `--test-threads=1` when it matters.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` -> clean.

Next: **E2** — `load_cached_file` (self-contained: 32-hex id check, `PROJECTS_DIR/<id>/files`
path, JSON read, `lru_cache(64)` keyed on `(file_id, mtime_ns)`, `file_index_expired` 410)
and the two wrappers. Then the data layer is complete and `Branch::is_ported()` can be
revisited. Nothing is wired.

**Data layer slice E2: the file-cache read path and the two branch wrappers (2026-09-16，uncommitted).**

`file_cache.rs` + the `list_project_files` / `read_file_chunk` branches in `projects.rs`.
The measurement held up: `load_cached_file` really is an id-shape check, a path
derivation, a JSON read and a cache, so the rest of that 1,494-line RAG module stays
untouched. **The data layer is now complete** — reminders, memory, the shared scorer,
projects.

Three details that are easy to get wrong, and were:

1. **The `lru_cache(64)` only applies without a project id** — a project-scoped read
   always re-reads. Key is `(file_id, mtime_ns)`, which is what stops a changed file
   hitting a stale entry. `FileCache` reproduces the bound and move-to-front-on-hit.
2. **`if project_id` tests the RAW value, not the stripped one.** A whitespace-only
   project id is truthy, so it reaches `project_file_cache_dir`'s shape check and fails
   with a 400 — it does **not** fall back to the global cache. The wrapper
   (`read_file_chunk`) strips first and passes `None`, so a blank id from the tool *does*
   use the global path. **Two different behaviours for a blank id, one call apart**; the
   probe caught my first version collapsing them.
3. **`int()` here is the bare one, not the store's `_safe_int`.** `"3.7"` and `"abc"`
   **raise** rather than falling back; a float truncates toward zero; `"1_0"` and
   `"  8  "` parse. `python_int` is deliberately separate from `safe_int`, with the same
   documented mapping as the projects `TypeError`.

Also faithful: `preview` is capped at **500** in the tool projection but **1800** in the
store; `count` sums the *emitted* files (after both caps); a `chunks[index]` that is not
an object is a 404, not a skip.

Verified locally:
- Byte-level parity: **identical MD5 `5baaaba2565bed0542b652627891039d`**, 29 keys, no
  differences — 4 file-id shapes, missing/malformed/scalar indexes, the project-scoped
  path, the blank-id 400, the `project_file_cache_dir` path, 13 chunk cases (default,
  explicit, zero, negative, out of range, non-dict chunk, missing/非-list `chunks`,
  project-scoped, invalid project id), and `list_project_files` named/missing/invalid-id
  plus the full `list_projects` payload shape with its two caps.
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` -> **214 tests, all pass**, run
  twice.

**One open item, stated plainly.** `mutation_gate::tests::concurrent_scopes_serialize_and_count_exactly`
failed **once** during this slice and passed on every other run — in isolation, serially,
and in two full serial runs. The symptom is a thread panicking inside its scope. Likely
cause is **parity, not a defect**: `lock_exclusive` reproduces `LK_LOCK`'s "retry once a
second, give up after ten attempts", so under contention the gate **errors** after ~10s
where a plain blocking lock would have waited — the oracle does the same. The test's
`.unwrap()` turns that refusal into a panic. **Not root-caused.** If it is the retry
budget the fix belongs in the test, not the lock semantics; if it is not, something else
is sharing state between tests, and that matters. Re-examine before wiring.

**Gate fidelity fix: poisoning is a failure mode the oracle does not have (2026-09-16，uncommitted).**

Follow-up to the open item recorded in E2. The intermittent failure of
`concurrent_scopes_serialize_and_count_exactly` did **not** reproduce on demand (5 further
full serial runs, all 214-green), so instead of shrugging I looked for a failure mode this
port has and the oracle does not. There was one:

**`std::sync::Mutex` poisons; Python's `threading.RLock` does not.**

`exclusive_gate` acquired `PROCESS_LOCK` with `.lock().map_err(...)?`, so once **any**
thread panicked while holding that mutex, every later acquisition in the process returned
an error — and `mutation_scope(...).unwrap()` in the test would panic with exactly the
observed shape. That was a real fidelity gap whether or not it explains this failure.

Fixed: recover from poisoning (`unwrap_or_else(PoisonError::into_inner)`) instead of
reporting it. The same applies to `MEMORY_LOCK` / `STORE_LOCK`; those call sites already
bound the whole `LockResult` and so were tolerant by accident of style — now documented as
intentional, because it is load-bearing.

**Narrowed, not closed.** Eleven subsequent full runs pass. If it recurs, the remaining
candidate is the gate's `lock_exclusive` retry budget (ten attempts a second apart,
faithful to `msvcrt.LK_LOCK`, but it means the gate *errors* under sustained contention
where a plain blocking lock would wait) — in which case the fix belongs in the test, not
in the lock semantics.

**Wiring-surface measurement (for the next slice).** `deepseek-gateway` already depends on
`deepseek-policy`, but only uses `PolicyDecision`/`codes` — it does **not** reference
`tool_dispatch` or `is_ported`. The `Branch` enum already carries all seven data variants
with `tool_name()` and `branch()` mappings. But **nothing executes them**: no caller
anywhere invokes `reminders::create_reminder` or `projects::list_project_files`. So wiring
is not "flip `is_ported()`" — it needs an executor plus routing, and end-to-end
verification. That is its own slice.

---

## E7 (2026-09-16): the gateway wiring — `dispatch()` has a production caller

**HEAD before this slice: `d92953bb` (main). The slice follows the seven data branches
being wired into the dispatcher (`432318d1`).**

The executor-plus-routing slice the measurement called for. Three pieces:

1. **`rust/crates/deepseek-gateway/src/chat_tool_loop.rs`** — the non-streaming tool
   round loop, mirroring `call_deepseek`'s loop body in the oracle's order:
   `exchange_turn` → `merge_usage_totals` → lenient `tool_calls` normalization →
   `decide_round` → `execute_tool_calls` (runner = `dispatch`) →
   `append_tool_exchange`; `force_final_answer_without_tools` at budget exhaustion;
   `final_answer` from the last turn plus the merged usage.
   - `WorkspaceBundle` (root + `FileCache` + `SystemEntropy` + `SystemClock`) is the
     oracle's module globals as one injectable object; the root comes from
     `DEEPSEEK_INFRA_ROOT`, and unset ⇒ the data branches answer
     "not enabled for this request", never a silent no-op.
   - `ToolRoundExecutor::from_env` builds the policy the oracle's
     `build_tool_policy` produces for main chat: `ToolPolicyConfig::default()`
     (capability `full`, `enforce_schema`/`require_confirm` off, `sanitize` on,
     `TOOL_POLICY_ENABLED` default on with `_env_bool` spellings) plus the
     process's `DEEPSEEK_API_KEY`/`AUTH_TOKEN` as the secrets blocklist. One
     policy object lives across the request — counters accumulate like the
     oracle's single `tool_policy`. The per-call lock recovers from poisoning
     (`PoisonError::into_inner`) for the same reason the stores do.
   - Execution runs on `spawn_blocking` (the data branches take OS file locks);
     a panicked blocking task resolves every selected slot through the batch
     layer's own "did not run" envelope rather than inventing results.

2. **`chat_execution.rs` reworked around turns** — `UpstreamTurn` +
   `turn_from_payload` (extraction does not refuse `tool_calls`; that is the
   loop's data), `exchange_turn` (the POST), `merge_usage_totals` +
   `usage_int` upgraded to Python `int()` coercion semantics (numeric strings,
   float truncation, bool), `final_answer` (keeps the facade's pre-existing
   empty-content refusal, now also covering the budget-exhausted partial turn).
   `ToolRoundsUnwired` / `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` is **deleted** from
   the non-streaming path; the SSE path keeps refusing in-band via
   `STREAM_TOOL_ROUNDS_NOT_READY` (streaming round continuation is its own seam).

3. **The route is actually reachable** — `/v1/chat/completions` now prepares the
   raw body through `prepare_chat_request` instead of re-encoding through the
   typed `ChatCompletionRequest` struct, which silently dropped every field it
   did not enumerate — including `tools`, without which the model could never
   have called anything and the loop would have been dead code on arrival.
   Malformed JSON → 400 "request must be valid JSON"; a malformed-typed field
   now surfaces as the preparation layer's own 400 instead of axum's 422.

**Honest state.** Eleven of eighteen branches execute for real. The other seven
(`browser_*`, `python_eval`, `search_files`, `fetch_url`, `create_mindmap`,
`create_pptx`, `create_document`) resolve to the visible `Tool did not run`
envelope — a degradation against the Python oracle for those tools, on an
opt-in sidecar, stated in the loop's module docs and pinned by a boundary test.
Also absent with owners: the web-search provider, `mcp__*` bridging, artifact
terminal handling, and the loop's surrounding machinery (semantic cache, memory
retrieval, scheduler, traces, budget ledger). Divergences kept on purpose are
listed in `docs/GATEWAY_TOOL_DISPATCH.md` (empty-content refusal, env-injected
root, `""` vs `null` assistant replay, no `memorySuggestions` channel).

**Verification.**
- `cargo test -p deepseek-gateway -j 1` → 129 lib + 7 `chat_execution` boundary
  (four new: continuation through dispatch, data branch against the workspace
  incl. the fence files landing under `DEEPSEEK_INFRA_ROOT`, budget exhaustion
  incl. the `MAX_TOOL_ROUNDS + 2` turn count and `tool_choice: "none"`, unported
  branch honesty) + 6 `chat_stream` + 2 control-boundary — all pass.
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` → 223 pass.
- `cargo clippy -p deepseek-gateway -p deepseek-policy --all-targets -- -D warnings`
  → only the pre-existing `control_proxy.rs:20` `result_large_err` (byte-identical
  to HEAD; local rustc 1.97.1 vs declared 1.85). One same-class local-toolchain
  lint (`unnecessary_sort_by` in `python_json.rs`) fixed mechanically — the two
  sort forms are identical.
- `cargo fmt --all -- --check` clean.

**Next.** The streaming tool loop (SSE round continuation interleaved with
`system_note`), the web-search provider behind `ExecutorContext.web_search`, and
the `schema_for_tool` catalog.

**Bug fix: the streamed tool-call accumulator coerced values the Rust way, not Python's
(2026-09-16，uncommitted).**

Found while checking whether E7's `ToolCallAccumulator` needed anything before the
streaming loop is built on it. It did — and the accumulator is **committed code from an
earlier slice**, so these are pre-existing bugs, not fallout from E7.

The oracle reads the delta index through a bare `int()`:

```python
index = len(accumulator) if index_value is None else int(index_value)
```

This port read it with `as_i64()`, which is strictly narrower. Measured against the real
`merge_stream_tool_call_deltas` before changing anything:

| delta | oracle | this port (before) |
| --- | --- | --- |
| `"index": "2"` | slot **2** | `len(accumulator)` = 0 |
| `"index": true` | slot **1** | `len(accumulator)` = 0 |
| `"index": 2.7` | slot **2** | `len(accumulator)` = 0 |
| `"id": 123` | `"123"` | placeholder `call_1` |

**The index decides which tool call a fragment lands in.** Sending three of those to
`len(accumulator)` merges the arguments of unrelated calls into one slot — a wrong tool
invocation, not a cosmetic difference. The id case is the same class the lenient
normalizer already guards with a comment ("Reading only string ids here would silently
renumber such calls"); the accumulator had the gap.

Fixed by using Python's semantics rather than Rust's: `python_int_opt` for the index,
`python_truthy` + `value_str` for `id` / `type` / `function.name` / `function.arguments`.
The slot key widened from `usize` to `i64` because `int()` accepts a negative index and
Python's dict holds one; `sorted()` then orders it first, which the new test pins.

**Consolidation this forced, and that is the real win.** `deepseek-policy` now has one
implementation of each Python coercion in `core_utils`, used by three call sites:
`python_int_opt` (the file-cache read path maps its failure to the documented 500; the
accumulator falls back to the running slot count) and `python_truthy` (the stores,
the file cache, the accumulator). `file_cache::python_int` and `projects::is_truthy`
delegate, so their committed APIs are unchanged. Two small corrections fell out of
writing the shared version: `"1__0"` and a non-finite float are both rejected by Python's
`int()` and were previously accepted.

Verified:
- Three new gateway tests pin the measured divergences and the negative-index ordering
  (`the_index_coerces_the_way_pythons_int_does`,
  `a_negative_index_orders_before_the_others`,
  `a_non_string_id_is_stringified_and_a_falsy_one_is_ignored`), plus one in
  `core_utils` for the shared coercion.
- `cargo test -p deepseek-gateway -j 1` -> 132 lib + 7 + 6 + 2, all pass.
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` -> 224 pass.
- `cargo clippy` -> only the pre-existing `control_proxy.rs:20`; `cargo fmt --check` clean.

**Not reachable from DeepSeek's own API today** — it sends `index` as a JSON number and
`id` as a string, so the two implementations agree in practice. That is exactly why it
was worth fixing rather than noting: the divergence is invisible until a provider
changes shape, and then it corrupts tool-call assembly instead of failing.

**Streaming slice, step 1: the SSE decoder now yields every delta a chunk carries (2026-09-16，uncommitted).**

Prerequisite for the streaming round loop, and a real divergence on its own.

The oracle's per-chunk body in `stream_deepseek` does everything **in one pass** —
it does not short-circuit:

```python
choices = chunk.get("choices") or []
if not choices: continue
delta = choices[0].get("delta") or {}
if choices[0].get("finish_reason"): round_finish = str(...)
if isinstance(chunk.get("usage"), dict): round_usage = chunk["usage"]
merge_stream_tool_call_deltas(stream_tool_calls, delta.get("tool_calls"))   # always
if delta_reasoning: ... forward reasoning ...
if delta_content:   ... forward content  ...
```

This port's `decode_chunk` checked `chunk_has_tool_calls` **first** and returned
`UpstreamDelta::ToolCalls`, dropping the same chunk's `content`, `reasoning`,
`finish_reason` and `usage`. Confirmed by reading the oracle, not by inference.

That is not cosmetic: the dropped `content` is the text `append_tool_exchange` replays
to the provider as the round's assistant message. A round-ending chunk that also
carried prose would have lost it.

**Fix.** `decode_event` / `decode_chunk` return `Vec<UpstreamDelta>` in the oracle's
order, and `UpstreamDelta` gained payloads: `ToolCalls(Value)` (the fragments the
accumulator needs), `Usage(Value)` and `FinishReason(String)`. `forward_line` iterates
and, when a tool round appears, forwards the chunk's content **first** and emits the
refusal **last** — the earlier ordering would have put the refusal ahead of text the
model did produce, which reads as the text being the problem.

An existing test asserted the old behavior under a name that defended it
(`a_tool_call_chunk_is_typed_as_tool_calls_even_with_content`, "forwarding the prose is
the silent-flattening failure the refusal exists to prevent"). That reasoning was
wrong: the oracle forwards the prose too. The test is replaced by one that pins the
oracle's behavior, plus two more for the ordering and for the round-ending chunk's
`usage` / `finish_reason`.

**The refusal itself is unchanged and still loud.** Streaming clients still get
`NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` on a tool round; what changed is that they get the
round's text first. The loop body (the `for tool_round in range(max_tool_rounds + 2)`
structure, the `system_note`s, `append_tool_exchange` and the next upstream request) is
the next step — the body is an `async_stream` generator, so awaiting a new upstream
mid-stream is already possible.

Verified:
- `cargo test -p deepseek-gateway -j 1` -> 134 lib + 7 + 6 + 2, all pass (3 new, 1
  replaced).
- **SSE byte-parity holds**: `tasks/native-runtime/sse_parity_probe.py` against the Rust
  example, identical MD5 `b9129475b6bae8b1239f4529e0a50932`. Note the corpus could not
  have caught this divergence — it has no chunk carrying both `content` and
  `tool_calls`, and it could not, because this transport refuses on a tool round where
  the oracle continues. The unit tests are the right level for it.
- `cargo clippy -p deepseek-gateway --all-targets -- -D warnings` -> only the
  pre-existing `control_proxy.rs:20`; `cargo fmt --check` clean.

**Streaming slice, step 2: the round loop. The refusal is gone (2026-09-16，uncommitted).**

`streaming_response` now runs the tool rounds, so `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY` is
deleted rather than kept as a seam. Streaming clients get the same round continuation the
non-streaming path has had since E7.

The shape mirrors `stream_deepseek`'s `for tool_round in range(max_tool_rounds + 2)`:

- each round streams one upstream turn, forwarding `content` as it arrives while
  accumulating the `tool_calls` fragments into the existing `ToolCallAccumulator`;
- at the end of the round `finalize()` + `decide_round` decide: no calls → the stop frame
  and the loop ends; budget spent → `force_final_answer_without_tools` and one more turn;
  otherwise → `executor.run_round` + `append_tool_exchange` and **a fresh upstream request
  opened from inside the generator** (a response body is single-shot, so every further
  round is a new request);
- `decode_event`'s per-chunk deltas are handled inline rather than through a helper,
  because the generator has to `yield` between them and a helper cannot yield on its
  behalf. That deleted `forward_line` and the refusal constant, and an obsolete test.

The round decision, the exchange assembly, the tool execution and the usage merge are the
**same functions** the non-streaming loop calls, so the two transports cannot drift.

**What is deliberately not emitted.** The oracle's `system_note`s (`正在调用本地工具…`, the
budget notice, the `finish_reason: "length"` truncation notice) never reach this endpoint:
`openai_chat_stream` maps only `content`, `done` and `error`. Same for the per-round `usage`
— the facade's frames carry no usage field. Both are still decoded, so the loop is not
reading a shape it cannot see, but they have no wire effect here.

**Verification.** The new `streaming_continues_a_tool_call_round` drives the real
`/v1/chat/completions` route against a per-request stub upstream and asserts four things:
both rounds' content arrives in order; there is no error frame; the reminder was actually
written under `DEEPSEEK_INFRA_ROOT`; and **the second upstream request carries the
exchange** — the assistant `tool_calls`, the `tool_call_id` and the tool result content.
That last assertion is the one that would catch a loop that ran but replayed nothing.

- `cargo test -p deepseek-gateway -j 1` -> 133 lib + 7 + 6 + 1 + 1, all pass.
- `cargo clippy -p deepseek-gateway --all-targets -- -D warnings` -> only the pre-existing
  `control_proxy.rs:20`.

**The intermittent gate failure recurred, and the poisoning fix was not the cause.**
`mutation_gate::tests::concurrent_scopes_serialize_and_count_exactly` failed once more
(223 passed, 1 failed, `--test-threads=1`) and then passed three runs in a row. That was
the honest label's payoff: it was recorded as "narrowed, not closed" precisely because the
poisoning gap was a real fidelity bug but never proven to be *this* failure. Now it is
disproven as the sole cause. Not captured this time (the reruns were green); the next
occurrence needs the panic message, which the earlier note never managed to record.

**Root cause of the intermittent gate failure: the lock file was reopened to seed it
(2026-09-16，uncommitted).**

Three rounds of this. Round 1 recorded it as "narrowed, not closed" after fixing a mutex
poisoning gap; round 2 saw it recur, which disproved poisoning as the sole cause. This
round captured the panic, and the cause was in the port all along.

**The evidence.** Reproduced on the 7th of 15 full serial runs:

```
thread '<unnamed>' panicked at mutation_gate.rs:741:58:
called `Result::unwrap()` on an `Err` value: GateError { kind: RuntimeError,
  message: "另一个程序已锁定文件的一部分，进程无法访问。 (os error 33)", code: None, status: None }
```

`os error 33` is `ERROR_LOCK_VIOLATION`: Windows refuses a **write-mode open** of a byte
range that another handle has locked, and refuses writes into it.

**The mechanism.** `exclusive_gate` created the lock file and then **reopened it for
write** to seed the byte:

```rust
if let Err(error) = OpenOptions::new().create_new(true).write(true).open(&target) { … }
else {
    let mut file = OpenOptions::new().write(true).open(&target)?;   // ← reopen
    file.write_all(b"0")?;
}
```

Between the create and the reopen, another thread can reach the OS lock on byte 0. The
reopen-for-write then fails with error 33, the code reports `GateError::misuse`, and
`mutation_scope` returns `Err` — which the test unwraps. The lock file only exists once
per workspace, so the window only opens while the file is being created, which is why it
took the full suite (many workspaces) and roughly one run in seven to hit.

**The fix is the oracle's shape, not a guess.** `_lock_file`/`exclusive_gate` in
`infra/workspace/mutation_gate.py`:

```python
try:
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
except FileExistsError:
    pass
else:
    os.write(descriptor, b"0")     # the SAME descriptor, then closed
    os.close(descriptor)
with _PROCESS_LOCK:
    ...
    with target.open("r+b") as handle:   # the only open for locking
        _lock_file(handle)
```

So the byte is written through the handle that created the file, and there is exactly one
other open — inside the process lock, for locking. **The reopen was an invention of this
port.** Fixed by writing through the creating handle.

**The regression test had to be able to fail.** `racing_first_scopes_never_fail_on_the_lock_file`
removes the lock file every round and races eight threads through `mutation_scope`, 25
rounds, because the window only opens during creation. Verified in both directions: it
passes with the fix, and it fails with the pre-fix reopen restored.

**What this round changes about the record.** Rounds 1 and 2 both said "not root-caused",
and that was right to say — the poisoning fix was a real fidelity bug, and describing it
as *the* cause would have been a plausible story standing in for evidence. The lesson is
the one already in the notes from the object-store work: a fixed bug is not a fixed
symptom until the symptom stops.

**The numbers.** Failure rate before: 1 in 7 full serial runs (run 7 of 15). After: **0 in
15**. The regression test, run against the pre-fix code restored temporarily: **failed on
run 2 of 5** at the thread's assert — so it is a test that can fail, not decoration. Run
against the fix: 5 of 5 green. Both directions measured, not asserted.

`cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` -> clean
(the only output is a transient `deepseek-core` incremental-artifact copy warning, which
is not a lint). `cargo fmt --all -- --check` -> clean.

**Tool catalog ported, generated rather than transcribed (2026-09-16，uncommitted).**

`schema_for_tool` and the web-search provider were the two remaining items. Measuring
them showed the catalog is the **shared** dependency — `schema_for_tool`,
`tool_parameter_schemas`, `agent_tool_definitions` and `tools_for_payload` all sit on it —
so it came first, and the web-search provider is its own slice (below).

**The 28 definitions are not typed out by hand.** They are the oracle's own
`json.dumps(available_tool_definitions(), ensure_ascii=False, indent=2)` bytes, 40,634 of
them, committed as `rust/crates/deepseek-policy/assets/tool_catalog_v1.json` and embedded
with `include_str!`. Hand-copying 40 KB of descriptions and JSON Schema is where typos
live, and a typo inside a `parameters` block would silently change what the model may
send. The probe's `asset::json` case is the guard: it compares the embedded text with the
oracle's rendering, so the committed bytes cannot drift without the diff failing.

**Three divergences caught by reading the oracle instead of guessing.** The appended
external-MCP definition is not what a reasonable guess produces:

```python
tools.append({
    "type": "function",
    "function": {
        "name": profile.bridged_name,     # a profile field, not derived from `tool`
        "strict": True,                   # easy to miss entirely
        "description": f"[External MCP: {profile.server}] {schema_desc}",
        "parameters": parameters,
    },
})
```

and `parameters` is `raw_schema if raw_schema.get("type") == "object" else
{"type": "object", "properties": raw_schema}` — the **whole** raw schema goes under
`properties`, not its entries. My first version derived `mcp__{tool}` as the name, omitted
`strict`, and spread the free-form schema's keys into `properties`. All three are now the
measured shape.

**The external arms are injected, with a documented default.** `schema_for_tool`'s
`mcp__` branch and `agent_tool_definitions`' appending both need `infra.mcp.bridge`. They
take an injected provider / profile list, and `None` reproduces the oracle's own
`except Exception: pass` degrades-to-local arm — so the default is the oracle's behaviour
rather than an invented one.

Verified:
- **Catalog parity holds**: identical MD5 `a2fa62de54c6f95e85065f6c008b8e58`, 18 keys, no
  differences — the asset bytes, the 28 names in declaration order, the schema index
  (count, sorted names, four full schemas), eight `schema_for_tool` cases including the
  trim, the unknown name and the empty string, and `agent_tool_definitions` with no
  bridge.
- Six unit tests cover the arm the probe cannot reach: an `mcp__` name with a bridge, a
  non-object profile schema (rejected by `schema_for_tool`, wrapped by
  `agent_tool_definitions`), and an unknown external name.

**The web-search provider is measured and left, on purpose.** It is not pure:
`_perform_web_search` needs `search_single_round` (a real Tavily HTTP call),
`tavily_api_key`, a per-request result cache, a citation counter, a turn limit and a
shared `search_budget`. `ExecutorContext.web_search` is already the injection point, so
the Rust side has the seam — what is missing is the HTTP integration and its config, which
is a connector-shaped slice that needs either a live key or a stub upstream to verify. Say
that plainly rather than half-wiring it.

**Also measured while sizing this, now unblocked:** `search_tool_enabled` and
`tools_for_payload` are pure and depend only on the catalog plus `search_mode`. They are
the natural companions to this slice whenever the search provider lands.

**Tavily search layers 1+2 ported: query planning, normalization, ranking, cache (2026-09-16，uncommitted).**

`search.rs` now carries everything the `web_search` tool branch needs from
`infra/tool_runtime/search.py` **except the HTTP call**. The boundary is a dependency
closure, not taste:

- **`format_search_context` / `format_search_failure_context` are not in it.** They build
  the *prompt context* at request-assembly time; the tool branch returns a compiled tool
  result and never calls them. Porting them would be porting a different consumer.
- **`search_tavily` / `search_tavily_with_retry` are not in it either** — but their retry
  *policy* is ([`should_retry_tavily_error`], [`simplified_retry_query`]). Only the request
  itself is missing, which is the next slice.

**One divergence, caught by the probe.** `domain_from_url` is
`urlsplit(url).netloc.lower().removeprefix("www.")` — and `netloc` is the **whole
authority, userinfo and port included**. Extracting just the host reads as the obvious
cleanup and is wrong: for `https://user:pw@Host.COM:8443/x` the oracle returns
`user:pw@host.com:8443` and my first version returned `host.com`. The probe diff was a
single line out of 97 keys. It is now the whole netloc.

This matters beyond the field itself: `search_result_score` feeds `domain` into
`TRUSTED_DOMAIN_HINTS` with a `contains` check and `rerank_search_results` uses it as the
per-domain diversity key, so a narrowed domain changes both ranking and the
two-per-domain cap.

**A recurring trap, hit a third time.** `serde_json`'s `json!` does not accept a **block
expression** as a value, so `"retryQuery": { let v = ...; if ... { v } else { json!("") } }`
fails with `unexpected end of macro invocation`. The fallbacks have to be hoisted into
`let` bindings first. Same class as `.iter()` on a temporary and `&"x".repeat(n)` in a
`Vec<&'static str>`: the macro's accepted grammar is narrower than the expression grammar.

**What is reproduced rather than tidied:** `search_cache_key` lowercases while its callers
pass the raw query (so the cache is case-insensitive by construction);
`save_search_cache` **prunes before writing**; the temp file is `with_extension("tmp")`,
which replaces `.json` rather than appending; `search_result_score`'s weights (score × 20,
title token +8, body token +3, trusted domain +10, official-docs +6, empty snippet −8) and
`rerank`'s two-per-domain cap run after the sort.

Verified:
- **Search parity holds**: identical MD5 `a425954350aeea8c6d935c476bda169e`, 97 keys, no
  differences — six query shapes through nine distinct functions, nine `should_search_for_query`
  cases across four modes, six intents, five URL authorities, three `normalize_search_response`
  shapes, eight per-result scores, the reranked URL order, the full aggregation (status,
  joined answer, reason, result URLs, normalized rounds), two compactions, round statuses,
  round ordering, and two cache round-trips.

**About the pasted credentials.** A live Tavily key and what appears to be an upstream API
key were pasted into the chat. Neither was written to any file (verified with a repo-wide
grep), neither was persisted as an environment variable, and all probe artifacts were
deleted. They still appear in this conversation's transcript, so both should be **rotated**
regardless of what this session did with them.

**What is left.** The HTTP layer: `search_tavily` (request body assembly, the `TAVILY_URL`
POST, `AppError` mapping for a missing key and for upstream failure) plus
`search_tavily_with_retry`, and a shared clock for `load_search_cache` /
`cleanup_search_cache` / `save_search_cache` (their `now_epoch` parameter is already there,
so only the wiring is missing). Verification for that slice is a **stub upstream**, because
the live path measured ~5% availability — one clean `http=200` in roughly forty attempts,
amid 308/405/400/301/502/522 from the proxy and its intermediaries. A real-call check stays
a one-off confirmation, not a regression test.
