from __future__ import annotations

import copy
import json
from pathlib import Path

from deepseek_infra.infra.workspace import evidence_proof
from scripts.native_runtime_contract import validate_corpus


ROOT = Path(__file__).resolve().parents[1]


def test_v16_safety_and_admission_matches_python_observations() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v16/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == "a37735c68398fc8f795babaa269e2de6a5acd567"
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    for group in fixture["groups"]:
        validator = getattr(evidence_proof, group["validator"])
        assert group["check_names"] == [name for name, fn in evidence_proof.VALIDATORS.items() if fn is validator]
        for case in group["cases"]:
            candidate = (
                json.loads(case["evidence_json"]) if "evidence_json" in case
                else copy.deepcopy(case.get("evidence", group["base_evidence"]))
            )
            for operation in case["operations"]:
                parts = operation["pointer"].lstrip("/").split("/")
                target = candidate
                for part in parts[:-1]:
                    target = target[int(part)] if isinstance(target, list) else target[part]
                leaf = int(parts[-1]) if isinstance(target, list) else parts[-1]
                if operation["op"] == "remove":
                    target.pop(leaf)
                else:
                    assert operation["op"] == "replace"
                    target[leaf] = operation["value"]
            for check_name in group["check_names"]:
                expected = case["per_check_errors"].get(check_name, case["expected_errors"])
                assert evidence_proof.validate_check(
                    check_name, {"status": "PASS", "evidence": candidate}
                ) == expected, (check_name, case["name"])
