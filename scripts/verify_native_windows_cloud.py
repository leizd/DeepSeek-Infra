"""Actual Windows native app/WebView2 gate on a disposable GitHub-hosted runner.

Python observes processes, public HTTP and readonly provenance as an offline
verifier. Production requests are served only by the original Go/Rust binaries.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
WINDOW_TEST = "TestNativeWindowLoadsAuthenticatedReactAndRunsFileWorkload"
WINDOW_PACKAGE = "github.com/leizd/DeepSeek-Infra/go/internal/desktop"
NATIVE_ROLES = {"deepseek-launch", "deepseekd", "deepseek-worker", "deepseek-gateway", "deepseek-desktop"}
ORIGINAL = "Native desktop upload 2026\n原生桌面阅读验证\nA nonempty retained file workload.".encode()


def require_cloud_windows(environment: dict[str, str], system: str) -> None:
    expected = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Windows",
                "GITHUB_REPOSITORY": "leizd/DeepSeek-Infra"}
    event = environment.get("GITHUB_EVENT_NAME")
    if (system != "win32" or any(environment.get(key) != value for key, value in expected.items())
            or event not in {"push", "workflow_dispatch"}
            or event == "push" and not environment.get("GITHUB_REF_NAME", "").startswith("codex/rust-cloud/control-rpc-mtls-")
            or not re.fullmatch(r"[0-9a-f]{40}", environment.get("GITHUB_SHA", ""))
            or not environment.get("GITHUB_RUN_ID", "").isdigit()):
        raise RuntimeError("WINDOWS_CLOUD_HOST_REQUIRED")


def validate_window_receipt(receipt: dict[str, Any]) -> str:
    checks = receipt.get("checks", {})
    if not isinstance(checks, dict):
        raise RuntimeError("WINDOWS_WEBVIEW_WORKLOAD_INCOMPLETE")
    booleans = ("react", "desktop", "fileId", "readerText", "originalEqual", "layout")
    statuses = ("configStatus", "uploadStatus", "readerStatus", "sourceStatus")
    file_id = checks.get("retainedFileId")
    if (receipt.get("platform") != "windows-amd64-webview2" or receipt.get("releaseQualified") is not False
            or any(checks.get(key) is not True for key in booleans) or any(checks.get(key) != 200 for key in statuses)
            or checks.get("failure") or not isinstance(file_id, str) or not 1 <= len(file_id) <= 512
            or any(char in file_id for char in "\r\n\0")):
        raise RuntimeError("WINDOWS_WEBVIEW_WORKLOAD_INCOMPLETE")
    return file_id


def inspect_window_events(path: Path) -> dict[str, Any]:
    starts = terminals = packages = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            raise RuntimeError("WINDOWS_NATIVE_TEST_INCOMPLETE") from None
        if row.get("Package") != WINDOW_PACKAGE:
            raise RuntimeError("WINDOWS_NATIVE_TEST_INCOMPLETE")
        test, action = row.get("Test"), row.get("Action")
        if test:
            if test != WINDOW_TEST:
                raise RuntimeError("WINDOWS_NATIVE_TEST_INCOMPLETE")
            starts += action == "run"
            if action in {"pass", "fail", "skip"}:
                if action != "pass":
                    raise RuntimeError("WINDOWS_NATIVE_TEST_INCOMPLETE")
                terminals += 1
        elif action in {"pass", "fail", "skip"}:
            if action != "pass":
                raise RuntimeError("WINDOWS_NATIVE_TEST_INCOMPLETE")
            packages += 1
        if "(cached)" in row.get("Output", ""):
            raise RuntimeError("WINDOWS_NATIVE_TEST_INCOMPLETE")
    if (starts, terminals, packages) != (1, 1, 1):
        raise RuntimeError("WINDOWS_NATIVE_TEST_INCOMPLETE")
    return dict(topLevelPassed=1, skipped=0, cached=False, sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def process_inventory() -> list[dict[str, Any]]:
    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_void_p), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("name", wintypes.WCHAR * 260)]

    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise RuntimeError("WINDOWS_PROCESS_OBSERVATION_FAILED")
    rows: list[dict[str, Any]] = []
    try:
        entry = Entry()
        entry.size = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            rows.append(dict(pid=int(entry.pid), parent=int(entry.parent), executable=entry.name.lower().removesuffix(".exe")))
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return rows


def process_tree(parent: int) -> list[dict[str, Any]]:
    rows = process_inventory()
    selected, frontier = [], {parent}
    while frontier:
        children = [row for row in rows if row["pid"] in frontier]
        selected.extend(children)
        frontier = {row["pid"] for row in rows if row["parent"] in frontier} - {row["pid"] for row in selected}
    return selected


def active_owned_pids(pids: set[int], inventory: list[dict[str, Any]]) -> set[int]:
    # A dead intermediate parent must not hide its surviving, now orphaned child.
    return pids.intersection(row["pid"] for row in inventory)


def actual_window(pids: set[int]) -> dict[str, int] | None:
    user = getattr(ctypes, "WinDLL")("user32", use_last_error=True)
    callback_type = getattr(ctypes, "WINFUNCTYPE")(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    result: list[dict[str, int]] = []

    def observe(hwnd: int, _: int) -> bool:
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(hwnd, name, len(name))
        if pid.value in pids and name.value == "DeepSeekNativeDesktopV1" and user.IsWindowVisible(hwnd):
            rect = wintypes.RECT()
            if user.GetWindowRect(hwnd, ctypes.byref(rect)) and rect.right - rect.left >= 760 and rect.bottom - rect.top >= 520:
                result.append(dict(hwnd=int(hwnd), pid=int(pid.value), width=rect.right - rect.left, height=rect.bottom - rect.top))
        return True

    user.EnumWindows(callback_type(observe), 0)
    if len(result) > 1:
        raise RuntimeError("WINDOWS_PRODUCTION_WINDOW_AMBIGUOUS")
    return result[0] if result else None


def request(origin: str, path: str, token: str) -> bytes:
    with urlopen(Request(origin + path, headers={"Authorization": "Bearer " + token}), timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("WINDOWS_PUBLIC_RESPONSE_REFUSED")
        body = response.read(2 * 1024**2 + 1)
        if len(body) > 2 * 1024**2:
            raise RuntimeError("WINDOWS_PUBLIC_RESPONSE_TOO_LARGE")
        return body


def profile_proof(profile: Path) -> dict[str, Any]:
    database = profile / "go-control/control.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        origin, state, publication = db.execute("SELECT origin,state,publication FROM control_native_origin WHERE singleton=1").fetchone()
        owner, fence = db.execute("SELECT owner_instance_id,fencing_token FROM control_writer WHERE singleton=1").fetchone()
        domains = db.execute("SELECT owner,state FROM control_cutover").fetchall()
    if state != "published" or not origin or not publication or not owner or fence <= 0 or len(domains) != 12:
        raise RuntimeError("WINDOWS_NATIVE_ORIGIN_NOT_PUBLISHED")
    if any(owner != "go" or state not in {"go_authoritative", "python_shadow", "python_disabled"} for owner, state in domains):
        raise RuntimeError("WINDOWS_NATIVE_DOMAIN_AUTHORITY_INCOMPLETE")
    public = {}
    for name in ["native-origin-intent.pb", "native-profile/profile-id", "native-profile/signer-public-pin"]:
        data = (profile / "go-control" / name).read_bytes()
        if not data or len(data) > 65536:
            raise RuntimeError("WINDOWS_RETAINED_PUBLIC_ORIGIN_INVALID")
        public[name] = hashlib.sha256(data).hexdigest()
    return dict(originSha256=hashlib.sha256(origin).hexdigest(), publicationSha256=hashlib.sha256(publication).hexdigest(),
                publicFiles=public, writerOwner=owner, writerFence=fence, authoritativeDomains=12)


def wait_ready(process: subprocess.Popen[bytes], origin: str, profile: Path) -> tuple[str, list[dict[str, Any]], dict[str, int]]:
    deadline = time.monotonic() + 75
    while process.poll() is None and time.monotonic() < deadline:
        try:
            token = (profile / ".auth-token").read_text().strip().splitlines()[0]
            status = json.loads(request(origin, "/api/control/status", token))
            rows = process_tree(process.pid)
            names = {row["executable"] for row in rows}
            if any("python" in name or name in {"node", "nodejs"} for name in names):
                raise RuntimeError("WINDOWS_PRODUCTION_LEGACY_RUNTIME_OBSERVED")
            view = actual_window({row["pid"] for row in rows if row["executable"] == "deepseek-desktop"})
            if status.get("ok") is True and NATIVE_ROLES <= names and view:
                return token, rows, view
        except (OSError, ValueError, IndexError):
            pass
        time.sleep(0.25)
    raise RuntimeError("WINDOWS_NATIVE_APP_DID_NOT_BECOME_READY")


def close_window(process: subprocess.Popen[bytes], view: dict[str, int], pids: set[int]) -> None:
    pids |= {row["pid"] for row in process_tree(process.pid)}
    user = getattr(ctypes, "WinDLL")("user32", use_last_error=True)
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    if not user.PostMessageW(view["hwnd"], 0x0010, 0, 0):
        raise RuntimeError("WINDOWS_NATIVE_WINDOW_CLOSE_REFUSED")
    try:
        code = process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        raise RuntimeError("WINDOWS_NATIVE_WINDOW_CLOSE_DID_NOT_STOP_LAUNCHER") from None
    if code != 0:
        raise RuntimeError("WINDOWS_NATIVE_WINDOW_CLOSE_FAILED")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not active_owned_pids(pids, process_inventory()):
            return
        time.sleep(0.1)
    raise RuntimeError("WINDOWS_NATIVE_DESCENDANTS_RETAINED_AFTER_CLOSE")


def main() -> int:
    require_cloud_windows(dict(os.environ), sys.platform)
    parser = argparse.ArgumentParser()
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    binaries, profile, out = args.bin.resolve(), args.profile.resolve(), args.out.resolve()
    if profile.exists() or profile.is_relative_to(ROOT) or out.is_relative_to(profile) or profile.is_relative_to(out):
        raise RuntimeError("WINDOWS_DISPOSABLE_EMPTY_PROFILE_REQUIRED")
    out.mkdir(parents=True, exist_ok=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    if sha != os.environ["GITHUB_SHA"] or subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip():
        raise RuntimeError("WINDOWS_EXACT_CLEAN_SOURCE_REQUIRED")
    for port in (8090, 50052):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = "http://127.0.0.1:" + str(port)
    environment = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT", "USERPROFILE", "LOCALAPPDATA", "APPDATA"}}
    environment.update(DEEPSEEK_NATIVE_BIN=str(binaries), DEEPSEEK_INFRA_ROOT=str(profile), DEEPSEEK_INFRA_STATIC_DIR=str(ROOT / "static"))
    report: dict[str, Any] = dict(status="FAIL", sourceCommit=sha, sourceTree=subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT).decode().strip(),
                                  runId=os.environ["GITHUB_RUN_ID"], event=os.environ["GITHUB_EVENT_NAME"], platform="windows-amd64-webview2",
                                  scope="actual app window, original native bootstrap, test-host WebView2 file workload and whole-app close/restart only",
                                  offlineStoreSeeding=False, privateKeyInspection=False, productionProviderQualified=False,
                                  allDesktopFeaturesQualified=False, fullMigrationQualified=False, readiness="NOT_READY")
    report["binarySha256"] = {name: hashlib.sha256((binaries / (name + ".exe")).read_bytes()).hexdigest() for name in sorted(NATIVE_ROLES)}
    report["runnerImage"] = {key: os.environ.get(key) for key in ("ImageOS", "ImageVersion", "RUNNER_ARCH")}
    process = None
    tokens: list[str] = []
    try:
        with (out / "launcher.log").open("wb") as log:
            def launch() -> subprocess.Popen[bytes]:
                return subprocess.Popen([str(binaries / "deepseek-launch.exe"), "--app", "--host", "127.0.0.1", "--port", str(port), "--no-open", "--no-prompt"],
                                        cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

            process = launch()
            token, first, view = wait_ready(process, origin, profile)
            tokens.append(token)
            initial = profile_proof(profile)
            report.update(initialProcesses=first, initialWindow=view, initialNativeOrigin=initial)
            receipt_path = out / "webview-workload.json"
            test_environment = dict(os.environ, CGO_ENABLED="0", GOTOOLCHAIN="local",
                                    DEEPSEEK_DESKTOP_INTEGRATION_URL=origin + "/?" + urlencode(dict(token=token, desktop="1")),
                                    DEEPSEEK_DESKTOP_INTEGRATION_PROFILE=str(profile / ".platform-test-webview"), DEEPSEEK_DESKTOP_INTEGRATION_REPORT=str(receipt_path))
            tested = subprocess.run(["go", "test", "-tags=native_desktop_integration", "-json", "-count=1", "-timeout=2m", "./internal/desktop", "-run", "^" + WINDOW_TEST + "$"],
                                    cwd=ROOT / "go", env=test_environment, capture_output=True, timeout=180)
            (out / "window-tests.jsonl").write_bytes(tested.stdout)
            (out / "window-tests-stderr.log").write_bytes(tested.stderr)
            if tested.returncode:
                raise RuntimeError("WINDOWS_ACTUAL_WEBVIEW_TEST_FAILED")
            report["actualWindowTest"] = inspect_window_events(out / "window-tests.jsonl")
            file_id = validate_window_receipt(json.loads(receipt_path.read_text()))
            close_window(process, view, {row["pid"] for row in first})
            report["firstWindowCloseStoppedNativeTree"] = True
            process = launch()
            token, second, view = wait_ready(process, origin, profile)
            tokens.append(token)
            restarted = profile_proof(profile)
            if {row["pid"] for row in first}.intersection(row["pid"] for row in second):
                raise RuntimeError("WINDOWS_RESTART_REUSED_OLD_NATIVE_PROCESS")
            if any(initial[key] != restarted[key] for key in ("originSha256", "publicationSha256", "publicFiles")):
                raise RuntimeError("WINDOWS_RESTART_REPLACED_ORIGINAL_PROVENANCE")
            if restarted["writerFence"] <= initial["writerFence"]:
                raise RuntimeError("WINDOWS_RESTART_DID_NOT_ADVANCE_ORIGINAL_WRITER")
            if request(origin, "/api/file-source?" + urlencode(dict(fileId=file_id)), token) != ORIGINAL:
                raise RuntimeError("WINDOWS_RESTART_LOST_ORIGINAL_FILE")
            close_window(process, view, {row["pid"] for row in second})
            report.update(status="PASS", restartedProcesses=second, restartedWindow=view, restartedNativeOrigin=restarted,
                          originalNativeOriginRetained=True, originalFileRetainedAfterAppRestart=True, writerFenceAdvanced=True,
                          secondWindowCloseStoppedNativeTree=True, controlledWindowsPlatformQualified=True)
    except Exception as error:
        value = str(error)
        report["failure"] = value if re.fullmatch(r"[A-Z_]+", value) else "WINDOWS_PLATFORM_PROBE_UNEXPECTED_FAILURE"
        report["exceptionType"] = type(error).__name__
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=15)
        for name in ["launcher.log", "window-tests.jsonl", "window-tests-stderr.log"]:
            path = out / name
            if path.exists():
                raw = path.read_bytes()
                for token in tokens:
                    raw = raw.replace(token.encode(), b"[isolated-auth-redacted]")
                path.write_bytes(raw)
        (out / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"initialProcesses", "restartedProcesses"}}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
