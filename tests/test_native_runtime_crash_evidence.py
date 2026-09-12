from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import evidence_proof
from scripts.native_runtime_contract import validate_corpus


ROOT = Path(__file__).resolve().parents[1]


def test_v15_crash_recovery_matches_python_observations() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v15/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == "a37735c68398fc8f795babaa269e2de6a5acd567"
    assert fixture["check_names"] == [
        name for name, validator in evidence_proof.VALIDATORS.items()
        if validator is evidence_proof.validate_crash_recovery_proof
    ]
    for check_name in fixture["check_names"]:
        for case in fixture["cases"]:
            candidate = copy.deepcopy(case.get("evidence", fixture["base_evidence"]))
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
            item = {"status": "PASS", "evidence": candidate}
            if "oracle_exception" in case:
                assert case["name"] == "naive-lease"
                assert case["oracle_exception"] == "TypeError"
                assert case["expected_errors"] == ["invalid-worker-a-lease-expiry"]
                with pytest.raises(TypeError, match="offset-naive and offset-aware"):
                    evidence_proof.validate_check(check_name, item)
            else:
                assert evidence_proof.validate_check(check_name, item) == case["expected_errors"], (check_name, case["name"])
