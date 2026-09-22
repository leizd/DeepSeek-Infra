# browser_* (safety gate, static controller, and the CDP engine)

<!-- docs-language-switcher:start -->
[中文](../README.md) / [English](../README.en.md)
<!-- docs-language-switcher:end -->

Status: **ported into the tool loop, and the browser engine behind it now exists.**
The safety gate and the static controller are green through CI on the PR that carried
them (35/35 jobs, head `bd1602e3`, merged as `5648329f`). The CDP engine and the
gateway seam that reaches it are probe-verified locally and are **not** yet on an
exact-head CI run or in any image.

`deepseek-policy::browser_safety` mirrors `infra/browser/safety.py`.
`deepseek-policy::browser` mirrors `execute_browser_action` with the oracle's
**StaticController** fallback (the path used when Playwright is not installed) *and*
the engine path, which is selected the way the oracle selects Playwright: an engine
that answers `Status` with `available: true`.

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
- **The engine**, when one is reachable: `deepseek-browser` is a separate process that
  spawns a headless Chromium and drives it over CDP. The gateway reaches it through
  `deepseek-policy::browser_engine` (the seam) and
  `deepseek-gateway::browser_engine_client` (the tonic client on its own runtime
  thread). `open_url`, `read_page`, `extract_links`, `extract_dom`, `screenshot`,
  `click`, `type_text`, `select`, `scroll`, `download` and `close_session` all run for
  real. `docs/specs/browser-engine-sidecar.md` records the measured parity and the
  divergences that are non-equal by construction.
- Without an engine (no `DEEPSEEK_BROWSER_ENGINE_ADDR`, or nothing listening) the
  static controller answers and HTTP fetch is refused
  (`static browser controller only reads approved file:// fixtures`). A deployment with
  no Chromium is **degraded, not broken**.
- Media/RAG snapshot **writes** stay Python-owned (`indexed: false`,
  `snapshot.persisted: false`)

## Configuration

| variable | meaning |
|---|---|
| `DEEPSEEK_BROWSER_ENGINE_ADDR` | where the gateway finds the engine; defaults to `http://127.0.0.1:50053` |
| `DEEPSEEK_BROWSER_ENGINE_LISTEN` | where the sidecar listens; loopback only, nonzero port |
| `DEEPSEEK_BROWSER_CHROMIUM` | the browser binary the sidecar spawns; unset means `available: false` |
| `DEEPSEEK_BROWSER_NO_SANDBOX` | `1`/`true` adds `--no-sandbox` (containers without user namespaces) |
| `DEEPSEEK_BROWSER_PROFILE_ROOT` | per-session profile directories; defaults under the temp dir |
| `DEEPSEEK_BROWSER_DOWNLOAD_ROOT` | where downloads are staged before they are returned as bytes |

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
- `tasks/native-runtime/browser_engine_parity_probe.py` ↔
  `rust/crates/deepseek-browser/examples/browser_engine_parity_probe.rs` — the engine
  against the Playwright oracle over six fixtures; `result: PASS`, 0 differing HTML
  bytes
- `cargo test -p deepseek-browser --test engine_live` — the engine against a real
  browser, every declared action, gated on `DEEPSEEK_BROWSER_CHROMIUM`
- `cargo test -p deepseek-gateway --test browser_engine_e2e` — gateway client → gRPC →
  sidecar process → CDP → Chromium → HTTP fixture, 10 named PASS checks including the
  gate refusing before the engine and `close_session` removing the profile
- `rust/Dockerfile` target `browser` — the engine's own image (Chromium, non-root
  `deepseek`, loopback listener), held to the same zero-Python and non-root rules as
  `worker` and `gateway` by `scripts/check_native_images.py`; built locally at **1.1 GB**
  against **166 MB** for the gateway from the same Dockerfile
- CI `native-browser-engine` — installs the Chromium `requirements-browser.txt` pins,
  then runs the live engine tests, the gateway seam, and the engine parity probe
- A browser that will not start reports **why**: the launch error carries the last of
  Chromium's own stderr and whether the process exited, because "did not report a
  DevTools socket" on its own is a failure nobody can act on
- `chat_route_blocks_a_private_browser_url`
- `file_fixture_open_returns_page_text`
