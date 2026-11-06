"""Cloud-only public media PATCH against an independent original-library oracle.

The retained media fixture is explicitly seeded while the native launcher is
stopped. This one workload is not authority, reconciliation or media migration
qualification. Neither the oracle nor the verifier is in the production tree.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import verify_agent_signing_process as signing
import verify_native_default_bootstrap as bootstrap
import verify_native_media_reads as reads
from native_media_compact_receipt import compact_patch_observation, media_outcome_type

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/native_media_library_read_v1.json"
PATCH_INPUT_FIXTURE = ROOT / "tests/fixtures/native_media_patch_input_v1.json"
PATCH_INPUT_FIXTURE_SHA256 = "f801b9784e88c4571a4da280c189dbb07c15a6b80d25414cf1262eb3d9182b1e"
PATCH_JSON_ERROR_FIXTURE = ROOT / "tests/fixtures/native_media_patch_json_errors_v1.json"
PATCH_JSON_ERROR_FIXTURE_SHA256 = "0ddbdde27c2944f4d7f7609c3664125c3a4eeb1ebb0e717a38d44e16c74a4ad4"
MEDIA_ID = "media_keep"
BASE_PATCH = {"title": "Native media update", "metadata": {"nativePatchProof": "controlled"}}
# These requests deliberately include fields accepted only by the internal
# library. Their public contract must come from the actual original HTTP route.
FIELD_PATCHES: dict[str, dict[str, Any]] = {
    "all-fields": {
        "title": "Native full field parity", "projectId": "proj_fields", "metadata": {"nativePatchProof": "all-fields"},
        "path": "objects/media_keep/alternate.png", "source": {"kind": "url", "refId": "https://example.invalid/source"},
        "status": "pending", "type": "image", "mimeType": "IMAGE/PNG; charset=binary",
    },
    "source-only": {"source": {"kind": "upload", "refId": "new-source.pdf", "label": "Changed origin"}},
    "path-only": {"path": "objects\\media_keep\\alternate.pdf"},
    "status-only": {"status": " PROCESSING "},
    "type-only": {"type": "video"},
    "mime-only": {"mimeType": "Audio/OGG; codecs=opus"},
    "null-reset": {"path": None, "source": None, "status": None, "mimeType": None, "title": None, "projectId": None},
    "type-inference": {"type": None, "mimeType": "audio/ogg", "title": "sound.ogg"},
    "source-ordered": {"source": {"kind": "manual", "refId": {"z": 0, "a": 1}, "title": "Ordered source"}},
}
PATCH = BASE_PATCH
LIMIT = 2 * 1024**2
ORACLE = r'''
import json, sys
from deepseek_infra.infra.media import library
patch = json.load(sys.stdin)
media = library.update_media("media_keep", patch)
print(json.dumps({"ok": True, "media": media}))
'''
HTTP_ORACLE = r'''
import json, sys
from fastapi.testclient import TestClient
from deepseek_infra.web.server import create_app, settings
client = TestClient(create_app(), base_url="http://127.0.0.1")
try:
    response = client.patch("/api/media/media_keep", content=sys.stdin.buffer.read(),
        headers={"Authorization": "Bearer " + settings.auth.token, "Content-Type": "application/json"})
    print(json.dumps({"status": response.status_code, "body": response.json()}))
finally:
    client.close()
'''
JSON_ERROR_HTTP_ORACLE = r'''
import base64, hashlib, json, sys
from pathlib import Path
from deepseek_infra.core.config import settings
from deepseek_infra.infra.media import library
from tests.test_web_media_routes import _auth_headers, _request, _running_server

assert sys.version_info[:2] == (3, 12) and settings.auth.enabled
profile = Path(sys.argv[1]).resolve()
store = library.store_path().resolve()
assert store.is_relative_to(profile) and settings.root == profile
cases = json.load(sys.stdin)
path = "/api/media/" + cases[0]["mediaId"]
initial = store.read_bytes()
with _running_server() as server:
    baseline_status, baseline, _ = _request(server, "GET", path, headers=_auth_headers())
    assert baseline_status == 200
    for case in cases:
        assert path == "/api/media/" + case["mediaId"]
        request = base64.b64decode(case["requestBase64"], validate=True)
        assert hashlib.sha256(request).hexdigest() == case["requestSha256"]
        status, raw, _ = _request(server, "PATCH", path, body=request, headers=_auth_headers())
        assert status == case["status"] and json.loads(raw) == json.loads(case["bodyJson"]), (case["name"], status, raw)
        assert store.read_bytes() == initial
        read_status, read_raw, _ = _request(server, "GET", path, headers=_auth_headers())
        assert read_status == baseline_status and read_raw == baseline
print(json.dumps({"status": "PASS", "originalTCP": True, "cases": len(cases), "requests": 1 + 2 * len(cases),
                  "python": ".".join(map(str, sys.version_info[:3])), "unchangedStoreSha256": hashlib.sha256(initial).hexdigest()}))
'''
CAPACITY_ORACLE = r'''
import json
from deepseek_infra.infra.media import library, schema
rows = library._load_store()
capacity = [row for row in rows if row["mediaId"].startswith("media_capacity_")]
left, right = "L" * 120000, "R" * 120000
expected = {f"media_capacity_{i:03}": schema.normalize_media_record({
    "mediaId": f"media_capacity_{i:03}", "title": f"Capacity metadata {i}",
    "type": "image", "mimeType": "image/png", "path": "", "source": {}, "status": "pending",
    "createdAt": "2026-11-02T00:00:00+00:00", "updatedAt": "2026-11-02T00:00:00+00:00",
    "metadata": {"capacity_left": left, "capacity_right": right}}) for i in range(600)}
accepted = len(capacity) == 600 and {row["mediaId"]: row for row in capacity} == expected
assert accepted and len(rows) <= 1000
print(json.dumps({"status": "PASS_ORIGINAL_LARGE_MEDIA_LIBRARY", "normalizedRecords": len(rows),
    "capacityRecords": len(capacity), "metadataCharactersPreserved": 600 * 2 * 120000,
    "storeBytes": library.store_path().stat().st_size, "allCapacityMetadataAccepted": accepted}))
'''


class ProbeFailure(RuntimeError):
    pass


def require(value: Any, code: str) -> None:
    if not value:
        raise ProbeFailure(code)


def case_patch(case: str) -> dict[str, Any]:
    if case == "maximum-array":
        return {"title": BASE_PATCH["title"], "metadata": [0] * 499_960}
    prefix = "k" * 80
    metadata_cases = {
        "top-level": {"a?": "first", "a!": "last"},
        "nested": {"outer": {"a?": "first", "a!": "last"}},
        "array": {"rows": [{"a?": "first", "a!": "last"}]},
        "truncated": {prefix + "z": "first", prefix + "a": "last"},
    }
    if case == "baseline":
        return json.loads(json.dumps(BASE_PATCH))
    require(case in metadata_cases, "MEDIA_PATCH_ORDER_CASE_INVALID")
    return {"title": BASE_PATCH["title"], "metadata": metadata_cases[case]}


def wire_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def large_library_media(base: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retain the original fixture and add600legal records, below its1000record bound.

    Both large strings are shared in memory. Only the cloud-only caller writes
    the full fixture; offline schema tests never serialize its144MB payload.
    """
    require(len(base) == 18, "MEDIA_PATCH_FIXTURE_CONTRACT_DIFFERS")
    left, right = "L" * 120_000, "R" * 120_000
    return [*base, *({"mediaId": f"media_capacity_{i:03}", "title": f"Capacity metadata {i}",
        "type": "image", "mimeType": "image/png", "path": "", "source": {}, "status": "pending",
        "createdAt": "2026-11-02T00:00:00+00:00", "updatedAt": "2026-11-02T00:00:00+00:00",
        "metadata": {"capacity_left": left, "capacity_right": right}} for i in range(600))]


def large_record_media(base: list[dict[str, Any]]) -> list[dict[str, Any]]:
    require(len(base) == 18, "MEDIA_PATCH_FIXTURE_CONTRACT_DIFFERS")
    records: list[dict[str, Any]] = json.loads(json.dumps(base))
    selected = next(row for row in records if isinstance(row, dict) and row.get("mediaId") == MEDIA_ID)
    existing = selected.get("metadata")
    selected["metadata"] = {**(existing if isinstance(existing, dict) else {}),
                            **{f"retained_large_{i:02}": "r" * 120_000 for i in range(36)}}
    return records


def capacity_original_observation(profile: Path, diagnostic: Path) -> dict[str, Any]:
    """Read the actual original writer's retained file in an isolated oracle process."""
    environment = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}}
    environment.update(DEEPSEEK_INFRA_ROOT=str(profile), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-B", "-c", CAPACITY_ORACLE], cwd=ROOT, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    require(len(result.stdout) <= LIMIT and len(result.stderr) <= LIMIT, "ORIGINAL_MEDIA_CAPACITY_OUTPUT_EXCEEDED_BOUND")
    diagnostic.write_bytes(result.stderr)
    require(result.returncode == 0, "ORIGINAL_MEDIA_CAPACITY_RETENTION_FAILED")
    observed = json.loads(result.stdout)
    require(observed.get("status") == "PASS_ORIGINAL_LARGE_MEDIA_LIBRARY"
        and observed.get("capacityRecords") == 600 and observed.get("allCapacityMetadataAccepted") is True
        and observed.get("storeBytes", 0) > 128 * 1024**2, "ORIGINAL_MEDIA_CAPACITY_NOT_OBSERVED")
    return observed


def phase_timing_environment(identities: list[dict[str, Any]]) -> bool:
    """Check only one opt-in flag in the same owned worker; never report env values."""
    workers = [row for row in identities if row["executable"] == "deepseek-worker"]
    require(len(workers) == 1, "MEDIA_PHASE_WORKER_IDENTITY_MISSING")
    worker = workers[0]
    pid = worker["pid"]
    require(reads.process_identity(pid) == worker["startTicks"], "MEDIA_PHASE_WORKER_IDENTITY_CHANGED")
    with (Path("/proc") / str(pid) / "environ").open("rb") as stream:
        raw = stream.read(256 * 1024 + 1)
    require(len(raw) <= 256 * 1024, "MEDIA_PHASE_ENVIRONMENT_EXCEEDED_BOUND")
    matches = [entry for entry in raw.split(b"\0") if entry.startswith(b"DEEPSEEK_MEDIA_PHASE_TIMING=")]
    enabled = matches == [b"DEEPSEEK_MEDIA_PHASE_TIMING=1"]
    require(reads.process_identity(pid) == worker["startTicks"], "MEDIA_PHASE_WORKER_IDENTITY_CHANGED")
    require(enabled, "MEDIA_PHASE_TIMING_ENVIRONMENT_NOT_RETAINED")
    return True


def capacity_peak_memory(identities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read process-owned procfs high-water marks without asserting a new budget."""
    rows = []
    for identity in identities:
        pid = identity["pid"]
        require(reads.process_identity(pid) == identity["startTicks"], "MEDIA_CAPACITY_PROCESS_IDENTITY_CHANGED")
        with (Path("/proc") / str(pid) / "status").open(encoding="utf-8") as stream:
            text = stream.read(16_385)
        require(len(text) <= 16_384, "MEDIA_CAPACITY_PROC_STATUS_EXCEEDED_BOUND")
        values = {line.split(":", 1)[0]: line.split(":", 1)[1].split() for line in text.splitlines() if ":" in line}
        row = {"pid": pid, "executable": identity["executable"], "startTicks": identity["startTicks"]}
        for key in ["VmRSS", "VmHWM"]:
            fields = values.get(key, [])
            require(len(fields) == 2 and fields[0].isdigit() and fields[1] == "kB", "MEDIA_CAPACITY_MEMORY_OBSERVATION_INVALID")
            row[key + "KiB"] = int(fields[0])
        require(reads.process_identity(pid) == identity["startTicks"], "MEDIA_CAPACITY_PROCESS_IDENTITY_CHANGED")
        rows.append(row)
    return rows


def diagnostic_table(owned_root: Path, filename: str, query: str, columns: tuple[str, ...]) -> dict[str, Any]:
    if filename not in {"control.sqlite3", "media-effects.sqlite3"} or owned_root.is_symlink():
        return {"status": "REFUSED"}
    try:
        paths = list(owned_root.rglob(filename))
        if len(paths) != 1:
            return {"status": "ABSENT" if not paths else "AMBIGUOUS", "matches": len(paths)}
        path = paths[0]
        require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(owned_root.resolve()),
                "MEDIA_DIAGNOSTIC_PATH_ESCAPED")
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)
        try:
            rows = connection.execute(query).fetchmany(2)
        finally:
            connection.close()
        values = [{name: value if value is None or type(value) is int or isinstance(value, str) and len(value) <= 128
                   else "INVALID_FIELD" for name, value in zip(columns, row, strict=True)} for row in rows]
        return {"status": "OBSERVED", "rows": values}
    except Exception as error:
        return {"status": "UNAVAILABLE", "error": type(error).__name__}


def request_resources(identities: list[dict[str, Any]], proc_root: Path = Path("/proc")) -> list[dict[str, Any]]:
    observations = []
    for identity in identities:
        row = {"pid": identity["pid"], "executable": identity["executable"]}
        try:
            pid = identity["pid"]
            require(type(pid) is int and pid > 1, "MEDIA_DIAGNOSTIC_PID_INVALID")
            with (proc_root / str(pid) / "stat").open(encoding="utf-8") as stream:
                raw = stream.read(16_385)
            require(len(raw) <= 16_384 and raw.startswith(str(pid) + " ("), "MEDIA_DIAGNOSTIC_STAT_INVALID")
            fields = raw[raw.rfind(")") + 2:].split()
            require(len(fields) >= 22, "MEDIA_DIAGNOSTIC_STAT_INVALID")
            if str(identity.get("startTicks")) != fields[19]:
                row.update(status="IDENTITY_CHANGED")
            else:
                row.update(status="OBSERVED", startTicks=fields[19], cpuTicks=int(fields[11]) + int(fields[12]),
                           rssPages=int(fields[21]))
        except Exception as error:
            row.update(status="UNAVAILABLE", error=type(error).__name__)
        observations.append(row)
    return observations


def uncertain_request_snapshot(profile: Path, identities: list[dict[str, Any]]) -> dict[str, Any]:
    return {"processes": request_resources(identities),
            "go": diagnostic_table(profile, "control.sqlite3",
                "SELECT operation_id,state,epoch,writer_fencing_token,lease_until,length(intent),length(outcome) FROM control_media_operations LIMIT 2",
                ("operationId", "state", "epoch", "writerFence", "leaseUntil", "intentBytes", "outcomeBytes")),
            "rust": diagnostic_table(profile, "media-effects.sqlite3",
                "SELECT operation_id,state,length(claim),length(outcome) FROM media_operations LIMIT 2",
                ("operationId", "state", "claimBytes", "outcomeBytes"))}


def journal_observation(owned_root: Path, *, allow_empty: bool = False) -> dict[str, Any]:
    journals = list(owned_root.rglob("media-effects.sqlite3"))
    require(len(journals) == 1, "MEDIA_PATCH_EFFECT_JOURNAL_INVALID")
    path = journals[0]
    require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(owned_root.resolve()),
            "MEDIA_PATCH_EFFECT_JOURNAL_INVALID")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        require(connection.execute("PRAGMA application_id").fetchone()[0] == 0x44534D50,
                "MEDIA_PATCH_EFFECT_JOURNAL_INVALID")
        rows = connection.execute(
            "SELECT operation_id,state,length(claim),substr(claim,1,?) FROM media_operations LIMIT 2",
            (4 * 1024**2 + 1,),
        ).fetchall()
    finally:
        connection.close()
    if allow_empty and not rows:
        return {"state": "EMPTY", "operationCount": 0}
    require(len(rows) == 1, "MEDIA_PATCH_EFFECT_JOURNAL_INVALID")
    operation, state, length, claim = rows[0]
    require(isinstance(operation, str) and 0 < len(operation) <= 128 and state == "APPLIED"
            and isinstance(length, int) and 0 < length <= 4 * 1024**2
            and isinstance(claim, bytes) and len(claim) == length, "MEDIA_PATCH_EFFECT_JOURNAL_INVALID")
    return {"operationId": operation, "state": state, "claimBytes": length,
            "claimSha256": hashlib.sha256(claim).hexdigest()}


def seed(profile: Path, media: list[dict[str, Any]]) -> None:
    directory = profile / ".media"
    require(not directory.exists(), "MEDIA_PATCH_SEED_WOULD_REPLACE_EXISTING_MEDIA")
    directory.mkdir(parents=True)
    (directory / "library.json").write_text(json.dumps({"media": media}, ensure_ascii=False) + "\n", encoding="utf-8")


def original_patch(profile: Path) -> dict[str, Any]:
    environment = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}}
    environment.update(DEEPSEEK_INFRA_ROOT=str(profile), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-B", "-c", ORACLE], cwd=ROOT, env=environment,
        input=json.dumps(PATCH).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=True)
    require(len(result.stdout) <= LIMIT, "ORIGINAL_MEDIA_PATCH_OUTPUT_EXCEEDED_BOUND")
    body = json.loads(result.stdout)
    require(body.get("ok") is True and body.get("media", {}).get("mediaId") == MEDIA_ID,
            "ORIGINAL_MEDIA_PATCH_WORKLOAD_DID_NOT_SUCCEED")
    return body


def original_http_patch(profile: Path, diagnostic: Path | None = None) -> tuple[int, dict[str, Any]]:
    """Exercise the original public route, including its three-field allowlist."""
    environment = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR",
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP"}}
    environment.update(DEEPSEEK_INFRA_ROOT=str(profile), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-B", "-c", HTTP_ORACLE], cwd=ROOT, env=environment,
        input=wire_payload(PATCH), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    require(len(result.stderr) <= LIMIT, "ORIGINAL_MEDIA_HTTP_DIAGNOSTIC_EXCEEDED_BOUND")
    if diagnostic is not None:
        diagnostic.write_bytes(result.stderr)
    require(result.returncode == 0, "ORIGINAL_MEDIA_HTTP_ORACLE_FAILED")
    require(len(result.stdout) <= LIMIT, "ORIGINAL_MEDIA_HTTP_OUTPUT_EXCEEDED_BOUND")
    observed = json.loads(result.stdout)
    require(observed.get("status") in {200, 400} and isinstance(observed.get("body"), dict),
            "ORIGINAL_MEDIA_HTTP_OBSERVATION_INVALID")
    return observed["status"], observed["body"]


def exchange(origin: str, path: str, method: str, token: str | None,
             payload: dict[str, Any] | None = None) -> tuple[int, bytes, str]:
    status, raw, content_type, _ = exchange_with_operation(origin, path, method, token, payload)
    return status, raw, content_type


def exchange_with_operation(origin: str, path: str, method: str, token: str | None,
                            payload: dict[str, Any] | bytes | None = None) -> tuple[int, bytes, str, str]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    data = wire_payload(payload) if isinstance(payload, dict) else payload
    try:
        response = urlopen(Request(origin + path, method=method, headers=headers, data=data), timeout=10)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(LIMIT + 1)
        require(len(raw) <= LIMIT, "NATIVE_MEDIA_PATCH_RESPONSE_EXCEEDED_BOUND")
        return (response.status, raw, response.headers.get("Content-Type", ""),
                response.headers.get("X-DeepSeek-Operation-Id", ""))


def without_update_time(body: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(body))
    require(isinstance(result.get("media"), dict), "NATIVE_MEDIA_PATCH_RECORD_INVALID")
    result["media"].pop("updatedAt", None)
    return result


def generation_observation(profile: Path) -> dict[str, Any]:
    path = profile / ".workspace-generation"
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return {"present": False, "value": 0, "sha256": None}
    require(stat.S_ISREG(entry.st_mode) and entry.st_size <= 32, "MEDIA_PATCH_GENERATION_COUNTER_INVALID")
    raw = path.read_bytes()
    require(len(raw) <= 32 and raw.strip().isdigit(), "MEDIA_PATCH_GENERATION_COUNTER_INVALID")
    return {"present": True, "value": int(raw.strip()), "sha256": hashlib.sha256(raw).hexdigest()}


RETAINED_MUTATION_ORACLE_SHA256 = "792e84e834886db4bd09babba350e66ca192f47c49bd4fb71966716689386f21"
RETAINED_COMBINED_ORACLE_SHA256 = "258af9dcf6b19d1736fd21c4114835c0bdf3a6c8851f8daeb8c174788a4569ae"
RETAINED_MUTATION_SEED_SHA256 = "ff70c2c7be7d29138368c4740cf6288812626303e637246579f2c62525f9c438"
RETAINED_EXACT_INTEGER = 1234567890123456789012345678901234567890


def retained_deep_value() -> dict[str, Any]:
    value: dict[str, Any] = {"last": 1}
    for _ in range(160):
        value = {"next": value}
    return value


def retained_mutation_receipt(scratch: Path, profile: Path, operation: str, state: str,
                              before: str, after: str | None, version: int) -> dict[str, Any]:
    worker = list(scratch.rglob("media-effects.sqlite3"))
    require(len(worker) == 1, "RETAINED_MUTATION_WORKER_JOURNAL_NOT_UNIQUE")
    control = profile / "go-control/control.sqlite3"
    rows = []
    for path, table in ((worker[0], "media_operations"), (control, "control_media_operations")):
        require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(scratch.resolve()),
                "RETAINED_MUTATION_JOURNAL_OUTSIDE_PROFILE")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute(f"SELECT state,preparation_sha256,length(outcome),substr(outcome,1,4096) "
                             f"FROM {table} WHERE operation_id=?", (operation,)).fetchone()
        require(row is not None and row[0] == state and isinstance(row[2], int) and 0 < row[2] < 4096
                and isinstance(row[3], bytes) and len(row[3]) == row[2],
                "RETAINED_MUTATION_TERMINAL_RECEIPT_MISSING")
        rows.append(row)
    require(rows[0] == rows[1], "RETAINED_MUTATION_GO_AND_RUST_RECEIPTS_DIFFER")
    _, preparation, _, raw = rows[0]
    outcome = media_outcome_type().FromString(raw)
    states = outcome.DESCRIPTOR.fields_by_name["state"].enum_type.values_by_name
    require(outcome.schema_version == version and outcome.operation_id == operation
            and outcome.original_fence.action_id == operation and outcome.original_fence.execution_epoch == 1
            and outcome.before_sha256.hex() == before and outcome.after_sha256.hex() == (after or "")
            and len(outcome.signature) == 64 and len(outcome.request_sha256) == 32
            and outcome.state == states["MEDIA_PATCH_EFFECT_STATE_" + state].number,
            "RETAINED_MUTATION_SIGNED_RECEIPT_DIFFERS")
    evidence: dict[str, Any] = {"operationId": operation, "state": state,
        "outcomeSha256": hashlib.sha256(raw).hexdigest(), "beforeSha256": before, "afterSha256": after,
        "schemaVersion": version, "settledGoAndRustBytesIdentical": True, "dispatchDigestMatches": True}
    if version == 3:
        require(outcome.http_status == 500 and outcome.error_code == "internal" and outcome.error_message == "Server error"
                and not outcome.HasField("patched"), "RETAINED_MUTATION_SIGNED_SERVER_ERROR_SHAPE_DIFFERS")
    else:
        manifest = outcome.patched
        require(version == 4 and state == "APPLIED" and outcome.http_status == 200 and not outcome.error_code
                and not outcome.error_message and not outcome.HasField("media") and outcome.HasField("patched")
                and manifest.original_fence == outcome.original_fence and len(manifest.preparation_sha256) == 32
                and len(manifest.record_sha256) == 32 and 0 < manifest.record_bytes < 1024**2,
                "RETAINED_MUTATION_FLAT_RECEIPT_SHAPE_DIFFERS")
        identity = hashlib.sha256(operation.encode()).hexdigest()
        records = list(scratch.glob(f"**/patch-results/{identity}/record.pb"))
        require(len(records) == 1 and records[0].is_file() and not records[0].is_symlink()
                and records[0].resolve().is_relative_to(scratch.resolve())
                and records[0].stat().st_size == manifest.record_bytes,
                "RETAINED_MUTATION_FLAT_RECORD_FILE_DIFFERS")
        require(hashlib.sha256(records[0].read_bytes()).digest() == manifest.record_sha256,
                "RETAINED_MUTATION_FLAT_RECORD_DIGEST_DIFFERS")
        evidence.update(recordBytes=manifest.record_bytes, recordSha256=manifest.record_sha256.hex())
    outcome.signature = b""
    require(hashlib.sha256(outcome.SerializeToString(deterministic=True)).digest() == preparation,
            "RETAINED_MUTATION_RECEIPT_DIFFERS_FROM_DISPATCH_PREPARATION")
    return evidence


def verify_retained_mutations(scratch: Path, profile: Path, origin: str, token: str,
                              report: dict[str, Any]) -> None:
    library = profile / ".media/library.json"
    cases = (
        ("refused_unicode", "PATCH", "media_target", 500, False),
        ("delete_unpaired", "DELETE", "media_unpaired", 200, True),
        ("applied_nan", "PATCH", "media_nonfinite", 500, True),
        ("unrelated_nan", "PATCH", "media_target", 200, True),
        ("exact_integer", "PATCH", "media_exact", 200, True),
        ("deep_object", "PATCH", "media_deep", 200, True),
    )
    observations: dict[str, dict[str, Any]] = {}
    report["retainedMutationCases"] = observations
    for label, method, media_id, wanted_status, applied in cases:
        before = hashlib.sha256(library.read_bytes()).hexdigest()
        generation_before = generation_observation(profile)
        payload = {"title": label} if method == "PATCH" else None
        report["nativePatchRequestStarted"] = True
        report["nativePatchReplyObserved"] = False
        status, raw, _, operation = exchange_with_operation(origin, "/api/media/" + media_id, method, token, payload)
        report["nativePatchReplyObserved"] = True
        report["nativePatchHTTPStatus"] = status
        after = hashlib.sha256(library.read_bytes()).hexdigest()
        generation_after = generation_observation(profile)
        require(status == wanted_status and bool(operation) and (before != after) == applied,
                "RETAINED_MUTATION_HTTP_OR_DURABLE_EFFECT_DIFFERS_" + label)
        require(generation_after["value"] == generation_before["value"] + (2 if applied else 0),
                "RETAINED_MUTATION_GENERATION_DIFFERS_" + label)
        body = json.loads(raw)
        evidence: dict[str, Any] = {"status": status, "beforeSha256": before, "afterSha256": after,
                                   "workspaceGeneration": generation_after["value"]}
        if status == 500:
            require(body == {"error": "Server error", "code": "internal"}, "RETAINED_MUTATION_PUBLIC_ERROR_DIFFERS")
            evidence["receipt"] = retained_mutation_receipt(scratch, profile, operation,
                "APPLIED" if applied else "REFUSED", before, after if applied else None, 3)
        elif method == "DELETE":
            require(body == {"ok": True, "deleted": 1}, "RETAINED_MUTATION_DELETE_COUNT_DIFFERS")
        else:
            require(body.get("ok") is True and body["media"]["title"] == label, "RETAINED_MUTATION_SUCCESS_BODY_DIFFERS")
            if label == "exact_integer":
                require(body["media"]["metadata"]["wide"] == RETAINED_EXACT_INTEGER,
                        "RETAINED_MUTATION_INTEGER_CHANGED_IN_PATCH_RESPONSE")
            if label == "deep_object":
                require(body["media"]["metadata"]["value"] == retained_deep_value(),
                        "RETAINED_MUTATION_DEEP_VALUE_CHANGED_IN_PATCH_RESPONSE")
                evidence["receipt"] = retained_mutation_receipt(scratch, profile, operation, "APPLIED", before, after, 4)
        observations[label] = evidence
    stored = {item["mediaId"]: item for item in json.loads(library.read_bytes())["media"]}
    require("media_unpaired" not in stored and stored["media_nonfinite"]["title"] == "applied_nan"
            and str(stored["media_nonfinite"]["metadata"]["f"]) == "nan"
            and stored["media_exact"]["metadata"]["wide"] == RETAINED_EXACT_INTEGER
            and stored["media_deep"]["metadata"]["value"] == retained_deep_value(),
            "RETAINED_MUTATION_VALUES_CHANGED_IN_DURABLE_STORE")


def assert_patch_input_record(actual: dict[str, Any], expected: dict[str, Any], label: str) -> None:
    require(without_update_time(actual) == without_update_time(expected),
            "PATCH_INPUT_RESPONSE_RECORD_DIFFERS_" + label)
    # Dictionary equality alone cannot detect the order of duplicate and
    # normalized metadata keys, including ordered values within that metadata.
    require(json.dumps(actual["media"]["metadata"]) == json.dumps(expected["media"]["metadata"]),
            "PATCH_INPUT_METADATA_ORDER_DIFFERS_" + label)


def verify_patch_input_read(origin: str, token: str, case: dict[str, Any]) -> None:
    status, raw, _ = exchange(origin, "/api/media/" + case["mediaId"], "GET", token)
    label = case["name"]
    require(status == case["getStatus"], "PATCH_INPUT_GET_STATUS_DIFFERS_" + label)
    if case["status"] == 200:
        assert_patch_input_record(json.loads(raw), json.loads(case["bodyJson"]), label + "_get")
    elif status == 500:
        require(json.loads(raw) == {"error": "Server error", "code": "internal"}, "PATCH_INPUT_GET_ERROR_DIFFERS_" + label)


def patch_input_request(case: dict[str, Any]) -> bytes:
    request = base64.b64decode(case["requestBase64"], validate=True)
    require(0 < len(request) <= 1_000_000 and hashlib.sha256(request).hexdigest() == case["requestSha256"],
            "PATCH_INPUT_REQUEST_DIFFERS_FROM_ORIGINAL_" + case["name"])
    return request


def verify_patch_inputs(scratch: Path, profile: Path, origin: str, token: str,
                        cases: list[dict[str, Any]], report: dict[str, Any]) -> None:
    library = profile / ".media/library.json"
    observations: dict[str, dict[str, Any]] = {}
    report["patchInputCases"] = observations
    for case in cases:
        label = case["name"]
        request = patch_input_request(case)
        before = hashlib.sha256(library.read_bytes()).hexdigest()
        generation_before = generation_observation(profile)
        report.update(nativePatchRequestStarted=True, nativePatchReplyObserved=False)
        status, raw, _, operation = exchange_with_operation(origin, "/api/media/" + case["mediaId"], "PATCH", token, request)
        report.update(nativePatchReplyObserved=True, nativePatchHTTPStatus=status)
        stored = library.read_bytes()
        after = hashlib.sha256(stored).hexdigest()
        generation_after = generation_observation(profile)
        applied = case["storeChanged"]
        evidence = observations[label] = {
            "requestSha256": case["requestSha256"], "originalStatus": case["status"], "status": status,
            "beforeSha256": before, "afterSha256": after, "workspaceGeneration": generation_after["value"],
        }
        admitted = label not in {"invalid_utf8", "malformed_trailing"}
        require(status == case["status"] and bool(operation) == admitted and (before != after) == applied,
                "PATCH_INPUT_HTTP_OR_DURABLE_EFFECT_DIFFERS_" + label)
        require(generation_after["value"] == generation_before["value"] + (2 if applied else 0)
                and (applied or generation_after == generation_before), "PATCH_INPUT_GENERATION_DIFFERS_" + label)
        body, expected = json.loads(raw), json.loads(case["bodyJson"])
        if status == 200:
            assert_patch_input_record(body, expected, label + "_patch")
            if label == "deep_object":
                evidence["receipt"] = retained_mutation_receipt(scratch, profile, operation, "APPLIED", before, after, 4)
        else:
            require(body == expected, "PATCH_INPUT_ERROR_BODY_DIFFERS_" + label)
            if admitted:
                evidence["receipt"] = retained_mutation_receipt(scratch, profile, operation,
                    "APPLIED" if applied else "REFUSED", before, after if applied else None, 3)
        if label in {"literal_nan", "decimal_overflow"}:
            value = next(row["metadata"]["value"] for row in json.loads(stored)["media"] if row["mediaId"] == case["mediaId"])
            require(isinstance(value, float) and str(value) == ("nan" if label == "literal_nan" else "inf"),
                    "PATCH_INPUT_NONFINITE_VALUE_CHANGED_" + label)
        verify_patch_input_read(origin, token, case)
        require(library.read_bytes() == stored and generation_observation(profile) == generation_after,
                "PATCH_INPUT_GET_CHANGED_LIBRARY_OR_GENERATION_" + label)
        evidence["getStatus"] = case["getStatus"]


def verify_original_patch_json_errors(profile: Path, seed_bytes: bytes, corpus: dict[str, Any], out: Path) -> dict[str, Any]:
    for path, key in (
        ("deepseek_infra/web/http_utils.py", "originalHelperSha256"),
        ("deepseek_infra/web/routes/media.py", "originalRouteSha256"),
    ):
        require(hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == corpus[key], "PATCH_JSON_ORIGINAL_HANDLER_CHANGED")
    library = profile / ".media/library.json"
    library.parent.mkdir(parents=True)
    library.write_bytes(seed_bytes)
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR"}}
    environment.update(DEEPSEEK_INFRA_ROOT=str(profile), DEEPSEEK_RUNTIME_MODE="python_authoritative", AUTH_DISABLED="0",
                       PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-B", "-c", JSON_ERROR_HTTP_ORACLE, str(profile)], cwd=ROOT, env=environment,
        input=json.dumps(corpus["cases"]).encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, check=False)
    require(len(result.stdout) <= LIMIT and len(result.stderr) <= LIMIT, "PATCH_JSON_ORIGINAL_HTTP_OUTPUT_EXCEEDED_BOUND")
    (out / "patch-json-original-http.log").write_bytes(result.stderr)
    require(result.returncode == 0, "PATCH_JSON_ORIGINAL_HTTP_REPLAY_FAILED")
    observed = json.loads(result.stdout.splitlines()[-1])
    require(observed["status"] == "PASS" and observed["originalTCP"] and observed["cases"] == len(corpus["cases"])
            and observed["requests"] == corpus["originalRequests"] and observed["python"].startswith("3.12.")
            and observed["unchangedStoreSha256"] == corpus["seedSha256"], "PATCH_JSON_ORIGINAL_HTTP_REPORT_DIFFERS")
    return observed


def verify_native_patch_json_errors(scratch: Path, profile: Path, origin: str, token: str,
                                    cases: list[dict[str, Any]], report: dict[str, Any]) -> None:
    library = profile / ".media/library.json"
    path = "/api/media/" + cases[0]["mediaId"]
    status, baseline, _ = exchange(origin, path, "GET", token)
    require(status == 200, "PATCH_JSON_NATIVE_SEED_READ_FAILED")
    initial = library.read_bytes()
    generation = generation_observation(profile)
    observed: dict[str, Any] = {}
    report["nativeMalformedPatchCases"] = observed
    for case in cases:
        label = case["name"]
        require(path == "/api/media/" + case["mediaId"], "PATCH_JSON_CASE_MEDIA_ID_DIFFERS")
        report.update(nativePatchRequestStarted=True, nativePatchReplyObserved=False)
        status, raw, _, operation = exchange_with_operation(origin, path, "PATCH", token, patch_input_request(case))
        report.update(nativePatchReplyObserved=True, nativePatchHTTPStatus=status)
        expected = json.loads(case["bodyJson"])
        observed[label] = dict(status=status, expectedStatus=case["status"], responseSha256=hashlib.sha256(raw).hexdigest())
        require(status == case["status"] == 400 and json.loads(raw) == expected and not operation,
                "PATCH_JSON_NATIVE_DIAGNOSTIC_DIFFERS_" + label)
        get_status, get_raw, _ = exchange(origin, path, "GET", token)
        require(get_status == 200 and get_raw == baseline, "PATCH_JSON_NATIVE_READ_CHANGED_" + label)
        require(library.read_bytes() == initial and generation_observation(profile) == generation, "PATCH_JSON_NATIVE_STORE_CHANGED_" + label)
    control = diagnostic_table(profile, "control.sqlite3", "SELECT count(*) FROM control_media_operations", ("operationCount",))
    journal = journal_observation(scratch, allow_empty=True)
    require(control == {"status": "OBSERVED", "rows": [{"operationCount": 0}]}
            and journal == {"state": "EMPTY", "operationCount": 0}, "PATCH_JSON_NATIVE_REJECTION_ADMITTED_OPERATION")
    report.update(nativeMalformedPatchDiagnosticsQualified=True, malformedPatchNativeRequests=1 + 2 * len(cases),
                  malformedPatchGoOperations=0, malformedPatchRustOperations=0)


def run(args: argparse.Namespace, report: dict[str, Any]) -> None:
    require(os.environ.get("GITHUB_ACTIONS") == "true" and os.name == "posix",
            "NATIVE_MEDIA_PATCH_VERIFICATION_IS_CLOUD_ONLY")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    require(revision == os.environ.get("GITHUB_SHA"), "CLOUD_SOURCE_REVISION_DIFFERS")
    require(not subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT),
            "CLOUD_TRACKED_SOURCE_IS_DIRTY")
    report["sourceCommit"] = revision
    fixture = json.loads(FIXTURE.read_text())
    require(fixture["schemaVersion"] == 1 and len(fixture["media"]) == 18, "MEDIA_PATCH_FIXTURE_CONTRACT_DIFFERS")
    report["fixtureSha256"] = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    capacity_case = bool(getattr(args, "large_library_case", False))
    large_record_case = bool(getattr(args, "large_record_case", False))
    fixture_media = large_library_media(fixture["media"]) if capacity_case else fixture["media"]
    if large_record_case:
        # The library is small; the original selected record alone exceeds both
        # old inline signing/transport bounds. The public PATCH body stays small.
        fixture_media = large_record_media(fixture_media)
    report["patchSha256"] = hashlib.sha256(json.dumps(PATCH, sort_keys=True).encode()).hexdigest()
    request_body = wire_payload(PATCH)
    require(len(request_body) <= 1_000_000, "MEDIA_PATCH_FIXTURE_EXCEEDS_PUBLIC_LIMIT")
    request_body_sha256 = hashlib.sha256(request_body).hexdigest()
    report["requestBodySha256"] = request_body_sha256
    report["requestBodyBytes"] = len(request_body)
    bootstrap.require_loopback_ports_available()
    with tempfile.TemporaryDirectory(prefix="native-media-patch-") as temporary:
        scratch = Path(temporary)
        binaries, profile, mirror = scratch / "bin", scratch / "profile", scratch / "oracle"
        binaries.mkdir()
        (scratch / "go-tmp").mkdir()
        environment = signing.cache_environment(ROOT,
            env=signing.child_environment(dict(os.environ)), target_dir=args.go_cache)
        environment.update(CGO_ENABLED="0", GOTOOLCHAIN="local", GOMAXPROCS="2", GOPROXY="off",
            GOSUMDB="off", GOWORK="off", GOCACHE=str(args.go_cache.resolve()), GOTMPDIR=str(scratch / "go-tmp"))
        require(subprocess.check_output(["go", "version"], env=environment, text=True).startswith(
            "go version " + signing.GO_VERSION + " "), "NATIVE_GO_TOOLCHAIN_PIN_DIFFERS")
        for name in ("deepseekd", "deepseek-launch"):
            signing.record_command(["go", "build", "-p=2", "-o", str(binaries / name), "./cmd/" + name],
                ROOT / "go", environment, args.out, "build-" + name, timeout=600)
        for name, source in (("deepseek-worker", args.worker), ("deepseek-gateway", args.edge)):
            (binaries / name).symlink_to(source.resolve(strict=True))
        require(not profile.exists() and (ROOT / "static/ui/index.html").is_file(),
                "MEDIA_PATCH_PROFILE_IS_NOT_FRESH_OR_FRONTEND_MISSING")
        report.update(nativeBuildsPassed=True, profileInitiallyAbsent=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            public_port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{public_port}"
        launch_env = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR",
            "DEEPSEEK_MEDIA_PHASE_TIMING"}}
        launch_env.update(DEEPSEEK_NATIVE_BIN=str(binaries), DEEPSEEK_INFRA_ROOT=str(profile),
            DEEPSEEK_INFRA_STATIC_DIR=str(ROOT / "static"))
        child: subprocess.Popen[bytes] | None = None
        identities: list[dict[str, Any]] = []

        def launch(label: str) -> str:
            nonlocal child, identities
            with (args.out / (label + ".log")).open("wb") as log:
                child = subprocess.Popen([str(binaries / "deepseek-launch"), "--server", "--no-prompt",
                    "--port", str(public_port)], cwd=scratch, env=launch_env, stdout=log, stderr=subprocess.STDOUT)
            bootstrap.wait_public(origin, child)
            rows = bootstrap.process_roles(child.pid)
            identities = [{**row, "startTicks": reads.process_identity(row["pid"])} for row in rows]
            require(all(row["startTicks"] is not None for row in identities), "MEDIA_PATCH_NATIVE_PROCESS_DISAPPEARED")
            report[label + "Processes"] = identities
            token_path = profile / ".auth-token"
            require(token_path.is_file() and not token_path.is_symlink() and token_path.stat().st_size <= 4096,
                    "MEDIA_PATCH_LOCAL_AUTH_FILE_INVALID")
            token = token_path.read_text().strip()
            require(len(token) == 64 and all(char in "0123456789abcdef" for char in token),
                    "MEDIA_PATCH_LOCAL_AUTH_TOKEN_INVALID")
            status, raw, _ = exchange(origin, "/api/control/status", "GET", token)
            require(status == 200 and json.loads(raw).get("ok") is True, "MEDIA_PATCH_CONTROL_NOT_HEALTHY")
            return token

        def shutdown() -> None:
            nonlocal child, identities
            if child is not None:
                reads.stop(child, identities)
                child, identities = None, []

        try:
            launch("empty-launch")
            require(reads.media_snapshot(profile) is None, "FRESH_NATIVE_LAUNCH_CREATED_MEDIA")
            initial_writer = reads.writer(profile)
            shutdown()
            original_auth = (profile / ".auth-token").read_bytes()
            if getattr(args, "patch_input_cases", False):
                library = profile / ".media/library.json"
                require(child is None and not library.parent.exists(), "PATCH_INPUT_SEED_PROFILE_NOT_FRESH")
                raw_corpus = PATCH_INPUT_FIXTURE.read_bytes()
                require(hashlib.sha256(raw_corpus).hexdigest() == PATCH_INPUT_FIXTURE_SHA256,
                        "PATCH_INPUT_ORIGINAL_CORPUS_FIXTURE_CHANGED")
                corpus = json.loads(raw_corpus)
                cases = corpus["cases"]
                require(corpus["schemaVersion"] == 1 and corpus["authEnabled"] and corpus["actualOriginalTCP"]
                        and corpus["originalRequests"] == 20 and len(cases) == 10,
                        "PATCH_INPUT_ORIGINAL_CORPUS_CONTRACT_DIFFERS")
                seed_bytes = base64.b64decode(corpus["seedBase64"], validate=True)
                require(hashlib.sha256(seed_bytes).hexdigest() == corpus["seedSha256"], "PATCH_INPUT_SEED_BYTES_DIFFER")
                error_fixture = PATCH_JSON_ERROR_FIXTURE.read_bytes()
                require(hashlib.sha256(error_fixture).hexdigest() == PATCH_JSON_ERROR_FIXTURE_SHA256, "PATCH_JSON_FIXTURE_CHANGED")
                error_corpus = json.loads(error_fixture)
                require(error_corpus["schemaVersion"] == 1 and error_corpus["seedSha256"] == corpus["seedSha256"]
                        and error_corpus["originalSourceCommit"] == corpus["originalSourceCommit"]
                        and error_corpus["originalSourceTree"] == corpus["originalSourceTree"]
                        and len(error_corpus["cases"]) == 36, "PATCH_JSON_ORIGINAL_CORPUS_CONTRACT_DIFFERS")
                report["originalMalformedPatchHTTP"] = verify_original_patch_json_errors(mirror, seed_bytes, error_corpus, args.out)
                report["originalMalformedPatchCorpusSha256"] = PATCH_JSON_ERROR_FIXTURE_SHA256
                library.parent.mkdir()
                library.write_bytes(seed_bytes)
                report.update(offlineMediaFixtureSeeded=True, semanticStageReached=True,
                              patchInputFixtureSha256=PATCH_INPUT_FIXTURE_SHA256,
                              originalPatchInputCorpusSha256=corpus["originalCorpusSha256"],
                              originalPatchInputProofSha256=corpus["originalInputProofSha256"],
                              originalPatchInputSourceCommit=corpus["originalSourceCommit"],
                              originalPatchInputSourceTree=corpus["originalSourceTree"],
                              patchInputSeedSha256=corpus["seedSha256"])
                token = launch("patch-input-launch")
                require(library.read_bytes() == seed_bytes, "PATCH_INPUT_LAUNCH_CHANGED_SEED")
                verify_native_patch_json_errors(scratch, profile, origin, token, error_corpus["cases"], report)
                verify_patch_inputs(scratch, profile, origin, token, cases, report)
                retained_bytes = library.read_bytes()
                retained_generation = generation_observation(profile)
                shutdown()
                token = launch("patch-input-restart")
                require((profile / ".auth-token").read_bytes() == original_auth, "PATCH_INPUT_RESTART_REPLACED_AUTH")
                for case in cases:
                    verify_patch_input_read(origin, token, case)
                    receipt = report["patchInputCases"][case["name"]].get("receipt")
                    if receipt:
                        require(retained_mutation_receipt(scratch, profile, receipt["operationId"], receipt["state"],
                            receipt["beforeSha256"], receipt["afterSha256"], receipt["schemaVersion"]) == receipt,
                            "PATCH_INPUT_RECEIPT_CHANGED_AFTER_RESTART_" + case["name"])
                require(library.read_bytes() == retained_bytes and generation_observation(profile) == retained_generation,
                        "PATCH_INPUT_RESTART_CHANGED_LIBRARY_OR_GENERATION")
                report.update(status="PASS", nativePatchInputCasesQualified=True, mediaRetainedAfterLauncherRestart=True,
                              mediaEffectJournalRetained=True, workspaceGenerationFenced=True, workspaceGenerationRetainedAfterRestart=True)
                return
            if getattr(args, "retained_mutation_cases", False):
                library = profile / ".media/library.json"
                require(child is None and not library.parent.exists(), "RETAINED_MUTATION_SEED_PROFILE_NOT_FRESH")
                library.parent.mkdir()
                rows = [
                    {"mediaId": "media_target", "type": "image", "title": "Before"},
                    {"mediaId": "media_unpaired", "type": "image", "metadata": {"text": "\ud800"}},
                    {"mediaId": "media_nonfinite", "type": "image", "metadata": {"f": float("nan")}},
                    {"mediaId": "media_exact", "type": "image", "metadata": {"wide": RETAINED_EXACT_INTEGER}},
                    {"mediaId": "media_deep", "type": "image", "metadata": {"value": retained_deep_value()}},
                ]
                seed_bytes = (json.dumps({"media": rows}, ensure_ascii=True) + "\n").encode()
                require(hashlib.sha256(seed_bytes).hexdigest() == RETAINED_MUTATION_SEED_SHA256,
                        "RETAINED_MUTATION_SEED_DIFFERS_FROM_ORIGINAL_ORACLE")
                library.write_bytes(seed_bytes)
                report.update(offlineMediaFixtureSeeded=True, semanticStageReached=True,
                              originalMutationOracleSha256=RETAINED_MUTATION_ORACLE_SHA256,
                              originalCombinedMutationOracleSha256=RETAINED_COMBINED_ORACLE_SHA256,
                              retainedMutationSeedSha256=RETAINED_MUTATION_SEED_SHA256)
                token = launch("retained-mutation-launch")
                verify_retained_mutations(scratch, profile, origin, token, report)
                retained_bytes = library.read_bytes()
                retained_generation = generation_observation(profile)
                shutdown()
                token = launch("retained-mutation-restart")
                require((profile / ".auth-token").read_bytes() == original_auth,
                        "RETAINED_MUTATION_RESTART_REPLACED_AUTH")
                for media_id, wanted_status in (
                    ("media_unpaired", 404), ("media_nonfinite", 500), ("media_exact", 200), ("media_deep", 200),
                ):
                    status, raw, _ = exchange(origin, "/api/media/" + media_id, "GET", token)
                    require(status == wanted_status, "RETAINED_MUTATION_GET_AFTER_RESTART_DIFFERS")
                    if media_id == "media_exact":
                        require(json.loads(raw)["media"]["metadata"]["wide"] == RETAINED_EXACT_INTEGER,
                                "RETAINED_MUTATION_INTEGER_CHANGED_AFTER_RESTART")
                    if media_id == "media_deep":
                        require(json.loads(raw)["media"]["metadata"]["value"] == retained_deep_value(),
                                "RETAINED_MUTATION_DEEP_VALUE_CHANGED_AFTER_RESTART")
                for case in report["retainedMutationCases"].values():
                    if receipt := case.get("receipt"):
                        require(retained_mutation_receipt(scratch, profile, receipt["operationId"], receipt["state"],
                            receipt["beforeSha256"], receipt["afterSha256"], receipt["schemaVersion"]) == receipt,
                            "RETAINED_MUTATION_RECEIPT_CHANGED_AFTER_RESTART")
                require(library.read_bytes() == retained_bytes and generation_observation(profile) == retained_generation,
                        "RETAINED_MUTATION_RESTART_CHANGED_LIBRARY_OR_GENERATION")
                report.update(status="PASS", nativeRetainedMutationCasesQualified=True,
                              mediaRetainedAfterLauncherRestart=True, mediaEffectJournalRetained=True,
                              workspaceGenerationFenced=True, workspaceGenerationRetainedAfterRestart=True)
                return
            if capacity_case:
                require(child is None and not identities, "MEDIA_CAPACITY_SEEDED_WITH_NATIVE_RUNNING")
            seed(profile, fixture_media)
            seed(mirror, fixture_media)
            report["offlineMediaFixtureSeeded"] = True
            if capacity_case:
                seeded = profile / ".media/library.json"
                require(seeded.stat().st_size > 128 * 1024**2 and seeded.stat().st_size < 160 * 1024**2,
                        "MEDIA_CAPACITY_FIXTURE_BYTES_DIFFER")
                report["largeLibraryFixture"] = {"originalFixtureRecords": 18, "additionalRecords": 600,
                    "metadataCharactersPerRecord": 240_000, "fileBytes": seeded.stat().st_size,
                    "fileSha256": hashlib.sha256(seeded.read_bytes()).hexdigest(),
                    "onlySeededWhileNativeStopped": True, "newProductLimit": False}
            before = reads.media_snapshot(profile)
            oracle_clock = time.monotonic()
            expected_status, expected = original_http_patch(mirror, args.out / "original-http-oracle.log")
            if capacity_case:
                report["originalHTTPRequestSeconds"] = time.monotonic() - oracle_clock
            require(bool(args.field_case) or expected_status == 200, "ORIGINAL_MEDIA_PATCH_WORKLOAD_DID_NOT_SUCCEED")
            require(reads.media_snapshot(profile) == before, "ORACLE_CHANGED_NATIVE_MEDIA_PROFILE")
            (args.out / "original-patch-oracle.json").write_text(
                json.dumps({"expectedPublicHTTPStatus": expected_status, "originalLibrarySucceeded": expected_status == 200,
                    "actualOriginalPublicRouteCompared": True,
                    "body": expected}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report.update(originalLibrarySucceeded=expected_status == 200, expectedPublicHTTPStatus=expected_status,
                          actualOriginalPublicRouteCompared=True)
            if capacity_case:
                report["originalLargeLibraryRetention"] = capacity_original_observation(mirror, args.out / "original-capacity-oracle.log")
            token = launch("patch-launch")
            require((profile / ".auth-token").read_bytes() == original_auth, "MEDIA_PATCH_RESTART_REPLACED_AUTH")
            current_writer = reads.writer(profile)
            require(current_writer[0] == initial_writer[0] + 1 and current_writer[1] != initial_writer[1],
                    "MEDIA_PATCH_RESTART_DID_NOT_ADVANCE_WRITER")
            status, raw, _ = exchange(origin, "/api/media/" + MEDIA_ID, "GET", token)
            require(status == 200 and json.loads(raw).get("media", {}).get("title") == "Notes",
                    "NATIVE_MEDIA_PATCH_PREREQUISITE_READ_FAILED")
            original_record = json.loads(raw)
            generation_before = generation_observation(profile)
            require(exchange(origin, "/api/media/" + MEDIA_ID, "PATCH", None, PATCH)[0] == 401,
                    "UNAUTHENTICATED_NATIVE_MEDIA_PATCH_WAS_NOT_DENIED")
            require(reads.media_snapshot(profile) == before, "MEDIA_CHANGED_BEFORE_AUTHENTICATED_PATCH")
            require(generation_observation(profile) == generation_before,
                    "UNAUTHENTICATED_PATCH_CHANGED_WORKSPACE_GENERATION")
            request_identities = [{**row, "startTicks": reads.process_identity(row["pid"])} for row in identities]
            if os.environ.get("DEEPSEEK_MEDIA_PHASE_TIMING") == "1":
                report["mediaPhaseTimingEnabledInWorker"] = phase_timing_environment(request_identities)
            report.update(controlHealthyBeforePatch=True, originalMediaReadBeforePatch=True,
                unauthenticatedPatchDenied=True, nativePatchRequestStarted=True,
                nativePatchOutcome="EFFECT_UNKNOWN")
            resources_before = uncertain_request_snapshot(profile, request_identities)
            if capacity_case:
                report["capacityMemoryBefore"] = capacity_peak_memory(request_identities)
            started = datetime.now(timezone.utc).timestamp()
            request_clock = time.monotonic()
            try:
                status, raw, content_type = exchange(origin, "/api/media/" + MEDIA_ID, "PATCH", token, PATCH)
            except Exception:
                elapsed = time.monotonic() - request_clock
                report["uncertainRequestDiagnostics"] = {"before": resources_before,
                    "after": uncertain_request_snapshot(profile, request_identities),
                    "elapsedSeconds": elapsed,
                    "requestRepeated": False, "outcomeStillUnknown": True}
                (args.out / "uncertain-request-diagnostics.json").write_text(
                    json.dumps(report["uncertainRequestDiagnostics"], indent=2) + "\n", encoding="utf-8")
                raise
            finished = datetime.now(timezone.utc).timestamp()
            if capacity_case:
                report["capacityRequestSeconds"] = time.monotonic() - request_clock
            generation_after = generation_observation(profile)
            (args.out / "native-patch-response.txt").write_bytes(raw)
            observation = {"sourceCommit": revision, "runId": os.environ.get("GITHUB_RUN_ID"),
                "method": "PATCH", "path": "/api/media/" + MEDIA_ID, "startedUnix": started,
                "finishedUnix": finished, "status": status, "contentType": content_type,
                "responseSha256": hashlib.sha256(raw).hexdigest(), "requestRepeated": False,
                "requestBodySha256": request_body_sha256,
                "generationBefore": generation_before, "generationAfter": generation_after,
                "resourcesBefore": resources_before, "resourcesAfter": uncertain_request_snapshot(profile, request_identities)}
            (args.out / "native-patch-observation.json").write_text(
                json.dumps(observation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report.update(semanticStageReached=True, nativePatchReplyObserved=True, nativePatchHTTPStatus=status,
                responseSha256=hashlib.sha256(raw).hexdigest(), responseContentType=content_type,
                mediaStoreUnchangedAfterReply=reads.media_snapshot(profile) == before)
            if capacity_case:
                # The actual reply is durably recorded before diagnostics; a
                # disappearing process must not hide the original HTTP result.
                try:
                    report["capacityMemoryAfter"] = capacity_peak_memory(request_identities)
                except Exception as error:
                    report["capacityMemoryObservationError"] = type(error).__name__
            if expected_status == 400:
                require(status == 400 and json.loads(raw) == expected, "NATIVE_MEDIA_PATCH_REFUSAL_DIFFERS_FROM_ORIGINAL_ROUTE")
                require(reads.media_snapshot(profile) == before and generation_after == generation_before,
                        "REFUSED_MEDIA_PATCH_CHANGED_STORE_OR_GENERATION")
                empty_journal = journal_observation(scratch, allow_empty=True)
                require(empty_journal == {"state": "EMPTY", "operationCount": 0}, "REFUSED_MEDIA_PATCH_CREATED_EFFECT")
                shutdown()
                token = launch("retained-restart")
                require((profile / ".auth-token").read_bytes() == original_auth, "MEDIA_PATCH_FINAL_RESTART_REPLACED_AUTH")
                retained_status, retained_raw, _ = exchange(origin, "/api/media/" + MEDIA_ID, "GET", token)
                require(retained_status == 200 and json.loads(retained_raw) == original_record,
                        "REFUSED_MEDIA_PATCH_CHANGED_RETAINED_RECORD")
                require(reads.media_snapshot(profile) == before and generation_observation(profile) == generation_before,
                        "REFUSED_MEDIA_PATCH_RESTART_CHANGED_STORE_OR_GENERATION")
                require(journal_observation(scratch, allow_empty=True) == empty_journal,
                        "REFUSED_MEDIA_PATCH_RESTART_CREATED_EFFECT")
                (args.out / "retained-media-response.txt").write_bytes(retained_raw)
                report.update(status="PASS", originalResponseMatched=True, originalPublicFieldAllowlistRetained=True,
                              nativePatchOutcome="REFUSED_WITHOUT_EFFECT", originalRefusalMatched=True,
                              refusedPatchStoreAndGenerationPreserved=True, refusedPatchEffectJournalEmpty=True,
                              mediaRetainedAfterLauncherRestart=True, internalLibraryFieldsMigrated=False)
                shutdown()
                return
            require(status == 200, "NATIVE_MEDIA_PATCH_REFUSED")
            require(not capacity_case or "capacityMemoryObservationError" not in report,
                    "MEDIA_CAPACITY_MEMORY_NOT_QUALIFIED")
            require(generation_after["present"] and generation_after["value"] == generation_before["value"] + 2,
                    "NATIVE_MEDIA_PATCH_DID_NOT_FENCE_CROSSING_BACKUPS")
            actual = json.loads(raw)
            require(without_update_time(actual) == without_update_time(expected), "NATIVE_MEDIA_PATCH_DIFFERS_FROM_ORIGINAL")
            stamp = actual["media"].get("updatedAt")
            require(isinstance(stamp, str) and len(stamp) <= 64, "NATIVE_MEDIA_PATCH_TIMESTAMP_INVALID")
            instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            require(instant.tzinfo is not None and started - 2 <= instant.timestamp() <= finished + 2,
                    "NATIVE_MEDIA_PATCH_TIMESTAMP_OUTSIDE_REQUEST")
            effect_after = journal_observation(scratch)
            observation["effectBeforeRestart"] = effect_after
            if large_record_case:
                public_record = actual.get("media", {})
                require(len(json.dumps(public_record).encode()) > 4 * 1024**2,
                        "MEDIA_PATCH_LARGE_RECORD_DID_NOT_CROSS_INLINE_BOUNDS")
                require(all(public_record.get("metadata", {}).get(f"retained_large_{i:02}") == "r" * 120_000
                            for i in range(36)), "MEDIA_PATCH_LARGE_RECORD_DATA_DIFFERS")
                observed = compact_patch_observation(scratch)
                require(observed["schemaVersion"] == 2 and observed["outcomeBytes"] < 4096
                        and observed["signatureBytes"] == 64 and observed["recordBytes"] > 4 * 1024**2,
                        "MEDIA_PATCH_LARGE_RECORD_DID_NOT_USE_BOUND_COMPACT_RECEIPT")
                report["compactPatchReceipt"] = observed
                report["allLargeRecordMetadataReturned"] = True
            if report.get("metadataOrderCase") == "maximum-array":
                require(effect_after["claimBytes"] > 2 * 1024**2,
                        "LARGE_MEDIA_INPUT_DID_NOT_CROSS_TYPED_NATIVE_BOUNDARY")
            capacity_retained = reads.media_snapshot(profile) if capacity_case else None
            shutdown()
            if capacity_case:
                report["nativeLargeLibraryBeforeRestart"] = capacity_original_observation(profile, args.out / "native-capacity-before-restart.log")
                require(report["nativeLargeLibraryBeforeRestart"]["normalizedRecords"] == report["originalLargeLibraryRetention"]["normalizedRecords"],
                        "NATIVE_MEDIA_CAPACITY_RECORD_COUNT_DIFFERS")
                require(reads.media_snapshot(profile) == capacity_retained, "MEDIA_CAPACITY_ORACLE_CHANGED_NATIVE_BYTES")
            token = launch("retained-restart")
            require((profile / ".auth-token").read_bytes() == original_auth, "MEDIA_PATCH_FINAL_RESTART_REPLACED_AUTH")
            status, raw, _ = exchange(origin, "/api/media/" + MEDIA_ID, "GET", token)
            (args.out / "retained-media-response.txt").write_bytes(raw)
            generation_retained = generation_observation(profile)
            effect_retained = journal_observation(scratch)
            observation.update(retainedGetStatus=status, retainedResponseSha256=hashlib.sha256(raw).hexdigest(),
                generationRetained=generation_retained, effectAfterRestart=effect_retained)
            (args.out / "native-patch-observation.json").write_text(
                json.dumps(observation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            require(status == 200 and json.loads(raw) == actual, "NATIVE_MEDIA_PATCH_NOT_RETAINED_AFTER_RESTART")
            require(generation_retained == generation_after, "MEDIA_READ_OR_RESTART_CHANGED_GENERATION")
            require(effect_retained == effect_after, "MEDIA_EFFECT_IDENTITY_CHANGED_AFTER_RESTART")
            shutdown()
            if capacity_case:
                require(reads.media_snapshot(profile) == capacity_retained, "MEDIA_CAPACITY_RESTART_CHANGED_RETAINED_BYTES")
                report["nativeLargeLibraryAfterRestart"] = capacity_original_observation(profile, args.out / "native-capacity-after-restart.log")
                require(report["nativeLargeLibraryAfterRestart"]["normalizedRecords"] == report["originalLargeLibraryRetention"]["normalizedRecords"],
                        "NATIVE_MEDIA_CAPACITY_RESTART_RECORD_COUNT_DIFFERS")
                require(reads.media_snapshot(profile) == capacity_retained, "MEDIA_CAPACITY_ORACLE_CHANGED_NATIVE_BYTES")
                report["allCapacityRecordsAndBytesRetainedAcrossRestart"] = True
            report.update(status="PASS", controlledNativeMediaPatchWorkloadQualified=True,
                originalResponseMatched=True, updatedAtWithinRequest=True, mediaRetainedAfterLauncherRestart=True,
                workspaceGenerationFenced=True, workspaceGenerationRetainedAfterRestart=True,
                mediaEffectJournalRetained=True,
                nativePatchOutcome="PUBLIC_SUCCESS_RETAINED_AUTHORITY_PROOF_NOT_ASSESSED")
        finally:
            shutdown()


def main() -> int:
    global PATCH, LIMIT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--edge", type=Path, required=True)
    parser.add_argument("--go-cache", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--metadata-order-case", choices=["baseline", "top-level", "nested", "array", "truncated", "maximum-array"],
                        default="baseline")
    parser.add_argument("--field-case", choices=tuple(FIELD_PATCHES))
    parser.add_argument("--large-library-case", action="store_true", help="Generate a legal144MB isolated library on the cloud runner only")
    parser.add_argument("--large-record-case", action="store_true", help="Exercise a retained legal record larger than4MiB through actual native HTTP")
    parser.add_argument("--retained-mutation-cases", action="store_true",
                        help="Exercise retained record writes and signed HTTP 500 receipts")
    parser.add_argument("--patch-input-cases", action="store_true",
                        help="Replay the original authenticated HTTP input corpus, using the exact request bytes")
    args = parser.parse_args()
    require(not args.field_case or args.metadata_order_case == "baseline", "MEDIA_PATCH_CASE_SELECTION_CONFLICT")
    require(not args.large_library_case or not args.field_case and args.metadata_order_case == "baseline",
            "MEDIA_PATCH_CAPACITY_CASE_SELECTION_CONFLICT")
    require(not args.large_record_case or not args.large_library_case and not args.field_case
            and args.metadata_order_case == "baseline", "MEDIA_PATCH_LARGE_RECORD_CASE_SELECTION_CONFLICT")
    require(not (args.retained_mutation_cases or args.patch_input_cases) or not args.large_record_case and not args.large_library_case
            and not args.field_case and args.metadata_order_case == "baseline",
            "MEDIA_PATCH_CORPUS_CASE_SELECTION_CONFLICT")
    require(not (args.retained_mutation_cases and args.patch_input_cases), "MEDIA_PATCH_CORPUS_CASE_SELECTION_CONFLICT")
    # Only this new retained-record workload needs a larger report/read bound.
    # Existing cases retain their original 2MiB response/report constraint.
    LIMIT = (8 if args.large_record_case else 2) * 1024**2
    PATCH = FIELD_PATCHES[args.field_case] if args.field_case else case_patch(args.metadata_order_case)
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"kind": "native-media-patch-process-v1", "status": "FAIL",
        "sourceCommit": os.environ.get("GITHUB_SHA"), "runId": os.environ.get("GITHUB_RUN_ID"),
        "event": os.environ.get("GITHUB_EVENT_NAME"), "semanticStageReached": False,
        "metadataOrderCase": args.metadata_order_case,
        "fieldCase": args.field_case,
        "largeLibraryCase": args.large_library_case,
        "largeRecordCase": args.large_record_case,
        "nativePatchRequestStarted": False, "nativePatchReplyObserved": False,
        "controlledNativeMediaPatchWorkloadQualified": False, "offlineMediaFixtureSeeded": False,
        "workspaceGenerationFenced": False, "workspaceGenerationRetainedAfterRestart": False,
        "mediaEffectJournalRetained": False,
        "mediaPatchAuthorityRecoveryQualified": False, "allMediaWritesQualified": False,
        "mediaMigrationQualified": False, "mediaGUIQualified": False,
        "fullMigrationQualified": False, "readiness": "NOT_READY"}
    try:
        run(args, report)
    except Exception as error:
        report.update(status="FAIL", controlledNativeMediaPatchWorkloadQualified=False,
            failureCode=str(error) if isinstance(error, (ProbeFailure, reads.ProbeFailure, bootstrap.ProbeFailure))
                else type(error).__name__)
        if report["nativePatchRequestStarted"] and not report["nativePatchReplyObserved"]:
            report["nativePatchOutcome"] = "EFFECT_UNKNOWN"
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in (
        "status", "sourceCommit", "failureCode", "semanticStageReached", "nativePatchHTTPStatus")}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
