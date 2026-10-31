"""Real PROCESS SIGKILL/reconciliation at processing and final library publications.

The external tracer delays only the real post-rename file-sync return. It never
substitutes application results or edits a running runtime's databases. Original
Python runs only as an isolated offline fixture/oracle before native ownership.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
from typing import Any

import verify_native_media_create as create
import verify_native_media_processing as processing
import verify_native_media_recovery as recovery

ROOT = Path(__file__).resolve().parents[1]
PHASES = ("processing", "final")
KINDS = ("webpage", "image", "screenshot", "pdf", "audio", "video")
BOUND = 4 * 1024**2
require = recovery.require
digest = recovery.digest


def held_phase(raw: bytes, journal: dict[str, Any], control: dict[str, Any], media_id: str, phase: str) -> bool:
    """Require a real, bound publication; an old library is not completion evidence."""
    require(phase in PHASES, "MEDIA_PROCESS_CRASH_PHASE_INVALID")
    require(len(raw) <= BOUND, "MEDIA_PROCESS_CRASH_LIBRARY_BOUND")
    library = json.loads(raw)
    require(isinstance(library, dict) and isinstance(library.get("media"), list), "MEDIA_PROCESS_CRASH_LIBRARY_INVALID")
    records = library["media"]
    require(all(isinstance(row, dict) for row in records), "MEDIA_PROCESS_CRASH_RECORD_INVALID")
    matches = [row for row in records if row.get("mediaId") == media_id]
    require(len(matches) == 1, "MEDIA_PROCESS_CRASH_TARGET_AMBIGUOUS")
    if (phase == "processing" and matches[0].get("status") != "processing") or (
        phase == "final" and digest(raw) != journal.get("afterSha256")
    ):
        return False
    require(journal.get("state") == "APPLYING" and control.get("state") == "DISPATCHED", "MEDIA_PROCESS_CRASH_TERMINAL_PRECEDED_HOLD")
    operation = journal.get("operationId")
    require(isinstance(operation, str) and re.fullmatch(r"media-process-[0-9a-f]{32}", operation), "MEDIA_PROCESS_CRASH_OPERATION_INVALID")
    marker = library.get("nativePublication")
    require(isinstance(marker, dict), "MEDIA_PROCESS_CRASH_PUBLICATION_MARKER_MISSING")
    require(operation == control.get("operationId") == control.get("actionId") == marker.get("operationId") == marker.get("actionId"),
            "MEDIA_PROCESS_CRASH_OPERATION_REBOUND")
    epoch, encoded = marker.get("executionEpoch"), marker.get("requestSha256")
    require(type(epoch) is int and type(control.get("epoch")) is int and epoch == control["epoch"], "MEDIA_PROCESS_CRASH_EPOCH_REBOUND")
    require(isinstance(encoded, list) and len(encoded) == 32 and all(type(v) is int and 0 <= v <= 255 for v in encoded),
            "MEDIA_PROCESS_CRASH_DIGEST_INVALID")
    require(bytes(encoded).hex() == control.get("requestSha256"), "MEDIA_PROCESS_CRASH_REQUEST_REBOUND")
    require((phase == "processing" and digest(raw) != journal["afterSha256"]) or (
        phase == "final" and matches[0].get("status") == "ready"
    ), "MEDIA_PROCESS_CRASH_PHASE_WITNESS_DIFFERS")
    return True


def require_recovered(before_go: dict[str, Any], before_rust: dict[str, Any], after_go: dict[str, Any],
                      after_rust: dict[str, Any], writer_before: int, writer_after: int) -> None:
    require(type(writer_before) is type(writer_after) is int and writer_after > writer_before,
            "MEDIA_PROCESS_CRASH_WRITER_NOT_ADVANCED")
    require(recovery.same_original(before_go, after_go, recovery.IMMUTABLE_GO)
            and recovery.same_original(before_rust, after_rust, recovery.IMMUTABLE_RUST),
            "MEDIA_PROCESS_CRASH_ORIGINAL_REBOUND")
    require(after_go.get("state") == after_rust.get("state") == "APPLIED", "MEDIA_PROCESS_CRASH_ORIGINAL_NOT_RECONCILED")
    outcome = after_go.get("outcomeSha256")
    require(isinstance(outcome, str) and re.fullmatch(r"[0-9a-f]{64}", outcome)
            and outcome == after_rust.get("outcomeSha256"), "MEDIA_PROCESS_CRASH_SIGNED_OUTCOME_DIFFERS")


def file_witness(path: Path, profile: Path) -> dict[str, Any]:
    path = recovery.owned_file(path, profile)
    stat = path.stat()
    require(stat.st_size <= BOUND, "MEDIA_PROCESS_CRASH_FIXTURE_FILE_BOUND")
    return dict(sha256=digest(path.read_bytes()), inode=stat.st_ino, mtimeNs=stat.st_mtime_ns)


def retained_files(profile: Path, media_id: str) -> dict[str, dict[str, Any]]:
    paths = list((profile / ".media/objects").rglob("*"))
    paths.extend((profile / ".media/segments").glob("*.json"))
    require(len(paths) <= 100, "MEDIA_PROCESS_CRASH_FIXTURE_FILE_COUNT_BOUND")
    require(all(not path.is_symlink() for path in paths), "MEDIA_PROCESS_CRASH_FIXTURE_LINK_REFUSED")
    return {path.relative_to(profile).as_posix(): file_witness(path, profile) for path in paths
            if path.is_file() and path != profile / f".media/segments/{media_id}.json"}


def index_snapshot(profile: Path, backend: str) -> dict[str, Any]:
    require(backend in {"sqlite", "sqlite_vec"}, "MEDIA_PROCESS_CRASH_BACKEND_INVALID")
    path = recovery.owned_file(profile / ".local-rag/rag.sqlite3", profile)
    require(path.stat().st_size <= BOUND, "MEDIA_PROCESS_CRASH_INDEX_BOUND")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        if backend == "sqlite_vec":
            import sqlite_vec
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            require(db.execute("SELECT vec_version()").fetchone()[0] == "v0.1.9", "MEDIA_PROCESS_CRASH_VECTOR_VERSION_DIFFERS")
        rows = [dict(row) for row in db.execute("SELECT * FROM rag_items ORDER BY item_id LIMIT 1001")]
        require(0 < len(rows) <= 1000, "MEDIA_PROCESS_CRASH_INDEX_ROWS_BOUND")
        meta = dict(db.execute("SELECT key,value FROM rag_meta ORDER BY key LIMIT 101").fetchall())
        require(len(meta) <= 100, "MEDIA_PROCESS_CRASH_INDEX_META_BOUND")
        vectors = None if backend == "sqlite" else [dict(row) for row in db.execute(
            "SELECT rowid,item_id,collection,source_id,project_id,scope,embedding FROM rag_vec ORDER BY rowid LIMIT 1001")]
    if vectors is not None:
        require(len(vectors) == len(rows), "MEDIA_PROCESS_CRASH_VECTOR_COUNT_DIFFERS")
        by_id = {row["rowid"]: row for row in vectors}
        require(len(by_id) == len(vectors), "MEDIA_PROCESS_CRASH_VECTOR_ID_COLLISION")
        for row in rows:
            vector = by_id.get(row["vector_id"])
            if vector is None:
                raise create.patch.ProbeFailure("MEDIA_PROCESS_CRASH_VECTOR_MISSING")
            require(all(vector[key] == row[key] for key in ["item_id", "collection", "source_id", "project_id", "scope"]),
                    "MEDIA_PROCESS_CRASH_VECTOR_BINDING_DIFFERS")
            embedding = json.loads(row["embedding"])
            require(vector["embedding"] == struct.pack("<" + "f" * len(embedding), *embedding), "MEDIA_PROCESS_CRASH_VECTOR_BYTES_DIFFER")
        vectors = [{**row, "embedding": row["embedding"].hex()} for row in vectors]
    return dict(rows=rows, metadata=meta, vectors=vectors)


def require_unrelated_index(before: dict[str, Any], after: dict[str, Any], media_id: str) -> None:
    def unrelated(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in rows if not (row["collection"] == "media" and row["source_id"] == media_id)]
    require(unrelated(before["rows"]) == unrelated(after["rows"]), "MEDIA_PROCESS_CRASH_UNRELATED_ROWS_CHANGED")
    require(before["metadata"] == after["metadata"], "MEDIA_PROCESS_CRASH_NAMESPACE_METADATA_CHANGED")
    if before["vectors"] is not None:
        require(after["vectors"] is not None and unrelated(before["vectors"]) == unrelated(after["vectors"]),
                "MEDIA_PROCESS_CRASH_UNRELATED_VECTORS_CHANGED")


class Runtime:
    def __init__(self, binaries: Path, profile: Path, port: int, backend: str, dimensions: int, out: Path, report: dict[str, Any]):
        self.binaries, self.profile, self.port, self.out, self.report = binaries, profile, port, out, report
        self.origin = f"http://127.0.0.1:{port}"
        self.child: subprocess.Popen[bytes] | None = None
        self.identities: list[dict[str, Any]] = []
        self.env = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}}
        self.env.update(DEEPSEEK_NATIVE_BIN=str(binaries), DEEPSEEK_INFRA_ROOT=str(profile),
                        DEEPSEEK_INFRA_STATIC_DIR=str(ROOT / "static"), LOCAL_RAG_ENABLED="true",
                        LOCAL_RAG_BACKEND=backend, LOCAL_RAG_EMBEDDING_PROVIDER="hash", LOCAL_RAG_EMBEDDING_DIMENSIONS=str(dimensions))

    def launch(self, label: str) -> str:
        if sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true":
            raise create.patch.ProbeFailure("MEDIA_PROCESS_CRASH_VERIFICATION_IS_CLOUD_ONLY")
        require(self.child is None, "MEDIA_PROCESS_CRASH_DUPLICATE_LAUNCH")
        with (self.out / (label + ".log")).open("wb") as log:
            self.child = subprocess.Popen([str(self.binaries / "deepseek-launch"), "--server", "--no-prompt", "--port", str(self.port)],
                                          cwd=self.profile.parent, env=self.env, stdout=log, stderr=subprocess.STDOUT)
        create.patch.bootstrap.wait_public(self.origin, self.child)
        self.identities = [{**row, "startTicks": create.patch.reads.process_identity(row["pid"])}
                           for row in create.patch.bootstrap.process_roles(self.child.pid)]
        require(len(self.identities) == 7 and {row["executable"] for row in self.identities}
                == {"deepseek-launch", "deepseekd", "deepseek-worker", "deepseek-gateway"}, "MEDIA_PROCESS_CRASH_NATIVE_ROLES_DIFFER")
        require(all(row["startTicks"] is not None for row in self.identities), "MEDIA_PROCESS_CRASH_NATIVE_PID_MISSING")
        self.report[label + "Processes"] = list(self.identities)
        token = recovery.owned_file(self.profile / ".auth-token", self.profile).read_text().strip()
        require(re.fullmatch(r"[0-9a-f]{64}", token), "MEDIA_PROCESS_CRASH_AUTH_INVALID")
        status, value, _ = create.exchange(self.origin, "/api/control/status", "GET", token)
        require(status == 200 and value.get("ok") is True, "MEDIA_PROCESS_CRASH_CONTROL_UNHEALTHY")
        return token

    def stop(self) -> None:
        if self.child is None:
            return
        if sys.platform != "linux":
            raise create.patch.ProbeFailure("MEDIA_PROCESS_CRASH_NATIVE_SIGNAL_REQUIRES_LINUX")
        for row in self.identities:
            if create.patch.reads.process_identity(row["pid"]) == row["startTicks"] and recovery.proc_state(row["pid"]) == "T":
                recovery.signal_owned(row, signal.SIGCONT)
        create.patch.reads.stop(self.child, self.identities)
        self.child, self.identities = None, []

    def kill(self, worker: dict[str, Any]) -> None:
        if sys.platform != "linux":
            raise create.patch.ProbeFailure("MEDIA_PROCESS_CRASH_NATIVE_SIGNAL_REQUIRES_LINUX")
        if self.child is None:
            raise create.patch.ProbeFailure("MEDIA_PROCESS_CRASH_LAUNCHER_MISSING")
        peers = [row for row in self.identities if row != worker]
        for row in sorted(peers, key=lambda row: row["executable"] != "deepseek-launch"):
            recovery.signal_owned(row, signal.SIGSTOP)
        recovery.wait_for(lambda: all(recovery.proc_state(row["pid"]) == "T" for row in peers), 5, "MEDIA_PROCESS_CRASH_PEERS_NOT_STOPPED")
        for row in [worker] + sorted(peers, key=lambda row: row["executable"] == "deepseek-launch"):
            recovery.signal_owned(row, signal.SIGKILL)
        require(self.child.wait(timeout=5) == -signal.SIGKILL, "MEDIA_PROCESS_CRASH_LAUNCHER_NOT_KILLED")
        recovery.wait_for(lambda: all(recovery.exited(row) for row in self.identities), 5, "MEDIA_PROCESS_CRASH_PROCESS_SURVIVED")
        self.report["allOriginalProcessesSIGKILLed"] = True
        self.child, self.identities = None, []


def public_result(runtime: Runtime, token: str, expected: dict[str, Any], reference: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    media_id = expected["media"]["mediaId"]
    status, body, _ = create.exchange(runtime.origin, "/api/media/" + media_id, "GET", token)
    segment_status, payload, _ = create.exchange(runtime.origin, "/api/media/" + media_id + "/segments", "GET", token)
    require(status == segment_status == 200 and body.get("ok") is True, "MEDIA_PROCESS_CRASH_PUBLIC_READ_FAILED")
    media, segments = body["media"], payload["segments"]
    suffix = Path(media["path"]).suffix
    require(create.same_json(create.canonical_media(media, suffix), create.canonical_media(expected["forced"]["media"], suffix)),
            "MEDIA_PROCESS_CRASH_PUBLIC_RECORD_DIFFERS")
    indexed = processing.read_index(runtime.profile, reference["ragDatabaseRelativePath"], media)
    processing.compare_processing(media, segments, indexed, expected, True)
    return body, payload


def run_case(args: argparse.Namespace, binaries: Path, scratch: Path, oracle: Path, reference: dict[str, Any],
             expected: dict[str, Any], phase: str, report: dict[str, Any]) -> None:
    media_id = expected["media"]["mediaId"]
    out = args.out / (expected["type"] + "-" + phase)
    out.mkdir()
    profile = scratch / out.name / "profile"
    profile.parent.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    dimensions = len(json.loads(expected["indexedRows"][0]["embedding"]))
    runtime = Runtime(binaries, profile, port, args.backend, dimensions, out, report)
    tracer: subprocess.Popen[bytes] | None = None
    try:
        runtime.launch("empty-launch")
        require(create.patch.reads.media_snapshot(profile) is None, "MEDIA_PROCESS_CRASH_PROFILE_NOT_EMPTY")
        runtime.stop()
        for name in (".media", ".local-rag"):
            require(not (profile / name).exists(), "MEDIA_PROCESS_CRASH_SEED_WOULD_REPLACE_STATE")
            shutil.copytree(oracle / name, profile / name)
        original_files = retained_files(profile, media_id)
        original_index = index_snapshot(profile, args.backend)
        token = runtime.launch("mutation-launch")
        writer_before = create.patch.reads.writer(profile)[0]
        library = recovery.owned_file(profile / ".media/library.json", profile)
        before_library = file_witness(library, profile)
        other_media = [row for row in json.loads(library.read_bytes())["media"] if row["mediaId"] != media_id]
        worker = next(row for row in runtime.identities if row["executable"] == "deepseek-worker")
        with (out / "trace-stderr.log").open("wb") as log:
            command = recovery.trace_command(args.strace, library, profile, out / "publication-syscalls.log", delay_seconds=5)
            tracer = subprocess.Popen(command + ["-p", str(worker["pid"])], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        tracer_pid = tracer.pid

        def attached() -> int | None:
            tasks = list(Path(f"/proc/{worker['pid']}/task").iterdir())
            try:
                ok = all(f"TracerPid:\t{tracer_pid}" in (task / "status").read_text() for task in tasks)
            except FileNotFoundError:
                return None
            return len(tasks) if tasks and ok else None

        report["tracedWorkerThreads"] = recovery.wait_for(attached, 5, "MEDIA_PROCESS_CRASH_ATTACH_FAILED")

        def observed() -> Any:
            journal = recovery.journal_snapshot(profile, True, mutation="process")
            control = recovery.control_snapshot(profile, True)
            if journal is None or control is None or journal["state"] == "PREPARED":
                return None
            raw = library.read_bytes()
            if not held_phase(raw, journal, control, media_id, phase):
                return None
            return journal, control, raw, file_witness(library, profile)

        with ThreadPoolExecutor(max_workers=1) as pool:
            report.update(writeRequestsSent=1, writeRequestsAfterRestart=0)
            response = pool.submit(create.exchange, runtime.origin, "/api/media/" + media_id + "/process?force=true", "POST", token, {})
            before_rust, before_go, held_bytes, at_crash = recovery.wait_for(observed, 15, "MEDIA_PROCESS_CRASH_PUBLICATION_NOT_HELD")
            require(not response.done(), "MEDIA_PROCESS_CRASH_RESPONSE_PRECEDED_KILL")
            require(at_crash["inode"] != before_library["inode"], "MEDIA_PROCESS_CRASH_REAL_RENAME_NOT_OBSERVED")
            require(before_go["claimWriterFence"] == writer_before, "MEDIA_PROCESS_CRASH_ORIGINAL_WRITER_DIFFERS")
            require([row for row in json.loads(held_bytes)["media"] if row["mediaId"] != media_id] == other_media,
                    "MEDIA_PROCESS_CRASH_UNRELATED_MEDIA_CHANGED")
            report.update(beforeCrashGo=before_go, beforeCrashRust=before_rust, publicationAtCrash=at_crash,
                          realRenameObserved=True, heldPhase=phase, interruptedOutcome="EFFECT_UNKNOWN")
            (out / "at-crash-library.json").write_bytes(held_bytes)
            runtime.kill(worker)
            recovery.stop_trace(tracer)
            tracer = None
            try:
                status, body, headers = response.result(timeout=12)
                report.update(interruptedRequestHTTPStatus=status, interruptedBody=body,
                              interruptedOperationHeader=headers.get("x-deepseek-operation-id"))
            except Exception as error:
                report["interruptedRequestException"] = type(error).__name__
        require(retained_files(profile, media_id) == original_files, "MEDIA_PROCESS_CRASH_SOURCE_OR_FOREIGN_SEGMENTS_CHANGED")
        crashed_go = recovery.control_snapshot(profile)
        crashed_rust = recovery.journal_snapshot(profile, mutation="process")
        require(crashed_go is not None and crashed_rust is not None and crashed_go["state"] == "DISPATCHED"
                and crashed_rust["state"] == "APPLYING", "MEDIA_PROCESS_CRASH_PRETERMINAL_ORIGINAL_MISSING")
        require(recovery.same_original(before_go, crashed_go, recovery.IMMUTABLE_GO)
                and recovery.same_original(before_rust, crashed_rust, recovery.IMMUTABLE_RUST), "MEDIA_PROCESS_CRASH_IDENTITY_CHANGED_BEFORE_RESTART")
        token = runtime.launch("recovery-launch")

        def reconciled() -> Any:
            go = recovery.control_snapshot(profile)
            rust = recovery.journal_snapshot(profile, mutation="process")
            require(go is not None and rust is not None, "MEDIA_PROCESS_CRASH_ORIGINAL_DISAPPEARED")
            require(recovery.same_original(before_go, go, recovery.IMMUTABLE_GO)
                    and recovery.same_original(before_rust, rust, recovery.IMMUTABLE_RUST), "MEDIA_PROCESS_CRASH_RECONCILIATION_REBOUND")
            report.update(afterRestartGo=go, afterRestartRust=rust)
            return (go, rust) if go["state"] == rust["state"] == "APPLIED" else None

        after_go, after_rust = recovery.wait_for(reconciled, 25, "MEDIA_PROCESS_CRASH_ORIGINAL_NOT_AUTOMATICALLY_RECONCILED")
        writer_after = create.patch.reads.writer(profile)[0]
        require_recovered(before_go, before_rust, after_go, after_rust, writer_before, writer_after)
        require(digest(library.read_bytes()) == before_rust["afterSha256"], "MEDIA_PROCESS_CRASH_FINAL_LIBRARY_NOT_ORIGINAL_PLAN")
        if phase == "final":
            recovery.require_same_publication(at_crash, file_witness(library, profile))
        final_public = public_result(runtime, token, expected, reference)
        final_index = index_snapshot(profile, args.backend)
        require_unrelated_index(original_index, final_index, media_id)
        require(retained_files(profile, media_id) == original_files, "MEDIA_PROCESS_CRASH_RECOVERY_RECREATED_SOURCE")
        final_library = file_witness(library, profile)
        final_segments = file_witness(profile / f".media/segments/{media_id}.json", profile)
        (out / "after-recovery-library.json").write_bytes(library.read_bytes())
        (out / "index-after-recovery.json").write_text(json.dumps(final_index, ensure_ascii=True, indent=2) + "\n", encoding="utf8")
        runtime.stop()
        token = runtime.launch("retained-launch")
        retained_go, retained_rust = recovery.control_snapshot(profile), recovery.journal_snapshot(profile, mutation="process")
        require(retained_go is not None and retained_rust is not None, "MEDIA_PROCESS_CRASH_RETAINED_ORIGINAL_MISSING")
        require_recovered(before_go, before_rust, retained_go, retained_rust, writer_before, create.patch.reads.writer(profile)[0])
        require(retained_go == after_go and retained_rust == after_rust, "MEDIA_PROCESS_CRASH_RETAINED_RECEIPT_CHANGED")
        require(public_result(runtime, token, expected, reference) == final_public, "MEDIA_PROCESS_CRASH_RETAINED_PUBLIC_READ_CHANGED")
        require(index_snapshot(profile, args.backend) == final_index, "MEDIA_PROCESS_CRASH_RESTART_REINDEXED")
        require(file_witness(library, profile) == final_library
                and file_witness(profile / f".media/segments/{media_id}.json", profile) == final_segments,
                "MEDIA_PROCESS_CRASH_RESTART_REPUBLISHED_FILES")
        require(retained_files(profile, media_id) == original_files, "MEDIA_PROCESS_CRASH_RESTART_SOURCE_CHANGED")
        report.update(status="PASS", semanticStageReached=True, originalOperationId=before_go["operationId"],
                      writerBefore=writer_before, writerAfter=writer_after, originalSignedOutcomeRetained=True,
                      originalOperationRepeated=False, originalSourceIdentityRetained=True, unrelatedMediaAndIndexRetained=True,
                      actualVectorBytesAndBindingsVerified=args.backend == "sqlite_vec", originalNamespaceMetadataRetained=True,
                      independentPublicReadsAndOriginalOracleVerified=True, retainedAcrossAnotherCompleteRestart=True,
                      publicationAfterRecovery=final_library, segmentsAfterRecovery=final_segments)
    except Exception as error:
        report.update(failureCode=str(error)[:300], exceptionType=type(error).__name__)
        raise
    finally:
        try:
            if tracer is not None:
                recovery.stop_trace(tracer)
        finally:
            try:
                runtime.stop()
            finally:
                (out / "result.json").write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf8")


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    require(sys.platform == "linux" and os.environ.get("GITHUB_ACTIONS") == "true", "MEDIA_PROCESS_CRASH_VERIFICATION_IS_CLOUD_ONLY")
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    require(source == os.environ.get("GITHUB_SHA"), "MEDIA_PROCESS_CRASH_SOURCE_DIFFERS")
    require(not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT), "MEDIA_PROCESS_CRASH_SOURCE_DIRTY")
    require(args.strace.is_file(), "MEDIA_PROCESS_CRASH_STRACE_REQUIRED")
    report.update(sourceCommit=source, sourceTree=subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=ROOT, text=True).strip(),
                  straceVersion=subprocess.check_output([str(args.strace), "--version"], text=True).splitlines()[0],
                  straceSha256=digest(args.strace.read_bytes()))
    for port in (8090, 50052):
        recovery.require_port_available(port)
    with tempfile.TemporaryDirectory(prefix="native-media-process-crash-") as temporary:
        scratch = Path(temporary)
        report["traceQualification"] = recovery.qualify_trace(args.strace, scratch, args.out, delay_seconds=5)
        binaries, oracle = scratch / "bin", scratch / "oracle"
        binaries.mkdir()
        (scratch / "go-tmp").mkdir()
        environment = create.patch.signing.cache_environment(ROOT, env=create.patch.signing.child_environment(dict(os.environ)), target_dir=args.go_cache)
        environment.update(CGO_ENABLED="0", GOTOOLCHAIN="local", GOMAXPROCS="2", GOPROXY="off", GOSUMDB="off",
                           GOWORK="off", GOCACHE=str(args.go_cache.resolve()), GOTMPDIR=str(scratch / "go-tmp"))
        require(subprocess.check_output(["go", "version"], env=environment, text=True).startswith(
            "go version " + create.patch.signing.GO_VERSION + " "), "MEDIA_PROCESS_CRASH_GO_PIN_DIFFERS")
        for name in ("deepseekd", "deepseek-launch"):
            create.patch.signing.record_command(["go", "build", "-p=2", "-o", str(binaries / name), "./cmd/" + name],
                                                ROOT / "go", environment, args.out, "build-" + name, timeout=600)
        for name, original in (("deepseek-worker", args.worker), ("deepseek-gateway", args.edge)):
            (binaries / name).symlink_to(original.resolve(strict=True))
        reference = processing.original(oracle, args.out / "original-processing.log", args.backend)
        (args.out / "original-processing.json").write_text(json.dumps(reference, ensure_ascii=True, indent=2) + "\n", encoding="utf8")
        cases = {row["type"]: row for row in reference["cases"]}
        require(set(cases) == set(KINDS), "MEDIA_PROCESS_CRASH_ORIGINAL_KINDS_MISSING")
        for kind in KINDS:
            for phase in PHASES:
                case = dict(status="FAIL", mediaKind=kind, phase=phase, backend=args.backend,
                            sourceCommit=source, runId=os.environ.get("GITHUB_RUN_ID"), fullMigrationQualified=False)
                report["cases"].append(case)
                run_case(args, binaries, scratch, oracle, reference, cases[kind], phase, case)
        require(len(report["cases"]) == 12 and all(case["status"] == "PASS" for case in report["cases"]),
                "MEDIA_PROCESS_CRASH_CASES_INCOMPLETE")
        report.update(status="PASS", actualProcessingAndFinalPublicationSIGKILLQualified=True,
                      originalOperationAutonomousRecoveryQualified=True, actualNativeBackendQualified=args.backend,
                      nativeSQLiteVecStandaloneForceRecoveryHTTPQualified=args.backend == "sqlite_vec", completedCases=12)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--edge", type=Path, required=True)
    parser.add_argument("--strace", type=Path, required=True)
    parser.add_argument("--go-cache", type=Path, required=True)
    parser.add_argument("--backend", choices=("sqlite", "sqlite_vec"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = dict(kind="native-media-process-crash-v1", status="FAIL", cases=[], backend=args.backend,
                                 sourceCommit=os.environ.get("GITHUB_SHA"), runId=os.environ.get("GITHUB_RUN_ID"),
                                 event=os.environ.get("GITHUB_EVENT_NAME"), attempt=os.environ.get("GITHUB_RUN_ATTEMPT"),
                                 actualProcessingAndFinalPublicationSIGKILLQualified=False, originalOperationAutonomousRecoveryQualified=False,
                                 nativeSQLiteVecStandaloneForceRecoveryHTTPQualified=False, allProcessingCrashPhasesQualified=False,
                                 scope="Six standalone force/read/recovery kinds on one backend at two real library-publication boundaries only",
                                 physicalPowerLossQualified=False, fullProcessingPipelineQualified=False, fullMigrationQualified=False,
                                 readiness="NOT_READY", checkedAt=datetime.now(timezone.utc).isoformat())
    try:
        run(args, report)
    except Exception as error:
        report.update(failureCode=str(error)[:300], exceptionType=type(error).__name__)
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=True, indent=2) + "\n", encoding="utf8")
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
