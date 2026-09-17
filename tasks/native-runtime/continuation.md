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

**Tavily HTTP layer ported, with the transport injected (2026-09-16，uncommitted).**

`search.rs` now carries `search_tavily`, `search_tavily_with_retry`, `format_upstream_error`
and the request-body assembly. That completes the module's non-cryptographic surface: what
is still missing is only the **client**, not the logic.

**The transport is a parameter, not a call.** `search_tavily(query, api_key, transport)`
takes a `dyn Fn(&str, &[u8], &[(&str, &str)]) -> TransportOutcome`, so the whole path —
body assembly, header construction, status mapping, response normalization, retry policy —
runs offline. That is what made the parity probe possible without a network, and it is why
the measured ~5% link availability does not block this slice.

`TransportOutcome` has three arms on purpose: `Response` (any status, with its body),
`Failure { reason, timed_out }` (the request never completed — the oracle's `URLError`
branch), and `Rejected(AppError)`. The third exists **for the probe**: the Python probe
drives the retry policy by raising an `AppError` from a stubbed `search_tavily`, so without
it the Rust side would be comparing "error mapping **and** retry policy" against Python's
"retry policy alone". The arm makes the layers line up.

**A real divergence, caught by the probe.** `format_upstream_error` is:

```python
message = error.get("message") or error.get("type")
if message: return str(message)
```

The `or` tests the **values' truthiness**, so `{"error": {"message": "", "type": "x"}}`
returns `"x"`. My first version checked the key's presence and then whether the rendered
text was empty, which fell through to the raw text instead. Fixed to filter both lookups
through `python_truthy`.

**A byte-level detail worth naming: `json.dumps` defaults to `ensure_ascii=True`.** The
request body sends `{"query": "\u6700\u65b0\u6d88\u606f"}`, not the raw UTF-8. Escaping is
CPython's exactly — BMP as a lowercase `\uXXXX`, an astral character as a lowercase
surrogate **pair** (`\ud83d\ude00`). Added `dumps_default_separators_ascii` /
`escape_non_ascii` to `python_json` for it. This is not cosmetic for a request body: the
bytes are what leave the process.

The body's **key order** is the oracle's dict-merge order — `query`, then the options in
their insertion order, then the filters — and `search_depth` / `include_answer` /
`include_raw_content` are *updated in place* by the intent rules, so they keep their
positions rather than moving to the end. `tavily_request_body_json` renders that order
explicitly, so the probe compares the bytes rather than a re-serialization.

**Two probe-side fixes, so the comparison is honest.** Python's f-string renders an enum
*member* (`ErrorCode.UPSTREAM_TIMEOUT`), not its value, so the stub messages had to use
`code.value`. And the Python fake replaces the whole `search_tavily`, so it has to apply
`normalize_search_response` itself — otherwise the two sides are compared at different
layers and the response bodies diverge for a reason that is not the implementation's.

Verified:
- **Search parity holds**: identical MD5 `b5077e1e730dfac3ffde3e024c6094cf`, **113 keys**
  (up from 97), no differences. The HTTP additions are five request bodies — including a
  600-character query, which exercises the `[:500]` truncation — six
  `format_upstream_error` inputs, and five retry-policy drives (first-call success, retry
  after a timeout, retry after a 503, both attempts failing, and a missing key that must
  **not** be retried).

**What is left.** A concrete `Transport` (a `reqwest::blocking` client honouring
`TAVILY_TIMEOUT_SECONDS`), the shared clock for the three cache functions, and the
`ExecutorContext.web_search` callback that binds them — the seam already exists, so this is
wiring rather than logic. Verification stays a **stub upstream**; a live call is a one-off
confirmation, since the measured link was one clean `http=200` in roughly forty attempts.

**Scoping: `format_search_context` is one link in an unported, security-bearing pipeline
(2026-09-17，measured not started).**

Both remaining `format_*` functions were previously listed as "the next slice". Measuring
the call path says they are not a slice of their own — they are the last step of a pipeline
whose other links, including a security module, are unported. Writing them alone would be
inert code with no consumer.

The pipeline, from `deepseek_client.py`:

```python
search_data = search_if_needed(payload, progress_callback=…, system_note_callback=…)
...
if search_data and search_data.get("results"):
    # Context Taint firewall: web content is untrusted — isolation-wrap and
    # scrub the per-turn search context before it joins the prompt.
    payload = {**payload, "searchContext": context_taint.harden_search_context(
        format_search_context(search_data))}
elif search_data and search_data.get("status") == "error":
    payload = {**payload, "searchContext": format_search_failure_context(search_data)}
prepared = build_deepseek_request(payload, stream=stream, memory_state=memory_state,
                                 validated=validated)
```

**Measured size of the missing links:**

| link | size | notes |
| --- | --- | --- |
| `search_if_needed` | ~35 lines | gates on `searchEnabled is True` **and** `forced_search_mode`; raises `INVALID_PAYLOAD` on an empty query; emits up to four `system_note`s |
| `search_multiple` | ~45 lines | **parallel** rounds (`ThreadPoolExecutor`, `SEARCH_ROUND_LIMIT` workers) — the only concurrent part of the search module |
| `format_search_context` / `_failure_context` | ~55 lines | the two functions originally scoped as "next" |
| **`context_taint.harden_search_context`** | **383-line module, 18 public items** | a **taint firewall**: `sanitize_external_text`, `UNTRUSTED_CONTENT_GUARD`, `taint_enabled()`, feature flags |
| `searchContext` → `build_deepseek_request` | — | **the consumer does not exist in Rust**; the gateway passes the prepared body through |

**Why this is a separate vertical slice, not an extension.** `searchContext` is consumed by
`build_deepseek_request`, which the Rust gateway does not own — the route prepares the raw
body and forwards it. So the pipeline's output has nowhere to go until the request-assembly
layer exists, and that layer is where the earlier recorded layering lesson lives
(`build_deepseek_request` composes the system turn from `payload["systemPrompt"]`).

**Recommendation.** Treat this as its own slice with the taint firewall as its centre, not
as a tail of the tool-round work. The ordering that keeps every step verifiable:
1. the pure predicates and the two formatters (byte-parity, offline) — inert until 3, so
   they commit safely;
2. `search_multiple`'s parallel shape, which is the part with real concurrency semantics;
3. `harden_search_context` and `sanitize_external_text` against the reference's own tables
   — this is a security boundary, so it needs the same treatment the IP-block sets needed:
   read the reference's data, do not rebuild the predicate from intuition;
4. the `searchContext` injection once `build_deepseek_request` exists to consume it.

Nothing here was started.

**Search-prefetch slice 1: the pure predicates and the two formatters (2026-09-17).**

Step 1 of the order recorded above, landed in `deepseek-policy::search`:
`search_mode`, `forced_search_mode`, `search_tool_enabled`,
`format_search_context`, `format_search_failure_context`. Byte-verified offline;
inert until the assembly layer exists, so nothing calls them yet.

**This slice resumed an interrupted working tree, and the interruption was not
clean.** The three modified files had never run: the Rust example failed to
compile (five `cannot find function` errors) and the Python probe crashed with
`AttributeError: …search has no attribute 'search_mode'`. The recovery found two
defects before anything was green:

1. **The predicates live in `gateway/deepseek_client.py`, not `search.py`.** The
   probe now extracts them verbatim from that file via `ast` (the SSE-probe
   pattern), so the definitions being compared are the oracle's own. The Rust
   port stays in this crate's `search` module because its consumers are the tool
   catalog and `tools_for_payload`; the placement is recorded in the module docs.
2. **`python_str` matched `str(x or "")` on the rendered text, not the raw
   truthiness.** A numeric `0` came through as `"0"` — an off-mode spelling —
   so `{"searchMode": 0}` made `search_mode` return `"0"` where the oracle
   returns `"auto"` (its `or` fallback is `"auto"`, which neither forces nor
   disables), **flipping `search_tool_enabled`**, and made
   `should_search_for_query` refuse where the oracle falls through to text
   matching. The same root cause would render a `Tavily 摘要` line for
   `{"answer": 0}`. Fixed through `python_truthy` + `python_json::value_str`;
   the corpus now pins every one of those shapes (mode `0`/`true`, answer `0`,
   title/citation/raw_content `0`, error `true`/`0`). This was a defect in
   **committed** code (`should_search_for_query` shares `python_str`), not just
   the interrupted WIP.

Two divergences measured and recorded rather than compared:

- A **non-dict result entry** (or a non-array `results`) makes the oracle raise
  `AttributeError`/`TypeError` and fail the request; the port renders through
  the fallbacks. Unreachable from the wired pipeline —
  `normalize_search_response` / `aggregate_search_rounds` guarantee dict entries
  in a list — and reachable only from a hand-corrupted cache file, where the
  oracle's own behaviour is an uncontrolled 500. Pinned by a unit test so the
  tolerance is a recorded decision, and the corpus case that would have crashed
  the Python probe was dropped. The *failure* formatter keeps its non-dict round
  entry, because there the oracle guards with `isinstance` and both sides agree.
- The query line renders containers through `value_str`'s JSON quoting — the
  crate's standing `repr` approximation, unreachable from the wired pipeline
  where `query` is always a string.

Verified locally:

- Byte-level parity: **identical MD5 `0f3877807ce994f0d3dbe852293c47ee`**, 157
  keys (up from 113), no differences, re-confirmed after `cargo fmt`.
- `cargo test -p deepseek-policy -j 1 -- --test-threads=1` → **237 tests, all
  pass** (6 new).
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings`
  → clean; one real find fixed (`useless_vec` on the example's corpus, which had
  never been clippy'd). The only other output is the transient `deepseek-core`
  incremental-copy warning, which is not a lint.
- `cargo check -p deepseek-gateway --all-targets` → still compiles.
- `cargo fmt --all -- --check` → clean.

Next per the recorded order: slice 2, `search_multiple`'s parallel shape (a
`ThreadPoolExecutor` over `SEARCH_ROUND_LIMIT` rounds — the module's only
concurrency), then the taint firewall against the reference's own tables, then
the `searchContext` consumer.


### Slice 2 landed; the lib-test harness regressed with it (2026-09-17, measured)

`search_multiple` is ported and committed (`e2354851`). It is the module's only
concurrency: cache gate, query planning, per-round "searching" announcements,
`as_completed`-style collection, the two error arms, the cache write, the
progress callback. Parity is byte-identical — 2461 lines, md5
`84b6f0f6b7c90b2d2a07f08d138659ae` on both sides; the probe grew the whole
`multi::` family (first run, announcements, cache hit, expiry, reversed
completion order, API error, worker exception, empty query list). `cargo fmt
--check` and `cargo clippy --all-targets` are clean.

**The harness no longer starts, and it did at slice 1.** The previous entry in
this file records `cargo test -p deepseek-policy -j 1 -- --test-threads=1` →
237 tests all pass, and that text came in with slice 1 (`6e6519cf`, 11:06). At
12:46 the same command dies before running anything:

    error: test failed, to rerun pass `-p deepseek-policy --lib`
      process didn't exit successfully: ... (exit code: 0xc0000139,
      STATUS_ENTRYPOINT_NOT_FOUND)

What was measured, not assumed:

- Of the 193 imported symbols in that binary, exactly one is unsatisfiable: it
  binds `WakeByAddressSingle` to `KERNEL32.dll`.
- This Windows build's `kernel32` exports **none** of `WakeByAddressSingle`,
  `WakeByAddressAll`, `WaitOnAddress`. Verified at the loader's own API with
  `GetProcAddress` (a five-line C probe): all three MISSING in kernel32, all
  three PRESENT in kernelbase. Two other checks agree (`grep` for the name in
  the DLL is 0 for kernel32, 1 for kernelbase; `objdump -p` the same).
- The other seven test binaries in `target/debug/deps` — deepseek-core's and
  deepseek-gateway's among them — bind those three to
  `api-ms-win-core-synch-l1-2-0.dll` (which the loader maps to kernelbase) and
  start normally. So does this crate's own `search_parity_probe` **example**
  after a clean rebuild.
- `cargo clean -p deepseek-policy` followed by a relink reproduces the bad
  binding, so it is not a stale artifact.

**The causal picture, stated honestly.** The example links the same lib code —
including `search_multiple` and its threads — and binds the api-set, so the
ported logic is not what breaks the import. What the lib-test target adds over
the example is the `#[cfg(test)]` code plus `libtest`, and it is one of those
that flips which of the two competing `__imp_WakeByAddressSingle` stubs the
linker takes (raw-dylib stubs carry their own DLL name, and `ld` keeps the
first). Slice 1's harness booted, slice 2's does not, and slice 2's only new
code is `search_multiple` plus its tests — so the correlation points at the new
test code, but **causality was not isolated**. Next diagnostic round: relink and
run with slice 2's unit tests removed, which separates "the new tests pull it"
from "the crate now links something else".

This is written down rather than fixed because it is not the ported logic, and
because guessing at the linker would be exactly the kind of change this project
does not want: the verification for slice 2 is the probe, which does pass.


### Isolation done: the harness failure is a link-shape fragility, not a test bug

The previous entry left the next round as "relink with slice 2's unit tests removed".
That was done, by disabling tests one at a time with `#[cfg(any())]` in front of the
`#[test]` attribute (the source was byte-restored afterwards; `git diff` is empty).
Each round relinked and ran the lib harness:

| new tests enabled | harness |
| --- | --- |
| none | **boots: 237 passed, 0 failed** |
| `an_empty_query_aggregates_without_searching` | dies, 0xc0000139 |
| `a_cache_hit_returns_without_searching` | dies, 0xc0000139 |
| those two together | dies, 0xc0000139 |
| all five | dies, 0xc0000139 |

With all five disabled the count is exactly 237 — slice 1's number — and
`search_multiple` is still in the lib, so the *library* code is not what breaks the
import. Enabling **any one** of the five is enough, and the two single tests are
trivial: no worker thread is spawned, no panic occurs, the transport is a closure that
is never called. So it is not the content of a test. What flips the import is the
lib-test target acquiring a reference to `search_multiple` at all — i.e. **the test
binary growing changes which competing import stub the linker keeps**. The same
reference in the **example** target binds the api-set and runs, so the outcome is
target-dependent, not source-dependent.

Where the stubs come from, checked: rustc's self-contained
`lib/rustlib/x86_64-pc-windows-gnu/lib/self-contained/libsynchronization.a` does
provide `WakeByAddressSingle`, and inside it the DLL name is
`api-ms-win-core-synch-l1-2-0.dll` — the correct one. `/d/mingw64`'s copy of the same
archive agrees. `libkernel32.a` (both the toolchain's and mingw64's) does **not** define
the symbol at all. So the KERNEL32 binding that appears in the failing binary is not
coming from those archives; it comes from a crate-level raw-dylib stub, i.e. some object
compiled against `kernel32.dll`, and which stub wins is decided by link order. Pinpointing
that object needs the std sources, and `rust-src` is not installed on this machine —
that is where this stopped, deliberately, rather than guessing further.

**Consequences.** (1) There is no "guilty test" to rewrite; the fragility will resurface
whenever this target's object set changes. (2) Slice 2's verification stays the probe,
which is byte-identical. (3) If the harness is wanted back, the options are a link-level
workaround (`RUSTFLAGS` with an explicit stub order or `-C link-self-contained`), a
toolchain pin, or installing `rust-src` to name the offending object first — none of
which belongs in a migration commit.

Corrects the earlier framing in this file: the regression did arrive with slice 2, but
it is not caused by slice 2's code or tests; slice 2 is what made the test binary big
enough to expose it.


### Slice 3 landed: the taint firewall's string layer (`591d0c38`)

`context_taint.rs` now carries the part of `gateway/context_taint.py` that has no I/O and no
consumer-dependent shape: the guard constant and the trust/source/marker vocabulary, both
pattern tables, the sensitive-tool alternation, `scan_text`, and the active hardening
(`harden_search_context`, `file_context_guard_line`, `escalation_enabled`) with
`ContextTaintSettings` at the oracle's defaults.

**Both tables are read off the reference, not rebuilt** — the lesson from the IP-block sets:

- the sensitive-tool list is *derived* from `TOOL_METADATA` with the oracle's own predicate
  (`requires_confirm || sensitive_sink || risk == "high"`), so a tool profile change cannot
  desynchronise the two. It comes out as eight names, in table order, and the alternation is
  order-bearing because alternative branches are tried left to right.
- the injection count is `tool_policy::sanitize_external_text`, the Tool Policy Engine's own
  sanitizer, because the oracle shares that table between the two modules.
- the exfiltration verb list keeps the oracle's deliberate exclusion of `提交`: it trips on
  benign advisory prose like `不要提交到仓库`, while genuine exfiltration in this corpus uses
  `发送` / `上传` / `发到`. The corpus pins that case, plus a 70-character gap (one past the
  pattern's `{0,60}` lifetime), a newline inside the gap, and `web_search` not matching the
  sensitive alternation — all four must *not* fire.

Verification is `tasks/native-runtime/context_taint_parity_probe.py` against
`examples/context_taint_parity_probe.rs`: 35 texts plus six flag combinations, 87 keys,
**byte-identical**, md5 `adff8e2723c890fe8c969d21e8d1fa0c`. The Python side imports the
oracle module directly rather than re-executing extracted source, because `context_taint`
only pulls `core.config` and `tool_policy` and both import cleanly — so the tables under
test are literally the oracle's own objects. `cargo fmt --check` and
`cargo clippy --all-targets` are clean (exit 0).

**Deliberately left out, as the honest boundary**: `_risk_level`, `classify_request_messages`,
`build_taint_report`, `report_is_tainted`, `taint_status`. That is the diagnostics half, and
its consumer — the gateway's diagnostics assembly and the `/api/taint` route — does not exist
in Rust. It is inert in a way this layer is not: `harden_search_context` is exactly what
slice 4's `searchContext` injection calls.

No unit tests were added with the module: the lib-test harness still cannot start on this host
(the link-shape finding above), so such tests could not be run, and the probe is the
verification that actually executes. That is a real gap to close once the harness boots.

Remaining: slice 4 — the `searchContext` injection into `build_deepseek_request`, which has to
exist first — and, separately, `search_if_needed`, which is what eventually calls
`search_multiple`.


### Slice 4 landed: the per-turn context, and the reader of `searchContext` (`4056e3c9`)

`dynamic_context.rs` carries `build_dynamic_turn_context` — the function that reads
`payload["searchContext"]` — plus everything it splices in: `format_current_time_context`,
`format_context_summary_context`, `format_memory_notice`, `format_slides_skill_context`,
`presentation_intent_requested` (over the already-ported `latest_user_query`),
`append_context_to_latest_user`, and the constants (`CURRENT_TIME_CONTEXT_HEADER`,
`CONTEXT_SUMMARY_MAX_CHARS = 12 000`, `WEB_SEARCH_SYSTEM_HINT`, the three slides
name/reference/guidance strings).

This closes the loop the earlier scoping note described: `harden_search_context` had a string
with nowhere to go, and this is the thing that puts it in the prompt. The ordering is the
whole design — the search context goes **after** the stable prefixes, so switching search on
and off does not invalidate the prompt cache behind it.

**The one real design decision: the clock is injected, not read.** The oracle calls
`datetime.now().astimezone()` and renders the machine's local zone. Rust's standard library
has no local-timezone support, and this workspace has **no time crate at all** — only
`std::time` epoch arithmetic. So `LocalNow` carries the instant, the offset and the zone name,
following the two precedents already in this tree: `utc_now_iso(epoch_seconds)`, whose doc
says "the clock is a parameter so callers can pin it", and the injected search transport.
**Resolving the OS zone is not implemented**, deliberately and visibly: faking it would be
worse. The oracle's naive-datetime arm (`tzinfo is None` → assume UTC, then convert to the
*machine's* local zone) has no counterpart for the same reason, and is excluded from the
corpus because its output is host-dependent.

Three details that would each be a silent divergence if "cleaned up":

- **Two spellings of the same instant coexist.** `format_current_time_context` renders UTC as
  `…Z`; `core_utils::utc_now_iso` renders `…+00:00`. The oracle replaces the suffix in exactly
  one of the two places, so `isoformat_seconds` (new in `core_utils`, sharing
  `civil_from_days` with `utc_now_iso`) appends the offset and leaves the choice to its caller.
- **The slides text is transcribed with `concat!` and explicit `\n`,** not as a multi-line raw
  string: a raw string takes its line endings from the source file, so a CRLF checkout would
  silently change every prompt byte those constants feed. Git confirmed the risk is live —
  committing these files printed `LF will be replaced by CRLF the next time Git touches it`.
- **A landmine is recorded for the assembly slice.** `append_context_to_latest_user` appends
  `{"role": …, "content": …}` and the oracle's body serializes in insertion order, but
  `serde_json::Map` here is a `BTreeMap`, so `json!` emits `content` first. Whoever writes the
  body builder must not let `json!` decide the order of the message it injects.

Verification: eight pinned instants (UTC, +08:00, −05:00, +05:30, −09:30, epoch 0, and two
day-rollover cases), the full assembly over fourteen payload/memory/tools combinations, both
12 001-character truncation paths, and the append cases — **byte-identical**, 39 keys, md5
`e3a065e999df38e421de8a17f74cfef6`. The Python probe imports the oracle modules directly and
stubs `format_current_time_context` for the assembly cases, because the oracle's builder reads
the machine clock; the mirror of that stub is the injected clock on this side. `cargo fmt
--check` and `cargo clippy --all-targets` are clean, and **both** this probe and slice 3's were
re-run after formatting so the committed bytes are the verified bytes (`e3a065e9…`,
`adff8e27…`).

**The honest remaining boundary.** The reader exists, but `build_deepseek_request` — the body
assembly that would actually consume `build_dynamic_turn_context` — still does not exist in
Rust: the gateway prepares the raw body and forwards it. So nothing injects into a request yet,
and this slice is inert in the same recorded sense as slices 1–3. Also outstanding: the OS
timezone resolution, `search_if_needed`, and the taint diagnostics half. No unit tests came
with this module, for the same reason as the last one.


### `build_deepseek_request` was next, and measuring says it is not a slice (`9b3a7825`)

The obvious next move after slice 4 was the assembly function that would finally consume
everything: `build_deepseek_request`. Measuring it first, the way the earlier scoping pass
should have, says **do not start it as one slice** — and the shapes below are what that
judgement rests on.

The function itself is only ~123 lines (`deepseek_client.py:240`–362), but it is a
convergence point, not a unit. Its dependency closure, measured:

| collaborator | size | ported? |
| --- | --- | --- |
| `model_router.py` (`route_request`, `is_auto_request`) | 279 lines | no |
| `budget_manager.py` (`budget_policy_from_payload`, `should_downgrade`, `budget_scope`) | 371 lines | no — and it owns a **ledger**, so it is not pure |
| `context_manager.py` (`manage_request_body`, `merge_context_manager_diagnostics`) | 137 lines | no |
| `validate_deepseek_payload` + `_validate_request_messages` + `normalize_chat_messages` | ~110 lines | partly — the gateway has its own `prepare_request`/`normalize_*`, but not these |
| `chat_payload.count_payload_attachments` | 32 lines | no |
| `empty_memory_state`, `_has_image_content`, `tools_for_payload`, `forced_artifact_tool_name`, `normalize_reasoning_effort`, `TOOL_PARALLEL_SYSTEM_HINT` | ~75 lines | no |
| `context_taint.build_taint_report` | 39 lines | no — until this commit |

So the closure is ~1,200 lines across six subsystems, one of which is stateful. That is a
milestone. The useful thing to do with a milestone is find its slices, and the first one was
already sitting there: **line 357 needs `build_taint_report`**, which is exactly what slice 3
deferred on the note that its consumer did not exist. Measuring the consumer turned it up, so
this commit ports the diagnostics half and **`context_taint.py` is now complete**.

What the classification half turned on, all pinned by the corpus:

- **`len()` counts characters, and `_segments_for_user` uses a found index as a length.** A CJK
  prefix before the file marker inflates the trusted-prefix segment if the index is treated as
  bytes; the corpus pins the case that would catch it (中文提问… → `chars: 4`).
- **The arm order in `tool_message_source` is the contract**: `browser_` and `mcp__` before the
  metadata table, and `search_files` reaches the RAG arm only when the payload says `local_rag`.
- **`segments_for_per_turn_system` inserts at 0 and 1** — that is what puts the media segment
  first and the trusted prefix before the web segment.
- The serialization landmine recorded for slice 4 applies to this block too: `build_taint_report`
  builds its object in insertion order and the caller splices it into `diagnostics`; `json!` here
  yields sorted keys, so the diagnostics serializer must own that order.

Verification: 176 keys, byte-identical, md5 `49e390b4c0c9f2ab499337326b308404` — 29 message
lists, 4 settings tuples × 6 bodies, the `taint_status` block and 8 risk combinations, on top of
slice 3's 87 keys (the earlier cases are still in the same probe, now 176). The first comparison
**failed**, and the cause was the probe corpus rather than the port: the Python body list indexed
one case off from the Rust one, and only the Python side needed changing to make the hashes agree.
That is the method working — the diff localised the fault before it became a story about the port.

**Sequence for the milestone, in the order that keeps each step verifiable.** None of these is
started:
1. the remaining pure collaborators (`empty_memory_state`, `_has_image_content`,
   `tools_for_payload`, `forced_artifact_tool_name`, `normalize_reasoning_effort`,
   `count_payload_attachments`, `TOOL_PARALLEL_SYSTEM_HINT`) — small, and each has an oracle
   function to compare against;
2. `context_manager` (137 lines) — pure but with the sliding-window semantics that make the
   body's bytes; needs its own probe over windowed bodies;
3. `model_router` (279 lines) — pure tier selection, but it needs a model catalog to compare
   against, so measure that dependency before assuming;
4. `budget_manager` (371 lines) — **last, and only with a store port**: it reads a ledger, so it
   is the one piece here that is not a pure function, and the ledger's own port would have to
   come first;
5. the assembly itself (`build_deepseek_request`), once its collaborators exist, with the
   diagnostics serializer that owns key order.

Until step 5 lands, every slice so far remains inert in exactly the recorded sense: verified
and unwired.


### The harness works again, and the earlier mechanism note was wrong (2026-09-17)

`cargo test -p deepseek-policy --lib` runs again: **242 passed; 0 failed** — 237 from before
plus the five slice-2 tests that had never been able to execute. The fix is one flag:

```
RUSTFLAGS="-C link-self-contained=yes" cargo test -p deepseek-policy
```

**Correction first.** The earlier entry here explained the failure as "two competing raw-dylib
stubs and `ld` keeps whichever it sees first". That was wrong. Asking the linker directly settles
it:

```
RUSTFLAGS='-C link-arg=-Wl,--trace-symbol=__imp_WakeByAddressSingle' \
  cargo test -p deepseek-policy --lib --no-run

warning: linker stderr: D:/mingw64/bin/../lib/gcc/x86_64-w64-mingw32/8.1.0/../../../../x86_64-w64-mingw32/lib/../lib/libkernel32.a(dqifs01464.o): definition of __imp_WakeByAddressSingle
```

The provider is **`/d/mingw64`'s `libkernel32.a`** — the *system* MinGW's import library, GCC 8.1.0,
from 2018, on `PATH` as `gcc`. rustc's `windows-gnu` target uses `gcc` as its linker driver, and
that driver injects its own library search path, so an import library built for a Windows era when
`kernel32` did export the futex APIs is searched — and its ordinal-era stub `dqifs01464.o` wins
`__imp_WakeByAddressSingle` from libstd's own stub. On this Windows build `kernel32` exports none of
`WakeByAddressSingle` / `WakeByAddressAll` / `WaitOnAddress` (verified with `GetProcAddress`: all
three live only in `kernelbase`), so the import is unsatisfiable and the loader stops with
`0xc0000139`.

Three things this re-explains, and one it does not:

- **Why the API-set stub never appeared to be the provider.** It *is* rustc's provider: every
  `WakeByAddress*` stub inside `libstd` (members `api-ms-win-core-synch-l1-2-0.dlls0000{0,1,2}.o`)
  has a four-byte, **all-zero** `.idata$7` — the DLL-name field. rustc does not name the DLL in the
  stub; the descriptor is chosen at link time. So a *different* archive can satisfy the symbol
  first, which is exactly what the old system import library does.
- **Why `cargo clean -p` and relinking never helped.** The offending archive is outside the target
  directory.
- **Why only the lib-test target died.** The symbol is undefined in several objects; which archive
  wins depends on the order the linker walks them, which differs per target. The examples and the
  other crates' test binaries happened to resolve it from libstd.
- **What is still unexplained, and is now moot:** why this target in particular. With the flag the
  ambiguity is gone, so there is nothing left to chase.

`-C link-self-contained=yes` makes rustc use its own bundled `rust-mingw` libraries (which ship
the correct-era import libs) instead of the system MinGW's, so the symbol resolves from libstd's
stub and the import points at the API set that the loader maps to `kernelbase`.

**It is committed, scoped as narrowly as the cause allows.** `rust/.cargo/config.toml` now carries

```toml
[target.x86_64-pc-windows-gnu]
rustflags = ["-C", "link-self-contained=yes"]
```

Scoped to the triple rather than `[build] rustflags` because the failing combination is specifically
`windows-gnu` plus a system MinGW on `PATH` — nothing about MSVC builds or other targets should
inherit the workaround. The file also carries the reasoning inline, since a config that changes link
inputs deserves its own explanation next to it. It requires the `rust-mingw` component (present here,
and installed by default for windows-gnu host toolchains).

Verified after adopting it, with no `RUSTFLAGS` in the environment:

- `cargo test -p deepseek-policy -j 1` → **242 passed, 0 failed**, in 0.77 s with the *same* artifact
  hash as the env-var run — so the config produces the same fingerprint as `RUSTFLAGS` did, and
  adopting it costs no rebuild;
- `cargo test -p deepseek-core -j 1` → 8 passed after a rebuild under the new flags, so the flag does
  not regress a crate that was already linking fine.

One caveat that survives, and is written into the config file: **`RUSTFLAGS` in the environment takes
precedence over the config**, so a stray value there silently overrides these flags and brings the
failure back along with a full rebuild. Picking one mechanism and staying with it still matters.

The consequence for the record: the "no unit tests came with the module, because the harness cannot
start" note that appears against slices 3, 4 and 5 is now **expired** — the harness starts, and
those modules can carry tests.


### The test debt is paid (`3b55437d`)

The note above said the harness starting again meant slices 3, 4 and 5 could carry tests. They do
now: 40 added, 242 → **282 passing**. `context_taint` gets the bulk, since it is the security
boundary and every "obvious" simplification in it is a divergence — the guard wrapping rather than
replacing, the `提交` exclusion, the gap neither crossing a newline nor sixty characters, the
character-counted CJK prefix, the arm order in `tool_message_source`, the per-turn split, the
media tail's position, and the report's cap-versus-totals behaviour. `dynamic_context` pins the
`Z`/`+00:00` pair that must not be unified, the cache argument (search off is byte-identical up to
the hint), the joins, and the falsy drops.

One expectation was wrong on the first run: the truncation test asserted five segments where both
implementations produce six. **The port was right and the arithmetic was mine** — and the corrected
assertion now carries the oracle's own sequence rather than a recomputed number. Same lesson as the
taint probe's mis-indexed corpus two slices ago: check the expectation before believing a failure.

These tests are the fast local net; the parity probes stay the cross-language evidence, since they
compare against the oracle rather than against expectations written by hand.


### Milestone step 1: the pure collaborators are ported (`11a08fff`)

The sequence recorded above said to take the assembly's pure leaves first. They are in, in a new
`request_shaping` module plus two functions in `memory`:

| what | where it came from |
| --- | --- |
| `TOOL_PARALLEL_SYSTEM_HINT` | `deepseek_client.py` |
| `normalize_reasoning_effort`, `tools_for_payload`, `forced_artifact_tool_name`, `should_force_create_pptx`, `has_create_pptx_tool`, `mindmap_intent_requested`, `has_image_content` | `deepseek_client.py` |
| `count_payload_attachments` | `chat_payload.py` |
| `empty_memory_state`, `memory_scope_from_payload` | `data/memory.py` |

That shrinks the closure between here and a working `build_deepseek_request` to four things:
`context_manager` (137 lines), `model_router` (279), `budget_manager` (371, ledger-backed and
therefore last), and the assembly itself with the serializer that owns the diagnostics key order.

What the corpus and the 13 new tests pin, each of which reads like a tidy-up waiting to happen:

- **`tools_for_payload` composes two filters and their order shows.** The allow-list is applied
  first, then the search tools are dropped — so naming `web_search` in `allowedTools` still loses
  it when search is off. A non-list `allowedTools` is ignored rather than treated as empty.
- **`forced_artifact_tool_name` needs availability *and* permission**, and with no allow-list the
  permitted set *is* the available one.
- **`normalize_reasoning_effort` is case-sensitive** — `MEDIUM` falls back like any unknown — and
  `"  high  "` is stripped before the membership test.
- **`memory_enabled` is `is not False`**, so `0` and `""` read as *enabled* while only the boolean
  `false` disables it; a malformed scope id is silently narrowed to `global`.
- **`memory_scope_from_payload` reads the latest user message only** and stops there either way, so
  an older `projectId` never leaks forward.
- **`mindmap_intent_requested` fires on `什么是 mindmap？`** with no create verb, because the
  oracle's verb alternation contains `map` and `mindmap` contains it. Left as-is, with the reason in
  the code: this is the oracle's behaviour, and tightening it would be a divergence, not a fix.

Verification: the probe pair replays six corpora and matches byte for byte — 86 keys, md5
`4bc6167d94f761c2a4178c70e2cdac6e`. `tools_for_payload` is compared as the **sequence of function
names**, not as whole definitions: the definitions are the tool catalog's own subject and are
covered there, and re-comparing them here would bury this probe's actual subject. Tests are at 295,
`cargo fmt --check` and `cargo clippy --all-targets` are clean, and the Rust probe was re-run after
formatting so the committed bytes reproduce the hash.


### Step 2 measured: `context_manager` is not a slice either (`0882b1b0`)

`context_manager` was the next recorded step. Measuring it first: its 137 lines depend on
`context_engine` (347 lines, entirely unported), whose identity half needs **SHA-1** — and this crate
depends on `sha2`, not `sha1`. So the work was split at the seam the dependency graph already has:

- **done here**: the token half of the engine — the heuristics, the three estimators, the body
  breakdown, the per-model window lookup, `available_input_tokens`, the budget plan with its
  recommendation ladder, and `token_trim`;
- **blocked on a decision**: `base_context_id` / `build_context_diff` / `build_engine_diagnostics`,
  which need the SHA-1 either hand-rolled (a hash implementation in-tree) or via a new dependency;
- **then**: `context_manager` itself, which is mostly ordering and diagnostics once the engine exists.

What the 168-key corpus and 12 new tests pin, beyond the arithmetic:

- an empty **object** message still pays the four-token structural overhead; only a non-object
  message costs nothing (measured: the oracle returns 4 for `{}` — my first test said 0 and was
  wrong, not the port);
- the trailing system message is `dynamic` only when it is last *and* there is more than one message;
- the CJK ranges include Fullwidth forms, so CJK-keyboard punctuation is not miscounted as Latin;
- `round(x, 1)` is ties-to-even on both sides, which `format!("{:.1}")` mirrors;
- `estimate_tools_tokens` measures a serialized tool array whose **key order differs** between
  `serde_json` and Python — and the estimate is deliberately insensitive to that, since reordering
  keys changes neither length nor CJK count. The string is not exposed, so nothing can start
  comparing it byte-for-byte;
- `token_trim` never touches the leading or trailing system message, keeps at least
  `min_keep_messages` of the middle, and returns the caller's list untouched when the budget is zero.

Verification: byte-identical, md5 `9c50e3cc29c8493f3c057fc3a3b79a07`; tests 295 → **307**; `fmt
--check` and `clippy --all-targets` clean, with the probe re-run after formatting. Two more of my
expectations were wrong on the first run and were corrected against the oracle rather than by
changing the port — the same failure mode as the taint corpus index and the five-versus-six segment
count, which is now three for three: **my arithmetic about the oracle is the weak link, so
expectations get taken from the oracle.**


### The context engine is whole (`3960be2c`), and the SHA-1 decision went to a dependency

The identity half was blocked on a decision worth recording: `base_context_id` needs SHA-1, this
crate depends on `sha2`, and the two ways out were hand-rolling the primitive or adding the crate.
Measurements that decided it:

- **`sha1` was not in the lockfile at all**, not even transitively — so the addition is a real one,
  not a free promotion of something already present;
- **every existing fingerprint in the crate delegates to a RustCrypto digest**
  (`memory.rs:195`, `search.rs:735`, `tool_policy.rs:1330` all call `Sha256::digest`), so writing a
  primitive by hand would have introduced a practice this codebase does not have.

So `sha1 = "0.10"` sits next to `sha2 = "0.10"` in the workspace table. What the corpus and four new
tests pin:

- **tool order is part of the prefix identity** — the parts string is the leading system content, the
  model, then the tool names *in order*, so a swap changes the id. That is the value's whole purpose:
  revealing accidental prefix churn.
- **an unnamed tool contributes nothing**, so a tool with an empty name, a non-dict `function`, or a
  bare string leaves the parts string untouched and the id equal to an empty body's.
- the dynamic block's `chars` counts characters, not bytes.
- the two ids asserted in the unit tests are **taken from the oracle**, which doubles them as a
  known-answer test of the digest path.

Verification: 204 keys byte-identical, md5 `0b430da00b2467c6a99e632880b4f38d`; tests 307 → **311**;
`fmt --check` and `clippy --all-targets` clean, probe re-run after formatting.

`context_engine` is now complete, which leaves `context_manager` as the only piece of this subsystem
— and it is mostly ordering plus diagnostics assembly, since both halves it depends on exist.


### The context subsystem is complete (`7c377889`)

`context_manager` was the last piece, and it went in small because both halves it depends on
already existed. What is worth recording is less the port than two mistakes in my own verification:

**The first corpus could not have caught a broken token-trim.** The manage bodies carried a model
that is *in* the window table, so the table's 131 072 beat the small patched default window and the
token-aware pass **never ran** — meaning the probe would have reported "parity holds" whether that
path worked or was a no-op. Switching the corpus to a model outside the table made the path
reachable, and the reference now shows the discrimination: 4 messages dropped with trim on, 0 with
it off. The same mistake was in the unit test, where the fix was to empty the table explicitly. This
is the strongest form of the recurring lesson — not "my expected value was wrong" but **"my corpus
could not tell the difference"**, which is worse because it reads as a pass.

**And the lint I introduced.** The settings tuple in the new probe tripped `type_complexity`, which
is a warning rather than a deny, so `clippy` still exited 0 — the diagnostic was there and my filter
was hiding it. It is fixed with a `SettingsCase` alias. Worth remembering: "clippy exit 0" and "no
diagnostics" are not the same claim, and it is the second one that was being asserted in these
messages.

Traps the corpus and seven tests pin: the sort is by `(name, type)` and **stable**, `toolOrder`
lists only named tools while `toolCount` counts every entry, both system ends are pinned and the
count window's budget floors at one, the engine block appears only while the engine is on, and
`merge_context_manager_diagnostics` **moves** the engine block out and copies a **zero**
`requestMessageCount` (a truthiness test would drop it). One measured divergence is kept and
documented: the oracle's `tool_name` raises on a non-dict tool where this port returns an empty name.

Verification: 341 keys byte-identical, md5 `ba5343c49b1b6aeef25fec1724b0d911`; tests 311 → **318**;
`fmt --check` clean and `clippy --all-targets` exit 0 with no diagnostics in the new files.

What is left before `build_deepseek_request` can exist: `model_router` (279 lines, pure, needs its
model catalogue measured first), `budget_manager` (371, ledger-backed and therefore last), the
validation/normalisation set (~110), and then the assembly itself with the diagnostics serializer
that owns key order.


### The model router landed, and the new check earned its keep (`b04772bc`)

Same shape as the last two: `model_router.py` is 279 lines and depends on `edge_inference`'s 529, of
which it uses **four names**. So the slice is the router plus that surface — the three query-shape
patterns and the two payload readers — with the edge-routing half (providers, quantisation,
local-versus-cloud) left for its own slice. The patterns are exported as **text** as well as
compiled, and the probe compares the strings: they carry CJK literals, and a wrong character
transcribed blind would otherwise surface only as a mysterious routing difference.

What the 251-key corpus and eleven tests pin, in the order they would bite:

- complexity tests run in the oracle's order — the complex pattern beats a short length, and the
  simple pattern only counts within 400 characters, so `解释` + 500 characters is `neutral`;
- `is_auto_request` mixes a case-fold with an identity check: `model: " AUTO "` opts in while
  `autoRoute: 1` does not;
- capability reads the **attachment** (`imageData: data:image/…`), not content parts — the test
  asserts both directions against `request_shaping::has_image_content` so the pair cannot collapse;
- an explicit model is normalised, checked against the supported list, then overridden by vision
  unless it is already the refine model;
- auto routing walks complexity → the soft cost cap (off at zero) → the default, and the tier falls
  back to the model name for anything that is neither draft nor refine;
- cascade is refused for agent and vision turns; the quality gate scores `1 - 0.34` per reason with
  one uncertainty marker passing and two failing.

**The "no diagnostics" standard caught a real lint this time.** Appending the test modules left the
`text_or_empty` helper after them — `items after a test module`, a warning that does not change
clippy's exit code. Under the old "exit 0" claim it would have shipped. Both files were reordered.
Same class of miss as `type_complexity` last round, and the reason the assertion was changed.

Verification: 251 keys byte-identical, md5 `ebad857e793f240bba7fa0d1c2f5a894`; tests 318 → **329**;
`fmt --check` clean; `clippy --all-targets` exit 0 with no diagnostics.

What is left before `build_deepseek_request`: `budget_manager` (371, ledger-backed and therefore
last), the validation/normalisation set (~110), the `edge_inference` edge-routing half (~430), and
then the assembly with the diagnostics serializer that owns key order.


### The message layer landed, and the corpus nearly failed to be able to fail (`01d252b1`)

`normalize_chat_messages` plus its two validators and the tool-call helpers. The layer is
**fail-closed** by design and the oracle's docstring records why: an earlier revision silently
skipped every turn it could not represent, so the caller's instruction reached the model as if it
had never been written — a `200` whose answer ignored what the user had said. Every unrepresentable
turn raises here, with its index and the codes the gateway's own preparation layer returns.

**The content expander is injected.** `expanded_message_content` reaches the file index through
`build_attachment_context`, which is I/O, so the pure layer takes the expander as a parameter — the
same move as the clock and the transport. Attachment *parts* are still covered, because
`_image_content_parts` is pure and is ported.

Three notes on verification, in order of how much they cost:

1. **The first corpus for the check layer could not have failed.** It reused the message sets, none
   of which contains more than 40 messages, so the `context_compression_required` path was
   unreachable and the probe would have reported parity whether that rule worked or not. It has its
   own corpus now, and the reference shows the discrimination: 41 messages without a summary is a
   **409**, with a summary it is fine, and exactly 40 is fine either way. This is the second
   instance of "a corpus that cannot fail reads as a pass" after the token-trim one.
2. **One measured divergence.** A JSON *object* as tool-call `arguments` is re-serialized in sorted
   key order here where the oracle keeps insertion order — `serde_json::Map` is a `BTreeMap` and
   `preserve_order` is off workspace-wide on purpose, and the order is gone at parse time. The wire
   format sends arguments as a string, so it is unreachable from the wired path; documented, pinned
   by a test, and the corpus uses an already-sorted object so the probe compares behaviour rather
   than that gap.
3. **A test case of mine was wrong, not the port.** The api-key-fallback case omitted `messages`,
   which the oracle rejects too — the probe showed both sides agreeing before the test changed.

The "no diagnostics" assertion earned its keep for the second slice running: the helper landed after
the test module again, a warning that does not move clippy's exit code.

Verification: 121 keys byte-identical, md5 `7c7860c9a70b4b4f07f0cff7503cdda7`; tests 329 → **337**;
`fmt --check` clean; `clippy --all-targets` exit 0 with no diagnostics.

What is left: `budget_manager` (371, ledger-backed and therefore last), the attachment-expansion path
behind the file index, and then `build_deepseek_request` itself with the diagnostics serializer that
owns key order. The `edge_inference` edge-routing half is **not** in this closure — only its four
consumed names were ever needed.


### The budget manager's pure half, and a float rendering that was wrong at real magnitudes (`04645d0c`)

`budget_manager` was the last leaf before the assembly, and its ledger is SQLite -- so the slice
line is the one the oracle's own docstring draws: pricing, cost arithmetic, the policy and its
payload override, the in-memory `ToolBudget`, the scope key and the cost diagnostic are pure; the
database (`connect_db`, `record_spend`, `daily_spend`, `over_daily_budget`, `should_downgrade`,
`budget_status`) is a store and its own slice. 209 keys byte-identical, md5
`08a4887495a2a11398f75daeae21393f`.

**The finding outlived the port.** Serializing `estimate_cost` in the probe meant comparing
serde_json's float rendering against Python's, and they disagree on *every* float below `1e-4`:
serde_json writes `4.93e-5` as `0.0000493` and `1e-6` as `1e-6`, where Python writes `4.93e-05` and
`1e-06`. A single request's cost is exactly that size -- a fraction of a cent -- and
`diagnostics["costUsd"]` is served to the caller. The crate already had the right renderer
(`python_json::float_str`, with `1e-05`/`1e+16` pinned), but three containers --
`dumps_default_separators`, `dumps_compact`, `OrderedJson::render` -- sent numbers through
serde_json's own `to_string` and bypassed it. Fixed in a commit of its own (`36fc2de7`), because
eight modules consume those renderers; the two probes with hashes on record were re-run
(`request_messages` still `7c7860c9...`, `memory` byte-identical). This was visible at all only
because the corpus uses **real costs** rather than round numbers -- the third time in this
migration that the corpus's composition decided whether the probe could see anything.

**Two asymmetries recorded, not smoothed over.** `diagnostics_with_cost` reads a non-dict as `{}`
where the oracle's bare `dict(...)` raises -- unreachable, and the module doc says so, explicitly
so that nobody later "unifies" it with `cost_from_usage`, which *does* guard in the oracle. And
`BudgetPolicy::to_value` cannot carry the oracle's `to_dict` insertion order (`maxTotalTokens,
maxAgentTokens, maxSearchCalls, maxToolCalls, maxEstimatedCostUsd, policy`) because a
`serde_json::Map` is a `BTreeMap` -- and the payload reaches the wire as
`diagnostics["budgetPolicy"]`, where Python's response `json.dumps` does not sort. The serializer
that serves it must be handed the order; the assembly slice owns that decision, and the module doc
now states it.

**Corpus traps pinned**: a usage value that is present but cannot convert is *skipped* (so a later
spelling can still win) rather than read as zero; a present negative limit is *floored* while an
unconvertible one *falls back* -- two directions, two assertions; an unknown `budgetPolicy` cannot
be turned on by a payload; the scope cap is 120 code points, which is what keeps a 200-character
Chinese scope from becoming an unbounded ledger key.

Verification: 209 keys byte-identical, md5 `08a48874...`; tests 337 -> **347**; `fmt --check` clean;
`clippy --all-targets` exit 0 with no diagnostics (one `explicit_auto_deref` found and fixed).

What is left before `build_deepseek_request` itself: the SQLite ledger (`should_downgrade` is the
assembly's consumer), the attachment-expansion path behind the file index, and then the assembly
with the diagnostics serializer.
