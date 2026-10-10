from __future__ import annotations

import copy
import json
from pathlib import Path

from deepseek_infra.infra.workspace import evidence_proof
from scripts.native_runtime_contract import validate_corpus


ROOT = Path(__file__).resolve().parents[1]


def test_v14_control_and_storage_evidence_matches_python_oracle() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v14/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == "a37735c68398fc8f795babaa269e2de6a5acd567"
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    validators = {
        "retention_safety": evidence_proof.validate_retention_safety_proof,
        "decision": evidence_proof.validate_decision_proof,
        "resilience": evidence_proof.validate_resilience_proof,
        "repair": evidence_proof.validate_autonomous_repair_proof,
        "rebalance": evidence_proof.validate_autonomous_rebalance_proof,
    }
    expected_names = {
        name for name, validator in evidence_proof.VALIDATORS.items()
        if validator in validators.values()
    }
    observed_names: set[str] = set()
    for group in fixture["groups"]:
        for check_name in group["check_names"]:
            observed_names.add(check_name)
            assert evidence_proof.VALIDATORS[check_name] is validators[group["validator"]]
            for case in group["cases"]:
                evidence = copy.deepcopy(case.get("evidence", group["base_evidence"]))
                for operation in case["operations"]:
                    parts = operation["pointer"].lstrip("/").split("/")
                    target = evidence
                    for part in parts[:-1]:
                        target = target[part]
                    if operation["op"] == "remove":
                        target.pop(parts[-1])
                    else:
                        assert operation["op"] == "replace"
                        target[parts[-1]] = operation["value"]
                assert evidence_proof.validate_check(
                    check_name, {"status": "PASS", "evidence": evidence}
                ) == case["expected_errors"], (check_name, case["name"])
    assert observed_names == expected_names
