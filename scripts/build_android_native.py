"""Build and stage the Android Rust/Go payload. Python is an offline build tool only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from scripts.native_android_codegen import check_existing  # noqa: E402
from scripts.rust_build import cache_environment, run_guarded  # noqa: E402

NDK_VERSION = "27.3.13750724"
RUST_VERSION = "1.85.0"
TARGETS = {"arm64-v8a": ("aarch64-linux-android", 183), "x86_64": ("x86_64-linux-android", 62)}
PYTHON_SUFFIXES = {".py", ".pyc", ".pyo", ".pyd", ".whl"}


def inspect_executable(path: Path, abi: str, kind: str) -> dict[str, Any]:
    data = path.read_bytes()
    if len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01":
        raise ValueError(f"Not a little-endian ELF64 executable: {path.name}")
    elf_type, machine = struct.unpack_from("<HH", data, 16)
    if machine != TARGETS[abi][1] or elf_type not in {2, 3}:
        raise ValueError(f"Wrong executable architecture/type for {abi}: {path.name}")
    phoff = struct.unpack_from("<Q", data, 32)[0]
    phsize, phcount = struct.unpack_from("<HH", data, 54)
    if phsize != 56 or phcount == 0 or phoff + phsize * phcount > len(data):
        raise ValueError(f"Invalid ELF program headers: {path.name}")
    interpreter, alignments = "", []
    for index in range(phcount):
        seg_type, _, offset, _, _, filesz, _, alignment = struct.unpack_from("<IIQQQQQQ", data, phoff + index * phsize)
        if offset + filesz > len(data):
            raise ValueError(f"Truncated ELF segment: {path.name}")
        if seg_type == 3:
            interpreter = data[offset : offset + filesz].rstrip(b"\0").decode("ascii")
        elif seg_type == 1:
            alignments.append(alignment)
    # Go 1.27.1 Android/amd64 requires cgo. The explicitly labelled pure-Go
    # static Linux compatibility build is tested in the x86_64 app sandbox.
    compatibility = kind == "go" and abi == "x86_64" and elf_type == 2 and not interpreter
    if not compatibility and (elf_type != 3 or interpreter != "/system/bin/linker64"):
        raise ValueError(f"Executable needs an Android PIE loader: {path.name}")
    minimum_alignment = 4096 if compatibility else 16384
    if not alignments or any(value < minimum_alignment or value & (value - 1) for value in alignments):
        raise ValueError(f"Insufficient ELF page alignment for {abi}: {path.name}")
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "elfType": elf_type,
            "elfMachine": machine, "interpreter": interpreter, "loadAlignments": alignments,
            "target": "linux/amd64 static compatibility" if compatibility else "android/arm64" if kind == "go" else TARGETS[abi][0]}


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".native-tmp")
    shutil.copyfile(source, temporary)
    temporary.replace(destination)


def source_identity(root: Path) -> dict[str, Any]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", "-C", str(root), *args])
    if Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != root.resolve():
        raise ValueError("Android source must be the Git working tree root")
    paths = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "rust", "go", "android", "proto",
                "frontend", "skills", "release", "compat", "evals/golden/skills", "VERSION", "scripts/build_android_native.py",
                "scripts/native_android_codegen.py", "scripts/rust_build.py")
    digest = hashlib.sha256()
    source_paths = sorted(set(item for item in paths.split(b"\0") if item))
    for raw in source_paths:
        path = root / raw.decode("utf-8")
        if not path.is_file():
            raise ValueError("Source identity includes a missing file")
        digest.update(raw + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return {"commit": git("rev-parse", "HEAD").decode().strip(), "dirty": bool(git("status", "--porcelain")),
            "sourceSha256": digest.hexdigest(), "sourceFiles": len(source_paths), "releaseQualified": False}


def stage_bundle(root: Path, output: Path, binaries: dict[str, dict[str, Path]]) -> dict[str, Any]:
    if not (root / "static/ui/index.html").is_file():
        raise ValueError("React frontend build is missing")
    golden = root / "evals/golden/skills/skill_eval_cases.jsonl"
    if not golden.is_file() or not golden.resolve().is_relative_to(root.resolve()):
        raise ValueError("Required Skill evaluation corpus missing or outside source root")
    output = output.resolve()
    destination = output / "assets/native"
    if destination.resolve() != destination:
        raise ValueError("Native asset output must not follow a directory link")
    manifest: dict[str, Any] = {"schemaVersion": 1, "version": (root / "VERSION").read_text().strip(),
                              "runtime": "rust-go", "productionAuthorityQualified": False,
                              "binaries": {}, "assets": []}
    # Validate the complete binary set before writing any staged output.
    for abi, executables in binaries.items():
        manifest["binaries"][abi] = {kind: inspect_executable(executables[kind], abi, kind) for kind in ("rust", "go")}
    for abi, executables in binaries.items():
        for kind, filename in (("rust", "libdeepseek_gateway.so"), ("go", "libdeepseek_control.so")):
            copy_file(executables[kind], output / "jniLibs" / abi / filename)
    # Publish a complete resource set. Copying over the previous generation leaves
    # old Vite chunks in the APK even though they are absent from the new manifest.
    # The temporary directory is under this explicit build output; only its own
    # staged/retired resources are cleaned, and sibling outputs are preserved.
    with tempfile.TemporaryDirectory(prefix=".native-stage-", dir=output) as temporary:
        staging = Path(temporary).resolve()
        if not staging.is_relative_to(output):
            raise ValueError("Native staging directory escapes build output")
        assets = staging / "native"
        for dirname in ("static", "skills"):
            directory = root / dirname
            if not directory.is_dir():
                raise ValueError(f"Required product assets missing: {dirname}")
            for source in sorted(directory.rglob("*")):
                if not source.is_file() or "__pycache__" in source.parts or source.suffix.lower() in PYTHON_SUFFIXES:
                    continue
                if not source.resolve().is_relative_to(directory.resolve()):
                    raise ValueError(f"Asset escapes product directory: {source.name}")
                relative = source.relative_to(root).as_posix()
                copy_file(source, assets / relative)
                manifest["assets"].append({"path": relative, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
        relative = golden.relative_to(root).as_posix()
        copy_file(golden, assets / relative)
        manifest["assets"].append({"path": relative, "sha256": hashlib.sha256(golden.read_bytes()).hexdigest()})
        (assets / "bundle.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        destination.parent.mkdir(parents=True, exist_ok=True)
        previous = staging / "previous"
        if destination.exists():
            destination.rename(previous)
        try:
            assets.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    return manifest


def run(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    if Path(args[0]).stem.lower() == "cargo":
        result = run_guarded(args, cwd=cwd, env=env, check=True, capture_output=True, text=True)
    else:
        result = subprocess.run(args, cwd=cwd, env=env, check=True, stdout=subprocess.PIPE, text=True)
    if result.stdout:
        print(result.stdout.rstrip(), flush=True)
    return result.stdout.strip()


def build(root: Path, sdk: Path, output: Path, cargo_target: Path, toolchain: str) -> dict[str, Any]:
    check_existing(root)
    initial_identity = source_identity(root)
    env = cache_environment(root, toolchain=toolchain, target_dir=cargo_target, kind="android")
    cargo = shutil.which("cargo") or "cargo"
    go = shutil.which("go") or "go"
    rustc = shutil.which("rustc") or "rustc"
    version = run([rustc, f"+{toolchain}", "--version"], root / "rust", env)
    if version.split()[1] != RUST_VERSION:
        raise ValueError(f"Rust must be pinned to {RUST_VERSION}")
    expected_go = json.loads((root / "release/native_runtime_toolchain_v1.json").read_text())['go']['version']
    if run([go, "version"], root / "go", env).split()[2] != f"go{expected_go}":
        raise ValueError(f"Go must be pinned to {expected_go}")
    ndk = sdk / "ndk" / NDK_VERSION
    host = "windows-x86_64" if os.name == "nt" else "darwin-x86_64" if platform.system() == "Darwin" else "linux-x86_64"
    compiler_dir = ndk / "toolchains/llvm/prebuilt" / host / "bin"
    if not compiler_dir.is_dir() or f"Pkg.Revision = {NDK_VERSION}" not in (ndk / "source.properties").read_text():
        raise ValueError(f"Install the pinned official Android NDK {NDK_VERSION}")
    binaries: dict[str, dict[str, Path]] = {}
    for abi, (target, _) in TARGETS.items():
        rust_env = dict(env)
        compiler = compiler_dir / (target + "24-clang" + (".cmd" if os.name == "nt" else ""))
        ar = compiler_dir / ("llvm-ar.exe" if os.name == "nt" else "llvm-ar")
        key = target.replace("-", "_")
        rust_env.update({f"CARGO_TARGET_{key.upper()}_LINKER": compiler.as_posix(), f"CC_{key}": compiler.as_posix(),
                         f"AR_{key}": ar.as_posix(), f"CARGO_TARGET_{key.upper()}_RUSTFLAGS": "-C link-arg=-Wl,-z,max-page-size=16384",
                         "CARGO_TARGET_DIR": cargo_target.as_posix()})
        run([cargo, f"+{toolchain}", "build", "--release", "--locked", "--target", target, "-p", "deepseek-gateway"], root / "rust", rust_env)
        go_env = dict(env, CGO_ENABLED="0", GOTOOLCHAIN="local", GOARCH="arm64" if abi == "arm64-v8a" else "amd64",
                      GOOS="android" if abi == "arm64-v8a" else "linux")
        control = output / "bin" / abi / "deepseekd"
        control.parent.mkdir(parents=True, exist_ok=True)
        run([go, "build", "-tags", "deepseek_android", "-trimpath", "-o", control.as_posix(), "./cmd/deepseekd"], root / "go", go_env)
        binaries[abi] = {"rust": cargo_target / target / "release/deepseek-gateway", "go": control}
    if source_identity(root) != initial_identity:
        raise ValueError("Android source changed during native compilation")
    manifest = stage_bundle(root, output, binaries)
    manifest["toolchains"] = {"rust": RUST_VERSION, "go": expected_go, "ndk": NDK_VERSION, "minimumAndroidApi": 24}
    manifest["sourceIdentity"] = initial_identity
    (output / "assets/native/bundle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cargo-target-dir", type=Path)
    parser.add_argument("--rust-toolchain", default=RUST_VERSION + ("-x86_64-pc-windows-gnu" if os.name == "nt" else ""))
    args = parser.parse_args()
    environment = cache_environment(args.root.resolve(), toolchain=args.rust_toolchain,
                                    target_dir=args.cargo_target_dir, kind="android")
    cargo_target = Path(environment["CARGO_TARGET_DIR"])
    manifest = build(args.root.resolve(), args.sdk.resolve(), args.output.resolve(), cargo_target.resolve(), args.rust_toolchain)
    print(json.dumps({"runtime": manifest["runtime"], "abis": list(manifest["binaries"]), "assets": len(manifest["assets"]),
                      "productionAuthorityQualified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
