"""Cloud-only multipart HTTP workloads against an independent original ASGI app.

The source parser and subprocess oracle are offline verification tools. They are
never called by the native production request path. Processing/OCR remains a
separate required capability; these workloads exercise process=false ingestion.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, build_opener

import verify_native_media_create as create

BOUNDARY = "native-media-upload-original-contract"
ORACLE = r'''
import hashlib,json,sys
from pathlib import Path
from fastapi.testclient import TestClient
from deepseek_infra.web.server import create_app,settings
from deepseek_infra.infra.media import library
client=TestClient(create_app(),base_url="http://127.0.0.1")
try:
    response=client.post("/api/media"+sys.argv[1],content=sys.stdin.buffer.read(),headers={
        "Authorization":"Bearer "+settings.auth.token,"Content-Type":sys.argv[2],
        "X-DeepSeek-Request-ID":"native-media-create-baseline"})
    path=library.media_dir()/"library.json"
    records=json.loads(path.read_bytes())["media"] if path.exists() else []
    hashes=[hashlib.sha256((library.media_dir()/record["path"]).read_bytes()).hexdigest() for record in records]
    print(json.dumps(dict(status=response.status_code,body=response.json(),storedMedia=records,sourceHashes=hashes,
        requestId=response.headers.get("X-DeepSeek-Request-ID"))))
finally:
    client.close()
'''


def field(name: str, value: str, charset: str = "utf-8") -> tuple[str, bytes, str]:
    return name, value.encode(charset), charset


def multipart(files: list[tuple[str, str, bytes]], fields: list[tuple[str, bytes, str]]) -> tuple[bytes, str]:
    pieces = []
    for name, data, charset in fields:
        pieces.append((f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n'
                       f'Content-Type: text/plain; charset={charset}\r\n\r\n').encode() + data + b"\r\n")
    for filename, mime, data in files:
        # Backslashes remain literal to exercise the original browser fakepath
        # cleanup; these controlled fixture names contain no quote or newline.
        create.require(not any(c in filename for c in ['"', "\r", "\n"]), "MEDIA_UPLOAD_FIXTURE_FILENAME_INVALID")
        pieces.append((f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\n'
                       f'Content-Type: {mime}\r\n\r\n').encode() + data + b"\r\n")
    pieces.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(pieces), "multipart/form-data; boundary=" + BOUNDARY


def original(profile: Path, diagnostic: Path, raw: bytes, content_type: str, query: str) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR",
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP"}}
    env.update(DEEPSEEK_INFRA_ROOT=str(profile), LOCAL_RAG_ENABLED="false", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    result = subprocess.run([sys.executable, "-B", "-c", ORACLE, query, content_type], cwd=create.ROOT, env=env,
                            input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180, check=False)
    create.require(len(result.stderr) <= create.LIMIT, "MEDIA_UPLOAD_ORACLE_DIAGNOSTIC_BOUND")
    diagnostic.write_bytes(result.stderr)
    create.require(result.returncode == 0 and len(result.stdout) <= create.LIMIT, "MEDIA_UPLOAD_ORIGINAL_ORACLE_FAILED")
    return dict(json.loads(result.stdout))


def exchange(origin: str, token: str, raw: bytes, content_type: str, query: str) -> tuple[int, dict[str, Any], dict[str, str]]:
    headers = {"Content-Type": content_type, "Authorization": "Bearer " + token, "X-DeepSeek-Request-ID": create.REQUEST_ID}
    request = Request(origin + "/api/media" + query, method="POST", headers=headers, data=raw)
    try:
        response = build_opener(create.NoRedirect()).open(request, timeout=660)
    except HTTPError as error:
        response = error
    with response:
        body = response.read(create.LIMIT + 1)
        create.require(len(body) <= create.LIMIT, "MEDIA_UPLOAD_NATIVE_RESPONSE_BOUND")
        parsed = json.loads(body)
        create.require(isinstance(parsed, dict), "MEDIA_UPLOAD_NATIVE_RESPONSE_NOT_OBJECT")
        return response.status, parsed, {key.lower(): value for key, value in response.headers.items()}


def library_records(profile: Path) -> list[dict[str, Any]]:
    path = profile / ".media/library.json"
    if not path.exists():
        return []
    return list(json.loads(path.read_bytes())["media"])


def canonical_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [create.canonical_media(record, Path(record["path"]).suffix) for record in records]


def error_code(body: dict[str, Any]) -> Any:
    error = body.get("error")
    return body.get("code") or (error.get("code") if isinstance(error, dict) else None)


def require_success(body: dict[str, Any], expected_count: int) -> list[dict[str, Any]]:
    create.require(body.get("ok") is True and isinstance(body.get("mediaItems"), list), "MEDIA_UPLOAD_SUCCESS_SHAPE_DIFFERS")
    records = body["mediaItems"]
    create.require(len(records) == expected_count and expected_count > 0, "MEDIA_UPLOAD_RESULT_COUNT_DIFFERS")
    create.require(create.same_json(body.get("media"), records[0]), "MEDIA_UPLOAD_FIRST_RESULT_DIFFERS")
    create.require(len({record.get("mediaId") for record in records}) == expected_count, "MEDIA_UPLOAD_DUPLICATE_ID")
    return list(records)


def cases() -> Any:
    mixed = [("photo.PNG", "image/png", b"\x00opaque image\xff"), ("scan.pdf", "application/pdf", b"opaque pdf"),
             ("speech.mp3", "audio/mpeg", b"opaque audio"), ("clip.mp4", "video/mp4", b"opaque video"),
             ("page.HTML", "text/html", b"  <h1>retained raw whitespace</h1>\n  ")]
    yield "five-upload-kinds", mixed, [field("title", "ignored batch title")], "?projectId=first&projectId=project-query", 200, 5
    yield "single-first-fields", mixed[:1], [field("projectId", " project-form "), field("projectId", "project-later"),
        field("title", "  retained title  "), field("process", "0"), field("process", "false")], "", 200, 1
    yield "filename-cleaning", [("C:\\fakepath\\photo.PNG", "image/png", b"fakepath"),
        ("folder/", "image/png", b"trailing slash")], [], "", 200, 2
    yield "latin1-field", mixed[:1], [field("title", "caf\u00e9", "iso-8859-1")], "", 200, 1
    yield "empty-file", [("empty.png", "image/png", b"")], [], "", 400, 0
    yield "unsupported-mime", [("data.pdf", "application/octet-stream", b"not inferred through explicit MIME")], [], "", 400, 0
    yield "partial-later-file-refusal", [mixed[0], ("second.bin", "application/octet-stream", b"refused")], [], "", 400, 1
    yield "no-files", [], [field("title", "empty")], "", 400, 0
    yield "maximum-media-file", [("maximum.png", "image/png", b"m" * (50 * 1024**2))], [], "", 200, 1
    names = [f"capacity-{i:02}.png" for i in range(20)]
    empty = [(name, "image/png", b"") for name in names]
    overhead = len(multipart(empty, [field("title", "ignored")])[0])
    available = 220_000_000 - overhead
    files = [(name, "image/png", bytes([65 + i]) * (available // 20 + (i < available % 20))) for i, name in enumerate(names)]
    yield "maximum-body-and-twenty-files", files, [field("title", "ignored")], "", 200, 20


def conformance_requests() -> Iterator[tuple[str, bytes, str, str, int, int, int]]:
    """Original parser boundaries exercised through both real public routes."""
    files = [("image.png", "image/png", b"\x00opaque\xff")]
    raw, mime = multipart(files, [])
    marker = b"Content-Type: image/png\r\n"
    opening = ("--" + BOUNDARY + "\r\n").encode()
    variants = [
        ("folded-header", raw.replace(marker, marker + b"X-Header: value\r\n continued\r\n", 1), mime, 200),
        ("last-disposition", raw.replace(marker, marker + b'Content-Disposition: form-data; name="files"; filename="last.png"\r\n', 1), mime, 200),
        ("last-content-type", raw.replace(marker, marker + b"Content-Type: text/html\r\n", 1), mime, 200),
        ("preamble", b"original preamble\r\n" + raw, mime, 200),
        ("no-final-crlf", raw.removesuffix(b"\r\n"), mime, 200),
        ("transport-padding", raw.replace(opening, opening[:-2] + b" \t\r\n", 1), mime, 415),
        ("malformed-header", raw.replace(marker, b"BrokenHeader\r\n", 1), mime, 415),
        ("attachment-disposition", raw.replace(b"form-data; name=", b"attachment; name=", 1), mime, 415),
        ("header-count", raw.replace(marker, marker + b"X-Header: value\r\n" * 7, 1), mime, 413),
        ("folded-header-limit", raw.replace(marker, marker + b"X-Header: " + b"x" * (4096-len("X-Header: ")) + b"\r\n continued\r\n", 1), mime, 413),
        ("truncated-header", opening + b"Content-Type: image/png", mime, 415),
        ("missing-boundary", raw, "multipart/form-data", 400),
        ("last-boundary-parameter", raw, "multipart/form-data; boundary=wrong; boundary=" + BOUNDARY, 200),
        ("part-length-exact", raw.replace(marker, marker + b"Content-Length: 8\r\n", 1), mime, 200),
        ("part-length-mismatch", raw.replace(marker, marker + b"Content-Length: 9\r\n", 1), mime, 415),
        ("independent-large-headers", raw.replace(marker, marker + b"X-First: " + b"x" * 4000 + b"\r\nX-Second: " + b"y" * 4000 + b"\r\n", 1), mime, 200),
    ]
    for label, wire, content_type, status in variants:
        yield "parser-" + label, wire, content_type, "", status, int(status == 200), 1
    for label, charset, data in [
        ("utf7-title", "utf-7", "café 中文".encode("utf-7")),
        ("utf16-no-bom-title", "utf-16", "café 中文".encode("utf-16-le")),
        ("utf32-no-bom-title", "utf-32", "café 中文".encode("utf-32-le")),
        ("utf32le-title", "utf-32-le", "café 中文".encode("utf-32-le")),
        ("utf32be-title", "utf-32-be", "café 中文".encode("utf-32-be")),
        ("replacement-character-title", "utf-16", "\ufffd".encode("utf-16-le")),
    ]:
        wire, content_type = multipart(files, [("title", data, charset)])
        yield "parser-" + label, wire, content_type, "", 200, 1, 1
    for label, inputs, fields in [
        ("file-count", files * 21, []),
        ("part-count", [], [field("title", "value")] * 25),
        ("memory-limit", [(f"memory-{i}.png", "image/png", b"m" * 1024**2) for i in range(5)], []),
    ]:
        wire, content_type = multipart(inputs, fields)
        yield "parser-" + label, wire, content_type, "", 413, 0, len(inputs)


def wire_requests() -> Iterator[tuple[str, bytes, str, str, int, int, int]]:
    for label, files, fields, query, status, stored in cases():
        raw, mime = multipart(files, fields)
        yield label, raw, mime, query, status, stored, len(files)
    yield from conformance_requests()


def verify_cases(origin: str, token: str, profile: Path, mirror: Path, out: Path, report: dict[str, Any]) -> list[tuple[str, dict[str, Any], str]]:
    create.require(os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux", "MEDIA_UPLOAD_VERIFICATION_IS_CLOUD_ONLY")
    report["multipartUploadCases"] = []
    observations = []
    for label, raw, content_type, query, expected_status, expected_stored, input_files in wire_requests():
        if label == "maximum-body-and-twenty-files":
            create.require(len(raw) == 220_000_000 and input_files == 20, "MEDIA_UPLOAD_CAPACITY_NOT_EXERCISED")
        reference = original(mirror / label, out / ("oracle-upload-" + label + ".log"), raw, content_type, query)
        create.require(reference["status"] == expected_status and len(reference["storedMedia"]) == expected_stored,
                       "MEDIA_UPLOAD_ORIGINAL_CONTRACT_DIFFERS_" + label)
        before = {record["mediaId"] for record in library_records(profile)}
        status, body, headers = exchange(origin, token, raw, content_type, query)
        row = dict(case=label, requestBytes=len(raw), files=input_files, expectedStatus=expected_status, nativeStatus=status,
                   originalStatus=reference["status"], originalStoredRecords=expected_stored,
                   nativeErrorCode=error_code(body), nativeErrorBody=body if status != 200 else None)
        report["multipartUploadCases"].append(row)
        create.require(status == expected_status, "MEDIA_UPLOAD_PUBLIC_STATUS_DIFFERS_" + label)
        retained = [record for record in library_records(profile) if record["mediaId"] not in before]
        create.require(len(retained) == expected_stored, "MEDIA_UPLOAD_PARTIAL_EFFECT_COUNT_DIFFERS_" + label)
        create.require(create.same_json(canonical_records(retained), canonical_records(reference["storedMedia"])),
                       "MEDIA_UPLOAD_STORED_RECORD_DIFFERS_" + label)
        hashes = [hashlib.sha256((profile / ".media" / record["path"]).read_bytes()).hexdigest() for record in retained]
        create.require(hashes == reference["sourceHashes"], "MEDIA_UPLOAD_SOURCE_BYTES_DIFFER_" + label)
        if status == 200:
            records = require_success(body, input_files)
            original_records = require_success(reference["body"], input_files)
            create.require(create.same_json(canonical_records(records), canonical_records(original_records)), "MEDIA_UPLOAD_PUBLIC_RECORD_DIFFERS_" + label)
            create.require(create.same_json(records, retained), "MEDIA_UPLOAD_PUBLIC_STORED_RECORD_DIFFERS_" + label)
            create.require(headers.get("x-deepseek-request-id") == create.REQUEST_ID, "MEDIA_UPLOAD_REQUEST_ID_DIFFERS")
        else:
            create.require(error_code(body) == error_code(reference["body"]), "MEDIA_UPLOAD_ERROR_CODE_DIFFERS_" + label)
        for record, digest in zip(retained, hashes, strict=True):
            observations.append((record["mediaId"], dict(ok=True, media=record), digest))
        row.update(status="PASS", nativeStoredRecords=len(retained), sourceHashes=hashes)
    report.update(actualMultipartIngestCasesQualified=True, actualMultipartIngestCaseCount=len(report["multipartUploadCases"]),
                  actualMultipartStoredRecords=len(observations), multipartMaximumRequestBytes=220_000_000,
                  multipartMaximumFileBytes=50 * 1024**2, multipartMaximumFiles=20, multipartProcessFalseOnly=True)
    return observations
