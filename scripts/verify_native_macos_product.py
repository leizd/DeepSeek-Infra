"""Observe the actual Go/Rust Mac app, a retained file and a full app restart on disposable Actions runners.

Python is an offline observer. It is absent from the launched app's owned tree.
"""
from __future__ import annotations

import argparse
from collections.abc import Iterator
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import struct
import subprocess
import sys
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import prepare_native_macos_product as fixture
from verify_native_windows_cloud import canonical_source, profile_proof

ROOT = Path(__file__).resolve().parents[1]
ROLES = {*fixture.GO_ROLES, "deepseek-worker", "deepseek-gateway"}
ORIGINAL = "Native macOS file workload\n原生桌面持久化验证\nRetained across an actual app restart.\n".encode()
BSD = struct.Struct("=12I16s32s6I2Q")
RESPONSE_METHODS = {"control-before.json": "GET", "react-index.txt": "GET", "file-upload.json": "POST",
    "file-reader.json": "POST", "file-source-before.txt": "GET", "file-source-after.txt": "GET", "control-after.json": "GET"}


def require_cloud(environment: dict[str, str], target: str) -> None:
    if target != "darwin" or environment.get("GITHUB_ACTIONS") != "true" or environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        raise RuntimeError("MACOS_PRODUCT_REQUIRES_DISPOSABLE_CLOUD")


def decode_bsdinfo(raw: bytes, pid: int) -> dict[str, int]:
    # Public Apple proc_bsdinfo ABI; identity includes both start timeval fields.
    # https://github.com/apple/darwin-xnu/blob/main/bsd/sys/proc_info.h
    if len(raw) != BSD.size:
        raise RuntimeError("MACOS_PROCESS_IDENTITY_INVALID")
    fields = BSD.unpack(raw)
    seconds, micros = fields[20:22]
    if fields[3] != pid or pid <= 1 or seconds <= 0 or not 0 <= micros < 1_000_000:
        raise RuntimeError("MACOS_PROCESS_IDENTITY_INVALID")
    return dict(pid=pid, parentPid=fields[4], uid=fields[5], status=fields[1], startMicroseconds=seconds * 1_000_000 + micros)


def owned_descendants(rows: list[dict[str, Any]], root: int) -> list[dict[str, Any]]:
    selected = {root} if any(row["pid"] == root for row in rows) else set()
    while True:
        expanded = selected | {row["pid"] for row in rows if row["parentPid"] in selected}
        if expanded == selected:
            return [row for row in rows if row["pid"] in selected]
        selected = expanded


class NativeProcesses:
    def __init__(self) -> None:
        self.library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        self.library.proc_pidinfo.restype = ctypes.c_int
        self.library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.library.proc_pidpath.restype = ctypes.c_int
        observed = self.identity(os.getpid())
        if observed is None or observed["parentPid"] != os.getppid() or observed["uid"] != getattr(os, "geteuid")() or BSD.size != 136:
            raise RuntimeError("MACOS_PROCESS_ABI_SELF_CHECK_FAILED")

    def identity(self, pid: int) -> dict[str, int] | None:
        buffer = ctypes.create_string_buffer(BSD.size)
        ctypes.set_errno(0)
        size = self.library.proc_pidinfo(pid, 3, 0, buffer, BSD.size)
        if size == 0 and ctypes.get_errno() in {errno.ESRCH, errno.ENOENT}:
            return None
        if size != BSD.size:
            raise RuntimeError("MACOS_PROCESS_IDENTITY_UNAVAILABLE")
        return decode_bsdinfo(buffer.raw, pid)

    def tree(self, root: int) -> list[dict[str, Any]]:
        text = subprocess.check_output(["ps", "-axo", "pid=,ppid="], text=True, timeout=10)
        parents = [dict(pid=int(parts[0]), parentPid=int(parts[1])) for line in text.splitlines() if len(parts := line.split()) == 2]
        result = []
        for selected in owned_descendants(parents, root):
            before = self.identity(selected["pid"])
            if before is None or before["status"] == 5:
                continue
            path = ctypes.create_string_buffer(4096)
            if self.library.proc_pidpath(selected["pid"], path, len(path)) <= 0:
                if self.identity(selected["pid"]) is None:
                    continue
                raise RuntimeError("MACOS_PROCESS_EXECUTABLE_UNAVAILABLE")
            after = self.identity(selected["pid"])
            if after is None or after["status"] == 5:
                continue
            stable = ["pid", "parentPid", "uid", "startMicroseconds"]
            if before["parentPid"] != selected["parentPid"] or any(before[key] != after[key] for key in stable):
                raise RuntimeError("MACOS_PROCESS_IDENTITY_CHANGED_DURING_OBSERVATION")
            executable = Path(os.fsdecode(path.value)).resolve()
            result.append(dict(before, executable=executable.name, executablePath=str(executable),
                               executableSha256=hashlib.sha256(executable.read_bytes()).hexdigest()))
        return result


def actual_window(pids: set[int]) -> dict[str, Any] | None:
    """Read public WindowServer metadata, restricted to the actual owned UI PIDs."""
    cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    cf.CFArrayGetCount.argtypes, cf.CFArrayGetCount.restype = [ctypes.c_void_p], ctypes.c_long
    cf.CFArrayGetValueAtIndex.argtypes, cf.CFArrayGetValueAtIndex.restype = [ctypes.c_void_p, ctypes.c_long], ctypes.c_void_p
    cf.CFDictionaryGetValue.argtypes, cf.CFDictionaryGetValue.restype = [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_void_p
    cf.CFNumberGetValue.argtypes, cf.CFNumberGetValue.restype = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p], ctypes.c_bool
    cf.CFRelease.argtypes, cf.CFRelease.restype = [ctypes.c_void_p], None
    keys = {name: ctypes.c_void_p.in_dll(cg, "kCGWindow" + name).value for name in ["OwnerPID", "Layer", "Number", "Bounds"]}

    class Rect(ctypes.Structure):
        _fields_ = [(name, ctypes.c_double) for name in ["x", "y", "width", "height"]]

    cg.CGRectMakeWithDictionaryRepresentation.argtypes = [ctypes.c_void_p, ctypes.POINTER(Rect)]
    cg.CGRectMakeWithDictionaryRepresentation.restype = ctypes.c_bool
    windows = cg.CGWindowListCopyWindowInfo(1 | 16, 0)
    if not windows:
        raise RuntimeError("MACOS_WINDOWSERVER_INFORMATION_UNAVAILABLE")
    try:
        for index in range(cf.CFArrayGetCount(windows)):
            row = cf.CFArrayGetValueAtIndex(windows, index)
            values = {}
            for name in ["OwnerPID", "Layer", "Number"]:
                pointer = cf.CFDictionaryGetValue(row, keys[name])
                number = ctypes.c_int64()
                if not pointer or not cf.CFNumberGetValue(pointer, 4, ctypes.byref(number)):
                    raise RuntimeError("MACOS_WINDOWSERVER_ROW_INVALID")
                values[name] = number.value
            if values["OwnerPID"] not in pids or values["Layer"] != 0:
                continue
            bounds = Rect()
            if not cg.CGRectMakeWithDictionaryRepresentation(cf.CFDictionaryGetValue(row, keys["Bounds"]), ctypes.byref(bounds)):
                raise RuntimeError("MACOS_WINDOWSERVER_BOUNDS_INVALID")
            if values["Number"] > 0 and bounds.width >= 760 and bounds.height >= 520:
                return dict(pid=values["OwnerPID"], windowId=values["Number"], width=bounds.width, height=bounds.height)
    finally:
        cf.CFRelease(windows)
    return None


def record_response(out: Path, name: str, raw: bytes, token: str, *, method: str, uri: str, status: int) -> dict[str, Any]:
    if (name not in RESPONSE_METHODS or method != RESPONSE_METHODS[name] or type(status) is not int or status != 200
            or not token or token.encode() in raw or token in uri or not uri.startswith("/")
            or any(char in uri for char in "\r\n\x00") or len(raw) > 8 * 1024**2):
        raise RuntimeError("MACOS_WORKLOAD_RESPONSE_CAPTURE_REFUSED")
    path = out / name
    try:
        with path.open("xb") as stream:
            stream.write(raw)
    except OSError as error:
        raise RuntimeError("MACOS_WORKLOAD_RESPONSE_CAPTURE_REFUSED") from error
    return dict(path=name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), method=method, uri=uri, status=status)


def response_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from response_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from response_strings(child)


def verify_workload_responses(out: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Recompute workload evidence from response bytes; never infer it from PASS flags."""
    try:
        rows = report.get("responses")
        if not isinstance(rows, dict) or set(rows) != set(RESPONSE_METHODS):
            raise RuntimeError("MACOS_WORKLOAD_RESPONSE_SET_INVALID")
        file_id = report["retainedFileId"]
        if not isinstance(file_id, str) or not file_id:
            raise RuntimeError("MACOS_WORKLOAD_RESPONSE_FILE_ID_INVALID")
        source_uri = "/api/file-source?" + urlencode(dict(fileId=file_id))
        uris = {"control-before.json": "/api/control/status", "control-after.json": "/api/control/status",
            "react-index.txt": "/", "file-upload.json": "/api/file-text", "file-reader.json": "/api/file-reader",
            "file-source-before.txt": source_uri, "file-source-after.txt": source_uri}
        captured = {}
        for name, method in RESPONSE_METHODS.items():
            row = rows[name]
            path = out / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("MACOS_WORKLOAD_RESPONSE_FILE_INVALID")
            raw = path.read_bytes()
            if row != dict(path=name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                           method=method, uri=uris[name], status=200) or len(raw) > 8 * 1024**2:
                raise RuntimeError("MACOS_WORKLOAD_RESPONSE_BINDING_INVALID")
            captured[name] = raw
        upload = json.loads(captured["file-upload.json"])
        if len(upload["files"]) != 1 or upload["files"][0]["fileId"] != file_id:
            raise RuntimeError("MACOS_WORKLOAD_RESPONSE_UPLOAD_ID_INVALID")
        for name in ["control-before.json", "control-after.json"]:
            if json.loads(captured[name]).get("ok") is not True:
                raise RuntimeError("MACOS_WORKLOAD_RESPONSE_CONTROL_NOT_READY")
        reader_text = "\n".join(response_strings(json.loads(captured["file-reader.json"])))
        if "Native macOS file workload" not in reader_text or "原生桌面持久化验证" not in reader_text:
            raise RuntimeError("MACOS_WORKLOAD_RESPONSE_READER_INVALID")
        before, after = captured["file-source-before.txt"], captured["file-source-after.txt"]
        if before != ORIGINAL or after != before or report["originalFileSha256"] != hashlib.sha256(before).hexdigest():
            raise RuntimeError("MACOS_WORKLOAD_RESPONSE_RETAINED_BYTES_DIFFER")
        html = captured["react-index.txt"]
        fixture_rows = json.loads((out / "product-fixture.json").read_bytes())["files"]
        indexes = [row for row in fixture_rows if row["path"] == "static/ui/index.html"]
        if (len(indexes) != 1 or indexes[0]["sha256"] != hashlib.sha256(html).hexdigest()
                or indexes[0]["bytes"] != len(html) or b'<div id="root"' not in html or b"/assets/" not in html
                or report["originalReactResponseSha256"] != hashlib.sha256(html).hexdigest()):
            raise RuntimeError("MACOS_WORKLOAD_RESPONSE_ORIGINAL_UI_DIFFERS")
        return dict(responseCount=7, retainedFileId=file_id, originalFileSha256=hashlib.sha256(before).hexdigest(),
                    originalReactSha256=hashlib.sha256(html).hexdigest())
    except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
        raise RuntimeError("MACOS_WORKLOAD_RESPONSE_INVALID") from error


def request(origin: str, uri: str, token: str, data: bytes | None = None, content_type: str | None = None, *,
            evidence: tuple[Path, dict[str, Any], str] | None = None) -> bytes:
    headers = {"Authorization": "Bearer " + token}
    if content_type:
        headers["Content-Type"] = content_type
    sent = Request(origin + uri, data=data, headers=headers)
    with urlopen(sent, timeout=15) as response:
        if response.status != 200:
            raise RuntimeError("MACOS_NATIVE_HTTP_STATUS_UNEXPECTED")
        raw = response.read(8 * 1024**2 + 1)
        if len(raw) > 8 * 1024**2:
            raise RuntimeError("MACOS_NATIVE_HTTP_RESPONSE_TOO_LARGE")
        if evidence is not None:
            out, report, name = evidence
            row = record_response(out, name, raw, token, method=sent.get_method(), uri=uri, status=response.status)
            report.setdefault("responses", {})[name] = row
        return raw


def wait_ready(process: subprocess.Popen[bytes], origin: str, profile: Path, native: NativeProcesses,
               binaries: dict[str, str]) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    deadline = time.monotonic() + 75
    while process.poll() is None and time.monotonic() < deadline:
        try:
            token = (profile / ".auth-token").read_text().strip().splitlines()[0]
            status = json.loads(request(origin, "/api/control/status", token))
        except (OSError, ValueError, IndexError, URLError):
            time.sleep(0.25)
            continue
        rows = native.tree(process.pid)
        names = {row["executable"] for row in rows}
        if any("python" in name.lower() or name.lower() in {"node", "nodejs"} for name in names):
            raise RuntimeError("MACOS_PRODUCTION_LEGACY_RUNTIME_OBSERVED")
        for row in rows:
            if row["executable"] in ROLES and (row["executableSha256"] != binaries[row["executable"]] or row["uid"] != getattr(os, "geteuid")()):
                raise RuntimeError("MACOS_RUNNING_EXECUTABLE_DIFFERS_FROM_BUILD")
        window = actual_window({row["pid"] for row in rows if row["executable"] == "deepseek-desktop"})
        if status.get("ok") is True and ROLES <= names and window:
            return token, rows, window
        time.sleep(0.25)
    raise RuntimeError("MACOS_NATIVE_APP_DID_NOT_BECOME_READY")


def stop(process: subprocess.Popen[bytes], rows: list[dict[str, Any]], native: NativeProcesses) -> None:
    # Popen owns the direct child's wait state. Never signal a PID from a process listing.
    process.terminate()
    if process.wait(timeout=20) not in {0, -15}:
        raise RuntimeError("MACOS_NATIVE_LAUNCHER_SHUTDOWN_FAILED")
    deadline = time.monotonic() + 15
    while True:
        active = []
        for row in rows:
            current = native.identity(row["pid"])
            if current and current["startMicroseconds"] == row["startMicroseconds"] and current["status"] != 5:
                active.append(row["pid"])
        if not active:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("MACOS_OWNED_NATIVE_PROCESS_SURVIVED_SHUTDOWN")
        time.sleep(0.1)


def main() -> int:
    require_cloud(dict(os.environ), sys.platform)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ["bin", "fixture", "profile", "out"]:
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    binaries, static, profile, out = args.bin.resolve(), args.fixture.resolve(), args.profile.resolve(), args.out.resolve()
    if profile.exists() or profile.is_relative_to(ROOT) or not out.is_relative_to(ROOT / "artifacts") or out.is_relative_to(profile):
        raise RuntimeError("MACOS_DISPOSABLE_EMPTY_PROFILE_REQUIRED")
    out.mkdir(parents=True, exist_ok=True)
    commit, tree = fixture.source()
    arch = os.environ["DEEPSEEK_MACOS_ARCH"]
    if platform.machine() != {"amd64": "x86_64", "arm64": "arm64"}[arch]:
        raise RuntimeError("MACOS_ACTUAL_RUNNER_ARCHITECTURE_DIFFERS")
    pin = json.loads((ROOT / "release/native_runtime_toolchain_v1.json").read_bytes())["go"]
    prepared = fixture.verify_fixture(static, commit, tree, os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"], pin)
    checksums = {}
    for role in sorted(ROLES):
        path = binaries / role
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("MACOS_PRODUCT_BINARY_UNAVAILABLE")
        fixture.macho(path, arch)
        checksums[role] = hashlib.sha256(path.read_bytes()).hexdigest()
        if role in fixture.GO_ROLES and checksums[role] != next(r["sha256"] for r in prepared["files"] if r["path"] == arch + "/" + role):
            raise RuntimeError("MACOS_PRODUCT_GO_BINARY_DIFFERS")
    (out / "product-fixture.json").write_bytes((static / "product-fixture.json").read_bytes())
    native = NativeProcesses()
    for port in [8090, 50052]:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = "http://127.0.0.1:" + str(port)
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}}
    environment.update(DEEPSEEK_NATIVE_BIN=str(binaries), DEEPSEEK_INFRA_ROOT=str(profile), DEEPSEEK_INFRA_STATIC_DIR=str(static / "static"))
    process = None
    tokens: list[str] = []
    report: dict[str, Any] = dict(status="FAIL", sourceCommit=commit, sourceTree=tree, runId=os.environ["GITHUB_RUN_ID"],
        attempt=os.environ["GITHUB_RUN_ATTEMPT"], architecture=arch, platform=platform.platform(), canonicalSource=canonical_source(ROOT),
        scope="Actual five-binary app, visible owned WindowServer frame, public file API and graceful complete restart only",
        binarySha256=checksums, offlineStoreSeeding=False, privateKeyInspection=False, productionPythonObserved=False,
        actualReactDOMWorkloadQualified=False, userWindowCloseQualified=False, defaultBrowserQualified=False,
        fullDesktopQualified=False, zeroPythonWorkloadsQualified=False, completeDynamicDependencyClosureQualified=False,
        actualAllPhasesRecoveryQualified=False, allSupportedOSQualified=False, fullMigrationQualified=False, readiness="NOT_READY")
    try:
        with (out / "launcher.log").open("wb") as log:
            def launch() -> subprocess.Popen[bytes]:
                return subprocess.Popen([str(binaries / "deepseek-launch"), "--app", "--host", "127.0.0.1", "--port", str(port),
                                         "--no-open", "--no-prompt"], cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)

            process = launch()
            token, first, window = wait_ready(process, origin, profile, native, checksums)
            tokens.append(token)
            initial = profile_proof(profile)
            request(origin, "/api/control/status", token, evidence=(out, report, "control-before.json"))
            html = request(origin, "/", token, evidence=(out, report, "react-index.txt"))
            if b'<div id="root"' not in html or b"/assets/" not in html:
                raise RuntimeError("MACOS_ORIGINAL_REACT_APPLICATION_NOT_SERVED")
            boundary = "native-mac-disposable-file-boundary"
            body = ("--" + boundary + '\r\nContent-Disposition: form-data; name="files"; filename="native-macos.txt"\r\n'
                    "Content-Type: text/plain\r\n\r\n").encode() + ORIGINAL + (
                    "\r\n--" + boundary + '\r\nContent-Disposition: form-data; name="ocrEnabled"\r\n\r\nfalse\r\n--' + boundary + "--\r\n").encode()
            # Exactly one upload; an uncertain effect is never retried.
            uploaded = json.loads(request(origin, "/api/file-text", token, body, "multipart/form-data; boundary=" + boundary,
                                          evidence=(out, report, "file-upload.json")))
            files = uploaded.get("files", [])
            if len(files) != 1 or not isinstance(files[0].get("fileId"), str) or not files[0]["fileId"]:
                raise RuntimeError("MACOS_NATIVE_FILE_UPLOAD_FAILED")
            file_id = files[0]["fileId"]
            reader = json.loads(request(origin, "/api/file-reader", token, json.dumps(dict(fileId=file_id)).encode(), "application/json",
                                        evidence=(out, report, "file-reader.json")))
            if "原生桌面持久化验证" not in json.dumps(reader, ensure_ascii=False):
                raise RuntimeError("MACOS_NATIVE_FILE_READER_DIFFERS")
            uri = "/api/file-source?" + urlencode(dict(fileId=file_id))
            original_source = request(origin, uri, token, evidence=(out, report, "file-source-before.txt"))
            if original_source != ORIGINAL:
                raise RuntimeError("MACOS_NATIVE_FILE_SOURCE_DIFFERS")
            report.update(initialProcesses=first, initialWindow=window, initialNativeOrigin=initial,
                          retainedFileId=file_id, originalFileSha256=hashlib.sha256(original_source).hexdigest(),
                          originalReactResponseSha256=hashlib.sha256(html).hexdigest(), successfulFileUploads=1)
            stop(process, first, native)
            report["firstGracefulShutdownStoppedOriginalNativeTree"] = True
            process = launch()
            token, second, window = wait_ready(process, origin, profile, native, checksums)
            tokens.append(token)
            restarted = profile_proof(profile)
            request(origin, "/api/control/status", token, evidence=(out, report, "control-after.json"))
            if any(initial[k] != restarted[k] for k in ["originSha256", "publicationSha256", "publicFiles"]):
                raise RuntimeError("MACOS_RESTART_REPLACED_ORIGINAL_PROVENANCE")
            if restarted["writerFence"] <= initial["writerFence"]:
                raise RuntimeError("MACOS_RESTART_DID_NOT_ADVANCE_ORIGINAL_WRITER")
            if request(origin, uri, token, evidence=(out, report, "file-source-after.txt")) != ORIGINAL:
                raise RuntimeError("MACOS_RESTART_LOST_ORIGINAL_FILE")
            report["rawWorkloadResponseVerification"] = verify_workload_responses(out, report)
            stop(process, second, native)
            report.update(status="PASS", restartedProcesses=second, restartedWindow=window, restartedNativeOrigin=restarted,
                          originalNativeOriginRetained=True, originalFileRetainedAfterAppRestart=True, writerFenceAdvanced=True,
                          secondGracefulShutdownStoppedOriginalNativeTree=True, controlledNativeMacProductWorkloadQualified=True)
    except Exception as error:
        report["failure"] = str(error) if re.fullmatch(r"[A-Z_]+", str(error)) else "MACOS_PRODUCT_PROBE_UNEXPECTED_FAILURE"
        report["exceptionType"] = type(error).__name__
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=15)
        auth = profile / ".auth-token"
        if auth.is_file():
            tokens.extend(auth.read_text().strip().splitlines()[:1])
        path = out / "launcher.log"
        if path.exists():
            raw = path.read_bytes()
            for token in tokens:
                raw = raw.replace(token.encode(), b"[isolated-auth-redacted]")
            path.write_bytes(raw)
        (out / "result.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["status", "sourceCommit", "architecture", "readiness", "fullMigrationQualified"]}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
