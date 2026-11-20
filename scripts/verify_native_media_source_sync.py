"""Cloud-only real Rust retained-file fsync fault regression, no product hook."""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
import verify_native_media_recovery as recovery

ROOT = Path(__file__).resolve().parents[1]
TEST = "media_create::tests::media_create_retained_sync_fault_probe"
DATA = b"retained-media-sync"


def require(value: Any, code: str) -> None:
    if not value:
        raise RuntimeError(code)


def qualify_case(result: int, output: str, trace: str, before: str, after: str) -> dict[str, Any]:
    one = bool(re.search(r"test result: ok\. 1 passed; 0 failed; 0 ignored", output))
    injected = "EIO" in trace and "INJECTED" in trace
    return dict(
        status="PASS" if result == 0 and one and injected and before == after else "FAIL",
        processExitCode=result,
        exactlyOneRustTestPassed=one,
        targetedEIOObserved=injected,
        originalFileSha256=before,
        afterFileSha256=after,
        sourceBytesUnchanged=before == after,
        retainedBytesAloneAccepted="retained bytes cannot replace" in output,
        physicalPowerLossSimulated=False,
    )


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    require(os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux", "SOURCE_SYNC_NATIVE_IS_CLOUD_ONLY")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    require(revision == os.environ["GITHUB_SHA"], "SOURCE_SYNC_SOURCE_DIFFERS")
    require(not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT), "SOURCE_SYNC_SOURCE_IS_DIRTY")
    require(
        args.strace.is_file() and not subprocess.check_output(["getcap", str(args.strace)], text=True).strip(),
        "SOURCE_SYNC_TRACER_MUST_BE_UNPRIVILEGED",
    )
    report["sourceCommit"] = revision
    command = [
        sys.executable,
        "-B",
        "scripts/rust_build.py",
        "--toolchain",
        "1.85.0",
        "--",
        "test",
        "--locked",
        "-p",
        "deepseek-worker",
        "--lib",
        "--no-run",
        "--message-format=json",
    ]
    built = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=600)
    (args.out / "build-test-binary.log").write_bytes(built.stdout + built.stderr)
    require(built.returncode == 0, "SOURCE_SYNC_TEST_BINARY_BUILD_FAILED")
    executables = []
    for line in built.stdout.decode().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (
            row.get("reason") == "compiler-artifact"
            and row.get("target", {}).get("name") == "deepseek_worker"
            and row.get("profile", {}).get("test")
            and row.get("executable")
        ):
            executables.append(Path(row["executable"]))
    require(len(executables) == 1, "SOURCE_SYNC_TEST_BINARY_AMBIGUOUS")
    binary = executables[0].resolve(strict=True)
    require(binary.is_relative_to(args.target_dir.resolve(strict=True)) and binary.is_file(), "SOURCE_SYNC_BINARY_ESCAPED_CLOUD_TARGET")
    report["binarySha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    report["cases"] = []
    for kind in ["file", "directory"]:
        folder = args.out / kind
        folder.mkdir()
        with tempfile.TemporaryDirectory(prefix="native-media-create-sync-") as raw:
            root = Path(raw)
            qualification = recovery.qualify_sync_fault_trace(args.strace, root, folder, kind)
            source = root / ".media/objects/media_sync/source.html"
            source.parent.mkdir(parents=True)
            source.write_bytes(DATA)
            (root / "owned-fixture-marker").write_bytes(b"native-media-source-sync-test-v1")
            target = source if kind == "file" else source.parent
            trace = folder / "retained-source-syscalls.log"
            env = dict(os.environ, DEEPSEEK_MEDIA_SYNC_TEST_ROOT=str(root))
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            child = subprocess.run(
                [
                    str(args.strace),
                    "-f",
                    "-P",
                    str(target),
                    "-e",
                    "trace=fsync,fdatasync",
                    "-e",
                    "inject=fsync,fdatasync:error=EIO",
                    "-o",
                    str(trace),
                    str(binary),
                    "--exact",
                    TEST,
                    "--nocapture",
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                timeout=30,
            )
            (folder / "rust-test-output.log").write_bytes(child.stdout + child.stderr)
            result = qualify_case(
                child.returncode,
                (child.stdout + child.stderr).decode(errors="replace"),
                trace.read_text(),
                before,
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
            result.update(kind=kind, qualifiedSyscallFilter=qualification, traceSha256=hashlib.sha256(trace.read_bytes()).hexdigest())
            report["cases"].append(result)
    report["semanticStageReached"] = True
    require(all(case["status"] == "PASS" for case in report["cases"]), "RETAINED_SOURCE_SYNC_BARRIER_MISSING")
    report["status"] = "PASS"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strace", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = dict(
        kind="native-media-retained-source-sync-v1",
        status="FAIL",
        sourceCommit=os.environ.get("GITHUB_SHA"),
        runId=os.environ.get("GITHUB_RUN_ID"),
        event=os.environ.get("GITHUB_EVENT_NAME"),
        semanticStageReached=False,
        scope="Actual Rust file custody helper under qualified targeted syscall EIO only",
        actualNativeHTTPRecoveryQualified=False,
        physicalPowerLossQualified=False,
        fullMigrationQualified=False,
        readiness="NOT_READY",
    )
    try:
        run(args, report)
    except Exception as error:
        report.update(failureCode=str(error)[:300], exceptionType=type(error).__name__)
    (args.out / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print(json.dumps(report), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
