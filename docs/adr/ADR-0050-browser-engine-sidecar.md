# ADR-0050: Browser Engine as a Versioned gRPC Sidecar

<!-- docs-language-switcher:start -->
[中文](../../README.md) / [English](../../README.en.md)
<!-- docs-language-switcher:end -->

- Status: Accepted
- Date: 2026-09-20
- Applies to: the `browser_*` engine, after 4.8.0 (**not implemented yet**)
- Related: ADR-0049 (process boundaries, versioned gRPC), [`docs/BROWSER.md`](../BROWSER.md)
- Specification: [Browser Engine Sidecar](../specs/browser-engine-sidecar.md)
- Machine contract: none yet — the boundary proto becomes it

## Context

`browser_*` is ported and wired, but only its **static controller** is: the oracle's
`PlaywrightController` is not ported, so HTTP fetch is refused by design and a page
whose content is injected by script reads empty.

The engine is not one dependency but three layers, measured on this machine:

| layer | size | note |
|---|---|---|
| `playwright` Python package | 108 MB | ships `driver/node.exe` — the driver is **Node**, not Python |
| Chromium (`chromium-1228`) | 416 MB | `chromium_headless_shell-1228` is 270 MB |
| pin | `requirements-browser.txt: playwright==1.61.0` | installed by CI only |

No production image carries a browser today: `Dockerfile` installs `requirements.txt`
alone, `rust/Dockerfile` runs `debian:bookworm-slim` with `ca-certificates curl` as a
non-root user, `go/Dockerfile` is a static `alpine:3.20`. All three pass the
zero-Python image audit. Two CI lanes install Chromium — for tests and release
evidence, never for a shipped image.

The Rust workspace already has `tokio`, `hyper`, `reqwest` and `futures-util`, and
**no WebSocket client**, which a CDP connection needs.

## Decision

1. **The engine is a separate optional process**, never a library in the gateway: its
   own binary, its own image, reached over **versioned gRPC/Protobuf** —
   `deepseek.browser.v1.BrowserEngine` — as ADR-0049 requires of new cross-language
   boundaries.
2. **Rust speaks CDP** to a headless Chromium it spawns itself. No Python, no Node
   driver, no second language runtime in the native plane.
3. **The seam does not move.** `execute_browser_action` and the safety gate stay where
   they are, and `playwright_available()` remains the single switch — still `false`
   until the sidecar answers. Its absence is not an error state: the static fallback
   runs, and that path is already byte-parity pinned.
4. **Ownership does not move.** The sidecar returns bytes and paths; media/RAG
   snapshot writes stay with their current owner.
5. **Parity is a declared subset, not a slogan.** The nine actions are in scope, each
   with its oracle timeout, and the semantics that cannot be reproduced (script
   timing, locator auto-waiting, download event windows) are an explicit list rather
   than a silence.

## Boundary contract (sketch, becomes the proto)

```
proto/browser/v1/browser.proto            package deepseek.browser.v1;
service BrowserEngine {
  Status, OpenUrl, ReadPage, ExtractLinks, Screenshot,
  Click, TypeText, Select, Scroll, Download
}
```

- one gRPC call per action; one browser context per session id.
- timeouts mirror the oracle: `goto` 30 s, `inner_text` 2 s, `click`/`fill`/
  `select_option` 5 s, `expect_download` 15 s.
- the sidecar never opens a durable store. Downloads land in the isolated download
  directory and come back as bytes.

## Image and CI consequences (measured)

- `scripts/check_native_images.py` audits **three** container targets today; the
  engine image is a fourth and needs its own audit entry.
- the image carries Chromium (270 MB headless shell, 416 MB full) plus the shared
  libraries Chromium needs — that library set could not be weighed on Windows and is
  estimated at 100–150 MB.
- **Running Chromium as non-root inside a container needs a decision**: relax the
  sandbox (`--no-sandbox`) or configure user namespaces. That is a security review
  item, not an implementation detail.
- adding one `.proto` moves `proto_files` from 8 to 9 and adds its generated outputs,
  which the contract assertion pins together.
- CI already has two lanes that run `playwright install --with-deps chromium` for
  tests; the engine lane copies that step rather than inventing one.

## Staging (proposal, in this order)

1. proto + sidecar skeleton + `Status` + the fail-closed switch (no engine ⇒ static).
2. `OpenUrl` + `ReadPage`, plus an engine parity probe shaped like
   `browser_page_parity_probe` (which is how the static path was pinned).
3. the remaining actions, one probe per group.
4. image, audit entry, CI lane, and the browser-version pin.

## Consequences

- the browser becomes optional and separately versioned: absent means degraded, not
  broken, because the fallback is already at parity.
- two new pins to maintain: the Chromium revision behind CDP, and the boundary proto.
- the real cost is semantics, not protocol: auto-waiting, load timing, and download
  events are where the work and the divergence live.
- `playwright_available()` stays `false` in the shipped runtime until the sidecar is
  actually wired, so nothing about today's behaviour changes by accepting this ADR.

## Rejected alternatives

- **Driving Playwright's Node driver from Rust.** Same footprint as the chosen path
  plus a Node runtime and a subprocess hop inside the native plane. It would pass the
  zero-Python audit — because the audit only looks for Python — which is why it is the
  wrong shape, not the right one.
- **No engine at all.** Loses arbitrary HTTP fetching in the native plane. Still a
  defensible choice; if it is ever chosen, this ADR is the record of what was given up.
- **An in-process engine inside the gateway image.** Puts 400 MB and the sandbox
  posture into the main image's attack surface.
- **Ad-hoc JSON RPC over localhost for the boundary.** Rejected by ADR-0049.
