"""Seal and verify cloud-only Mac executable/static fixtures; never export them to the host."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GO_ROLES = ("deepseekd", "deepseek-launch", "deepseek-desktop")
CPUS = {"amd64": 0x01000007, "arm64": 0x0100000C}


def failure() -> RuntimeError:
    return RuntimeError("MACOS_PRODUCT_FIXTURE_INVALID")


def macho(path: Path, architecture: str) -> None:
    with path.open("rb") as stream:
        header = stream.read(32)
    if len(header) != 32 or struct.unpack_from("<II", header) != (0xFEEDFACF, CPUS[architecture]):
        raise failure()


def source() -> tuple[str, str]:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip()
    if commit != os.environ.get("GITHUB_SHA") or subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
    ):
        raise RuntimeError("MACOS_PRODUCT_EXACT_CLEAN_SOURCE_REQUIRED")
    return commit, tree


def verify_fixture(root: Path, commit: str, tree: str, run: str, attempt: str, pin: dict[str, Any]) -> dict[str, Any]:
    root = root.resolve()
    report = json.loads((root / "product-fixture.json").read_bytes())
    expected = (commit, tree, run, attempt, "go version go" + pin["version"] + " linux/amd64",
                pin["artifacts"]["linux_amd64"]["sha256"], False)
    observed = tuple(report.get(key) for key in ["sourceCommit", "sourceTree", "runId", "attempt", "goVersion",
                                                "linuxGoArchiveSha256", "cgoEnabled"])
    if observed != expected or report["cgoEnabled"] is not False:
        raise failure()
    rows = report["files"]
    names = [row["path"] for row in rows]
    actual = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p != root / "product-fixture.json"]
    if len(names) != len(set(names)) or sorted(names) != sorted(actual):
        raise failure()
    required = {arch + "/" + role for arch in CPUS for role in GO_ROLES}
    if not required <= set(names) or not {"static/ui/index.html", "static/ui/.vite/manifest.json"} <= set(names):
        raise failure()
    for row in rows:
        name = row["path"]
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
            raise failure()
        if name not in required and not name.startswith("static/"):
            raise failure()
        path = root / relative
        if not path.is_file() or not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in [path, *path.parents]):
            raise failure()
        if path.stat().st_size != row["bytes"] or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise failure()
        if name in required:
            macho(path, relative.parts[0])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--seal", action="store_true")
    mode.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise RuntimeError("MACOS_PRODUCT_FIXTURE_REQUIRES_CLOUD_DISPATCH")
    if args.seal and sys.platform != "linux":
        raise RuntimeError("MACOS_PRODUCT_FIXTURE_REQUIRES_PINNED_LINUX_CROSS_COMPILER")
    commit, tree = source()
    root = args.fixture.resolve()
    pin = json.loads((ROOT / "release/native_runtime_toolchain_v1.json").read_bytes())["go"]
    if args.seal:
        if os.environ.get("CGO_ENABLED") != "0" or subprocess.check_output(["go", "env", "CGO_ENABLED"], text=True).strip() != "0":
            raise RuntimeError("MACOS_PRODUCT_FIXTURE_REQUIRES_CGO_DISABLED")
        if (root / "product-fixture.json").exists():
            raise failure()
        files = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise failure()
            if path.is_file():
                files.append(dict(path=path.relative_to(root).as_posix(), bytes=path.stat().st_size,
                                  sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        value = dict(sourceCommit=commit, sourceTree=tree, runId=os.environ["GITHUB_RUN_ID"],
                     attempt=os.environ["GITHUB_RUN_ATTEMPT"], goVersion=subprocess.check_output(["go", "version"], text=True).strip(),
                     linuxGoArchiveSha256=pin["artifacts"]["linux_amd64"]["sha256"], cgoEnabled=False, files=files)
        (root / "product-fixture.json").write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    result = verify_fixture(root, commit, tree, os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], pin)
    print(json.dumps(dict(status="PASS", sourceCommit=commit, sourceTree=tree, files=len(result["files"]),
                          cloudOnlyFixture=True, fullMigrationQualified=False)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
