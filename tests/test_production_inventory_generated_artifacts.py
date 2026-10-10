from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_inventory_uses_tracked_build_recipe_without_generated_specs(tmp_path: Path, monkeypatch) -> None:
    name = "production_inventory_generated_artifact_test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/production_runtime_inventory.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    recipe = tmp_path / "scripts/build_exe.py"
    recipe.parent.mkdir()
    recipe.write_text("# PyInstaller recipe for legacy launch.py\n", encoding="utf-8")
    artifacts = {"desktop_pyinstaller_spec": "DeepSeekInfra.spec", "mobile_pyinstaller_spec": "DeepSeekMobile.spec"}
    for entry in module.LEGACY_ENTRIES:
        if entry["id"] in artifacts:
            record = module._entry_record(entry)
            assert record["path"] == "scripts/build_exe.py"
            assert record["generated_artifact"] == artifacts[entry["id"]]
            assert record["marker_present"] is True
