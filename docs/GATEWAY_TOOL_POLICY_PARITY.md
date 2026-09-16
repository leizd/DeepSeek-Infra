# Gateway tool-policy parity

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->


Status: **guards, engine, and audit layer ported and byte-verified; tool execution
still refuses.**

This document records how the Rust port of the oracle's tool policy is proven
equivalent to `deepseek_infra/infra/tool_runtime/tool_policy.py`, what is
deliberately **not** ported yet, and where the crate's pre-existing generic
guards diverge from the oracle.

## Why this slice comes before tool execution

`tool_policy.py` is the gate the oracle applies **before** a tool runs. Porting
tool execution (layer 2 of the tool-loop decomposition) without it would mean
executing model-chosen side effects with no SSRF guard, no path-escape guard, no
secret-exfiltration guard, and no injection scrubbing — strictly weaker than the
Python it replaces. So the gate comes first, and it is pure, which makes it
measurable byte-for-byte.

## Scope

| Part of `tool_policy.py` | Status |
| --- | --- |
| `RISK_ORDER`, `ALLOW`/`DENY`/`NEEDS_CONFIRMATION` | ported |
| `ToolMetadata`, `TOOL_METADATA`, `tool_metadata`, `all_tool_names` | ported |
| `CAPABILITY_PROFILES`, `capability_tools` | ported |
| `validate_arguments`, `_validate_scalar` | ported |
| `evaluate_url_safety` | ported |
| `evaluate_path_safety`, `_evaluate_generic_path_safety`, `_iter_named_string_values`, `_url_candidate_for_guard` | ported |
| `evaluate_network_argument_safety` | ported |
| `arguments_contain_secret` | ported |
| `sanitize_external_text`, `sanitize_tool_result`, `sanitize_tool_result_for_external`, `_scrub_node` | ported |
| `_max_risk` | ported |
| `ToolPolicy.evaluate` | **not ported** — reads config and audit state |
| `write_audit_entry`, `read_recent_audit`, `write_external_audit_entry`, `tool_policy_status` | **not ported** — write/read an audit log |

The stateful remainder is a separate slice. Until it lands, nothing may execute a
tool on the strength of this module alone; the route keeps refusing tool rounds
with `NATIVE_CHAT_TOOL_ROUNDS_NOT_READY`.

## Method

Same method as the SSE and tool-round slices: replay an identical fixed corpus
through the **real oracle** and through the Rust port, then diff canonical JSON.

- Python: `tasks/native-runtime/tool_policy_parity_probe.py`
- Rust: `rust/crates/deepseek-policy/examples/tool_policy_parity_probe.rs`

The Python probe slices the contiguous pure region of `tool_policy.py`
(lines 59–563) and `exec`s it, rather than re-implementing anything. That region
is contiguous *because* several definitions are built from earlier ones at import
time (`CAPABILITY_PROFILES` calls `all_tool_names()`; `TOOL_METADATA`
instantiates the `ToolMetadata` dataclass), so slicing preserves those
relationships. The boundaries are asserted, so oracle drift fails loudly instead
of silently shrinking what is compared.

Reproduce:

```sh
python tasks/native-runtime/tool_policy_parity_probe.py > python.json
cd rust && cargo run -p deepseek-policy --example tool_policy_parity_probe > ../rust.json
diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)
```

`tr -d '\r'` is required: Python's text-mode stdout emits CRLF on Windows.

Result: **158 keys, byte-identical**, normalized MD5
`26c7723c89a4fb59c7ef9e412f1b4b97` on both sides.

## The IP classifier, derived rather than guessed

This was the only genuinely hard part, and the first attempt was wrong.

The oracle blocks when
`not is_global or is_private or is_loopback or is_link_local or is_multicast or is_reserved or is_unspecified`.
Guessing which ranges that union covers produced two defects — a false negative
(`1:0:0:2::3` allowed when the oracle blocks it) and a false positive
(`192.88.99.1` blocked when the oracle allows it).

The fix was to read CPython's own tables and collapse the union:

- **IPv4** — `is_global` is `not in 100.64.0.0/10 and not is_private`, so
  `not is_global` adds exactly the shared range on top of `is_private`;
  `is_reserved` is `240.0.0.0/4`, already inside `is_private`. The 14 ranges
  encoded are `_private_networks` ∪ {`100.64.0.0/10`} ∪ multicast.
- **IPv6** — `is_global` is literally `not is_private`, so `not is_global` adds
  nothing. The blocking set is `_private_networks` ∪ `_reserved_networks` ∪
  multicast, 23 ranges.
- **IPv4-mapped IPv6** — every predicate delegates to the underlying IPv4 address
  (`ipv4_mapped` is non-`None` exactly for `::ffff:0:0/96`). This is why
  `::ffff:1.2.3.4` is **allowed** while `::ffff:0:1` is **blocked**, even though
  `_private_networks` lists `::ffff:0.0.0.0/96` as a blanket block. Rust's
  `Ipv6Addr::to_ipv4_mapped()` matches CPython's `ipv4_mapped` exactly.

`fec0::/10` (deprecated site-local) is deliberately **not** blocked: it is in
neither table, and `fe00::/9` stops at `fe7f::`, so the oracle allows it. This
looks like a hole but mirroring it is correctness — a stricter guard here would
be a behavior change.

## Oracle rules that are load-bearing

### The deny reason embeds Python's `str(ip)` and `repr(list)`

Two message-parity surfaces that are easy to miss because they look like
formatting:

- The blocked-IP reason is `f"private or local ip is not allowed: {ip}"`, and
  Python renders an IPv4-mapped address in dotted form. Rust's `Display` emits
  `::ffff:0:1` where Python emits `::ffff:0.0.0.1`, so `render_ip` special-cases
  the mapping. All blocked IPs share this one message — the oracle does **not**
  distinguish loopback from link-local from reserved.
- The enum violation is `f"{key} must be one of {enum}"`, embedding Python's
  `repr` of the list (`['x', 'y']`, not `["x","y"]`), so the port carries a
  `python_repr` renderer.

### A bracketed non-IPv6 host is `invalid url`, not a blocked IP

`urlsplit` raises `ValueError` for `http://[127.0.0.1]/`, and the oracle maps
that to `"invalid url"`. It never reaches the IP check, so a naive port that
parses the bracket content as an IP would report the wrong reason.

### An unparseable short IPv4 form is a hostname

`ip_address("127.1")` raises in Python (strict parsing), so `http://127.1/` is
treated as a *name* and allowed, deferring to the DNS-time check inside
`fetch_url`. Rust's `Ipv4Addr::from_str` also rejects it, so the behavior matches
without special-casing.

### Check order in `evaluate_url_safety` decides the reason

`empty` → `invalid url` → `scheme not allowed` → `url credentials are not
allowed` → `missing host` → `local host is not allowed` → IP check. Reordering
any pair changes the message for inputs that fail several tests.

### Credentials are denied, not stripped

`parsed.username or parsed.password` → deny. Note this is exactly where the
crate's pre-existing `url_guard` diverges: it splits userinfo at `@` and
continues, i.e. it *rejects nothing*.

### `str(value or fallback)` on identifiers

`fileId`/`projectId` are read as `str(arguments.get(key) or "").strip()`, so a
falsy value (`0`, `false`, `""`, `null`, `[]`, `{}`) collapses to `""` while a
truthy non-string is stringified. The port mirrors the truthiness mapping.

### Only `external_output` tools are scrubbed

`sanitize_tool_result` consults metadata, so the same payload passed to
`web_search` is redacted but to `recall_memory` is untouched. External MCP tools
use `sanitize_tool_result_for_external`, which skips the lookup.

## Known divergences

### The pre-existing generic guards were weaker than the oracle

`url_guard.rs` and `path_guard.rs` back the gateway's `/policy/*` routes. They
are a *different model* (`Capability`/`RiskLevel`/`decision_id`) and they did not
match the oracle:

| Gap | `url_guard` (before) | oracle |
| --- | --- | --- |
| `.local` / `.localhost` / `.internal` suffix | not checked | blocked |
| trailing dot (`http://localhost./`) | not stripped | blocked |
| URL credentials | stripped and allowed | denied |
| multicast / reserved / CGNAT / non-global IPv4 | not checked | blocked |
| IPv6 reserved ranges (`4000::/3`, `e000::/4`, …) | not checked | blocked |

**The URL half of this is now fixed** — see "Aligning the URL guard" below.
`path_guard.rs` is a different operation (workspace containment over a
`{root, requested}` pair, not an argument-key scan) and is untouched; it needs its
own analysis rather than a straight delegation.

### Object key order

This workspace compiles `serde_json` without `preserve_order`, so sibling keys
iterate sorted rather than in insertion order. In
`collect_named_string_values` that can change *which* violation is reported first
when an arguments object has several offending keys; the verdict is unaffected.
`validate_arguments` can likewise order multiple violations differently. The
corpus keeps offending keys to one per case so the comparison is meaningful.

## Explicit non-goals

- Executing any tool, or wiring policy to a live route.
- `ToolPolicy.evaluate` and the audit log.
- Changing the frozen wire contracts: `object-set-v1`, Receipt v4, Commit v4,
  FastCDC v3, randomized Age, control-authority-v1, AuthorityCheckpoint v1,
  `dr-readiness-proof-v1`, `evidence-proof-v2`,
  `predictive-planning-proof-v1`, or the signed Federation documents.

## Dependency note

Ported code needs `regex`. `regex 1.13.0` was already in `Cargo.lock` as a
transitive dependency, so it is pinned exactly (`regex = "=1.13.0"`) and promoted
to a direct dependency of `deepseek-policy` — the lockfile gains one dependency
edge and no new crate version.

## Rollback

Additive and inert: a new module with no production caller, plus one dependency
edge. Reverting the commit restores the previous tree with no state to migrate.

## Test inventory

30 unit tests cover: table order and size, metadata trimming and `to_dict`
omission of internal tags, `max_risk` ordering and unknown-risk tie, capability
profile slices, local-host suffixes, credentials, scheme and missing-host
reasons, private/special-purpose IPv4 ranges, short IPv4 as hostname, public
addresses, IPv6 reserved ranges, IPv4-mapped rendering and delegation,
`fec0::/10` allowance, bracketed IPv4 as invalid URL, path identifiers, `fileId`
length bounds, generic path escapes, recursive network arguments, secret
detection in string leaves, injection redaction and counting, external-output
gating, nested scrubbing with non-text keys preserved, and schema validation
messages including Python `repr` rendering.

---

# Layer 3b: the engine and the audit layer

The pure guards above are the *predicates*. Layer 3b adds the thing that actually
decides — `ToolPolicy.evaluate` — plus the audit log that records every verdict.

## Scope

| Part of `tool_policy.py` | Status |
| --- | --- |
| `ToolPolicy.__init__`, `permissive`, `evaluate`, `_record`, `mark_tainted`, `is_tainted` | ported |
| `ToolPolicy.sanitize_result`, `denial_output`, `diagnostics` | ported |
| `is_sensitive_memory` (from `infra/data/memory.py`) | ported |
| `write_audit_entry`, `write_external_audit_entry` (entry construction + JSONL append) | ported |
| `_normalized_args_hash` | ported |
| `read_recent_audit` | ported |
| `tool_policy_status` | **not ported** — reads the audit path global and the config object |
| the `deepseek_infra.core.config` env reader itself | **not ported** — this is a config-layer concern, not a policy one |

## The decision order is the contract

`evaluate` returns on the first failing check, so the *order* determines which
reason a call reports when it fails several. Reordering any pair changes observable
output:

1. unknown tool → `unknown_tool`
2. capability → `capability_denied:{capability}`
3. schema (only denying when `enforce_schema`) → `schema_invalid`
4. SSRF → `ssrf_blocked:{why}`
5. path escape → `path_blocked:{why}`
6. sensitive memory → `sensitive_memory_blocked`
7. secret exfiltration → `secret_exfiltration_blocked`
8. confirmation → `requires_confirmation`
9. taint escalation → `taint_escalated_confirmation`
10. otherwise → `allow`, with `schema_warning` appended when soft violations exist

Two details that are easy to get wrong:

- **`fetch_url` bypasses the recursive guard.** It calls `evaluate_url_safety` on
  `args["url"]` directly, while every other network tool goes through
  `evaluate_network_argument_safety`, which *prefixes the offending key*. So the
  same private host yields `ssrf_blocked:private or local ip is not allowed: …`
  for `fetch_url` but `ssrf_blocked:host: private or local ip is not allowed: …`
  for `web_search`. The first draft of the unit test asserted the unprefixed form
  for `web_search` and was wrong; the probe settled it.
- **`denial_output` does not check the action.** Called on an allow it still
  returns a denial-shaped payload with `code: "forbidden"` and
  `error: "… blocked by tool policy (allow)"`. That looks like a bug and is not:
  it is the oracle's behaviour, and there is an explicit test so nobody "fixes" it.

## The audit layer

`_record` calls `write_audit_entry` inline. That is a side effect inside the
decision path, which makes the decision itself hard to compare. The port splits it
behind an `AuditSink`:

- `NullAuditSink` — the default; drops entries.
- `InMemoryAuditSink` — captures them, for tests and for shadow evaluation where
  decisions must be observable without touching the authoritative log.
- `JsonlAuditSink` — mirrors `write_audit_entry`: `create_dir_all` + append one
  line, JSON with sorted keys and unescaped non-ASCII.

**Best-effort is the contract, not laziness.** The oracle swallows every write
error so an unwritable audit log can never break a tool call. This port keeps that
behaviour but records the failure in `last_error()`, so the same non-fatal
semantics stay observable instead of vanishing. There is a test that points the
sink at a directory where the log file should be and asserts the failure is
recorded rather than propagated.

The entry is `{"ts": …, "scope": …, **decision.to_dict()}`, written with
`sort_keys=True`. The only non-deterministic field is `ts`, so the probe injects a
fixed clock and masks `ts` on both sides before comparing — everything else is
byte-compared, and the timestamp's own shape is pinned by unit tests
(`utc_isoformat_seconds`) with hand-checked anchors (epoch, day boundary, Unix 1e9).

`normalized_args_hash` is the audit's redaction primitive: `sha256` of sorted
compact JSON, truncated to 16 hex, prefixed `sha256:`. Four oracle-derived vectors
are pinned in unit tests, including a non-ASCII payload that proves
`ensure_ascii=False` is reproduced.

## A naming decision worth recording

The oracle's decision type is called `PolicyDecision`. This crate **already**
exports a different `PolicyDecision` — the `Capability`/`RiskLevel` model behind
the `/policy/*` routes. The port therefore names its type `ToolPolicyDecision`:
the two are unrelated, and sharing a name would make importing the wrong one an
easy mistake with security consequences.

## Method

The Python probe now measures two regions of the same file:

1. the contiguous constants + guards + engine region (lines 59–901), `exec`ed
   verbatim with only the config globals and the audit path rebound;
2. the audit functions, lifted individually and driven against a **real temporary
   JSONL file** — the writer that ships is the writer measured, not a
   re-implementation of its entry dict.

Result: **197 keys, byte-identical**, normalized MD5
`d51462e06a0e6ccd03db7ed05ab77d71` on both sides — covering 59 URL cases, 25 path
cases, 10 network cases, 6 secret cases, 12 text cases, 6 result cases, 13 schema
cases, 11 metadata cards, 8 capability profiles, 7 risk-reduction cases, 25 engine
evaluations (each with its decision, denial payload, and diagnostics), 5
sanitization runs, 2 permissive runs, 4 argument hashes, and the 5 audit entries
the run produced (2 from the permissive default, which audits because its `audit`
comes from config, then 2 policy verdicts and 1 external-MCP entry).

## Verification

- `cargo test -p deepseek-policy` → 77 tests, all pass.
- `cargo clippy -p deepseek-policy --all-targets --all-features -- -D warnings` →
  clean; `cargo fmt` applied.
- Parity re-confirmed after formatting.

## Explicit non-goals

- Wiring the policy into any live route or executor.
- `tool_policy_status` and the config/env reader.
- The frozen wire contracts.

## Rollback

Additive and inert: the engine has no production caller and the audit sink writes
only where a caller points it. Reverting the commit restores the previous tree.

---

# Aligning the URL guard, and the status endpoint

## The finding that reframed this work

While scoping the executor slice, the oracle's own Rust delegation turned out to
be the risk:

```
execute_tool_call  ->  _evaluate_rust_policy(...)   (tools.py)
                   ->  rust_core.policy_client.check_url / check_path
                   ->  POST /policy/url, /policy/path   (gateway)
                   ->  url_guard::validate_url_access, path_guard::...
```

`DEEPSEEK_RUST_POLICY` defaults to **false** (`infra/rust_core/config.py`), so
today the Python guards decide. But flipping that flag would have moved SSRF and
path decisions onto the **weaker** guard documented above — `.local`/`.internal`
hosts, trailing-dot localhost, credential-bearing URLs, multicast, reserved,
CGNAT and the IPv6 reserved ranges would all have started passing.

That is the "migration must not weaken security" line, so wiring execution onto
that gate first would have been the wrong order. The gate had to become correct
before anything was allowed to depend on it.

## The fix

`url_guard::validate_url_access` now delegates to
`tool_policy::evaluate_url_safety` — the oracle-parity guard — and maps the
oracle's denial reason onto the crate's decision codes.

The response envelope is unchanged, so the bridge contract holds:
`policy_client._parse_response` requires `allowed` (bool) plus non-empty string
`code`, `reason`, `decision_id`, `capability`, `risk_level`, and treats `code` as
**opaque** — no branch depends on its value.

One consequence is deliberate and worth stating: the oracle reports a single
`private or local ip is not allowed: …` verdict, so loopback, link-local, reserved
and multicast now all return `PRIVATE_NETWORK_BLOCKED` from the URL route.
`codes::LINK_LOCAL_BLOCKED` therefore stops being emitted *by this guard*; the
constant remains for callers that distinguish the two. Reproducing the oracle
means reproducing its collapsing, not inventing a finer taxonomy.

`UrlPolicy` can only **tighten**: the oracle accepts http(s) only, so listing
another scheme cannot reintroduce it. There is a test for exactly that.

`path_guard` is deliberately untouched. `validate_workspace_path` solves a
different problem — containment of a `{root, requested}` pair — and the oracle's
`evaluate_path_safety` is an argument-key scan over tool arguments. They are
complementary, not interchangeable, and merging them would change what the route
means. It needs its own slice.

## The status endpoint

`tool_policy_status` is ported together with the settings it reads, modelled as
`ToolPolicySettings` (the five knobs, with the config module's defaults) and
`ToolAuditPaths::under(root)` (mirroring `tool_audit_dir = root / ".tool-audit"`).

`ToolPolicyConfig::default()` now reads its four strictness fields *through*
`ToolPolicySettings::default()`, so the engine and the status payload cannot drift
apart — a test asserts the two agree.

`auditLogPath` is `str(pathlib.Path(...))`, which on Windows uses backslashes while
`PathBuf::display()` keeps whatever the caller wrote, so `render_path_like_python`
normalises the separator. Python additionally collapses `..` and repeated
separators; that is not reproduced because the config never produces such a path.

## Evidence

The URL corpus is now checked **two ways**, so the route cannot silently drift from
the guard it delegates to:

- `url::<label>` — the guard verdict and reason;
- `guard::<label>` — the `/policy/url` route's verdict, compared case by case
  against the oracle's `evaluate_url_safety`.

Result: **257 keys, byte-identical**, normalized MD5
`bae3a9e5eb30cdd80a7a28b31e1f433b`, covering the 59 URL cases twice, plus
`status` (settings, all six capability profiles, the 28-card catalog, and the
rendered audit path).

`cargo test -p deepseek-policy` → 82 tests, all pass. `cargo clippy
-p deepseek-policy --all-targets --all-features -- -D warnings` → clean.
`cargo check -p deepseek-gateway --all-targets` → still compiles.

## What this does *not* do

It does not enable `DEEPSEEK_RUST_POLICY`, and it does not wire tool execution. The
flag stays off; enabling it is a separate, explicit cutover that also needs
`path_guard` aligned and the failure-mode policy reviewed.
