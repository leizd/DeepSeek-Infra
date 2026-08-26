"""Run the real-browser frontend safety, React chat, and offline smoke gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import random
import re
import struct
import sys
import threading
import time
import zlib
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
UPDATE_READY_TIMEOUT_MS = 120_000


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
            """() => {
              for (let i = 0; i < localStorage.length; i += 1) {
                const key = localStorage.key(i);
                if (key && key.startsWith('deepseek-infra.session.v3.snapshot.')) {
                  const raw = localStorage.getItem(key);
                  if (raw && raw.includes('Browser smoke reply')) return true;
                }
              }
              return false;
            }"""
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
        try:
            await page.wait_for_function(
                """() => {
                  const banner = document.querySelector('.build-update-banner');
                  const button = Array.from(banner?.querySelectorAll('button') || [])
                    .find((candidate) => candidate.textContent?.includes('更新并重新加载'));
                  if (Boolean(banner?.textContent?.includes('bbbbbbbbbbbbbbbb') && button && !button.disabled)) {
                    return true;
                  }
                  const checkBtn = document.querySelector('button[aria-label="检查更新"]') || Array.from(document.querySelectorAll('button')).find(b => b.textContent === '检查更新');
                  if (checkBtn && (!window.__lastCheckNudge || Date.now() - window.__lastCheckNudge > 2000)) {
                    window.__lastCheckNudge = Date.now();
                    checkBtn.click();
                  }
                  return false;
                }""",
                timeout=UPDATE_READY_TIMEOUT_MS,
            )
        except Exception as error:
            staged_b_state = await page.evaluate(
                """async () => ({
                  text: document.querySelector('.build-update-banner')?.textContent || '',
                  controller: navigator.serviceWorker.controller?.scriptURL || '',
                  registrations: (await navigator.serviceWorker.getRegistrations()).map((registration) => ({
                    scope: registration.scope,
                    active: registration.active?.scriptURL || '',
                    waiting: registration.waiting?.scriptURL || '',
                    installing: registration.installing?.scriptURL || '',
                  })),
                })"""
            )
            raise AssertionError(f"staged build B was not ready: {staged_b_state}") from error
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
                  if (Boolean(banner?.textContent?.includes('cccccccccccccccc') && button && !button.disabled)) {
                    return true;
                  }
                  const checkBtn = document.querySelector('button[aria-label="检查更新"]') || Array.from(document.querySelectorAll('button')).find(b => b.textContent === '检查更新');
                  if (checkBtn && (!window.__lastCheckNudgeC || Date.now() - window.__lastCheckNudgeC > 2000)) {
                    window.__lastCheckNudgeC = Date.now();
                    checkBtn.click();
                  }
                  return false;
                }""",
                timeout=UPDATE_READY_TIMEOUT_MS,
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
        primary_update = page.get_by_role("button", name="完成后更新")
        # 若同一轮协调器已经进入 activating，按钮会 disabled：此时不伪造 DOM
        # 点击，激活事务会在 controller 验证后观察 blocker 并登记自动续跑。
        if await primary_update.is_enabled():
            await primary_update.click(timeout=15_000)
        else:
            banner_state = await page.evaluate(
                """async () => {
                  const banner = document.querySelector('.build-update-banner');
                  const primary = banner?.querySelector('button.primary');
                  return {
                    bannerText: banner?.textContent || '',
                    primaryDisabled: primary?.disabled ?? null,
                    primaryLabel: primary?.textContent || '',
                    controller: navigator.serviceWorker.controller?.scriptURL || '',
                    registrations: (await navigator.serviceWorker.getRegistrations()).map((registration) => ({
                      scope: registration.scope,
                      active: registration.active?.scriptURL || '',
                      waiting: registration.waiting?.scriptURL || '',
                      installing: registration.installing?.scriptURL || '',
                    })),
                  }
                }"""
            )
            if (
                not banner_state["primaryDisabled"]
                or banner_state["primaryLabel"] != "完成后更新"
                or "正在生成回复" not in banner_state["bannerText"]
                or not any(item["waiting"].endswith("/sw-cccccccccccccccc.js") for item in banner_state["registrations"])
            ):
                raise AssertionError(f"unexpected blocked activation state: {banner_state}")
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
        try:
            # Service Worker 接管触发的是同 URL reload。Playwright 的
            # expect_navigation(wait_until=...) 会把主 frame 导航与 lifecycle
            # 事件绑在一起；controllerchange 期间偶尔会观察到前者却漏掉后者，
            # 从而在已经 reload 的页面上误报超时。先只等待主 frame 导航，随后
            # 由 React 输入框与 Worker identity 分别核验页面和 controller 就绪。
            async with page.expect_event(
                "framenavigated",
                predicate=lambda frame: frame == page.main_frame,
                timeout=20_000,
            ):
                await page.locator("button.stop-button").click()
                stop_release.set()
        except Exception as error:
            stop_release.set()
            try:
                activation_state = await page.evaluate(
                    """async () => ({
                      url: window.location.href,
                      bannerText: document.querySelector('.build-update-banner')?.textContent || '',
                      stopButtonPresent: Boolean(document.querySelector('button.stop-button')),
                      promptPresent: Boolean(document.querySelector('#reactPromptInput')),
                      controller: navigator.serviceWorker.controller?.scriptURL || '',
                      registrations: (await navigator.serviceWorker.getRegistrations()).map((registration) => ({
                        scope: registration.scope,
                        active: registration.active?.scriptURL || '',
                        waiting: registration.waiting?.scriptURL || '',
                        installing: registration.installing?.scriptURL || '',
                      })),
                    })"""
                )
            except PlaywrightError as diagnostic_error:
                activation_state = {"diagnosticError": str(diagnostic_error), "url": page.url}
            raise AssertionError(
                "update activation did not navigate the initiating tab: "
                f"navigations={main_navigations!r}, state={activation_state!r}"
            ) from error
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


async def run_durable_checkpoint_smoke(base_url: str) -> dict[str, str]:
    """Exercise the v4.3.5 durable checkpoint and recovery contracts in a real browser."""
    from playwright.async_api import async_playwright

    checks: dict[str, str] = {}
    canonical_version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()

    config_payload = {
        "hasServerKey": True,
        "hasSearch": False,
        "version": VERSION,
        "defaultModel": "deepseek-v4-pro",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "modelRoutes": {},
        "computerUrl": base_url,
        "phoneUrl": base_url,
        "uploadLimits": {"fileMaxBytes": 200_000_000, "requestMaxBytes": 220_000_000, "maxFiles": 8},
    }

    async def mock_config(route: Any) -> None:
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(config_payload))

    async def mock_chat(route: Any) -> None:
        body = "\n".join(
            [
                json.dumps({"type": "content", "text": "检查点冒烟回复"}),
                json.dumps({"type": "done", "content": "检查点冒烟回复", "model": "deepseek-v4-pro", "usage": {}}),
                "",
            ]
        )
        await route.fulfill(status=200, headers={"Content-Type": "application/x-ndjson"}, body=body)

    async def mock_title(route: Any) -> None:
        await route.fulfill(status=200, content_type="application/json", body=json.dumps({"title": "检查点冒烟"}))

    def checkpoint_snapshot(generation: int, conversation_id: str, messages: list[dict[str, Any]], title: str) -> str:
        return json.dumps(
            {
                "schemaVersion": 2,
                "generation": generation,
                "savedAt": 1700000000000,
                "currentConversationId": conversation_id,
                "conversations": [
                    {
                        "id": conversation_id,
                        "title": title,
                        "createdAt": 1,
                        "updatedAt": 2,
                        "messages": messages,
                    }
                ],
            },
            ensure_ascii=False,
        )

    def seed_storage_script(entries: dict[str, str]) -> str:
        return "".join(
            f"localStorage.setItem({json.dumps(key)}, {json.dumps(value, ensure_ascii=False)});"
            for key, value in entries.items()
        )

    # 只拦截 localStorage 的会话 checkpoint 键：conversation 让整份提交失败，head 只让指针推进失败。
    storage_failure_init = """
window.__checkpointSmokeFailures = { conversation: false, head: false };
{
  const originalSetItem = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {
    const flags = window.__checkpointSmokeFailures || {};
    const name = String(key);
    const failConversation = flags.conversation && name.startsWith('deepseek-infra.session.v3.');
    const failHead = flags.head && name.startsWith('deepseek-infra.session.v3.head.');
    if (this === window.localStorage && (failConversation || failHead)) {
      throw new DOMException('simulated smoke storage failure', 'QuotaExceededError');
    }
    return originalSetItem.call(this, key, value);
  };
}
"""

    draft_failure_init = """
{
  const originalSetItem = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {
    if (this === window.sessionStorage && String(key).startsWith('deepseek:composer-draft:')) {
      throw new DOMException('simulated smoke draft failure', 'QuotaExceededError');
    }
    return originalSetItem.call(this, key, value);
  };
}
"""

    legacy_draft_text = "旧版草稿在迁移失败时必须保留"
    legacy_draft_init = (
        """
{
  const legacyKey = 'deepseek:composer-draft:new';
  if (!sessionStorage.getItem('__checkpointSmokeSeeded')) {
    sessionStorage.setItem(legacyKey, JSON.stringify({
      conversationId: 'new',
      text: """
        + json.dumps(legacy_draft_text, ensure_ascii=False)
        + """,
      updatedAt: 1700000000000,
    }));
    sessionStorage.setItem('__checkpointSmokeSeeded', '1');
  }
  const originalSetItem = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {
    if (this === window.sessionStorage && String(key) === 'deepseek:composer-draft:new:') {
      throw new DOMException('simulated scoped draft write failure', 'QuotaExceededError');
    }
    return originalSetItem.call(this, key, value);
  };
}
"""
    )

    beforeunload_probe = """
() => {
  const event = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(event);
  return event.defaultPrevented;
}
"""

    registration_snapshot = """
async () => (await navigator.serviceWorker.getRegistrations()).map((registration) => ({
  scope: registration.scope,
  active: registration.active?.scriptURL || '',
}))
"""

    async with async_playwright() as playwright:
        api_headers = {"Authorization": f"Bearer {settings.auth.token}"} if settings.auth.enabled else {}
        api_context = await playwright.request.new_context(extra_http_headers=api_headers)
        try:
            real_config_response = await api_context.get(f"{base_url}api/config")
            if real_config_response.status != 200:
                raise AssertionError(f"/api/config returned HTTP {real_config_response.status} for the canonical version probe")
            real_config = await real_config_response.json()
        finally:
            await api_context.dispose()
        if real_config.get("version") != canonical_version:
            raise AssertionError(
                f"/api/config reports version {real_config.get('version')!r}, repo VERSION is {canonical_version!r}"
            )

        browser = await playwright.chromium.launch(headless=True)

        context = await browser.new_context(service_workers="allow")
        await context.add_init_script(storage_failure_init)
        await context.route("**/api/config", mock_config)
        await context.route("**/api/chat", mock_chat)
        await context.route("**/api/title", mock_title)
        page = await context.new_page()
        await page.goto(base_url, wait_until="networkidle")
        await page.locator("#reactPromptInput").wait_for()

        meta_version = await page.evaluate(
            """() => document.querySelector('meta[name="deepseek-infra-version"]')?.content || ''"""
        )
        if meta_version != canonical_version:
            raise AssertionError(f"served page version meta is {meta_version!r}, repo VERSION is {canonical_version!r}")
        checks["canonicalReleaseVersion"] = "PASS"

        await page.evaluate("() => { window.__checkpointSmokeFailures.conversation = true; }")
        await page.locator("#reactPromptInput").fill("制造一次失败的会话保存")
        await page.locator("button.send-button").click()
        banner = page.locator(".build-update-banner")
        await banner.wait_for(timeout=10_000)
        await banner.get_by_text("对话记录保存失败", exact=True).wait_for(timeout=10_000)
        banner_text = await banner.inner_text()
        if "unknown" in banner_text.lower():
            raise AssertionError(f"flush failure banner fell back to a generic message: {banner_text!r}")
        if not await banner.get_by_role("button", name="重新保存").count():
            raise AssertionError(f"flush failure banner is missing the 重新保存 retry action: {banner_text!r}")
        checks["flushFailureIdentified"] = "PASS"

        blocked = await page.evaluate(beforeunload_probe)
        if not blocked:
            raise AssertionError("beforeunload was not blocked immediately after a failed persistence flush")
        await page.evaluate("() => { window.__checkpointSmokeFailures.conversation = false; }")
        await page.wait_for_timeout(400)
        allowed = await page.evaluate(beforeunload_probe)
        if allowed:
            raise AssertionError("beforeunload stayed blocked with healthy storage and no active reload blockers")
        checks["beforeUnloadBlocksFailedFlush"] = "PASS"

        head_state = await page.evaluate(
            """() => {
              const heads = [];
              for (let i = 0; i < localStorage.length; i += 1) {
                const key = localStorage.key(i);
                if (key && key.startsWith('deepseek-infra.session.v3.head.')) {
                  heads.push({ key, value: localStorage.getItem(key) });
                }
              }
              if (heads.length !== 1) return { ok: false, reason: `expected exactly one v3 head, found ${heads.length}` };
              const conversationId = heads[0].key.slice('deepseek-infra.session.v3.head.'.length);
              try {
                const head = JSON.parse(heads[0].value);
                return { ok: Boolean(head && typeof head.revision === 'string'), conversationId, head };
              } catch (error) {
                return { ok: false, reason: String(error) };
              }
            }"""
        )
        if not head_state["ok"]:
            raise AssertionError(f"healthy flush never committed a v3 session head: {head_state}")
        conversation_id = head_state["conversationId"]
        head_revision = head_state["head"]["revision"]
        snapshot_state = await page.evaluate(
            """({ conversationId, revision }) => {
              const key = `deepseek-infra.session.v3.snapshot.${conversationId}.${revision}`;
              const raw = localStorage.getItem(key);
              if (!raw) return { ok: false, reason: `snapshot missing: ${key}` };
              try {
                const parsed = JSON.parse(raw);
                return {
                  ok: parsed.schemaVersion === 3
                    && parsed.revision === revision
                    && parsed.conversation
                    && Array.isArray(parsed.conversation.messages)
                    && raw.includes('检查点冒烟回复'),
                  reason: '',
                };
              } catch (error) {
                return { ok: false, reason: String(error) };
              }
            }""",
            {"conversationId": conversation_id, "revision": head_revision},
        )
        if not snapshot_state["ok"]:
            raise AssertionError(f"committed checkpoint snapshot is unusable: {snapshot_state}")
        if await page.evaluate("() => localStorage.getItem('deepseek-infra.conversations') !== null"):
            raise AssertionError("legacy conversations key survived a verified v3 checkpoint commit")
        await page.evaluate("() => { window.__checkpointSmokeFailures.head = true; }")
        await page.locator("#reactPromptInput").fill("原子性第二条消息")
        await page.locator("button.send-button").click()
        await page.wait_for_function(
            """({ conversationId, revision }) => {
              const prefix = `deepseek-infra.session.v3.snapshot.${conversationId}.`;
              for (let i = 0; i < localStorage.length; i += 1) {
                const key = localStorage.key(i);
                if (key && key.startsWith(prefix) && !key.endsWith(`.${revision}`)) return true;
              }
              return false;
            }""",
            arg={"conversationId": conversation_id, "revision": head_revision},
            timeout=10_000,
        )
        await page.wait_for_timeout(300)
        head_after = await page.evaluate(
            """(key) => {
              const raw = localStorage.getItem(key);
              try { return JSON.parse(raw)?.revision || null; } catch (error) { return null; }
            }""",
            f"deepseek-infra.session.v3.head.{conversation_id}",
        )
        if head_after != head_revision:
            raise AssertionError(f"torn head write moved the checkpoint head: {head_revision!r} -> {head_after!r}")
        # 撕裂瞬间仍是 4.3.5 契约：快照已核验落盘、head 停在第 N 代。记下撕裂代际的
        # revision（快照键后缀），供重载后核验对账收敛到同一代。
        torn_revisions = await page.evaluate(
            """({ conversationId, headRevision }) => {
              const prefix = `deepseek-infra.session.v3.snapshot.${conversationId}.`;
              const revisions = [];
              for (let i = 0; i < localStorage.length; i += 1) {
                const key = localStorage.key(i);
                if (key && key.startsWith(prefix) && !key.endsWith(`.${headRevision}`)) {
                  revisions.push(key.slice(prefix.length));
                }
              }
              return revisions;
            }""",
            {"conversationId": conversation_id, "headRevision": head_revision},
        )
        if len(torn_revisions) != 1:
            raise AssertionError(f"expected exactly one torn snapshot generation: {torn_revisions}")
        torn_revision = torn_revisions[0]
        # pagehide 应急胶囊带着这份"快照已核验落盘"的脏会话退出；4.3.7 的新
        # Document 使用新 Writer，因此启动对账会以相同逻辑代际、不同 Writer
        # revision 重新提交已验证内容（每份胶囊至多生效一次）。
        await page.reload(wait_until="networkidle")
        await page.locator("#reactPromptInput").wait_for()
        try:
            await page.get_by_text("原子性第二条消息", exact=True).wait_for(timeout=10_000)
            if await page.get_by_text("检查点冒烟回复", exact=True).count() != 2:
                raise AssertionError("restored conversation does not contain exactly the two committed replies")
        except Exception as error:
            restored_state = await page.evaluate(
                """() => {
                  const storage = {};
                  for (let i = 0; i < localStorage.length; i += 1) {
                    const key = localStorage.key(i);
                    if (key.startsWith('deepseek-infra.session') || key.startsWith('deepseek-infra.conversations')) {
                      const value = localStorage.getItem(key) || '';
                      storage[key] = value.length > 200 ? `${value.slice(0, 200)}…(${value.length})` : value;
                    }
                  }
                  return { storage, chat: document.querySelector('.chat-messages')?.textContent || '' };
                }"""
            )
            raise AssertionError(f"capsule-reconciled generation was not restored after reload: {restored_state}") from error
        healed_head = await page.evaluate(
            """(key) => {
              const raw = localStorage.getItem(key);
              try { return JSON.parse(raw)?.revision || null; } catch (error) { return null; }
            }""",
            f"deepseek-infra.session.v3.head.{conversation_id}",
        )
        if (
            not isinstance(healed_head, str)
            or healed_head.partition(".")[0] != torn_revision.partition(".")[0]
        ):
            raise AssertionError(f"capsule reconcile healed to an unexpected generation: {torn_revision!r} -> {healed_head!r}")
        capsules_left = await page.evaluate(
            """() => {
              let count = 0;
              for (let i = 0; i < localStorage.length; i += 1) {
                const key = localStorage.key(i);
                if (key && key.startsWith('deepseek-infra.session.v3.recovery.')) count += 1;
              }
              return count;
            }"""
        )
        if capsules_left != 0:
            raise AssertionError(f"recovery capsule survived a successful startup reconcile: {capsules_left} left")
        checks["conversationCheckpointAtomic"] = "PASS"
        await context.close()

        draft_context = await browser.new_context(service_workers="allow")
        await draft_context.add_init_script(legacy_draft_init)
        await draft_context.route("**/api/config", mock_config)
        draft_page = await draft_context.new_page()
        await draft_page.goto(base_url, wait_until="networkidle")
        await draft_page.locator("#reactPromptInput").wait_for()
        await draft_page.wait_for_function(
            "(expected) => document.querySelector('#reactPromptInput').value === expected",
            arg=legacy_draft_text,
            timeout=10_000,
        )
        await draft_page.reload(wait_until="networkidle")
        await draft_page.locator("#reactPromptInput").wait_for()
        restored_draft = await draft_page.locator("#reactPromptInput").input_value()
        legacy_kept = await draft_page.evaluate("() => sessionStorage.getItem('deepseek:composer-draft:new') !== null")
        scoped_written = await draft_page.evaluate("() => sessionStorage.getItem('deepseek:composer-draft:new:') !== null")
        if restored_draft != legacy_draft_text or not legacy_kept or scoped_written:
            raise AssertionError(
                "legacy draft migration was not lossless: "
                f"restored={restored_draft!r}, legacyKept={legacy_kept}, scopedWritten={scoped_written}"
            )
        checks["legacyDraftMigrationLossless"] = "PASS"
        await draft_context.close()

        scope_context = await browser.new_context(service_workers="allow")
        await scope_context.add_init_script(draft_failure_init)
        await scope_context.route("**/api/config", mock_config)
        await scope_context.route("**/api/chat", mock_chat)
        await scope_context.route("**/api/title", mock_title)
        scope_page = await scope_context.new_page()
        await scope_page.goto(base_url, wait_until="networkidle")
        await scope_page.locator("#reactPromptInput").wait_for()
        await scope_page.locator("#reactPromptInput").fill("作用域切换的第一条消息")
        await scope_page.locator("button.send-button").click()
        await scope_page.get_by_text("检查点冒烟回复", exact=True).wait_for(timeout=10_000)
        scope_draft_text = "切走再切回仍然保留的草稿"
        await scope_page.locator("#reactPromptInput").fill(scope_draft_text)
        await scope_page.wait_for_timeout(400)
        await scope_page.locator("button.new-chat-button").click()
        await scope_page.wait_for_function(
            "() => document.querySelector('#reactPromptInput').value === ''",
            timeout=5_000,
        )
        await scope_page.locator(".conversation-open").first.click()
        await scope_page.wait_for_function(
            "(expected) => document.querySelector('#reactPromptInput').value === expected",
            arg=scope_draft_text,
            timeout=5_000,
        )
        persisted_drafts = await scope_page.evaluate(
            "() => Object.keys(sessionStorage).filter((key) => key.startsWith('deepseek:composer-draft:')).length"
        )
        if persisted_drafts != 0:
            raise AssertionError(f"draft unexpectedly reached failing sessionStorage: {persisted_drafts} keys")
        checks["scopeSwitchDraftRetained"] = "PASS"
        await scope_context.close()

        fallback_messages = [
            {"id": "u7", "role": "user", "content": "回退世代用户消息", "createdAt": 1},
            {"id": "a7", "role": "assistant", "content": "回退世代助手回复", "createdAt": 2},
        ]
        fallback_context = await browser.new_context(service_workers="allow")
        await fallback_context.add_init_script(
            seed_storage_script(
                {
                    "deepseek-infra.session.v2.head": "5",
                    "deepseek-infra.session.v2.snapshot.5": '{"schemaVersion":2,"generation":5,"conversations":[corrupt',
                    "deepseek-infra.session.v2.snapshot.4": checkpoint_snapshot(
                        4, "conv-fallback", fallback_messages, "回退会话"
                    ),
                }
            )
        )
        await fallback_context.route("**/api/config", mock_config)
        fallback_page = await fallback_context.new_page()
        await fallback_page.goto(base_url, wait_until="networkidle")
        await fallback_page.locator("#reactPromptInput").wait_for()
        await fallback_page.get_by_text("回退世代助手回复", exact=True).wait_for(timeout=10_000)
        checks["checkpointFallbackRecovered"] = "PASS"
        await fallback_context.close()

        interrupted_messages = [
            {"id": "u8", "role": "user", "content": "中断恢复用户消息", "createdAt": 1},
            {
                "id": "a8",
                "role": "assistant",
                "content": "生成到一半的回答",
                "createdAt": 2,
                "phase": "answering",
                "streaming": True,
            },
        ]
        interrupted_context = await browser.new_context(service_workers="allow")
        await interrupted_context.add_init_script(
            seed_storage_script(
                {
                    "deepseek-infra.session.v2.head": "1",
                    "deepseek-infra.session.v2.snapshot.1": checkpoint_snapshot(
                        1, "conv-interrupted", interrupted_messages, "中断会话"
                    ),
                }
            )
        )
        await interrupted_context.route("**/api/config", mock_config)
        interrupted_page = await interrupted_context.new_page()
        await interrupted_page.goto(base_url, wait_until="networkidle")
        await interrupted_page.locator("#reactPromptInput").wait_for()
        await interrupted_page.get_by_text("生成到一半的回答", exact=True).wait_for(timeout=10_000)
        await interrupted_page.get_by_role("button", name="继续生成").wait_for(timeout=10_000)
        await interrupted_page.get_by_text("生成已由用户停止", exact=False).wait_for(timeout=10_000)
        interrupted_notes = await interrupted_page.locator(
            "p.system-note", has_text="页面关闭或刷新时生成被中断。"
        ).count()
        if interrupted_notes != 1:
            raise AssertionError(f"interrupted checkpoint note rendered {interrupted_notes} times, expected 1")
        if await interrupted_page.locator(".stream-dot").count() != 0:
            raise AssertionError("restored interrupted message still renders an in-flight stream indicator")
        if await interrupted_page.locator("button.stop-button").count() != 0:
            raise AssertionError("restored interrupted message still renders a stop-generation button")
        if await interrupted_page.get_by_text("正在回答", exact=True).count() != 0:
            raise AssertionError("restored interrupted message still claims to be answering")
        checks["interruptedStreamRecoveredHonestly"] = "PASS"
        await interrupted_context.close()

        agent_requests: list[dict[str, str]] = []

        async def mock_agent_runs(route: Any) -> None:
            method = route.request.method
            path = urlsplit(route.request.url).path
            agent_requests.append({"method": method, "path": path})
            if method == "GET" and path == "/api/agent-runs/run-smoke-missing":
                await route.fulfill(
                    status=404,
                    content_type="application/json",
                    body=json.dumps({"error": "agent run not found"}),
                )
                return
            await route.fulfill(
                status=500,
                content_type="application/json",
                body=json.dumps({"error": "unexpected agent run call"}),
            )

        agent_messages = [
            {"id": "u9", "role": "user", "content": "代理对账用户消息", "createdAt": 1},
            {
                "id": "a9",
                "role": "assistant",
                "content": "Agent 半截输出",
                "createdAt": 2,
                "phase": "answering",
                "agentRunId": "run-smoke-missing",
                "agentRunStatus": "running",
            },
        ]
        agent_context = await browser.new_context(service_workers="allow")
        await agent_context.add_init_script(
            seed_storage_script(
                {
                    "deepseek-infra.session.v2.head": "1",
                    "deepseek-infra.session.v2.snapshot.1": checkpoint_snapshot(
                        1, "conv-agent", agent_messages, "代理会话"
                    ),
                }
            )
        )
        await agent_context.route("**/api/config", mock_config)
        await agent_context.route("**/api/agent-runs**", mock_agent_runs)
        agent_page = await agent_context.new_page()
        await agent_page.goto(base_url, wait_until="networkidle")
        await agent_page.locator("#reactPromptInput").wait_for()
        await agent_page.get_by_role("button", name="继续生成").wait_for(timeout=10_000)
        await agent_page.get_by_text("生成已由用户停止", exact=False).wait_for(timeout=10_000)
        if await agent_page.get_by_role("button", name="恢复 Agent Run").count() != 0:
            raise AssertionError("a missing agent run must settle as interrupted, not orphaned")
        status_gets = [r for r in agent_requests if r["method"] == "GET" and r["path"] == "/api/agent-runs/run-smoke-missing"]
        if len(status_gets) != 1:
            raise AssertionError(f"restored agent run was not reconciled with exactly one read-only GET: {agent_requests}")
        replayed_posts = [r for r in agent_requests if r["method"] == "POST"]
        if replayed_posts:
            raise AssertionError(f"restore replayed paid agent work: {replayed_posts}")
        if any(r["path"].endswith("/stream") for r in agent_requests):
            raise AssertionError(f"missing agent run still attached a stream: {agent_requests}")
        checks["agentRunReconciledWithoutReplay"] = "PASS"
        await agent_context.close()

        bfcache_context = await browser.new_context(service_workers="allow")
        await bfcache_context.route("**/api/config", mock_config)
        bfcache_page = await bfcache_context.new_page()
        pointer_hits: list[str] = []
        bfcache_page.on(
            "request",
            lambda request: pointer_hits.append(request.url)
            if urlsplit(request.url).path == "/ui/workspace-assets.json"
            else None,
        )
        await bfcache_page.goto(base_url, wait_until="networkidle")
        await bfcache_page.locator("#reactPromptInput").wait_for()
        await bfcache_page.evaluate(
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
        await bfcache_page.wait_for_timeout(500)
        baseline_pointer_hits = len(pointer_hits)
        if baseline_pointer_hits == 0:
            raise AssertionError("startup build check never fetched /ui/workspace-assets.json")
        navigations: list[str] = []
        bfcache_page.on(
            "framenavigated",
            lambda frame: navigations.append(frame.url) if frame == bfcache_page.main_frame else None,
        )
        await bfcache_page.evaluate("() => { window.__bfcacheSmokeMarker = 'alive'; }")
        registrations_before = await bfcache_page.evaluate(registration_snapshot)
        if len(registrations_before) != 1:
            raise AssertionError(f"expected exactly one service worker registration per build, got {registrations_before}")
        await bfcache_page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))")
        deadline = time.monotonic() + 10
        while len(pointer_hits) <= baseline_pointer_hits and time.monotonic() < deadline:
            await bfcache_page.wait_for_timeout(100)
        if len(pointer_hits) <= baseline_pointer_hits:
            raise AssertionError("persisted pageshow did not force a fresh build re-check of /ui/workspace-assets.json")
        await bfcache_page.wait_for_timeout(300)
        if navigations:
            raise AssertionError(f"bfcache resync navigated or reloaded the page: {navigations}")
        if await bfcache_page.evaluate("() => window.__bfcacheSmokeMarker") != "alive":
            raise AssertionError("bfcache resync reloaded the page (marker lost)")
        registrations_after = await bfcache_page.evaluate(registration_snapshot)
        if registrations_after != registrations_before:
            raise AssertionError(
                f"bfcache resync duplicated service worker registrations: {registrations_before} -> {registrations_after}"
            )
        checks["bfcacheRuntimeResynchronized"] = "PASS"
        await bfcache_context.close()

        await browser.close()
    return checks


async def run_cross_tab_checkpoint_smoke(base_url: str) -> dict[str, str]:
    """Exercise the v4.3.7 replica-convergence persistence contracts in a real browser."""
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import FilePayload
    from playwright.async_api import async_playwright

    checks: dict[str, str] = {}
    reply_text = "检查点冒烟回复"
    v3_head_prefix = "deepseek-infra.session.v3.head."
    v3_snapshot_prefix = "deepseek-infra.session.v3.snapshot."
    v3_recovery_prefix = "deepseek-infra.session.v3.recovery."
    tab_selection_key = "deepseek-infra.current-conversation.v3"
    restore_release = asyncio.Event()
    restore_requested = asyncio.Event()
    chat_request_count = 0

    config_payload = {
        "hasServerKey": True,
        "hasSearch": False,
        "version": VERSION,
        "defaultModel": "deepseek-v4-pro",
        "models": ["deepseek-v4-pro", "deepseek-v4-flash"],
        "modelRoutes": {},
        "computerUrl": base_url,
        "phoneUrl": base_url,
        "uploadLimits": {"fileMaxBytes": 200_000_000, "requestMaxBytes": 220_000_000, "maxFiles": 8},
    }

    async def mock_config(route: Any) -> None:
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(config_payload))

    async def mock_chat(route: Any) -> None:
        nonlocal chat_request_count
        chat_request_count += 1
        try:
            request_data = route.request.post_data_json
        except (json.JSONDecodeError, TypeError):
            request_data = {}
        messages = request_data.get("messages", []) if isinstance(request_data, dict) else []
        if any(isinstance(message, dict) and message.get("content") == "Fence restore stream" for message in messages):
            restore_requested.set()
            await restore_release.wait()
            try:
                await route.abort("aborted")
            except PlaywrightError:
                pass
            return
        body = "\n".join(
            [
                json.dumps({"type": "content", "text": reply_text}),
                json.dumps({"type": "done", "content": reply_text, "model": "deepseek-v4-pro", "usage": {}}),
                "",
            ]
        )
        await route.fulfill(status=200, headers={"Content-Type": "application/x-ndjson"}, body=body)

    async def mock_title(route: Any) -> None:
        await route.fulfill(status=200, content_type="application/json", body=json.dumps({"title": "检查点冒烟"}))

    # 会话级 head 阻断（每页面 window 独立）：window.__staleSmokeBlockedHeadCid 指向的会话，
    # 其 head 键写入抛 QuotaExceededError——快照照常落盘，head 永远推不进。
    # 该标签页因此恒为 stale 写入者：冲突路径（分支快照 + 冲突指针，不碰 head）与
    # tombstone 拒绝路径（恢复副本写新 cid 的 head，不受阻断）都不受注入影响，全程无需
    # 解除开关，时序完全确定。
    stale_failure_init = """
{
  const originalSetItem = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {
    const blocked = window.__staleSmokeBlockedHeadCid || '';
    if (blocked && this === window.localStorage && String(key) === 'deepseek-infra.session.v3.head.' + blocked) {
      throw new DOMException('simulated smoke storage failure', 'QuotaExceededError');
    }
    return originalSetItem.call(this, key, value);
  };
}
"""

    # 只拦 head / snapshot 键（恢复胶囊键放行）：提交必然失败，pagehide 只能写胶囊。
    capsule_failure_init = """
{
  const originalSetItem = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {
    if (window.__capsuleSmokeFailCheckpoint && this === window.localStorage) {
      const name = String(key);
      if (name.startsWith('deepseek-infra.session.v3.snapshot.') || name.startsWith('deepseek-infra.session.v3.head.')) {
        throw new DOMException('simulated smoke storage failure', 'QuotaExceededError');
      }
    }
    return originalSetItem.call(this, key, value);
  };
}
"""

    # 存储压力注入：写入值只要含 data:image 预览就抛 QuotaExceededError，触发压缩重试链。
    quota_failure_init = """
{
  const originalSetItem = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {
    if (this === window.localStorage && String(value).includes('data:image')) {
      throw new DOMException('simulated smoke quota pressure', 'QuotaExceededError');
    }
    return originalSetItem.call(this, key, value);
  };
}
"""

    # 4.3.7 无锁副本探针：禁用 Web Locks 与跨页广播，让两个 Document 从同一
    # base 独立提交；共享 localStorage 中的 immutable Proposal 负责确定性收敛。
    lock_free_init = """
{
  Object.defineProperty(navigator, 'locks', { configurable: true, value: undefined });
  class SilentBroadcastChannel {
    postMessage() {}
    addEventListener() {}
    removeEventListener() {}
    close() {}
  }
  Object.defineProperty(window, 'BroadcastChannel', {
    configurable: true,
    value: SilentBroadcastChannel,
  });
  const originalSetItem = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {
    const barrierConversationId = window.__lockFreeBarrierCid || '';
    const name = String(key);
    if (barrierConversationId
        && this === window.localStorage
        && name === `deepseek-infra.session.v3.head.${barrierConversationId}`) {
      let head = null;
      try { head = JSON.parse(String(value)); } catch (error) {}
      const parent = head && typeof head.parentRevision === 'string' ? head.parentRevision : 'root';
      const proposalPrefix =
        `deepseek-infra.session.v3.proposal.${barrierConversationId}.${parent}.`;
      let proposals = 0;
      for (let index = 0; index < localStorage.length; index += 1) {
        const candidate = localStorage.key(index);
        if (candidate && candidate.startsWith(proposalPrefix)) proposals += 1;
      }
      // 被动观测：不阻断 head 写入。胞兄弟 Proposal 落盘时序不确定，阻断 throw
      // 会把单次提交变成无可恢复的永久失败（autosave 无重试机制）。最终收敛与冲突
      // 账本由下方探针 wait_for_function 核验。
    }
    return originalSetItem.call(this, key, value);
  };
}
"""

    # 锁实现先执行 callback、随后让锁 Promise 失败。正确实现必须把异常原样上抛；
    # 若误走 Proposal fallback，Storage 探针会在同一 task 内观测到第二次写入。
    lock_callback_once_init = """
{
  window.__armLockCallbackFailure = false;
  window.__lockCallbackInvocations = 0;
  window.__postCallbackFallbackWrites = 0;
  window.__watchPostCallbackFallback = false;
  const originalSetItem = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {
    const name = String(key);
    if (window.__watchPostCallbackFallback
        && this === window.localStorage
        && (name.startsWith('deepseek-infra.session.v3.snapshot.')
            || name.startsWith('deepseek-infra.session.v3.proposal.')
            || name.startsWith('deepseek-infra.session.v3.head.'))) {
      window.__postCallbackFallbackWrites += 1;
    }
    return originalSetItem.call(this, key, value);
  };
  const nativeLocks = navigator.locks;
  Object.defineProperty(navigator, 'locks', {
    configurable: true,
    value: {
      request: async function (name, options, callback) {
        if (!window.__armLockCallbackFailure) {
          return nativeLocks.request(name, options, callback);
        }
        window.__armLockCallbackFailure = false;
        window.__lockCallbackInvocations += 1;
        await callback();
        window.__watchPostCallbackFallback = true;
        setTimeout(() => { window.__watchPostCallbackFallback = false; }, 0);
        throw new Error('simulated lock transport failure after callback');
      },
    },
  });
}
"""

    # 慢速流探针：fetch 拦截 /api/chat，用真实 ReadableStream 每 500ms 吐一个增量；
    # 同时给所有 V3 快照键的 setItem 调用打上 performance.now() 时间戳。
    stream_chunks = ["流式", "节流", "冒烟", "校验", "片段", "回复", "完成"]
    stream_full_text = "".join(stream_chunks)
    stream_probe_init = (
        """
window.__v3StreamStartAt = 0;
window.__v3StreamDoneAt = 0;
window.__v3SnapshotWriteTimes = [];
{
  const originalSetItem = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) {
    if (this === window.localStorage && String(key).startsWith('deepseek-infra.session.v3.snapshot.')) {
      window.__v3SnapshotWriteTimes.push(performance.now());
    }
    return originalSetItem.call(this, key, value);
  };
  const originalFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = String(typeof input === 'string' ? input : (input && input.url) || '');
    if (!url.includes('/api/chat')) return originalFetch(input, init);
    window.__v3StreamStartAt = performance.now();
    const chunks = """
        + json.dumps(stream_chunks, ensure_ascii=False)
        + """;
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start(controller) {
        let index = 0;
        const push = () => {
          if (index < chunks.length) {
            controller.enqueue(encoder.encode(JSON.stringify({ type: 'content', text: chunks[index] }) + '\\n'));
            index += 1;
            setTimeout(push, 500);
            return;
          }
          window.__v3StreamDoneAt = performance.now();
          controller.enqueue(encoder.encode(JSON.stringify({ type: 'done', content: chunks.join(''), model: 'deepseek-v4-pro', usage: {} }) + '\\n'));
          controller.close();
        };
        setTimeout(push, 500);
      },
    });
    return Promise.resolve(new Response(stream, { status: 200, headers: { 'Content-Type': 'application/x-ndjson' } }));
  };
}
"""
    )

    # 找到 head 快照同时包含 user_text 且回复至少出现 min_replies 次的会话分片：
    # 回复次数把"结算提交"（含本轮回复）与"流式中的中间提交"（不含本轮回复）区分开。
    settled_head_js = """
({ userText, reply, minReplies }) => {
  const headPrefix = 'deepseek-infra.session.v3.head.';
  const snapshotPrefix = 'deepseek-infra.session.v3.snapshot.';
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i);
    if (!key || !key.startsWith(headPrefix)) continue;
    let head = null;
    try { head = JSON.parse(localStorage.getItem(key) || ''); } catch (error) { continue; }
    if (!head || typeof head.revision !== 'string') continue;
    const conversationId = key.slice(headPrefix.length);
    const raw = localStorage.getItem(`${snapshotPrefix}${conversationId}.${head.revision}`);
    if (!raw || !raw.includes(userText)) continue;
    if (raw.split(reply).length - 1 < minReplies) continue;
    return { conversationId, revision: head.revision, writerId: head.writerId || '' };
  }
  return null;
}
"""

    read_heads_js = """
() => {
  const headPrefix = 'deepseek-infra.session.v3.head.';
  const heads = [];
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i);
    if (!key || !key.startsWith(headPrefix)) continue;
    try {
      const head = JSON.parse(localStorage.getItem(key) || '');
      if (head && typeof head.revision === 'string') {
        heads.push({ conversationId: key.slice(headPrefix.length), revision: head.revision, writerId: head.writerId || '' });
      }
    } catch (error) {}
  }
  return heads;
}
"""

    keys_with_prefix_js = """
(prefix) => {
  const keys = [];
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i);
    if (key && key.startsWith(prefix)) keys.push(key);
  }
  return keys;
}
"""

    read_head_js = """
(conversationId) => {
  const raw = localStorage.getItem(`deepseek-infra.session.v3.head.${conversationId}`);
  try { return raw ? JSON.parse(raw) : null; } catch (error) { return null; }
}
"""

    read_conflict_js = """
(conversationId) => {
  const raw = localStorage.getItem(`deepseek-infra.session.v3.conflict.${conversationId}`);
  try { return raw ? JSON.parse(raw) : null; } catch (error) { return null; }
}
"""

    # 按标题后缀 + 内容标记定位一个分片（冲突副本 / 恢复副本的 id 不可预知）。
    suffixed_head_js = """
({ suffix, marker }) => {
  const headPrefix = 'deepseek-infra.session.v3.head.';
  const snapshotPrefix = 'deepseek-infra.session.v3.snapshot.';
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i);
    if (!key || !key.startsWith(headPrefix)) continue;
    let head = null;
    try { head = JSON.parse(localStorage.getItem(key) || ''); } catch (error) { continue; }
    if (!head || typeof head.revision !== 'string') continue;
    const conversationId = key.slice(headPrefix.length);
    const raw = localStorage.getItem(`${snapshotPrefix}${conversationId}.${head.revision}`);
    if (!raw || !raw.includes(marker)) continue;
    try {
      const parsed = JSON.parse(raw);
      const title = parsed && parsed.conversation && typeof parsed.conversation.title === 'string'
        ? parsed.conversation.title
        : '';
      if (title.endsWith(suffix)) return { conversationId, revision: head.revision, title };
    } catch (error) {}
  }
  return null;
}
"""

    def noisy_png_payload(size: int = 512, seed: int = 20260726) -> bytes:
        """生成确定性的随机噪声 PNG（纯标准库编码），噪声在任何尺寸下都压不出小 JPEG。"""
        rng = random.Random(seed)
        raw = bytearray()
        for _row in range(size):
            raw.append(0)
            raw.extend(rng.randbytes(size * 3))

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")

    async def send_message(page: Any, text: str) -> None:
        await page.locator("#reactPromptInput").fill(text)
        await page.locator("button.send-button").click()

    async def wait_settled_head(page: Any, user_text: str, min_replies: int) -> dict[str, str]:
        handle = await page.wait_for_function(
            settled_head_js,
            arg={"userText": user_text, "reply": reply_text, "minReplies": min_replies},
            timeout=15_000,
        )
        value = await handle.json_value()
        if not isinstance(value, dict) or not value.get("conversationId"):
            raise AssertionError(f"settled checkpoint head containing {user_text!r} never appeared")
        return value

    async def read_heads(page: Any) -> list[dict[str, str]]:
        heads = await page.evaluate(read_heads_js)
        return list(heads) if isinstance(heads, list) else []

    def revision_seq(revision: str) -> int:
        seq, _, _ = revision.partition(".")
        return int(seq) if seq.isdigit() else 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        # ---- 1. crossTabDisjointWritesPreserved：同一 context 两个标签页写互不相同的会话分片 ----
        disjoint_context = await browser.new_context(service_workers="allow")
        await disjoint_context.route("**/api/config", mock_config)
        await disjoint_context.route("**/api/chat", mock_chat)
        await disjoint_context.route("**/api/title", mock_title)
        page_a = await disjoint_context.new_page()
        await page_a.goto(base_url, wait_until="networkidle")
        await page_a.locator("#reactPromptInput").wait_for()
        await send_message(page_a, "标签页甲的第一条消息")
        head_a1 = await wait_settled_head(page_a, "标签页甲的第一条消息", 1)
        conversation_a = head_a1["conversationId"]

        page_b = await disjoint_context.new_page()
        await page_b.goto(base_url, wait_until="networkidle")
        await page_b.locator("#reactPromptInput").wait_for()
        await page_b.get_by_text("标签页甲的第一条消息", exact=True).wait_for(timeout=10_000)
        await page_b.locator("button.new-chat-button").click()
        await send_message(page_b, "标签页乙的第一条消息")
        head_b1 = await wait_settled_head(page_b, "标签页乙的第一条消息", 1)
        conversation_b = head_b1["conversationId"]
        heads_after_pair = await read_heads(page_a)
        if conversation_a == conversation_b or len(heads_after_pair) != 2:
            raise AssertionError(f"disjoint tab writes did not produce two shards: {heads_after_pair}")

        await send_message(page_a, "标签页甲的第二条消息")
        head_a2 = await wait_settled_head(page_a, "标签页甲的第二条消息", 2)
        await send_message(page_b, "标签页乙的第二条消息")
        head_b2 = await wait_settled_head(page_b, "标签页乙的第二条消息", 2)
        heads_final = await read_heads(page_a)
        if len(heads_final) != 2:
            raise AssertionError(f"further edits leaked an extra shard: {heads_final}")
        if head_a2["conversationId"] != conversation_a or head_b2["conversationId"] != conversation_b:
            raise AssertionError(f"further edits landed on the wrong shard: {head_a2} vs {head_b2}")
        if revision_seq(head_a2["revision"]) <= revision_seq(head_a1["revision"]):
            raise AssertionError(f"tab A head did not advance: {head_a1} -> {head_a2}")
        if revision_seq(head_b2["revision"]) <= revision_seq(head_b1["revision"]):
            raise AssertionError(f"tab B head did not advance: {head_b1} -> {head_b2}")
        checks["crossTabDisjointWritesPreserved"] = "PASS"
        if (
            head_a2.get("writerId") == head_b2.get("writerId")
            or len(head_a2.get("writerId", "")) < 32
            or len(head_b2.get("writerId", "")) < 32
        ):
            raise AssertionError(f"document writers were not isolated UUID identities: {head_a2} vs {head_b2}")
        checks["duplicateTabWriterIdentityRotated"] = "PASS"
        await disjoint_context.close()

        # ---- 2/3/4. 冲突检测、stale 写入者不得推进 head、冲突分支可物化为副本 ----
        conflict_context = await browser.new_context(service_workers="allow")
        await conflict_context.add_init_script(stale_failure_init)
        await conflict_context.route("**/api/config", mock_config)
        await conflict_context.route("**/api/chat", mock_chat)
        await conflict_context.route("**/api/title", mock_title)
        page_a = await conflict_context.new_page()
        await page_a.goto(base_url, wait_until="networkidle")
        await page_a.locator("#reactPromptInput").wait_for()
        await send_message(page_a, "跨标签页冲突基础消息")
        base_head = await wait_settled_head(page_a, "跨标签页冲突基础消息", 1)
        conflict_cid = base_head["conversationId"]

        page_b = await conflict_context.new_page()
        await page_b.goto(base_url, wait_until="networkidle")
        await page_b.locator("#reactPromptInput").wait_for()
        await page_b.get_by_text("跨标签页冲突基础消息", exact=True).wait_for(timeout=10_000)
        # 只阻断 B 页面对该会话的 head 写入：B 的编辑永远推不进共享 head（恒为 stale
        # 写入者），但冲突分支 / 副本分片的写入不受影响——全程无需解除阻断，无时序竞态。
        await page_b.evaluate(
            "(conversationId) => { window.__staleSmokeBlockedHeadCid = conversationId; }", conflict_cid
        )
        await send_message(page_b, "stale 编辑一")
        await page_b.get_by_text(reply_text, exact=True).nth(1).wait_for(timeout=10_000)

        await send_message(page_a, "胜者编辑")
        winner_head = await wait_settled_head(page_a, "胜者编辑", 2)
        if winner_head["conversationId"] != conflict_cid:
            raise AssertionError(f"winner edit landed on a different shard: {winner_head}")
        winner_revision = winner_head["revision"]

        # B 的第二次编辑：防抖提交在排他锁内重读共享 head → 过期 base → 写冲突分支 +
        # 冲突指针（冲突路径根本不写 head）；紧随的结算提交 base 已跟进胜方，试图推进
        # head 时被注入阻断——共享 head 因此确定性停在胜方 revision。
        await send_message(page_b, "stale 编辑二")
        notice = page_b.locator(".chat-notice", has_text="隔离冲突分支")
        await notice.wait_for(timeout=10_000)
        pointer_handle = await page_b.wait_for_function(read_conflict_js, arg=conflict_cid, timeout=10_000)
        pointer = await pointer_handle.json_value()
        if not isinstance(pointer, dict) or not pointer.get("revision"):
            raise AssertionError(f"conflict pointer never materialized for {conflict_cid}: {pointer}")
        if pointer.get("sharedRevision") != winner_revision:
            raise AssertionError(f"conflict pointer does not name the winning head: {pointer} vs {winner_revision!r}")
        branch_raw = await page_b.evaluate(
            """({ conversationId, revision }) =>
              localStorage.getItem(`deepseek-infra.session.v3.snapshot.${conversationId}.${revision}`) || ''""",
            {"conversationId": conflict_cid, "revision": pointer["revision"]},
        )
        if "stale 编辑一" not in branch_raw or "stale 编辑二" not in branch_raw:
            raise AssertionError(f"conflict branch snapshot lost stale edits: {branch_raw[:200]!r}")
        checks["sameConversationConflictDetected"] = "PASS"

        head_after_conflict = await page_b.evaluate(read_head_js, conflict_cid)
        if not isinstance(head_after_conflict, dict) or head_after_conflict.get("revision") != winner_revision:
            raise AssertionError(f"stale writer moved the shared head: {winner_revision!r} -> {head_after_conflict}")
        checks["staleWriterCannotAdvanceHead"] = "PASS"

        # 第三个 Document 从胜方 Head 打开；胜方再推进后，C 的旧 base 形成第二条独立 Ledger。
        page_c = await conflict_context.new_page()
        await page_c.add_init_script(lock_free_init)
        await page_c.goto(base_url, wait_until="networkidle")
        await page_c.locator("#reactPromptInput").wait_for()
        await page_c.get_by_text("胜者编辑", exact=True).wait_for(timeout=10_000)
        await send_message(page_a, "胜者编辑二")
        winner_head_2 = await wait_settled_head(page_a, "胜者编辑二", 3)
        winner_revision = winner_head_2["revision"]
        await send_message(page_c, "第三标签页失败分支")
        ledger_handle = await page_c.wait_for_function(
            """(conversationId) => {
              try {
                const value = JSON.parse(
                  localStorage.getItem(`deepseek-infra.session.v3.conflict-index.${conversationId}`) || '{}'
                );
                return Array.isArray(value.conflictIds) && value.conflictIds.length === 2 ? value.conflictIds : null;
              } catch (error) { return null; }
            }""",
            arg=conflict_cid,
            timeout=10_000,
        )
        ledger_ids = await ledger_handle.json_value()
        if not isinstance(ledger_ids, list) or len(ledger_ids) != 2:
            raise AssertionError(f"concurrent conflict branches overwrote each other: {ledger_ids}")
        checks["multipleConflictBranchesRetained"] = "PASS"

        # 解除注入后继续编辑失败分支：4.3.7 必须仍只推进 branch revision，不能借下一次输入覆盖 Head。
        await page_b.evaluate("() => { window.__staleSmokeBlockedHeadCid = ''; }")
        await send_message(page_b, "stale 隔离后续编辑")
        await page_b.get_by_text(reply_text, exact=True).nth(3).wait_for(timeout=10_000)
        head_after_branch_edit = await page_b.evaluate(read_head_js, conflict_cid)
        if not isinstance(head_after_branch_edit, dict) or head_after_branch_edit.get("revision") != winner_revision:
            raise AssertionError(
                f"isolated conflict branch advanced shared Head: {winner_revision!r} -> {head_after_branch_edit}"
            )
        checks["conflictBranchCannotAdvanceSharedHead"] = "PASS"

        conflict_entries = await page_b.evaluate(
            """({ conversationId, writerId }) => {
              try {
                const parsed = JSON.parse(
                  localStorage.getItem(`deepseek-infra.session.v3.conflict-index.${conversationId}`) || '{}'
                );
                const ids = Array.isArray(parsed.conflictIds) ? parsed.conflictIds : [];
                return ids.map((conflictId) => {
                  try {
                    return JSON.parse(
                      localStorage.getItem(
                        `deepseek-infra.session.v3.conflict.${conversationId}.${conflictId}`
                      ) || '{}'
                    );
                  } catch (error) { return {}; }
                }).filter((entry) => entry.writerSessionId === writerId);
              } catch (error) { return []; }
            }""",
            {"conversationId": conflict_cid, "writerId": pointer.get("writerId")},
        )
        if len(conflict_entries) != 1:
            raise AssertionError(f"could not identify B's durable conflict before resolution: {conflict_entries}")
        conflict_id = conflict_entries[0]["conflictId"]
        conflict_copy_id = f"{conflict_cid}.conflict.{conflict_id}"
        await page_b.evaluate(
            """(copyId) => {
              window.__failConflictCopyHead = true;
              const inheritedSetItem = Storage.prototype.setItem;
              Storage.prototype.setItem = function (key, value) {
                if (window.__failConflictCopyHead
                    && this === window.localStorage
                    && String(key) === `deepseek-infra.session.v3.head.${copyId}`) {
                  throw new DOMException('simulated conflict resolution crash', 'QuotaExceededError');
                }
                return inheritedSetItem.call(this, key, value);
              };
            }""",
            conflict_copy_id,
        )
        await page_b.get_by_role("button", name="保留副本").click()
        await page_b.wait_for_timeout(300)
        failed_resolution = await page_b.evaluate(
            """({ conversationId, conflictId, copyId }) => ({
              ledger: localStorage.getItem(
                `deepseek-infra.session.v3.conflict.${conversationId}.${conflictId}`
              ),
              copyHead: localStorage.getItem(`deepseek-infra.session.v3.head.${copyId}`),
            })""",
            {"conversationId": conflict_cid, "conflictId": conflict_id, "copyId": conflict_copy_id},
        )
        if not failed_resolution.get("ledger") or failed_resolution.get("copyHead") is not None:
            raise AssertionError(f"failed conflict-copy transaction released its source: {failed_resolution}")
        checks["conflictResolutionCrashRecoverable"] = "PASS"
        await page_b.evaluate("() => { window.__failConflictCopyHead = false; }")
        await page_b.get_by_role("button", name="保留副本").click()
        try:
            copy_handle = await page_b.wait_for_function(
                suffixed_head_js, arg={"suffix": "（冲突副本）", "marker": "stale 编辑一"}, timeout=10_000
            )
        except Exception as error:
            copy_state = await page_b.evaluate(
                """({ conversationId, conflictId, copyId }) => {
                  const storage = {};
                  for (let index = 0; index < localStorage.length; index += 1) {
                    const key = localStorage.key(index);
                    if (key && (key.includes(copyId) || key.includes(conflictId))) {
                      storage[key] = localStorage.getItem(key);
                    }
                  }
                  return {
                    storage,
                    notice: document.querySelector('.chat-notice')?.textContent || '',
                    buttons: Array.from(document.querySelectorAll('.chat-notice button')).map((button) => ({
                      text: button.textContent || '',
                      disabled: button.disabled,
                    })),
                    index: localStorage.getItem(
                      `deepseek-infra.session.v3.conflict-index.${conversationId}`
                    ),
                  };
                }""",
                {"conversationId": conflict_cid, "conflictId": conflict_id, "copyId": conflict_copy_id},
            )
            raise AssertionError(f"conflict-copy retry did not converge: {copy_state}") from error
        conflict_copy = await copy_handle.json_value()
        if not isinstance(conflict_copy, dict) or conflict_copy.get("conversationId") in (None, conflict_cid):
            raise AssertionError(f"conflict branch was not materialized as an independent copy: {conflict_copy}")
        checks["conflictCopyCommittedBeforeRelease"] = "PASS"
        await page_b.wait_for_function(
            """({ conversationId, conflictId }) =>
              localStorage.getItem(
                `deepseek-infra.session.v3.conflict.${conversationId}.${conflictId}`
              ) === null""",
            arg={"conversationId": conflict_cid, "conflictId": conflict_id},
            timeout=10_000,
        )
        remaining_conflicts = await page_b.evaluate(
            """(conversationId) => {
              try {
                const parsed = JSON.parse(
                  localStorage.getItem(`deepseek-infra.session.v3.conflict-index.${conversationId}`) || '{}'
                );
                return Array.isArray(parsed.conflictIds) ? parsed.conflictIds.length : 0;
              } catch (error) { return 0; }
            }""",
            conflict_cid,
        )
        if remaining_conflicts != 1:
            raise AssertionError(f"resolving one conflict disturbed sibling ledger entries: {remaining_conflicts}")
        await page_a.reload(wait_until="networkidle")
        await page_a.locator("#reactPromptInput").wait_for()
        await page_b.reload(wait_until="networkidle")
        await page_b.locator("#reactPromptInput").wait_for()
        for reloaded in (page_a, page_b):
            await reloaded.locator(".conversation-open").nth(1).wait_for(timeout=10_000)
            if await reloaded.locator(".conversation-open").count() != 2:
                raise AssertionError("original conversation and conflict copy did not both survive a reload")
        await page_b.locator(".conversation-open", has_text="（冲突副本）").click()
        await page_b.get_by_text("stale 编辑一", exact=True).wait_for(timeout=10_000)
        checks["conflictBranchRecoverable"] = "PASS"
        await conflict_context.close()

        # ---- 5. lockFreeSiblingWritesConverged：无 Web Locks 时同胞 Proposal 确定性收敛 ----
        lock_free_context = await browser.new_context(service_workers="allow")
        await lock_free_context.add_init_script(lock_free_init)
        await lock_free_context.route("**/api/config", mock_config)
        await lock_free_context.route("**/api/chat", mock_chat)
        await lock_free_context.route("**/api/title", mock_title)
        page_a = await lock_free_context.new_page()
        await page_a.goto(base_url, wait_until="networkidle")
        await page_a.locator("#reactPromptInput").wait_for()
        await send_message(page_a, "无锁同胞基础消息")
        lock_free_base = await wait_settled_head(page_a, "无锁同胞基础消息", 1)
        lock_free_cid = lock_free_base["conversationId"]
        lock_free_base_revision = lock_free_base["revision"]

        page_b = await lock_free_context.new_page()
        await page_b.goto(base_url, wait_until="networkidle")
        await page_b.locator("#reactPromptInput").wait_for()
        await page_b.get_by_text("无锁同胞基础消息", exact=True).wait_for(timeout=10_000)
        lock_free_current_head = await page_a.evaluate(read_head_js, lock_free_cid)
        if not isinstance(lock_free_current_head, dict) or not lock_free_current_head.get("revision"):
            raise AssertionError(f"lock-free base Head disappeared before the sibling race: {lock_free_current_head}")
        # 标题生成可能在首轮回复结算后追加一个干净 revision；以两个页面都已打开
        # 时的真实共享 Head 为同胞 parent，避免把标题提交误当并发编辑。
        lock_free_base_revision = lock_free_current_head["revision"]
        await page_a.evaluate("(conversationId) => { window.__lockFreeBarrierCid = conversationId; }", lock_free_cid)
        await page_b.evaluate("(conversationId) => { window.__lockFreeBarrierCid = conversationId; }", lock_free_cid)
        await asyncio.gather(
            send_message(page_a, "无锁提案甲"),
            send_message(page_b, "无锁提案乙"),
        )
        await page_a.get_by_text("无锁提案甲", exact=True).wait_for(timeout=10_000)
        await page_b.get_by_text("无锁提案乙", exact=True).wait_for(timeout=10_000)
        lock_free_probe_args = {
            "conversationId": lock_free_cid,
            "parentRevision": lock_free_base_revision,
            "markerA": "无锁提案甲",
            "markerB": "无锁提案乙",
        }
        try:
            lock_free_handle = await page_a.wait_for_function(
                """({ conversationId, parentRevision, markerA, markerB }) => {
              const snapshotPrefix = `deepseek-infra.session.v3.snapshot.${conversationId}.`;
              const proposalPrefix = `deepseek-infra.session.v3.proposal.${conversationId}.${parentRevision}.`;
              const snapshots = [];
              const proposals = [];
              for (let index = 0; index < localStorage.length; index += 1) {
                const key = localStorage.key(index);
                if (!key) continue;
                if (key.startsWith(snapshotPrefix)) {
                  try {
                    const value = JSON.parse(localStorage.getItem(key) || '');
                    if (value && value.parentRevision === parentRevision) {
                      snapshots.push({
                        revision: value.revision,
                        writerId: value.writerId,
                        raw: localStorage.getItem(key) || '',
                      });
                    }
                  } catch (error) {}
                }
                if (key.startsWith(proposalPrefix)) proposals.push(key);
              }
              const hasA = snapshots.some((item) => item.raw.includes(markerA));
              const hasB = snapshots.some((item) => item.raw.includes(markerB));
              let indexValue = null;
              try {
                indexValue = JSON.parse(
                  localStorage.getItem(`deepseek-infra.session.v3.conflict-index.${conversationId}`) || ''
                );
              } catch (error) {}
              const conflictIds = Array.isArray(indexValue && indexValue.conflictIds)
                ? indexValue.conflictIds
                : [];
              let head = null;
              try {
                head = JSON.parse(localStorage.getItem(`deepseek-infra.session.v3.head.${conversationId}`) || '');
              } catch (error) {}
              if (!head || snapshots.length < 2 || !hasA || !hasB || proposals.length < 1 || conflictIds.length < 1) {
                return null;
              }
              return {
                head,
                siblingRevisions: snapshots.map((item) => item.revision),
                siblingWriters: [...new Set(snapshots.map((item) => item.writerId))],
                conflictIds,
                proposalCount: proposals.length,
              };
            }""",
                arg=lock_free_probe_args,
                timeout=15_000,
            )
        except Exception as error:
            lock_free_state = await page_a.evaluate(
                """({ conversationId }) => {
                  const storage = {};
                  for (let index = 0; index < localStorage.length; index += 1) {
                    const key = localStorage.key(index);
                    if (key && key.includes(conversationId)) storage[key] = localStorage.getItem(key);
                  }
                  return { storage, notice: document.querySelector('.chat-notice')?.textContent || '' };
                }""",
                {"conversationId": lock_free_cid},
            )
            raise AssertionError(f"lock-free sibling probe did not converge: {lock_free_state}") from error
        lock_free_probe = await lock_free_handle.json_value()
        if (
            not isinstance(lock_free_probe, dict)
            or len(lock_free_probe.get("siblingWriters") or []) != 2
            or not lock_free_probe.get("head", {}).get("revision")
        ):
            raise AssertionError(f"lock-free sibling Proposals did not converge: {lock_free_probe}")
        shared_a = await page_a.evaluate(read_head_js, lock_free_cid)
        shared_b = await page_b.evaluate(read_head_js, lock_free_cid)
        if shared_a != shared_b:
            raise AssertionError(f"lock-free replicas disagree on shared Head: {shared_a} vs {shared_b}")
        checks["lockFreeSiblingWritesConverged"] = "PASS"
        await lock_free_context.close()

        # ---- 6. lockCallbackExecutedOnce：callback 已开始后的锁异常不得 fallback 重跑 ----
        lock_once_context = await browser.new_context(service_workers="allow")
        await lock_once_context.add_init_script(lock_callback_once_init)
        await lock_once_context.route("**/api/config", mock_config)
        await lock_once_context.route("**/api/chat", mock_chat)
        await lock_once_context.route("**/api/title", mock_title)
        lock_once_page = await lock_once_context.new_page()
        await lock_once_page.goto(base_url, wait_until="networkidle")
        await lock_once_page.locator("#reactPromptInput").wait_for()
        await send_message(lock_once_page, "锁回调基础消息")
        await wait_settled_head(lock_once_page, "锁回调基础消息", 1)
        await lock_once_page.evaluate(
            """() => {
              window.__lockCallbackInvocations = 0;
              window.__postCallbackFallbackWrites = 0;
              window.__armLockCallbackFailure = true;
            }"""
        )
        await send_message(lock_once_page, "锁回调单次消息")
        await lock_once_page.get_by_text("锁回调单次消息", exact=True).wait_for(timeout=10_000)
        await lock_once_page.wait_for_function("() => window.__lockCallbackInvocations === 1", timeout=10_000)
        await lock_once_page.wait_for_timeout(500)
        lock_once_probe = await lock_once_page.evaluate(
            """() => ({
              callbacks: window.__lockCallbackInvocations,
              fallbackWrites: window.__postCallbackFallbackWrites,
            })"""
        )
        if lock_once_probe != {"callbacks": 1, "fallbackWrites": 0}:
            raise AssertionError(f"lock callback was replayed through fallback: {lock_once_probe}")
        checks["lockCallbackExecutedOnce"] = "PASS"
        await lock_once_context.close()

        # ---- 7. degradedHeadSelfHealed：损坏 Head 快照回退 parent，并在锁内修复与隔离 ----
        degraded_context = await browser.new_context(service_workers="allow")
        await degraded_context.route("**/api/config", mock_config)
        await degraded_context.route("**/api/chat", mock_chat)
        await degraded_context.route("**/api/title", mock_title)
        degraded_page = await degraded_context.new_page()
        await degraded_page.goto(base_url, wait_until="networkidle")
        await degraded_page.locator("#reactPromptInput").wait_for()
        await send_message(degraded_page, "降级 Head 有效父快照")
        await wait_settled_head(degraded_page, "降级 Head 有效父快照", 1)
        await send_message(degraded_page, "降级 Head 损坏子快照")
        degraded_head = await wait_settled_head(degraded_page, "降级 Head 损坏子快照", 2)
        degraded_details = await degraded_page.evaluate(
            """(conversationId) => {
              const headKey = `deepseek-infra.session.v3.head.${conversationId}`;
              const head = JSON.parse(localStorage.getItem(headKey) || '');
              const snapshotKey = `deepseek-infra.session.v3.snapshot.${conversationId}.${head.revision}`;
              localStorage.setItem(snapshotKey, 'corrupt{{');
              return head;
            }""",
            degraded_head["conversationId"],
        )
        degraded_parent = degraded_details.get("parentRevision")
        if not degraded_parent:
            raise AssertionError(f"degraded Head did not have a recoverable parent: {degraded_details}")
        await degraded_page.reload(wait_until="networkidle")
        await degraded_page.locator("#reactPromptInput").wait_for()
        await degraded_page.wait_for_function(
            """({ conversationId, advertisedRevision, recoveredRevision }) => {
              let head = null;
              try {
                head = JSON.parse(localStorage.getItem(`deepseek-infra.session.v3.head.${conversationId}`) || '');
              } catch (error) {}
              const quarantine =
                `deepseek-infra.session.v3.quarantine.head.${conversationId}.${advertisedRevision}`;
              return head && head.revision === recoveredRevision && localStorage.getItem(quarantine) !== null;
            }""",
            arg={
                "conversationId": degraded_head["conversationId"],
                "advertisedRevision": degraded_head["revision"],
                "recoveredRevision": degraded_parent,
            },
            timeout=15_000,
        )
        checks["degradedHeadSelfHealed"] = "PASS"
        await degraded_context.close()

        # ---- 8. missingHeadCannotResurrectId：本地已知 base 丢失 Head 时只建恢复副本 ----
        missing_head_context = await browser.new_context(service_workers="allow")
        await missing_head_context.route("**/api/config", mock_config)
        await missing_head_context.route("**/api/chat", mock_chat)
        await missing_head_context.route("**/api/title", mock_title)
        missing_head_page = await missing_head_context.new_page()
        await missing_head_page.goto(base_url, wait_until="networkidle")
        await missing_head_page.locator("#reactPromptInput").wait_for()
        await send_message(missing_head_page, "缺失 Head 基础消息")
        missing_base = await wait_settled_head(missing_head_page, "缺失 Head 基础消息", 1)
        await missing_head_page.evaluate(
            """(conversationId) => {
              localStorage.removeItem(`deepseek-infra.session.v3.head.${conversationId}`);
              localStorage.removeItem(`deepseek-infra.session.v3.tombstone.${conversationId}`);
            }""",
            missing_base["conversationId"],
        )
        await send_message(missing_head_page, "缺失 Head 睡眠标签页编辑")
        await missing_head_page.locator(
            ".chat-notice", has_text="远端已删除，已保留为恢复副本"
        ).wait_for(timeout=10_000)
        missing_recovery_handle = await missing_head_page.wait_for_function(
            suffixed_head_js,
            arg={"suffix": "（恢复副本）", "marker": "缺失 Head 睡眠标签页编辑"},
            timeout=15_000,
        )
        missing_recovery = await missing_recovery_handle.json_value()
        original_missing_head = await missing_head_page.evaluate(read_head_js, missing_base["conversationId"])
        if (
            not isinstance(missing_recovery, dict)
            or missing_recovery.get("conversationId") in (None, missing_base["conversationId"])
            or original_missing_head is not None
        ):
            raise AssertionError(
                f"known missing Head resurrected the original id: original={original_missing_head}, copy={missing_recovery}"
            )
        checks["missingHeadCannotResurrectId"] = "PASS"
        await missing_head_context.close()

        # ---- 5. deletedConversationNotResurrected：tombstone 拒绝过期写入，内容物化为恢复副本 ----
        tombstone_context = await browser.new_context(service_workers="allow")
        await tombstone_context.add_init_script(stale_failure_init)
        await tombstone_context.route("**/api/config", mock_config)
        await tombstone_context.route("**/api/chat", mock_chat)
        await tombstone_context.route("**/api/title", mock_title)
        page_a = await tombstone_context.new_page()
        await page_a.goto(base_url, wait_until="networkidle")
        await page_a.locator("#reactPromptInput").wait_for()
        await send_message(page_a, "墓碑守卫基础消息")
        tombstone_head = await wait_settled_head(page_a, "墓碑守卫基础消息", 1)
        tombstone_cid = tombstone_head["conversationId"]

        page_b = await tombstone_context.new_page()
        await page_b.goto(base_url, wait_until="networkidle")
        await page_b.locator("#reactPromptInput").wait_for()
        await page_b.get_by_text("墓碑守卫基础消息", exact=True).wait_for(timeout=10_000)
        # 与冲突场景同一注入：B 页面推不进该会话的 head，过期编辑只能滞留本地。
        await page_b.evaluate(
            "(conversationId) => { window.__staleSmokeBlockedHeadCid = conversationId; }", tombstone_cid
        )
        await send_message(page_b, "墓碑过期编辑一")
        await page_b.get_by_text(reply_text, exact=True).nth(1).wait_for(timeout=10_000)

        await page_a.locator("button.conversation-tool.danger").first.click()
        await page_a.wait_for_function(
            """(conversationId) =>
              localStorage.getItem(`deepseek-infra.session.v3.tombstone.${conversationId}`) !== null
              && localStorage.getItem(`deepseek-infra.session.v3.head.${conversationId}`) === null""",
            arg=tombstone_cid,
            timeout=10_000,
        )
        baseline_shards = await page_a.evaluate(keys_with_prefix_js, f"{v3_snapshot_prefix}{tombstone_cid}.")

        # B 的下一次保存撞上 tombstone：原 cid 绝不复活（head 阻断之外还有 tombstone 守卫），
        # 本地内容以新 id 恢复副本落盘——副本的 head 不属于被阻断的 cid，写入不受注入影响。
        await send_message(page_b, "墓碑过期编辑二")
        recovery_notice = page_b.locator(".chat-notice", has_text="远端已删除，已保留为恢复副本")
        await recovery_notice.wait_for(timeout=10_000)
        recovered_handle = await page_b.wait_for_function(
            suffixed_head_js, arg={"suffix": "（恢复副本）", "marker": "墓碑过期编辑一"}, timeout=10_000
        )
        recovered = await recovered_handle.json_value()
        if not isinstance(recovered, dict) or recovered.get("conversationId") in (None, tombstone_cid):
            raise AssertionError(f"stale content was not materialized as a recovery copy: {recovered}")
        if await page_b.evaluate(
            "(conversationId) => localStorage.getItem(`deepseek-infra.session.v3.tombstone.${conversationId}`) === null",
            tombstone_cid,
        ):
            raise AssertionError("tombstone disappeared after a refused stale write")
        shards_after = await page_b.evaluate(keys_with_prefix_js, f"{v3_snapshot_prefix}{tombstone_cid}.")
        if not set(shards_after) <= set(baseline_shards) or await page_b.evaluate(read_head_js, tombstone_cid) is not None:
            raise AssertionError(
                f"refused stale write resurrected shard state: head resurrected, snapshots {baseline_shards} -> {shards_after}"
            )
        await page_a.reload(wait_until="networkidle")
        await page_a.locator("#reactPromptInput").wait_for()
        await page_b.reload(wait_until="networkidle")
        await page_b.locator("#reactPromptInput").wait_for()
        for reloaded in (page_a, page_b):
            await reloaded.locator(".conversation-open").first.wait_for(timeout=10_000)
            if await reloaded.locator(".conversation-open").count() != 1:
                raise AssertionError("deleted conversation resurfaced in the history list after reload")
            if await reloaded.locator(".conversation-open", has_text="（恢复副本）").count() != 1:
                raise AssertionError("recovery copy missing from the history list after reload")
        heads_after_delete = await read_heads(page_a)
        if len(heads_after_delete) != 1 or heads_after_delete[0]["conversationId"] != recovered["conversationId"]:
            raise AssertionError(f"deleted conversation left a live head behind: {heads_after_delete}")
        checks["deletedConversationNotResurrected"] = "PASS"
        await tombstone_context.close()

        # ---- 6. tabSelectionRemainsIndependent：远端提交与本地切换都不跨标签页劫持选中 ----
        selection_context = await browser.new_context(service_workers="allow")
        await selection_context.route("**/api/config", mock_config)
        await selection_context.route("**/api/chat", mock_chat)
        await selection_context.route("**/api/title", mock_title)
        page_a = await selection_context.new_page()
        await page_a.goto(base_url, wait_until="networkidle")
        await page_a.locator("#reactPromptInput").wait_for()
        await send_message(page_a, "会话一的种子消息")
        first_head = await wait_settled_head(page_a, "会话一的种子消息", 1)
        conversation_one = first_head["conversationId"]
        await page_a.locator("button.new-chat-button").click()
        await send_message(page_a, "会话二的种子消息")
        second_head = await wait_settled_head(page_a, "会话二的种子消息", 1)
        conversation_two = second_head["conversationId"]

        page_b = await selection_context.new_page()
        await page_b.goto(base_url, wait_until="networkidle")
        await page_b.locator("#reactPromptInput").wait_for()
        await page_b.locator(".conversation-open").nth(1).wait_for(timeout=10_000)
        await page_b.locator(".conversation-open").nth(1).click()
        await page_b.wait_for_function(
            "(expected) => sessionStorage.getItem('deepseek-infra.current-conversation.v3') === expected",
            arg=conversation_one,
            timeout=10_000,
        )
        await page_b.get_by_text("会话一的种子消息", exact=True).wait_for(timeout=10_000)
        await page_b.evaluate(
            """() => {
              window.__conversationSyncSmokeMessages = [];
              window.__conversationSyncSmokeProbe = new BroadcastChannel('deepseek-conversation-sync');
              window.__conversationSyncSmokeProbe.addEventListener('message', (event) => {
                const message = event.data;
                if (message && typeof message === 'object') {
                  window.__conversationSyncSmokeMessages.push(message);
                }
              });
            }"""
        )

        await send_message(page_a, "远端提交不应劫持选中")
        selection_commit = await wait_settled_head(page_a, "远端提交不应劫持选中", 2)
        expected_selection_revision = selection_commit["revision"]
        try:
            # 先等 B 的独立探针收到目标 conversation + revision，再等控制器把同一
            # 提交反映到目标历史项。这样不会把任意会话的“4 条”误当作同步完成，
            # 也不会把广播尚未到达的时间计入 React 状态收敛窗口。
            await page_b.wait_for_function(
                """({ conversationId, revision }) =>
                  Array.isArray(window.__conversationSyncSmokeMessages)
                  && window.__conversationSyncSmokeMessages.some((message) =>
                    message.type === 'conversation_committed'
                    && message.conversationId === conversationId
                    && message.revision === revision)""",
                arg={"conversationId": conversation_two, "revision": expected_selection_revision},
                timeout=15_000,
            )
            await page_b.wait_for_function(
                """(conversationId) => {
                  const button = Array.from(document.querySelectorAll('.conversation-open'))
                    .find((candidate) => candidate.dataset.conversationId === conversationId);
                  return Boolean(button?.querySelector('small')?.textContent?.includes('4 条'));
                }""",
                arg=conversation_two,
                timeout=30_000,
            )
        except Exception as error:
            selection_sync_state = await page_b.evaluate(
                """({ conversationId, expectedRevision, selectionKey }) => {
                  const headKey = `deepseek-infra.session.v3.head.${conversationId}`;
                  let head = null;
                  let snapshot = null;
                  try {
                    head = JSON.parse(localStorage.getItem(headKey) || 'null');
                    if (head && typeof head.revision === 'string') {
                      snapshot = JSON.parse(
                        localStorage.getItem(
                          `deepseek-infra.session.v3.snapshot.${conversationId}.${head.revision}`
                        ) || 'null'
                      );
                    }
                  } catch (parseError) {}
                  return {
                    conversationId,
                    expectedRevision,
                    selectedConversationId: sessionStorage.getItem(selectionKey),
                    visibility: document.visibilityState,
                    receivedMessages: window.__conversationSyncSmokeMessages || [],
                    targetHead: head,
                    targetSnapshotMessageCount: Array.isArray(snapshot?.conversation?.messages)
                      ? snapshot.conversation.messages.length
                      : null,
                    history: Array.from(document.querySelectorAll('.conversation-open')).map((button) => ({
                      conversationId: button.dataset.conversationId || '',
                      text: button.textContent || '',
                      disabled: button.disabled,
                    })),
                    visibleMessages: document.querySelector('.message-list')?.textContent || '',
                  };
                }""",
                arg={
                    "conversationId": conversation_two,
                    "expectedRevision": expected_selection_revision,
                    "selectionKey": tab_selection_key,
                },
            )
            raise AssertionError(f"cross-tab selection sync stalled: {selection_sync_state}") from error
        b_selection = await page_b.evaluate(f"() => sessionStorage.getItem({json.dumps(tab_selection_key)})")
        if b_selection != conversation_one:
            raise AssertionError(f"remote commit hijacked tab B selection: {b_selection!r} != {conversation_one!r}")
        b_visible = await page_b.locator(".message-list").inner_text()
        if "会话一的种子消息" not in b_visible or "远端提交不应劫持选中" in b_visible:
            raise AssertionError("remote commit switched the conversation tab B is viewing")
        a_selection_before = await page_a.evaluate(f"() => sessionStorage.getItem({json.dumps(tab_selection_key)})")
        if a_selection_before != conversation_two:
            raise AssertionError(f"tab A lost its own selection: {a_selection_before!r} != {conversation_two!r}")

        await page_b.locator(".conversation-open").nth(0).click()
        await page_b.wait_for_function(
            "(expected) => sessionStorage.getItem('deepseek-infra.current-conversation.v3') === expected",
            arg=conversation_two,
            timeout=10_000,
        )
        await page_b.get_by_text("远端提交不应劫持选中", exact=True).wait_for(timeout=10_000)
        a_selection_after = await page_a.evaluate(f"() => sessionStorage.getItem({json.dumps(tab_selection_key)})")
        a_visible = await page_a.locator(".message-list").inner_text()
        if a_selection_after != conversation_two or "远端提交不应劫持选中" not in a_visible:
            raise AssertionError("tab B local switch leaked into tab A selection or view")
        checks["tabSelectionRemainsIndependent"] = "PASS"
        await selection_context.close()

        # ---- 7. checkpointCleanupRemainsConstantTime：连发 12 条，快照数量保持有界 ----
        cleanup_context = await browser.new_context(service_workers="allow")
        await cleanup_context.route("**/api/config", mock_config)
        await cleanup_context.route("**/api/chat", mock_chat)
        await cleanup_context.route("**/api/title", mock_title)
        cleanup_page = await cleanup_context.new_page()
        await cleanup_page.goto(base_url, wait_until="networkidle")
        await cleanup_page.locator("#reactPromptInput").wait_for()
        cleanup_cid = ""
        snapshot_counts: list[int] = []
        for turn in range(1, 13):
            burst_text = f"突发消息 {turn:02d}"
            await send_message(cleanup_page, burst_text)
            burst_head = await wait_settled_head(cleanup_page, burst_text, turn)
            cleanup_cid = burst_head["conversationId"]
            shard_keys = await cleanup_page.evaluate(keys_with_prefix_js, f"{v3_snapshot_prefix}{cleanup_cid}.")
            snapshot_counts.append(len(shard_keys))
        final_cleanup_head = await cleanup_page.evaluate(read_head_js, cleanup_cid)
        if not isinstance(final_cleanup_head, dict):
            raise AssertionError(f"cleanup conversation lost its head after the burst: {cleanup_cid}")
        if revision_seq(str(final_cleanup_head.get("revision") or "")) < 12:
            raise AssertionError(f"burst did not produce at least 12 commits: {final_cleanup_head}")
        if max(snapshot_counts) > 6 or snapshot_counts[-1] > 4:
            raise AssertionError(f"snapshot retention grew unboundedly across 12 commits: {snapshot_counts}")
        checks["checkpointCleanupRemainsConstantTime"] = "PASS"
        await cleanup_context.close()

        # ---- 8. streamCheckpointRateBounded：慢速流期间快照写入被 1Hz 节流 ----
        stream_context = await browser.new_context(service_workers="allow")
        await stream_context.add_init_script(stream_probe_init)
        await stream_context.route("**/api/config", mock_config)
        await stream_context.route("**/api/title", mock_title)
        stream_page = await stream_context.new_page()
        await stream_page.goto(base_url, wait_until="networkidle")
        await stream_page.locator("#reactPromptInput").wait_for()
        await send_message(stream_page, "流式节流冒烟消息")
        await stream_page.get_by_text(stream_full_text, exact=False).wait_for(timeout=15_000)
        # 完整文本在最后一个内容增量时就已渲染，必须显式等 done 落地（done 在最后一个
        # 增量之后再隔 500ms 才入队），否则探针会在流仍在进行时被读取。
        await stream_page.wait_for_function("() => window.__v3StreamDoneAt > 0", timeout=15_000)
        await stream_page.wait_for_function(
            """(fullText) => {
              const doneAt = window.__v3StreamDoneAt || 0;
              const writes = window.__v3SnapshotWriteTimes || [];
              if (!writes.some((moment) => moment > doneAt)) return false;
              const headPrefix = 'deepseek-infra.session.v3.head.';
              const snapshotPrefix = 'deepseek-infra.session.v3.snapshot.';
              for (let i = 0; i < localStorage.length; i += 1) {
                const key = localStorage.key(i);
                if (!key || !key.startsWith(headPrefix)) continue;
                let head = null;
                try { head = JSON.parse(localStorage.getItem(key) || ''); } catch (error) { continue; }
                if (!head || typeof head.revision !== 'string') continue;
                const conversationId = key.slice(headPrefix.length);
                const raw = localStorage.getItem(`${snapshotPrefix}${conversationId}.${head.revision}`);
                if (raw && raw.includes(fullText)) return true;
              }
              return false;
            }""",
            arg=stream_full_text,
            timeout=15_000,
        )
        probe = await stream_page.evaluate(
            """() => ({
              start: window.__v3StreamStartAt || 0,
              done: window.__v3StreamDoneAt || 0,
              writes: window.__v3SnapshotWriteTimes || [],
            })"""
        )
        stream_duration = float(probe["done"]) - float(probe["start"])
        if not 3_000 <= stream_duration <= 6_000:
            raise AssertionError(f"slow stream mock did not span ~3.5s of streaming: {probe}")
        write_times = [float(moment) for moment in probe["writes"]]
        during_stream = [moment for moment in write_times if moment <= float(probe["done"])]
        after_done = [moment for moment in write_times if moment > float(probe["done"])]
        streaming_budget = int(stream_duration // 1000) + (1 if stream_duration % 1000 else 0) + 1
        if len(during_stream) > streaming_budget:
            raise AssertionError(
                f"streaming throttle wrote {len(during_stream)} snapshots in {stream_duration:.0f}ms (budget {streaming_budget})"
            )
        if not 1 <= len(after_done) <= 2:
            raise AssertionError(f"settle flush did not add exactly one snapshot write after done: {probe}")
        checks["streamCheckpointRateBounded"] = "PASS"
        await stream_context.close()

        # ---- 9. storagePressureCompactionLossless：配额压力下 level 1 压缩且内容无损 ----
        png_bytes = noisy_png_payload()

        async def mock_file_text(route: Any) -> None:
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "files": [
                            {
                                "name": "smoke-noise.png",
                                "type": "image/png",
                                "kind": "image",
                                "size": len(png_bytes),
                                "text": "噪声图片识别文本",
                            }
                        ],
                        "errors": [],
                    }
                ),
            )

        compaction_context = await browser.new_context(service_workers="allow")
        await compaction_context.add_init_script(quota_failure_init)
        await compaction_context.route("**/api/config", mock_config)
        await compaction_context.route("**/api/chat", mock_chat)
        await compaction_context.route("**/api/title", mock_title)
        await compaction_context.route("**/api/file-text", mock_file_text)
        compaction_page = await compaction_context.new_page()
        await compaction_page.goto(base_url, wait_until="networkidle")
        await compaction_page.locator("#reactPromptInput").wait_for()
        upload_file: FilePayload = {"name": "smoke-noise.png", "mimeType": "image/png", "buffer": png_bytes}
        await compaction_page.locator('input[type="file"]').set_input_files(files=upload_file)
        await compaction_page.locator(".attachment-item.ready img.attachment-thumb").wait_for(timeout=15_000)
        await send_message(compaction_page, "存储压力压缩冒烟消息")
        compaction_snapshot = None
        compaction_deadline = time.monotonic() + 15
        while time.monotonic() < compaction_deadline:
            compaction_snapshot = await compaction_page.evaluate(
                """({ userText, reply }) => {
                  const headPrefix = 'deepseek-infra.session.v3.head.';
                  const snapshotPrefix = 'deepseek-infra.session.v3.snapshot.';
                  for (let i = 0; i < localStorage.length; i += 1) {
                    const key = localStorage.key(i);
                    if (!key || !key.startsWith(headPrefix)) continue;
                    let head = null;
                    try { head = JSON.parse(localStorage.getItem(key) || ''); } catch (error) { continue; }
                    if (!head || typeof head.revision !== 'string') continue;
                    const conversationId = key.slice(headPrefix.length);
                    const raw = localStorage.getItem(`${snapshotPrefix}${conversationId}.${head.revision}`);
                    if (!raw || !raw.includes(userText) || !raw.includes(reply)) continue;
                    try {
                      const parsed = JSON.parse(raw);
                      if (parsed && parsed.compaction) {
                        return { compaction: parsed.compaction, conversation: parsed.conversation, hasDataImage: raw.includes('data:image') };
                      }
                    } catch (error) {}
                  }
                  return null;
                }""",
                {"userText": "存储压力压缩冒烟消息", "reply": reply_text},
            )
            if compaction_snapshot:
                break
            await compaction_page.wait_for_timeout(200)
        if not compaction_snapshot:
            raise AssertionError("storage-pressure commit never landed a compacted checkpoint")
        compaction = compaction_snapshot["compaction"]
        if compaction.get("level") != 1 or compaction.get("reason") != "storage-pressure" or not compaction.get("removedPreviewBytes"):
            raise AssertionError(f"unexpected compaction record: {compaction}")
        if compaction_snapshot["hasDataImage"]:
            raise AssertionError("compacted checkpoint still embeds a data:image preview")
        compaction_messages = compaction_snapshot["conversation"]["messages"]
        compaction_user = next((message for message in compaction_messages if message.get("role") == "user"), None)
        if not compaction_user or compaction_user.get("content") != "存储压力压缩冒烟消息":
            raise AssertionError(f"compaction corrupted the user message text: {compaction_user}")
        compaction_attachments = compaction_user.get("attachments") or []
        if len(compaction_attachments) != 1:
            raise AssertionError(f"compaction dropped the attachment: {compaction_attachments}")
        compacted_attachment = compaction_attachments[0]
        if compacted_attachment.get("name") != "smoke-noise.png" or compacted_attachment.get("size") != len(png_bytes):
            raise AssertionError(f"compaction lost attachment metadata: {compacted_attachment}")
        if compacted_attachment.get("thumbnail") or compacted_attachment.get("imagePreview"):
            raise AssertionError("compaction kept a strippable preview payload")
        if not any(message.get("role") == "assistant" and reply_text in str(message.get("content") or "") for message in compaction_messages):
            raise AssertionError("compaction lost the assistant reply text")
        checks["storagePressureCompactionLossless"] = "PASS"
        await compaction_context.close()

        # ---- 10. recoveryCapsuleReconciledOnce：pagehide 胶囊在下次启动恰好对账一次 ----
        capsule_context = await browser.new_context(service_workers="allow")
        await capsule_context.add_init_script(capsule_failure_init)
        await capsule_context.route("**/api/config", mock_config)
        await capsule_context.route("**/api/chat", mock_chat)
        await capsule_context.route("**/api/title", mock_title)
        page_a = await capsule_context.new_page()
        await page_a.goto(base_url, wait_until="networkidle")
        await page_a.locator("#reactPromptInput").wait_for()
        await page_a.evaluate("() => { window.__capsuleSmokeFailCheckpoint = true; }")
        await send_message(page_a, "胶囊冒烟消息")
        await page_a.get_by_text(reply_text, exact=True).wait_for(timeout=10_000)
        if await read_heads(page_a):
            raise AssertionError("failure injection did not block checkpoint commits before pagehide")
        await page_a.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        capsules = await page_a.evaluate(
            """() => {
              const prefix = 'deepseek-infra.session.v3.recovery.';
              const capsules = [];
              for (let i = 0; i < localStorage.length; i += 1) {
                const key = localStorage.key(i);
                if (key && key.startsWith(prefix)) capsules.push({ key, raw: localStorage.getItem(key) || '' });
              }
              return capsules;
            }"""
        )
        if len(capsules) != 1 or "胶囊冒烟消息" not in capsules[0]["raw"]:
            raise AssertionError(f"pagehide did not persist exactly one dirty recovery capsule: {capsules}")
        capsule_owner = capsules[0]["key"][len(v3_recovery_prefix):]
        try:
            capsule_parsed = json.loads(capsules[0]["raw"])
        except json.JSONDecodeError as error:
            raise AssertionError(f"recovery capsule is not parseable: {capsules[0]['raw'][:200]!r}") from error
        if (
            capsule_parsed.get("schemaVersion") != 2
            or capsule_parsed.get("writerSessionId") != capsule_owner
            or not capsule_parsed.get("digest")
            or len(capsule_parsed.get("entries") or []) != 1
            or not capsule_parsed["entries"][0].get("digest")
        ):
            raise AssertionError(f"recovery capsule is malformed: {capsule_parsed}")
        checks["recoveryCapsuleDigestVerified"] = "PASS"

        page_b = await capsule_context.new_page()
        await page_b.goto(base_url, wait_until="networkidle")
        await page_b.locator("#reactPromptInput").wait_for()
        await page_b.wait_for_function(
            """() => {
              let heads = 0;
              let capsules = 0;
              for (let i = 0; i < localStorage.length; i += 1) {
                const key = localStorage.key(i);
                if (!key) continue;
                if (key.startsWith('deepseek-infra.session.v3.head.')) heads += 1;
                if (key.startsWith('deepseek-infra.session.v3.recovery.')) capsules += 1;
              }
              return heads === 1 && capsules === 0;
            }""",
            timeout=15_000,
        )
        await page_b.locator(".conversation-open").first.click()
        await page_b.get_by_text("胶囊冒烟消息", exact=True).wait_for(timeout=10_000)
        await page_b.get_by_text(reply_text, exact=True).wait_for(timeout=10_000)
        await page_b.reload(wait_until="networkidle")
        await page_b.locator("#reactPromptInput").wait_for()
        await page_b.locator(".conversation-open").first.wait_for(timeout=10_000)
        heads_reloaded = await read_heads(page_b)
        capsules_reloaded = await page_b.evaluate(keys_with_prefix_js, v3_recovery_prefix)
        recovered_keys = await page_b.evaluate(
            keys_with_prefix_js,
            f"{v3_head_prefix}{capsule_parsed['entries'][0]['conversationId']}.recovered.",
        )
        if len(heads_reloaded) != 1 or capsules_reloaded or recovered_keys:
            raise AssertionError(
                f"capsule reconcile was not exactly-once: heads={heads_reloaded}, capsules={capsules_reloaded}, recovered={recovered_keys}"
            )
        checks["recoveryCapsuleReconciledOnce"] = "PASS"
        await capsule_context.close()

        # ---- 11. Restore Fence：旧标签页先冻结，只写旧 Epoch 胶囊且不重放请求 ----
        restore_context = await browser.new_context(service_workers="allow")
        await restore_context.route("**/api/config", mock_config)
        await restore_context.route("**/api/chat", mock_chat)
        await restore_context.route("**/api/title", mock_title)
        stale_page = await restore_context.new_page()
        owner_page = await restore_context.new_page()
        await stale_page.goto(base_url, wait_until="networkidle")
        await owner_page.goto(base_url, wait_until="networkidle")
        await stale_page.locator("#reactPromptInput").fill("Fence restore stream")
        await stale_page.locator("button.send-button").click()
        await asyncio.wait_for(restore_requested.wait(), timeout=5)
        requests_at_fence = chat_request_count
        fence = {
            "schemaVersion": 1,
            "restoreId": "restore_browser_smoke",
            "previousEpoch": "legacy",
            "targetEpoch": "epoch-browser-smoke",
            "ownerDocumentId": "owner-browser-smoke",
            "phase": "preparing",
            "createdAt": int(time.time() * 1000),
            "expiresAt": int(time.time() * 1000) + 60_000,
        }
        await owner_page.evaluate(
            """fence => localStorage.setItem('deepseek-infra.workspace.restore-fence', JSON.stringify(fence))""",
            fence,
        )
        await stale_page.locator(".workspace-restored-overlay").wait_for(timeout=10_000)
        await stale_page.evaluate(
            """() => {
              const button = document.querySelector('button.send-button');
              if (button instanceof HTMLButtonElement) button.click();
              window.dispatchEvent(new PageTransitionEvent('pagehide'));
            }"""
        )
        await stale_page.wait_for_timeout(500)
        if chat_request_count != requests_at_fence:
            raise AssertionError("stale restore-fenced tab issued another paid chat request")
        capsule = await stale_page.evaluate(
            """() => {
              for (let i = 0; i < localStorage.length; i += 1) {
                const key = localStorage.key(i);
                if (key && key.startsWith('deepseek-infra.session.v3.recovery.')) {
                  const raw = localStorage.getItem(key) || '';
                  if (raw.includes('Fence restore stream')) return { key, raw };
                }
              }
              return null;
            }"""
        )
        if not capsule:
            raise AssertionError("stale restore-fenced tab did not preserve dirty state in the previous Epoch capsule")
        await owner_page.evaluate(
            "() => localStorage.setItem('deepseek-infra.workspace.active-epoch', 'epoch-browser-smoke')"
        )
        await stale_page.wait_for_timeout(300)
        target_heads = await stale_page.evaluate(
            """() => {
              const prefix = 'deepseek-infra.session.v4.epoch-browser-smoke.head.';
              return Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i))
                .filter(key => key && key.startsWith(prefix));
            }"""
        )
        if target_heads:
            raise AssertionError(f"stale tab advanced restored Epoch heads: {target_heads}")
        checks["restoreFenceBlocksPeerWrites"] = "PASS"
        checks["staleTabDoesNotFlushAfterRestore"] = "PASS"
        checks["noPaidRequestReplayed"] = "PASS"
        restore_release.set()
        await restore_context.close()

        await browser.close()
    return checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write JSON evidence to this path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = None
    if args.out:
        output = args.out if args.out.is_absolute() else ROOT / args.out
        # CI 的 always() artifact 上传不能捡到仓库中上一轮已提交的 PASS
        # Evidence。每次运行先移除目标；失败分支会写入本次 FAIL 诊断。
        output.unlink(missing_ok=True)
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
        try:
            checks = asyncio.run(run_browser(base_url, trace_id))
            checks.update(asyncio.run(run_demand_loading_smoke(base_url)))
            checks.update(asyncio.run(run_query_smoke(base_url)))
            checks.update(asyncio.run(run_recovery_smoke(base_url)))
            checks.update(asyncio.run(run_mutation_smoke(base_url)))
            checks.update(asyncio.run(run_mutation_lifecycle_smoke(base_url)))
            checks.update(asyncio.run(run_mutation_continuity_smoke(base_url)))
            checks.update(asyncio.run(run_durable_checkpoint_smoke(base_url)))
            checks.update(asyncio.run(run_cross_tab_checkpoint_smoke(base_url)))
        except Exception as error:
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
                "status": "FAIL",
                "browser": "chromium",
                "checks": {},
                "error": {
                    "type": type(error).__name__,
                    "message": str(error),
                },
            }
            if output:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
            raise
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
        if output:
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
