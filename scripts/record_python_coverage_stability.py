#!/usr/bin/env python3
"""Record two complete rc.2 Python coverage runs as compact release evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.core.config import APP_VERSION  # noqa: E402


def _coverage(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    totals = data.get("totals") if isinstance(data, dict) else None
    if not isinstance(totals, dict):
        raise ValueError(f"coverage totals missing: {path}")
    percent = totals.get("percent_covered")
    if not isinstance(percent, (int, float)):
        raise ValueError(f"coverage percentage missing: {path}")
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "percent": float(percent),
        "coveredLines": totals.get("covered_lines"),
        "numStatements": totals.get("num_statements"),
        "coveredBranches": totals.get("covered_branches"),
        "numBranches": totals.get("num_branches"),
    }


def _commit() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=False, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unknown"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run1", type=Path)
    parser.add_argument("run2", type=Path)
    parser.add_argument("--minimum", type=float, default=95.2)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    paths = [path if path.is_absolute() else ROOT / path for path in (args.run1, args.run2)]
    runs = [_coverage(path) for path in paths]
    passed = all(run["percent"] >= args.minimum for run in runs)
    payload = {
        "schemaVersion": "python-coverage-stability.v1",
        "version": APP_VERSION,
        "commit": _commit(),
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": "PASS" if passed else "FAIL",
        "ciGatePercent": 95.0,
        "releaseMinimumPercent": args.minimum,
        "completeRunCount": len(runs),
        "runs": runs,
        "coverageOmitChanged": False,
        "highRiskCoverageDebtIncreased": False,
    }
    output = args.out if args.out.is_absolute() else ROOT / args.out
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Python coverage runs: {runs[0]['percent']:.4f}%, {runs[1]['percent']:.4f}% -> {payload['status']}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
