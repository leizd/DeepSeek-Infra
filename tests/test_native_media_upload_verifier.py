"""Independent observer checks; no local native compilation or execution."""
import importlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
upload = importlib.import_module("verify_native_media_upload")


@pytest.mark.parametrize("body,count", [
    ({"ok": True, "media": {}, "mediaItems": []}, 1),
    ({"ok": True, "media": {"mediaId": "one"}, "mediaItems": [{"mediaId": "two"}]}, 1),
    ({"ok": True, "media": {"mediaId": "one"}, "mediaItems": [{"mediaId": "one"}, {"mediaId": "one"}]}, 2),
    ({"ok": False, "media": {"mediaId": "one"}, "mediaItems": [{"mediaId": "one"}]}, 1),
])
def test_multipart_observer_refuses_empty_partial_duplicate_or_failed_success(body, count):
    with pytest.raises(upload.create.ProbeFailure):
        upload.require_success(body, count)


def test_multipart_observer_retains_raw_bytes_field_encoding_and_literal_fakepath():
    raw, mime = upload.multipart([("C:\\fakepath\\image.PNG", "image/png", b"\x00binary\xff\r\n")],
                                 [upload.field("title", "caf\u00e9", "iso-8859-1")])
    assert mime == "multipart/form-data; boundary=" + upload.BOUNDARY
    assert b'filename="C:\\fakepath\\image.PNG"' in raw
    assert b"caf\xe9" in raw and b"charset=iso-8859-1" in raw
    assert b"\x00binary\xff\r\n\r\n--" in raw


def test_multipart_independent_original_retains_order_raw_sources_and_batch_title(tmp_path):
    files = [("C:\\fakepath\\image.PNG", "image/png", b"\x00binary\xff"),
             ("page.HTML", "text/html", b"  <h1>raw source</h1>\n  ")]
    raw, mime = upload.multipart(files, [upload.field("title", "ignored")])
    result = upload.original(tmp_path / "oracle", tmp_path / "oracle.log", raw, mime,
                             "?projectId=first&projectId=project-query")
    assert result["status"] == 200
    records = upload.require_success(result["body"], 2)
    assert [record["title"] for record in records] == ["image.PNG", "page.HTML"]
    assert [record["projectId"] for record in records] == ["project-query", "project-query"]
    assert [record["type"] for record in records] == ["image", "webpage"]
    assert records == result["storedMedia"]
    import hashlib
    assert result["sourceHashes"] == [hashlib.sha256(data).hexdigest() for _, _, data in files]


def test_multipart_independent_original_honors_non_utf8_form_charset(tmp_path):
    raw, mime = upload.multipart([("image.png", "image/png", b"opaque")],
                                 [upload.field("title", "caf\u00e9", "iso-8859-1")])
    result = upload.original(tmp_path / "oracle", tmp_path / "oracle.log", raw, mime, "")
    assert result["status"] == 200 and result["body"]["media"]["title"] == "caf\u00e9"


def test_multipart_native_probe_remains_cloud_only(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(upload.create.ProbeFailure, match="CLOUD_ONLY"):
        upload.verify_cases("http://127.0.0.1:1", "", tmp_path, tmp_path, tmp_path, {})


@pytest.mark.parametrize("uploaded,total", [(0, 11), (31, 42)])
def test_retained_observation_report_separates_json_cases_from_uploaded_records(uploaded, total):
    report = dict(jsonRegistrationCases=[{} for _ in range(10)], actualMultipartStoredRecords=uploaded)
    assert upload.create.observation_counts(report, total) == dict(actualJSONRegistrationCases=11, actualRetainedMediaObservations=total)


def test_retained_observation_report_refuses_missing_or_pooled_counts():
    report = dict(jsonRegistrationCases=[{} for _ in range(10)], actualMultipartStoredRecords=31)
    with pytest.raises(upload.create.ProbeFailure, match="OBSERVATION_COUNTS_DIFFER"):
        upload.create.observation_counts(report, 41)


@pytest.mark.parametrize("name", [
    "parser-folded-header", "parser-last-disposition", "parser-last-content-type",
    "parser-preamble", "parser-no-final-crlf", "parser-transport-padding",
    "parser-malformed-header", "parser-attachment-disposition", "parser-header-count",
    "parser-folded-header-limit", "parser-truncated-header", "parser-missing-boundary",
    "parser-last-boundary-parameter", "parser-part-length-exact", "parser-part-length-mismatch",
    "parser-utf7-title", "parser-utf16-no-bom-title", "parser-utf32-no-bom-title",
    "parser-utf32le-title", "parser-utf32be-title", "parser-replacement-character-title",
    "parser-independent-large-headers", "parser-file-count", "parser-part-count", "parser-memory-limit",
])
def test_wire_conformance_corpus_observes_the_actual_original_route(name, tmp_path):
    requests = list(upload.conformance_requests())
    assert len(requests) == 25 and len({row[0] for row in requests}) == 25
    label, raw, mime, query, expected_status, stored, _ = next(row for row in requests if row[0] == name)
    assert label == name
    reference = upload.original(tmp_path / "oracle", tmp_path / "oracle.log", raw, mime, query)
    assert reference["status"] == expected_status
    assert len(reference["storedMedia"]) == len(reference["sourceHashes"]) == stored
    if "title" in name:
        title = reference["body"]["media"]["title"]
        assert title == ("\ufffd" if name == "parser-replacement-character-title" else "café 中文")
