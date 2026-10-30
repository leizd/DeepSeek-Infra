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
