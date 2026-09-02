from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts.native_runtime_contract import ContractError, load_ownership, validate_ownership


ROOT = Path(__file__).resolve().parents[1]


def _git_diff(rel: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--", rel],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def test_accepted_ownership_contract_is_unique_and_python_authoritative() -> None:
    data = load_ownership()
    validate_ownership(data)
    ids = [item["id"] for item in data["domains"]]
    assert len(ids) == len(set(ids))
    assert data["current_production_authority"] == "python"
    assert data["source_commit"] == "a37735c68398fc8f795babaa269e2de6a5acd567"
    production = [item for item in data["domains"] if item.get("production", True) is True]
    assert production
    assert {item["target_owner"] for item in production} <= {"rust", "go"}
    assert {item["current_owner"] for item in production} == {"python"}


def test_duplicate_domain_is_rejected() -> None:
    data = copy.deepcopy(load_ownership())
    data["domains"].append(copy.deepcopy(data["domains"][0]))
    with pytest.raises(ContractError, match="duplicate"):
        validate_ownership(data)


def test_shared_store_is_rejected() -> None:
    data = copy.deepcopy(load_ownership())
    data["durable_stores"]["go_control"]["shared_with"] = ["rust"]
    with pytest.raises(ContractError, match="cannot be shared"):
        validate_ownership(data)


def test_production_python_owner_at_target_is_rejected() -> None:
    data = copy.deepcopy(load_ownership())
    data["domains"][0]["target_owner"] = "python"
    data["domains"][0]["production"] = True
    with pytest.raises(ContractError, match="python"):
        validate_ownership(data)


def test_go_control_store_cannot_target_rust() -> None:
    data = copy.deepcopy(load_ownership())
    data["domains"][0]["durable_store"] = "go_control"
    data["domains"][0]["target_owner"] = "rust"
    data["domains"][0]["production"] = True
    with pytest.raises(ContractError, match="go_control"):
        validate_ownership(data)


def test_existing_4_8_0_plan_artifacts_are_unchanged() -> None:
    assert _git_diff("tasks/plan.md") == ""
    assert _git_diff("tasks/todo.md") == ""


def test_adr_0049_is_accepted_and_linked() -> None:
    adr = (ROOT / "docs/adr/ADR-0049-native-runtime-ownership-inversion.md").read_text(encoding="utf-8")
    data = json.loads((ROOT / "release/native_runtime_ownership_v1.json").read_text(encoding="utf-8"))
    assert "- Status: Accepted" in adr
    assert "release/native_runtime_ownership_v1.json" in adr
    assert data["adr"].endswith("ADR-0049-native-runtime-ownership-inversion.md")


def test_ownership_json_is_valid_object() -> None:
    payload: Any = json.loads((ROOT / "release/native_runtime_ownership_v1.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert "one_table_one_authoritative_writer" in payload["invariants"]
