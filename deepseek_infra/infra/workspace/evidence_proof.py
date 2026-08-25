"""Machine-readable Evidence proof contract (evidence-proof-v1).

Runners must derive claim PASS/FAIL from proof documents, not pytest exit alone.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

EVIDENCE_PROOF_SCHEMA = "evidence-proof-v1"
ENV_EVIDENCE_PROOF_PATH = "DEEPSEEK_EVIDENCE_PROOF_PATH"


def write_evidence_proof(
    path: Path | str,
    *,
    scenario: str,
    checks: dict[str, dict[str, Any]],
    meta: dict[str, Any] | None = None,
) -> Path:
    """Write evidence-proof-v1 JSON. Each check: {status: PASS|FAIL, evidence: {...}}."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": EVIDENCE_PROOF_SCHEMA,
        "scenario": str(scenario),
        "checks": checks,
        "meta": dict(meta or {}),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


def load_evidence_proof(path: Path | str) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("evidence-proof-must-be-object")
    if str(data.get("schema") or "") != EVIDENCE_PROOF_SCHEMA:
        raise ValueError(f"evidence-proof-schema-mismatch:{data.get('schema')}")
    if not isinstance(data.get("checks"), dict):
        raise ValueError("evidence-proof-checks-required")
    return data


def proof_check_status(proof: dict[str, Any], check_name: str) -> str:
    raw_checks = proof.get("checks")
    checks: dict[str, Any] = raw_checks if isinstance(raw_checks, dict) else {}
    item = checks.get(check_name)
    if not isinstance(item, dict):
        return "FAIL"
    status = str(item.get("status") or "").upper()
    return "PASS" if status == "PASS" else "FAIL"


def resolve_proof_path(*, env: dict[str, str] | None = None, scenario: str | None = None) -> Path | None:
    environ = env if env is not None else dict(os.environ)
    raw = environ.get(ENV_EVIDENCE_PROOF_PATH)
    if raw:
        return Path(raw)
    if scenario:
        # Convention under cwd artifacts/
        candidate = Path("artifacts") / f"evidence-proof-{scenario}.json"
        if candidate.is_file():
            return candidate
    return None


def merge_checks_from_proof(
    *,
    checks: dict[str, str],
    check_to_scenario: dict[str, str],
    scenario_results: dict[str, dict[str, Any]],
    required_proof_checks: dict[str, tuple[str, ...]],
) -> dict[str, str]:
    """Upgrade/downgrade checks using proof files when scenarios declare required proofs.

    ``required_proof_checks`` maps scenario_id → check names that MUST have PASS in proof.
    """
    out = dict(checks)
    for scenario, required in required_proof_checks.items():
        result = scenario_results.get(scenario) or {}
        exit_code = result.get("exitCode")
        if exit_code is None or int(exit_code) != 0:
            for check in required:
                out[check] = "FAIL"
            continue
        proof_path_raw = result.get("proofPath")
        path: Path | None = Path(str(proof_path_raw)) if proof_path_raw else None
        if path is None or not path.is_file():
            candidate = Path("artifacts") / f"evidence-proof-{scenario}.json"
            path = candidate if candidate.is_file() else None
        if path is None or not path.is_file():
            for check in required:
                out[check] = "FAIL"
            continue
        try:
            proof = load_evidence_proof(path)
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            for check in required:
                out[check] = "FAIL"
            continue
        for check in required:
            out[check] = proof_check_status(proof, check)
    return out
