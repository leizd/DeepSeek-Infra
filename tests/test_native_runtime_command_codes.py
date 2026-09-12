from __future__ import annotations

import json
from pathlib import Path

from scripts.native_runtime_contract import validate_command_codes


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "release" / "native_runtime_command_codes_v1.json"


def test_go_and_rust_native_command_codes_match() -> None:
    data = json.loads(CONTRACT.read_text(encoding="utf-8"))
    report = validate_command_codes()
    assert report["codes"] == data["codes"]
    assert report["commands"]["ExecuteBackup"] == "STORAGE_NOT_AUTHORITATIVE"
    assert report["commands"]["VerifyProof"] == "PROOF_NOT_AUTHORITATIVE"
    assert report["production_code"] == "MUTATION_DENIED"
    assert "ExecuteFederatedTransfer" in report["production_execute"]
    assert "deepseek-proof" in report["rust_crates"]
