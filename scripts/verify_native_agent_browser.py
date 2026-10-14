"""Actual browser acceptance for the isolated native Agent execution chain."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import urlsplit

BROWSER_QUERY = "Prepare an isolated browser verification plan"
BROWSER_CREDENTIAL = "request-only-credential"


def run_browser_agent(origin: str, output: Path) -> dict[str, Any]:
    """Drive shipped React controls; no route interception or page-side API calls."""
    target = urlsplit(origin)
    if target.scheme != "http" or target.hostname != "127.0.0.1" or not target.port or target.path:
        raise ValueError("Native browser acceptance requires its isolated loopback origin")
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RuntimeError("Actual native browser acceptance is cloud-only on this host")
    from playwright.sync_api import sync_playwright
    page_errors: list[str] = []
    console_errors: list[str] = []
    traffic: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(service_workers="allow", viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(45_000)
        page.on("pageerror", lambda error: page_errors.append(str(error)[:500]))
        page.on("console", lambda message: console_errors.append(message.text[:500]) if message.type == "error" else None)

        def record_response(response: Any) -> None:
            url = urlsplit(response.url)
            if url.netloc == target.netloc and url.path.startswith("/api/"):
                if len(traffic) >= 200:
                    raise ValueError("Native browser exceeded its bounded API request inventory")
                traffic.append({"method": response.request.method, "path": url.path, "status": response.status})

        page.on("response", record_response)

        def response_for(response: Any, path: str) -> bool:
            url = urlsplit(response.url)
            return url.netloc == target.netloc and url.path == path and response.request.method == "POST"

        try:
            landing = page.goto(origin, wait_until="domcontentloaded")
            if landing is None or landing.status != 200:
                raise ValueError("Actual native browser could not open the built frontend")
            page.locator("#reactPromptInput").wait_for()
            page.locator(".composer-options").get_by_role("button", name="连接设置", exact=True).click()
            page.locator("#reactApiKeyInput").fill(BROWSER_CREDENTIAL)
            page.locator("#reactAgentPresetSelect").select_option("auto")
            page.get_by_role("button", name="关闭连接设置", exact=True).click()
            mode = page.get_by_role("button", name="多 Agent", exact=True)
            if mode.get_attribute("aria-pressed") == "false":
                mode.click()
            if mode.get_attribute("aria-pressed") != "true":
                raise ValueError("Actual multi-Agent control was not enabled")
            page.locator("#reactPromptInput").fill(BROWSER_QUERY)
            with page.expect_response(lambda response: response_for(response, "/api/agent-runs"), timeout=120_000) as created, \
                    page.expect_response(lambda response: response_for(response, "/api/title"), timeout=120_000) as title:
                page.get_by_role("button", name="发送消息", exact=True).click()
                page.get_by_text("Controlled final answer", exact=True).last.wait_for(timeout=120_000)
            creation = created.value
            body = creation.json()
            run_id = body.get("runId")
            if creation.status not in {200, 201} or not isinstance(run_id, str) or not run_id:
                raise ValueError("The React create action did not return the actual native run")
            if title.value.status != 200 or title.value.json().get("title") != "Native browser conversation":
                raise ValueError("Actual native conversation title generation failed")
            page.reload(wait_until="domcontentloaded")
            page.get_by_text("Controlled final answer", exact=True).last.wait_for()
            page.wait_for_load_state("networkidle", timeout=30_000)
            screenshot = output / "browser-final.png"
            page.screenshot(path=str(screenshot), full_page=True)
            image = screenshot.read_bytes()
            if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > 2 * 1024**2:
                raise ValueError("Native browser rendered evidence exceeds its image bound")
            return dict(createdRunId=run_id, creationByBrowser=True, finalAnswerVisible=True,
                        reloadRetainedFinalAnswer=True, requestInterceptionUsed=False, apiInvokedOutsideUI=False,
                        pageErrors=list(page_errors), consoleErrors=list(console_errors), requests=list(traffic),
                        screenshot=screenshot.name, screenshotSha256=hashlib.sha256(image).hexdigest())
        except Exception:
            (output / "browser-failed.json").write_text(json.dumps({"pageErrors": page_errors,
                "consoleErrors": console_errors, "requests": traffic}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            try:
                page.screenshot(path=str(output / "browser-failure.png"), full_page=True, timeout=10_000)
            except Exception:
                pass
            raise
        finally:
            context.close()
            browser.close()


def inspect_browser_custody(artifacts: Path, state: Path) -> dict[str, bool]:
    """Validate isolated Rust outputs without creating or changing runtime data."""
    with sqlite3.connect((state / "rust-worker/authority.sqlite3").as_uri() + "?mode=ro", uri=True) as database:
        effects = database.execute("SELECT state,output,output_sha256 FROM agent_node_effects").fetchall()
    if len(effects) != 4 or any(row[0] != "CONFIRMED" or hashlib.sha256(row[1]).hexdigest() != row[2] for row in effects):
        raise ValueError("Actual browser Rust effects were not independently retained")
    sealed = list((artifacts / "agent-provider-credentials-v1").glob("*.json"))
    if len(sealed) != 1 or BROWSER_CREDENTIAL.encode() in sealed[0].read_bytes():
        raise ValueError("Actual browser request credential custody differs")
    with sqlite3.connect((artifacts / "objects-v1.sqlite").as_uri() + "?mode=ro", uri=True) as database:
        bodies = database.execute("SELECT bytes FROM artifacts").fetchall()
    if not bodies or any(BROWSER_CREDENTIAL.encode() in row[0] for row in bodies):
        raise ValueError("Actual browser plaintext credential entered public artifact custody")
    return {"rustEffectsIndependentlyRetained": True, "requestCredentialEncrypted": True}


def validate_browser_result(result: dict[str, Any], exchanges: list[dict[str, Any]]) -> None:
    flags = ["actualDaemonCLIQualified", "independentlyValidatedByGoStore", "resourcesReleased",
             "builtFrontendAssetsServed", "rustEffectsIndependentlyRetained", "requestCredentialEncrypted", "cloudOnlyNativeBuild"]
    denied = ["inputBodySeededByOfflineOracle", "privateCustodyVariablesExportedToGo", "productionProviderQualified", "fullMigrationQualified"]
    if any(result.get(key) is not True for key in flags) or any(result.get(key) is not False for key in denied) or \
            result.get("productionRuntimeProcesses") != ["Go", "Rust"] or result.get("pythonMutatingPublicRequests") != 0 or \
            result.get("readiness") != "NOT_READY" or not re.fullmatch(r"[a-f0-9]{40}", str(result.get("sourceCommit", ""))):
        raise ValueError("Actual native browser chain evidence is missing or overclaims production readiness")
    if result.get("status") != "PASS" or result.get("runStatus") != "done" or result.get("nextIndex") != 8 or \
            result.get("actualPreset") != "auto" or result.get("durablePhases") != 3 or \
            not re.fullmatch(r"[a-f0-9]{64}", str(result.get("frontendIndexSha256", ""))) or \
            not isinstance(result.get("frontendAssetCount"), int) or result["frontendAssetCount"] < 1:
        raise ValueError("Actual browser run, immutable assets or retained phases differ")
    observed = result.get("browser")
    if not isinstance(observed, dict) or observed.get("createdRunId") != result.get("runId") or \
            any(observed.get(key) is not True for key in ["creationByBrowser", "finalAnswerVisible", "reloadRetainedFinalAnswer"]) or \
            any(observed.get(key) is not False for key in ["requestInterceptionUsed", "apiInvokedOutsideUI"]) or \
            observed.get("pageErrors") != [] or observed.get("consoleErrors") != [] or \
            observed.get("screenshot") != "browser-final.png" or \
            not re.fullmatch(r"[a-f0-9]{64}", str(observed.get("screenshotSha256", ""))):
        raise ValueError("Actual rendered React flow was substituted, failed or omitted retained history")
    requests = observed.get("requests")
    if not isinstance(requests, list):
        raise ValueError("Actual browser network evidence is missing")
    creations = [row for row in requests if row.get("method") == "POST" and row.get("path") == "/api/agent-runs"]
    streams = [row for row in requests if row.get("method") == "GET" and row.get("path") == f"/api/agent-runs/{result['runId']}/stream"]
    titles = [row for row in requests if row.get("method") == "POST" and row.get("path") == "/api/title"]
    if len(creations) != 1 or creations[0].get("status") not in {200, 201} or \
            not streams or any(row.get("status") != 200 for row in streams) or len(titles) != 1 or titles[0].get("status") != 200:
        raise ValueError("Native browser creation, run NDJSON or conversation title did not succeed")
    agents = [row for row in exchanges if row.get("kind", "agent") == "agent"]
    titles = [row for row in exchanges if row.get("kind") == "title"]
    if len(agents) != 4 or len(titles) != 1 or len(exchanges) != 5 or len({row.get("operationId") for row in agents}) != 4 or \
            any(row.get("requestCredentialUsed") is not True or row.get("credentialInProviderBody") is not False for row in exchanges) or \
            any(not row.get("actionId") or not row.get("operationId") or not str(row.get("executionEpoch", "")).isdigit() or
                int(row["executionEpoch"]) <= 0 for row in agents):
        raise ValueError("Actual browser provider calls lost their credential isolation or Agent fences")
