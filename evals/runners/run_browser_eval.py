#!/usr/bin/env python3
"""Offline Browser Control Runtime eval for v2.8.1."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402
from scripts.smoke_browser import FIXTURES, configure_browser_runtime  # noqa: E402


def load_cases() -> list[dict[str, Any]]:
    path = REPO_ROOT / "evals" / "golden" / "browser" / "browser_cases.jsonl"
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if isinstance(data, dict):
            cases.append(data)
    return cases


def build_report(version: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="deepseek-browser-eval-") as tmp:
        root = Path(tmp)
        configure_browser_runtime(root)
        from deepseek_infra.infra.browser.actions import execute_browser_action
        from deepseek_infra.infra.browser.evidence import browser_evidence_payload
        from deepseek_infra.infra.rag import local_rag
        from deepseek_infra.infra.workspace import projects

        project = projects.create_project("Browser Eval")
        project_id = str(project["projectId"])
        media_ids: list[str] = []
        for case in load_cases():
            opened = execute_browser_action({"action": "open_url", "projectId": project_id, "url": (FIXTURES / str(case["fixture"])).as_uri()})
            session_id = str(opened.get("session", {}).get("browserSessionId") or "")
            read = execute_browser_action({"action": "read_page", "sessionId": session_id})
            snapshot = read.get("result", {}).get("snapshot") if isinstance(read.get("result"), dict) else {}
            media_id = str(snapshot.get("mediaId") or "") if isinstance(snapshot, dict) else ""
            if media_id:
                media_ids.append(media_id)

        eval_cases = [{"query": case["query"], "relevant": media_ids[index:index + 1]} for index, case in enumerate(load_cases()) if index < len(media_ids)]
        recall = local_rag.evaluate_recall(eval_cases, k=3, collection=local_rag.COLLECTION_MEDIA)
        unsafe = execute_browser_action({"action": "open_url", "url": "http://127.0.0.1:9/private"})
        form = execute_browser_action({"action": "open_url", "url": (FIXTURES / "form.html").as_uri()})
        form_session = str(form.get("session", {}).get("browserSessionId") or "")
        submit = execute_browser_action({"action": "click", "sessionId": form_session, "selector": "button.submit", "reason": "Submit form"})
        checks = {
            "browserRecall": "PASS" if recall["recallAtK"] >= 1.0 else "FAIL",
            "privateHostBlocked": "PASS" if unsafe.get("ok") is False and unsafe.get("safety", {}).get("risk") == "critical" else "FAIL",
            "confirmationGate": "PASS" if submit.get("code") == "requires_confirmation" else "FAIL",
            "mediaRagCollection": "PASS" if local_rag.status().get("indexedMedia", 0) >= len(media_ids) else "FAIL",
        }
        report = browser_evidence_payload(version, checks=checks, details={"recall": recall, "projectId": project_id, "mediaIds": media_ids})
        report["summary"] = {"caseCount": len(eval_cases), "recallAtK": recall["recallAtK"], "mrr": recall["mrr"]}
        return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Browser Control Runtime eval")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", default=str(REPO_ROOT / "evals" / "reports" / f"browser-v{APP_VERSION}.json"))
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args.version)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": report["checks"], "out": str(target)}, ensure_ascii=False, indent=2))
    return 1 if args.strict and report["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
