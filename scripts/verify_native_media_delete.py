"""Cloud-only real launcher HTTP DELETE against the original ASGI route.

Python seeds and observes disposable offline fixtures only. The running product
processes and actual mutation are the unmodified Go/Rust production binaries.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any

import verify_native_media_patch as patch

ROOT = Path(__file__).resolve().parents[1]
TARGET = "media_keep"
OTHER = "media_other"
LIMIT = 2 * 1024**2
SEED = r'''
import json
from deepseek_infra.infra.media import library, indexer
from deepseek_infra.infra.rag import local_rag
for mid in ('media_keep', 'media_other'):
    path = library.save_source_bytes(mid, 'source.pdf', b'%PDF-1.4\ncontrolled deletion fixture\n')
    record = library.register_media(media_id=mid, project_id='project-a', media_type='pdf',
        title=mid, mime_type='application/pdf', path=path, status='ready')
    segments = library.save_segments(mid, [{'segmentId':'seg_'+mid, 'type':'page_text', 'text':'Retained '+mid, 'page':1}])
    assert indexer.index_media_segments(record, segments) == 1
assert local_rag.upsert_items([
    {'item_id':'cross-project-target', 'collection':'media', 'source_id':'media_keep', 'project_id':'project-b', 'text':'Cross project', 'embedding':[1.0]+[0.0]*63},
    {'item_id':'unrelated-collection', 'collection':'files', 'source_id':'media_keep', 'project_id':'project-a', 'text':'Other collection', 'embedding':[1.0]+[0.0]*63},
]) == 2
db, vector = local_rag.db_ready()
try:
    rows = db.execute('SELECT COUNT(*) FROM rag_items').fetchone()[0]
    assert rows == 4
    version = db.execute('SELECT vec_version()').fetchone()[0] if vector else None
finally:
    db.close()
print(json.dumps({'rows':rows, 'vector':vector, 'version':version}))
'''
ORACLE = r'''
import json
from fastapi.testclient import TestClient
from deepseek_infra.web.server import create_app, settings
with TestClient(create_app(), base_url='http://127.0.0.1') as client:
    refused = client.delete('/api/media/media_keep')
    first = client.delete('/api/media/media_keep', headers={'Authorization':'Bearer '+settings.auth.token})
    repeat = client.delete('/api/media/media_keep', headers={'Authorization':'Bearer '+settings.auth.token})
print(json.dumps({'refused':refused.status_code, 'firstStatus':first.status_code,
    'first':first.json(), 'repeatStatus':repeat.status_code, 'repeat':repeat.json()}))
'''


def require(value: Any, code: str) -> None:
    patch.require(value, code)


def offline(profile: Path, backend: str, program: str, log: Path) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR",
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP"}}
    env.update(DEEPSEEK_INFRA_ROOT=str(profile), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
        LOCAL_RAG_ENABLED="true", LOCAL_RAG_BACKEND=backend, LOCAL_RAG_EMBEDDING_DIMENSIONS="64")
    result = subprocess.run([sys.executable, "-B", "-c", program], cwd=ROOT, env=env,
        capture_output=True, timeout=45)
    require(len(result.stderr) <= LIMIT and len(result.stdout) <= LIMIT, "MEDIA_DELETE_ORACLE_OUTPUT_TOO_LARGE")
    log.write_bytes(result.stderr)
    require(result.returncode == 0, "MEDIA_DELETE_ORIGINAL_ORACLE_FAILED")
    return json.loads(result.stdout)


def snapshot(profile: Path, backend: str) -> dict[str, Any]:
    """Read bounded existing fixture files/indexes, without creating a database."""
    require(not profile.is_symlink(), "MEDIA_DELETE_OBSERVER_LINK_REFUSED")
    profile = profile.resolve(strict=True)
    paths = [profile / ".media/library.json", profile / ".local-rag/rag.sqlite3"]
    for name in (TARGET, OTHER):
        paths.extend([profile / f".media/objects/{name}/source.pdf", profile / f".media/segments/{name}.json"])
    files: dict[str, str | None] = {}
    for path in paths:
        cursor = path
        while cursor != profile:
            require(not cursor.is_symlink(), "MEDIA_DELETE_OBSERVER_LINK_REFUSED")
            cursor = cursor.parent
        if not path.exists():
            files[path.relative_to(profile).as_posix()] = None
            continue
        require(path.is_file() and path.stat().st_size <= LIMIT, "MEDIA_DELETE_OBSERVER_FILE_INVALID")
        files[path.relative_to(profile).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    library = profile / ".media/library.json"
    require(library.is_file(), "MEDIA_DELETE_LIBRARY_REQUIRED")
    media = json.loads(library.read_bytes())["media"]
    require(isinstance(media, list) and len(media) <= 1000, "MEDIA_DELETE_LIBRARY_BOUND_INVALID")
    db_path = profile / ".local-rag/rag.sqlite3"
    require(db_path.is_file(), "MEDIA_DELETE_INDEX_REQUIRED")
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        if backend == "sqlite_vec":
            import sqlite_vec
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            require(db.execute("SELECT vec_version()").fetchone()[0] == "v0.1.9", "MEDIA_DELETE_VECTOR_VERSION_DIFFERS")
        rows = db.execute("SELECT item_id,vector_id,collection,source_id,project_id FROM rag_items ORDER BY item_id LIMIT 1001").fetchall()
        require(len(rows) <= 1000, "MEDIA_DELETE_INDEX_BOUND_INVALID")
        complete = db.execute("SELECT item_id,* FROM rag_items ORDER BY item_id LIMIT 1001").fetchall()
        contents = {row[0]: hashlib.sha256(json.dumps(
            [dict(blobHex=value.hex()) if isinstance(value, bytes) else value for value in row],
            ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest() for row in complete}
        vectors = db.execute("SELECT rowid,embedding FROM rag_vec ORDER BY rowid LIMIT 1001").fetchall() if backend == "sqlite_vec" else None
        require(vectors is None or len(vectors) <= 1000, "MEDIA_DELETE_VECTOR_BOUND_INVALID")
    # SQLite layout/transaction bytes can differ; compare logical rows and the
    # target/unrelated source bytes, retaining the raw DB hash diagnostically.
    return dict(files=files, media=media, rows=[list(row) for row in rows], rowContents=contents,
        vectors=None if vectors is None else [dict(rowid=row[0], sha256=hashlib.sha256(row[1]).hexdigest()) for row in vectors])


def require_deleted(before: dict[str, Any], after: dict[str, Any], expected: dict[str, Any]) -> None:
    require(after["media"] == expected["media"], "MEDIA_DELETE_RETAINED_RECORDS_DIFFER")
    wanted = [row for row in before["rows"] if not (row[2] == "media" and row[3] == TARGET)]
    require(len(before["rows"]) == 4 and len(wanted) == 2 and after["rows"] == expected["rows"] == wanted,
        "MEDIA_DELETE_TARGET_OR_UNRELATED_INDEX_ROWS_DIFFER")
    wanted_contents = {row[0]: before["rowContents"][row[0]] for row in wanted}
    require(after["rowContents"] == expected["rowContents"] == wanted_contents, "MEDIA_DELETE_UNRELATED_ROW_CONTENT_CHANGED")
    if before["vectors"] is not None:
        require(len(before["vectors"]) == 4, "MEDIA_DELETE_VECTOR_FIXTURE_NOT_INDEXED")
        kept_ids = {row[1] for row in wanted}
        wanted_vectors = [row for row in before["vectors"] if row["rowid"] in kept_ids]
        require(after["vectors"] == expected["vectors"] == wanted_vectors, "MEDIA_DELETE_VECTOR_ROWS_DIFFER")
    for name in (TARGET, OTHER):
        for relative in (f".media/objects/{name}/source.pdf", f".media/segments/{name}.json"):
            wanted_hash = None if name == TARGET else before["files"][relative]
            require(wanted_hash is not None or name == TARGET, "MEDIA_DELETE_UNRELATED_FIXTURE_MISSING")
            require(after["files"][relative] == expected["files"][relative] == wanted_hash,
                "MEDIA_DELETE_SOURCE_OR_SEGMENT_BYTES_DIFFER")


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    require(os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux", "MEDIA_DELETE_VERIFICATION_IS_CLOUD_ONLY")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    require(revision == os.environ.get("GITHUB_SHA"), "MEDIA_DELETE_SOURCE_DIFFERS")
    require(not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT), "MEDIA_DELETE_SOURCE_IS_DIRTY")
    report["sourceCommit"] = revision
    with tempfile.TemporaryDirectory(prefix="native-media-delete-") as temporary:
        scratch = Path(temporary)
        binaries, profile, mirror = scratch / "bin", scratch / "profile", scratch / "oracle"
        binaries.mkdir()
        (scratch / "go-tmp").mkdir()
        environment = patch.signing.cache_environment(ROOT,
            env=patch.signing.child_environment(dict(os.environ)), target_dir=args.go_cache)
        environment.update(CGO_ENABLED="0", GOTOOLCHAIN="local", GOMAXPROCS="2", GOPROXY="off", GOSUMDB="off",
            GOWORK="off", GOCACHE=str(args.go_cache.resolve()), GOTMPDIR=str(scratch / "go-tmp"))
        require(subprocess.check_output(["go", "version"], env=environment, text=True).startswith(
            "go version " + patch.signing.GO_VERSION + " "), "MEDIA_DELETE_GO_PIN_DIFFERS")
        for name in ("deepseekd", "deepseek-launch"):
            patch.signing.record_command(["go", "build", "-p=2", "-o", str(binaries / name), "./cmd/" + name],
                ROOT / "go", environment, args.out, "build-" + name, timeout=600)
        for name, source in (("deepseek-worker", args.worker), ("deepseek-gateway", args.edge)):
            (binaries / name).symlink_to(source.resolve(strict=True))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        launch_env = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}}
        launch_env.update(DEEPSEEK_NATIVE_BIN=str(binaries), DEEPSEEK_INFRA_ROOT=str(profile),
            DEEPSEEK_INFRA_STATIC_DIR=str(ROOT / "static"), LOCAL_RAG_ENABLED="true",
            LOCAL_RAG_BACKEND=args.backend, LOCAL_RAG_EMBEDDING_DIMENSIONS="64")
        child: subprocess.Popen[bytes] | None = None
        identities: list[dict[str, Any]] = []

        def launch(label: str) -> str:
            nonlocal child, identities
            with (args.out / (label + ".log")).open("wb") as log:
                child = subprocess.Popen([str(binaries / "deepseek-launch"), "--server", "--no-prompt",
                    "--port", origin.rsplit(":", 1)[1]], cwd=scratch, env=launch_env, stdout=log, stderr=subprocess.STDOUT)
            patch.bootstrap.wait_public(origin, child)
            identities = [{**row, "startTicks": patch.reads.process_identity(row["pid"])}
                for row in patch.bootstrap.process_roles(child.pid)]
            require(all(row["startTicks"] is not None for row in identities), "MEDIA_DELETE_PROCESS_DISAPPEARED")
            report[label + "Processes"] = identities
            return (profile / ".auth-token").read_text().strip()

        def stop() -> None:
            nonlocal child, identities
            if child is not None:
                patch.reads.stop(child, identities)
                child, identities = None, []

        try:
            launch("empty-launch")
            require(not (profile / ".media").exists(), "MEDIA_DELETE_FRESH_PROFILE_HAS_MEDIA")
            stop()
            seeded = offline(mirror, args.backend, SEED, args.out / "seed.log")
            require(seeded["vector"] is (args.backend == "sqlite_vec"), "MEDIA_DELETE_SEED_BACKEND_DIFFERS")
            for name in (".media", ".local-rag"):
                require(not (profile / name).exists(), "MEDIA_DELETE_SEED_WOULD_REPLACE_STATE")
                shutil.copytree(mirror / name, profile / name)
            before = snapshot(profile, args.backend)
            oracle = offline(mirror, args.backend, ORACLE, args.out / "original-route.log")
            require(oracle == dict(refused=401, firstStatus=200, first=dict(ok=True, deleted=1),
                repeatStatus=200, repeat=dict(ok=True, deleted=0)), "MEDIA_DELETE_ORIGINAL_ROUTE_DID_NOT_SUCCEED")
            expected = snapshot(mirror, args.backend)
            require_deleted(before, expected, expected)
            token = launch("delete-launch")
            generation_before = patch.generation_observation(profile)
            original_library = (profile / ".media/library.json").read_bytes()
            status, _, _ = patch.exchange(origin, "/api/media/" + TARGET, "DELETE", None)
            require(status == 401 and (profile / ".media/library.json").read_bytes() == original_library,
                "MEDIA_DELETE_AUTH_REFUSAL_CHANGED_LIBRARY")
            status, raw, _ = patch.exchange(origin, "/api/media/" + TARGET, "DELETE", token)
            require(status == oracle["firstStatus"] and json.loads(raw) == oracle["first"], "MEDIA_DELETE_PUBLIC_RESULT_DIFFERS")
            after = snapshot(profile, args.backend)
            require_deleted(before, after, expected)
            generation_after = patch.generation_observation(profile)
            require(generation_after["value"] == generation_before["value"] + 2, "MEDIA_DELETE_GENERATION_NOT_FENCED")
            retained = (profile / ".media/library.json").read_bytes()
            status, raw, _ = patch.exchange(origin, "/api/media/" + TARGET, "DELETE", token)
            require(status == oracle["repeatStatus"] and json.loads(raw) == oracle["repeat"], "MEDIA_DELETE_REPEAT_RESULT_DIFFERS")
            require((profile / ".media/library.json").read_bytes() == retained
                and patch.generation_observation(profile) == generation_after, "MEDIA_DELETE_MISSING_RECORD_MUTATED_STATE")
            stop()
            launch("retained-launch")
            require_deleted(before, snapshot(profile, args.backend), expected)
            require((profile / ".media/library.json").read_bytes() == retained
                and patch.generation_observation(profile) == generation_after, "MEDIA_DELETE_RESTART_REPUBLISHED_STATE")
            report.update(actualOriginalASGIRouteCompared=True, originalOracle=oracle, before=before, after=after,
                generationBefore=generation_before, generationAfter=generation_after,
                actualNativeHTTPDeleteQualified=True, repeatNoopQualified=True, retainedRestartQualified=True, status="PASS")
        finally:
            stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--edge", type=Path, required=True)
    parser.add_argument("--go-cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--backend", choices=("sqlite", "sqlite_vec"), required=True)
    args = parser.parse_args()
    args.out = args.out.resolve()
    require(args.out.is_relative_to((ROOT / "artifacts").resolve()), "MEDIA_DELETE_REPORT_PATH_ESCAPED")
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = dict(kind="native-media-delete-http-v1", status="FAIL", backend=args.backend,
        sourceCommit=os.environ.get("GITHUB_SHA"), runId=os.environ.get("GITHUB_RUN_ID"),
        actualNativeHTTPDeleteQualified=False, actualDeleteSIGKILLQualified=False,
        backupRestoreInteractionQualified=False, fullMigrationQualified=False, readiness="NOT_READY")
    try:
        run(args, report)
    except Exception as error:
        report.update(status="FAIL", failureCode=str(error) if isinstance(error, patch.ProbeFailure)
            else "MEDIA_DELETE_VERIFIER_ERROR", exceptionType=type(error).__name__)
    report["finishedAt"] = datetime.now(timezone.utc).isoformat()
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
