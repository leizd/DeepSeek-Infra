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


FEATURES = {
    "settings": "src/features/settings/ConnectionSettingsFeature.tsx",
    "projects": "src/features/projects/ProjectsFeature.tsx",
    "skills": "src/features/skills/SkillsFeature.tsx",
    "memory": "src/features/memory/MemoryFeature.tsx",
    "reminders": "src/features/reminders/RemindersFeature.tsx",
    "diagnostics": "src/features/diagnostics/DiagnosticsFeature.tsx",
    "file-preview": "src/features/file-reader/FilePreviewFeature.tsx",
    "image-lightbox": "src/features/file-reader/ImageLightboxFeature.tsx",
    "activity": "src/features/activity/ActivityFeature.tsx",
}
BUILD_ID = "0123456789abcdef"
ASSET_SET_DIGEST = "a" * 64


def _write_offline_manifest(output: Path, payload: dict[str, object]) -> None:
    encoded = json.dumps(payload)
    (output / "workspace-assets.json").write_text(encoded, encoding="utf-8")
    (output / f"workspace-assets-{BUILD_ID}.json").write_text(encoded, encoding="utf-8")


def _bundle(root: Path, *, leak: bool = False, initial_size: int = 100) -> None:
    output = root / "static" / "ui"
    assets = output / "assets"
    manifest_dir = output / ".vite"
    assets.mkdir(parents=True)
    manifest_dir.mkdir()
    (root / "frontend").mkdir()
    (root / "frontend/package.json").write_text(json.dumps({"version": "4.3.2"}), encoding="utf-8")
    (output / "index.html").write_text(
        f'<meta name="deepseek-infra-build-id" content="{BUILD_ID}" />'
        '<meta name="deepseek-infra-source-revision" content="test-revision" />',
        encoding="utf-8",
    )
    for worker_name in (f"sw-{BUILD_ID}.js", f"sw-root-{BUILD_ID}.js"):
        (output / worker_name).write_text(
            "\n".join(
                (
                    f'const WORKER_BUILD_ID = "{BUILD_ID}";',
                    f'const WORKER_ASSET_SET_DIGEST = "{ASSET_SET_DIGEST}";',
                    f'const ASSET_MANIFEST_URL = "/ui/workspace-assets-{BUILD_ID}.json";',
                )
            ),
            encoding="utf-8",
        )
    entry_source = "Category summary" if leak else "w" * initial_size
    (assets / "index.js").write_text(entry_source, encoding="utf-8")
    (assets / "shared.js").write_text("shared", encoding="utf-8")
    (assets / "TracePage.js").write_text("trace page", encoding="utf-8")
    (assets / "TraceDetailView.js").write_text("Category summary Span tree trace-primary-grid", encoding="utf-8")
    (assets / "index.css").write_text("body{}", encoding="utf-8")
    (assets / "trace.css").write_text(".trace-primary-grid{}", encoding="utf-8")
    (assets / "recovery.js").write_text("recovery", encoding="utf-8")

    dynamic_imports = [*FEATURES.values(), "src/features/trace/TracePage.tsx"]
    manifest: dict[str, object] = {
        "_shared.js": {"file": "assets/shared.js"},
        "index.html": {
            "file": "assets/index.js",
            "isEntry": True,
            "imports": ["_shared.js"],
            "dynamicImports": dynamic_imports,
            "css": ["assets/index.css"],
        },
        "src/features/trace/TracePage.tsx": {
            "file": "assets/TracePage.js",
            "isDynamicEntry": True,
            "imports": ["src/features/trace/TraceDetailView.tsx"],
        },
        "src/features/trace/TraceDetailView.tsx": {
            "file": "assets/TraceDetailView.js",
            "isDynamicEntry": True,
            "css": ["assets/trace.css"],
        },
    }
    offline_primary: list[str] = []
    for feature, key in FEATURES.items():
        js_name = f"{feature}.js"
        css_name = f"{feature}.css"
        (assets / js_name).write_text(feature, encoding="utf-8")
        (assets / css_name).write_text(f".{feature}{{}}", encoding="utf-8")
        manifest[key] = {"file": f"assets/{js_name}", "isDynamicEntry": True, "css": [f"assets/{css_name}"]}
        offline_primary.extend([f"/ui/assets/{js_name}", f"/ui/assets/{css_name}"])
    (manifest_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _write_offline_manifest(
        output,
        {
            "schemaVersion": 1,
            "version": "4.3.2",
            "sourceRevision": "test-revision",
            "buildId": BUILD_ID,
            "assetSetDigest": ASSET_SET_DIGEST,
            "core": ["/ui/assets/index.js", "/ui/assets/shared.js", "/ui/assets/index.css"],
            "offlinePrimary": offline_primary,
            "recovery": ["/ui/assets/recovery.js"],
            "routeOptional": ["/ui/assets/TracePage.js", "/ui/assets/TraceDetailView.js", "/ui/assets/trace.css"],
        },
    )


def test_bundle_contract_accepts_workspace_demand_loading(tmp_path: Path) -> None:
    checker = _load_checker()
    _bundle(tmp_path)
    report = checker.check_bundle(tmp_path)
    assert report["checks"]["workspaceProjectsDynamicEntry"] == "PASS"
    assert report["checks"]["workspaceOptionalCssDeferred"] == "PASS"
    assert report["checks"]["workspaceOfflineAssetManifest"] == "PASS"
    assert report["initialBundleBudgetBytes"] == 390_000
    assert report["initialCssBudgetBytes"] == 28_000
    assert report["optionalChunkBudgetBytes"] == 90_000
    assert report["initialGraphBytes"] > report["initialBytes"]


def test_bundle_contract_rejects_trace_implementation_in_entry(tmp_path: Path) -> None:
    checker = _load_checker()
    _bundle(tmp_path, leak=True)
    with pytest.raises(AssertionError, match="leaked into the initial bundle"):
        checker.check_bundle(tmp_path)


def test_bundle_contract_rejects_missing_workspace_dynamic_entry(tmp_path: Path) -> None:
    checker = _load_checker()
    _bundle(tmp_path)
    manifest_path = tmp_path / "static/ui/.vite/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["index.html"]["dynamicImports"].remove(FEATURES["skills"])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(AssertionError, match="Workspace dynamic entries are missing"):
        checker.check_bundle(tmp_path)


def test_bundle_contract_rejects_primary_asset_missing_from_offline_manifest(tmp_path: Path) -> None:
    checker = _load_checker()
    _bundle(tmp_path)
    offline_path = tmp_path / "static/ui/workspace-assets.json"
    offline = json.loads(offline_path.read_text(encoding="utf-8"))
    offline["offlinePrimary"].remove("/ui/assets/skills.js")
    _write_offline_manifest(offline_path.parent, offline)
    with pytest.raises(AssertionError, match="offline manifest is missing primary assets"):
        checker.check_bundle(tmp_path)


def test_bundle_contract_rejects_recovery_assets_in_primary_warmup(tmp_path: Path) -> None:
    checker = _load_checker()
    _bundle(tmp_path)
    offline_path = tmp_path / "static/ui/workspace-assets.json"
    offline = json.loads(offline_path.read_text(encoding="utf-8"))
    offline["offlinePrimary"].append(offline["recovery"].pop())
    _write_offline_manifest(offline_path.parent, offline)
    with pytest.raises(AssertionError, match="no on-demand recovery assets"):
        checker.check_bundle(tmp_path)


def test_bundle_contract_rejects_less_than_eight_percent_reduction(tmp_path: Path) -> None:
    checker = _load_checker()
    _bundle(tmp_path, initial_size=400_000)
    with pytest.raises(AssertionError, match="exceeds budget"):
        checker.check_bundle(tmp_path)
