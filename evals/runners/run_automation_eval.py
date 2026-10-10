#!/usr/bin/env python3
"""Offline Automation Runtime eval for v2.9.1."""

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
from scripts.smoke_automation import configure_automation_runtime, run_automation_smoke  # noqa: E402


def load_cases() -> list[dict[str, Any]]:
    path = REPO_ROOT / "evals" / "golden" / "automation" / "automation_cases.jsonl"
    cases: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            cases.append(item)
    return cases


def build_report(version: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="deepseek-automation-eval-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        configure_automation_runtime(root)
        checks, details = run_automation_smoke(root)
        cases = load_cases()
        expected_actions = {str(case.get("action") or "") for case in cases}
        eval_checks = {
            **checks,
            "goldenCasesLoaded": "PASS" if len(cases) >= 3 else "FAIL",
            "coreActionsCovered": "PASS" if {"project_summary", "save_item", "browser_snapshot"} <= expected_actions else "FAIL",
        }
        from deepseek_infra.infra.automation.evidence import automation_evidence_payload

        report = automation_evidence_payload(version, checks=eval_checks, details={**details, "caseCount": len(cases)})
        report["summary"] = {
            "caseCount": len(cases),
            "passCount": sum(1 for value in eval_checks.values() if value == "PASS"),
            "checkCount": len(eval_checks),
        }
        return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Automation Runtime eval")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", default=str(REPO_ROOT / "evals" / "reports" / f"automation-v{APP_VERSION}.json"))
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
