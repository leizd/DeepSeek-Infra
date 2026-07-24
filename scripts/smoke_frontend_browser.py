"""Run the real-browser frontend safety, React chat, and offline smoke gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
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

        await page.locator("#reactPromptInput").fill("Browser draft survives reload")
        await page.wait_for_function(
            """() => {
              const raw = sessionStorage.getItem('deepseek:composer-draft:new:');
              return raw && JSON.parse(raw).text === 'Browser draft survives reload';
            }"""
        )
        await page.reload(wait_until="networkidle")
        await page.locator("#reactPromptInput").wait_for()
        if await page.locator("#reactPromptInput").input_value() != "Browser draft survives reload":
            raise AssertionError("Composer draft did not restore from sessionStorage")
        await page.locator("#reactPromptInput").fill("")
        await page.wait_for_function(
            "() => sessionStorage.getItem('deepseek:composer-draft:new:') === null"
        )
        checks["composerDraftRestored"] = "PASS"

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
        immutable_identity = await page.evaluate(
            """async () => {
              const pageBuildId = document.querySelector('meta[name="deepseek-infra-build-id"]')?.content || '';
              const pageRevision = document.querySelector('meta[name="deepseek-infra-source-revision"]')?.content || '';
              const pointer = await fetch('/ui/workspace-assets.json', { cache: 'no-store' }).then((response) => response.json());
              const immutableResponse = await fetch(`/ui/workspace-assets-${pageBuildId}.json`, { cache: 'no-store' });
              const immutable = await immutableResponse.json();
              const workerSource = await fetch(`/sw-${pageBuildId}.js`, { cache: 'no-store' }).then((response) => response.text());
              const workerIdentity = await new Promise((resolve, reject) => {
                const controller = navigator.serviceWorker.controller;
                if (!controller) return reject(new Error('current page has no controlling worker'));
                const channel = new MessageChannel();
                const timer = setTimeout(() => reject(new Error('worker identity handshake timeout')), 5000);
                channel.port1.onmessage = (event) => {
                  clearTimeout(timer);
                  resolve(event.data);
                };
                controller.postMessage({ type: 'get_build_identity' }, [channel.port2]);
              });
              navigator.serviceWorker.controller.postMessage({
                type: 'cache_workspace_primary',
                buildId: 'ffffffffffffffff',
                assetSetDigest: 'f'.repeat(64),
              });
              await new Promise((resolve) => setTimeout(resolve, 50));
              const cacheNames = await caches.keys();
              return {
                pageBuildId,
                pageRevision,
                pointer,
                immutable,
                workerSource,
                workerIdentity,
                wrongBuildCacheCreated: cacheNames.includes('deepseek-react-root-ffffffffffffffff'),
              };
            }"""
        )
        identity = immutable_identity
        if (
            not re.fullmatch(r"[0-9a-f]{16}", identity["pageBuildId"])
            or identity["pointer"] != identity["immutable"]
            or identity["pointer"]["buildId"] != identity["pageBuildId"]
            or identity["pointer"]["sourceRevision"] != identity["pageRevision"]
            or identity["workerIdentity"]["buildId"] != identity["pageBuildId"]
            or identity["workerIdentity"]["assetSetDigest"] != identity["pointer"]["assetSetDigest"]
            or not identity["workerIdentity"]["cacheReady"]
        ):
            raise AssertionError(f"page, worker and immutable manifest identities diverged: {identity}")
        if (
            f'const WORKER_BUILD_ID = "{identity["pageBuildId"]}"' not in identity["workerSource"]
            or f'/ui/workspace-assets-{identity["pageBuildId"]}.json' not in identity["workerSource"]
        ):
            raise AssertionError("build-scoped root worker does not embed its immutable identity")
        if identity["wrongBuildCacheCreated"]:
            raise AssertionError("worker accepted a warmup request for the wrong page build")
        checks["immutableWorkerBuildIdentity"] = "PASS"
        checks["workerManifestIdentityBound"] = "PASS"
        checks["controllerHandshakeRequired"] = "PASS"
        checks["wrongWorkerWarmupRejected"] = "PASS"

        pointer_response = await context.request.get(f"{base_url}ui/workspace-assets.json")
        immutable_response = await context.request.get(
            f"{base_url}ui/workspace-assets-{identity['pageBuildId']}.json"
        )
        worker_response = await context.request.get(f"{base_url}sw-{identity['pageBuildId']}.js")
        core_response = await context.request.get(f"{base_url}{identity['pointer']['core'][0].removeprefix('/')}")
        cache_headers = {
            "index": (await response.header_value("cache-control")) or "",
            "pointer": pointer_response.headers.get("cache-control", ""),
            "manifest": immutable_response.headers.get("cache-control", ""),
            "worker": worker_response.headers.get("cache-control", ""),
            "core": core_response.headers.get("cache-control", ""),
        }
        immutable_cache = "public, max-age=31536000, immutable"
        if (
            cache_headers["index"] != "no-store"
            or cache_headers["pointer"] != "no-store"
            or any(cache_headers[name] != immutable_cache for name in ("manifest", "worker", "core"))
        ):
            raise AssertionError(f"frontend cache policy diverged: {cache_headers}")
        checks["cacheControlContracts"] = "PASS"

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
        await page.wait_for_function(
            r"""async () => {
              const names = await caches.keys();
              const paths = [];
              for (const name of names) {
                const cache = await caches.open(name);
                paths.push(...(await cache.keys()).map((request) => new URL(request.url).pathname));
              }
              return paths.some((path) => /\/ui\/assets\/SkillsFeature-/.test(path))
                && paths.some((path) => /\/ui\/assets\/ProjectsFeature-/.test(path));
            }""",
            timeout=15_000,
        )
        cached_recovery = await page.evaluate(
            """async () => {
              const manifest = await fetch('/ui/workspace-assets.json').then((response) => response.json());
              const cached = new Set();
              for (const name of await caches.keys()) {
                const cache = await caches.open(name);
                for (const request of await cache.keys()) cached.add(new URL(request.url).pathname);
              }
              return manifest.recovery.filter((path) => cached.has(path));
            }"""
        )
        if cached_recovery:
            raise AssertionError(f"recovery chunks entered normal Workspace warmup: {cached_recovery}")
        checks["recoveryChunksDeferred"] = "PASS"

        build_cache = await page.evaluate(
            """async () => {
              const manifest = await fetch('/ui/workspace-assets.json').then((response) => response.json());
              const currentId = manifest.buildId;
              const currentName = `deepseek-react-root-${currentId}`;
              const previousId = '0000000000000000';
              const previousName = `deepseek-react-root-${previousId}`;
              return { currentId, currentName, previousId, previousName };
            }"""
        )

        offline_page = await context.new_page()
        offline_response = await offline_page.goto(base_url, wait_until="networkidle")
        if offline_response is None or offline_response.status != 200:
            raise AssertionError("React page did not load before the offline check")
        await offline_page.locator("#reactPromptInput").wait_for()
        await offline_page.wait_for_timeout(200)
        await offline_page.evaluate(
            """async ({ currentId }) => {
              const previousId = '0000000000000000';
              const previous = await caches.open(`deepseek-react-root-${previousId}`);
              await previous.put(
                '/ui/assets/LegacyChunk-abcdefgh.js',
                new Response('export const legacy = true', { headers: { 'content-type': 'application/javascript' } }),
              );
              const history = await caches.open('deepseek-workspace-root-build-history');
              await history.put(
                '/__deepseek_workspace_metadata__/builds',
                new Response(JSON.stringify([currentId, previousId])),
              );
            }""",
            {"currentId": build_cache["currentId"]},
        )
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
        offline_build = await offline_page.evaluate(
            """async () => {
              const buildId = document.querySelector('meta[name="deepseek-infra-build-id"]')?.content || '';
              const metadata = await caches.open('deepseek-workspace-root-build-history');
              const historyResponse = await metadata.match('/__deepseek_workspace_metadata__/builds');
              const history = historyResponse ? await historyResponse.json() : [];
              const cacheNames = await caches.keys();
              const previous = await caches.open('deepseek-react-root-0000000000000000');
              const previousPaths = (await previous.keys()).map((request) => new URL(request.url).pathname);
              const legacy = await fetch('/ui/assets/LegacyChunk-abcdefgh.js').then((response) => response.text());
              const searched = await fetch('/ui/assets/LegacyChunk-abcdefgh.js?wrong-build=1')
                .then(
                  async (response) => ({ status: response.status, text: await response.text() }),
                  () => ({ status: 0, text: '' }),
                );
              return { buildId, history, cacheNames, previousPaths, legacy, searched };
            }"""
        )
        if offline_build["buildId"] != build_cache["currentId"]:
            raise AssertionError(f"offline metadata came from the wrong build: {offline_build}")
        if offline_build["legacy"] != "export const legacy = true":
            raise AssertionError(f"previous build hash chunk was unavailable: {offline_build}")
        if offline_build["searched"]["text"] == "export const legacy = true":
            raise AssertionError(f"query-insensitive cache match crossed builds: {offline_build}")
        checks["currentBuildShellWinsOffline"] = "PASS"
        checks["previousBuildChunkStillAvailable"] = "PASS"
        await offline_page.get_by_role("button", name="技能", exact=True).click()
        await offline_page.get_by_role("heading", name="技能", exact=True).wait_for(timeout=10_000)
        checks["offlineUnopenedFeatureAvailable"] = "PASS"
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

        await context.add_init_script(
            """() => {
              Object.defineProperty(navigator, 'connection', {
                configurable: true,
                value: { effectiveType: '4g', saveData: true },
              });
            }"""
        )
        warmup_peer = await context.new_page()
        await warmup_peer.goto(base_url, wait_until="networkidle")
        await warmup_peer.evaluate("() => navigator.serviceWorker.ready")
        warmup_state = await page.evaluate(
            """async () => {
              const manifest = await fetch('/ui/workspace-assets.json', { cache: 'no-store' }).then((response) => response.json());
              const cache = await caches.open(`deepseek-react-root-${manifest.buildId}`);
              const targets = manifest.offlinePrimary.slice(0, 2);
              for (const target of targets) await cache.delete(target);
              const metadata = await caches.open('deepseek-workspace-root-build-history');
              await metadata.delete(`/__deepseek_workspace_metadata__/${encodeURIComponent(`warmup:${manifest.buildId}`)}`);
              return { buildId: manifest.buildId, assetSetDigest: manifest.assetSetDigest, targets };
            }"""
        )
        warmup_message = {
            "type": "cache_workspace_primary",
            "buildId": warmup_state["buildId"],
            "assetSetDigest": warmup_state["assetSetDigest"],
        }
        await asyncio.gather(
            page.evaluate(
                "(message) => navigator.serviceWorker.controller.postMessage(message)",
                warmup_message,
            ),
            warmup_peer.evaluate(
                "(message) => navigator.serviceWorker.controller.postMessage(message)",
                warmup_message,
            ),
        )
        await page.wait_for_function(
            """async ({ buildId, assetSetDigest, targets }) => {
              const metadata = await caches.open('deepseek-workspace-root-build-history');
              const marker = await metadata.match(
                `/__deepseek_workspace_metadata__/${encodeURIComponent(`warmup:${buildId}`)}`,
              );
              const state = marker ? await marker.json() : {};
              const cache = await caches.open(`deepseek-react-root-${buildId}`);
              return state.assetSetDigest === assetSetDigest
                && state.offlinePrimaryComplete === true
                && (await Promise.all(targets.map((target) => cache.match(target)))).every(Boolean);
            }""",
            arg=warmup_state,
            timeout=15_000,
        )
        checks["warmupDeduplicatedAcrossTabs"] = "PASS"

        await page.evaluate(
            """async ({ buildId, targets }) => {
              const cache = await caches.open(`deepseek-react-root-${buildId}`);
              await cache.delete(targets[0]);
              const retained = (await cache.match(targets[1])) || (await fetch(targets[1], { cache: 'no-store' }));
              const headers = new Headers(retained.headers);
              headers.set('x-deepseek-smoke-retained', 'true');
              await cache.put(targets[1], new Response(await retained.blob(), {
                status: retained.status,
                statusText: retained.statusText,
                headers,
              }));
              const metadata = await caches.open('deepseek-workspace-root-build-history');
              await metadata.delete(`/__deepseek_workspace_metadata__/${encodeURIComponent(`warmup:${buildId}`)}`);
            }""",
            warmup_state,
        )
        await page.evaluate(
            "(message) => navigator.serviceWorker.controller.postMessage(message)",
            warmup_message,
        )
        await page.wait_for_function(
            """async ({ buildId, targets }) => {
              const cache = await caches.open(`deepseek-react-root-${buildId}`);
              const metadata = await caches.open('deepseek-workspace-root-build-history');
              const retained = await cache.match(targets[1]);
              return Boolean(await cache.match(targets[0]))
                && retained?.headers.get('x-deepseek-smoke-retained') === 'true'
                && Boolean(await metadata.match(
                  `/__deepseek_workspace_metadata__/${encodeURIComponent(`warmup:${buildId}`)}`,
                ));
            }""",
            arg=warmup_state,
            timeout=15_000,
        )
        checks["warmupResumesMissingAssets"] = "PASS"
        await warmup_peer.close()

        lease_page = await context.new_page()
        await lease_page.goto(base_url, wait_until="networkidle")
        await lease_page.evaluate("() => navigator.serviceWorker.ready")
        lease_state = await lease_page.evaluate(
            """async () => {
              const currentId = document.querySelector('meta[name="deepseek-infra-build-id"]')?.content || '';
              const buildB = '1111111111111111';
              const buildA = '2222222222222222';
              const chunk = '/ui/assets/LeasedFeature-abcdefgh.js';
              const cacheA = await caches.open(`deepseek-react-root-${buildA}`);
              await cacheA.put(chunk, new Response('leased-a-chunk', {
                headers: { 'content-type': 'application/javascript' },
              }));
              await caches.open(`deepseek-react-root-${buildB}`);
              const metadata = await caches.open('deepseek-workspace-root-build-history');
              await metadata.put(
                '/__deepseek_workspace_metadata__/builds',
                new Response(JSON.stringify([currentId, buildB, buildA])),
              );
              navigator.serviceWorker.controller.postMessage({ type: 'report_build_lease', buildId: buildA });
              return { currentId, buildB, buildA, chunk };
            }"""
        )
        await lease_page.wait_for_timeout(200)
        leased_chunk = await lease_page.evaluate(
            """async ({ chunk }) => fetch(chunk).then((response) => response.text())""",
            lease_state,
        )
        if leased_chunk != "leased-a-chunk":
            raise AssertionError("active build A lease did not preserve its lazy chunk through build C")
        checks["activeClientCacheLeaseRetained"] = "PASS"
        await lease_page.close()
        await page.wait_for_timeout(1_000)

        await page.evaluate(
            """async ({ currentId, buildA }) => {
              const metadata = await caches.open('deepseek-workspace-root-build-history');
              const response = await metadata.match('/__deepseek_workspace_metadata__/leases');
              const leases = response ? await response.json() : {};
              for (const lease of Object.values(leases)) {
                if (lease.buildId === buildA) lease.lastSeenAt = 0;
              }
              await metadata.put('/__deepseek_workspace_metadata__/leases', new Response(JSON.stringify(leases)));
              navigator.serviceWorker.controller.postMessage({ type: 'report_build_lease', buildId: currentId });
            }""",
            lease_state,
        )
        remaining_cache_state = await page.evaluate(
            """async ({ buildA }) => {
              const deadline = Date.now() + 10_000;
              let stableSince = 0;
              let state = {};
              while (Date.now() < deadline) {
                const metadata = await caches.open('deepseek-workspace-root-build-history');
                const historyResponse = await metadata.match('/__deepseek_workspace_metadata__/builds');
                const leaseResponse = await metadata.match('/__deepseek_workspace_metadata__/leases');
                state = {
                  caches: (await caches.keys()).filter((name) => name.startsWith('deepseek-react-root-')),
                  history: historyResponse ? await historyResponse.json() : [],
                  leases: leaseResponse ? await leaseResponse.json() : {},
                };
                const expiredBuildAbsent = !state.caches.includes(`deepseek-react-root-${buildA}`)
                  && !state.history.includes(buildA)
                  && !Object.values(state.leases).some((lease) => lease.buildId === buildA);
                const stable = expiredBuildAbsent && state.caches.length <= 2;
                if (stable) {
                  stableSince ||= Date.now();
                  if (Date.now() - stableSince >= 500) return state;
                } else {
                  stableSince = 0;
                }
                await new Promise((resolve) => setTimeout(resolve, 50));
              }
              throw new Error(`expired cache lease did not stabilize: ${JSON.stringify(state)}`);
            }""",
            lease_state,
        )
        if len(remaining_cache_state["caches"]) > 2:
            raise AssertionError(f"unleased build caches exceeded current plus previous: {remaining_cache_state}")
        checks["expiredClientCacheLeasePruned"] = "PASS"

        for effective_type, check_name, save_data in (
            ("4g", "optionalWarmRespectsSaveData", True),
            ("2g", "optionalWarmRespects2G", False),
        ):
            constrained = await browser.new_context(service_workers="allow")
            await constrained.add_init_script(
                """() => {
                  Object.defineProperty(navigator, 'connection', {
                    configurable: true,
                    value: {
                      effectiveType: __EFFECTIVE_TYPE__,
                      saveData: __SAVE_DATA__,
                    },
                  });
                  window.__workspaceIdleRequested = false;
                  window.requestIdleCallback = (callback) => {
                    window.__workspaceIdleRequested = true;
                    callback();
                    return 1;
                  };
                }"""
                .replace("__EFFECTIVE_TYPE__", json.dumps(effective_type))
                .replace("__SAVE_DATA__", json.dumps(save_data)),
            )
            constrained_page = await constrained.new_page()
            await constrained_page.goto(base_url, wait_until="load")
            await constrained_page.evaluate("() => navigator.serviceWorker.ready")
            await constrained_page.wait_for_timeout(100)
            idle_requested = await constrained_page.evaluate("() => window.__workspaceIdleRequested")
            if idle_requested:
                raise AssertionError(f"Workspace warmup scheduled on constrained connection {effective_type}")
            checks[check_name] = "PASS"
            await constrained.close()

        await page.goto(base_url, wait_until="networkidle")
        await page.locator("#reactPromptInput").wait_for()
        update_peer = await context.new_page()
        await update_peer.goto(base_url, wait_until="networkidle")
        await update_peer.locator("#reactPromptInput").wait_for()
        await update_peer.evaluate("() => { window.__updatePeerMarker = 'alive'; }")

        current_build = identity["pageBuildId"]
        current_digest = identity["pointer"]["assetSetDigest"]
        targets = {
            "bbbbbbbbbbbbbbbb": "b" * 64,
            "cccccccccccccccc": "c" * 64,
        }
        target_manifests = {
            build_id: {
                **identity["pointer"],
                "version": f"{VERSION}-smoke-{build_id[0]}",
                "sourceRevision": f"browser-smoke-{build_id[0]}",
                "buildId": build_id,
                "assetSetDigest": digest,
            }
            for build_id, digest in targets.items()
        }
        target_workers = {
            build_id: identity["workerSource"]
            .replace(current_build, build_id)
            .replace(current_digest, digest)
            for build_id, digest in targets.items()
        }
        deployed_target = {"buildId": "bbbbbbbbbbbbbbbb"}

        async def mock_deployed_build(route: Any) -> None:
            manifest = target_manifests[deployed_target["buildId"]]
            await route.fulfill(
                status=200,
                headers={"Content-Type": "application/json", "Cache-Control": "no-store"},
                body=json.dumps(manifest),
            )

        async def mock_update_worker(route: Any) -> None:
            path = urlsplit(route.request.url).path
            match = re.fullmatch(r"/sw-([0-9a-f]{16})\.js", path)
            build_id = match.group(1) if match else ""
            source = target_workers.get(build_id)
            if source is None:
                await route.fallback()
                return
            await route.fulfill(
                status=200,
                headers={
                    "Content-Type": "application/javascript",
                    "Cache-Control": "public, max-age=31536000, immutable",
                    "Service-Worker-Allowed": "/",
                },
                body=source,
            )

        async def mock_update_manifest(route: Any) -> None:
            path = urlsplit(route.request.url).path
            match = re.fullmatch(r"/ui/workspace-assets-([0-9a-f]{16})\.json", path)
            build_id = match.group(1) if match else ""
            manifest = target_manifests.get(build_id)
            if manifest is None:
                await route.fallback()
                return
            await route.fulfill(
                status=200,
                headers={
                    "Content-Type": "application/json",
                    "Cache-Control": "public, max-age=31536000, immutable",
                },
                body=json.dumps(manifest),
            )

        await context.route("**/ui/workspace-assets.json", mock_deployed_build)
        await context.route(re.compile(r".*/sw-[0-9a-f]{16}\.js$"), mock_update_worker)
        await context.route(
            re.compile(r".*/ui/workspace-assets-[0-9a-f]{16}\.json$"),
            mock_update_manifest,
        )

        await page.get_by_role("button", name="检查更新").click()
        await page.get_by_text("bbbbbbbbbbbbbbbb", exact=False).wait_for(timeout=15_000)
        await page.wait_for_function(
            """() => {
              const banner = document.querySelector('.build-update-banner');
              const button = Array.from(banner?.querySelectorAll('button') || [])
                .find((candidate) => candidate.textContent?.includes('更新并重新加载'));
              return Boolean(banner?.textContent?.includes('bbbbbbbbbbbbbbbb') && button && !button.disabled);
            }""",
            timeout=15_000,
        )
        try:
            await update_peer.get_by_text("bbbbbbbbbbbbbbbb", exact=False).wait_for(timeout=15_000)
        except Exception as error:
            peer_state = await update_peer.evaluate(
                """async () => ({
                  text: document.querySelector('.build-update-banner')?.textContent || '',
                  controller: navigator.serviceWorker.controller?.scriptURL || '',
                  visibility: document.visibilityState,
                  registrations: (await navigator.serviceWorker.getRegistrations()).map((registration) => ({
                    scope: registration.scope,
                    active: registration.active?.scriptURL || '',
                    waiting: registration.waiting?.scriptURL || '',
                    installing: registration.installing?.scriptURL || '',
                  })),
                })"""
            )
            raise AssertionError(f"peer did not receive staged build B: {peer_state}") from error

        async def controller_identity(target_page: Any) -> dict[str, Any]:
            return await target_page.evaluate(
                """async () => new Promise((resolve, reject) => {
                  const controller = navigator.serviceWorker.controller;
                  if (!controller) return reject(new Error('missing controller'));
                  const channel = new MessageChannel();
                  const timer = setTimeout(() => reject(new Error('identity timeout')), 5000);
                  channel.port1.onmessage = (event) => {
                    clearTimeout(timer);
                    resolve(event.data);
                  };
                  controller.postMessage({ type: 'get_build_identity' }, [channel.port2]);
                })"""
            )

        before_consent = await controller_identity(page)
        if before_consent["buildId"] != current_build:
            raise AssertionError(f"staged build activated without consent: {before_consent}")
        staged_b = await page.evaluate(
            """async () => {
              const registration = await navigator.serviceWorker.getRegistration('/');
              return {
                controller: navigator.serviceWorker.controller?.scriptURL || '',
                active: registration?.active?.scriptURL || '',
                waiting: registration?.waiting?.scriptURL || '',
                installing: registration?.installing?.scriptURL || '',
              };
            }"""
        )
        if not staged_b["waiting"].endswith("/sw-bbbbbbbbbbbbbbbb.js"):
            raise AssertionError(f"build B did not remain waiting before consent: {staged_b!r}")
        checks["stableBuildDiscovery"] = "PASS"
        checks["updateConsentRequired"] = "PASS"

        deployed_target["buildId"] = "cccccccccccccccc"
        await page.get_by_role("button", name="检查更新").click()
        await page.get_by_text("cccccccccccccccc", exact=False).wait_for(timeout=15_000)
        try:
            await page.wait_for_function(
                """() => {
                  const banner = document.querySelector('.build-update-banner');
                  const button = Array.from(banner?.querySelectorAll('button') || [])
                    .find((candidate) => candidate.textContent?.includes('更新并重新加载'));
                  return Boolean(banner?.textContent?.includes('cccccccccccccccc') && button && !button.disabled);
                }""",
                timeout=15_000,
            )
        except Exception as error:
            staged_state = await page.evaluate(
                """async () => ({
                  text: document.querySelector('.build-update-banner')?.textContent || '',
                  frames: Array.from(document.querySelectorAll('iframe')).map((frame) => ({
                    src: frame.src,
                    controller: (() => {
                      try {
                        return frame.contentWindow?.navigator.serviceWorker?.controller?.scriptURL || '';
                      } catch (error) {
                        return String(error);
                      }
                    })(),
                  })),
                  registrations: (await navigator.serviceWorker.getRegistrations()).map((registration) => ({
                    scope: registration.scope,
                    active: registration.active?.scriptURL || '',
                    waiting: registration.waiting?.scriptURL || '',
                    installing: registration.installing?.scriptURL || '',
                  })),
                })"""
            )
            raise AssertionError(f"newer target C was not ready: {staged_state}") from error
        await update_peer.get_by_text("cccccccccccccccc", exact=False).wait_for(timeout=15_000)
        superseded_state = await page.evaluate(
            """async () => {
              const root = await navigator.serviceWorker.getRegistration('/');
              return {
                controller: navigator.serviceWorker.controller?.scriptURL || '',
                rootWaiting: root?.waiting?.scriptURL || '',
              };
            }"""
        )
        if (
            not superseded_state["controller"].endswith(f"/sw-{current_build}.js")
            or not superseded_state["rootWaiting"].endswith("/sw-cccccccccccccccc.js")
        ):
            raise AssertionError(f"newer target did not supersede the staged build: {superseded_state}")
        checks["supersededBuildRejected"] = "PASS"

        stop_requested.clear()
        stop_release.clear()
        await page.locator("button.new-chat-button").click()
        await page.locator("#reactPromptInput").fill("Stop the React stream")
        await page.locator("button.send-button").click()
        await asyncio.wait_for(stop_requested.wait(), timeout=5)
        await page.get_by_text("正在生成回复", exact=False).wait_for(timeout=10_000)
        await page.get_by_role("button", name="完成后更新").click()
        await page.wait_for_timeout(200)
        blocked_identity = await controller_identity(page)
        if blocked_identity["buildId"] != current_build:
            raise AssertionError(f"reload blocker allowed early activation: {blocked_identity}")
        checks["reloadBlockerPreventsActivation"] = "PASS"

        main_navigations: list[str] = []
        page.on(
            "framenavigated",
            lambda frame: main_navigations.append(frame.url) if frame == page.main_frame else None,
        )
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=20_000):
            await page.locator("button.stop-button").click()
            stop_release.set()
        await page.locator("#reactPromptInput").wait_for(timeout=10_000)
        activated_identity = await controller_identity(page)
        if activated_identity["buildId"] != "cccccccccccccccc" or not activated_identity["cacheReady"]:
            raise AssertionError(f"reload happened without the verified target controller: {activated_identity}")
        if len(main_navigations) != 1:
            raise AssertionError(f"update activation reloaded the initiating tab {len(main_navigations)} times")
        checks["controllerVerifiedBeforeReload"] = "PASS"

        await update_peer.wait_for_timeout(500)
        peer_marker = await update_peer.evaluate("() => window.__updatePeerMarker")
        peer_identity = await controller_identity(update_peer)
        if peer_marker != "alive" or peer_identity["buildId"] != "cccccccccccccccc":
            raise AssertionError(
                f"peer tab was reloaded or missed controller handoff: marker={peer_marker!r}, identity={peer_identity}"
            )
        await update_peer.get_by_text("cccccccccccccccc", exact=False).wait_for(timeout=10_000)
        checks["crossTabReloadNotForced"] = "PASS"
        await update_peer.close()
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
        saved_memories: list[dict[str, Any]] = []

        async def mock_memory(route: Any) -> None:
            if route.request.method == "GET":
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "memories": [
                                {"id": "m-old", "content": "旧记忆", "category": "fact", "scope": "global"},
                                *saved_memories,
                            ]
                        }
                    ),
                )
                return
            body = route.request.post_data_json or {}
            memory_adds.append(body)
            saved = {"id": f"m-new-{len(saved_memories) + 1}", "content": body.get("content", ""), "category": body.get("category", "fact"), "scope": body.get("scope", "global")}
            saved_memories.append(saved)
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"ok": True, "memory": saved}),
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


async def run_mutation_smoke(base_url: str) -> dict[str, str]:
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import expect
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

        async def tracked_page(context: Any, page_errors: list[str]) -> Any:
            await context.add_init_script(
                """
                window.__mutationUnhandledRejections = [];
                window.addEventListener('unhandledrejection', (event) => {
                  window.__mutationUnhandledRejections.push(String(event.reason));
                  event.preventDefault();
                });
                """
            )
            page = await context.new_page()
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            return page

        projects_state = [
            {"id": "p-a", "name": "项目A", "documents": [], "createdAt": 1, "updatedAt": 1},
            {"id": "p-b", "name": "项目B", "documents": [], "createdAt": 1, "updatedAt": 1},
            {"id": "p-confirm", "name": "确认项目", "documents": [], "createdAt": 1, "updatedAt": 1},
        ]
        project_delete_started = {"p-a": asyncio.Event(), "p-b": asyncio.Event()}
        project_delete_release = {"p-a": asyncio.Event(), "p-b": asyncio.Event()}
        project_delete_calls: dict[str, int] = {}
        project_control = {"failCreate": True, "failRename": True, "failNextList": False}

        async def mock_projects(route: Any) -> None:
            try:
                body = route.request.post_data_json or {}
            except (json.JSONDecodeError, TypeError):
                body = {}
            action = body.get("action", "list")
            if action == "list":
                if project_control["failNextList"]:
                    project_control["failNextList"] = False
                    await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "list failed"}))
                    return
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"projects": projects_state}))
                return
            if action == "create" and project_control["failCreate"]:
                project_control["failCreate"] = False
                await route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "create failed"}))
                return
            if action == "rename" and project_control["failRename"]:
                project_control["failRename"] = False
                await route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "rename failed"}))
                return
            if action == "delete":
                project_id = str(body.get("id", ""))
                project_delete_calls[project_id] = project_delete_calls.get(project_id, 0) + 1
                if project_id in project_delete_started:
                    project_delete_started[project_id].set()
                    await project_delete_release[project_id].wait()
                projects_state[:] = [project for project in projects_state if project["id"] != project_id]
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                return
            await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "unexpected project action"}))

        skills_state = [
            {"skillId": "s-a", "name": "Skill A", "description": "", "version": "1.0.0", "builtin": False, "disabled": False},
            {"skillId": "s-b", "name": "Skill B", "description": "", "version": "1.0.0", "builtin": False, "disabled": False},
        ]
        skill_toggle_started = {"s-a": asyncio.Event(), "s-b": asyncio.Event()}
        skill_toggle_release = {"s-a": asyncio.Event(), "s-b": asyncio.Event()}
        skill_toggle_calls: dict[str, int] = {}

        async def mock_skills(route: Any) -> None:
            try:
                body = route.request.post_data_json or {}
            except (json.JSONDecodeError, TypeError):
                body = {}
            action = body.get("action", "list")
            if action == "list":
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "skills": skills_state}))
                return
            if action in {"disable", "enable"}:
                skill_id = str(body.get("skillId", ""))
                skill_toggle_calls[skill_id] = skill_toggle_calls.get(skill_id, 0) + 1
                skill_toggle_started[skill_id].set()
                await skill_toggle_release[skill_id].wait()
                for skill in skills_state:
                    if skill["skillId"] == skill_id:
                        skill["disabled"] = action == "disable"
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                return
            await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "unexpected skill action"}))

        memories_state = [
            {"id": "m-a", "content": "记忆A", "category": "fact", "scope": "global"},
            {"id": "m-b", "content": "记忆B", "category": "fact", "scope": "global"},
        ]
        memory_delete_started = {"m-a": asyncio.Event(), "m-b": asyncio.Event()}
        memory_delete_release = {"m-a": asyncio.Event(), "m-b": asyncio.Event()}

        async def mock_memory(route: Any) -> None:
            if route.request.method == "GET":
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"memories": memories_state}))
                return
            body = route.request.post_data_json or {}
            if body.get("action") == "deleteById":
                memory_id = str(body.get("id", ""))
                memory_delete_started[memory_id].set()
                await memory_delete_release[memory_id].wait()
                memories_state[:] = [memory for memory in memories_state if memory["id"] != memory_id]
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                return
            await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "unexpected memory action"}))

        context = await browser.new_context(service_workers="allow")
        page_errors: list[str] = []
        await context.route("**/api/config", mock_config)
        await context.route("**/api/projects", mock_projects)
        await context.route("**/api/skills", mock_skills)
        await context.route("**/api/memory**", mock_memory)
        page = await tracked_page(context, page_errors)
        await page.goto(base_url, wait_until="domcontentloaded")

        await page.get_by_role("button", name="项目", exact=True).click()
        await page.locator(".project-create-form input").fill("失败项目")
        await page.get_by_role("button", name="创建", exact=True).click()
        await page.locator(".workspace-error").wait_for()
        if await page.locator(".project-create-form input").input_value() != "失败项目":
            raise AssertionError("failed project creation cleared its draft")

        project_control["failNextList"] = True
        await page.get_by_role("button", name="重新同步").click()
        await page.wait_for_timeout(100)
        await page.get_by_role("button", name="重新同步").click()
        await page.locator(".workspace-error").wait_for(state="detached")
        unhandled = await page.evaluate("() => window.__mutationUnhandledRejections")
        if unhandled or page_errors:
            raise AssertionError(f"mutation or recovery rejection escaped the UI: {unhandled + page_errors}")
        checks["workspaceMutationRejectionContained"] = "PASS"

        await page.get_by_role("button", name="重命名项目 项目A").click()
        rename_input = page.get_by_role("textbox", name="重命名项目")
        await rename_input.fill("失败重命名")
        await rename_input.press("Enter")
        await page.locator(".workspace-error").wait_for()
        if not await rename_input.is_visible() or await rename_input.input_value() != "失败重命名":
            raise AssertionError("failed project rename closed the editor or lost its draft")
        checks["failedRenameDraftPreserved"] = "PASS"
        await rename_input.press("Escape")

        confirm_button = page.get_by_role("button", name="删除项目 确认项目")
        dialog_waiter = asyncio.create_task(page.wait_for_event("dialog"))
        confirm_click = asyncio.create_task(confirm_button.click())
        dialog = await dialog_waiter
        if dialog.message != "确定删除项目“确认项目”？":
            raise AssertionError(f"unexpected project confirmation text: {dialog.message}")
        await dialog.dismiss()
        await confirm_click
        await page.wait_for_timeout(50)
        if project_delete_calls.get("p-confirm", 0) != 0:
            raise AssertionError("dismissed project deletion reached the backend")
        checks["destructiveMutationConfirmation"] = "PASS"

        for project_id, project_name in (("p-a", "项目A"), ("p-b", "项目B")):
            page.once("dialog", lambda pending_dialog: asyncio.create_task(pending_dialog.accept()))
            await page.get_by_role("button", name=f"删除项目 {project_name}").click()
            await asyncio.wait_for(project_delete_started[project_id].wait(), timeout=5)
        project_a_delete = page.get_by_role("button", name="删除项目 项目A")
        project_b_delete = page.get_by_role("button", name="删除项目 项目B")
        await expect(project_a_delete).to_be_disabled(timeout=3000)
        await expect(project_b_delete).to_be_disabled(timeout=3000)
        project_delete_release["p-a"].set()
        await project_a_delete.wait_for(state="detached")
        if not await project_b_delete.is_disabled():
            raise AssertionError("second project delete button re-enabled while still pending")
        project_delete_release["p-b"].set()
        await project_b_delete.wait_for(state="detached")
        checks["concurrentProjectPendingTracked"] = "PASS"

        await page.get_by_role("button", name="关闭项目面板").click()
        await page.get_by_role("button", name="技能", exact=True).click()
        skill_a = page.locator(".skill-card", has_text="Skill A")
        skill_b = page.locator(".skill-card", has_text="Skill B")
        await skill_a.get_by_role("button", name="禁用").evaluate("button => { button.click(); button.click(); }")
        await asyncio.wait_for(skill_toggle_started["s-a"].wait(), timeout=5)
        await skill_b.get_by_role("button", name="禁用").click()
        await asyncio.wait_for(skill_toggle_started["s-b"].wait(), timeout=5)
        if skill_toggle_calls.get("s-a") != 1:
            raise AssertionError(f"duplicate skill toggle sent {skill_toggle_calls.get('s-a', 0)} requests")
        checks["duplicateMutationSuppressed"] = "PASS"
        await expect(skill_a.get_by_role("button", name="…")).to_be_disabled(timeout=3000)
        await expect(skill_b.get_by_role("button", name="…")).to_be_disabled(timeout=3000)
        skill_toggle_release["s-a"].set()
        await skill_a.get_by_role("button", name="启用").wait_for()
        if not await skill_b.get_by_role("button", name="…").is_disabled():
            raise AssertionError("second skill toggle button re-enabled while still pending")
        skill_toggle_release["s-b"].set()
        await skill_b.get_by_role("button", name="启用").wait_for()
        checks["concurrentSkillPendingTracked"] = "PASS"

        await page.get_by_role("button", name="关闭技能面板").click()
        await page.get_by_role("button", name="记忆", exact=True).click()
        memory_a = page.locator(".workspace-item", has_text="记忆A")
        memory_b = page.locator(".workspace-item", has_text="记忆B")
        await memory_a.get_by_role("button", name="删除这条记忆").click()
        await memory_b.get_by_role("button", name="删除这条记忆").click()
        await asyncio.wait_for(memory_delete_started["m-a"].wait(), timeout=5)
        await asyncio.wait_for(memory_delete_started["m-b"].wait(), timeout=5)
        await expect(memory_a.get_by_role("button", name="删除这条记忆")).to_be_disabled(timeout=3000)
        await expect(memory_b.get_by_role("button", name="删除这条记忆")).to_be_disabled(timeout=3000)
        memory_delete_release["m-a"].set()
        await memory_a.wait_for(state="detached")
        if not await memory_b.get_by_role("button", name="删除这条记忆").is_disabled():
            raise AssertionError("second memory delete button re-enabled while still pending")
        memory_delete_release["m-b"].set()
        await memory_b.wait_for(state="detached")
        checks["concurrentMemoryPendingTracked"] = "PASS"
        await context.close()

        stale_projects = [
            {"id": "p-stale", "name": "待删除项目", "documents": [], "createdAt": 1, "updatedAt": 1},
            {"id": "p-keep", "name": "保留项目", "documents": [], "createdAt": 1, "updatedAt": 1},
        ]
        stale_list_started = asyncio.Event()
        stale_list_release = asyncio.Event()
        stale_control = {"holdNextList": False}

        async def mock_stale_projects(route: Any) -> None:
            body = route.request.post_data_json or {}
            action = body.get("action", "list")
            if action == "list":
                snapshot = [dict(project) for project in stale_projects]
                if stale_control["holdNextList"]:
                    stale_control["holdNextList"] = False
                    stale_list_started.set()
                    await stale_list_release.wait()
                    try:
                        await route.fulfill(status=200, content_type="application/json", body=json.dumps({"projects": snapshot}))
                    except PlaywrightError:
                        pass
                    return
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"projects": snapshot}))
                return
            if action == "create":
                created = {"id": "p-new", "name": str(body.get("name", "新项目")), "documents": [], "createdAt": 1, "updatedAt": 1}
                stale_projects.append(created)
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"project": created}))
                return
            if action == "delete":
                project_id = str(body.get("id", ""))
                stale_projects[:] = [project for project in stale_projects if project["id"] != project_id]
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                return
            await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "unexpected project action"}))

        stale_context = await browser.new_context(service_workers="allow")
        stale_page_errors: list[str] = []
        await stale_context.route("**/api/config", mock_config)
        await stale_context.route("**/api/projects", mock_stale_projects)
        await stale_context.route("**/api/skills", mock_skills)
        await stale_context.route("**/api/memory**", mock_memory)
        stale_page = await tracked_page(stale_context, stale_page_errors)
        await stale_page.goto(base_url, wait_until="domcontentloaded")
        await stale_page.get_by_role("button", name="项目", exact=True).click()
        await stale_page.get_by_role("button", name="删除项目 待删除项目").wait_for()

        stale_control["holdNextList"] = True
        await stale_page.locator(".project-create-form input").fill("触发后台读取")
        await stale_page.get_by_role("button", name="创建", exact=True).click()
        await asyncio.wait_for(stale_list_started.wait(), timeout=5)
        stale_page.once("dialog", lambda pending_dialog: asyncio.create_task(pending_dialog.accept()))
        await stale_page.get_by_role("button", name="删除项目 待删除项目").click()
        await stale_page.get_by_role("button", name="删除项目 待删除项目").wait_for(state="detached")
        stale_list_release.set()
        await stale_page.wait_for_timeout(200)
        if await stale_page.get_by_role("button", name="删除项目 待删除项目").count() != 0:
            raise AssertionError("cancelled stale project list restored deleted data")
        stale_unhandled = await stale_page.evaluate("() => window.__mutationUnhandledRejections")
        if stale_unhandled or stale_page_errors:
            raise AssertionError(f"stale-read smoke reported browser errors: {stale_unhandled + stale_page_errors}")
        checks["staleReadCannotOverwriteMutation"] = "PASS"
        await stale_context.close()

        await browser.close()
    return checks


async def run_mutation_lifecycle_smoke(base_url: str) -> dict[str, str]:
    from playwright.async_api import expect
    from playwright.async_api import async_playwright

    checks: dict[str, str] = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="allow")
        page_errors: list[str] = []

        await context.add_init_script(
            """
            window.__mutationUnhandledRejections = [];
            window.addEventListener('unhandledrejection', (event) => {
              window.__mutationUnhandledRejections.push(String(event.reason));
              event.preventDefault();
            });
            """
        )

        projects = [
            {"id": "life-a", "name": "生命周期项目A", "documents": [], "createdAt": 1, "updatedAt": 1},
            {"id": "life-b", "name": "生命周期项目B", "documents": [], "createdAt": 1, "updatedAt": 1},
        ]
        project_control = {"failNextList": False}
        project_delete_calls: dict[str, int] = {}
        project_delete_started = asyncio.Event()
        project_delete_release = asyncio.Event()
        project_rename_started = asyncio.Event()
        project_rename_release = asyncio.Event()

        async def mock_config(route: Any) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "hasServerKey": True,
                        "hasSearch": False,
                        "defaultModel": "deepseek-v4-pro",
                        "models": ["deepseek-v4-pro"],
                        "modelRoutes": {},
                        "uploadLimits": {"fileMaxBytes": 200_000_000, "requestMaxBytes": 220_000_000, "maxFiles": 8},
                        "computerUrl": base_url,
                        "phoneUrl": base_url,
                    }
                ),
            )

        async def mock_projects(route: Any) -> None:
            body = route.request.post_data_json or {}
            action = body.get("action", "list")
            if action == "list":
                if project_control["failNextList"]:
                    project_control["failNextList"] = False
                    await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "list failed"}))
                    return
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"projects": projects}))
                return
            if action == "create":
                created = {
                    "id": "life-created",
                    "name": str(body.get("name", "新项目")),
                    "documents": [],
                    "createdAt": 1,
                    "updatedAt": 1,
                }
                projects.append(created)
                project_control["failNextList"] = True
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"project": created}))
                return
            if action == "rename":
                project_rename_started.set()
                await project_rename_release.wait()
                project_id = str(body.get("id", ""))
                name = str(body.get("name", ""))
                for project in projects:
                    if project["id"] == project_id:
                        project["name"] = name
                        await route.fulfill(status=200, content_type="application/json", body=json.dumps({"project": project}))
                        return
            if action == "delete":
                project_id = str(body.get("id", ""))
                project_delete_calls[project_id] = project_delete_calls.get(project_id, 0) + 1
                if project_id == "life-b":
                    project_delete_started.set()
                    await project_delete_release.wait()
                projects[:] = [project for project in projects if project["id"] != project_id]
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                return
            await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "unexpected project action"}))

        binding_patch_calls = {"count": 0}

        async def mock_binding(route: Any) -> None:
            if route.request.method == "GET":
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"skills": {"enabledSkills": [], "defaultSkill": "", "recentSkills": [], "enabledPacks": []}}),
                )
                return
            binding_patch_calls["count"] += 1
            await route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "binding failed"}))

        skills = [
            {
                "skillId": "life-skill",
                "name": "生命周期技能",
                "description": "",
                "version": "1.0.0",
                "systemPrompt": "原提示",
                "builtin": False,
                "disabled": False,
                "updatedAt": "",
            }
        ]
        skill_update_started = asyncio.Event()
        skill_update_release = asyncio.Event()
        skill_delete_calls = {"count": 0}

        async def mock_skills(route: Any) -> None:
            body = route.request.post_data_json or {}
            action = body.get("action", "list")
            if action == "list":
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True, "skills": skills}))
                return
            if action == "update":
                skill_update_started.set()
                await skill_update_release.wait()
                skills[0]["name"] = str(body.get("patch", {}).get("name", skills[0]["name"]))
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"skill": skills[0]}))
                return
            if action == "delete":
                skill_delete_calls["count"] += 1
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                return
            await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "unexpected skill action"}))

        upload_started = asyncio.Event()
        upload_release = asyncio.Event()
        upload_targets: list[str] = []

        async def mock_project_upload(route: Any) -> None:
            upload_targets.append(route.request.url)
            upload_started.set()
            await upload_release.wait()
            await route.fulfill(status=200, content_type="application/json", body=json.dumps({"documents": []}))

        memories = [
            {"id": "life-memory", "content": "生命周期记忆", "category": "fact", "scope": "global"},
        ]
        memory_delete_started = asyncio.Event()
        memory_delete_release = asyncio.Event()
        memory_clear_calls = {"count": 0}

        async def mock_memory(route: Any) -> None:
            if route.request.method == "GET":
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"memories": memories}))
                return
            body = route.request.post_data_json or {}
            if body.get("action") == "deleteById":
                memory_delete_started.set()
                await memory_delete_release.wait()
                memories.clear()
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                return
            if body.get("action") == "clear":
                memory_clear_calls["count"] += 1
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                return
            await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "unexpected memory action"}))

        await context.route("**/api/config", mock_config)
        await context.route("**/api/projects", mock_projects)
        await context.route("**/api/project-files?projectId=*", mock_project_upload)
        await context.route("**/api/workspace/projects/*/skills", mock_binding)
        await context.route("**/api/skills", mock_skills)
        await context.route("**/api/memory**", mock_memory)
        page = await context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        await page.goto(base_url, wait_until="domcontentloaded")

        await page.get_by_role("button", name="项目", exact=True).click()
        await page.locator(".workspace-open", has_text="生命周期项目A").click()
        await page.locator(".project-skill-options label", has_text="生命周期技能").locator("input").click()
        await page.locator(".project-skill-binding .workspace-error").wait_for()
        if binding_patch_calls["count"] != 1:
            raise AssertionError("binding save failure did not reach the binding endpoint exactly once")
        if await page.locator("section.settings-drawer > .workspace-error").count() != 0:
            raise AssertionError("binding save error leaked into the project-list error region")
        checks["mutationScopeIsolation"] = "PASS"
        checks["bindingErrorRemainsLocal"] = "PASS"

        await page.get_by_role("button", name="重命名项目 生命周期项目A").click()
        rename_input = page.get_by_role("textbox", name="重命名项目")
        await rename_input.fill("生命周期项目A-改名")
        await page.evaluate(
            """() => {
              window.confirm = () => true;
              const input = document.querySelector('input[aria-label="重命名项目"]');
              const row = input?.closest('.workspace-item');
              input?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
              row?.querySelector('button[aria-label^="删除项目"]')?.click();
            }"""
        )
        await asyncio.wait_for(project_rename_started.wait(), timeout=5)
        await page.wait_for_timeout(100)
        if project_delete_calls.get("life-a", 0) != 0:
            raise AssertionError("project remove raced with an active rename")
        project_rename_release.set()
        await rename_input.wait_for(state="detached")
        checks["projectLifecycleActionsExclusive"] = "PASS"

        upload_input = page.locator(".project-upload-button input")
        await upload_input.set_input_files({"name": "lifecycle.txt", "mimeType": "text/plain", "buffer": b"lifecycle"})
        await asyncio.wait_for(upload_started.wait(), timeout=5)
        project_a_delete = page.get_by_role("button", name="删除项目 生命周期项目A-改名")
        await expect(project_a_delete).to_be_disabled(timeout=3000)
        checks["projectUploadBlocksDeletion"] = "PASS"
        await page.locator(".workspace-open", has_text="生命周期项目B").click()
        if not upload_targets or "projectId=life-a" not in upload_targets[0]:
            raise AssertionError(f"upload target changed with active project: {upload_targets}")
        if await page.locator(".project-upload-button").get_by_text("上传中…").count() != 0:
            raise AssertionError("project B inherited project A's uploading state")
        if await page.locator(".project-upload-button input").is_disabled():
            raise AssertionError("project B upload was disabled by project A's upload")
        checks["projectUploadTargetStable"] = "PASS"
        upload_release.set()
        await page.wait_for_timeout(100)

        await page.locator(".project-create-form input").fill("触发恢复")
        await page.get_by_role("button", name="创建", exact=True).click()
        await page.locator("section.settings-drawer > .workspace-error").wait_for()
        await page.locator(".workspace-open", has_text="生命周期项目B").click()
        page.once("dialog", lambda dialog: asyncio.create_task(dialog.accept()))
        await page.get_by_role("button", name="删除项目 生命周期项目B").click()
        await asyncio.wait_for(project_delete_started.wait(), timeout=5)
        pending_delete = page.get_by_role("button", name="删除项目 生命周期项目B")
        await expect(pending_delete).to_be_disabled(timeout=3000)
        binding_calls_before_delete = binding_patch_calls["count"]
        deletion_blocked_binding = page.locator(
            ".project-skill-options label", has_text="生命周期技能"
        ).locator("input")
        await deletion_blocked_binding.evaluate("input => { input.removeAttribute('disabled'); input.click(); }")
        await page.wait_for_timeout(100)
        if binding_patch_calls["count"] != binding_calls_before_delete:
            raise AssertionError("project binding save raced with an active deletion")
        checks["projectDeletionBlocksBinding"] = "PASS"
        await page.locator("section.settings-drawer > .workspace-error").get_by_role("button", name="重新同步").click()
        await page.locator("section.settings-drawer > .workspace-error").wait_for(state="detached")
        if not await pending_delete.is_disabled():
            raise AssertionError("project recovery removed a pending deletion from MutationCache")
        checks["recoveryPreservesPendingWork"] = "PASS"
        project_delete_release.set()
        await pending_delete.wait_for(state="detached")

        await page.get_by_role("button", name="关闭项目面板").click()
        await page.get_by_role("button", name="技能", exact=True).click()
        skill_card = page.locator(".skill-card", has_text="生命周期技能")
        await skill_card.get_by_role("button", name="编辑").click()
        await skill_card.get_by_role("textbox", name="技能名称").fill("生命周期技能-改名")
        await skill_card.evaluate(
            """card => {
              window.confirm = () => true;
              card.querySelector('form')?.requestSubmit();
              [...card.querySelectorAll('button')].find(button => button.textContent?.trim() === '删除')?.click();
            }"""
        )
        await asyncio.wait_for(skill_update_started.wait(), timeout=5)
        await page.wait_for_timeout(100)
        if skill_delete_calls["count"] != 0:
            raise AssertionError("skill remove raced with an active update")
        skill_update_release.set()
        await page.locator(".skill-card", has_text="生命周期技能-改名").wait_for()
        checks["skillLifecycleActionsExclusive"] = "PASS"

        await page.get_by_role("button", name="关闭技能面板").click()
        await page.get_by_role("button", name="记忆", exact=True).click()
        memory_row = page.locator(".workspace-item", has_text="生命周期记忆")
        await memory_row.get_by_role("button", name="删除这条记忆").click()
        await asyncio.wait_for(memory_delete_started.wait(), timeout=5)
        clear_button = page.get_by_role("button", name="全部清空")
        await clear_button.click()
        memory_coordination_error = page.locator("section.settings-drawer > .workspace-error")
        await memory_coordination_error.wait_for()
        await expect(memory_coordination_error).to_contain_text("长期记忆正在删除")
        if memory_clear_calls["count"] != 0:
            raise AssertionError("memory clear raced with an active removal")
        checks["crossEntityBlockerAttributed"] = "PASS"
        checks["crossEntityConflictPersists"] = "PASS"
        memory_delete_release.set()
        await memory_row.wait_for(state="detached")
        await memory_coordination_error.wait_for(state="detached")
        checks["exactBlockerSettlementClears"] = "PASS"
        checks["memoryClearWriteBarrier"] = "PASS"

        unhandled = await page.evaluate("() => window.__mutationUnhandledRejections")
        if unhandled or page_errors:
            raise AssertionError(f"mutation lifecycle smoke reported browser errors: {unhandled + page_errors}")
        await context.close()
        await browser.close()
    return checks


async def run_mutation_continuity_smoke(base_url: str) -> dict[str, str]:
    from playwright.async_api import expect
    from playwright.async_api import async_playwright

    checks: dict[str, str] = {}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(service_workers="allow")
        page_errors: list[str] = []
        await context.add_init_script(
            """
            window.__continuityUnhandledRejections = [];
            window.addEventListener('unhandledrejection', (event) => {
              window.__continuityUnhandledRejections.push(String(event.reason));
              event.preventDefault();
            });
            window.confirm = () => true;
            """
        )

        projects = [
            {"id": "intent-project", "name": "连续性项目", "documents": [], "createdAt": 1, "updatedAt": 1},
            {"id": "latest-project", "name": "最新选择项目", "documents": [], "createdAt": 1, "updatedAt": 1},
            {"id": "late-project", "name": "晚到失败项目", "documents": [], "createdAt": 1, "updatedAt": 1},
        ]
        project_create_started = asyncio.Event()
        project_create_release = asyncio.Event()
        project_create_calls = {"count": 0}
        project_delete_started = asyncio.Event()
        project_delete_release = asyncio.Event()
        late_delete_started = asyncio.Event()
        late_delete_release = asyncio.Event()
        project_rename_started = asyncio.Event()
        project_rename_release = asyncio.Event()

        async def mock_config(route: Any) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "hasServerKey": True,
                        "hasSearch": False,
                        "defaultModel": "deepseek-v4-pro",
                        "models": ["deepseek-v4-pro"],
                        "modelRoutes": {},
                        "uploadLimits": {"fileMaxBytes": 200_000_000, "requestMaxBytes": 220_000_000, "maxFiles": 8},
                        "computerUrl": base_url,
                        "phoneUrl": base_url,
                    }
                ),
            )

        async def mock_projects(route: Any) -> None:
            body = route.request.post_data_json or {}
            action = body.get("action", "list")
            if action == "list":
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"projects": projects}))
                return
            if action == "create":
                project_create_calls["count"] += 1
                project_create_started.set()
                await project_create_release.wait()
                created = {
                    "id": "intent-created",
                    "name": str(body.get("name", "意图项目")),
                    "documents": [],
                    "createdAt": 1,
                    "updatedAt": 1,
                }
                projects.append(created)
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"project": created}))
                return
            if action == "delete":
                project_id = str(body.get("id", ""))
                if project_id == "intent-created":
                    project_delete_started.set()
                    await project_delete_release.wait()
                    projects[:] = [project for project in projects if project["id"] != project_id]
                    await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                    return
                if project_id == "late-project":
                    late_delete_started.set()
                    await late_delete_release.wait()
                    await route.fulfill(
                        status=500,
                        content_type="application/json",
                        body=json.dumps({"error": "晚到的删除失败"}),
                    )
                    return
            if action == "rename":
                project_id = str(body.get("id", ""))
                name = str(body.get("name", ""))
                if project_id == "late-project" and name == "旧请求名称":
                    project_rename_started.set()
                    await project_rename_release.wait()
                current = next(project for project in projects if project["id"] == project_id)
                current["name"] = name
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"project": current}))
                return
            await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "unexpected project action"}))

        upload_started = asyncio.Event()
        upload_release = asyncio.Event()
        upload_calls: list[str] = []

        async def mock_upload(route: Any) -> None:
            upload_calls.append(route.request.url)
            upload_started.set()
            await upload_release.wait()
            await route.fulfill(status=200, content_type="application/json", body=json.dumps({"documents": []}))

        skills = [
            {
                "skillId": "intent-skill",
                "name": "连续性技能",
                "description": "",
                "version": "1.0.0",
                "systemPrompt": "提示",
                "builtin": False,
                "disabled": False,
                "updatedAt": "",
            }
        ]
        skill_create_started = asyncio.Event()
        skill_create_release = asyncio.Event()
        skill_create_calls = {"count": 0}
        skill_list_calls = {"count": 0}

        async def mock_skills(route: Any) -> None:
            body = route.request.post_data_json or {}
            action = body.get("action", "list")
            if action == "list":
                skill_list_calls["count"] += 1
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"skills": skills}))
                return
            if action == "create":
                skill_create_calls["count"] += 1
                skill_create_started.set()
                await skill_create_release.wait()
                config = body.get("skill", {})
                created = {
                    "skillId": "intent-created-skill",
                    "name": str(config.get("name", "重复技能")),
                    "description": str(config.get("description", "")),
                    "version": "1.0.0",
                    "systemPrompt": str(config.get("systemPrompt", "提示")),
                    "builtin": False,
                    "disabled": False,
                    "updatedAt": "",
                }
                skills.append(created)
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"skill": created}))
                return
            await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "unexpected skill action"}))

        binding_save_started = asyncio.Event()
        binding_save_release = asyncio.Event()
        binding_state: dict[str, Any] = {"enabledSkills": [], "defaultSkill": ""}

        async def mock_binding(route: Any) -> None:
            if route.request.method == "GET":
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"skills": {**binding_state, "recentSkills": [], "enabledPacks": []}}),
                )
                return
            binding_save_started.set()
            await binding_save_release.wait()
            body = route.request.post_data_json or {}
            binding_state.update({
                "enabledSkills": body.get("enabledSkills", []),
                "defaultSkill": body.get("defaultSkill", ""),
            })
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"skills": {**binding_state, "recentSkills": [], "enabledPacks": []}}),
            )

        memories = [{"id": "intent-memory", "content": "连续性记忆", "category": "fact", "scope": "global"}]
        memory_clear_started = asyncio.Event()
        memory_clear_release = asyncio.Event()
        memory_clear_calls = {"count": 0}
        memory_save_started = asyncio.Event()
        memory_save_release = asyncio.Event()

        async def mock_memory(route: Any) -> None:
            if route.request.method == "GET":
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"memories": memories}))
                return
            body = route.request.post_data_json or {}
            if body.get("action") == "clear":
                memory_clear_calls["count"] += 1
                memory_clear_started.set()
                await memory_clear_release.wait()
                memories.clear()
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
                return
            if body.get("action") == "add":
                memory_save_started.set()
                await memory_save_release.wait()
                saved = {
                    "id": "saved-suggestion-a",
                    "content": str(body.get("content", "")),
                    "category": str(body.get("category", "fact")),
                    "scope": str(body.get("scope", "global")),
                }
                memories.append(saved)
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"ok": True, "memory": saved}),
                )
                return
            await route.fulfill(status=400, content_type="application/json", body=json.dumps({"error": "unexpected memory action"}))

        chat_calls = {"count": 0}

        async def mock_chat(route: Any) -> None:
            chat_calls["count"] += 1
            suggestion = "记忆建议 A" if chat_calls["count"] == 1 else "记忆建议 B"
            body = "\n".join(
                [
                    json.dumps({"type": "memory_suggestion", "content": suggestion, "category": "fact", "scope": "global"}),
                    json.dumps({"type": "done", "content": ""}),
                    "",
                ]
            )
            await route.fulfill(status=200, headers={"Content-Type": "application/x-ndjson"}, body=body)

        async def mock_title(route: Any) -> None:
            await route.fulfill(status=200, content_type="application/json", body=json.dumps({"title": "连续性"}))

        await context.route("**/api/config", mock_config)
        await context.route("**/api/projects", mock_projects)
        await context.route("**/api/project-files?projectId=*", mock_upload)
        await context.route("**/api/skills", mock_skills)
        await context.route("**/api/workspace/projects/*/skills", mock_binding)
        await context.route("**/api/memory**", mock_memory)
        await context.route("**/api/chat", mock_chat)
        await context.route("**/api/title", mock_title)
        page = await context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        await page.goto(base_url, wait_until="domcontentloaded")

        async def navigate_workspace(path: str) -> None:
            await page.evaluate(
                """path => {
                  window.history.pushState({}, '', path);
                  window.dispatchEvent(new PopStateEvent('popstate'));
                }""",
                path,
            )
            await page.wait_for_function("path => window.location.pathname === path", arg=path)

        await page.get_by_role("button", name="项目", exact=True).click()
        project_form = page.locator(".project-create-form")
        await project_form.locator("input").fill("重复意图项目")
        await project_form.evaluate("form => { form.requestSubmit(); form.requestSubmit(); }")
        await asyncio.wait_for(project_create_started.wait(), timeout=5)
        await page.wait_for_timeout(100)
        if project_create_calls["count"] != 1:
            raise AssertionError(f"duplicate project create escaped intent lock: {project_create_calls}")
        checks["mutationIntentIdentity"] = "PASS"
        checks["projectCreateDuplicateSuppressed"] = "PASS"
        await project_form.locator("input").fill("等待创建时的新草稿")
        await page.locator(".workspace-open", has_text="最新选择项目").click()
        project_create_release.set()
        created_row = page.locator(".workspace-item", has_text="重复意图项目")
        await created_row.wait_for()
        await expect(project_form.locator("input")).to_have_value("等待创建时的新草稿")
        latest_row = page.locator(".workspace-item", has_text="最新选择项目")
        await expect(latest_row).to_have_class(re.compile(r"\bactive\b"))
        checks["workspaceDraftLatestIntentWins"] = "PASS"

        await created_row.locator(".workspace-open").click()
        await created_row.get_by_role("button", name="删除项目 重复意图项目").click()
        await asyncio.wait_for(project_delete_started.wait(), timeout=5)
        await latest_row.locator(".workspace-open").click()
        project_delete_release.set()
        await created_row.wait_for(state="detached")
        await expect(latest_row).to_have_class(re.compile(r"\bactive\b"))
        checks["projectSelectionLatestIntentWins"] = "PASS"

        await page.locator(".workspace-open", has_text="连续性项目").click()
        upload_input = page.locator(".project-upload-button input")
        await upload_input.set_input_files({"name": "first.txt", "mimeType": "text/plain", "buffer": b"first"})
        await asyncio.wait_for(upload_started.wait(), timeout=5)
        await navigate_workspace("/trace/mutation-continuity")
        await page.locator(".settings-drawer").wait_for(state="detached")
        await navigate_workspace("/")
        await page.get_by_role("button", name="项目", exact=True).click()
        await page.locator(".project-upload-button", has_text="上传中…").wait_for()
        project_row = page.locator(".workspace-item", has_text="连续性项目")
        await expect(project_row.get_by_role("button", name="重命名项目 连续性项目")).to_be_disabled()
        await expect(project_row.get_by_role("button", name="删除项目 连续性项目")).to_be_disabled()
        checks["workspaceMutationSurvivesRemount"] = "PASS"
        checks["lazyMutationSurvivesClose"] = "PASS"

        remounted_upload = page.locator(".project-upload-button input")
        await remounted_upload.evaluate("input => input.removeAttribute('disabled')")
        await remounted_upload.set_input_files({"name": "different.txt", "mimeType": "text/plain", "buffer": b"different"})
        await page.locator("section.settings-drawer > .workspace-error").wait_for()
        if len(upload_calls) != 1:
            raise AssertionError(f"different upload intent was reported as sent: {upload_calls}")
        checks["differentIntentNotReportedAsSuccess"] = "PASS"
        checks["coordinationConflictVisible"] = "PASS"
        upload_release.set()
        await page.locator(".project-upload-button", has_text="上传文档").wait_for()
        await page.locator("section.settings-drawer > .workspace-error").wait_for(state="detached")
        checks["coordinationErrorAutoClears"] = "PASS"

        late_row = page.locator(".workspace-item", has_text="晚到失败项目")
        await late_row.get_by_role("button", name="重命名项目 晚到失败项目").click()
        late_rename_input = page.get_by_role("textbox", name="重命名项目")
        await late_rename_input.fill("旧请求名称")
        await late_rename_input.press("Enter")
        await asyncio.wait_for(project_rename_started.wait(), timeout=5)
        await latest_row.get_by_role("button", name="重命名项目 最新选择项目").click()
        await expect(page.get_by_role("textbox", name="重命名项目")).to_have_value("最新选择项目")
        project_rename_release.set()
        await page.locator(".workspace-open", has_text="旧请求名称").wait_for()
        await expect(page.get_by_role("textbox", name="重命名项目")).to_have_value("最新选择项目")
        checks["renameCompletionIsolation"] = "PASS"
        await page.get_by_role("textbox", name="重命名项目").press("Escape")

        late_row = page.locator(".workspace-item", has_text="旧请求名称")
        await late_row.get_by_role("button", name="删除项目 旧请求名称").click()
        await asyncio.wait_for(late_delete_started.wait(), timeout=5)
        await latest_row.get_by_role("button", name="重命名项目 最新选择项目").click()
        latest_rename_input = page.get_by_role("textbox", name="重命名项目")
        await latest_rename_input.fill("最新项目成功")
        await latest_rename_input.press("Enter")
        await page.locator(".workspace-open", has_text="最新项目成功").wait_for()
        late_delete_release.set()
        await page.locator("section.settings-drawer > .workspace-error", has_text="晚到的删除失败").wait_for()
        checks["lateConcurrentFailureVisible"] = "PASS"
        await page.locator("section.settings-drawer > .workspace-error").get_by_role("button", name="重新同步").click()
        await page.locator("section.settings-drawer > .workspace-error").wait_for(state="detached")

        await page.get_by_role("button", name="关闭项目面板").click()
        await page.get_by_role("button", name="技能", exact=True).click()
        await page.get_by_role("button", name="新建技能").click()
        skill_form = page.locator(".skill-form")
        await skill_form.get_by_role("textbox", name="技能名称").fill("重复技能")
        await skill_form.get_by_role("textbox", name="技能提示词").fill("重复提示")
        await skill_form.evaluate("form => { form.requestSubmit(); form.requestSubmit(); }")
        await asyncio.wait_for(skill_create_started.wait(), timeout=5)
        await page.wait_for_timeout(100)
        if skill_create_calls["count"] != 1:
            raise AssertionError(f"duplicate skill create escaped intent lock: {skill_create_calls}")
        checks["skillCreateDuplicateSuppressed"] = "PASS"
        await skill_form.get_by_role("button", name="取消").click()
        await page.get_by_role("button", name="新建技能").click()
        reopened_skill_form = page.locator(".skill-form")
        await reopened_skill_form.get_by_role("textbox", name="技能名称").fill("等待旧请求时的新技能")
        await reopened_skill_form.get_by_role("textbox", name="技能提示词").fill("新提示")
        skill_create_release.set()
        await page.locator(".skill-card", has_text="重复技能").wait_for()
        await expect(reopened_skill_form.get_by_role("textbox", name="技能名称")).to_have_value("等待旧请求时的新技能")
        checks["skillFormCompletionIsolation"] = "PASS"
        await reopened_skill_form.get_by_role("button", name="取消").click()

        await page.get_by_role("button", name="关闭技能面板").click()
        await page.get_by_role("button", name="记忆", exact=True).click()
        await page.get_by_role("button", name="全部清空").click()
        await asyncio.wait_for(memory_clear_started.wait(), timeout=5)
        await navigate_workspace("/trace/memory-continuity")
        await navigate_workspace("/")
        await page.get_by_role("button", name="记忆", exact=True).click()
        await expect(page.get_by_role("button", name="清空中…")).to_be_disabled()
        if memory_clear_calls["count"] != 1:
            raise AssertionError("memory clear was resubmitted after Workspace remount")
        checks["memoryClearStateSurvivesRemount"] = "PASS"
        memory_clear_release.set()
        await page.get_by_text("还没有长期记忆").wait_for()
        memories.append({
            "id": "barrier-memory",
            "content": "跨 Provider 屏障记忆",
            "category": "fact",
            "scope": "global",
        })

        await page.get_by_role("button", name="关闭记忆面板").click()
        await page.locator("#reactPromptInput").fill("触发记忆建议 A")
        await page.locator("button.send-button").click()
        suggestion_toast = page.locator(".memory-suggestion-toast")
        await suggestion_toast.get_by_text("记忆建议 A", exact=True).wait_for()
        await suggestion_toast.get_by_role("button", name="保存", exact=True).click()
        await asyncio.wait_for(memory_save_started.wait(), timeout=5)
        await page.locator("#reactPromptInput").fill("触发记忆建议 B")
        await page.locator("button.send-button").click()
        await suggestion_toast.get_by_text("记忆建议 B", exact=True).wait_for()
        await page.wait_for_timeout(30_100)
        await page.get_by_role("button", name="记忆", exact=True).click()
        await page.get_by_text("跨 Provider 屏障记忆", exact=True).wait_for()
        page.once("dialog", lambda pending_dialog: asyncio.create_task(pending_dialog.accept()))
        await page.get_by_role("button", name="全部清空").click()
        memory_coordination_error = page.locator("section.settings-drawer > .workspace-error")
        await memory_coordination_error.wait_for()
        await expect(memory_coordination_error).to_contain_text("长期记忆正在保存")
        if memory_clear_calls["count"] != 1:
            raise AssertionError("lazy Memory clear raced with the root MemoryProvider save")
        checks["memoryBarrierCrossProvider"] = "PASS"
        await page.get_by_role("button", name="关闭记忆面板").click()
        await page.get_by_role("button", name="记忆", exact=True).click()
        await page.get_by_text("跨 Provider 屏障记忆", exact=True).wait_for()
        page.once("dialog", lambda pending_dialog: asyncio.create_task(pending_dialog.accept()))
        await page.get_by_role("button", name="全部清空").click()
        await page.locator("section.settings-drawer > .workspace-error", has_text="长期记忆正在保存").wait_for()
        if memory_clear_calls["count"] != 1:
            raise AssertionError("Memory blocker disappeared after lazy provider remount")
        checks["memoryBarrierSurvivesLazyRemount"] = "PASS"
        memory_save_release.set()
        await expect(suggestion_toast.get_by_text("记忆建议 B", exact=True)).to_be_visible()
        checks["memorySuggestionCompletionIsolation"] = "PASS"
        await page.get_by_role("button", name="关闭记忆面板").click()

        await page.get_by_role("button", name="项目", exact=True).click()
        await page.get_by_role("heading", name="项目", exact=True).wait_for()
        continuity_row = page.locator(".workspace-item", has_text="连续性项目")
        await continuity_row.wait_for()
        if "active" not in ((await continuity_row.get_attribute("class")) or "").split():
            await continuity_row.locator(".workspace-open").click()
        binding_checkbox = page.locator(".project-skill-options label", has_text="连续性技能").locator("input")
        try:
            await binding_checkbox.click(timeout=10_000)
        except Exception as exc:
            body = await page.locator("body").inner_text()
            state = await page.evaluate(
                """() => ({
                  activeRows: Array.from(document.querySelectorAll('.workspace-item.active')).map((node) => node.textContent),
                  projectHeadings: Array.from(document.querySelectorAll('.settings-drawer h3')).map((node) => node.textContent),
                  errors: Array.from(document.querySelectorAll('.workspace-error')).map((node) => node.textContent),
                })"""
            )
            raise AssertionError(
                f"project skill binding did not remount: skillListCalls={skill_list_calls['count']}, state={state}, body={body[-800:]!r}"
            ) from exc
        await asyncio.wait_for(binding_save_started.wait(), timeout=5)
        await navigate_workspace("/trace/binding-continuity")
        await navigate_workspace("/")
        await page.get_by_role("button", name="项目", exact=True).click()
        await page.locator(".project-skill-binding h3", has_text="保存中").wait_for()
        await expect(page.locator(".project-skill-options label", has_text="连续性技能").locator("input")).to_be_disabled()
        binding_project_row = page.locator(".workspace-item", has_text="连续性项目")
        await expect(binding_project_row.get_by_role("button", name="删除项目 连续性项目")).to_be_disabled()
        checks["projectBindingBlocksDeletion"] = "PASS"
        checks["bindingStateSurvivesRemount"] = "PASS"
        remounted_binding_checkbox = page.locator(".project-skill-options label", has_text="连续性技能").locator("input")
        await remounted_binding_checkbox.evaluate("input => input.removeAttribute('disabled')")
        await remounted_binding_checkbox.click()
        binding_error = page.locator(".project-skill-binding .workspace-error")
        await binding_error.wait_for()
        await binding_error.get_by_role("button", name="重试").click()
        await binding_error.wait_for(state="detached")
        checks["bindingCoordinationRecovery"] = "PASS"
        binding_save_release.set()
        await page.locator(".project-skill-binding h3", has_text="项目技能").wait_for()

        unhandled = await page.evaluate("() => window.__continuityUnhandledRejections")
        if unhandled or page_errors:
            raise AssertionError(f"mutation continuity smoke reported browser errors: {unhandled + page_errors}")
        await context.close()
        await browser.close()
    return checks


async def run_demand_loading_smoke(base_url: str) -> dict[str, str]:
    from playwright.async_api import async_playwright

    checks: dict[str, str] = {}

    async def install_api_routes(context: Any, counters: dict[str, int]) -> None:
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
                        "models": ["deepseek-v4-pro"],
                        "modelRoutes": {},
                        "computerUrl": base_url,
                        "phoneUrl": base_url,
                        "uploadLimits": {"fileMaxBytes": 200_000_000, "requestMaxBytes": 220_000_000, "maxFiles": 8},
                    }
                ),
            )

        async def mock_projects(route: Any) -> None:
            await route.fulfill(status=200, content_type="application/json", body=json.dumps({"projects": []}))

        async def mock_skills(route: Any) -> None:
            body = route.request.post_data_json or {}
            if body.get("action") == "list":
                counters["skills"] += 1
            await route.fulfill(status=200, content_type="application/json", body=json.dumps({"skills": []}))

        async def mock_memory(route: Any) -> None:
            if route.request.method == "GET":
                counters["memory"] += 1
                await route.fulfill(status=200, content_type="application/json", body=json.dumps({"memories": []}))
                return
            await route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))

        await context.route("**/api/config", mock_config)
        await context.route("**/api/projects", mock_projects)
        await context.route("**/api/skills", mock_skills)
        await context.route("**/api/memory**", mock_memory)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        counters = {"skills": 0, "memory": 0}
        context = await browser.new_context(service_workers="block")
        await install_api_routes(context, counters)
        page = await context.new_page()
        await page.goto(base_url, wait_until="networkidle")
        initial_features = await page.evaluate(
            r"""() => performance.getEntriesByType('resource')
              .map((entry) => entry.name)
              .filter((name) => /\/ui\/assets\/(Projects|Skills|Memory|ConnectionSettings|Reminders|Diagnostics|FilePreview|ImageLightbox|Activity)Feature-/.test(name))"""
        )
        if initial_features:
            raise AssertionError(f"Workspace feature chunks loaded on cold start: {initial_features}")
        if counters != {"skills": 0, "memory": 0}:
            raise AssertionError(f"Workspace feature queries loaded on cold start: {counters}")
        checks["workspaceOptionalChunksDeferred"] = "PASS"
        checks["skillsQueryDeferred"] = "PASS"
        checks["memoryListQueryDeferred"] = "PASS"

        skills_button = page.get_by_role("button", name="技能", exact=True)
        await skills_button.hover()
        await page.wait_for_function(
            r"""() => performance.getEntriesByType('resource')
              .some((entry) => /\/ui\/assets\/SkillsFeature-/.test(entry.name))""",
            timeout=10_000,
        )
        if counters != {"skills": 0, "memory": 0}:
            raise AssertionError(f"intent preload started a Workspace query: {counters}")
        checks["workspaceFeaturePreloadsOnIntent"] = "PASS"
        checks["preloadDoesNotStartQueries"] = "PASS"

        before_click = await page.evaluate(
            r"""() => performance.getEntriesByType('resource')
              .filter((entry) => /\/ui\/assets\/SkillsFeature-/.test(entry.name)).length"""
        )
        await skills_button.click()
        await page.get_by_role("heading", name="技能", exact=True).wait_for()
        await page.wait_for_function("() => true")
        if counters["skills"] != 1:
            raise AssertionError(f"Skills query did not start on first activation: {counters}")
        checks["workspaceFeatureLoadsOnDemand"] = "PASS"
        await page.get_by_role("button", name="关闭技能面板").click()
        await skills_button.click()
        await page.get_by_role("heading", name="技能", exact=True).wait_for()
        after_reopen = await page.evaluate(
            r"""() => performance.getEntriesByType('resource')
              .filter((entry) => /\/ui\/assets\/SkillsFeature-/.test(entry.name)).length"""
        )
        if after_reopen != before_click:
            raise AssertionError(f"Skills chunk downloaded again after reopen: {before_click} -> {after_reopen}")
        await page.get_by_role("button", name="关闭技能面板").click()
        await page.get_by_role("button", name="记忆", exact=True).click()
        await page.get_by_role("heading", name="长期记忆", exact=True).wait_for()
        if counters["memory"] != 1:
            raise AssertionError(f"Memory list query did not start on activation: {counters}")
        await context.close()

        race_counters = {"skills": 0, "memory": 0}
        race_context = await browser.new_context(service_workers="block")
        await install_api_routes(race_context, race_counters)
        project_chunk_started = asyncio.Event()
        project_chunk_release = asyncio.Event()

        async def hold_project_chunk(route: Any) -> None:
            project_chunk_started.set()
            await project_chunk_release.wait()
            await route.continue_()

        await race_context.route("**/ui/assets/ProjectsFeature-*.js", hold_project_chunk)
        race_page = await race_context.new_page()
        await race_page.goto(base_url, wait_until="networkidle")
        await race_page.get_by_role("button", name="项目", exact=True).click()
        await asyncio.wait_for(project_chunk_started.wait(), timeout=5)
        await race_page.get_by_role("button", name="记忆", exact=True).evaluate("button => button.click()")
        await race_page.get_by_role("heading", name="长期记忆", exact=True).wait_for()
        project_chunk_release.set()
        await race_page.wait_for_timeout(200)
        if await race_page.get_by_role("heading", name="项目", exact=True).count():
            raise AssertionError("late Projects chunk replaced the current Memory overlay")
        checks["latestOverlayWinsDuringLoad"] = "PASS"
        await race_context.close()

        failure_counters = {"skills": 0, "memory": 0}
        failure_context = await browser.new_context(service_workers="block")
        await install_api_routes(failure_context, failure_counters)
        project_chunk_requests = 0

        async def fail_project_chunk_once(route: Any) -> None:
            nonlocal project_chunk_requests
            project_chunk_requests += 1
            if project_chunk_requests == 1:
                await route.fulfill(
                    status=503,
                    content_type="application/javascript",
                    body="throw new Error('simulated Workspace chunk outage')",
                )
                return
            await route.continue_()

        await failure_context.route("**/ui/assets/ProjectsFeature-*.js", fail_project_chunk_once)
        failure_page = await failure_context.new_page()
        await failure_page.goto(base_url, wait_until="networkidle")
        await failure_page.get_by_role("button", name="项目", exact=True).evaluate("button => button.click()")
        await failure_page.wait_for_timeout(2_000)
        if await failure_page.get_by_text("项目面板加载失败", exact=True).count() == 0:
            resources = await failure_page.evaluate(
                "() => performance.getEntriesByType('resource').map((entry) => entry.name).filter((name) => name.includes('Projects'))"
            )
            body = await failure_page.locator("body").inner_text()
            raise AssertionError(
                f"Workspace chunk failure UI missing: requests={project_chunk_requests}, resources={resources}, body={body[:500]!r}"
            )
        runtime_requests_before_retry = await failure_page.evaluate(
            r"""() => performance.getEntriesByType('resource')
              .filter((entry) => /\/ui\/assets\/SkillsRuntimeBoundary-/.test(entry.name)).length"""
        )
        await failure_page.get_by_role("button", name="重试", exact=True).click()
        await failure_page.get_by_role("heading", name="项目", exact=True).wait_for(timeout=10_000)
        if project_chunk_requests != 2:
            raise AssertionError(f"Workspace chunk retry count was {project_chunk_requests}, expected 2")
        checks["workspaceChunkFailureContained"] = "PASS"
        checks["chunkRetryProducesNewRequest"] = "PASS"
        runtime_requests = await failure_page.evaluate(
            r"""() => performance.getEntriesByType('resource')
              .filter((entry) => /\/ui\/assets\/SkillsRuntimeBoundary-/.test(entry.name)).length"""
        )
        if runtime_requests != runtime_requests_before_retry:
            raise AssertionError(
                f"Feature recovery reset the loaded Skills runtime: {runtime_requests_before_retry} -> {runtime_requests}"
            )
        checks["featureRuntimeRecoveryIsolated"] = "PASS"
        await failure_context.close()

        exhaustion_counters = {"skills": 0, "memory": 0}
        exhaustion_context = await browser.new_context(service_workers="block")
        await install_api_routes(exhaustion_context, exhaustion_counters)
        exhausted_requests = 0

        async def fail_project_chunk_always(route: Any) -> None:
            nonlocal exhausted_requests
            exhausted_requests += 1
            await route.fulfill(
                status=503,
                content_type="application/javascript",
                body="throw new Error('simulated exhausted Workspace recovery')",
            )

        await exhaustion_context.route("**/ui/assets/ProjectsFeature-*.js", fail_project_chunk_always)
        exhaustion_page = await exhaustion_context.new_page()
        await exhaustion_page.goto(base_url, wait_until="networkidle")
        await exhaustion_page.get_by_role("button", name="项目", exact=True).evaluate("button => button.click()")
        await exhaustion_page.get_by_role("button", name="重试", exact=True).wait_for(timeout=10_000)
        await exhaustion_page.get_by_role("button", name="重试", exact=True).click()
        await exhaustion_page.get_by_role("button", name="刷新应用", exact=True).wait_for(timeout=10_000)
        if await exhaustion_page.get_by_role("button", name="重试", exact=True).count():
            raise AssertionError("exhausted Workspace recovery still offered a fake retry")
        if exhausted_requests != 2:
            raise AssertionError(f"Workspace recovery requested {exhausted_requests} chunks, expected exactly 2")
        checks["chunkRetryExhaustionTruthful"] = "PASS"
        await exhaustion_context.close()
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
        checks.update(asyncio.run(run_demand_loading_smoke(base_url)))
        checks.update(asyncio.run(run_query_smoke(base_url)))
        checks.update(asyncio.run(run_recovery_smoke(base_url)))
        checks.update(asyncio.run(run_mutation_smoke(base_url)))
        checks.update(asyncio.run(run_mutation_lifecycle_smoke(base_url)))
        checks.update(asyncio.run(run_mutation_continuity_smoke(base_url)))
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
