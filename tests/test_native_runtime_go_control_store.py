from __future__ import annotations

import json
from pathlib import Path

from scripts.native_runtime_contract import load_ownership


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "release" / "native_runtime_go_control_store_v1.json"


def test_go_control_tables_cover_go_owned_stores() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    ownership = load_ownership()
    assert catalog["runtime"] == "go"
    assert catalog["mode"] == "shadow"
    assert catalog["unique_writer"]["table"] == "writer.json"
    covered: set[str] = set()
    for table in catalog["tables"]:
        covered.update(table["ownership_ids"])
    covered.update(catalog["writer_plan"].keys())
    go_control = {
        item["id"]
        for item in ownership["domains"]
        if item.get("durable_store") == "go_control"
    }
    assert go_control <= covered


def test_go_schema_matches_catalog_and_rejects_python_paths() -> None:
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    schema = (ROOT / "go/internal/store/schema.go").read_text(encoding="utf-8")
    control = (ROOT / "go/internal/store/control.go").read_text(encoding="utf-8")
    for table in catalog["tables"]:
        assert f'"{table["name"]}"' in schema
        assert f'"{table["domain"]}"' in schema
    assert "ErrPythonStorePath" in schema
    assert "ErrWriterFenceHeld" in schema
    assert "DenyMutation" in control
    assert "C.CString" not in control
    assert "database/sql" not in control
    for part in catalog["forbidden_python_path_components"]:
        assert part in schema


def test_python_shadow_export_report(tmp_path: Path) -> None:
    from scripts.control_plane_shadow import export_report

    out = tmp_path / "shadow-report.json"
    payload = export_report(out)
    assert payload["kernel"] == "control-shadow-decision-v1"
    assert payload["mutationDenied"] is True
    assert payload["cases"]
    assert all(item["digest"] for item in payload["cases"])
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["cases"] == payload["cases"]
