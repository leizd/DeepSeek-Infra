"""Original processing references and fail-closed cloud observer checks."""
import importlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_original_processing_reference_exercises_all_six_types_index_search_and_force(tmp_path):
    processing = importlib.import_module("verify_native_media_processing")
    result = processing.original(tmp_path / "original", tmp_path / "original.log", "sqlite")
    processing.require_reference(result, "sqlite")
    assert {row["type"] for row in result["cases"]} == {"webpage", "image", "screenshot", "pdf", "audio", "video"}
    assert all(row["forceStatus"] == 200 and row["publicReadsMatch"] and row["search"] for row in result["cases"])
    assert result["embeddingProvider"] == "hash"
    assert result["failedCase"]["status"] == 422
    assert result["failedCase"]["media"]["status"] == "failed"
    assert result["failedCase"]["segments"] == result["failedCase"]["indexedRows"] == []


def test_processing_observer_never_starts_native_workloads_locally(tmp_path, monkeypatch):
    processing = importlib.import_module("verify_native_media_processing")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(processing.create.ProbeFailure, match="CLOUD_ONLY"):
        processing.verify("http://127.0.0.1:1", "", tmp_path, tmp_path, tmp_path, {})


@pytest.mark.parametrize("reference", [
    {"status": "PASS", "backend": "sqlite", "cases": []},
    {"status": "FAIL", "backend": "sqlite", "cases": []},
    {"status": "PASS", "backend": "sqlite", "embeddingProvider": "hash", "cases": None},
    {"status": "PASS", "backend": "sqlite_vec", "vectorTableAvailable": False, "cases": []},
])
def test_processing_observer_refuses_partial_or_unavailable_original_references(reference):
    processing = importlib.import_module("verify_native_media_processing")
    with pytest.raises(processing.create.ProbeFailure):
        processing.require_reference(reference, reference["backend"])


@pytest.mark.parametrize("ready_read_matches", [True, False])
def test_processing_observer_checks_all_creations_before_force_and_keeps_force_required(tmp_path, monkeypatch, ready_read_matches):
    from types import SimpleNamespace
    processing = importlib.import_module("verify_native_media_processing")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(processing, "sys", SimpleNamespace(platform="linux"))
    kinds = ["webpage", "image", "screenshot", "pdf", "audio", "video"]
    rows = [dict(status=200, media=dict(mediaId="media_" + kind, path="", type=kind), segments=[]) for kind in kinds]
    failed_media = dict(mediaId="media_failed", path="objects/media_failed/source.txt", status="failed",
                        title=processing.failure_fixture()["title"], projectId="original-processing-reference")
    failed_error = dict(error="Could not extract text from this PDF", code="invalid_payload")
    source = tmp_path / ".media" / failed_media["path"]
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not a PDF")
    failed = dict(status=422, media=failed_media, errorBody=failed_error, sourceSha256=processing.hashlib.sha256(b"not a PDF").hexdigest())
    monkeypatch.setattr(processing, "fixtures", lambda: [dict(type=kind) for kind in kinds])
    monkeypatch.setattr(processing, "original", lambda root, log, backend: dict(
        cases=rows, failedCase=failed, vectorTableAvailable=backend == "sqlite_vec", ragDatabaseRelativePath=".local-rag/rag.sqlite3"))
    monkeypatch.setattr(processing.create, "canonical_media", lambda media, suffix: media)
    monkeypatch.setattr(processing, "compare_processing", lambda *args: None)
    monkeypatch.setattr(processing, "read_index", lambda *args: [])
    calls = []

    def exchange(origin, path, method, token, payload=None):
        calls.append((method, path))
        if path.endswith("/process?force=true"):
            return 503, dict(code="EFFECT_UNKNOWN"), {}
        if method == "POST":
            if payload.get("title") == processing.failure_fixture()["title"]:
                return 422, failed_error, {}
            row = rows[kinds.index(payload["type"])]
            return 200, dict(media=row["media"]), {}
        if path == "/api/media?projectId=original-processing-reference":
            return 200, dict(media=[failed_media]), {}
        if path == "/api/media/media_failed":
            return 200, dict(media=failed_media), {}
        if method == "GET" and not path.endswith("/segments"):
            matching = [row["media"] for row in rows if path.endswith("/" + row["media"]["mediaId"])]
            if matching:
                media = matching[0] if ready_read_matches else dict(mediaId="media_wrong")
                return 200, dict(media=media), {}
        return 200, dict(segments=[]), {}

    monkeypatch.setattr(processing.create, "exchange", exchange)
    report = {}
    failure = "MEDIA_PROCESS_FORCE_PUBLIC_RESULT_DIFFERS" if ready_read_matches else "MEDIA_PROCESS_READY_PUBLIC_READ_DIFFERS"
    with pytest.raises(processing.create.ProbeFailure, match=failure):
        processing.verify("http://native.invalid", "synthetic", tmp_path, tmp_path, tmp_path, report)
    if not ready_read_matches:
        assert calls == [("POST", "/api/media"), ("GET", "/api/media/media_webpage")]
        assert not report.get("nativeMediaCreationProcessingSubstepQualified")
        assert not report.get("actualNativeSQLiteTextProcessingCasesQualified")
        assert report["nativeMediaProcessingCases"][0]["createPublicReadStatus"] == 200
        return
    assert calls[:18:3] == [("POST", "/api/media")] * 6
    assert len(calls) == 23 and calls[-1][1].endswith("/process?force=true")
    assert report["nativeMediaProcessingCreatesVerified"] == 6
    assert report["nativeMediaCreationProcessingSubstepQualified"] is True
    assert all(row["createProcessingStatus"] == "PASS" for row in report["nativeMediaProcessingCases"])
    assert all(row["createPublicReadMatches"] is True for row in report["nativeMediaProcessingCases"])
    assert report["nativeMediaProcessingCases"][0]["forceStatus"] == 503
    assert report["nativeMediaProcessingCases"][0]["forceErrorBody"] == dict(code="EFFECT_UNKNOWN")
    assert report["nativeMediaProcessingFailedCase"]["status"] == "PASS"
    assert not report.get("actualNativeSQLiteTextProcessingCasesQualified")
    assert not report.get("processPipelineQualified")
