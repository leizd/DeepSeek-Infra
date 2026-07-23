#!/usr/bin/env python3
"""Verify Workspace demand-loading, budgets, and offline chunk inventory."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deepseek_infra.infra.diagnostics.evidence_revision import evidence_revision  # noqa: E402

MANIFEST_PATH = Path("static/ui/.vite/manifest.json")
OFFLINE_MANIFEST_PATH = Path("static/ui/workspace-assets.json")
ENTRY_KEY = "index.html"
TRACE_PAGE_KEY = "src/features/trace/TracePage.tsx"
TRACE_DETAIL_KEY = "src/features/trace/TraceDetailView.tsx"
TRACE_MARKERS = ("Category summary", "Span tree", "trace-primary-grid")
WORKSPACE_FEATURE_KEYS = {
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
UTILITY_FEATURES = ("reminders", "diagnostics", "file-preview", "image-lightbox", "activity")
BASELINE_428_INITIAL_BYTES = 425_914
MAX_INITIAL_BYTES = 390_000
MAX_INITIAL_CSS_BYTES = 28_000
MAX_OPTIONAL_CHUNK_BYTES = 90_000
MINIMUM_REDUCTION_PERCENT = 8


def _entry(manifest: dict[str, Any], key: str) -> dict[str, Any]:
    value = manifest.get(key)
    if not isinstance(value, dict):
        raise AssertionError(f"Vite manifest is missing {key}")
    return value


def _asset(root: Path, entry: dict[str, Any]) -> Path:
    name = entry.get("file")
    if not isinstance(name, str) or not name:
        raise AssertionError("Vite manifest entry has no output file")
    path = root / "static" / "ui" / name
    if not path.is_file():
        raise AssertionError(f"Vite output is missing {name}")
    return path


def _asset_name(entry: dict[str, Any]) -> str:
    name = entry.get("file")
    if not isinstance(name, str) or not name:
        raise AssertionError("Vite manifest entry has no output file")
    return name


def _initial_graph_keys(manifest: dict[str, Any]) -> set[str]:
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visited:
            return
        visited.add(key)
        entry = _entry(manifest, key)
        for dependency in entry.get("imports") or []:
            if isinstance(dependency, str):
                visit(dependency)

    visit(ENTRY_KEY)
    return visited


def _css_assets(root: Path, entry: dict[str, Any]) -> list[Path]:
    assets: list[Path] = []
    for name in entry.get("css") or []:
        if not isinstance(name, str):
            continue
        path = root / "static" / "ui" / name
        if not path.is_file():
            raise AssertionError(f"CSS output is missing {name}")
        assets.append(path)
    return assets


def _entry_graph_css(
    root: Path,
    manifest: dict[str, Any],
    key: str,
    *,
    stop_keys: frozenset[str] = frozenset(),
) -> list[Path]:
    visited: set[str] = set()
    assets: dict[str, Path] = {}

    def visit(current: str) -> None:
        if current in visited or current in stop_keys:
            return
        visited.add(current)
        entry = _entry(manifest, current)
        for asset in _css_assets(root, entry):
            assets[asset.as_posix()] = asset
        for dependency in entry.get("imports") or []:
            if isinstance(dependency, str):
                visit(dependency)

    visit(key)
    return list(assets.values())


def _load_offline_manifest(root: Path) -> dict[str, Any]:
    path = root / OFFLINE_MANIFEST_PATH
    if not path.is_file():
        raise AssertionError("Workspace offline asset manifest is missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("buildId"), str):
        raise AssertionError("Workspace offline asset manifest has no buildId")
    asset_groups = ("core", "offlinePrimary", "recovery", "routeOptional")
    for field in asset_groups:
        values = payload.get(field)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise AssertionError(f"Workspace offline asset manifest has invalid {field} assets")
        for value in values:
            if not (root / "static" / "ui" / value.removeprefix("/ui/")).is_file():
                raise AssertionError(f"Workspace offline asset manifest references missing output {value}")
    seen: set[str] = set()
    for field in asset_groups:
        overlap = seen & set(payload[field])
        if overlap:
            raise AssertionError(f"Workspace offline asset groups overlap at {sorted(overlap)[0]}")
        seen.update(payload[field])
    return payload


def check_bundle(root: Path) -> dict[str, Any]:
    manifest_path = root / MANIFEST_PATH
    if not manifest_path.is_file():
        raise AssertionError("React build manifest is missing; run npm run build --prefix frontend")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise AssertionError("Vite manifest root must be an object")

    entry = _entry(manifest, ENTRY_KEY)
    dynamic_imports = entry.get("dynamicImports")
    if not isinstance(dynamic_imports, list):
        raise AssertionError("frontend entry does not declare dynamic imports")
    missing_features = [key for key in WORKSPACE_FEATURE_KEYS.values() if key not in dynamic_imports]
    if missing_features:
        raise AssertionError(f"Workspace dynamic entries are missing: {', '.join(missing_features)}")

    feature_entries: dict[str, dict[str, Any]] = {}
    feature_assets: dict[str, Path] = {}
    for feature, key in WORKSPACE_FEATURE_KEYS.items():
        feature_entry = _entry(manifest, key)
        if feature_entry.get("isDynamicEntry") is not True:
            raise AssertionError(f"Workspace feature must remain dynamic: {feature}")
        feature_entries[feature] = feature_entry
        feature_assets[feature] = _asset(root, feature_entry)

    trace_page = _entry(manifest, TRACE_PAGE_KEY)
    trace_detail = _entry(manifest, TRACE_DETAIL_KEY)
    if TRACE_PAGE_KEY not in dynamic_imports:
        raise AssertionError("Trace page is missing from the workspace dynamic imports")
    if trace_page.get("isDynamicEntry") is not True or trace_detail.get("isDynamicEntry") is not True:
        raise AssertionError("Trace page and detail view must remain dynamic entries")

    entry_asset = _asset(root, entry)
    trace_page_asset = _asset(root, trace_page)
    trace_detail_asset = _asset(root, trace_detail)
    if entry_asset.stat().st_size > MAX_INITIAL_BYTES:
        raise AssertionError(f"initial frontend bundle exceeds budget: {entry_asset.stat().st_size} > {MAX_INITIAL_BYTES}")
    reduction_percent = 100 * (BASELINE_428_INITIAL_BYTES - entry_asset.stat().st_size) / BASELINE_428_INITIAL_BYTES
    if reduction_percent < MINIMUM_REDUCTION_PERCENT:
        raise AssertionError(
            f"initial frontend bundle reduction is {reduction_percent:.2f}%, expected at least {MINIMUM_REDUCTION_PERCENT}%"
        )

    initial_graph_keys = _initial_graph_keys(manifest)
    initial_graph_assets = [_asset(root, _entry(manifest, key)) for key in initial_graph_keys]
    entry_css = _entry_graph_css(root, manifest, ENTRY_KEY)
    initial_css_bytes = sum(path.stat().st_size for path in entry_css)
    if initial_css_bytes > MAX_INITIAL_CSS_BYTES:
        raise AssertionError(f"initial frontend CSS exceeds budget: {initial_css_bytes} > {MAX_INITIAL_CSS_BYTES}")

    entry_source = entry_asset.read_text(encoding="utf-8")
    detail_source = trace_detail_asset.read_text(encoding="utf-8")
    leaked = [marker for marker in TRACE_MARKERS if marker in entry_source]
    missing_markers = [marker for marker in TRACE_MARKERS if marker not in detail_source]
    if leaked:
        raise AssertionError(f"Trace implementation leaked into the initial bundle: {', '.join(leaked)}")
    if missing_markers:
        raise AssertionError(f"Trace detail chunk is missing expected markers: {', '.join(missing_markers)}")

    entry_css_names = {path.relative_to(root / "static" / "ui").as_posix() for path in entry_css}
    workspace_css_names: set[str] = set()
    feature_css_names: dict[str, list[str]] = {}
    for feature, key in WORKSPACE_FEATURE_KEYS.items():
        css_assets = _entry_graph_css(root, manifest, key, stop_keys=frozenset({ENTRY_KEY}))
        if not css_assets:
            raise AssertionError(f"Workspace feature has no owned CSS output: {feature}")
        feature_css_names[feature] = sorted(path.relative_to(root / "static" / "ui").as_posix() for path in css_assets)
        workspace_css_names.update(feature_css_names[feature])
    if entry_css_names & workspace_css_names:
        raise AssertionError("Optional Workspace CSS is included in the initial stylesheet")

    trace_css = {
        path.relative_to(root / "static" / "ui").as_posix()
        for path in _css_assets(root, trace_detail)
    }
    if not trace_css or entry_css_names & trace_css:
        raise AssertionError("Trace CSS must remain feature-owned and deferred")

    offline = _load_offline_manifest(root)
    expected_primary = {
        f"/ui/{_asset_name(feature_entry)}"
        for feature_entry in feature_entries.values()
    }
    expected_primary.update(f"/ui/{name}" for name in workspace_css_names)
    missing_offline = sorted(expected_primary - set(offline["offlinePrimary"]))
    if missing_offline:
        raise AssertionError(f"Workspace offline manifest is missing primary assets: {', '.join(missing_offline)}")
    if not offline["recovery"]:
        raise AssertionError("Workspace offline manifest has no on-demand recovery assets")
    if any("workspace-retry" in value for value in offline["offlinePrimary"]):
        raise AssertionError("Workspace recovery chunks must not enter the primary warmup layer")
    expected_route_optional = {
        f"/ui/{_asset_name(trace_page)}",
        f"/ui/{_asset_name(trace_detail)}",
        *(f"/ui/{name}" for name in trace_css),
    }
    missing_routes = sorted(expected_route_optional - set(offline["routeOptional"]))
    if missing_routes:
        raise AssertionError(f"Workspace offline manifest is missing route-optional assets: {', '.join(missing_routes)}")
    optional_js = [
        root / "static" / "ui" / name.removeprefix("/ui/")
        for field in ("offlinePrimary", "recovery", "routeOptional")
        for name in offline[field]
        if name.endswith(".js")
    ]
    oversized = [path for path in optional_js if path.stat().st_size > MAX_OPTIONAL_CHUNK_BYTES]
    if oversized:
        raise AssertionError(f"optional frontend chunk exceeds budget: {oversized[0].name}")

    feature_chunks = {
        feature: {
            "path": asset.relative_to(root).as_posix(),
            "bytes": asset.stat().st_size,
            "css": feature_css_names[feature],
        }
        for feature, asset in feature_assets.items()
    }
    return {
        "baseline428InitialBytes": BASELINE_428_INITIAL_BYTES,
        "minimumReductionPercent": MINIMUM_REDUCTION_PERCENT,
        "initialEntry": entry_asset.relative_to(root).as_posix(),
        "initialBytes": entry_asset.stat().st_size,
        "initialGraphBytes": sum(path.stat().st_size for path in initial_graph_assets),
        "initialReductionPercent": round(reduction_percent, 2),
        "initialBundleBudgetBytes": MAX_INITIAL_BYTES,
        "initialCss": sorted(entry_css_names),
        "initialCssBytes": initial_css_bytes,
        "initialCssBudgetBytes": MAX_INITIAL_CSS_BYTES,
        "optionalChunkBudgetBytes": MAX_OPTIONAL_CHUNK_BYTES,
        "workspaceFeatureChunks": feature_chunks,
        "workspaceOfflineManifest": OFFLINE_MANIFEST_PATH.as_posix(),
        "workspaceBuildId": offline["buildId"],
        "tracePageChunk": trace_page_asset.relative_to(root).as_posix(),
        "tracePageBytes": trace_page_asset.stat().st_size,
        "traceDetailChunk": trace_detail_asset.relative_to(root).as_posix(),
        "traceDetailBytes": trace_detail_asset.stat().st_size,
        "traceCss": sorted(trace_css),
        "checks": {
            "workspaceProjectsDynamicEntry": "PASS",
            "workspaceSkillsDynamicEntry": "PASS",
            "workspaceMemoryDynamicEntry": "PASS",
            "workspaceSettingsDynamicEntry": "PASS",
            "workspaceUtilitiesDynamicEntry": "PASS",
            "workspaceOptionalCssDeferred": "PASS",
            "initialBundleReducedFrom428": "PASS",
            "initialBundleBudget": "PASS",
            "initialCssBudget": "PASS",
            "optionalFeatureChunkBudget": "PASS",
            "workspaceOfflineAssetManifest": "PASS",
            "workspacePrimaryWarmLayer": "PASS",
            "workspaceRecoveryChunksDeferred": "PASS",
            "routeOptionalChunksSeparated": "PASS",
            "tracePageDynamicEntry": "PASS",
            "traceDetailDynamicEntry": "PASS",
            "traceImplementationDeferred": "PASS",
            "traceCssDeferred": "PASS",
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    try:
        report = check_bundle(root)
    except (AssertionError, json.JSONDecodeError, OSError) as exc:
        print(f"frontend bundle check failed: {exc}", file=sys.stderr)
        return 1
    payload = {
        "schemaVersion": 1,
        "version": _frontend_version(root),
        **evidence_revision(root),
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "environment": {"os": platform.system(), "python": platform.python_version(), "ci": bool(os.getenv("CI"))},
        "status": "PASS",
        **report,
    }
    if args.out:
        output = args.out if args.out.is_absolute() else root / args.out
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _frontend_version(root: Path) -> str:
    package = json.loads((root / "frontend" / "package.json").read_text(encoding="utf-8"))
    return str(package.get("version") or "")


if __name__ == "__main__":
    raise SystemExit(main())
