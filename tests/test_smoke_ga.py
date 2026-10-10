from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def _load_smoke_ga() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_ga.py"
    spec = importlib.util.spec_from_file_location("smoke_ga_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ga_smoke_writes_pass_evidence(tmp_path: Path) -> None:
    mod = _load_smoke_ga()
    out = tmp_path / "ga-evidence.json"

    code = mod.main(["--offline", "--out", str(out), "--version", "3.3.0"])
    evidence = json.loads(out.read_text(encoding="utf-8"))

    assert code == 0
    assert evidence["schemaVersion"] == "ga-smoke.v1"
    assert evidence["version"] == "3.3.0"
    assert evidence["status"] == "PASS"
    for check in (
        "workspaceHome",
        "project",
        "memory",
        "skill",
        "media",
        "browserSnapshot",
        "savedItem",
        "artifact",
        "automation",
        "export",
        "provenance",
        "exportRedaction",
    ):
        assert evidence["checks"][check] == "PASS"
    assert evidence["details"]["export"]["includes"]["projectId"]
    assert evidence["details"]["provenanceSummary"]["nodes"] >= 7
