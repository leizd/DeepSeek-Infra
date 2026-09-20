# browser_* (safety gate + static HTML controller)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **ported into the tool loop. Probe-verified locally, and green through
CI on the PR that carries it (35/35 jobs, head `bd1602e3`, merged as `5648329f`).**

`deepseek-policy::browser_safety` mirrors `infra/browser/safety.py`.
`deepseek-policy::browser` mirrors `execute_browser_action` with the oracle's
**StaticController** fallback (the path used when Playwright is not installed).

## What runs

- Default **off** (`BROWSER_CONTROL_ENABLED` unset) → `forbidden` /
  `browser_control_disabled`
- Private hosts / loopback / credentials → `forbidden` / `unsafe_url:…`
- High-risk click (`submit`/`pay`/…) and password fields →
  `requires_confirmation`
- Approved `file://` fixtures → static HTML parse (title, text, links), **byte-parity
  with `_TextAndLinksParser`**: one part per data node joined with `"\n"`, `html.unescape`,
  script/style/noscript skipped by open-tag stack, and link hrefs resolved the way
  `urllib.parse.urljoin` resolves them (including its dropped empty fragment)
- Playwright is **not** ported. HTTP fetch by the static controller is refused
  (`static browser controller only reads approved file:// fixtures`).
- Media/RAG snapshot **writes** stay Python-owned (`indexed: false`,
  `snapshot.persisted: false`)

## Verification

- `tasks/native-runtime/browser_safety_parity_probe.py` ↔
  `rust/crates/deepseek-policy/examples/browser_safety_parity_probe.rs`
  (md5 `ae657d74bdda48155bea65a6b20c6993`, 2252 chars)
- `tasks/native-runtime/browser_controller_parity_probe.py` ↔
  `…/examples/browser_controller_parity_probe.rs` — which controller answered, across
  `unstarted` / refused / dispatched / failed (md5 `9e7a75c486d7dfdbd57eb6c871ce2409`)
- `tasks/native-runtime/browser_page_parity_probe.py` ↔
  `…/examples/browser_page_parity_probe.rs` — `{title, text, links}` for all five
  fixtures (md5 `8e2153f380f7b2b8d40f71b005d467e6`)
- `chat_route_blocks_a_private_browser_url`
- `file_fixture_open_returns_page_text`
