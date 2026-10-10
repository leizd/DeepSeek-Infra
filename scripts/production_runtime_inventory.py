"""Inventory production entries and ownership domains still on Python or server-side TypeScript.

The committed snapshot is ``release/production_runtime_inventory_v1.json``.
``--check`` fails when that snapshot drifts from the sources this script reads.
Python remains legal for offline tests, evals, oracles, and migration tools;
those are not production entries.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[1]
OWNERSHIP_PATH = ROOT / "release" / "native_runtime_ownership_v1.json"
SNAPSHOT_PATH = ROOT / "release" / "production_runtime_inventory_v1.json"

ROUTE_RE = re.compile(r'@router\.(get|post|put|patch|delete|api_route)\(\s*"([^"]+)"')
RUST_ROUTE_RE = re.compile(r'\.route\(\s*"([^"]+)"')
GO_ROUTE_RE = re.compile(r'HandleFunc\("([^"]+)"')
PARAM_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")

# Live production entry points. ``marker`` is a source string that proves the
# entry still executes Python or server-side TypeScript. Native packaging
# inputs are listed separately and must not carry these markers.
LEGACY_ENTRIES = (
    {
        "id": "root_app_shim",
        "path": "app.py",
        "kind": "server",
        "executor": "python",
        "marker": "deepseek_infra.app",
    },
    {
        "id": "python_http_server",
        "path": "deepseek_infra/app.py",
        "kind": "server",
        "executor": "python",
        "marker": "assert_production_python_allowed",
    },
    {
        "id": "launch_desktop_server_mobile",
        "path": "launch.py",
        "kind": "desktop",
        "executor": "python",
        "marker": "deepseek_infra.desktop_app",
    },
    {
        "id": "desktop_webview",
        "path": "deepseek_infra/desktop_app.py",
        "kind": "desktop",
        "executor": "python",
        "marker": "webview",
    },
    {
        "id": "launcher_server_command",
        "path": "deepseek_infra/launcher/runtime.py",
        "kind": "desktop",
        "executor": "python",
        "marker": "deepseek_infra.app",
    },
    {
        "id": "launcher_gui",
        "path": "deepseek_infra/launcher/gui.py",
        "kind": "desktop",
        "executor": "python",
        "marker": "tkinter",
    },
    {
        "id": "launcher_mobile",
        "path": "deepseek_infra/launcher/mobile.py",
        "kind": "desktop",
        "executor": "python",
        "marker": "def main(",
    },
    {
        "id": "android_python_bridge",
        "path": "deepseek_infra/android_entry.py",
        "kind": "android",
        "executor": "python",
        "marker": "prepare_and_start",
    },
    {
        "id": "desktop_pyinstaller_spec",
        "path": "scripts/build_exe.py",
        "kind": "desktop",
        "executor": "python",
        "marker": "PyInstaller",
        "generated_artifact": "DeepSeekInfra.spec",
    },
    {
        "id": "mobile_pyinstaller_spec",
        "path": "scripts/build_exe.py",
        "kind": "desktop",
        "executor": "python",
        "marker": "PyInstaller",
        "generated_artifact": "DeepSeekMobile.spec",
    },
    {
        "id": "pyinstaller_build",
        "path": "scripts/build_exe.py",
        "kind": "desktop",
        "executor": "python",
        "marker": "PyInstaller",
    },
    {
        "id": "backup_worker",
        "path": "deepseek_infra/backup_worker.py",
        "kind": "background_worker",
        "executor": "python",
        "marker": "BackupWorker",
    },
    {
        "id": "federation_node",
        "path": "deepseek_infra/federation_app.py",
        "kind": "server",
        "executor": "python",
        "marker": "uvicorn",
    },
    {
        "id": "federation_http_app",
        "path": "deepseek_infra/web/federation_app.py",
        "kind": "server",
        "executor": "python",
        "marker": "FastAPI",
    },
)

# These inputs are native, but they are not yet the only production entry.
# Android looks for a gateway binary that the APK build does not package.
NATIVE_OR_GAP_ENTRIES = (
    {
        "id": "root_container_image",
        "path": "Dockerfile",
        "kind": "server",
        "executor": "rust_go",
        "marker": "deepseek-gateway",
    },
    {
        "id": "root_compose",
        "path": "docker-compose.yml",
        "kind": "server",
        "executor": "rust_go",
        "marker": "deepseek-infra:5.0.0",
    },
    {
        "id": "native_compose",
        "path": "docker-compose.native.yml",
        "kind": "server",
        "executor": "rust_go",
        "marker": "DEEPSEEKD_MODE: authoritative",
    },
    {
        "id": "native_entrypoint",
        "path": "packaging/native/entrypoint.sh",
        "kind": "server",
        "executor": "rust_go",
        "marker": "exec deepseek-launch --server",
    },
    {
        "id": "android_activity",
        "path": "android/app/src/main/java/com/deepseek/mobile/MainActivity.java",
        "kind": "android",
        "executor": "rust_go_unpackaged",
        "marker": "startNativeServer",
        "packaged_native_binary": False,
    },
    {
        "id": "native_supervisor",
        "path": "go/cmd/deepseek-launch/main.go",
        "kind": "server",
        "executor": "rust_go",
        "marker": "launch.ProductionPlan",
    },
    {
        "id": "stateless_mcp_server",
        "path": "rust/crates/deepseek-stateless-mcp/src/main.rs",
        "kind": "stateless_mcp",
        "executor": "rust",
        "marker": "stateless_mcp_listening",
    },
    {
        "id": "stateless_mcp_image",
        "path": "stateless-mcp/Dockerfile",
        "kind": "stateless_mcp",
        "executor": "rust",
        "marker": "deepseek-stateless-mcp",
    },
    {
        "id": "stateless_mcp_compose",
        "path": "docker-compose.stateless-mcp.yml",
        "kind": "stateless_mcp",
        "executor": "rust",
        "marker": "deepseek-stateless-mcp",
    },
    {
        "id": "stateless_mcp_task_worker",
        "path": "rust/crates/deepseek-stateless-mcp/src/runner.rs",
        "kind": "background_worker",
        "executor": "rust",
        "marker": "MCP_TASK_PROGRAM",
    },
)

BACKGROUND_WORKERS_STILL_PYTHON = (
    {
        "id": "embedded_backup_worker",
        "owner_module": "deepseek_infra/backup_worker.py",
        "started_from": "deepseek_infra/app.py",
    },
    {
        "id": "recovery_lease_keeper",
        "owner_module": "deepseek_infra/infra/workspace/backup_recovery_keeper.py",
        "started_from": "deepseek_infra/app.py",
    },
    {
        "id": "gateway_scheduler_orphan_recovery",
        "owner_module": "deepseek_infra/infra/gateway/scheduler.py",
        "started_from": "deepseek_infra/app.py",
    },
    {
        "id": "cache_cleanup_loop",
        "owner_module": "deepseek_infra/app.py",
        "started_from": "deepseek_infra/app.py",
    },
    {
        "id": "agent_run_auto_resume",
        "owner_module": "deepseek_infra/infra/agent_runtime/agent_runs.py",
        "started_from": "deepseek_infra/app.py",
    },
    {
        "id": "automation_scheduler",
        "owner_module": "deepseek_infra/infra/automation/scheduler.py",
        "started_from": "deepseek_infra/web/routes/automation.py",
    },
)


def _posix(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _normalize(path: str) -> str:
    return PARAM_RE.sub(r":\1", path)


def _python_routes() -> list[dict[str, str]]:
    routes: list[dict[str, str]] = []
    for path in sorted((ROOT / "deepseek_infra" / "web" / "routes").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for match in ROUTE_RE.finditer(text):
            routes.append({"method": match.group(1), "path": match.group(2), "file": _posix(path)})
    return routes


def _native_route_paths() -> set[str]:
    found: set[str] = set()
    gateway = ROOT / "rust" / "crates" / "deepseek-gateway" / "src"
    for path in gateway.rglob("*.rs"):
        found.update(RUST_ROUTE_RE.findall(path.read_text(encoding="utf-8")))
    for path in (ROOT / "go").rglob("*.go"):
        if path.name.endswith("_test.go"):
            continue
        found.update(GO_ROUTE_RE.findall(path.read_text(encoding="utf-8")))
    return found


def _routes_only_on_python(native_paths: set[str]) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    for route in _python_routes():
        if _normalize(route["path"]) not in native_paths:
            missing.append(route)
    return missing


def _domains() -> tuple[list[str], list[str], list[str], str]:
    document = json.loads(OWNERSHIP_PATH.read_text(encoding="utf-8"))
    python_ids: list[str] = []
    rust_ids: list[str] = []
    go_ids: list[str] = []
    for item in document["domains"]:
        if item.get("production", True) is not True:
            continue
        owner = str(item.get("current_owner") or "")
        domain_id = str(item["id"])
        if owner == "python":
            python_ids.append(domain_id)
        elif owner == "rust":
            rust_ids.append(domain_id)
        elif owner == "go":
            go_ids.append(domain_id)
    return python_ids, rust_ids, go_ids, str(document["current_production_authority"])


def _entry_record(spec: Mapping[str, object]) -> dict[str, object]:
    path = ROOT / str(spec["path"])
    text = path.read_text(encoding="utf-8")
    marker = str(spec["marker"])
    record = {
        "id": spec["id"],
        "path": spec["path"],
        "kind": spec["kind"],
        "executor": spec["executor"],
        "marker": marker,
        "marker_present": marker in text,
    }
    if "packaged_native_binary" in spec:
        record["packaged_native_binary"] = spec["packaged_native_binary"]
    if "generated_artifact" in spec:
        record["generated_artifact"] = spec["generated_artifact"]
    return record


class ProductionInventory(TypedDict):
    schema: str
    current_production_authority: str
    summary: str
    legacy_production_entries: list[dict[str, object]]
    native_or_gap_entries: list[dict[str, object]]
    background_workers_still_python_or_typescript: list[dict[str, str]]
    ownership_domains_current_owner_python: list[str]
    ownership_domains_current_owner_rust: list[str]
    ownership_domains_current_owner_go: list[str]
    non_production_left_on_python_or_typescript: list[str]
    http_routes_only_on_python: list[dict[str, str]]


def build_inventory() -> ProductionInventory:
    python_ids, rust_ids, go_ids, authority = _domains()
    native_paths = _native_route_paths()
    return {
        "schema": "deepseek.production_runtime_inventory.v1",
        "current_production_authority": authority,
        "summary": (
            "Default server, desktop, Android bridge, backup worker, and federation node "
            "still execute Python. The stateless MCP server and its task worker execute "
            "in Rust. Container entrypoints can start Rust and Go, and twelve ownership "
            "domains are declared rust, but those domains are not the sole executors "
            "while the Python entries above remain. current_production_authority stays "
            "python until every production domain has left Python."
        ),
        "legacy_production_entries": [_entry_record(item) for item in LEGACY_ENTRIES],
        "native_or_gap_entries": [_entry_record(item) for item in NATIVE_OR_GAP_ENTRIES],
        "background_workers_still_python_or_typescript": list(BACKGROUND_WORKERS_STILL_PYTHON),
        "ownership_domains_current_owner_python": python_ids,
        "ownership_domains_current_owner_rust": rust_ids,
        "ownership_domains_current_owner_go": go_ids,
        "non_production_left_on_python_or_typescript": [
            "offline_eval_oracle",
            "migration_release_tooling",
            "browser_ui",
        ],
        "http_routes_only_on_python": _routes_only_on_python(native_paths),
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    inventory = build_inventory()
    encoded = json.dumps(inventory, indent=2, ensure_ascii=False) + "\n"
    if args == ["--check"]:
        current = SNAPSHOT_PATH.read_text(encoding="utf-8")
        if current != encoded:
            print("production runtime inventory snapshot is stale", file=sys.stderr)
            return 1
        missing = [item["id"] for item in inventory["legacy_production_entries"] if not item["marker_present"]]
        if missing:
            print("legacy markers missing: " + ", ".join(str(item) for item in missing), file=sys.stderr)
            return 1
        if inventory["ownership_domains_current_owner_go"]:
            print("unexpected go-owned production domains", file=sys.stderr)
            return 1
        if not inventory["ownership_domains_current_owner_python"]:
            print("inventory lost the remaining python domains", file=sys.stderr)
            return 1
        return 0
    if args not in ([], ["--write"]):
        print("usage: production_runtime_inventory.py [--write|--check]", file=sys.stderr)
        return 2
    SNAPSHOT_PATH.write_text(encoded, encoding="utf-8", newline="\n")
    print(f"wrote {SNAPSHOT_PATH.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
