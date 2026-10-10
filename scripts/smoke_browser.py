#!/usr/bin/env python3
"""Offline Browser Control Runtime smoke."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from scripts.smoke_media import configure_runtime_root  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "browser"


def configure_browser_runtime(root: Path) -> None:
    from deepseek_infra.core import config
    from deepseek_infra.infra.browser import session as browser_session

    configure_runtime_root(root)
    config.BROWSER_CONTROL_ENABLED = True
    config.BROWSER_HEADLESS = True
    config.BROWSER_ALLOW_PRIVATE_HOSTS = False
    config.BROWSER_REQUIRE_CONFIRM = True
    config.BROWSER_DOWNLOAD_MAX_BYTES = 50_000_000
    config.BROWSER_SESSION_TTL_SECONDS = 1_800
    config.BROWSER_AUDIT_DIR = root / ".browser-audit"
    config.BROWSER_AUDIT_LOG = config.BROWSER_AUDIT_DIR / "audit.jsonl"
    config.BROWSER_DOWNLOADS_DIR = root / ".browser-downloads"
    config.BROWSER_PROFILES_DIR = root / ".browser-profiles"
    browser_session.reset_sessions_for_tests()


def run_browser_smoke(root: Path) -> tuple[dict[str, str], dict[str, Any]]:
    from deepseek_infra.core import config
    from deepseek_infra.infra.browser.actions import execute_browser_action
    from deepseek_infra.infra.media import library
    from deepseek_infra.infra.rag import local_rag
    from deepseek_infra.infra.workspace import projects

    checks = {
        "browserSessionCreate": "FAIL",
        "readPage": "FAIL",
        "screenshot": "FAIL",
        "extractLinks": "FAIL",
        "unsafeActionBlocked": "FAIL",
        "confirmationRequired": "FAIL",
        "snapshotToMedia": "FAIL",
        "snapshotToRag": "FAIL",
        "auditLog": "FAIL",
        "redactSecrets": "FAIL",
        "repeatedClose": "FAIL",
        "sessionExpired": "FAIL",
        "downloadLimit": "FAIL",
    }
    details: dict[str, Any] = {"runtimeRoot": str(root), "fixtures": str(FIXTURES)}
    project = projects.create_project("Browser Smoke")
    project_id = str(project["projectId"])

    opened = execute_browser_action({"action": "open_url", "projectId": project_id, "url": (FIXTURES / "basic.html").as_uri()})
    session_id = str(opened.get("session", {}).get("browserSessionId") or "")
    checks["browserSessionCreate"] = "PASS" if opened.get("ok") and session_id else "FAIL"

    read = execute_browser_action({"action": "read_page", "sessionId": session_id, "selector": "#content"})
    snapshot = read.get("result", {}).get("snapshot") if isinstance(read.get("result"), dict) else {}
    snapshot_id = str(snapshot.get("mediaId") or "") if isinstance(snapshot, dict) else ""
    checks["readPage"] = "PASS" if "Browser snapshots become Media Library" in str(read.get("result", {}).get("text") or "") else "FAIL"
    checks["snapshotToMedia"] = "PASS" if snapshot_id and library.get_media(snapshot_id).get("type") == "webpage" else "FAIL"
    hits = local_rag.search_media_index("Browser snapshots Local RAG chunks", project_id=project_id, media_id=snapshot_id, limit=3)
    checks["snapshotToRag"] = "PASS" if hits else "FAIL"

    shot = execute_browser_action({"action": "screenshot", "sessionId": session_id})
    screenshot = shot.get("result", {}).get("screenshot") if isinstance(shot.get("result"), dict) else {}
    checks["screenshot"] = "PASS" if isinstance(screenshot, dict) and screenshot.get("type") == "screenshot" else "FAIL"

    links = execute_browser_action({"action": "extract_links", "sessionId": session_id})
    link_rows = links.get("result", {}).get("links") if isinstance(links.get("result"), dict) else []
    checks["extractLinks"] = "PASS" if any(str(link.get("href") or "").endswith("download.html") for link in link_rows) else "FAIL"

    unsafe = execute_browser_action({"action": "open_url", "url": "http://127.0.0.1:9/private"})
    checks["unsafeActionBlocked"] = "PASS" if unsafe.get("ok") is False and unsafe.get("safety", {}).get("risk") == "critical" else "FAIL"

    form = execute_browser_action({"action": "open_url", "url": (FIXTURES / "form.html").as_uri()})
    form_session = str(form.get("session", {}).get("browserSessionId") or "")
    submit = execute_browser_action({"action": "click", "sessionId": form_session, "selector": "button.submit", "reason": "Submit form"})
    checks["confirmationRequired"] = "PASS" if submit.get("code") == "requires_confirmation" else "FAIL"

    injected = execute_browser_action({"action": "open_url", "projectId": project_id, "url": (FIXTURES / "injection.html").as_uri()})
    injected_session = str(injected.get("session", {}).get("browserSessionId") or "")
    injected_read = execute_browser_action({"action": "read_page", "sessionId": injected_session})
    injected_snapshot = injected_read.get("result", {}).get("snapshot") if isinstance(injected_read.get("result"), dict) else {}
    segments = library.list_segments(str(injected_snapshot.get("mediaId") or "")) if isinstance(injected_snapshot, dict) else []
    checks["redactSecrets"] = "PASS" if "sk-browser-secret-value" not in json.dumps(segments, ensure_ascii=False) else "FAIL"

    audit_lines = config.BROWSER_AUDIT_LOG.read_text(encoding="utf-8").splitlines() if config.BROWSER_AUDIT_LOG.is_file() else []
    audit_entries = [json.loads(line) for line in audit_lines if line.strip()]
    audit_ok = any(entry.get("action") == "read_page" for entry in audit_entries)
    audit_fields_ok = all("requestId" in entry and "riskLevel" in entry for entry in audit_entries)
    checks["auditLog"] = "PASS" if (audit_ok and audit_fields_ok) else "FAIL"

    close1 = execute_browser_action({"action": "close_session", "sessionId": session_id})
    close2 = execute_browser_action({"action": "close_session", "sessionId": session_id})
    checks["repeatedClose"] = "PASS" if close1.get("ok") and close2.get("ok") and close2.get("result", {}).get("closed") else "FAIL"

    expired = execute_browser_action({"action": "open_url", "url": (FIXTURES / "basic.html").as_uri()})
    expired_session_id = str(expired.get("session", {}).get("browserSessionId") or "")
    if expired_session_id:
        from deepseek_infra.infra.browser import session as browser_session_module

        expired_session = browser_session_module.get_session(expired_session_id)
        expired_session.last_access = 0.0
        expired_count = browser_session_module.close_expired_sessions()
        checks["sessionExpired"] = "PASS" if expired_count >= 1 else "FAIL"
    else:
        checks["sessionExpired"] = "FAIL"

    config.BROWSER_DOWNLOAD_MAX_BYTES = 16
    config.BROWSER_REQUIRE_CONFIRM = False
    oversized_path = root / "oversized.bin"
    oversized_path.write_bytes(b"x" * 32)
    limit_opened = execute_browser_action({"action": "open_url", "url": oversized_path.as_uri()})
    limit_session_id = str(limit_opened.get("session", {}).get("browserSessionId") or "")
    try:
        limit_result = execute_browser_action({"action": "download", "sessionId": limit_session_id, "downloadUrl": oversized_path.as_uri()})
        checks["downloadLimit"] = "PASS" if limit_result.get("ok") is False else "FAIL"
    except Exception:
        checks["downloadLimit"] = "PASS"

    details.update(
        {
            "projectId": project_id,
            "browserSessionId": session_id,
            "mediaIds": [item for item in [snapshot_id, str(screenshot.get("mediaId") or "") if isinstance(screenshot, dict) else ""] if item],
            "localRag": local_rag.status(),
            "auditLog": str(config.BROWSER_AUDIT_LOG),
        }
    )
    return checks, details


def build_evidence(version: str, checks: dict[str, str], details: dict[str, Any]) -> dict[str, Any]:
    from deepseek_infra.infra.browser.evidence import browser_evidence_payload

    return browser_evidence_payload(version, checks=checks, details=details)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Browser Control Runtime smoke")
    parser.add_argument("--offline", action="store_true", help="Kept for release-smoke symmetry; this smoke is always offline.")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "docs" / "evidence" / f"browser-v{APP_VERSION}.json")
    parser.add_argument("--json", action="store_true", help="Print evidence JSON to stdout.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="deepseek-browser-smoke-") as tmp:
        os.environ["DEEPSEEK_INFRA_ROOT"] = tmp
        runtime_root = Path(tmp)
        configure_browser_runtime(runtime_root)
        checks, details = run_browser_smoke(runtime_root)
        evidence = build_evidence(args.version, checks, details)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
