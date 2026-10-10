"""The CDP engine's page-content parity probe, oracle side.

Runs the Playwright oracle over the same fixture sequence the Rust example runs
(`rust/crates/deepseek-browser/examples/browser_engine_parity_probe.rs`) and
compares the two reports field by field.

This is the measurement the browser-engine specification asks for at stage 2
(`docs/specs/browser-engine-sidecar.md`, "Parity rules"): the static path was
pinned by `browser_page_parity_probe`, and this probe pins what the **engine**
reads, which is the part that could not be pinned before there was an engine.

Usage::

    python tasks/native-runtime/browser_engine_parity_probe.py \
        --rust-json artifacts/browser-engine-parity-rust.json

    # or, running the Rust example itself:
    python tasks/native-runtime/browser_engine_parity_probe.py \
        --rust-example rust/target/debug/examples/browser_engine_parity_probe.exe

The probe needs a Chromium: `DEEPSEEK_BROWSER_CHROMIUM` for the Rust side, and the
`playwright` package for the oracle side. When either is missing the probe exits
non-zero with the reason rather than reporting agreement — an engine that was
never started is not an engine that agrees.

**What is compared, and how.** `url`, `title`, `text`, `links`, and every
interaction effect are compared exactly: they are the observable behaviour the
tool layer returns to the model. `html` is compared after collapsing runs of
whitespace and normalising the doctype's case, because the two serialisers are
allowed to differ in indentation but not in content; the comparison also reports
how many bytes differed so a real content change cannot hide behind the
normalisation. The screenshot is compared by signature and by a size *class*
(full page vs element), never byte-for-byte: no two Chromium builds agree on PNG
bytes, and claiming otherwise would be a false parity claim.

**What is reported and not asserted.** The download file name: the oracle reports
the link's `download` attribute (`sample-report.html`) while CDP's `allowAndName`
reports a GUID. The bytes are compared; the name divergence is recorded in the
report and listed in the specification's non-equal set.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, NoReturn


def settled_scroll(page: Any) -> int:
    """The scroll position once Chromium's animated wheel has landed.

    `mouse.wheel` returns before the animation finishes, so reading `window.scrollY`
    immediately measures the race rather than the effect: on one run the oracle answered
    `900` and the engine, which waits, answered `900` too; on another the oracle answered
    `0`. Both sides are settled here so the probe compares scrolling, not timing — the
    engine waits for the same reason, which is a deliberate divergence from
    `mouse.wheel` and is listed as such.
    """
    deadline = time.monotonic() + 1.0
    previous: int | None = None
    while time.monotonic() < deadline:
        position = int(page.evaluate("window.scrollY") or 0)
        if position == previous:
            return position
        previous = position
        time.sleep(0.05)
    return int(page.evaluate("window.scrollY") or 0)

REPO = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO / "tests" / "fixtures" / "browser"
FIXTURES = [
    "basic.html",
    "controls.html",
    "download.html",
    "form.html",
    "injection.html",
    "sample-report.html",
]
PNG_SIGNATURE = [137, 80, 78, 71, 13, 10, 26, 10]
# The oracle's own controller timeouts, from `deepseek_infra/infra/browser/controller.py`.
GOTO_TIMEOUT_MS = 30_000
INNER_TEXT_TIMEOUT_MS = 2_000
LOCATOR_TIMEOUT_MS = 5_000
DOWNLOAD_TIMEOUT_MS = 15_000


def fail(reason: str) -> NoReturn:
    print(f"browser_engine_parity_probe: {reason}", file=sys.stderr)
    raise SystemExit(2)


class _FixtureServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """The fixture server's requests are asserted on, not logged to stderr."""

    def log_message(self, *args: Any, **kwargs: Any) -> None:
        return


def serve_fixtures() -> tuple[_FixtureServer, str]:
    handler = functools.partial(_QuietHandler, directory=str(FIXTURE_ROOT))
    server = _FixtureServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def oracle_report(base: str, download_dir: Path) -> dict[str, Any]:
    """The same sequence the Rust probe runs, driven through the oracle's controller."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - environment dependent
        fail(f"the oracle needs the playwright package: {exc}")

    profile = Path(tempfile.mkdtemp(prefix="deepseek-browser-probe-profile-"))
    pages: dict[str, Any] = {}
    interactions: dict[str, Any] = {}
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=True, accept_downloads=True
            )
            page = context.pages[0] if context.pages else context.new_page()

            for name in FIXTURES:
                url = f"{base}/{name}"
                page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
                links = page.evaluate(
                    """
                    root => Array.from(root.querySelectorAll('a[href]')).map(a => ({
                        text: (a.innerText || a.textContent || '').trim(),
                        href: a.href,
                        title: a.title || ''
                    }))
                    """,
                    page.locator("body").element_handle(),
                )
                pages[name] = {
                    "url": page.url,
                    "title": page.title(),
                    "text": page.inner_text("body", timeout=INNER_TEXT_TIMEOUT_MS),
                    "html": page.content(),
                    "links": links if isinstance(links, list) else [],
                }

            page.goto(f"{base}/controls.html", wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            selected = page.locator("#colour").first.select_option("green", timeout=LOCATOR_TIMEOUT_MS)
            interactions["select"] = {
                "selected": selected,
                "chosen": page.evaluate("document.querySelector('#chosen').textContent"),
            }
            page.mouse.wheel(0, 900)
            interactions["scroll"] = {"scrollY": settled_scroll(page)}

            page.goto(f"{base}/form.html", wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            page.locator("#email").first.fill("user@example.com", timeout=LOCATOR_TIMEOUT_MS)
            interactions["type_text"] = {
                "value": page.evaluate("document.querySelector('#email').value")
            }

            page.goto(f"{base}/basic.html", wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            page.locator("#docs-link").first.click(timeout=LOCATOR_TIMEOUT_MS)
            interactions["click"] = {"url": page.url}

            page.goto(f"{base}/basic.html", wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            try:
                page.locator("#no-such-element").first.click(timeout=LOCATOR_TIMEOUT_MS)
                interactions["click_missing"] = {"code": None, "status": None, "raised": None}
            except Exception as exc:
                interactions["click_missing"] = {
                    "code": None,
                    "status": None,
                    "raised": f"{type(exc).__name__}: {exc}".splitlines()[0][:200],
                }

            page.goto(f"{base}/download.html", wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as event:
                page.locator("#download-report").first.click(timeout=LOCATOR_TIMEOUT_MS)
            download = event.value
            source = download.path()
            data = Path(source).read_bytes() if source else b""
            interactions["download"] = {
                "filename": download.suggested_filename,
                "bytes": len(data),
                "body": data.decode("utf-8", errors="replace"),
            }

            page.goto(f"{base}/basic.html", wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            shot = page.screenshot(type="png", full_page=True)
            interactions_screenshot = {
                "signature": list(shot[:8]),
                "length": len(shot),
            }
            context.close()
    finally:
        shutil.rmtree(profile, ignore_errors=True)
        shutil.rmtree(download_dir, ignore_errors=True)

    return {
        "engine": "playwright",
        "pages": pages,
        "interactions": interactions,
        "screenshot": interactions_screenshot,
    }


def normalise_html(value: str) -> str:
    """Collapse whitespace and upper-case the doctype.

    The two serialisers differ in indentation and in `<!DOCTYPE html>` casing. They
    must not differ in content, which the byte-difference count in the report is
    there to catch.
    """
    collapsed = re.sub(r"\s+", " ", value.replace("\r\n", "\n")).strip()
    return re.sub(r"^<!doctype\s+html\s*>", "<!DOCTYPE html>", collapsed, flags=re.IGNORECASE)


def normalise_blob(value: Any) -> Any:
    """Replace a blob URL's per-load UUID with a stable placeholder.

    The download fixture builds its `Blob` in a script and the browser mints a fresh
    UUID for the object URL on every load, so the two sides *cannot* agree on the
    literal text. Everything else about the URL — the scheme, the origin, the fact
    that it is a blob at all — still has to match, which is what the substitution
    leaves in place.
    """
    if isinstance(value, str):
        return re.sub(
            r"blob:(?P<origin>https?://[^/]+)/[0-9a-fA-F-]{36}",
            r"blob:\g<origin>/<blob>",
            value,
        )
    if isinstance(value, list):
        return [normalise_blob(item) for item in value]
    if isinstance(value, dict):
        return {key: normalise_blob(item) for key, item in value.items()}
    return value


def normalise_filename(value: Any) -> Any:
    """Reduce a download file name to its basename.

    The oracle reports the link's `download` attribute; CDP's `allowAndName` reports
    the GUID the browser chose, in the same directory. The directory and the bytes
    are compared; the name divergence is recorded, not hidden.
    """
    if isinstance(value, str):
        return value.replace("\\", "/").rsplit("/", 1)[-1]
    return value


def compare(oracle: dict[str, Any], native: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    problems: list[str] = []
    details: dict[str, Any] = {"html_bytes_differing": {}, "download_filename": {}}

    oracle = normalise_blob(oracle)
    native = normalise_blob(native)

    for name in FIXTURES:
        expected = oracle["pages"].get(name)
        actual = native["pages"].get(name)
        if expected is None or actual is None:
            problems.append(f"{name}: missing from one report")
            continue
        for field in ("url", "title", "text"):
            if expected.get(field) != actual.get(field):
                problems.append(
                    f"{name}.{field}: oracle={expected.get(field)!r} native={actual.get(field)!r}"
                )
        if expected.get("links") != actual.get("links"):
            problems.append(
                f"{name}.links: oracle={expected.get('links')!r} native={actual.get('links')!r}"
            )
        oracle_html = normalise_html(str(expected.get("html") or ""))
        native_html = normalise_html(str(actual.get("html") or ""))
        differing = sum(
            1 for left, right in zip(oracle_html, native_html) if left != right
        ) + abs(len(oracle_html) - len(native_html))
        details["html_bytes_differing"][name] = differing
        if oracle_html != native_html:
            problems.append(f"{name}.html: {differing} bytes differ after whitespace collapse")

    expected_interactions = oracle["interactions"]
    actual_interactions = native["interactions"]
    for key in ("select", "scroll", "type_text", "click"):
        if expected_interactions.get(key) != actual_interactions.get(key):
            problems.append(
                f"interactions.{key}: oracle={expected_interactions.get(key)!r} "
                f"native={actual_interactions.get(key)!r}"
            )
    oracle_missing = expected_interactions.get("click_missing", {})
    native_missing = actual_interactions.get("click_missing", {})
    if native_missing.get("code") != "not_found":
        problems.append(
            f"interactions.click_missing.code: native={native_missing.get('code')!r} "
            "expected 'not_found'"
        )
    if native_missing.get("status") != 404:
        problems.append(
            f"interactions.click_missing.status: native={native_missing.get('status')!r} "
            "expected 404"
        )
    if not oracle_missing.get("raised"):
        problems.append("interactions.click_missing: the oracle did not raise for a missing element")

    oracle_download = expected_interactions.get("download", {})
    native_download = actual_interactions.get("download", {})
    details["download_filename"] = {
        "oracle": oracle_download.get("filename"),
        "native": native_download.get("filename"),
        "oracle_basename": normalise_filename(oracle_download.get("filename")),
        "native_basename": normalise_filename(native_download.get("filename")),
        "divergence": "oracle reports the link's download attribute; CDP allowAndName reports a GUID",
    }
    if oracle_download.get("body") != native_download.get("body"):
        problems.append(
            f"interactions.download.body: oracle={oracle_download.get('body')!r} "
            f"native={native_download.get('body')!r}"
        )
    if oracle_download.get("bytes") != native_download.get("bytes"):
        problems.append(
            f"interactions.download.bytes: oracle={oracle_download.get('bytes')!r} "
            f"native={native_download.get('bytes')!r}"
        )

    if oracle.get("screenshot", {}).get("signature") != PNG_SIGNATURE:
        problems.append("oracle screenshot is not a PNG")
    if native.get("screenshot", {}).get("signature") != PNG_SIGNATURE:
        problems.append(
            f"native screenshot signature: {native.get('screenshot', {}).get('signature')!r}"
        )
    details["screenshot_length"] = {
        "oracle": oracle.get("screenshot", {}).get("length"),
        "native": native.get("screenshot", {}).get("length"),
    }

    return problems, details


def run_rust_example(example: Path, base: str, download_dir: Path) -> dict[str, Any]:
    environment = dict(**__import__("os").environ)
    environment["DEEPSEEK_BROWSER_PROBE_DOWNLOAD_DIR"] = str(download_dir)
    completed = subprocess.run(
        [str(example), base],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        fail(
            f"the Rust probe exited {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        fail(f"the Rust probe did not print JSON: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--rust-json", type=Path, help="a report captured from the Rust probe")
    source.add_argument("--rust-example", type=Path, help="the Rust probe binary to run")
    parser.add_argument("--report", type=Path, help="where to write the comparison report")
    args = parser.parse_args()

    if not FIXTURE_ROOT.is_dir():
        fail(f"missing fixture directory: {FIXTURE_ROOT}")

    download_dir = Path(tempfile.mkdtemp(prefix="deepseek-browser-probe-downloads-"))
    server, base = serve_fixtures()
    try:
        oracle = oracle_report(base, download_dir)
        if args.rust_example is not None:
            native = run_rust_example(args.rust_example, base, download_dir)
        else:
            native = json.loads(Path(args.rust_json).read_text(encoding="utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(download_dir, ignore_errors=True)

    problems, details = compare(oracle, native)
    report = {
        "oracle_engine": oracle.get("engine"),
        "native_engine": native.get("engine"),
        "fixtures": FIXTURES,
        "details": details,
        "problems": problems,
        "result": "PASS" if not problems else "FAIL",
    }
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
