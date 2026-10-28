"""Cloud-only real media publication crash, restart and original-operation recovery.

strace delays the file-sync return after real target-file replacement. It never substitutes a
return value or edits application databases. A separate fixture first proves
that the rename executes, unrelated paths remain unheld, and userspace is held.
"""
from __future__ import annotations

import argparse
import errno
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable

import verify_native_media_patch as patch

ROOT = Path(__file__).resolve().parents[1]
IMMUTABLE_GO = ("operationId", "actionId", "epoch", "claimWriterFence", "intentSha256",
                "requestSha256", "claimTokenSha256", "ownerSha256")
IMMUTABLE_RUST = ("operationId", "claimSha256", "preparationSha256", "beforeSha256",
                  "afterSha256", "plannedSha256")


def require(value: Any, code: str) -> None:
    patch.require(value, code)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wait_for(condition: Callable[[], Any], timeout: float, code: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value is not None and value is not False:
            return value
        time.sleep(0.05)
    raise patch.ProbeFailure(code)


def require_port_available(port: int) -> None:
    with socket.socket() as listener:
        # A stopped previous case can leave TIME_WAIT sockets. Reuse their local
        # address while still refusing a live TCP listener; never set REUSEPORT.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))


def qualify_port_preflight() -> dict[str, Any]:
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        server.settimeout(2)
        port = server.getsockname()[1]
        try:
            require_port_available(port)
        except OSError as error:
            require(error.errno == errno.EADDRINUSE, "MEDIA_PREFLIGHT_LISTENER_ERRNO_DIFFERS")
        else:
            raise patch.ProbeFailure("MEDIA_PREFLIGHT_ACCEPTED_ACTIVE_LISTENER")
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            connection, _ = server.accept()
            with connection:
                connection.shutdown(socket.SHUT_RDWR)
            require(client.recv(1) == b"", "MEDIA_PREFLIGHT_CONNECTION_NOT_CLOSED")
    def retained_time_wait() -> bool:
        for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
            fields = line.split()
            if fields[1].endswith(f":{port:04X}") and fields[3] == "06":
                return True
        return False
    wait_for(retained_time_wait, 2, "MEDIA_PREFLIGHT_TIME_WAIT_NOT_OBSERVED")
    require_port_available(port)
    return dict(activeListenerDenied=True, kernelTimeWaitObserved=True, timeWaitAddressReusable=True,
                reusePortEnabled=False)


def owned_file(path: Path, root: Path) -> Path:
    root, path = root.absolute(), path.absolute()
    require(path.is_relative_to(root) and ".." not in path.relative_to(root).parts,
            "MEDIA_OBSERVER_PATH_ESCAPED")
    require(not root.is_symlink(), "MEDIA_OBSERVER_LINK_REFUSED")
    resolved = path.resolve(strict=True)
    require(resolved.is_relative_to(root.resolve(strict=True)) and resolved.is_file(), "MEDIA_OBSERVER_PATH_ESCAPED")
    current = path
    while current != root:
        require(not current.is_symlink(), "MEDIA_OBSERVER_LINK_REFUSED")
        current = current.parent
    return resolved


def readonly(path: Path, root: Path) -> sqlite3.Connection:
    return sqlite3.connect(owned_file(path, root).as_uri() + "?mode=ro", uri=True, timeout=0.25)


def journal_snapshot(root: Path, allow_empty: bool = False) -> dict[str, Any] | None:
    paths = list(root.rglob("media-effects.sqlite3"))
    require(len(paths) == 1, "MEDIA_RUST_JOURNAL_REQUIRED")
    with closing(readonly(paths[0], root)) as db:
        require(db.execute("PRAGMA application_id").fetchone()[0] == 0x44534D50, "MEDIA_RUST_JOURNAL_ID_DIFFERS")
        rows = db.execute("SELECT operation_id,state,claim,preparation_sha256,before_bytes,after_bytes,planned,outcome FROM media_operations LIMIT 2").fetchall()
    if not rows and allow_empty:
        return None
    require(len(rows) == 1, "MEDIA_ORIGINAL_RUST_ROW_REQUIRED")
    operation, state, claim, preparation, before, after, planned, outcome = rows[0]
    require(isinstance(operation, str) and re.fullmatch("media-patch-[0-9a-f]{32}", operation), "MEDIA_OPERATION_ID_INVALID")
    require(state in {"PREPARED", "APPLYING", "APPLIED", "REFUSED"}, "MEDIA_RUST_STATE_INVALID")
    require(isinstance(claim, bytes) and 0 < len(claim) <= 4 * 1024**2, "MEDIA_CLAIM_BOUND_INVALID")
    require(isinstance(preparation, bytes) and len(preparation) == 32, "MEDIA_PREPARATION_INVALID")
    require(isinstance(before, bytes) and isinstance(after, bytes) and isinstance(planned, bytes), "MEDIA_WITNESS_MISSING")
    require(max(len(before), len(after), len(planned)) <= 1_000_000, "MEDIA_CONTROLLED_WITNESS_TOO_LARGE")
    return dict(operationId=operation, state=state, claimSha256=digest(claim),
                preparationSha256=preparation.hex(), beforeSha256=digest(before),
                afterSha256=digest(after), plannedSha256=digest(planned),
                outcomeSha256=None if outcome is None else digest(outcome), rowCount=1)


def control_snapshot(root: Path, allow_empty: bool = False) -> dict[str, Any] | None:
    paths = list(root.rglob("control.sqlite3"))
    require(len(paths) == 1, "MEDIA_GO_CONTROL_REQUIRED")
    with closing(readonly(paths[0], root)) as db:
        meta = db.execute("SELECT runtime,unique_writer,schema_version FROM control_store_meta").fetchall()
        require(len(meta) == 1 and meta[0][0:2] == ("go", "go") and meta[0][2] >= 20, "MEDIA_GO_AUTHORITY_INVALID")
        rows = db.execute("SELECT operation_id,state,action_id,epoch,writer_fencing_token,intent,request_sha256,claim_token,owner,outcome FROM control_media_operations LIMIT 2").fetchall()
    if not rows and allow_empty:
        return None
    require(len(rows) == 1, "MEDIA_ORIGINAL_GO_ROW_REQUIRED")
    operation, state, action, epoch, fence, intent, request, token, owner, outcome = rows[0]
    require(operation == action and epoch == 1 and fence > 0, "MEDIA_ORIGINAL_FENCE_DIFFERS")
    require(isinstance(intent, bytes) and 0 < len(intent) <= 4 * 1024**2 and len(request) == 32, "MEDIA_ORIGINAL_INTENT_INVALID")
    require(isinstance(token, str) and isinstance(owner, str), "MEDIA_ORIGINAL_CLAIM_INVALID")
    return dict(operationId=operation, state=state, actionId=action, epoch=epoch, claimWriterFence=fence,
                intentSha256=digest(intent), requestSha256=request.hex(), claimTokenSha256=digest(token.encode()),
                ownerSha256=digest(owner.encode()), outcomeSha256=None if outcome is None else digest(outcome), rowCount=1)


def same_original(before: dict[str, Any], after: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return all(key in before and key in after and type(before[key]) is type(after[key])
               and before[key] == after[key] for key in keys)


def require_recovery_generation(before: dict[str, Any], after: dict[str, Any]) -> None:
    require(type(before["value"]) is int and type(after["value"]) is int and after["present"] is True
            and after["value"] == before["value"] + 2, "MEDIA_RECOVERY_GENERATION_NOT_FENCED")


def require_same_publication(before: dict[str, Any], after: dict[str, Any]) -> None:
    require(same_original(before, after, ("sha256", "inode", "mtimeNs")), "MEDIA_RECOVERY_PUBLICATION_WITNESS_CHANGED")


def proc_state(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    return raw[raw.rfind(")") + 2:].split()[0]


def trace_command(binary: Path, library: Path, root: Path, log: Path) -> list[str]:
    target = owned_file(library, root)
    return [str(binary), "-f", "-P", str(target), "-e", "trace=fsync,fdatasync",
            "-e", "inject=fsync,fdatasync:delay_exit=20s", "-o", str(log)]


def sync_fault_command(binary: Path, library: Path, root: Path, log: Path, kind: str) -> list[str]:
    require(kind in {"file", "directory"}, "MEDIA_SYNC_FAULT_KIND_INVALID")
    target = owned_file(library, root)
    if kind == "directory":
        target = target.parent
    return [str(binary), "-f", "-P", str(target), "-e", "trace=fsync,fdatasync",
            "-e", "inject=fsync,fdatasync:error=EIO", "-o", str(log)]


def require_sync_failure_retains_original(before_go: dict[str, Any], before_rust: dict[str, Any],
                                         after_go: dict[str, Any], after_rust: dict[str, Any]) -> None:
    require(same_original(before_go, after_go, IMMUTABLE_GO)
            and same_original(before_rust, after_rust, IMMUTABLE_RUST), "MEDIA_RECOVERY_REBOUND_ORIGINAL_OPERATION")
    require(after_go["state"] == "DISPATCHED" and after_rust["state"] == "APPLYING"
            and after_go["outcomeSha256"] is None and after_rust["outcomeSha256"] is None,
            "MEDIA_RECOVERY_SETTLED_WITHOUT_SYNC_BARRIER")


def qualify_sync_fault_trace(binary: Path, scratch: Path, out: Path, kind: str) -> dict[str, Any]:
    root = scratch / "sync-fault-qualification"
    (root / "target").mkdir(parents=True)
    library = root / "target/library.json"
    library.write_bytes(b"retained")
    code = """from pathlib import Path
import errno,os,sys
p=Path(sys.argv[1]);kind=sys.argv[2]
with (p/'unrelated').open('wb') as f:
 f.write(b'other');f.flush();os.fsync(f.fileno())
print('unrelated-sync-passed',flush=True)
fd=os.open(p/'target/library.json',os.O_RDWR) if kind=='file' else os.open(p/'target',os.O_RDONLY|os.O_DIRECTORY)
try:
 try:os.fsync(fd)
 except OSError as e:
  if e.errno!=errno.EIO:raise
  print('target-sync-eio',flush=True)
 else:raise RuntimeError('target sync did not fail')
finally:os.close(fd)
"""
    log = out / "sync-fault-qualification-syscalls.log"
    command = sync_fault_command(binary, library, root, log, kind)
    result = subprocess.run(command + [sys.executable, "-c", code, str(root), kind],
                            capture_output=True, timeout=10)
    (out / "sync-fault-qualification-output.log").write_bytes(result.stdout + result.stderr)
    require(result.returncode == 0 and result.stdout.splitlines() == [b"unrelated-sync-passed", b"target-sync-eio"],
            "MEDIA_SYNC_FAULT_FILTER_NOT_QUALIFIED")
    require("EIO" in log.read_text() and "INJECTED" in log.read_text(), "MEDIA_SYNC_EIO_NOT_OBSERVED")
    return dict(targetKind=kind, unrelatedSyncPassed=True, targetedEIOObserved=True,
                syscallErrorInjected=True, physicalPowerLossSimulated=False)


def stop_trace(child: subprocess.Popen[bytes]) -> None:
    if sys.platform != "linux":
        raise patch.ProbeFailure("MEDIA_TRACE_REQUIRES_LINUX")
    if child.poll() is None:
        require(child.pid > 1 and os.getpgid(child.pid) == child.pid, "MEDIA_TRACE_GROUP_NOT_OWNED")
        os.killpg(child.pid, signal.SIGKILL)
    child.wait(timeout=5)


def qualify_trace(binary: Path, scratch: Path, out: Path) -> dict[str, Any]:
    root = scratch / "trace-qualification"
    root.mkdir()
    target = root / "target"
    target.write_bytes(b"before")
    code = """from pathlib import Path
import os,sys
p=Path(sys.argv[1])
with (p/'other-temp').open('wb') as f:
 f.write(b'other-after'); f.flush(); os.fsync(f.fileno())
 os.replace(p/'other-temp',p/'other'); os.fsync(f.fileno())
(p/'unrelated-finished').write_bytes(b'yes')
with (p/'target-temp').open('wb') as f:
 f.write(b'after'); f.flush(); os.fsync(f.fileno())
 os.replace(p/'target-temp',p/'target'); os.fsync(f.fileno())
 (p/'returned-to-userspace').write_bytes(b'yes')
"""
    command = trace_command(binary, target, root, out / "trace-qualification.log")
    with (out / "trace-qualification-stderr.log").open("wb") as log:
        child = subprocess.Popen(command + [sys.executable, "-c", code, str(root)],
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        wait_for(lambda: (root / "unrelated-finished").exists(), 5, "STRACE_FILTER_DELAYED_UNRELATED_PATH")
        wait_for(lambda: target.read_bytes() == b"after", 5, "STRACE_DID_NOT_EXECUTE_REAL_RENAME")
        require(child.poll() is None and not (root / "returned-to-userspace").exists(), "STRACE_DID_NOT_HOLD_POST_RENAME_SYNC_EXIT")
        return dict(realRenameExecuted=True, unrelatedPathUnheld=True, userspaceReturnHeld=True, heldSyscall="post-rename file sync",
                    substitutedReturnValue=False, injectedErrno=False)
    finally:
        stop_trace(child)


def signal_owned(row: dict[str, Any], signum: int) -> None:
    if sys.platform != "linux":
        raise patch.ProbeFailure("MEDIA_NATIVE_SIGNAL_REQUIRES_LINUX")
    pid = row["pid"]
    require(pid > 1 and patch.reads.process_identity(pid) == row["startTicks"], "MEDIA_CRASH_PID_IDENTITY_CHANGED")
    os.kill(pid, signum)


def exited(row: dict[str, Any]) -> bool:
    return patch.reads.process_identity(row["pid"]) != row["startTicks"] or proc_state(row["pid"]) in {None, "Z", "X"}


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    if sys.platform != "linux":
        raise patch.ProbeFailure("MEDIA_RECOVERY_VERIFICATION_IS_CLOUD_LINUX_ONLY")
    require(os.environ.get("GITHUB_ACTIONS") == "true", "MEDIA_RECOVERY_VERIFICATION_IS_CLOUD_LINUX_ONLY")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    require(revision == os.environ.get("GITHUB_SHA"), "MEDIA_RECOVERY_SOURCE_DIFFERS")
    require(not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT), "MEDIA_RECOVERY_SOURCE_IS_DIRTY")
    require(args.strace.is_file(), "STRACE_REQUIRED")
    report["straceVersion"] = subprocess.check_output([str(args.strace), "--version"], text=True).splitlines()[0]
    if args.recovery_sync_fault != "none":
        require(args.sync_strace.is_file(), "MEDIA_SYNC_STRACE_REQUIRED")
        require(args.sync_strace.resolve() != args.strace.resolve(), "MEDIA_SYNC_TRACER_MUST_BE_UNPRIVILEGED")
        require(args.sync_strace.read_bytes() == args.strace.read_bytes(), "MEDIA_SYNC_TRACER_BINARY_DIFFERS")
        capabilities = subprocess.check_output(["getcap", str(args.sync_strace)], text=True).strip()
        require(not capabilities, "MEDIA_SYNC_TRACER_HAS_FILE_CAPABILITIES")
        report.update(syncTracerUnprivileged=True, syncTracerMatchesOfficialAttachCopy=True,
                      syncTracerSha256=digest(args.sync_strace.read_bytes()))
    fixture = json.loads(patch.FIXTURE.read_text())
    require(fixture["schemaVersion"] == 1 and len(fixture["media"]) == 18, "MEDIA_FIXTURE_DIFFERS")
    request_body = patch.wire_payload(patch.PATCH)
    report.update(requestBodyBytes=len(request_body), requestBodySha256=digest(request_body))
    report["portPreflightQualification"] = qualify_port_preflight()
    for port in (8090, 50052):
        try:
            require_port_available(port)
        except OSError as error:
            report.update(portPreflightFailedPort=port, portPreflightErrno=error.errno)
            raise patch.ProbeFailure("MEDIA_RECOVERY_PRIVATE_PORT_UNAVAILABLE") from error
    with tempfile.TemporaryDirectory(prefix="native-media-recovery-") as temporary:
        scratch = Path(temporary)
        report["traceQualification"] = qualify_trace(args.strace, scratch, args.out)
        binaries, profile, mirror = scratch / "bin", scratch / "profile", scratch / "oracle"
        binaries.mkdir()
        (scratch / "go-tmp").mkdir()
        environment = patch.signing.cache_environment(ROOT, env=patch.signing.child_environment(dict(os.environ)), target_dir=args.go_cache)
        environment.update(CGO_ENABLED="0", GOTOOLCHAIN="local", GOMAXPROCS="2", GOPROXY="off", GOSUMDB="off",
                           GOWORK="off", GOCACHE=str(args.go_cache.resolve()), GOTMPDIR=str(scratch / "go-tmp"))
        require(subprocess.check_output(["go", "version"], env=environment, text=True).startswith(
            "go version " + patch.signing.GO_VERSION + " "), "MEDIA_RECOVERY_GO_PIN_DIFFERS")
        for name in ("deepseekd", "deepseek-launch"):
            patch.signing.record_command(["go", "build", "-p=2", "-o", str(binaries / name), "./cmd/" + name],
                                         ROOT / "go", environment, args.out, "build-" + name, timeout=600)
        for name, source in (("deepseek-worker", args.worker), ("deepseek-gateway", args.edge)):
            (binaries / name).symlink_to(source.resolve(strict=True))
        require(not profile.exists() and (ROOT / "static/ui/index.html").is_file(), "MEDIA_RECOVERY_PROFILE_NOT_FRESH")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        launch_env = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}}
        launch_env.update(DEEPSEEK_NATIVE_BIN=str(binaries), DEEPSEEK_INFRA_ROOT=str(profile),
                          DEEPSEEK_INFRA_STATIC_DIR=str(ROOT / "static"))
        child: subprocess.Popen[bytes] | None = None
        identities: list[dict[str, Any]] = []
        tracer: subprocess.Popen[bytes] | None = None
        sync_traced = False

        def launch(label: str, sync_fault: str = "none") -> str:
            nonlocal child, identities, sync_traced
            command = [str(binaries / "deepseek-launch"), "--server", "--no-prompt", "--port", str(port)]
            sync_traced = sync_fault != "none"
            if sync_traced:
                command = sync_fault_command(args.sync_strace, profile / ".media/library.json", profile,
                                             args.out / "recovery-sync-syscalls.log", sync_fault) + command
            with (args.out / (label + ".log")).open("wb") as log:
                child = subprocess.Popen(command, cwd=scratch, env=launch_env, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=sync_traced)
            patch.bootstrap.wait_public(origin, child)
            identities = [{**row, "startTicks": patch.reads.process_identity(row["pid"])} for row in patch.bootstrap.process_roles(child.pid)]
            if sync_traced:
                wrappers = [row for row in identities if row["pid"] == child.pid]
                require(len(wrappers) == 1 and wrappers[0]["executable"] == args.sync_strace.name,
                        "MEDIA_SYNC_TRACER_IDENTITY_DIFFERS")
                report["syncFaultTracerProcess"] = wrappers[0]
                identities = [row for row in identities if row["pid"] != child.pid]
            require({row["executable"] for row in identities} == {"deepseek-launch", "deepseekd", "deepseek-worker", "deepseek-gateway"}, "MEDIA_RECOVERY_NATIVE_ROLES_DIFFER")
            require(all(row["startTicks"] is not None for row in identities), "MEDIA_RECOVERY_NATIVE_PROCESS_DISAPPEARED")
            report[label + "Processes"] = identities
            auth = owned_file(profile / ".auth-token", profile).read_text().strip()
            require(re.fullmatch("[0-9a-f]{64}", auth), "MEDIA_RECOVERY_AUTH_INVALID")
            status, raw, _ = patch.exchange(origin, "/api/control/status", "GET", auth)
            require(status == 200 and json.loads(raw).get("ok") is True, "MEDIA_RECOVERY_CONTROL_UNHEALTHY")
            return auth

        def shutdown() -> None:
            nonlocal child, identities, sync_traced
            if child is not None:
                if sync_traced:
                    if child.poll() is None and not any(row["executable"] == "deepseek-launch" for row in identities):
                        observed = patch.bootstrap.process_roles(child.pid, required=set())
                        identities = [{**row, "startTicks": patch.reads.process_identity(row["pid"])}
                                      for row in observed if row["pid"] != child.pid]
                    launchers = [row for row in identities if row["executable"] == "deepseek-launch"]
                    if child.poll() is None and len(launchers) == 1:
                        signal_owned(launchers[0], signal.SIGTERM)
                    deadline = time.monotonic() + 5
                    while not all(exited(row) for row in identities) and time.monotonic() < deadline:
                        time.sleep(0.05)
                    # Keep the tracer alive while draining tracees. Killing the
                    # tracer first can detach native children in separate groups.
                    forced = [row for row in identities if not exited(row)]
                    for row in forced:
                        signal_owned(row, signal.SIGKILL)
                    wait_for(lambda: all(exited(row) for row in identities), 5, "MEDIA_SYNC_NATIVE_CHILD_SURVIVED")
                    if child.poll() is None:
                        try:
                            child.wait(timeout=5)
                        finally:
                            if child.poll() is None:
                                stop_trace(child)
                    report.setdefault("syncFaultCleanups", []).append(dict(
                        nativeRoles=len(identities), forcedPIDs=[row["pid"] for row in forced], allNativeProcessesExited=True))
                else:
                    patch.reads.stop(child, identities)
                child, identities = None, []
                sync_traced = False

        try:
            launch("empty-launch")
            require(patch.reads.media_snapshot(profile) is None, "MEDIA_RECOVERY_FRESH_PROFILE_HAS_MEDIA")
            shutdown()
            patch.seed(profile, fixture["media"])
            patch.seed(mirror, fixture["media"])
            expected = patch.original_patch(mirror)
            report["offlineMediaFixtureSeeded"] = True
            (args.out / "original-patch-oracle.json").write_bytes((json.dumps(expected, ensure_ascii=False, indent=2) + "\n").encode())
            token = launch("mutation-launch")
            writer_before = patch.reads.writer(profile)
            worker = next(row for row in identities if row["executable"] == "deepseek-worker")
            library = owned_file(profile / ".media/library.json", profile)
            with (args.out / "publication-trace-stderr.log").open("wb") as log:
                tracer = subprocess.Popen(trace_command(args.strace, library, profile, args.out / "publication-syscalls.log") + ["-p", str(worker["pid"])],
                                          stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            tracer_pid = tracer.pid

            def all_threads_attached() -> int | None:
                tasks = list(Path(f"/proc/{worker['pid']}/task").iterdir())
                try:
                    attached = all(f"TracerPid:\t{tracer_pid}" in (task / "status").read_text() for task in tasks)
                except FileNotFoundError:
                    return None
                return len(tasks) if tasks and attached else None

            report["tracedWorkerThreads"] = wait_for(all_threads_attached, 5, "MEDIA_STRACE_ATTACH_FAILED")

            def held_publication() -> Any:
                journal = journal_snapshot(scratch, True)
                if journal is None or journal["state"] == "PREPARED":
                    return None
                require(journal["state"] == "APPLYING", "MEDIA_PUBLICATION_BOUNDARY_NOT_HELD")
                raw = library.read_bytes()
                if digest(raw) != journal["afterSha256"]:
                    return None
                control = control_snapshot(scratch)
                if control is None:
                    raise patch.ProbeFailure("MEDIA_ORIGINAL_GO_ROW_REQUIRED")
                require(control["state"] == "DISPATCHED", "MEDIA_DISPATCH_NOT_DURABLE")
                require(control["claimWriterFence"] == writer_before[0], "MEDIA_ORIGINAL_WRITER_FENCE_DIFFERS")
                marker = json.loads(raw)["nativePublication"]
                require(marker["operationId"] == marker["actionId"] == control["operationId"] == journal["operationId"], "MEDIA_PUBLICATION_OPERATION_DIFFERS")
                require(type(marker["executionEpoch"]) is int and marker["executionEpoch"] == control["epoch"]
                        and bytes(marker["requestSha256"]).hex() == control["requestSha256"], "MEDIA_PUBLICATION_FENCE_DIFFERS")
                return journal, control, raw, library.stat()

            with ThreadPoolExecutor(max_workers=1) as pool:
                report["writeRequestsSent"] = 1
                response = pool.submit(patch.exchange, origin, "/api/media/" + patch.MEDIA_ID, "PATCH", token, patch.PATCH)
                before_rust, before_go, raw_after, stat_after = wait_for(held_publication, 8, "MEDIA_REAL_PUBLICATION_NOT_OBSERVED")
                require(not response.done(), "MEDIA_RESPONSE_RETURNED_BEFORE_CRASH")
                report.update(publicationHeldAfterRealRename=True, beforeCrashRust=before_rust, beforeCrashGo=before_go,
                              generationAtCrash=patch.generation_observation(profile))
                report["publicationAtCrash"] = dict(sha256=digest(raw_after), inode=stat_after.st_ino, mtimeNs=stat_after.st_mtime_ns)
                (args.out / "after-crash-library.json").write_bytes(raw_after)
                nonworkers = [row for row in identities if row != worker]
                for row in sorted(nonworkers, key=lambda row: row["executable"] != "deepseek-launch"):
                    signal_owned(row, signal.SIGSTOP)
                wait_for(lambda: all(proc_state(row["pid"]) == "T" for row in nonworkers), 5, "MEDIA_CRASH_PEERS_NOT_STOPPED")
                for row in [worker] + sorted(nonworkers, key=lambda row: row["executable"] == "deepseek-launch"):
                    signal_owned(row, signal.SIGKILL)
                require(child is not None and child.wait(timeout=5) == -signal.SIGKILL, "MEDIA_LAUNCHER_NOT_SIGKILLED")
                wait_for(lambda: all(exited(row) for row in identities), 5, "MEDIA_ORIGINAL_PROCESS_STILL_EXECUTING")
                report["allOriginalProcessesExited"] = True
                child, identities = None, []
                stop_trace(tracer)
                tracer = None
                try:
                    status, raw, _ = response.result(timeout=12)
                    report["interruptedRequestHTTPStatus"] = status
                    report["interruptedResponseSha256"] = digest(raw)
                except Exception as error:
                    report["interruptedRequestException"] = type(error).__name__
            crashed_go, crashed_rust = control_snapshot(scratch), journal_snapshot(scratch)
            if crashed_go is None or crashed_rust is None:
                raise patch.ProbeFailure("MEDIA_ORIGINAL_RECOVERY_ROW_MISSING")
            require(crashed_go["state"] == "DISPATCHED" and crashed_rust["state"] == "APPLYING", "MEDIA_TERMINAL_RESULT_PRECEDED_RESTART")
            report["interruptedOutcome"] = "EFFECT_UNKNOWN"
            if args.recovery_sync_fault != "none":
                report["syncFaultQualification"] = qualify_sync_fault_trace(args.sync_strace, scratch, args.out, args.recovery_sync_fault)
                report["semanticStageReached"] = True
                launch("sync-fault-launch", args.recovery_sync_fault)
                deadline = time.monotonic() + 4
                observations = 0
                while time.monotonic() < deadline:
                    fault_go, fault_rust = control_snapshot(scratch), journal_snapshot(scratch)
                    if fault_go is None or fault_rust is None:
                        raise patch.ProbeFailure("MEDIA_ORIGINAL_RECOVERY_ROW_MISSING")
                    report.update(duringSyncFaultGo=fault_go, duringSyncFaultRust=fault_rust)
                    require_sync_failure_retains_original(before_go, before_rust, fault_go, fault_rust)
                    observed = dict(sha256=digest(library.read_bytes()), inode=library.stat().st_ino,
                                    mtimeNs=library.stat().st_mtime_ns)
                    require_same_publication(report["publicationAtCrash"], observed)
                    observations += 1
                    time.sleep(0.1)
                trace = (args.out / "recovery-sync-syscalls.log").read_text()
                injected = sum("EIO" in line and "INJECTED" in line for line in trace.splitlines())
                require(injected >= 2, "MEDIA_RECOVERY_DID_NOT_RETRY_SYNC_BARRIER")
                report.update(syncFailureKeptOriginalUnresolved=True, syncFailureObservations=observations,
                              injectedRecoverySyncErrors=injected, publicationUnchangedDuringSyncFailure=True)
                shutdown()
            token = launch("recovery-launch")
            writer_after = patch.reads.writer(profile)
            require(writer_after[0] > writer_before[0], "MEDIA_RESTART_WRITER_FENCE_NOT_ADVANCED")
            report.update(semanticStageReached=True, originalOperationRepeated=False, writeRequestsAfterRestart=0,
                          writerBefore=writer_before[0], writerAfter=writer_after[0])
            deadline = time.monotonic() + args.reconcile_timeout
            after_go, after_rust = crashed_go, crashed_rust
            while time.monotonic() < deadline:
                observed_go, observed_rust = control_snapshot(scratch), journal_snapshot(scratch)
                if observed_go is None or observed_rust is None:
                    raise patch.ProbeFailure("MEDIA_ORIGINAL_RECOVERY_ROW_MISSING")
                after_go, after_rust = observed_go, observed_rust
                require(same_original(before_go, after_go, IMMUTABLE_GO) and same_original(before_rust, after_rust, IMMUTABLE_RUST), "MEDIA_RECOVERY_REBOUND_ORIGINAL_OPERATION")
                if after_go["state"] == after_rust["state"] == "APPLIED":
                    break
                time.sleep(0.1)
            report.update(afterRestartGo=after_go, afterRestartRust=after_rust,
                          generationAfterRestart=patch.generation_observation(profile))
            final_bytes, final_stat = library.read_bytes(), library.stat()
            report["publicationAfterRestart"] = dict(sha256=digest(final_bytes), inode=final_stat.st_ino, mtimeNs=final_stat.st_mtime_ns)
            require_same_publication(report["publicationAtCrash"], report["publicationAfterRestart"])
            report["postRestartPublicationWitnessRetained"] = True
            (args.out / "after-restart-library.json").write_bytes(final_bytes)
            require(after_go["state"] == after_rust["state"] == "APPLIED", "ORIGINAL_MEDIA_NOT_AUTOMATICALLY_RECONCILED")
            require_recovery_generation(report["generationAtCrash"], report["generationAfterRestart"])
            require(library.read_bytes() == raw_after and library.stat().st_ino == stat_after.st_ino and library.stat().st_mtime_ns == stat_after.st_mtime_ns, "MEDIA_RECOVERY_REPUBLISHED_BYTES")
            status, raw, _ = patch.exchange(origin, "/api/media/" + patch.MEDIA_ID, "GET", token)
            require(status == 200 and patch.without_update_time(json.loads(raw)) == patch.without_update_time(expected), "MEDIA_RECOVERED_PUBLIC_RESULT_DIFFERS")
            require(after_go["outcomeSha256"] is not None and after_go["outcomeSha256"] == after_rust["outcomeSha256"], "MEDIA_ORIGINAL_SIGNED_OUTCOME_DIFFERS")
            report.update(controlledOriginalOperationRecoveryQualified=True, libraryRepublicationObserved=False,
                          originalSignedOutcomeRetained=True, status="PASS")
        except (patch.ProbeFailure, patch.bootstrap.ProbeFailure, patch.reads.ProbeFailure) as error:
            report["failureBeforeCleanup"] = str(error)
            raise
        finally:
            if tracer is not None:
                stop_trace(tracer)
            for row in identities:
                if patch.reads.process_identity(row["pid"]) == row["startTicks"] and proc_state(row["pid"]) == "T":
                    signal_owned(row, signal.SIGCONT)
            shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--edge", type=Path, required=True)
    parser.add_argument("--go-cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--strace", type=Path, default=Path(shutil.which("strace") or "/not-installed/strace"))
    parser.add_argument("--sync-strace", type=Path, default=Path(shutil.which("strace") or "/not-installed/strace"))
    parser.add_argument("--reconcile-timeout", type=float, default=20)
    parser.add_argument("--recovery-sync-fault", choices=("none", "file", "directory"), default="none")
    args = parser.parse_args()
    require(1 <= args.reconcile_timeout <= 30, "MEDIA_RECOVERY_TIMEOUT_INVALID")
    args.out = args.out.resolve()
    require(args.out.is_relative_to((ROOT / "artifacts").resolve()), "MEDIA_RECOVERY_REPORT_PATH_ESCAPED")
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = dict(kind="native-media-recovery-process-v1", status="FAIL",
        sourceCommit=os.environ.get("GITHUB_SHA"), runId=os.environ.get("GITHUB_RUN_ID"),
        event=os.environ.get("GITHUB_EVENT_NAME"), semanticStageReached=False,
        recoverySyncFault=args.recovery_sync_fault,
        controlledOriginalOperationRecoveryQualified=False, mediaAuthorityRecoveryQualified=False,
        powerLossRecoveryQualified=False, windowsRecoveryQualified=False, fullMigrationQualified=False,
        readiness="NOT_READY", startedAt=datetime.now(timezone.utc).isoformat())
    try:
        run(args, report)
    except (patch.ProbeFailure, patch.bootstrap.ProbeFailure, patch.reads.ProbeFailure) as error:
        report.update(status="FAIL", failureCode=str(error))
    except Exception as error:
        report.update(status="FAIL", failureCode="MEDIA_RECOVERY_VERIFIER_ERROR", exceptionType=type(error).__name__)
        if isinstance(error, OSError):
            report["exceptionErrno"] = error.errno
    report["finishedAt"] = datetime.now(timezone.utc).isoformat()
    (args.out / "result.json").write_bytes((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
