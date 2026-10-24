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
import time
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
ANDROID_AMD64_GO_VERSION = "1.27.1"
ANDROID_AMD64_PIDFD_SOURCE_SHA256 = "f2a7c0e31bda943d4e2151a61f6a9eb2084815ed5c887cc8aa4fd7506805de33"
ANDROID_AMD64_PIDFD_OVERLAY_SHA256 = "088985fc4797f0207363a57264717c95620ce6450ae817781be8d4cb6274ff72"


def expected_android_go_compatibility() -> dict[str, Any]:
    """Exact effective standard-library variant admitted by cloud APK evidence."""
    return {
        "x86_64": dict(mode="android-linux-amd64-pidwait-v1", baseGoVersion=ANDROID_AMD64_GO_VERSION,
                       sdkFile="src/os/pidfd_linux.go", sdkSourceSha256=ANDROID_AMD64_PIDFD_SOURCE_SHA256,
                       overlaySourceSha256=ANDROID_AMD64_PIDFD_OVERLAY_SHA256, replacedFunctions=["pidfdWorks"],
                       pidfdProbeEnabled=False, sandboxModified=False, sourceSDKModified=False),
        "arm64-v8a": dict(mode="official-android-arm64", sourceSDKModified=False),
    }


def android_amd64_go_overlay(goroot: Path, output: Path) -> tuple[Path, dict[str, Any]]:
    """Select Go's PID/wait implementation for the pure-Go Android x86 build.

    Android <12 can trap the availability probe itself. This ABI is built with
    GOOS=linux because Go requires external/cgo linking for android/amd64.
    The private build overlay changes only pidfdWorks; SDK files, signal
    handlers, seccomp and the existing native guardian remain unchanged.
    """
    goroot, output = goroot.resolve(), output.resolve()
    if output.is_relative_to(goroot):
        raise ValueError("Android Go compatibility output must be outside the SDK")
    source = goroot / "src/os/pidfd_linux.go"
    original = source.read_bytes()
    if hashlib.sha256(original).hexdigest() != ANDROID_AMD64_PIDFD_SOURCE_SHA256:
        raise ValueError("Android Go compatibility requires the exact pinned SDK source")
    before = b"func pidfdWorks() bool {\n\treturn checkPidfdOnce() == nil\n}"
    after = b"func pidfdWorks() bool {\n\t// Android linux/amd64 uses the existing PID/wait implementation.\n\treturn false\n}"
    if original.count(before) != 1:
        raise ValueError("Android Go compatibility source shape changed")
    patched = original.replace(before, after, 1)
    output.mkdir(parents=True, exist_ok=True)
    backing, overlay = output / "pidfd_linux.go", output / "overlay.json"
    config = (json.dumps({"Replace": {source.as_posix(): backing.as_posix()}}, indent=2) + "\n").encode()
    for path, value in [(backing, patched), (overlay, config)]:
        try:
            with path.open("xb") as destination:
                destination.write(value)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != value:
                raise ValueError("Refuse a foreign Android Go compatibility output") from None
    metadata = dict(mode="android-linux-amd64-pidwait-v1", baseGoVersion=ANDROID_AMD64_GO_VERSION,
                    sdkFile="src/os/pidfd_linux.go", sdkSourceSha256=hashlib.sha256(original).hexdigest(),
                    overlaySourceSha256=hashlib.sha256(patched).hexdigest(), replacedFunctions=["pidfdWorks"],
                    pidfdProbeEnabled=False, sandboxModified=False, sourceSDKModified=False)
    return overlay, metadata


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


def rename_bundle_directory(source: Path, destination: Path) -> None:
    # Windows can temporarily refuse a newly written directory move. Retry only
    # the same unpublished move, with a 300 ms total wait and no permission changes.
    for attempt in range(4):
        try:
            source.rename(destination)
            return
        except PermissionError as error:
            if (getattr(error, "winerror", None) not in (5, 32) or attempt == 3
                    or not source.exists() or destination.exists()):
                raise
            time.sleep(0.05 * (attempt + 1))


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


STAGED_EXECUTABLES = {
    "rust": ("rust", "libdeepseek_gateway.so"),
    "go": ("go", "libdeepseek_control.so"),
    "worker": ("rust", "libdeepseek_worker.so"),
    "launcher": ("go", "libdeepseek_launch.so"),
}


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
        if abi not in TARGETS or set(executables) != set(STAGED_EXECUTABLES):
            raise ValueError("Android bundle requires the complete native process tree")
        manifest["binaries"][abi] = {
            role: inspect_executable(executables[role], abi, kind)
            for role, (kind, _) in STAGED_EXECUTABLES.items()
        }
    for abi, executables in binaries.items():
        for role, (_, filename) in STAGED_EXECUTABLES.items():
            copy_file(executables[role], output / "jniLibs" / abi / filename)
    # Publish a complete resource set. Copying over the previous generation leaves
    # old Vite chunks in the APK even though they are absent from the new manifest.
    # The temporary directory is under this explicit build output; only its own
    # staged/retired resources are cleaned, and sibling outputs are preserved.
    staging = Path(tempfile.mkdtemp(prefix=".native-stage-", dir=output)).resolve()
    if not staging.is_relative_to(output):
        raise ValueError("Native staging directory escapes build output")
    preserve_staging = False
    try:
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
            # Keep the prior generation through interruption or failed restore.
            preserve_staging = True
            rename_bundle_directory(destination, previous)
        try:
            rename_bundle_directory(assets, destination)
        except BaseException as publication_error:
            if previous.exists():
                try:
                    rename_bundle_directory(previous, destination)
                except OSError as restore_error:
                    raise RuntimeError(
                        f"Native asset publication failed ({publication_error}); "
                        f"prior assets remain for recovery at {previous}"
                    ) from restore_error
            preserve_staging = False
            raise
        preserve_staging = False
    finally:
        if preserve_staging:
            # A refused retirement leaves the prior generation at destination;
            # only staged copies remain to clean. Use lstat so an access error
            # cannot be mistaken for absence and delete a recoverable generation.
            try:
                (staging / "previous").lstat()
            except FileNotFoundError:
                preserve_staging = False
        if not preserve_staging:
            shutil.rmtree(staging)
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
    if expected_go != ANDROID_AMD64_GO_VERSION:
        raise ValueError("Review the Android Go compatibility overlay before changing Go")
    goroot = Path(run([go, "env", "GOROOT"], root / "go", env))
    overlay, go_compatibility = android_amd64_go_overlay(goroot, output / "go-android-amd64-compat")
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
        run([cargo, f"+{toolchain}", "build", "--release", "--locked", "--target", target, "-p", "deepseek-gateway", "-p", "deepseek-worker"], root / "rust", rust_env)
        go_env = dict(env, CGO_ENABLED="0", GOTOOLCHAIN="local", GOARCH="arm64" if abi == "arm64-v8a" else "amd64",
                      GOOS="android" if abi == "arm64-v8a" else "linux")
        go_binaries = {}
        for role, command in (("go", "deepseekd"), ("launcher", "deepseek-launch")):
            binary = output / "bin" / abi / command
            binary.parent.mkdir(parents=True, exist_ok=True)
            compatibility = ["-overlay", overlay.as_posix()] if abi == "x86_64" else []
            run([go, "build", *compatibility, "-tags", "deepseek_android,sqlite3_flock", "-trimpath", "-o", binary.as_posix(), "./cmd/" + command], root / "go", go_env)
            go_binaries[role] = binary
        binaries[abi] = {"rust": cargo_target / target / "release/deepseek-gateway",
                         "worker": cargo_target / target / "release/deepseek-worker", **go_binaries}
    if source_identity(root) != initial_identity:
        raise ValueError("Android source changed during native compilation")
    if hashlib.sha256((goroot / "src/os/pidfd_linux.go").read_bytes()).hexdigest() != go_compatibility["sdkSourceSha256"]:
        raise ValueError("Android Go compilation changed the SDK source")
    manifest = stage_bundle(root, output, binaries)
    manifest["toolchains"] = {"rust": RUST_VERSION, "go": expected_go, "ndk": NDK_VERSION, "minimumAndroidApi": 24}
    manifest["sourceIdentity"] = initial_identity
    manifest["goCompatibility"] = {"x86_64": go_compatibility, "arm64-v8a": {"mode": "official-android-arm64", "sourceSDKModified": False}}
    (output / "assets/native/bundle.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def build_lock_probes(root: Path, output: Path) -> dict[str, Any]:
    """Build test-APK-only syscall probes; these never enter a product bundle."""
    expected = json.loads((root / "release/native_runtime_toolchain_v1.json").read_text())["go"]["version"]
    env = dict(os.environ, CGO_ENABLED="0", GOTOOLCHAIN="local")
    go = shutil.which("go") or "go"
    if run([go, "version"], root / "go", env).split()[2] != f"go{expected}":
        raise ValueError(f"Go must be pinned to {expected}")
    source = root / "android/app/src/androidTest/fixtures/lockprobe/main.go"
    probes = {}
    for abi in TARGETS:
        binary = output / abi / "libdeepseek_lock_probe.so"
        binary.parent.mkdir(parents=True, exist_ok=True)
        target_env = dict(env, GOARCH="arm64" if abi == "arm64-v8a" else "amd64",
                          GOOS="android" if abi == "arm64-v8a" else "linux")
        run([go, "build", "-trimpath", "-o", binary.as_posix(), source.as_posix()], root / "go", target_env)
        probes[abi] = inspect_executable(binary, abi, "go")
    return probes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--sdk", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cargo-target-dir", type=Path)
    parser.add_argument("--lock-probes-only", action="store_true")
    parser.add_argument("--rust-toolchain", default=RUST_VERSION + ("-x86_64-pc-windows-gnu" if os.name == "nt" else ""))
    args = parser.parse_args()
    if args.lock_probes_only:
        print(json.dumps({"instrumentationOnly": True, "probes": build_lock_probes(args.root.resolve(), args.output.resolve())}))
        return 0
    if args.sdk is None:
        parser.error("--sdk is required to build the native runtime")
    environment = cache_environment(args.root.resolve(), toolchain=args.rust_toolchain,
                                    target_dir=args.cargo_target_dir, kind="android")
    cargo_target = Path(environment["CARGO_TARGET_DIR"])
    manifest = build(args.root.resolve(), args.sdk.resolve(), args.output.resolve(), cargo_target.resolve(), args.rust_toolchain)
    print(json.dumps({"runtime": manifest["runtime"], "abis": list(manifest["binaries"]), "assets": len(manifest["assets"]),
                      "productionAuthorityQualified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
