# browser_* (safety gate + static HTML controller)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **ported into the tool loop. Safety probe-verified locally.**
Exact-head CI has not run against this slice.

`deepseek-policy::browser_safety` mirrors `infra/browser/safety.py`.
`deepseek-policy::browser` mirrors `execute_browser_action` with the oracle's
**StaticController** fallback (the path used when Playwright is not installed).

## What runs

- Default **off** (`BROWSER_CONTROL_ENABLED` unset) → `forbidden` /
  `browser_control_disabled`
- Private hosts / loopback / credentials → `forbidden` / `unsafe_url:…`
- High-risk click (`submit`/`pay`/…) and password fields →
  `requires_confirmation`
- Approved `file://` fixtures → static HTML parse (title, text, links)
- Playwright is **not** ported. HTTP fetch by the static controller is refused
  (`static browser controller only reads approved file:// fixtures`).
- Media/RAG snapshot **writes** stay Python-owned (`indexed: false`,
  `snapshot.persisted: false`)

## Verification

- `tasks/native-runtime/browser_safety_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/browser_safety_parity_probe.rs`
  (md5 `ae657d74bdda48155bea65a6b20c6993`, 2252 chars)
- `chat_route_blocks_a_private_browser_url`
- `file_fixture_open_returns_page_text`
