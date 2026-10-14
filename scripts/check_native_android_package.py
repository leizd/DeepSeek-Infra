"""Inspect a built native APK; passing does not qualify ownership or all product features."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from build_android_native import PYTHON_SUFFIXES, inspect_executable


def inspect_package(apk: Path) -> dict[str, Any]:
    with zipfile.ZipFile(apk) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError("Duplicate APK entries")
        forbidden = [name for name in names if Path(name).suffix.lower() in PYTHON_SUFFIXES
                     or any(value in name.lower() for value in ("chaquopy", "libpython", "python_stdlib", "requirements-mobile"))]
        if forbidden:
            raise ValueError(f"Python runtime files in APK: {forbidden[:5]}")
        for name in names:
            if name.endswith(".dex") and any(value in archive.read(name) for value in (b"com/chaquo/python/", b"Lorg/python/")):
                raise ValueError(f"Python bridge in {name}")
        manifest = json.loads(archive.read("assets/native/bundle.json"))
        if manifest["schemaVersion"] != 1 or manifest["runtime"] != "rust-go":
            raise ValueError("Unexpected native bundle manifest")
        if set(manifest["binaries"]) != {"arm64-v8a", "x86_64"}:
            raise ValueError("APK must retain both supported ABIs")
        declared_assets = set()
        for entry in manifest["assets"]:
            name = "assets/native/" + entry["path"]
            if name in declared_assets or hashlib.sha256(archive.read(name)).hexdigest() != entry["sha256"]:
                raise ValueError("Native resource integrity mismatch")
            declared_assets.add(name)
        packaged_assets = {name for name in names if name.startswith("assets/native/") and not name.endswith("/")}
        if packaged_assets != declared_assets | {"assets/native/bundle.json"}:
            raise ValueError("Undeclared or missing native resources")
        binaries: dict[str, Any] = {}
        with tempfile.TemporaryDirectory(prefix="deepseek-apk-inspect-") as temp:
            for abi in manifest["binaries"]:
                binaries[abi] = {}
                for kind, filename in (("rust", "libdeepseek_gateway.so"), ("go", "libdeepseek_control.so")):
                    content = archive.read(f"lib/{abi}/{filename}")
                    path = Path(temp) / filename
                    path.write_bytes(content)
                    inspected = inspect_executable(path, abi, kind)
                    if inspected["sha256"] != manifest["binaries"][abi][kind]["sha256"]:
                        raise ValueError(f"Packaged executable hash mismatch: {abi}/{kind}")
                    binaries[abi][kind] = inspected
        return {"status": "PASS", "checkedAt": datetime.now(timezone.utc).isoformat(), "releaseQualified": False,
                "apkSha256": hashlib.sha256(apk.read_bytes()).hexdigest(), "apkBytes": apk.stat().st_size,
                "pythonEntries": [], "pythonDexBridge": False, "assets": len(declared_assets), "binaries": binaries,
                "toolchains": manifest["toolchains"], "sourceIdentity": manifest["sourceIdentity"],
                "productionAuthorityQualified": manifest["productionAuthorityQualified"], "deviceExecutionVerified": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = inspect_package(args.apk.resolve())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "apkSha256", "apkBytes", "assets", "pythonEntries", "releaseQualified")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
