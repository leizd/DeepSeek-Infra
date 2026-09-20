# Specification: Browser Engine Sidecar (Rust CDP)

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

- Status: Accepted direction, **not implemented**
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

## Open items for review before staging 4

- non-root Chromium inside a container: relax the sandbox or configure user
  namespaces — a security review item.
- the image grows by 270–420 MB plus Chromium's shared libraries (100–150 MB,
  estimated: it could not be weighed on Windows).
- one more `.proto` moves `proto_files` from 8 to 9 and adds generated outputs; the
  contract assertion pins count and outputs together.
