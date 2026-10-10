#!/usr/bin/env python3
"""Fail closed unless every claim in an exact Evidence proof is semantically valid."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.workspace import evidence_proof, resilience_slo_ledger  # noqa: E402


def _base_result(path: Path, raw: bytes = b"") -> dict[str, Any]:
    return {
        "proofPath": str(path),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest() if raw else "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proof", type=Path, required=True, help="Exact evidence-proof JSON to verify")
    parser.add_argument("--scenario", help="Expected scenario name")
    args = parser.parse_args(argv)

    path = args.proof.resolve()
    raw = b""
    try:
        raw = path.read_bytes()
        proof = evidence_proof.load_evidence_proof(path, expected_scenario=args.scenario)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, TypeError) as exc:
        result = {
            **_base_result(path, raw),
            "status": "FAIL",
            "error": str(exc),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1

    checks = proof["checks"]
    errors: dict[str, list[str]] = {}
    if not checks:
        errors["$proof"] = ["proof-has-no-checks"]
    for check_name, item in sorted(checks.items()):
        if not isinstance(item, dict):
            errors[str(check_name)] = ["check-item-must-be-object"]
            continue
        try:
            check_errors = evidence_proof.validate_check(str(check_name), item)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            check_errors = [f"validator-error:{type(exc).__name__}:{exc}"]
        if check_errors:
            errors[str(check_name)] = check_errors

    digest = hashlib.sha256(raw).hexdigest()
    if not errors:
        try:
            resilience_slo_ledger.record_evidence_verification(
                proof_sha256=digest,
                scenario=str(proof.get("scenario") or ""),
            )
        except Exception as exc:
            errors["$slo"] = [f"evidence-verification-not-durable:{type(exc).__name__}:{exc}"]

    result = {
        **_base_result(path, raw),
        "status": "FAIL" if errors else "PASS",
        "schema": str(proof.get("schema") or ""),
        "scenario": str(proof.get("scenario") or ""),
        "checkCount": len(checks),
        "errors": errors,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
