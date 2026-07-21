"""Run the real-browser frontend safety, React chat, and offline smoke gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.core.config import APP_VERSION, settings  # noqa: E402
from deepseek_infra.infra.diagnostics.evidence_revision import evidence_revision  # noqa: E402
from deepseek_infra.infra.observability.observability import finish_trace, start_span, start_trace  # noqa: E402
from deepseek_infra.web.server import create_server  # noqa: E402


VERSION = APP_VERSION


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


async def run_browser(base_url: str, trace_id: str) -> dict[str, str]:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import FilePayload
    from playwright.async_api import async_playwright

    checks: dict[str, str] = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="allow")
        if settings.auth.enabled:
            await context.add_cookies([{"name": "auth_token", "value": settings.auth.token, "url": base_url}])
        page = await context.new_page()
        console_errors: list[str] = []
        page_errors: list[str] = []
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        stop_release = asyncio.Event()
        stop_requested = asyncio.Event()

        async def mock_chat(route: Any) -> None:
            try:
                request_data = route.request.post_data_json
            except (json.JSONDecodeError, TypeError):
                request_data = {}
            messages = request_data.get("messages", []) if isinstance(request_data, dict) else []
            if any(isinstance(message, dict) and message.get("content") == "Stop the React stream" for message in messages):
                stop_requested.set()
                await stop_release.wait()
                try:
                    await route.abort("aborted")
                except PlaywrightError:
                    pass
                return
            body = "\n".join(
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
            await route.fulfill(status=200, content_type="application/json", body=json.dumps({"title": "React smoke chat"}))

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

        response = await page.goto(base_url, wait_until="networkidle")
        if response is None or response.status != 200:
            raise AssertionError("root page did not return HTTP 200")
        csp = (await response.header_value("content-security-policy")) or ""
        if "script-src 'self'" not in csp or "font-src 'self'" not in csp:
            raise AssertionError(f"unexpected CSP: {csp}")
        checks["cspHeader"] = "PASS"

        await page.locator("#reactPromptInput").wait_for()
        if await page.locator("#promptInput").count() != 0:
            raise AssertionError("legacy chat DOM is still present at the root entry")
        asset_urls = await page.locator('script[type="module"][src]').evaluate_all(
            "elements => elements.map((element) => element.src)"
        )
        if not asset_urls or any(not url.startswith(f"{base_url}ui/assets/") for url in asset_urls):
            raise AssertionError(f"React assets are not served from static/ui: {asset_urls}")
        checks["reactOnlyRoot"] = "PASS"

        await page.locator("#reactPromptInput").fill("Run the React browser smoke")
        await page.locator("button.send-button").click()
        await page.get_by_text("Browser smoke reply", exact=True).last.wait_for(timeout=10_000)
        checks["reactChatVerticalSlice"] = "PASS"

        upload_file: FilePayload = {"name": "smoke.txt", "mimeType": "text/plain", "buffer": b"cancel me"}
        await page.locator('input[type="file"]').set_input_files(files=upload_file)
        cancel = page.locator(".attachment-item.uploading button").first
        await cancel.wait_for(timeout=10_000)
        await cancel.click()
        upload_release.set()
        await page.wait_for_function("() => document.querySelectorAll('.attachment-item').length === 0")
        checks["uploadCancel"] = "PASS"

        await page.wait_for_function(
            """() => (localStorage.getItem('deepseek-infra.conversations') || '').includes('Browser smoke reply')"""
        )
        await page.reload(wait_until="networkidle")
        await page.get_by_text("Browser smoke reply", exact=True).last.wait_for(timeout=10_000)
        checks["reactHistoryPersistence"] = "PASS"

        await page.locator("button.new-chat-button").click()
        await page.locator("#reactPromptInput").fill("Stop the React stream")
        await page.locator("button.send-button").click()
        await asyncio.wait_for(stop_requested.wait(), timeout=5)
        stop_button = page.locator("button.stop-button")
        await stop_button.wait_for(timeout=10_000)
        await stop_button.click()
        stop_release.set()
        await page.locator(".chat-notice").wait_for(timeout=10_000)
        checks["reactStopGeneration"] = "PASS"

        deep_link_response = await page.goto(f"{base_url}projects/example", wait_until="networkidle")
        if deep_link_response is None or deep_link_response.status != 200:
            raise AssertionError("root React SPA deep-link fallback did not return HTTP 200")
        await page.get_by_role("heading", name="Page not found").wait_for()
        checks["rootSpaDeepLink"] = "PASS"

        deferred_trace_assets = await page.evaluate(
            r"""() => performance.getEntriesByType('resource')
              .map((entry) => entry.name)
              .filter((name) => /\/ui\/assets\/Trace(Page|DetailView)-/.test(name))"""
        )
        if deferred_trace_assets:
            raise AssertionError(f"Trace chunks loaded before Trace navigation: {deferred_trace_assets}")
        checks["traceChunkDeferred"] = "PASS"

        isolated_trace_page = await context.new_page()
        isolated_trace_api_requests: list[str] = []

        def record_trace_api_request(request: Any) -> None:
            path = urlsplit(request.url).path
            if path.startswith("/api/"):
                isolated_trace_api_requests.append(path)

        isolated_trace_page.on("request", record_trace_api_request)
        isolated_response = await isolated_trace_page.goto(f"{base_url}trace/{trace_id}", wait_until="networkidle")
        if isolated_response is None or isolated_response.status != 200:
            raise AssertionError("isolated React trace route did not return HTTP 200")
        await isolated_trace_page.get_by_role("heading", name="Browser trace smoke").wait_for()
        expected_trace_api = f"/api/traces/{trace_id}"
        unexpected_api_requests = sorted({path for path in isolated_trace_api_requests if path != expected_trace_api})
        if not isolated_trace_api_requests or unexpected_api_requests:
            raise AssertionError(
                "Trace route initialized workspace APIs: "
                f"requests={isolated_trace_api_requests}, unexpected={unexpected_api_requests}"
            )
        checks["traceRouteProviderIsolation"] = "PASS"
        await isolated_trace_page.close()

        retry_trace_id = trace_id
        retry_trace_requests = 0

        async def mock_retry_trace(route: Any) -> None:
            nonlocal retry_trace_requests
            retry_trace_requests += 1
            if retry_trace_requests == 1:
                await route.fulfill(
                    status=503,
                    content_type="application/json",
                    body=json.dumps({"error": "Trace service temporarily unavailable"}),
                )
                return
            await route.continue_()

        await context.route(f"**/api/traces/{retry_trace_id}", mock_retry_trace)
        retry_trace_page = await context.new_page()
        retry_response = await retry_trace_page.goto(f"{base_url}trace/{retry_trace_id}", wait_until="networkidle")
        if retry_response is None or retry_response.status != 200:
            raise AssertionError("retry Trace route did not return HTTP 200")
        await retry_trace_page.get_by_role("alert").wait_for()
        await retry_trace_page.get_by_role("button", name="Retry").click()
        await retry_trace_page.get_by_role("heading", name="Browser trace smoke").wait_for()
        if retry_trace_requests != 2:
            raise AssertionError(f"Trace retry issued {retry_trace_requests} API requests, expected 2")
        checks["traceRetryRecovery"] = "PASS"
        await retry_trace_page.close()
        await context.unroute(f"**/api/traces/{retry_trace_id}", mock_retry_trace)

        trace_response = await page.goto(f"{base_url}trace/{trace_id}", wait_until="networkidle")
        if trace_response is None or trace_response.status != 200:
            raise AssertionError("React trace route did not return HTTP 200")
        await page.get_by_role("heading", name="Browser trace smoke").wait_for()
        await page.get_by_role("heading", name="Waterfall").wait_for()
        loaded_trace_assets = await page.evaluate(
            r"""() => performance.getEntriesByType('resource')
              .map((entry) => entry.name)
              .filter((name) => /\/ui\/assets\/Trace(Page|DetailView)-/.test(name))"""
        )
        if not any("TracePage-" in name for name in loaded_trace_assets) or not any(
            "TraceDetailView-" in name for name in loaded_trace_assets
        ):
            raise AssertionError(f"Trace navigation did not load both route chunks: {loaded_trace_assets}")
        await page.reload(wait_until="networkidle")
        await page.get_by_role("heading", name="Browser trace smoke").wait_for()
        if await page.locator('script[src="/modules/trace_viewer.js"]').count() != 0:
            raise AssertionError("legacy Trace Viewer script is still loaded")
        checks["reactTraceRouteRefresh"] = "PASS"

        legacy_response = await context.request.get(f"{base_url}legacy")
        if legacy_response.status != 404:
            raise AssertionError(f"legacy route returned HTTP {legacy_response.status}, expected 404")
        checks["legacyRouteRetired"] = "PASS"

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
        offline_response = await offline_page.goto(base_url, wait_until="networkidle")
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


async def run_query_smoke(base_url: str) -> dict[str, str]:
    from playwright.async_api import async_playwright

    checks: dict[str, str] = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="allow")
        await context.add_init_script(
            "localStorage.setItem('deepseek-infra.active-project', 'deleted-project');"
        )

        async def mock_config(route: Any) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "hasServerKey": True,
                        "hasSearch": False,
                        "defaultModel": "deepseek-v4-pro",
                        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
                        "modelRoutes": {},
                        "uploadLimits": {"fileMaxBytes": 200_000_000, "requestMaxBytes": 220_000_000, "maxFiles": 8},
                        "computerUrl": base_url,
                        "phoneUrl": base_url,
                    }
                ),
            )

        memory_requests = 0

        async def mock_memory(route: Any) -> None:
            nonlocal memory_requests
            if route.request.method == "GET":
                memory_requests += 1
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"memories": []}))
                return
            await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))

        projects_payload = {
            "projects": [
                {"id": "p-a", "name": "项目A", "documents": [], "createdAt": 1, "updatedAt": 1},
                {"id": "p-b", "name": "项目B", "documents": [], "createdAt": 1, "updatedAt": 1},
            ]
        }

        async def mock_projects(route: Any) -> None:
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(projects_payload))

        async def mock_skills(route: Any) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "ok": True,
                        "skills": [
                            {"skillId": "s-slow", "name": "Skill A", "description": "", "version": "1.0.0", "builtin": False},
                            {"skillId": "s-fast", "name": "Skill B", "description": "", "version": "1.0.0", "builtin": False},
                        ],
                    }
                ),
            )

        binding_a_release = asyncio.Event()
        binding_b_release = asyncio.Event()
        binding_b_release.set()
        binding_patch_events: list[str] = []
        binding_patch_state: dict[str, Any] = {"enabled": []}

        async def mock_binding(route: Any) -> None:
            url = route.request.url
            if route.request.method == "PATCH":
                binding_patch_events.append("start")
                body = route.request.post_data_json or {}
                binding_patch_state["enabled"] = body.get("enabledSkills", binding_patch_state["enabled"])
                binding_patch_events.append("respond")
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": True, "skills": {"enabledSkills": binding_patch_state["enabled"], "defaultSkill": ""}}),
                )
                return
            if "/p-a/" in url:
                await binding_a_release.wait()
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": True, "skills": {"enabledSkills": ["s-slow"], "defaultSkill": ""}}),
                )
                return
            await binding_b_release.wait()
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "skills": {"enabledSkills": ["s-fast"], "defaultSkill": ""}}),
            )

        await context.route("**/api/config", mock_config)
        await context.route("**/api/memory**", mock_memory)
        await context.route("**/api/projects", mock_projects)
        await context.route("**/api/skills", mock_skills)
        await context.route("**/api/workspace/projects/**", mock_binding)

        page = await context.new_page()
        await page.goto(base_url, wait_until="domcontentloaded")

        await page.get_by_role("button", name="记忆", exact=True).click()
        await page.get_by_role("dialog", name="长期记忆").wait_for()
        await page.wait_for_timeout(200)
        if memory_requests != 1:
            raise AssertionError(f"memory drawer triggered {memory_requests} list requests, expected exactly 1")
        checks["memoryDrawerSingleRefresh"] = "PASS"

        await page.get_by_role("button", name="关闭记忆面板").click()
        if not await page.evaluate("() => localStorage.getItem('deepseek-infra.active-project') === null"):
            raise AssertionError("stale activeProjectId was not repaired after the project list loaded")
        if await page.locator(".project-chip").count() != 0:
            raise AssertionError("stale active project still renders the composer chip")
        checks["staleActiveProjectRepaired"] = "PASS"

        await page.get_by_role("button", name="项目", exact=True).click()
        await page.get_by_role("dialog", name="项目").wait_for()
        await page.locator(".workspace-open", has_text="项目A").click()
        await page.get_by_text("加载绑定中…").wait_for()
        await page.locator(".workspace-open", has_text="项目B").click()
        binding_a_release.set()
        await page.locator(".project-skill-options label", has_text="Skill B").first.wait_for()
        if await page.locator(".project-skill-options input:checked").count() != 1:
            raise AssertionError("project B binding did not render its enabled skill")
        if not await page.locator(".project-skill-options label", has_text="Skill B").locator("input").is_checked():
            raise AssertionError("late project A binding overwrote project B selection")
        checks["projectBindingLatestProjectWins"] = "PASS"

        await page.locator(".workspace-open", has_text="项目A").click()
        await page.locator(".project-skill-options label", has_text="Skill A").first.wait_for()
        before = len(binding_patch_events)
        await page.locator(".project-skill-options label", has_text="Skill A").locator("input").click()
        await page.locator(".project-skill-options label", has_text="Skill B").locator("input").click()
        for _ in range(100):
            if binding_patch_events[before:].count("respond") >= 2:
                break
            await page.wait_for_timeout(50)
        events = binding_patch_events[before:]
        if events.count("start") < 2:
            raise AssertionError(f"expected two binding saves, saw {events}")
        first_respond = events.index("respond")
        if "start" not in events[first_respond + 1 :]:
            raise AssertionError(f"second binding save started before the first completed: {events}")
        if binding_patch_state["enabled"] != ["s-slow", "s-fast"]:
            raise AssertionError(f"final binding state is not the second save: {binding_patch_state}")
        checks["projectBindingSavesSerialized"] = "PASS"

        list_calls = {"count": 0}
        slow_list_started = asyncio.Event()
        slow_list_release = asyncio.Event()

        async def mock_projects_with_delay(route: Any) -> None:
            try:
                body = route.request.post_data_json or {}
            except (json.JSONDecodeError, TypeError):
                body = {}
            action = body.get("action", "list")
            if action == "create":
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": True, "project": {"id": "p-c", "name": body.get("name", "项目C"), "documents": [], "createdAt": 1, "updatedAt": 1}}),
                )
                return
            list_calls["count"] += 1
            slow_list_started.set()
            await slow_list_release.wait()
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(projects_payload))

        await context.unroute("**/api/projects", mock_projects)
        await context.route("**/api/projects", mock_projects_with_delay)
        await page.locator(".workspace-open", has_text="项目A").wait_for()
        await page.locator(".project-create-form input").fill("项目C")
        await page.get_by_role("button", name="创建", exact=True).click()
        await slow_list_started.wait()
        await page.locator(".workspace-sync-status").wait_for(timeout=5_000)
        if not await page.locator(".workspace-open", has_text="项目A").is_visible():
            raise AssertionError("cached project list disappeared during background refresh")
        slow_list_release.set()
        await page.locator(".workspace-sync-status").wait_for(state="detached", timeout=10_000)
        checks["queryRefreshingKeepsCachedData"] = "PASS"

        fail_context = await browser.new_context(service_workers="allow")
        fail_calls = {"count": 0}

        async def mock_projects_fail(route: Any) -> None:
            fail_calls["count"] += 1
            if fail_calls["count"] <= 2:
                await route.abort("aborted")
                return
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(projects_payload))

        await fail_context.route("**/api/config", mock_config)
        await fail_context.route("**/api/projects", mock_projects_fail)
        fail_page = await fail_context.new_page()
        await fail_page.goto(base_url, wait_until="domcontentloaded")
        await fail_page.get_by_role("button", name="项目", exact=True).click()
        await fail_page.locator(".workspace-error").wait_for()
        await fail_page.get_by_role("button", name="重新同步").click()
        await fail_page.locator(".workspace-open", has_text="项目A").wait_for()
        checks["queryFailureRetryRecovery"] = "PASS"
        await fail_context.close()

        await browser.close()
    return checks


async def run_recovery_smoke(base_url: str) -> dict[str, str]:
    from playwright.async_api import async_playwright

    checks: dict[str, str] = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        async def mock_config(route: Any) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "hasServerKey": True,
                        "hasSearch": False,
                        "defaultModel": "deepseek-v4-pro",
                        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
                        "modelRoutes": {},
                        "uploadLimits": {"fileMaxBytes": 200_000_000, "requestMaxBytes": 220_000_000, "maxFiles": 8},
                        "computerUrl": base_url,
                        "phoneUrl": base_url,
                    }
                ),
            )

        projects_payload = {
            "projects": [
                {"id": "p-a", "name": "项目A", "documents": [], "createdAt": 1, "updatedAt": 1},
                {"id": "p-b", "name": "项目B", "documents": [], "createdAt": 1, "updatedAt": 1},
            ]
        }

        create_attempts = {"count": 0}

        async def mock_projects(route: Any) -> None:
            try:
                body = route.request.post_data_json or {}
            except (json.JSONDecodeError, TypeError):
                body = {}
            if body.get("action") == "create" and create_attempts["count"] == 0:
                create_attempts["count"] += 1
                await route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "create failed"}))
                return
            if body.get("action") == "create":
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": True, "project": {"id": "p-c", "name": body.get("name", "项目C"), "documents": [], "createdAt": 1, "updatedAt": 1}}),
                )
                return
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(projects_payload))

        binding_patch_calls: list[dict[str, Any]] = []
        binding_state = {"failNext": True}

        async def mock_binding(route: Any) -> None:
            if route.request.method == "PATCH":
                binding_patch_calls.append(route.request.post_data_json or {})
                if binding_state["failNext"]:
                    binding_state["failNext"] = False
                    await route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "save failed"}))
                    return
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": True, "skills": {"enabledSkills": (route.request.post_data_json or {}).get("enabledSkills", []), "defaultSkill": ""}}),
                )
                return
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "skills": {"enabledSkills": [], "defaultSkill": ""}}),
            )

        async def mock_skills(route: Any) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "ok": True,
                        "skills": [
                            {"skillId": "s1", "name": "Skill One", "description": "", "version": "1.0.0", "builtin": False},
                        ],
                    }
                ),
            )

        memory_adds: list[dict[str, Any]] = []

        async def mock_memory(route: Any) -> None:
            if route.request.method == "GET":
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"memories": [{"id": "m-old", "content": "旧记忆", "category": "fact", "scope": "global"}]}),
                )
                return
            body = route.request.post_data_json or {}
            memory_adds.append(body)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "memory": {"id": "m-new", "content": body.get("content", ""), "category": body.get("category", "fact"), "scope": body.get("scope", "global")}}),
            )

        async def mock_chat(route: Any) -> None:
            body = "\n".join(
                [
                    json.dumps({"type": "content", "text": "好的，我记住了。"}),
                    json.dumps({"type": "memory_suggestion", "content": "偏好深色主题", "category": "preference", "scope": "global"}),
                    json.dumps({"type": "done", "content": "好的，我记住了。"}),
                    "",
                ]
            )
            await route.fulfill(status=200, headers={"Content-Type": "application/x-ndjson"}, body=body)

        async def mock_title(route: Any) -> None:
            await route.fulfill(status=200, content_type="application/json", body=json.dumps({"title": "记忆"}))

        context = await browser.new_context(service_workers="allow")
        await context.route("**/api/config", mock_config)
        await context.route("**/api/projects", mock_projects)
        await context.route("**/api/skills", mock_skills)
        await context.route("**/api/workspace/projects/**", mock_binding)
        await context.route("**/api/memory**", mock_memory)
        await context.route("**/api/chat", mock_chat)
        await context.route("**/api/title", mock_title)

        page = await context.new_page()
        await page.goto(base_url, wait_until="domcontentloaded")

        await page.get_by_role("button", name="项目", exact=True).click()
        await page.get_by_role("dialog", name="项目").wait_for()
        await page.locator(".project-create-form input").fill("失败项目")
        await page.get_by_role("button", name="创建", exact=True).click()
        await page.locator(".workspace-error").wait_for()
        await page.get_by_role("button", name="重新同步").click()
        await page.locator(".workspace-error").wait_for(state="detached")
        checks["mutationErrorRecovery"] = "PASS"

        await page.locator(".workspace-open", has_text="项目A").click()
        await page.locator(".project-skill-options label", has_text="Skill One").locator("input").click()
        await page.locator(".project-skill-binding .workspace-error").wait_for()
        await page.locator(".project-skill-binding").get_by_role("button", name="重试").click()
        await page.locator(".project-skill-binding .workspace-error").wait_for(state="detached")
        if len(binding_patch_calls) != 2 or binding_patch_calls[0] != binding_patch_calls[1]:
            raise AssertionError(f"binding retry did not replay the last desired state: {binding_patch_calls}")
        checks["bindingSaveRetryRecovery"] = "PASS"

        binding_state["failNext"] = True
        await page.locator(".project-skill-options label", has_text="Skill One").locator("input").click()
        await page.locator(".project-skill-binding .workspace-error").wait_for()
        await page.locator(".workspace-open", has_text="项目B").click()
        await page.wait_for_timeout(100)
        if await page.locator(".project-skill-binding .workspace-error").count() != 0:
            raise AssertionError("project A save error leaked into project B binding view")
        checks["bindingMutationProjectIsolation"] = "PASS"

        await page.get_by_role("button", name="关闭项目面板").click()

        await page.locator("#reactPromptInput").fill("帮我记住：偏好深色主题")
        await page.locator("button.send-button").click()
        await page.locator(".memory-suggestion-toast").wait_for()
        await page.get_by_role("button", name="记忆", exact=True).click()
        await page.get_by_text("旧记忆").wait_for()
        await page.get_by_role("button", name="保存", exact=True).click()
        await page.locator(".memory-entry", has_text="偏好深色主题").wait_for()
        if not memory_adds:
            raise AssertionError("memory suggestion save never reached the backend")
        checks["memorySuggestionCacheCoherence"] = "PASS"
        await context.close()

        client_error_context = await browser.new_context(service_workers="allow")
        client_error_calls = {"count": 0}

        async def mock_projects_400(route: Any) -> None:
            client_error_calls["count"] += 1
            await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "bad request", "code": "bad_request"}))

        await client_error_context.route("**/api/config", mock_config)
        await client_error_context.route("**/api/projects", mock_projects_400)
        client_error_page = await client_error_context.new_page()
        await client_error_page.goto(base_url, wait_until="domcontentloaded")
        await client_error_page.get_by_role("button", name="项目", exact=True).click()
        await client_error_page.locator(".workspace-error").wait_for()
        await client_error_page.wait_for_timeout(300)
        if client_error_calls["count"] != 1:
            raise AssertionError(f"HTTP 400 triggered {client_error_calls['count']} requests, expected exactly 1")
        checks["clientErrorNoAutomaticRetry"] = "PASS"
        await client_error_context.close()

        transient_context = await browser.new_context(service_workers="allow")
        transient_calls = {"count": 0}

        async def mock_projects_503(route: Any) -> None:
            transient_calls["count"] += 1
            if transient_calls["count"] <= 2:
                await route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "unavailable"}))
                return
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(projects_payload))

        await transient_context.route("**/api/config", mock_config)
        await transient_context.route("**/api/projects", mock_projects_503)
        transient_page = await transient_context.new_page()
        await transient_page.goto(base_url, wait_until="domcontentloaded")
        await transient_page.get_by_role("button", name="项目", exact=True).click()
        await transient_page.locator(".workspace-error").wait_for()
        if transient_calls["count"] != 2:
            raise AssertionError(f"HTTP 503 triggered {transient_calls['count']} requests, expected exactly 2 (one retry)")
        checks["transientQueryRetry"] = "PASS"
        await transient_context.close()

        await browser.close()
    return checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write JSON evidence to this path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    trace_id = start_trace(kind="browser_smoke", title="Browser trace smoke")
    if not trace_id:
        raise RuntimeError("tracing is disabled; cannot exercise the routed Trace page")
    span = start_span(trace_id, name="browser trace render", kind="browser")
    span.finish(status="ok", usage={"total_tokens": 12}, diagnostics={"cacheHit": True})
    finish_trace(trace_id)
    server, port = create_server(0, host="127.0.0.1")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}/"
    try:
        wait_until_ready(base_url)
        checks = asyncio.run(run_browser(base_url, trace_id))
        checks.update(asyncio.run(run_query_smoke(base_url)))
        checks.update(asyncio.run(run_recovery_smoke(base_url)))
        payload = {
            "schemaVersion": 1,
            "version": VERSION,
            **evidence_revision(ROOT),
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
