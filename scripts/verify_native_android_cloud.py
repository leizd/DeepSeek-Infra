"""Run existing packaged Android tests on a disposable cloud emulator; reports only."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import struct
import zipfile
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "scripts"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from scripts.build_android_native import expected_android_go_compatibility, source_identity  # noqa: E402
from scripts.check_native_android_package import inspect_package  # noqa: E402

APPLICATION = "com.deepseek.mobile.nativeverify"
RUNNER = APPLICATION + ".test/androidx.test.runner.AndroidJUnitRunner"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cloud_context(commit: str) -> dict[str, str]:
    keys = {"runId": "GITHUB_RUN_ID", "attempt": "GITHUB_RUN_ATTEMPT", "repository": "GITHUB_REPOSITORY",
            "ref": "GITHUB_REF", "event": "GITHUB_EVENT_NAME"}
    context = {key: os.environ.get(variable, "") for key, variable in keys.items()}
    expected_ref = r"refs/pull/[1-9][0-9]*/merge" if context["event"] == "pull_request" else r"refs/heads/[^\s]+"
    if (os.environ.get("GITHUB_SHA") != commit or context["repository"] != "leizd/DeepSeek-Infra"
            or not re.fullmatch(r"[1-9][0-9]*", context["runId"]) or not re.fullmatch(r"[1-9][0-9]*", context["attempt"])
            or not re.fullmatch(expected_ref, context["ref"])
            or context["event"] not in {"push", "pull_request", "workflow_dispatch"}):
        raise ValueError("Missing or rebound exact-source cloud workflow context")
    return context


def instrumentation_cases(root: Path) -> set[str]:
    directory = root / "android/app/src/androidTest/java"
    cases: set[str] = set()
    for path in sorted(directory.rglob("*.java")):
        source = path.read_text(encoding="utf-8")
        methods = re.findall(r"@Test(?:\([^)]*\))?\s+public\s+void\s+(\w+)\s*\(", source)
        if not methods:
            continue
        package = re.search(r"(?m)^package\s+([\w.]+);", source)
        if not package:
            raise ValueError("Instrumentation source has no package")
        for method in methods:
            case = f"{package[1]}.{path.stem}#{method}"
            if case in cases:
                raise ValueError("Duplicate instrumentation method")
            cases.add(case)
    if not cases:
        raise ValueError("Missing Android instrumentation inventory")
    return cases


def validate_instrumentation(text: str, expected: set[str]) -> list[str]:
    if not expected or "INSTRUMENTATION_FAILED:" in text or "FAILURES!!!" in text:
        raise ValueError("Instrumentation failed or has no expected workload")
    fields: dict[str, str] = {}
    started: set[str] = set()
    passed: set[str] = set()
    final: list[int] = []
    for line in text.splitlines():
        if line.startswith("INSTRUMENTATION_STATUS: "):
            key, separator, value = line.removeprefix("INSTRUMENTATION_STATUS: ").partition("=")
            if separator:
                if key in fields:
                    raise ValueError("Duplicate instrumentation field")
                fields[key] = value
        elif line.startswith("INSTRUMENTATION_STATUS_CODE: "):
            code = int(line.removeprefix("INSTRUMENTATION_STATUS_CODE: "))
            case = fields.get("class", "") + "#" + fields.get("test", "")
            if case not in expected or int(fields.get("numtests", "0")) != len(expected):
                raise ValueError("Unbound instrumentation case or workload count")
            if code == 1 and case not in started and case not in passed:
                started.add(case)
            elif code == 0 and case in started and case not in passed:
                passed.add(case)
                started.remove(case)
            else:
                raise ValueError("Failed, ignored, duplicate or unstarted instrumentation case")
            fields = {}
        elif line.startswith("INSTRUMENTATION_CODE: "):
            final.append(int(line.removeprefix("INSTRUMENTATION_CODE: ")))
    summaries = re.findall(r"(?m)^OK \((\d+) tests?\)\s*$", text)
    if fields or started or passed != expected or final != [-1] or summaries != [str(len(expected))]:
        raise ValueError("Incomplete Android instrumentation result")
    return sorted(passed)



MCP_CUSTODY_KINDS = ("timeout-regular", "timeout-detached", "parent-exit", "driver-death", "cancel")


def custody_probe_hashes(apk: Path, test_apk: Path) -> dict[str, str]:
    """Bind actual app-UID observations to test-APK-only NDK executables."""
    name = "libdeepseek_mcp_custody_probe.so"
    expected = {f"lib/{abi}/{name}" for abi in ("x86_64", "arm64-v8a")}
    with zipfile.ZipFile(apk) as product, zipfile.ZipFile(test_apk) as tests:
        if any(name in entry for entry in product.namelist()):
            raise ValueError("Instrumentation custody fixture entered the production APK")
        selected = [entry for entry in tests.namelist() if name in entry]
        if len(selected) != 2 or set(selected) != expected:
            raise ValueError("Missing, duplicate or extra custody probe ABI")
        result = {}
        for abi, machine in (("x86_64", 62), ("arm64-v8a", 183)):
            entry = tests.getinfo(f"lib/{abi}/{name}")
            if entry.file_size > 64 * 1024 * 1024:
                raise ValueError("Custody probe exceeded the bound")
            data = tests.read(entry)
            if (len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01"
                    or struct.unpack_from("<H", data, 16)[0] != 3 or struct.unpack_from("<H", data, 18)[0] != machine):
                raise ValueError("Custody probe is not the expected NDK PIE executable")
            result[abi] = hashlib.sha256(data).hexdigest()
    return result


def collect_custody_reports(output: Path, expected_hash: str, api_level: int) -> list[dict[str, Any]]:
    cases = []
    for kind in MCP_CUSTODY_KINDS:
        name = f"mcp-custody-{kind}.json"
        text = adb(["exec-out", "run-as", APPLICATION, "head", "-c", "65537", "files/" + name], output / name, timeout=15)
        if len(text.encode("utf-8")) > 65536:
            raise ValueError("Custody observation exceeded the bound")
        case = json.loads(text)
        if (case.get("kind") != kind or case.get("api") != api_level or case.get("status") != "PASS"
                or case.get("binarySha256") != expected_hash or case.get("fixtureOnly") is not True
                or case.get("fullSandboxQualified") is not False or case.get("shortControlPassed") is not True
                or case.get("rootReaped") is not True or case.get("descendantReaped") is not True
                or case.get("driverExited") is not True or case.get("lateEffectPresent") is not False
                or not isinstance(case.get("appUid"), int) or case["appUid"] < 10000
                or case.get("sampleElapsedMs", 0) < 3000 or case.get("lateEffectElapsedMs", 0) < 6500):
            raise ValueError("Actual Android custody observation did not qualify: " + kind)
        identities = [case[role] for role in ("driver", "root", "descendant")]
        if len({identity["pid"] for identity in identities}) != 3:
            raise ValueError("Reused Android fixture identities")
        for identity in identities:
            if (not isinstance(identity["pid"], int) or identity["pid"] <= 0 or identity.get("uid") != case["appUid"]
                    or not re.fullmatch(r"[1-9][0-9]*", identity.get("startTicks", ""))):
                raise ValueError("Native child identity is not bound to the app UID")
        if kind != "timeout-regular":
            if (case["root"]["session"] == case["descendant"]["session"]
                    or str(case["descendant"]["pid"]) != case["descendant"]["session"]):
                raise ValueError("Detached Android session was not actually observed")
        outcome = case.get("outcome")
        if kind == "driver-death":
            if outcome is not None or not isinstance(case.get("driverExitCode"), int) or case["driverExitCode"] == 0:
                raise ValueError("Actual Android parent death was not observed")
        else:
            if case.get("driverExitCode") != 0 or not isinstance(outcome, dict) or outcome.get("stderr") != "":
                raise ValueError("Android native runner did not complete normally")
            if "native Android root started" not in outcome.get("stdout", ""):
                raise ValueError("Android root output was lost")
            if kind.startswith("timeout"):
                if outcome.get("error") != "test run exceeded 2 seconds" or outcome.get("exitCode") is not None:
                    raise ValueError("Original Android timeout result was lost")
            elif kind == "cancel":
                if outcome.get("error") is not None or outcome.get("exitCode") is not None or outcome.get("cancelRequested") is not True:
                    raise ValueError("Actual Android cancellation result was lost")
            elif (outcome.get("error") is not None or outcome.get("exitCode") != 0
                    or "native Android root completed" not in outcome.get("stdout", "")):
                raise ValueError("Original Android root success result was lost")
        cases.append(case)
    return cases


def build_report(root: Path, apk: Path, test_apk: Path, commit: str) -> dict[str, Any]:
    identity = source_identity(root)
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or identity["commit"] != commit or identity["dirty"]:
        raise ValueError("Android build is not bound to the clean exact source commit")
    package = inspect_package(apk)
    if package["sourceIdentity"] != identity:
        raise ValueError("Packaged native source differs from the exact checkout")
    # Canonical JSON also distinguishes false from numeric zero. The runtime
    # APK hash binds this exact manifest and every retained executable hash.
    if json.dumps(package.get("goCompatibility"), sort_keys=True) != json.dumps(expected_android_go_compatibility(), sort_keys=True):
        raise ValueError("Packaged Android Go standard-library variant is missing or unreviewed")
    return {"schemaVersion": 1, "kind": "native-android-cloud-build-v1", "status": "PASS", "sourceCommit": commit,
            "sourceIdentity": identity, "workflow": cloud_context(commit), "runtimeApkSha256": digest(apk), "testApkSha256": digest(test_apk),
            "instrumentationCases": sorted(instrumentation_cases(root)), "packageInspection": package,
            "instrumentationCustodyProbeSha256": custody_probe_hashes(apk, test_apk), "custodyFixtureInProduct": False,
            "deviceExecutionVerified": False, "releaseQualified": False, "fullMigrationQualified": False}


def adb(arguments: list[str], output: Path, timeout: int = 120) -> str:
    result = subprocess.run(["adb", *arguments], capture_output=True, timeout=timeout)
    output.write_bytes(result.stdout + result.stderr)
    if result.returncode:
        raise ValueError("ADB command failed; inspect " + output.name)
    return result.stdout.decode("utf-8", errors="replace").strip()


def collect_runtime_logs(output: Path) -> list[dict[str, str]]:
    """Read bounded child stderr only from the installed disposable test app."""
    results = []
    for service in ("native-launcher", "gateway", "control", "worker"):
        path = output / f"runtime-{service}.log"
        try:
            adb(["shell", "-T", "run-as", APPLICATION, "tail", "-c", "131072",
                 f"app_native-logs/{service}.log"], path, timeout=15)
            state = "COLLECTED"
        except (ValueError, OSError, subprocess.SubprocessError):
            state = "UNAVAILABLE"
        results.append({"service": service, "status": state, "path": path.name})
    return results


def collect_device_environment(output: Path) -> dict[str, dict[str, str]]:
    """Collect fixed kernel facts without personal identifiers or app state."""
    facts = {}
    for name, option in (("kernelRelease", "-r"), ("kernelMachine", "-m")):
        try:
            value = adb(["shell", "-T", "uname", option], output / f"device-{name}.log", timeout=15)
            facts[name] = {"status": "COLLECTED", "value": value[:512]} if value else {"status": "UNAVAILABLE"}
        except (ValueError, OSError, subprocess.SubprocessError):
            facts[name] = {"status": "UNAVAILABLE"}
    return facts


def run_device(args: argparse.Namespace) -> int:
    output: Path = args.output
    output.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"schemaVersion": 1, "status": "FAIL", "sourceCommit": args.expected_commit,
                              "checkedAt": datetime.now(timezone.utc).isoformat(), "emulatorApiLevel": args.api_level,
                              "deviceExecutionVerified": False, "releaseQualified": False, "fullMigrationQualified": False,
                              "productionAuthorityQualified": False, "zeroPythonWorkloadsQualified": False,
                              "arm64DeviceExecutionVerified": False, "nativeCompilationOnCloudOnly": True}
    sandbox_prepared = False
    try:
        supplied = json.loads(args.build_report.read_text(encoding="utf-8"))
        actual = build_report(args.root, args.apk, args.test_apk, args.expected_commit)
        # Inspection timestamps are observational; every source/package binding is exact.
        for field in ("schemaVersion", "kind", "status", "sourceCommit", "sourceIdentity", "runtimeApkSha256",
                      "workflow", "testApkSha256", "instrumentationCases", "instrumentationCustodyProbeSha256", "custodyFixtureInProduct",
                      "deviceExecutionVerified", "releaseQualified", "fullMigrationQualified"):
            if supplied.get(field) != actual[field]:
                raise ValueError("Cloud APK artifact or source binding changed: " + field)
        api = adb(["shell", "getprop", "ro.build.version.sdk"], output / "api-level.log")
        abi = adb(["shell", "getprop", "ro.product.cpu.abi"], output / "device-abi.log")
        if api != str(args.api_level) or abi != "x86_64":
            raise ValueError("Unexpected emulator API or ABI")
        report.update(runtimeApkSha256=actual["runtimeApkSha256"], testApkSha256=actual["testApkSha256"],
                      sourceIdentity=actual["sourceIdentity"], workflow=actual["workflow"], packagedABIs=sorted(actual["packageInspection"]["binaries"]),
                      apkPythonEntries=actual["packageInspection"]["pythonEntries"], emulatorABI=abi)
        adb(["install", "-r", "-t", str(args.apk)], output / "install-runtime.log")
        adb(["install", "-r", "-t", str(args.test_apk)], output / "install-tests.log")
        if adb(["shell", "pm", "clear", APPLICATION], output / "clear-disposable-app.log") != "Success":
            raise ValueError("Disposable cloud app sandbox was not reset")
        sandbox_prepared = True
        text = adb(["shell", "am", "instrument", "-w", "-r", "-e", "disableAnalytics", "true", RUNNER],
                   output / "instrumentation.log", timeout=900)
        passed = validate_instrumentation(text, set(actual["instrumentationCases"]))
        custody = collect_custody_reports(output, actual["instrumentationCustodyProbeSha256"]["x86_64"], args.api_level)
        report.update(status="PASS", deviceExecutionVerified=True, executedTests=passed, executedTestCount=len(passed),
                      actualAppUidCustodyCases=custody, instrumentationCustodyProbeSha256=actual["instrumentationCustodyProbeSha256"],
                      custodyFixtureInProduct=False, fullSandboxQualified=False, retainedTestEngineQualified=False)
    except (ValueError, OSError, subprocess.SubprocessError, KeyError, TypeError, zipfile.BadZipFile, struct.error) as exc:
        report["error"] = str(exc)
    finally:
        # A bad source/APK must not authorize reading an existing app sandbox.
        # These files remain after the instrumentation runner stops its process.
        report["nativeRuntimeLogs"] = collect_runtime_logs(output) if sandbox_prepared else []
        report["platformEnvironment"] = collect_device_environment(output) if sandbox_prepared else {}
        # Failed instrumentation must retain observations too; these fixed files
        # are read only after the disposable app's successful reset.
        custody_diagnostics = []
        if sandbox_prepared:
            for kind in MCP_CUSTODY_KINDS:
                name = f"mcp-custody-{kind}.json"
                path = output / name
                try:
                    if not path.exists():
                        adb(["shell", "-T", "run-as", APPLICATION, "head", "-c", "65537", "files/" + name], path, timeout=15)
                    value = json.loads(path.read_text(encoding="utf-8"))
                    custody_diagnostics.append(dict(kind=kind, status="COLLECTED", observationStatus=value.get("status", "FAIL")))
                except (ValueError, OSError, subprocess.SubprocessError):
                    custody_diagnostics.append(dict(kind=kind, status="UNAVAILABLE"))
        report["custodyDiagnostics"] = custody_diagnostics
        try:
            adb(["logcat", "-d", "-t", "2000"], output / "logcat.log", timeout=30)
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
        report["logs"] = [{"path": path.name, "bytes": path.stat().st_size, "sha256": digest(path)}
                          for path in sorted(output.glob("*.log"))]
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "sourceCommit", "emulatorApiLevel", "deviceExecutionVerified", "fullMigrationQualified")}))
    return 0 if report["status"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("stage", "run"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--test-apk", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-level", type=int, choices=(24, 26, 28, 30, 34))
    parser.add_argument("--build-report", type=Path)
    args = parser.parse_args()
    if args.mode == "stage":
        report = build_report(args.root, args.apk, args.test_apk, args.expected_commit)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "PASS", "sourceCommit": args.expected_commit, "deviceExecutionVerified": False}))
        return 0
    if args.api_level is None or args.build_report is None:
        parser.error("Device runs require --api-level and --build-report")
    return run_device(args)


if __name__ == "__main__":
    raise SystemExit(main())
