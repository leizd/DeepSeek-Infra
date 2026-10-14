"""Actual native debug APK workload/recovery on an isolated emulator (no host backend)."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import re
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

PACKAGE = "com.deepseek.mobile.nativeverify"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--package-report", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--screenshot", required=True, type=Path)
    parser.add_argument("--port", type=int, default=58460)
    args = parser.parse_args()
    package_report = json.loads(args.package_report.read_text(encoding="utf-8"))
    assert package_report["status"] == "PASS" and not package_report["releaseQualified"]
    adb = [args.adb, "-s", args.serial]

    def command(*parts: str) -> bytes:
        return subprocess.check_output([*adb, *parts], stderr=subprocess.PIPE, timeout=30)

    def shell(*parts: str) -> str:
        return command("shell", *parts).decode("utf-8").strip()

    def app(*parts: str) -> bytes:
        return command("exec-out", "run-as", PACKAGE, *parts)

    assert args.serial.startswith("emulator-") and shell("getprop", "ro.kernel.qemu") == "1", "Use an isolated emulator"
    code_dir = str(PurePosixPath(shell("pm", "path", PACKAGE).removeprefix("package:")).parent)
    assert code_dir.startswith("/data/app/"), code_dir
    uid = int(app("id", "-u").decode())
    checks: list[dict[str, Any]] = []
    token = ""

    def progress(status: str, **details: Any) -> None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        payload = {"status": status, "checkedAt": datetime.now(timezone.utc).isoformat(), "releaseQualified": False,
                   "serial": args.serial, "package": PACKAGE, "appUid": uid, "apkSha256": package_report["apkSha256"],
                   "sourceIdentity": package_report["sourceIdentity"], "checks": checks, **details}
        args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def call(method: str, path: str, body: Any = None, *, authorized: bool = True) -> tuple[int, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", args.port, timeout=15)
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer " + token
        try:
            connection.request(method, path, None if body is None else json.dumps(body).encode(), headers)
            response = connection.getresponse()
            raw = response.read()
            result = json.loads(raw) if "json" in response.getheader("Content-Type", "") else raw
            return response.status, result
        finally:
            connection.close()

    def record(name: str, ok: bool, **details: Any) -> None:
        checks.append({"name": name, "ok": ok, **details})
        progress("RUNNING" if ok else "FAIL")
        assert ok, name

    def ready() -> None:
        nonlocal token
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                token = app("cat", "files/.auth-token").decode().strip().splitlines()[0]
                status, result = call("GET", "/api/control/status")
                if status == 200 and result.get("ok"):
                    return
            except (OSError, subprocess.SubprocessError, IndexError):
                pass
            time.sleep(0.5)
        progress("FAIL", error="APK runtime readiness timeout")
        raise AssertionError("APK runtime readiness timeout")

    def processes() -> list[dict[str, Any]]:
        rows = []
        for line in shell("ps", "-A", "-n", "-w", "-o", "UID,PID,PPID,ARGS").splitlines()[1:]:
            columns = line.strip().split(None, 3)
            if len(columns) == 4 and columns[0].isdigit() and int(columns[0]) == uid:
                rows.append({"uid": uid, "pid": int(columns[1]), "parentPid": int(columns[2]), "command": columns[3]})
        return rows

    def native_children() -> dict[str, dict[str, Any]]:
        children = {}
        for row in processes():
            for kind, binary in (("rust", "libdeepseek_gateway.so"), ("go", "libdeepseek_control.so")):
                # Android's process listing may report only the executable's
                # basename. Verify the actual executable and UID before killing.
                if PurePosixPath(row["command"].split()[0]).name == binary:
                    executable = app("readlink", f"/proc/{row['pid']}/exe").decode().strip()
                    assert executable.startswith(code_dir + "/") and executable.endswith("/" + binary)
                    status = app("cat", f"/proc/{row['pid']}/status").decode()
                    uid_match = re.search(r"^Uid:\s+(\d+)", status, re.M)
                    assert uid_match is not None and int(uid_match[1]) == uid
                    children[kind] = row
        return children

    installed_hash = shell("sha256sum", code_dir + "/base.apk").split()[0]
    record("installed-apk-hash-bound", installed_hash == package_report["apkSha256"], installedSha256=installed_hash)
    record("emulator-abi", shell("getprop", "ro.product.cpu.abi") == "x86_64")
    command("forward", f"tcp:{args.port}", "tcp:8000")
    shell("am", "force-stop", PACKAGE)
    shell("am", "start", "-W", "-n", PACKAGE + "/com.deepseek.mobile.MainActivity")
    ready()
    initial_token = token
    children = native_children()
    record("app-uid-native-children", set(children) == {"rust", "go"}, children=children, uid=uid)
    for kind, child in children.items():
        executable = app("readlink", f"/proc/{child['pid']}/exe").decode().strip()
        digest = app("sha256sum", executable).decode().split()[0]
        record(kind + "-packaged-binary-hash", digest == package_report["binaries"]["x86_64"][kind]["sha256"], sha256=digest)
    status, result = call("POST", "/api/skills", {"action": "list"}, authorized=False)
    record("unauthenticated-denied", status == 401)
    skill = {"skillId": "apk-host-recovery", "name": "APK Host Recovery", "description": "Isolated emulator fixture", "version": "1.0",
             "systemPrompt": "Explain topic", "inputSchema": {"type": "object", "required": ["topic"]},
             "outputSchema": {"type": "object", "required": ["content"]}, "allowedTools": [], "memoryPolicy": {"scope": "none"},
             "artifactPolicy": {"types": ["md"], "autoSave": True}, "projectBinding": {"enabled": False}}
    status, result = call("POST", "/api/skills", {"action": "create", "skill": skill, "overwrite": True})
    record("native-skill-create", status == 200 and result.get("ok"))
    status, run = call("POST", "/api/skills/apk-host-recovery/run", {"offline": True, "input": {"topic": "APK kill recovery", "apiKey": "fixture-secret"}})
    record("native-run-and-artifact", status == 200 and run.get("status") == "completed" and len(run.get("artifacts", [])) == 1)
    run_id, trace_id = run["skillRunId"], run["traceId"]
    record("credential-summary-redacted", "fixture-secret" not in run["analytics"]["inputSummary"])
    url = run["artifacts"][0]["downloadUrl"]
    status, markdown = call("GET", url)
    record("artifact-download", status == 200 and b"APK kill recovery" in markdown, sha256=hashlib.sha256(markdown).hexdigest())

    def retained(name: str) -> None:
        status, stored = call("POST", "/api/skills", {"action": "get_run", "runId": run_id})
        record(name, status == 200 and stored["skillRun"]["status"] == "completed" and token == initial_token)
        status, content = call("GET", url)
        record(name + "-artifact", status == 200 and content == markdown)

    for kind in ("rust", "go"):
        before = native_children()
        assert set(before) == {"rust", "go"}
        started = time.monotonic()
        app("kill", "-9", str(before[kind]["pid"]))
        time.sleep(1)
        ready()
        after = native_children()
        record(kind + "-strong-kill-pair-recovery", set(after) == {"rust", "go"}
               and all(after[key]["pid"] != before[key]["pid"] for key in before), before=before, after=after,
               recoverySeconds=round(time.monotonic() - started, 3))
        retained(kind + "-kill-persistence")

    parents = [row for row in processes() if row["command"] == PACKAGE]
    assert len(parents) == 1, parents
    app("kill", "-9", str(parents[0]["pid"]))
    time.sleep(2)
    record("parent-death-no-orphans", not native_children())
    shell("am", "start", "-W", "-n", PACKAGE + "/com.deepseek.mobile.MainActivity")
    ready()
    retained("parent-kill-relaunch-persistence")
    snapshot = processes()
    record("no-python-app-processes", not any("python" in row["command"].lower() for row in snapshot), processes=snapshot)
    time.sleep(3)
    args.screenshot.parent.mkdir(parents=True, exist_ok=True)
    args.screenshot.write_bytes(command("exec-out", "screencap", "-p"))
    ui = shell("uiautomator", "dump", "/data/local/tmp/deepseek-native-ui.xml")
    ui_xml = shell("cat", "/data/local/tmp/deepseek-native-ui.xml")
    record("webview-visible", 'class="android.webkit.WebView"' in ui_xml and PACKAGE in ui_xml, screenshotSha256=hashlib.sha256(args.screenshot.read_bytes()).hexdigest())
    args.screenshot.with_suffix(".xml").write_text(ui_xml, encoding="utf-8")
    shell("am", "force-stop", PACKAGE)
    record("force-stop-no-native-children", not native_children())
    with tempfile.TemporaryDirectory(prefix="deepseek-apk-trace-") as temporary:
        trace_copy = Path(temporary) / "traces.sqlite3"
        trace_files = app("ls", "files/.traces").decode().splitlines()
        for filename in ("traces.sqlite3", "traces.sqlite3-wal", "traces.sqlite3-shm"):
            if filename in trace_files:
                (Path(temporary) / filename).write_bytes(app("cat", "files/.traces/" + filename))
        with closing(sqlite3.connect(trace_copy.as_uri() + "?mode=ro", uri=True)) as database:
            record("durable-trace-completed", database.execute("SELECT status FROM trace_runs WHERE trace_id=?", (trace_id,)).fetchone() == ("completed",))
            record("trace-integrity", database.execute("PRAGMA integrity_check").fetchone() == ("ok",))
    shell("am", "start", "-W", "-n", PACKAGE + "/com.deepseek.mobile.MainActivity")
    ready()
    retained("force-stop-relaunch-persistence")
    report = {"status": "PASS", "checkedAt": datetime.now(timezone.utc).isoformat(), "releaseQualified": False,
              "serial": args.serial, "api": shell("getprop", "ro.build.version.sdk"), "package": PACKAGE, "appUid": uid,
              "apkSha256": package_report["apkSha256"], "sourceIdentity": package_report["sourceIdentity"],
              "rustTarget": "x86_64-linux-android", "goTarget": "linux/amd64 static compatibility", "cgoEnabled": False,
              "goProductionAuthority": False, "successfulBusinessRuns": 1, "runId": run_id, "traceId": trace_id,
              "checks": checks, "pythonProcesses": [row for row in snapshot if "python" in row["command"].lower()],
              "screenshot": args.screenshot.as_posix(), "uiDumpStatus": ui}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": len(checks), "appUid": uid, "pythonProcesses": report["pythonProcesses"], "releaseQualified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
