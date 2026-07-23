#!/usr/bin/env python3
"""Offline Skill and Skill Pack quality evaluation for v2.6."""

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
from deepseek_infra.infra.diagnostics.evidence_revision import evidence_revision  # noqa: E402
from deepseek_infra.infra.skills import eval as skill_eval  # noqa: E402
from scripts.smoke_skills import patch_runtime  # noqa: E402


def _load_json(path: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def build_report(
    *,
    version: str,
    scope: str = "all",
    skill_id: str = "",
    pack_id: str = "",
    baseline_path: str = "",
) -> dict[str, Any]:
    baseline = _load_json(baseline_path)
    report = skill_eval.build_skill_eval_report(
        version=version,
        scope=scope,
        skill_id=skill_id,
        pack_id=pack_id,
        baseline=baseline,
    )
    return {**report, **evidence_revision(REPO_ROOT)}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Skill quality eval")
    parser.add_argument("--version", default=APP_VERSION)
    parser.add_argument("--out", default=str(REPO_ROOT / "evals" / "reports" / f"skills-v{APP_VERSION}.json"))
    parser.add_argument("--markdown", default="")
    parser.add_argument("--baseline", default="", help="Optional previous Skill eval report for regression comparison.")
    parser.add_argument("--scope", choices=("all", "skill", "pack"), default="all")
    parser.add_argument("--skill-id", default="")
    parser.add_argument("--pack-id", default="")
    parser.add_argument("--use-runtime", action="store_true", help="Use the current runtime dirs instead of an isolated temp workspace.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when the Skill eval status is not PASS.")
    return parser.parse_args(argv)


def _write_report(report: dict[str, Any], out: str, markdown: str = "") -> None:
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if markdown:
        md_path = Path(markdown)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(skill_eval.markdown_summary(report), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.use_runtime:
        report = build_report(
            version=args.version,
            scope=args.scope,
            skill_id=args.skill_id,
            pack_id=args.pack_id,
            baseline_path=args.baseline,
        )
    else:
        with tempfile.TemporaryDirectory(prefix="deepseek-skill-eval-", ignore_cleanup_errors=True) as tmp:
            patch_runtime(Path(tmp))
            report = build_report(
                version=args.version,
                scope=args.scope,
                skill_id=args.skill_id,
                pack_id=args.pack_id,
                baseline_path=args.baseline,
            )
    _write_report(report, args.out, args.markdown)
    print(
        json.dumps(
            {
                "status": report["status"],
                "summary": report.get("summary", {}),
                "checks": report.get("checks", {}),
                "out": str(Path(args.out)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if args.strict and report["status"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
