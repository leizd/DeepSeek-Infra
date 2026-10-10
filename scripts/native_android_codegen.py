"""Pinned Android Java Lite bindings for the typed read-only platform boundary."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("proto/platform/v1/ocr.proto")
LOCK = Path("release/native_android_platform_toolchain_v1.json")
MANIFEST = Path("proto/generated/android-codegen-manifest.v1.json")
JAVA_DIR = Path("android/app/src/main/java/com/deepseek/mobile/platform/v1")
OUTPUTS = (JAVA_DIR / "PlatformOcr.java", JAVA_DIR / "PlatformOcrEngineGrpc.java")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load_lock(root: Path) -> dict[str, Any]:
    lock: dict[str, Any] = json.loads((root / LOCK).read_text(encoding="utf-8"))
    if (lock.get("schemaVersion"), lock.get("protocVersion"), lock.get("grpcJavaVersion"),
            lock.get("protobufJavaLiteVersion")) != (1, "36.1", "1.84.0", "4.36.1"):
        raise ValueError("Android Java/protoc toolchain drift")
    if lock.get("protobufJavaLiteSha256") != "91d3dba2521322103230c509d41a017da3a5e6766610adcb10c5bb127cbb26c5" or lock.get("generatorArtifacts") != {
        "windows_amd64": {"filename": "protoc-gen-grpc-java-1.84.0-windows-x86_64.exe",
                          "sha256": "1d0b584f436c2521542a98914d8e6c42ba0aa5d86b459bc632e4c3884a3b5d4d"},
        "linux_amd64": {"filename": "protoc-gen-grpc-java-1.84.0-linux-x86_64.exe",
                        "sha256": "907c2d4efc2bae9b21cada75c31b36f3821e15b7be96311e214a6be65bcd20aa"},
    }:
        raise ValueError("Android platform artifact identities drifted")
    return lock


def manifest_for(root: Path, generated_root: Path) -> dict[str, Any]:
    return {"schemaVersion": 1, "source": {"path": SOURCE.as_posix(), "sha256": digest(root / SOURCE)},
            "toolchainSha256": digest(root / LOCK),
            "outputs": [{"path": path.as_posix(), "sha256": digest(generated_root / path)} for path in OUTPUTS]}


def check_existing(root: Path = ROOT) -> None:
    load_lock(root)
    expected = manifest_for(root, root)
    actual = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    if actual != expected:
        raise ValueError("Android Java Lite bindings drifted; run native_android_codegen.py --write")
    if {path.name for path in (root / JAVA_DIR).glob("*.java")} != {path.name for path in OUTPUTS}:
        raise ValueError("Android generated Java file set drifted")


def generate(*, root: Path, protoc: Path, plugin: Path, write: bool) -> None:
    lock = load_lock(root)
    version = subprocess.check_output([str(protoc), "--version"], text=True).strip()
    if version != "libprotoc " + lock["protocVersion"]:
        raise ValueError("Android protoc version mismatch")
    platform_key = "windows_amd64" if os.name == "nt" else "linux_amd64"
    expected = lock["generatorArtifacts"][platform_key]["sha256"]
    if hashlib.sha256(plugin.read_bytes()).hexdigest() != expected:
        raise ValueError("Android gRPC Java generator checksum mismatch")
    with tempfile.TemporaryDirectory(prefix="deepseek-android-codegen-") as temporary:
        stage = Path(temporary)
        java = stage / "java"
        java.mkdir()
        subprocess.run([str(protoc), "--proto_path=" + str(root / "proto"),
                        "--plugin=protoc-gen-grpc-java=" + str(plugin), "--java_out=lite:" + str(java),
                        "--grpc-java_out=lite:" + str(java), str(root / SOURCE)], check=True)
        for path in OUTPUTS:
            source = java / path.relative_to(Path("android/app/src/main/java"))
            output = stage / path
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, output)
        manifest = manifest_for(root, stage)
        if write:
            for path in OUTPUTS:
                output = root / path
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(stage / path, output)
            (root / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        check_existing(root)
        if json.loads((root / MANIFEST).read_text(encoding="utf-8")) != manifest:
            raise ValueError("Pinned Android Java regeneration changed bindings")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--protoc", type=Path, default=os.environ.get("PROTOC") or shutil.which("protoc"))
    parser.add_argument("--grpc-plugin", type=Path, default=os.environ.get("PROTOC_GEN_GRPC_JAVA"))
    args = parser.parse_args()
    if not args.protoc or not args.grpc_plugin:
        parser.error("protoc and the checksum-pinned gRPC Java plugin are required")
    generate(root=ROOT, protoc=args.protoc.resolve(), plugin=args.grpc_plugin.resolve(), write=args.write)
    print(json.dumps({"status": "PASS", "source": SOURCE.as_posix(), "javaFiles": len(OUTPUTS)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
