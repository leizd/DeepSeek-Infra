#!/usr/bin/env python3
"""Refresh committed eval reports and compare the stable offline suite baseline."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Update evals/reports/latest.json and latest.md")
    parser.add_argument("--out", default=str(REPO_ROOT / "evals" / "reports" / "latest.json"))
    parser.add_argument("--markdown", default=str(REPO_ROOT / "evals" / "reports" / "latest.md"))
    parser.add_argument("--baseline", default=str(REPO_ROOT / "evals" / "baselines" / "v2.2.6.json"))
    parser.add_argument("--agent-report-dir", default=str(REPO_ROOT / "evals" / "reports"))
    parser.add_argument("--skip-agent", action="store_true")
    parser.add_argument("--skip-compare", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    suite_cmd = [
        sys.executable,
        str(REPO_ROOT / "evals" / "runners" / "run_offline_eval_suite.py"),
        "--out",
        args.out,
        "--markdown",
        args.markdown,
    ]
    suite = subprocess.run(suite_cmd, cwd=REPO_ROOT, check=False)
    if suite.returncode != 0:
        return suite.returncode

    if not args.skip_agent:
        agent_cmd = [
            sys.executable,
            str(REPO_ROOT / "evals" / "runners" / "run_agent_eval.py"),
            "--report-dir",
            args.agent_report_dir,
            "--report-only",
        ]
        agent = subprocess.run(agent_cmd, cwd=REPO_ROOT, check=False)
        if agent.returncode != 0:
            return agent.returncode

    if args.skip_compare:
        return 0

    compare_cmd = [
        sys.executable,
        str(REPO_ROOT / "evals" / "runners" / "compare_eval_baseline.py"),
        "--baseline",
        args.baseline,
        "--current",
        args.out,
    ]
    compare = subprocess.run(compare_cmd, cwd=REPO_ROOT, check=False)
    return compare.returncode


if __name__ == "__main__":
    raise SystemExit(main())
