from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


def _load_smoke_workspace() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "smoke_workspace.py"
    spec = importlib.util.spec_from_file_location("smoke_workspace_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_workspace_smoke_writes_pass_evidence(tmp_path: Path) -> None:
    mod = _load_smoke_workspace()
    out = tmp_path / "workspace-evidence.json"

    code = mod.main(["--offline", "--out", str(out)])
    evidence = json.loads(out.read_text(encoding="utf-8"))

    assert code == 0
    assert evidence["status"] == "PASS"
    assert evidence["version"]
    for check in ("projectCreate", "savedItemCreate", "artifactList", "conversationExport", "projectExportZip", "secretRedaction"):
        assert evidence["checks"][check] == "PASS"
    entries = set(evidence["details"]["projectExport"]["entries"])
    assert "metadata.json" in entries
    assert "saved-items/saved-items.json" in entries
    assert any(name.startswith("artifacts/") for name in entries)
