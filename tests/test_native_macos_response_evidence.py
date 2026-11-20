from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_native_macos_product as probe

FILE_ID = "controlled-native-file"
TOKEN = "ephemeral-verifier-auth-token"


def original_capture(folder: Path) -> dict[str, Any]:
    index = b'<html><div id="root"></div><script src="/assets/original.js"></script></html>'
    data = {
        "control-before.json": (b'{"ok":true}', "GET", "/api/control/status"),
        "react-index.txt": (index, "GET", "/"),
        "file-upload.json": (json.dumps(dict(files=[dict(fileId=FILE_ID)])).encode(), "POST", "/api/file-text"),
        "file-reader.json": (json.dumps(dict(chunks=[dict(text=probe.ORIGINAL.decode())]), ensure_ascii=False).encode(), "POST", "/api/file-reader"),
        "file-source-before.txt": (probe.ORIGINAL, "GET", "/api/file-source?fileId=" + FILE_ID),
        "file-source-after.txt": (probe.ORIGINAL, "GET", "/api/file-source?fileId=" + FILE_ID),
        "control-after.json": (b'{"ok":true}', "GET", "/api/control/status"),
    }
    rows = {}
    for name, (raw, method, uri) in data.items():
        (folder / name).write_bytes(raw)
        rows[name] = dict(path=name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(), method=method, uri=uri, status=200)
    (folder / "product-fixture.json").write_text(json.dumps(dict(files=[dict(path="static/ui/index.html", bytes=len(index),
        sha256=hashlib.sha256(index).hexdigest())])), encoding="utf-8")
    return dict(retainedFileId=FILE_ID, originalFileSha256=hashlib.sha256(probe.ORIGINAL).hexdigest(),
        originalReactResponseSha256=hashlib.sha256(index).hexdigest(), responses=rows,
        originalFileRetainedAfterAppRestart=True, controlledNativeMacProductWorkloadQualified=True)


def replace_capture(folder: Path, report: dict[str, Any], name: str, raw: bytes) -> None:
    (folder / name).write_bytes(raw)
    report["responses"][name].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def test_raw_workload_binds_the_original_upload_id_reader_control_and_source_bytes_across_restart(tmp_path: Path) -> None:
    report = original_capture(tmp_path)
    result = probe.verify_workload_responses(tmp_path, report)
    assert result["responseCount"] == 7 and result["retainedFileId"] == FILE_ID
    assert result["originalFileSha256"] == hashlib.sha256(probe.ORIGINAL).hexdigest()


@pytest.mark.parametrize("change", ["missing", "hash-drift", "wrong-source-with-valid-hash", "wrong-upload-id",
    "empty-reader", "control-unhealthy", "wrong-method", "wrong-uri", "wrong-status", "wrong-ui-with-valid-hash",
    "claimed-source-hash", "extra-response", "no-raw-responses"])
def test_success_flags_cannot_replace_missing_altered_or_unbound_raw_workload(
    tmp_path: Path, change: str,
) -> None:
    report = original_capture(tmp_path)
    if change == "missing":
        (tmp_path / "file-source-after.txt").unlink()
    elif change == "hash-drift":
        (tmp_path / "file-source-after.txt").write_bytes(b"changed after capture")
    elif change == "wrong-source-with-valid-hash":
        replace_capture(tmp_path, report, "file-source-after.txt", b"different retained data")
    elif change == "wrong-upload-id":
        replace_capture(tmp_path, report, "file-upload.json", b'{"files":[{"fileId":"another-file"}]}')
    elif change == "empty-reader":
        replace_capture(tmp_path, report, "file-reader.json", b'{"ok":true,"chunks":[]}')
    elif change == "control-unhealthy":
        replace_capture(tmp_path, report, "control-after.json", b'{"ok":false}')
    elif change == "wrong-method":
        report["responses"]["file-upload.json"]["method"] = "GET"
    elif change == "wrong-uri":
        report["responses"]["file-source-after.txt"]["uri"] = "/api/file-source?fileId=another-file"
    elif change == "wrong-status":
        report["responses"]["file-source-after.txt"]["status"] = 503
    elif change == "wrong-ui-with-valid-hash":
        raw = b'<div id="root"></div><script src="/assets/substitute.js"></script>'
        replace_capture(tmp_path, report, "react-index.txt", raw)
        report["originalReactResponseSha256"] = hashlib.sha256(raw).hexdigest()
    elif change == "claimed-source-hash":
        report["originalFileSha256"] = "0" * 64
    elif change == "extra-response":
        report["responses"]["another-upload.json"] = dict(report["responses"]["file-upload.json"])
    else:
        report.pop("responses")
    with pytest.raises(RuntimeError, match="MACOS_WORKLOAD_RESPONSE"):
        probe.verify_workload_responses(tmp_path, report)


def test_recorder_retains_the_received_bytes_and_status_without_authentication_headers(tmp_path: Path) -> None:
    raw = b'{"files":[{"fileId":"controlled-native-file"}]}'
    row = probe.record_response(tmp_path, "file-upload.json", raw, TOKEN, method="POST", uri="/api/file-text", status=200)
    assert (tmp_path / row["path"]).read_bytes() == raw
    assert row == dict(path="file-upload.json", bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
                       method="POST", uri="/api/file-text", status=200)
    assert TOKEN not in json.dumps(row)


@pytest.mark.parametrize("case", ["auth-in-body", "unsafe-name", "duplicate", "non-success-status"])
def test_recorder_refuses_authentication_disclosure_path_escape_overwrite_or_failed_http(tmp_path: Path, case: str) -> None:
    name, raw, status = "file-upload.json", b"original", 200
    if case == "auth-in-body":
        raw = TOKEN.encode()
    elif case == "unsafe-name":
        name = "../file-upload.json"
    elif case == "duplicate":
        (tmp_path / name).write_bytes(b"existing")
    else:
        status = 500
    with pytest.raises(RuntimeError, match="MACOS_WORKLOAD_RESPONSE"):
        probe.record_response(tmp_path, name, raw, TOKEN, method="POST", uri="/api/file-text", status=status)
    if case == "duplicate":
        assert (tmp_path / name).read_bytes() == b"existing"
    else:
        assert list(tmp_path.iterdir()) == []


def test_http_capture_records_the_actual_returned_response_not_a_filled_success_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status = 200

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def read(self, limit: int) -> bytes:
            assert limit == 8 * 1024**2 + 1
            return probe.ORIGINAL

    calls = []

    def urlopen(request: Any, timeout: int) -> Response:
        calls.append((request.get_method(), request.full_url, timeout))
        return Response()

    monkeypatch.setattr(probe, "urlopen", urlopen)
    report: dict[str, Any] = {}
    raw = probe.request("http://127.0.0.1:12345", "/api/file-source?fileId=" + FILE_ID, TOKEN,
        evidence=(tmp_path, report, "file-source-before.txt"))
    assert calls == [("GET", "http://127.0.0.1:12345/api/file-source?fileId=" + FILE_ID, 15)]
    assert raw == (tmp_path / "file-source-before.txt").read_bytes() == probe.ORIGINAL
    assert report["responses"]["file-source-before.txt"]["status"] == 200
