# Browser Control Runtime

Applicable version: v2.8.2.

Browser Control Runtime lets Agents and Skills read pages, capture screenshots,
click, type, download, and persist browser evidence into Workspace Media.

The browser is not a raw Playwright escape hatch. Every action flows through:

```text
Agent / Skill
  -> browser.* tool
  -> Browser Safety
  -> Tool Policy
  -> Playwright Controller or static HTML fallback
  -> Browser audit + Media Library + Local RAG
```

## Configuration

Browser control is off by default.

```bash
BROWSER_CONTROL_ENABLED=0
BROWSER_HEADLESS=1
BROWSER_ALLOW_PRIVATE_HOSTS=0
BROWSER_REQUIRE_CONFIRM=1
BROWSER_DOWNLOAD_MAX_BYTES=50000000
BROWSER_SESSION_TTL_SECONDS=1800
```

Runtime state is local and must not be committed or packaged:

- `.browser-audit/audit.jsonl`
- `.browser-downloads/`
- `.browser-profiles/`

## Session Schema

```json
{
  "browserSessionId": "browser_xxx",
  "projectId": "proj_xxx",
  "status": "idle",
  "currentUrl": "https://example.com",
  "createdAt": "2026-07-03T00:00:00Z",
  "updatedAt": "2026-07-03T00:00:00Z",
  "headless": true,
  "engine": "playwright"
}
```

Each session uses an isolated temporary profile. The runtime never reads an
existing user browser profile or cookie jar.

## Actions

Registered tool names:

- `browser.open_url`
- `browser.read_page`
- `browser.screenshot`
- `browser.click`
- `browser.type_text`
- `browser.select`
- `browser.scroll`
- `browser.download`
- `browser.extract_links`
- `browser.extract_dom`
- `browser.close_session`

Write-like actions default to confirmation. Password fields, form submit,
delete, purchase, payment, confirm, and executable downloads are always treated
as high risk.

## Safety

Default policy:

- Browser control requires `BROWSER_CONTROL_ENABLED=1`.
- Private hosts and localhost are blocked unless `BROWSER_ALLOW_PRIVATE_HOSTS=1`.
- File URLs are accepted only for offline browser fixture directories.
- Downloads go to `.browser-downloads/<sessionId>/`.
- Existing browser cookies are never read.
- Browser action decisions are appended to `.browser-audit/audit.jsonl`.
- Browser DOM text, screenshots, and downloads are tainted as untrusted browser context.

## Workspace And Media

`browser.read_page` and `browser.save_snapshot` create a `webpage` media object
with `webpage_text` segments. Segments are indexed into Local RAG and retain
Browser citations such as:

```text
browser://browser_xxx#selector=main
```

`browser.screenshot` creates a `screenshot` media object. Downloaded files are
stored in the isolated download directory and registered as Media when the MIME
type is supported.

## Verification

Offline smoke:

```bash
python scripts/smoke_browser.py --offline --out docs/evidence/browser-v2.8.2.json --version 2.8.2
```

Offline eval:

```bash
python evals/runners/run_browser_eval.py --strict --out evals/reports/browser-v2.8.2.json --version 2.8.2
```

Preflight checks `docs/evidence/browser-v<version>.json` for:

- browser session creation
- page read
- screenshot
- link extraction
- private host blocking
- confirmation gate
- snapshot to Media
- snapshot to RAG
- audit log
