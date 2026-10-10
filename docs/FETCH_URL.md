# fetch_url (DNS-time SSRF + locked HTTP)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **ported, wired into the tool loop, probe-verified locally.** Exact-head
CI has not run against this slice.

`deepseek-policy::fetch_url` mirrors `tools.fetch_url` /
`resolve_public_url` / `fetch_public_url` in
`deepseek_infra/infra/tool_runtime/tools.py`. The gateway's
`fetch_provider::locked_http_get` is the connection the oracle's
`LockedHTTPConnection` performs: connect to the DNS-pinned address, send the
original `Host` header, use the original hostname as SNI.

## Two layers of SSRF

1. **Static** — `tool_policy::evaluate_url_safety`, run by the tool gate before
   any branch. Scheme, credentials, localhost suffixes, literal private IPs.
2. **DNS-time** — `resolve_public_url` / `ensure_public_address`. Every address
   `getaddrinfo` returns must be public, and the client must not resolve the
   name a second time. Redirects go back through `resolve_public_url`, so a
   `302` to `http://127.0.0.1/admin` is refused.

A missing, unported, or fail-closed branch is not this slice: without a
`FetchContext` the dispatcher reports `fetch_url is not enabled for this
request`, matching `web_search` without its callback.

## What is injected

DNS and HTTP are `FetchContext` callbacks, so the policy crate stays free of
TLS. Tests and the parity probe drive both. The gateway binds
`system_dns` + `locked_http_get` (`reqwest::blocking` with
`redirect(Policy::none())` and `resolve(host, pinned_addr)`).

## Trafilatura is not reproduced

The oracle uses `trafilatura.extract` when that package is importable. It is
not a production dependency, so every shipped deployment and every CI leg takes
the `HTMLTextExtractor` fallback. This port is that fallback.

## Verification

- `tasks/native-runtime/fetch_url_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/fetch_url_parity_probe.rs`
- `cargo test -p deepseek-policy fetch_url`
- `cargo test -p deepseek-gateway fetch_provider`
- `chat_route_refuses_a_private_fetch_url_target` — the wired loop refuses
  `http://127.0.0.1/admin` instead of answering `Tool did not run`
