"""Run the real-browser 4.0.1 frontend safety and offline smoke gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from deepseek_infra.web.server import create_server  # noqa: E402


VERSION = APP_VERSION


def git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, check=False, text=True
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def git_dirty() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, check=False, text=True
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def wait_until_ready(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"frontend server did not become ready: {url}")


async def run_browser(base_url: str) -> dict[str, str]:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import FilePayload
    from playwright.async_api import async_playwright

    checks: dict[str, str] = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="allow")
        await context.add_init_script(
            """
            localStorage.setItem('deepseek-infra.theme-style', 'linear');
            localStorage.setItem('deepseek-infra.theme-mode', 'dark');
            """
        )
        page = await context.new_page()
        console_errors: list[str] = []
        page_errors: list[str] = []
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        async def mock_chat(route: Any) -> None:
            body = '\n'.join(
                [
                    json.dumps({"type": "content", "text": "Browser smoke reply"}),
                    json.dumps(
                        {
                            "type": "done",
                            "content": "Browser smoke reply",
                            "model": "deepseek-v4-pro",
                            "usage": {},
                        }
                    ),
                    "",
                ]
            )
            await route.fulfill(status=200, headers={"Content-Type": "application/x-ndjson"}, body=body)

        async def mock_config(route: Any) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "hasServerKey": True,
                        "hasSearch": False,
                        "computerUrl": base_url,
                        "phoneUrl": base_url,
                        "uploadLimits": {"fileMaxBytes": 200_000_000, "requestMaxBytes": 220_000_000, "maxFiles": 8},
                    }
                ),
            )

        upload_release = asyncio.Event()

        async def hold_upload(route: Any) -> None:
            await upload_release.wait()
            try:
                await route.abort("aborted")
            except PlaywrightError:
                pass

        await page.route("**/api/config", mock_config)
        await page.route("**/api/chat", mock_chat)
        await page.route("**/api/file-text", hold_upload)
        response = await page.goto(base_url, wait_until="domcontentloaded")
        if response is None or response.status != 200:
            raise AssertionError("home page did not return HTTP 200")
        csp = (await response.header_value("content-security-policy")) or ""
        if "script-src 'self'" not in csp or "font-src 'self'" not in csp:
            raise AssertionError(f"unexpected CSP: {csp}")
        checks["cspHeader"] = "PASS"

        theme = await page.evaluate("() => ({ theme: document.documentElement.dataset.theme, mode: document.documentElement.dataset.mode })")
        if theme != {"theme": "linear", "mode": "dark"}:
            raise AssertionError(f"theme boot did not run before application startup: {theme}")
        checks["firstPaintTheme"] = "PASS"

        await page.keyboard.press("Escape")
        await page.locator("#workspaceAgentsTab").click()
        if await page.locator("#workspaceAgentsTab").get_attribute("aria-selected") != "true":
            raise AssertionError("workspace tab click did not update aria-selected")
        await page.locator("#workspaceAgentsTab").press("ArrowRight")
        if await page.locator("#workspaceMemoryTab").get_attribute("aria-selected") != "true":
            raise AssertionError("workspace tab keyboard navigation failed")
        checks["workspaceTabs"] = "PASS"

        await page.locator("#apiKeyInput").fill("sk-browser-smoke")
        await page.locator("#promptInput").fill("Run the browser smoke")
        await page.locator("#sendButton").wait_for(state="visible")
        await page.wait_for_function("() => !document.querySelector('#sendButton').disabled")
        await page.locator("#sendButton").click()
        await page.get_by_text("Browser smoke reply", exact=True).wait_for(timeout=10_000)
        checks["mockChat"] = "PASS"

        upload_file: FilePayload = {"name": "smoke.txt", "mimeType": "text/plain", "buffer": b"cancel me"}
        await page.locator("#fileInput").set_input_files(files=upload_file)
        cancel = page.locator("button[data-cancel-upload]").first
        await cancel.wait_for(timeout=10_000)
        await cancel.click()
        upload_release.set()
        await page.get_by_text("上传已取消", exact=False).first.wait_for(timeout=10_000)
        if await page.locator("#attachmentButton").get_attribute("aria-disabled") != "false":
            raise AssertionError("upload state did not recover after cancellation")
        checks["uploadCancel"] = "PASS"

        await page.evaluate(
            """async () => {
              await navigator.serviceWorker.ready;
              if (!navigator.serviceWorker.controller) {
                await new Promise((resolve, reject) => {
                  const timer = setTimeout(() => reject(new Error('service worker control timeout')), 10000);
                  navigator.serviceWorker.addEventListener('controllerchange', () => { clearTimeout(timer); resolve(); }, { once: true });
                });
              }
            }"""
        )
        cached_paths = await page.evaluate(
            """async () => {
              const cache = await caches.open('deepseek-infra-v401');
              return (await cache.keys()).map((request) => new URL(request.url).pathname);
            }"""
        )
        for required in ("/vaultr-brutalist.css", "/vendor/inter/inter.css", "/vendor/inter/Inter-Variable.ttf"):
            if required not in cached_paths:
                raise AssertionError(f"service worker cache is missing {required}")
        checks["completeAppShell"] = "PASS"

        await context.set_offline(True)
        await page.reload(wait_until="domcontentloaded", timeout=15_000)
        await page.evaluate("() => document.fonts.ready")
        await page.evaluate("() => document.fonts.load('16px Inter')")
        offline_style = await page.evaluate(
            """() => ({
              sheets: Array.from(document.styleSheets).map((sheet) => sheet.href || ''),
              border: getComputedStyle(document.querySelector('.vaultr-tab')).borderTopWidth,
              font: getComputedStyle(document.body).fontFamily,
              interReady: document.fonts.check('16px Inter'),
            })"""
        )
        if not any("/vaultr-brutalist.css" in href for href in offline_style["sheets"]):
            raise AssertionError(f"offline skin stylesheet missing: {offline_style}")
        if offline_style["border"] != "3px" or not offline_style["interReady"]:
            raise AssertionError(f"offline CSS/font rendering is incomplete: {offline_style}")
        checks["offlineRefresh"] = "PASS"

        csp_errors = [
            error
            for error in console_errors + page_errors
            if "content security policy" in error.lower() or "violates the following" in error.lower()
        ]
        if csp_errors:
            raise AssertionError(f"browser reported CSP errors: {csp_errors}")
        checks["noCspConsoleErrors"] = "PASS"
        await context.set_offline(False)
        await browser.close()
    return checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write JSON evidence to this path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server, port = create_server(0, host="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}/"
    try:
        wait_until_ready(base_url)
        checks = asyncio.run(run_browser(base_url))
        payload = {
            "schemaVersion": 1,
            "version": VERSION,
            "commit": git_commit(),
            "gitDirty": git_dirty(),
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "environment": {
                "os": platform.system(),
                "python": platform.python_version(),
                "ci": bool(os.getenv("CI")),
            },
            "status": "PASS",
            "browser": "chromium",
            "checks": checks,
        }
        if args.out:
            output = args.out if args.out.is_absolute() else ROOT / args.out
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
