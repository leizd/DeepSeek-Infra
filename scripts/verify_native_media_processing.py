"""Cloud-only public processing/index/force probe with isolated original oracles.

The native runtime receives public HTTP requests. SQLite reads below only inspect
its resulting index; they never create state or serve as promotion authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any

import verify_native_media_create as create


def fixtures() -> list[dict[str, Any]]:
    return list(json.loads((create.ROOT / "tests/native/media_processing_reference_v1.json").read_bytes()))


def failure_fixture() -> dict[str, Any]:
    return dict(type="pdf", title="Retained failed PDF processing", projectId="original-processing-reference",
                process=True, text="not a PDF")


def require_reference(report: dict[str, Any], backend: str) -> None:
    create.require(report.get("status") == "PASS" and report.get("backend") == backend and
                   report.get("embeddingProvider") == "hash", "MEDIA_PROCESS_ORIGINAL_CONFIGURATION_DIFFERS")
    cases = report.get("cases")
    if not isinstance(cases, list):
        raise create.ProbeFailure("MEDIA_PROCESS_ORIGINAL_CASES_MISSING")
    create.require(len(cases) == 6, "MEDIA_PROCESS_ORIGINAL_CASES_MISSING")
    create.require({row["type"] for row in cases} == {"webpage", "image", "screenshot", "pdf", "audio", "video"},
                   "MEDIA_PROCESS_ORIGINAL_TYPES_MISSING")
    if backend == "sqlite_vec":
        create.require(report.get("vectorTableAvailable") is True, "MEDIA_PROCESS_ORIGINAL_VECTOR_UNAVAILABLE")
    for row in cases:
        segments, media = row["segments"], row["media"]
        create.require(media["status"] == "ready" and len(segments) > 0 and
                       media["metadata"]["segmentCount"] == media["metadata"]["indexedChunkCount"] == len(segments),
                       "MEDIA_PROCESS_ORIGINAL_INDEX_MISSING")
        create.require(row["forceStatus"] == 200 and row["publicReadsMatch"] is True and len(row["search"]) > 0,
                       "MEDIA_PROCESS_ORIGINAL_FORCE_SEARCH_OR_READ_MISSING")
        create.require(len(row["indexedRows"]) == len(segments), "MEDIA_PROCESS_ORIGINAL_INDEX_ROWS_MISSING")
    failed = report.get("failedCase")
    create.require(isinstance(failed, dict) and failed.get("status") == 422 and failed.get("publicReadsMatch") is True and
                   failed.get("media", {}).get("status") == "failed" and failed.get("segments") == [] and
                   failed.get("indexedRows") == [] and failed.get("sourceSha256") == hashlib.sha256(b"not a PDF").hexdigest(),
                   "MEDIA_PROCESS_ORIGINAL_FAILED_EFFECT_MISSING")


def original(profile: Path, diagnostic: Path, backend: str) -> dict[str, Any]:
    create.require(backend in {"sqlite", "sqlite_vec"}, "MEDIA_PROCESS_ORIGINAL_BACKEND_INVALID")
    env = {key:value for key,value in os.environ.items() if key in {
        "PATH", "HOME", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR",
        "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP"}}
    env.update(DEEPSEEK_INFRA_ROOT=str(profile), LOCAL_RAG_ENABLED="true", LOCAL_RAG_BACKEND=backend,
               PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    run = subprocess.run([sys.executable, "-B", "-c", ORACLE], cwd=create.ROOT, env=env,
                         input=json.dumps(dict(cases=fixtures(), failedCase=failure_fixture()), ensure_ascii=True).encode(), capture_output=True, timeout=180)
    create.require(len(run.stderr) <= create.LIMIT, "MEDIA_PROCESS_ORIGINAL_DIAGNOSTIC_BOUND")
    diagnostic.write_bytes(run.stderr)
    create.require(run.returncode == 0 and len(run.stdout) <= create.LIMIT, "MEDIA_PROCESS_ORIGINAL_ORACLE_FAILED")
    report = dict(json.loads(run.stdout))
    require_reference(report, backend)
    return report


def read_index(profile: Path, relative: str, media: dict[str, Any]) -> list[dict[str, Any]]:
    database = profile / relative
    create.require(database.is_file(), "MEDIA_PROCESS_INDEX_MISSING")
    create.require(database.resolve(strict=True).is_relative_to(profile.resolve()), "MEDIA_PROCESS_INDEX_ESCAPED_PROFILE")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            "SELECT item_id,collection,source_id,project_id,chunk_index,name,kind,scope,text,embedding,metadata "
            "FROM rag_items WHERE collection=? AND source_id=? AND project_id=? ORDER BY chunk_index",
            ("media", media["mediaId"], media["projectId"]))]


def compare_processing(media: dict[str, Any], segments: list[dict[str, Any]], index: list[dict[str, Any]],
                       expected: dict[str, Any], forced: bool = False) -> None:
    reference = expected["forced"] if forced else expected
    reference_media = reference["media"]
    reference_segments = reference["segments"]
    create.require(media["status"] == "ready" and len(segments) == len(reference_segments) == len(index) > 0,
                   "MEDIA_PROCESS_SEGMENTS_OR_INDEX_COUNT_DIFFERS")
    actual_metadata = media["metadata"]
    for key in ["segmentCount", "indexedChunkCount", "pageCount", "durationSec"]:
        create.require(create.same_json(actual_metadata[key], reference_media["metadata"][key]), "MEDIA_PROCESS_METADATA_DIFFERS")
    for ordinal, (segment, prior, indexed, prior_index) in enumerate(zip(segments, reference_segments, index, expected["indexedRows"])):
        normalized, wanted = copy.deepcopy(segment), copy.deepcopy(prior)
        for value in [normalized, wanted]:
            value.pop("segmentId", None)
            value.pop("mediaId", None)
            value["citation"].pop("segmentId", None)
            value["citation"].pop("mediaId", None)
            value["citation"]["uri"] = value["citation"]["uri"].replace(media["mediaId"], "MEDIA").replace(reference_media["mediaId"], "MEDIA")
            value["citation"]["uri"] = value["citation"]["uri"].replace(segment["segmentId"], "SEGMENT").replace(prior["segmentId"], "SEGMENT")
        create.require(create.same_json(normalized, wanted), "MEDIA_PROCESS_SEGMENT_OR_CITATION_DIFFERS")
        create.require(segment["mediaId"] == segment["citation"]["mediaId"] == media["mediaId"] and
                       segment["segmentId"] == segment["citation"]["segmentId"], "MEDIA_PROCESS_CITATION_UNBOUND")
        create.require(indexed["item_id"] == f"media:{media['mediaId']}:{segment['segmentId']}:{ordinal}" and
                       indexed["source_id"] == media["mediaId"] and indexed["collection"] == "media" and
                       indexed["project_id"] == media["projectId"] and indexed["chunk_index"] == ordinal,
                       "MEDIA_PROCESS_INDEX_UNBOUND")
        for key in ["name", "kind", "scope", "text"]:
            create.require(indexed[key] == prior_index[key], "MEDIA_PROCESS_INDEX_VALUE_DIFFERS")
        create.require(create.same_json(json.loads(indexed["embedding"]), json.loads(prior_index["embedding"])),
                       "MEDIA_PROCESS_INDEX_EMBEDDING_DIFFERS")
        actual_index_metadata = json.loads(indexed["metadata"])
        prior_index_metadata = json.loads(prior_index["metadata"])
        prior_index_metadata["mediaId"] = media["mediaId"]
        previous_segment_id = prior_index_metadata["segmentId"]
        prior_index_metadata["segmentId"] = segment["segmentId"]
        prior_index_metadata["citation"] = prior_index_metadata["citation"].replace(reference_media["mediaId"], media["mediaId"]).replace(previous_segment_id, segment["segmentId"])
        create.require(create.same_json(actual_index_metadata, prior_index_metadata), "MEDIA_PROCESS_INDEX_METADATA_DIFFERS")


def verify(origin: str, token: str, profile: Path, mirror: Path, out: Path, report: dict[str, Any]) -> list[dict[str, Any]]:
    create.require(os.environ.get("GITHUB_ACTIONS") == "true" and sys.platform == "linux", "MEDIA_PROCESS_VERIFICATION_IS_CLOUD_ONLY")
    references = {}
    report["mediaProcessingOriginalReferences"] = []
    for backend in ["sqlite", "sqlite_vec"]:
        reference = original(mirror / backend, out / ("processing-original-" + backend + ".log"), backend)
        (out / ("processing-original-" + backend + ".json")).write_text(json.dumps(reference, ensure_ascii=True) + "\n", encoding="utf8")
        references[backend] = reference
        report["mediaProcessingOriginalReferences"].append(dict(backend=backend, cases=6, forceCases=6, status="PASS",
            actualIndexAndSearchVerified=True, vectorTableAvailable=reference["vectorTableAvailable"]))
    report["nativeMediaProcessingCases"] = []
    created = []
    for payload, expected in zip(fixtures(), references["sqlite"]["cases"]):
        status, body, _ = create.exchange(origin, "/api/media", "POST", token, payload)
        row = dict(case=payload["type"], originalStatus=expected["status"], nativeStatus=status,
                   nativeErrorBody=body if status != 200 else None, status="FAIL")
        report["nativeMediaProcessingCases"].append(row)
        report["nativeMediaProcessingSemanticStageReached"] = True
        create.require(status == expected["status"] == 200, "NATIVE_MEDIA_PROCESSING_PUBLIC_RESULT_DIFFERS:" + payload["type"])
        media = body["media"]
        suffix = Path(expected["media"]["path"]).suffix
        create.require(create.same_json(create.canonical_media(media, suffix), create.canonical_media(expected["media"], suffix)),
                       "MEDIA_PROCESS_READY_RECORD_DIFFERS")
        source = profile / ".media" / media["path"] if media["path"] else None
        if source is not None:
            create.require(source.resolve(strict=True).is_relative_to((profile / ".media").resolve()), "MEDIA_PROCESS_SOURCE_ESCAPED_PROFILE")
            create.require(hashlib.sha256(source.read_bytes()).hexdigest() == expected["sourceSha256"], "MEDIA_PROCESS_SOURCE_BYTES_DIFFER")
        read_status, retained, _ = create.exchange(origin, "/api/media/" + media["mediaId"], "GET", token)
        row.update(createPublicReadStatus=read_status)
        create.require(read_status == 200 and create.same_json(retained["media"], media), "MEDIA_PROCESS_READY_PUBLIC_READ_DIFFERS")
        status, segment_body, _ = create.exchange(origin, "/api/media/" + media["mediaId"] + "/segments", "GET", token)
        create.require(status == 200, "MEDIA_PROCESS_PUBLIC_SEGMENTS_UNAVAILABLE")
        compare_processing(media, segment_body["segments"], read_index(profile, references["sqlite"]["ragDatabaseRelativePath"], media), expected)
        row.update(createProcessingStatus="PASS", createPublicReadMatches=True, createdSegments=len(segment_body["segments"]), mediaId=media["mediaId"])
        created.append((row, expected, media, suffix, source))
    # Every retained creation case must complete before force is exercised.
    # A later force failure cannot erase which actual create/index paths ran.
    report["nativeMediaProcessingCreatesVerified"] = len(created)
    report["nativeMediaCreationProcessingSubstepQualified"] = len(created) == 6
    failed_expected = references["sqlite"]["failedCase"]
    status, failed_body, headers = create.exchange(origin, "/api/media", "POST", token, failure_fixture())
    report["nativeMediaProcessingFailedCase"] = dict(status="FAIL", nativeStatus=status, body=failed_body)
    create.require(status == failed_expected["status"] and create.same_json(failed_body, failed_expected["errorBody"]),
                   "MEDIA_PROCESS_FAILED_PUBLIC_ERROR_DIFFERS")
    list_status, listed, _ = create.exchange(origin, "/api/media?projectId=original-processing-reference", "GET", token)
    create.require(list_status == 200, "MEDIA_PROCESS_FAILED_RECORD_LIST_UNAVAILABLE")
    matches = [m for m in listed["media"] if m["title"] == failure_fixture()["title"]]
    create.require(len(matches) == 1, "MEDIA_PROCESS_FAILED_EFFECT_DISAPPEARED")
    failed_media = matches[0]
    suffix = Path(failed_expected["media"]["path"]).suffix
    create.require(create.same_json(create.canonical_media(failed_media, suffix), create.canonical_media(failed_expected["media"], suffix)),
                   "MEDIA_PROCESS_FAILED_RECORD_DIFFERS")
    read_status, retained_failed, _ = create.exchange(origin, "/api/media/" + failed_media["mediaId"], "GET", token)
    segment_status, failed_segments, _ = create.exchange(origin, "/api/media/" + failed_media["mediaId"] + "/segments", "GET", token)
    create.require(read_status == segment_status == 200 and retained_failed["media"] == failed_media and failed_segments["segments"] == [],
                   "MEDIA_PROCESS_FAILED_READS_DIFFER")
    source = profile / ".media" / failed_media["path"]
    create.require(source.resolve(strict=True).is_relative_to((profile / ".media").resolve()) and
                   hashlib.sha256(source.read_bytes()).hexdigest() == failed_expected["sourceSha256"], "MEDIA_PROCESS_FAILED_SOURCE_DIFFERS")
    create.require(read_index(profile, references["sqlite"]["ragDatabaseRelativePath"], failed_media) == [], "MEDIA_PROCESS_FAILED_UNEXPECTED_INDEX")
    report["nativeMediaProcessingFailedCase"].update(status="PASS", retainedFailedRecord=True, originalSourceRetained=True,
        originalPublicErrorRetained=True, operationId=headers.get("x-deepseek-operation-id"))
    observations = []
    for row, expected, media, suffix, source in created:
        status, force_body, headers = create.exchange(origin, "/api/media/" + media["mediaId"] + "/process?force=true", "POST", token, {})
        # Retain the actual numeric failure and body before any assertion.
        row.update(forceStatus=status, forceErrorBody=force_body if status != 200 else None,
                   forceOperationId=headers.get("x-deepseek-operation-id"))
        create.require(status == 200, "MEDIA_PROCESS_FORCE_PUBLIC_RESULT_DIFFERS")
        forced_media = force_body["media"]
        create.require(create.same_json(create.canonical_media(forced_media, suffix), create.canonical_media(expected["forced"]["media"], suffix)),
                       "MEDIA_PROCESS_FORCE_RECORD_DIFFERS")
        compare_processing(forced_media, force_body["segments"], read_index(profile, references["sqlite"]["ragDatabaseRelativePath"], forced_media), expected, True)
        read_status, retained, _ = create.exchange(origin, "/api/media/" + media["mediaId"], "GET", token)
        create.require(read_status == 200 and create.same_json(retained["media"], forced_media), "MEDIA_PROCESS_PUBLIC_READ_DIFFERS")
        segment_status, stored_segments, _ = create.exchange(origin, "/api/media/" + media["mediaId"] + "/segments", "GET", token)
        create.require(segment_status == 200 and create.same_json(stored_segments["segments"], force_body["segments"]),
                       "MEDIA_PROCESS_FORCE_PUBLIC_SEGMENTS_DIFFER")
        create.require(isinstance(row["forceOperationId"], str) and row["forceOperationId"].startswith("media-process-"),
                       "MEDIA_PROCESS_ORIGINAL_OPERATION_MISSING")
        if source is not None:
            create.require(hashlib.sha256(source.read_bytes()).hexdigest() == expected["sourceSha256"], "MEDIA_PROCESS_FORCE_SOURCE_CHANGED")
        row.update(status="PASS", segments=len(force_body["segments"]), indexed=force_body["indexed"], forcePublicReadsMatch=True)
        observations.append(dict(media=forced_media, segments=force_body["segments"], sourceSha256=expected["sourceSha256"] if source is not None else None,
                                 indexedRows=read_index(profile, references["sqlite"]["ragDatabaseRelativePath"], forced_media),
                                 ragDatabaseRelativePath=references["sqlite"]["ragDatabaseRelativePath"]))
    report.update(actualNativeSQLiteTextProcessingCasesQualified=True, actualNativeProcessingBackend="sqlite",
        nativeSQLiteVecProcessingQualified=False, nativeBinaryOCRAndPDFExtractionQualified=False,
        nativeProcessingCrashRecoveryQualified=False, processPipelineQualified=False)
    return observations

ORACLE = r'''
import dataclasses,hashlib,json,os
from pathlib import Path
from fastapi.testclient import TestClient
from deepseek_infra.web.server import create_app,settings
from deepseek_infra.infra.media import library,indexer
from deepseek_infra.infra.rag import local_rag
from deepseek_infra.infra.rust_core.config import load_rust_flags
root=Path(os.environ["DEEPSEEK_INFRA_ROOT"]).resolve()
assert local_rag.LOCAL_RAG_DB.resolve().is_relative_to(root)
assert local_rag.LOCAL_RAG_ENABLED and local_rag.LOCAL_RAG_EMBEDDING_PROVIDER=="hash"
assert not any(dataclasses.asdict(load_rust_flags()).values())
client=TestClient(create_app(),base_url="http://127.0.0.1")
headers={"Authorization":"Bearer "+settings.auth.token}
request=json.loads(__import__("sys").stdin.read());cases=request["cases"];failed_payload=request["failedCase"]
rows=[]
try:
 for payload in cases:
  response=client.post("/api/media",json=payload,headers=headers)
  assert response.status_code==200,(payload["type"],response.status_code,response.text[:400])
  media=response.json()["media"];mid=media["mediaId"]
  assert media["status"]=="ready" and media["metadata"]["segmentCount"]>0
  segments=client.get("/api/media/"+mid+"/segments",headers=headers)
  assert segments.status_code==200
  before=segments.json()["segments"]
  assert len(before)==media["metadata"]["segmentCount"]==media["metadata"]["indexedChunkCount"]
  found=indexer.search_media_index("neptune",media_id=mid,project_id=payload["projectId"],limit=20)
  assert found and all(row.source_id==mid and row.collection=="media" for row in found)
  path=library.media_dir()/media["path"] if media["path"] else None
  original_hash=hashlib.sha256(path.read_bytes()).hexdigest() if path else None
  forced=client.post("/api/media/"+mid+"/process?force=true",json={},headers=headers)
  assert forced.status_code==200,(payload["type"],forced.status_code,forced.text[:400])
  value=forced.json();assert value["ok"] and value["media"]["status"]=="ready"
  assert value["indexed"]==len(value["segments"])==len(before)
  assert [row["text"] for row in value["segments"]]==[row["text"] for row in before]
  assert [row["citation"]["label"] for row in value["segments"]]==[row["citation"]["label"] for row in before]
  assert all(row["citation"]["mediaId"]==mid and row["citation"]["segmentId"]==row["segmentId"] for row in value["segments"])
  assert not path or hashlib.sha256(path.read_bytes()).hexdigest()==original_hash
  reads=client.get("/api/media/"+mid,headers=headers)
  retained=client.get("/api/media/"+mid+"/segments",headers=headers)
  assert reads.status_code==retained.status_code==200 and reads.json()["media"]==value["media"] and retained.json()["segments"]==value["segments"]
  rows.append(dict(type=payload["type"],status=200,media=media,segments=before,search=[dataclasses.asdict(row) for row in found],
                   forceStatus=forced.status_code,forced=value,publicReadsMatch=True,sourceSha256=original_hash))
 failed_response=client.post("/api/media",json=failed_payload,headers=headers)
 assert failed_response.status_code==422,failed_response.text
 listed=client.get("/api/media",params={"projectId":failed_payload["projectId"]},headers=headers)
 assert listed.status_code==200
 failed_records=[m for m in listed.json()["media"] if m["title"]==failed_payload["title"]]
 assert len(failed_records)==1
 failed_media=failed_records[0];assert failed_media["status"]=="failed"
 failed_read=client.get("/api/media/"+failed_media["mediaId"],headers=headers)
 failed_segments=client.get("/api/media/"+failed_media["mediaId"]+"/segments",headers=headers)
 assert failed_read.status_code==failed_segments.status_code==200 and failed_read.json()["media"]==failed_media
 assert failed_segments.json()["segments"]==[]
 failed_source=library.media_dir()/failed_media["path"]
 assert failed_source.read_bytes()==b"not a PDF"
 failed_case=dict(status=failed_response.status_code,errorBody=failed_response.json(),media=failed_media,segments=[],
     publicReadsMatch=True,sourceSha256=hashlib.sha256(failed_source.read_bytes()).hexdigest())
 import sqlite3
 database_relative=str(local_rag.LOCAL_RAG_DB.relative_to(root))
 with sqlite3.connect(local_rag.LOCAL_RAG_DB.as_uri()+"?mode=ro",uri=True) as conn:
  conn.row_factory=sqlite3.Row
  for row in rows:
   row["indexedRows"]=[dict(item) for item in conn.execute("SELECT item_id,collection,source_id,project_id,chunk_index,name,kind,scope,text,embedding,metadata FROM rag_items WHERE collection=? AND source_id=? AND project_id=? ORDER BY chunk_index",("media",row["media"]["mediaId"],row["media"]["projectId"]))]
  failed_case["indexedRows"]=[dict(item) for item in conn.execute("SELECT item_id FROM rag_items WHERE collection=? AND source_id=?",("media",failed_media["mediaId"]))]
  assert not failed_case["indexedRows"]
 status=local_rag.status()
 assert status["backend"]==os.environ["LOCAL_RAG_BACKEND"]
 if status["backend"]=="sqlite_vec":assert status["sqliteVecAvailable"] and status["vectorTableAvailable"]
 print(json.dumps(dict(status="PASS",backend=status["backend"],embeddingProvider=status["embeddingProvider"],
     indexedMedia=status["indexedMedia"],vectorTableAvailable=status["vectorTableAvailable"],cases=rows,failedCase=failed_case,ragDatabaseRelativePath=database_relative),ensure_ascii=True))
finally:client.close()
'''
