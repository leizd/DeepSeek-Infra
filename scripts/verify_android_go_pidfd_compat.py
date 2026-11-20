"""Cloud-only actual SIGSYS regression for the Android Go x86 compatibility build."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.build_android_native import ANDROID_AMD64_GO_VERSION, android_amd64_go_overlay  # noqa: E402


def command(args: list[str], env: dict[str, str], timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT / "go", env=env, capture_output=True, text=True, timeout=timeout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, default=ROOT / "artifacts/control-rpc-development/android-pidfd")
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.name != "posix" or platform.machine() not in {"x86_64", "amd64"}:
        raise RuntimeError("ANDROID_PIDFD_PROOF_IS_CLOUD_ONLY")
    env = dict(os.environ, CGO_ENABLED="0", GOOS="linux", GOARCH="amd64", GOTOOLCHAIN="local", GOENV="off", GOWORK="off", GOFLAGS="", GOTRACEBACK="single")
    source = command(["git", "rev-parse", "HEAD"], env).stdout.strip()
    if source != os.environ.get("GITHUB_SHA") or command(["git", "status", "--porcelain"], env).stdout.strip():
        raise RuntimeError("ANDROID_PIDFD_PROOF_SOURCE_MISMATCH")
    version = command(["go", "version"], env)
    if version.returncode or version.stdout.split()[2] != "go" + ANDROID_AMD64_GO_VERSION:
        raise RuntimeError("ANDROID_PIDFD_PROOF_TOOLCHAIN_MISMATCH")
    goroot_result = command(["go", "env", "GOROOT"], env)
    if goroot_result.returncode:
        raise RuntimeError("ANDROID_PIDFD_PROOF_SDK_UNAVAILABLE")
    goroot = Path(goroot_result.stdout.strip())
    output = args.report_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = dict(sourceCommit=source, sourceTree=command(["git", "rev-parse", "HEAD^{tree}"], env).stdout.strip(),
                                   runId=os.environ.get("GITHUB_RUN_ID"), event=os.environ.get("GITHUB_EVENT_NAME"),
                                   checkedAt=datetime.now(timezone.utc).isoformat(), cases={}, nativeBuildsOnCloudOnly=True,
                                   packagedAndroidQualified=False, fullGoSuiteQualified=False, fullMigrationQualified=False, readiness="NOT_READY")
    try:
        with tempfile.TemporaryDirectory(prefix="android-pidfd-proof-", dir=os.environ["RUNNER_TEMP"]) as directory:
            scratch = Path(directory)
            overlay, metadata = android_amd64_go_overlay(goroot, scratch / "overlay")
            report["compatibility"] = metadata
            binaries: dict[str, Path] = {}
            fixture = ROOT / "tests/native/fixtures/android_pidfd_probe.go"
            for variant in ["original", "compat"]:
                binary = scratch / variant
                flags = ["-overlay", overlay.as_posix()] if variant == "compat" else []
                built = command(["go", "build", "-trimpath", "-mod=readonly", *flags, "-o", binary.as_posix(), fixture.as_posix()], env)
                (output / (variant + "-build.log")).write_text(built.stdout + built.stderr, encoding="utf-8")
                if built.returncode:
                    raise RuntimeError("ANDROID_PIDFD_PROOF_BUILD_FAILED: " + variant)
                info = command(["go", "version", "-m", binary.as_posix()], env)
                if info.returncode or not all(flag in info.stdout for flag in ["CGO_ENABLED=0", "GOOS=linux", "GOARCH=amd64"]):
                    raise RuntimeError("ANDROID_PIDFD_PROOF_BUILD_FLAGS_NOT_RETAINED")
                (output / (variant + "-build-info.log")).write_text(info.stdout, encoding="utf-8")
                binaries[variant] = binary
            for mode in ["open", "send", "both"]:
                baseline = command([binaries["original"].as_posix(), mode], env, timeout=40)
                (output / (mode + "-original.log")).write_text(baseline.stdout + baseline.stderr, encoding="utf-8")
                if baseline.returncode == 0 or "SIGSYS" not in baseline.stderr or "os.checkPidfd" not in baseline.stderr:
                    raise RuntimeError("ANDROID_PIDFD_ORIGINAL_TRAP_NOT_REPRODUCED: " + mode)
                positive = command([binaries["compat"].as_posix(), mode], env, timeout=40)
                (output / (mode + "-compat.log")).write_text(positive.stdout + positive.stderr, encoding="utf-8")
                if positive.returncode:
                    raise RuntimeError("ANDROID_PIDFD_COMPAT_WORKLOAD_FAILED: " + mode)
                result = json.loads(positive.stdout)
                if result != dict(status="PASS", trap=mode, actualConcurrentChildren=8, readyChildCancelledAndReaped=True, broadSignalSuppression=False):
                    raise RuntimeError("ANDROID_PIDFD_COMPAT_WORKLOAD_INCOMPLETE: " + mode)
                forbidden = command([binaries["compat"].as_posix(), mode, "forbidden-direct-call"], env, timeout=40)
                (output / (mode + "-forbidden.log")).write_text(forbidden.stdout + forbidden.stderr, encoding="utf-8")
                if forbidden.returncode == 0 or "SIGSYS" not in forbidden.stderr or "os.checkPidfd" in forbidden.stderr:
                    raise RuntimeError("ANDROID_PIDFD_SANDBOX_OR_SIGNAL_REFUSAL_LOST: " + mode)
                report["cases"][mode] = dict(status="PASS", originalProbeActuallyFailed=True, originalExitCode=baseline.returncode,
                                              actualConcurrentChildren=8, actualReadyChildCancelledAndReaped=True,
                                              forbiddenSyscallStillFatal=True, forbiddenExitCode=forbidden.returncode)
            sdk_hash = hashlib.sha256((goroot / "src/os/pidfd_linux.go").read_bytes()).hexdigest()
            if sdk_hash != metadata["sdkSourceSha256"] or command(["git", "status", "--porcelain"], env).stdout.strip():
                raise RuntimeError("ANDROID_PIDFD_PROOF_CHANGED_SOURCE_OR_SDK")
            report.update(status="PASS", originalSDKUnchanged=True, sourceUnchanged=True)
    except Exception as error:
        report.update(status="FAIL", error=str(error)[:1500])
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if report["status"] != "PASS":
        raise SystemExit("ANDROID_PIDFD_PROOF_FAILED")


if __name__ == "__main__":
    main()
