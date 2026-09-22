# Specification: Browser Engine Sidecar (Rust CDP)

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

- Status: Accepted direction, **stages 1–3 implemented and probe-verified locally**
- Date: 2026-09-20
- Delivery line: after 4.8.0
- Architecture decision: [ADR-0050](../adr/ADR-0050-browser-engine-sidecar.md)
- Delivery roadmap: [Native Runtime Migration Roadmap](../NATIVE_RUNTIME_MIGRATION_ROADMAP.md)

## Objective

Give the native runtime the engine behaviour the oracle reaches through Playwright —
real navigation, script execution, screenshots, input, downloads — without putting
Python or Node into the native plane, and without changing what today's static path
already answers.

## Measured starting point

| fact | measurement |
|---|---|
| Engine layers | `playwright` package 108 MB (contains `driver/node.exe` — Node) + Chromium 416 MB + headless shell 270 MB |
| Pin | `requirements-browser.txt: playwright==1.61.0`; local CLI 1.60.0 |
| Production images | none carries a browser: `Dockerfile` installs `requirements.txt` only; `rust/Dockerfile` = `debian:bookworm-slim` + `ca-certificates curl`, non-root uid 10001; `go/Dockerfile` = static `alpine:3.20` |
| Image audits | `scripts/check_native_images.py` audits three targets for zero-Python isolation |
| CI precedent | two lanes run `playwright install --with-deps chromium` for tests and release evidence |
| Rust workspace | has `tokio`, `hyper`, `reqwest`, `futures-util`; has **no WebSocket client** |

## Boundary

The engine is a separate process reached over versioned gRPC/Protobuf (ADR-0049).

```
proto/browser/v1/browser.proto      package deepseek.browser.v1;
service BrowserEngine { Status, OpenUrl, ReadPage, ExtractLinks, Screenshot,
                        Click, TypeText, Select, Scroll, Download }
```

- one browser context per session id; the gateway keeps the session id mapping it
  already has.
- one call per action, with the oracle's timeouts: `goto` 30 s, `inner_text` 2 s,
  `click`/`fill`/`select_option` 5 s, `expect_download` 15 s.
- the sidecar writes no durable store. Downloads land in the isolated download
  directory and return as bytes, so media/RAG ownership does not move.
- `playwright_available()` stays the single switch and stays `false` until the
  sidecar answers; its absence means the static controller runs.

## Engine surface (what the oracle uses, action by action)

| action | oracle API | CDP work |
|---|---|---|
| controller | `chromium.launch_persistent_context(accept_downloads=True)` | spawn with `--user-data-dir`, `Target`/`Browser` setup, `Page.setDownloadBehavior` |
| `open_url` | `goto(wait_until="domcontentloaded", 30 s)` | `Page.navigate` + `Page.domContentEventFired` |
| `read_page` | `inner_text(2 s)`, `content()`, `title()`, `url` | `Runtime.evaluate` (`innerText`/`documentElement.outerHTML`), `Page.getNavigationHistory` |
| `screenshot` | `screenshot(type="png", full_page=True)` | `Page.captureScreenshot` + `Page.getLayoutMetrics` for full page |
| `click` / `type_text` / `select` | `locator.click/fill/select_option(5 s)` | `DOM.querySelector` + `Input.dispatchMouseEvent` / `dispatchKeyEvent` / `Runtime.callFunctionOn` |
| `scroll` | `mouse.wheel(x, y)` | `Input.dispatchMouseEvent(type="mouseWheel")` |
| `download` | `expect_download(15 s)` | `Browser.setDownloadBehavior` + the download event window |
| `extract_links` | `locator.evaluate(...)` | `Runtime.evaluate` |

## Parity rules

1. **Declared subset.** The nine actions above are in scope; anything else stays
   refused.
2. **Equal where equality is measurable.** Page shape (`url`, `title`, `text`, links)
   and the action result envelopes are pinned by probes in the same shape as
   `browser_page_parity_probe`, which is how the static path was pinned
   (`8e2153f380f7b2b8d40f71b005d467e6`).
3. **Non-equal by construction, listed rather than hidden.**
   - locator auto-waiting and actionability checks
   - `domcontentloaded` timing and lazy-load side effects
   - the download event window and `suggested_filename` semantics
   - error text for timeouts (same code, wording not guaranteed)
4. **"Byte parity with Playwright" is not a goal** and must not be claimed in
   `migration-matrix.md` or `docs/BROWSER.md`.

## Staging

1. proto + sidecar skeleton + `Status` + the fail-closed switch.
2. `OpenUrl` + `ReadPage` + an engine parity probe.
3. the remaining actions, one probe per group.
4. image (`rust/Dockerfile`-style multi-stage), the fourth audit entry in
   `scripts/check_native_images.py`, a CI lane reusing the existing chromium install
   step, and the Chromium revision pin.

### What each stage actually delivered

1. `proto/browser/v1/browser.proto`, `rust/crates/deepseek-browser` (binary
   `deepseek-browser`, loopback-only listener), the `BrowserEngine` service, and the
   fail-closed switch. Merged as `bae68f0b`.
2. The CDP engine (`rust/crates/deepseek-browser/src/engine.rs`) and the sidecar's
   ten RPCs; `tasks/native-runtime/browser_engine_parity_probe.py` compares the
   engine against the Playwright oracle over six fixtures.
3. The gateway seam: `deepseek-policy::browser_engine` declares what the policy crate
   needs, `deepseek-gateway::browser_engine_client` implements it over the generated
   tonic client on a dedicated runtime thread, and `ToolRoundExecutor` attaches it to
   the tool loop. `CloseSession` was added to the proto so a closed session releases
   its Chromium and profile.
4. **Image, audit and lane.** `rust/Dockerfile` gained a `browser-runtime` base and a
   `browser` stage (Chromium from the distro, non-root `deepseek`, entrypoint
   `deepseek-browser`). **Every** build of that Dockerfile now names its target: adding
   a stage moved the default, and for one CI run five lanes built the browser image and
   ran it as their gateway, timing out on a process that speaks gRPC instead of HTTP.
   `tests/test_rust_docker_config.py` now fails if any build of that file omits
   `--target`, so the class cannot return quietly.
   `scripts/check_native_images.py` requires the `browser` stage and audits it for the
   same zero-Python, non-root rules as `worker` and `gateway` — a rule shown able to
   fail by deleting the stage. The `native-browser-engine` CI lane installs the
   Chromium that `requirements-browser.txt` pins, points the engine at it, and runs
   the live engine tests, the gateway seam, and this specification's parity probe.

## Measured parity (stage 3)

`python tasks/native-runtime/browser_engine_parity_probe.py --rust-example
rust/target/debug/examples/browser_engine_parity_probe.exe` runs the oracle and the
engine over `basic.html`, `controls.html`, `download.html`, `form.html`,
`injection.html` and `sample-report.html` and compares them field by field:

| field | result |
|---|---|
| `url`, `title`, `text` | **identical**, all six fixtures |
| `links` | **identical** (after the per-load blob UUID is normalised) |
| `html` | **0 bytes differ** after collapsing whitespace; the engine reads `documentElement.outerHTML` with the doctype prepended, which is what `page.content()` serialises |
| `select`, `type_text`, `click`, `scroll` effects | **identical**, read back out of the live page — with one timing note below |
| a missing element | the oracle raises its timeout error; the engine answers `not_found`/404 — same condition, different code |
| screenshot | both PNG; byte length differs (12925 vs 17284), which no two Chromium builds agree on |
| download file name | the oracle reports the link's `download` attribute (`sample-report.html`); CDP's `allowAndName` reports a GUID. The bytes are identical |

### One timing divergence, measured the hard way

Chromium animates a wheel event, and `page.mouse.wheel` returns before the animation
lands. A probe that reads `window.scrollY` straight afterwards measures the race, not
the scroll: two consecutive runs produced `oracle 900 / engine 0` and then `oracle 0 /
engine 900`. The engine therefore waits for the position to settle before answering —
a deliberate divergence from `mouse.wheel`, since a caller asking the engine to scroll
wants a scrolled page — and the probe settles **both** sides before reading. With that,
the pair passes on repeat runs.

## Open items

- **Non-root Chromium inside a container** needs user namespaces or an explicit
  `DEEPSEEK_BROWSER_NO_SANDBOX=1`. The image does not set it, the compose service does
  not set it, and both say so — it is a security decision, not a default. A container
  that has neither the namespaces nor the opt-in fails closed: no engine, static
  controller, `Status.available == false`.
- **The image now builds locally, and here is what it weighs.** `--target browser` on the
  development machine produces **1.1 GB** (`deepseek-browser-engine:4.8.0`) against
  **166 MB** for the gateway built from the same Dockerfile — the delta is Chromium plus
  its libraries, measured rather than the 100–150 MB this file first estimated.
  `--target gateway` was built too, and that is what confirms the target fix: an
  untargeted build would have produced the browser image under the gateway's name.
- **Three of the four probe pairs are still not CI steps.** The engine's parity probe
  runs in `native-browser-engine`; `title`, `file_routes` and `chat_stream_events`
  remain local evidence, as the older pairs are. Making them gates is a separate slice.
- The browser engine's actions beyond the declared nine — `extract_dom`, `save_snapshot`,
  `close_session` — stay refused by the seam: the first is carried by `ReadPage`, the
  second is Python-owned, and the third is `CloseSession` on the engine boundary.
