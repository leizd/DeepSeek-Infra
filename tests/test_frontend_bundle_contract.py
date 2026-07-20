from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_checker() -> ModuleType:
    path = ROOT / "scripts" / "check_frontend_bundle.py"
    spec = importlib.util.spec_from_file_location("check_frontend_bundle_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle(root: Path, *, leak: bool = False) -> None:
    output = root / "static" / "ui"
    assets = output / "assets"
    manifest_dir = output / ".vite"
    assets.mkdir(parents=True)
    manifest_dir.mkdir()
    entry_source = "Category summary" if leak else "workspace entry"
    (assets / "index.js").write_text(entry_source, encoding="utf-8")
    (assets / "TracePage.js").write_text("trace page", encoding="utf-8")
    (assets / "TraceDetailView.js").write_text(
        "Category summary Span tree trace-primary-grid", encoding="utf-8"
    )
    (assets / "index.css").write_text("body{}", encoding="utf-8")
    (assets / "trace.css").write_text(".trace-primary-grid{}", encoding="utf-8")
    manifest = {
        "index.html": {
            "file": "assets/index.js",
            "isEntry": True,
            "dynamicImports": [
                "src/features/trace/TraceDetailView.tsx",
                "src/features/trace/TracePage.tsx",
            ],
            "css": ["assets/index.css"],
        },
        "src/features/trace/TracePage.tsx": {
            "file": "assets/TracePage.js",
            "isDynamicEntry": True,
        },
        "src/features/trace/TraceDetailView.tsx": {
            "file": "assets/TraceDetailView.js",
            "isDynamicEntry": True,
            "css": ["assets/trace.css"],
        },
    }
    (manifest_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_bundle_contract_accepts_deferred_trace_assets(tmp_path: Path) -> None:
    checker = _load_checker()
    _bundle(tmp_path)
    report = checker.check_bundle(tmp_path)
    assert report["checks"] == {
        "tracePageDynamicEntry": "PASS",
        "traceDetailDynamicEntry": "PASS",
        "traceImplementationDeferred": "PASS",
        "traceCssDeferred": "PASS",
        "initialBundleBudget": "PASS",
    }
    assert report["initialBundleBudgetBytes"] == 450_000


def test_bundle_contract_rejects_trace_implementation_in_entry(tmp_path: Path) -> None:
    checker = _load_checker()
    _bundle(tmp_path, leak=True)
    with pytest.raises(AssertionError, match="leaked into the initial bundle"):
        checker.check_bundle(tmp_path)
