# Gateway tool-policy parity (pure core)

Status: **pure policy core ported and byte-verified; tool execution still refuses.**

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

### The pre-existing generic guards are weaker than the oracle

`url_guard.rs` and `path_guard.rs` back the gateway's `/policy/*` routes. They
are a *different model* (`Capability`/`RiskLevel`/`decision_id`) and they do not
match the oracle:

| Gap | `url_guard` | oracle |
| --- | --- | --- |
| `.local` / `.localhost` / `.internal` suffix | not checked | blocked |
| trailing dot (`http://localhost./`) | not stripped | blocked |
| URL credentials | stripped and allowed | denied |
| multicast / reserved / CGNAT / non-global IPv4 | not checked | blocked |
| IPv6 reserved ranges (`4000::/3`, `e000::/4`, …) | not checked | blocked |

This is **not** fixed here: tightening it changes a registered route's behavior,
which deserves its own slice with its own evidence. It is recorded as a real
finding and a follow-up. No production path depends on it yet (the Rust gateway
is not the authority), so the exposure is currently latent.

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
