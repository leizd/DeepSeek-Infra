from __future__ import annotations

import ast
import copy
import json
import subprocess
from pathlib import Path

import pytest

from deepseek_infra.infra.workspace import evidence_proof
from scripts.native_runtime_contract import validate_corpus


ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "a37735c68398fc8f795babaa269e2de6a5acd567"


def test_wave_crash_validator_ast_matches_frozen_4_8_0() -> None:
    current = ast.parse((ROOT / "deepseek_infra/infra/workspace/evidence_proof.py").read_text(encoding="utf-8"))
    frozen = ast.parse(
        subprocess.check_output(
            ["git", "show", f"{SOURCE_COMMIT}:deepseek_infra/infra/workspace/evidence_proof.py"],
            encoding="utf-8",
        )
    )
    assert isinstance(current, ast.Module)
    assert isinstance(frozen, ast.Module)

    def dump(tree: ast.Module) -> str:
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "validate_wave_crash_recovery_proof"
        )
        return ast.dump(fn)

    assert dump(current) == dump(frozen)


def test_v19_wave_crash_matches_python_observations() -> None:
    manifest = validate_corpus(ROOT / "compat/native-runtime/v19/manifest.json")
    fixture = json.loads((ROOT / manifest["corpora"][0]["path"]).read_text(encoding="utf-8"))
    assert fixture["source_commit"] == SOURCE_COMMIT
    assert fixture["scope"] == "validator-parity-only-not-provider-execution-evidence"
    assert fixture["ast_matches_source_commit"] is True
    assert fixture["check_names"] == [
        name for name, validator in evidence_proof.VALIDATORS.items()
        if validator is evidence_proof.validate_wave_crash_recovery_proof
    ]
    exceptions = 0
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
                with pytest.raises(TypeError, match="offset-naive and offset-aware"):
                    evidence_proof.validate_check(check_name, item)
                exceptions += 1
            else:
                assert evidence_proof.validate_check(check_name, item) == case["expected_errors"], (
                    check_name,
                    case["name"],
                )
    assert exceptions == len(fixture["check_names"])
