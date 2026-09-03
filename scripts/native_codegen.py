#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.native_runtime_contract import ContractError, check_descriptor, validate_toolchain  # noqa: E402

TOOLCHAIN_PATH = ROOT / "release" / "native_runtime_toolchain_v1.json"
PROTO_ROOT = ROOT / "proto"
GO_ROOT = ROOT / "go"
BINARY_DESCRIPTOR_PATH = PROTO_ROOT / "generated" / "descriptor.pb"
CODEGEN_MANIFEST_PATH = PROTO_ROOT / "generated" / "codegen-manifest.v1.json"
GO_MODULE = "github.com/leizd/DeepSeek-Infra/go"


class CodegenError(RuntimeError):
    pass


def _resolve_tool(explicit: Path | None, environment_name: str, executable: str) -> Path:
    raw = str(explicit) if explicit is not None else os.environ.get(environment_name)
    if raw:
        candidate = Path(raw).resolve()
    else:
        located = shutil.which(executable)
        if located is None and os.name == "nt":
            located = shutil.which(f"{executable}.exe")
        if located is None:
            raise CodegenError(f"TOOL_NOT_FOUND: {executable}; set {environment_name}")
        candidate = Path(located).resolve()
    if not candidate.is_file():
        raise CodegenError(f"TOOL_NOT_FOUND: {candidate}")
    return candidate


def _run(command: list[str], *, cwd: Path = ROOT) -> str:
    result = subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)
    output = "\n".join(item.strip() for item in (result.stdout, result.stderr) if item.strip())
    if result.returncode != 0:
        rendered = subprocess.list2cmdline(command)
        raise CodegenError(f"CODEGEN_COMMAND_FAILED: {rendered}: {output}")
    return output


def _require_version(tool: Path, arguments: list[str], expected: str | tuple[str, ...], label: str) -> None:
    output = _run([str(tool), *arguments])
    allowed = (expected,) if isinstance(expected, str) else expected
    if output.strip() not in allowed:
        raise CodegenError(f"TOOL_VERSION_MISMATCH: {label} expected one of {allowed!r}, got {output!r}")


def _sha256_file(path: Path) -> str:
    payload = path.read_bytes()
    if path.suffix in {".go", ".json", ".proto"}:
        payload = payload.replace(b"\r\n", b"\n")
    return hashlib.sha256(payload).hexdigest()


def _proto_files() -> list[Path]:
    files = sorted(path for path in PROTO_ROOT.rglob("*.proto") if "generated" not in path.parts)
    if not files:
        raise CodegenError("PROTO_SOURCES_MISSING")
    return files


def _manifest(stage: Path, toolchain: dict[str, Any], proto_files: list[Path]) -> dict[str, Any]:
    outputs: list[dict[str, str]] = []
    for generated in sorted((stage / "go").rglob("*.pb.go")):
        relative = Path("go") / generated.relative_to(stage / "go")
        outputs.append({"path": relative.as_posix(), "sha256": _sha256_file(generated)})
    descriptor = stage / "proto" / "generated" / "descriptor.pb"
    outputs.append({"path": "proto/generated/descriptor.pb", "sha256": _sha256_file(descriptor)})
    generators = toolchain["generators"]
    return {
        "schema_version": 1,
        "go_module": GO_MODULE,
        "toolchain": {
            "protoc": toolchain["protoc"]["version"],
            "protoc_gen_go": generators["protoc_gen_go"]["version"],
            "protoc_gen_go_grpc": generators["protoc_gen_go_grpc"]["version"],
            "prost": generators["prost"]["version"],
            "tonic_build": generators["tonic_build"]["version"],
            "protox": generators["protox"]["version"],
        },
        "sources": [
            {
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": _sha256_file(path),
            }
            for path in proto_files
        ],
        "outputs": outputs,
    }


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _generate(
    stage: Path,
    *,
    protoc: Path,
    protoc_gen_go: Path,
    protoc_gen_go_grpc: Path,
    toolchain: dict[str, Any],
) -> tuple[dict[str, Any], list[Path]]:
    proto_files = _proto_files()
    go_output = stage / "go"
    descriptor = stage / "proto" / "generated" / "descriptor.pb"
    go_output.mkdir(parents=True, exist_ok=True)
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    relative_protos = [path.relative_to(PROTO_ROOT).as_posix() for path in proto_files]
    _run(
        [
            str(protoc),
            "--proto_path=.",
            f"--plugin=protoc-gen-go={protoc_gen_go}",
            f"--plugin=protoc-gen-go-grpc={protoc_gen_go_grpc}",
            f"--go_out={go_output}",
            f"--go_opt=module={GO_MODULE}",
            f"--go-grpc_out={go_output}",
            f"--go-grpc_opt=module={GO_MODULE}",
            f"--descriptor_set_out={descriptor}",
            "--include_imports",
            *relative_protos,
        ],
        cwd=PROTO_ROOT,
    )
    return _manifest(stage, toolchain, proto_files), proto_files


def _repo_path(relative: str) -> Path:
    target = (ROOT / relative).resolve()
    try:
        target.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise CodegenError(f"OUTPUT_PATH_ESCAPE: {relative}") from exc
    return target


def _check(stage: Path, manifest: dict[str, Any]) -> None:
    expected_go = {str(item["path"]): stage / str(item["path"]) for item in manifest["outputs"] if str(item["path"]).endswith(".pb.go")}
    actual_go = {path.relative_to(ROOT).as_posix() for path in (GO_ROOT / "internal" / "protocol").rglob("*.pb.go")}
    if actual_go != set(expected_go):
        missing = sorted(set(expected_go) - actual_go)
        stale = sorted(actual_go - set(expected_go))
        raise CodegenError(f"GENERATED_GO_FILE_SET_DRIFT: missing={missing}, stale={stale}")
    comparisons = {
        **expected_go,
        "proto/generated/descriptor.pb": stage / "proto" / "generated" / "descriptor.pb",
    }
    for relative, expected in comparisons.items():
        actual = _repo_path(relative)
        if not actual.is_file():
            raise CodegenError(f"GENERATED_OUTPUT_MISSING: {relative}")
        if _sha256_file(actual) != _sha256_file(expected):
            raise CodegenError(f"GENERATED_OUTPUT_DRIFT: {relative}")
    if not CODEGEN_MANIFEST_PATH.is_file() or CODEGEN_MANIFEST_PATH.read_bytes().replace(b"\r\n", b"\n") != _manifest_bytes(manifest):
        raise CodegenError("CODEGEN_MANIFEST_DRIFT")


def _write(stage: Path, manifest: dict[str, Any]) -> None:
    generated_go = {str(item["path"]): stage / str(item["path"]) for item in manifest["outputs"] if str(item["path"]).endswith(".pb.go")}
    for existing in (GO_ROOT / "internal" / "protocol").rglob("*.pb.go"):
        if existing.relative_to(ROOT).as_posix() not in generated_go:
            existing.unlink()
    for relative, source in generated_go.items():
        destination = _repo_path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    BINARY_DESCRIPTOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(stage / "proto" / "generated" / "descriptor.pb", BINARY_DESCRIPTOR_PATH)
    CODEGEN_MANIFEST_PATH.write_bytes(_manifest_bytes(manifest))


def run_codegen(
    *,
    check: bool,
    write: bool,
    protoc: Path | None = None,
    protoc_gen_go: Path | None = None,
    protoc_gen_go_grpc: Path | None = None,
) -> None:
    toolchain = validate_toolchain(TOOLCHAIN_PATH)
    protoc_path = _resolve_tool(protoc, "PROTOC", "protoc")
    go_path = _resolve_tool(protoc_gen_go, "PROTOC_GEN_GO", "protoc-gen-go")
    grpc_path = _resolve_tool(protoc_gen_go_grpc, "PROTOC_GEN_GO_GRPC", "protoc-gen-go-grpc")
    _require_version(protoc_path, ["--version"], f"libprotoc {toolchain['protoc']['version']}", "protoc")
    generators = toolchain["generators"]
    _require_version(
        go_path,
        ["--version"],
        (
            f"protoc-gen-go v{generators['protoc_gen_go']['version']}",
            f"protoc-gen-go.exe v{generators['protoc_gen_go']['version']}",
        ),
        "protoc-gen-go",
    )
    _require_version(
        grpc_path,
        ["--version"],
        (
            f"protoc-gen-go-grpc {generators['protoc_gen_go_grpc']['version']}",
            f"protoc-gen-go-grpc.exe {generators['protoc_gen_go_grpc']['version']}",
        ),
        "protoc-gen-go-grpc",
    )
    with tempfile.TemporaryDirectory(prefix="deepseek-native-codegen-") as temporary:
        stage = Path(temporary)
        manifest, _ = _generate(
            stage,
            protoc=protoc_path,
            protoc_gen_go=go_path,
            protoc_gen_go_grpc=grpc_path,
            toolchain=toolchain,
        )
        if check or write:
            check_descriptor(binary_path=stage / "proto" / "generated" / "descriptor.pb")
        if write:
            _write(stage, manifest)
        if check or write:
            _check(stage, manifest)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or check native Go/Rust Protobuf contracts")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--protoc", type=Path)
    parser.add_argument("--protoc-gen-go", type=Path)
    parser.add_argument("--protoc-gen-go-grpc", type=Path)
    args = parser.parse_args(argv)
    try:
        run_codegen(
            check=args.check or not args.write,
            write=args.write,
            protoc=args.protoc,
            protoc_gen_go=args.protoc_gen_go,
            protoc_gen_go_grpc=args.protoc_gen_go_grpc,
        )
    except (CodegenError, ContractError) as exc:
        print(f"native codegen FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
