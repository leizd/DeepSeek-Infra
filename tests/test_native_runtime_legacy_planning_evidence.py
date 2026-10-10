from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import evidence_proof
from scripts.native_runtime_contract import validate_corpus


ROOT = Path(__file__).resolve().parents[1]


def test_v18_legacy_planning_matches_reference_and_records_unhashable_exceptions() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v18/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == "a37735c68398fc8f795babaa269e2de6a5acd567"
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    assert [group["check_name"] for group in fixture["groups"]] == [
        name for name, validator in evidence_proof.VALIDATORS.items()
        if validator is evidence_proof.validate_predictive_planning_proof
    ]
    exceptions = 0
    for group in fixture["groups"]:
        for case in group["cases"]:
            candidate = (
                json.loads(case["evidence_json"]) if "evidence_json" in case
                else copy.deepcopy(case.get("evidence", group["base_evidence"]))
            )
            for operation in case["operations"]:
                if operation["op"] == "remove":
                    candidate.pop(operation["field"])
                else:
                    assert operation["op"] == "replace"
                    candidate[operation["field"]] = operation["value"]
            item = {"status": "PASS", "evidence": candidate}
            if "oracle_exception" in case:
                assert case["oracle_exception"] == "TypeError"
                expected = {
                    "fleetSloExposes1h24h7d30dWindows": "slo-windows-incomplete",
                    "unknownTargetPriceDoesNotBecomeZero": "invalid-monthly-cost-type",
                }[group["check_name"]]
                assert case["expected_errors"] == [expected]
                with pytest.raises(TypeError, match="unhashable type"):
                    evidence_proof.validate_check(group["check_name"], item)
                exceptions += 1
            else:
                assert evidence_proof.validate_check(group["check_name"], item) == case["expected_errors"], (
                    group["check_name"], case["name"]
                )
    assert exceptions == 4
