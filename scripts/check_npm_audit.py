"""Gate ``npm audit --json`` output with an explicit, documented advisory exception list.

The CI ``security`` job runs ``npm audit --prefix frontend --audit-level=high --json``
and pipes the report here. Every high/critical vulnerability must map to a GitHub
advisory; anything not covered by ``ADVISORY_EXCEPTIONS`` fails the gate. Exceptions
are only allowed when the advisory is not exploitable in this codebase, and each entry
must say why and when it can be removed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GATED_SEVERITIES = ("high", "critical")

# Documented exceptions: GHSA id -> (justification, removal condition). Keep entries
# actionable — an exception is a reviewed risk acceptance, not a mute button.
ADVISORY_EXCEPTIONS = {
    "GHSA-qwww-vcr4-c8h2": (
        "react-router RSC-mode CSRF (published 2026-07-24). The advisory only affects "
        "React Router's RSC/framework mode; this frontend is a client-only Vite SPA "
        "(no @react-router/dev, no ServerRouter), so it is not exploitable here. "
        "Remove once react-router is upgraded to >= 8.3.0 or a fixed 7.x backport lands."
    ),
}


def _advisory_id(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _via_advisory_ids(vuln: dict, vulns: dict, seen: set[str]) -> list[str]:
    ids: list[str] = []
    for via in vuln.get("via") or []:
        if isinstance(via, dict):
            url = str(via.get("url", ""))
            if "github.com/advisories/" in url:
                ids.append(_advisory_id(url))
        elif isinstance(via, str) and via in vulns and via not in seen:
            seen.add(via)
            ids.extend(_via_advisory_ids(vulns[via], vulns, seen))
    return ids


def evaluate_report(report: dict) -> tuple[list[str], list[str]]:
    """Return (blocking, excepted) human-readable entries for high/critical vulns."""
    vulns = report.get("vulnerabilities") or {}
    blocking: list[str] = []
    excepted: list[str] = []
    for name, vuln in sorted(vulns.items()):
        severity = str(vuln.get("severity", "")).lower()
        if severity not in GATED_SEVERITIES:
            continue
        ids = _via_advisory_ids(vuln, vulns, {name})
        if ids and all(advisory_id in ADVISORY_EXCEPTIONS for advisory_id in ids):
            excepted.append(f"{name} ({severity}): {', '.join(sorted(set(ids)))}")
        else:
            detail = ", ".join(sorted(set(ids))) or "no advisory url"
            blocking.append(f"{name} ({severity}): {detail}")
    return blocking, excepted


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    report_path = Path(args[0]) if args else Path("artifacts/npm-audit.json")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"FAIL: cannot read npm audit report {report_path}: {error}")
        return 1
    blocking, excepted = evaluate_report(report)
    for entry in excepted:
        print(f"NOTE: {entry} [documented exception]")
    for advisory_id, reason in ADVISORY_EXCEPTIONS.items():
        if any(advisory_id in entry for entry in excepted):
            print(f"NOTE: exception {advisory_id}: {reason}")
    if blocking:
        for entry in blocking:
            print(f"FAIL: {entry}")
        return 1
    print("PASS: no unexcepted high/critical npm audit vulnerabilities")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
