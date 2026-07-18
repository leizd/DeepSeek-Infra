"""Run the real-browser frontend safety, React chat, and offline smoke gate."""

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

        react_stop_release = asyncio.Event()
        react_stop_requested = asyncio.Event()

        async def mock_chat(route: Any) -> None:
            try:
                request_data = route.request.post_data_json
            except (json.JSONDecodeError, TypeError):
                request_data = {}
            messages = request_data.get("messages", []) if isinstance(request_data, dict) else []
            if any(
                isinstance(message, dict) and message.get("content") == "Stop the React stream"
                for message in messages
            ):
                react_stop_requested.set()
                await react_stop_release.wait()
                try:
                    await route.abort("aborted")
                except PlaywrightError:
                    pass
                return
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
                        "version": VERSION,
                        "defaultModel": "deepseek-v4-pro",
                        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
                        "modelRoutes": {
                            "deepseek-v4-pro": "deepseek-chat",
                            "deepseek-v4-flash": "deepseek-chat",
                        },
                        "computerUrl": base_url,
                        "phoneUrl": base_url,
                        "uploadLimits": {"fileMaxBytes": 200_000_000, "requestMaxBytes": 220_000_000, "maxFiles": 8},
                    }
                ),
            )

        async def mock_title(route: Any) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"title": "React smoke chat"}),
            )

        upload_release = asyncio.Event()

        async def hold_upload(route: Any) -> None:
            await upload_release.wait()
            try:
                await route.abort("aborted")
            except PlaywrightError:
                pass

        await context.route("**/api/config", mock_config)
        await context.route("**/api/chat", mock_chat)
        await context.route("**/api/title", mock_title)
        await context.route("**/api/file-text", hold_upload)
        response = await page.goto(f"{base_url}legacy", wait_until="domcontentloaded")
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

        react_page = await context.new_page()
        react_page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        react_page.on("pageerror", lambda error: page_errors.append(str(error)))
        react_response = await react_page.goto(f"{base_url}ui/", wait_until="networkidle")
        if react_response is None or react_response.status != 200:
            raise AssertionError("React chat did not return HTTP 200")
        await react_page.locator("#reactPromptInput").wait_for()
        if await react_page.locator("#promptInput").count() != 0:
            raise AssertionError("legacy and React frontends unexpectedly share one DOM tree")
        asset_urls = await react_page.locator('script[type="module"][src]').evaluate_all(
            "elements => elements.map((element) => element.src)"
        )
        if not asset_urls or any(not url.startswith(f"{base_url}ui/assets/") for url in asset_urls):
            raise AssertionError(f"React assets are not served from the isolated /ui/ base: {asset_urls}")

        await react_page.locator("#reactPromptInput").fill("Run the React browser smoke")
        await react_page.locator("button.send-button").click()
        await react_page.get_by_text("Browser smoke reply", exact=True).last.wait_for(timeout=10_000)
        checks["reactChatVerticalSlice"] = "PASS"
        await react_page.wait_for_function(
            """() => (localStorage.getItem('deepseek-infra.conversations') || '').includes('Browser smoke reply')"""
        )
        await react_page.reload(wait_until="networkidle")
        await react_page.get_by_text("Browser smoke reply", exact=True).last.wait_for(timeout=10_000)
        checks["reactHistoryPersistence"] = "PASS"

        await react_page.locator("button.new-chat-button").click()
        await react_page.locator("#reactPromptInput").fill("Stop the React stream")
        await react_page.locator("button.send-button").click()
        await asyncio.wait_for(react_stop_requested.wait(), timeout=5)
        stop_button = react_page.locator("button.stop-button")
        await stop_button.wait_for(timeout=10_000)
        await stop_button.click()
        react_stop_release.set()
        await react_page.locator(".chat-notice").filter(has_text="已停止生成").wait_for(timeout=10_000)
        checks["reactStopGeneration"] = "PASS"

        deep_link_response = await react_page.goto(f"{base_url}ui/projects/example", wait_until="networkidle")
        if deep_link_response is None or deep_link_response.status != 200:
            raise AssertionError("React SPA deep-link fallback did not return HTTP 200")
        await react_page.locator("#reactPromptInput").wait_for()
        checks["reactPreview"] = "PASS"
        await react_page.close()

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
              const names = await caches.keys();
              const collected = [];
              for (const name of names) {
                const cache = await caches.open(name);
                collected.push(...(await cache.keys()).map((request) => new URL(request.url).pathname));
              }
              return collected;
            }"""
        )
        if not any(path.startswith("/ui/assets/") for path in cached_paths):
            raise AssertionError(f"service worker cache is missing the React shell assets: {cached_paths}")
        checks["completeAppShell"] = "PASS"

        offline_page = await context.new_page()
        offline_response = await offline_page.goto(f"{base_url}ui/", wait_until="networkidle")
        if offline_response is None or offline_response.status != 200:
            raise AssertionError("React page did not load before the offline check")
        await offline_page.locator("#reactPromptInput").wait_for()
        await context.set_offline(True)
        await offline_page.reload(wait_until="domcontentloaded", timeout=15_000)
        await offline_page.locator("#reactPromptInput").wait_for(timeout=10_000)
        offline_style = await offline_page.evaluate(
            """() => ({
              sheets: Array.from(document.styleSheets).map((sheet) => sheet.href || ''),
              bodyFont: getComputedStyle(document.body).fontFamily,
            })"""
        )
        if not any("/ui/assets/" in href for href in offline_style["sheets"]):
            raise AssertionError(f"offline React stylesheet missing from cache: {offline_style}")
        checks["offlineRefresh"] = "PASS"
        await offline_page.close()

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
