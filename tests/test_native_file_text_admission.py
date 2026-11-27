"""Oracle for the Rust ``POST /api/file-text`` text and empty-upload gate.

These tests call the shipped decoder and classifiers through
``file_text_admission``. They also require the Rust route to call the matching
functions before the cache write. They do not start Cargo, Go, or Docker, and
they do not treat a route registration as an extraction.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.native_runtime.file_text_admission import (
    MAX_UPLOAD_BYTES,
    admit_file_text_part,
    admit_upload_body,
)
from deepseek_infra.infra.rag.files import (
    decode_text_file,
    extract_docx_text,
    extract_epub_text,
    extract_pptx_text,
    extract_xlsx_text,
    normalize_extracted_text,
)
from deepseek_infra.web.server import format_upload_limit

ROOT = Path(__file__).resolve().parents[1]
UPLOAD = ROOT / "rust/crates/deepseek-policy/src/file_upload.rs"
ROUTE = ROOT / "rust/crates/deepseek-gateway/src/file_text_route.rs"
OCR = ROOT / "rust/crates/deepseek-policy/src/ocr.rs"
PNG = b"\x89PNG\r\n\x1a\nrest"
OCR_OFF = "Image OCR requires OCR to be enabled. Enable OCR and retry, or set OCR_ENABLED=1."


def _keep(decision: dict[str, Any], sink: list[str]) -> None:
    if decision.get("cacheWrite") is True:
        sink.append(str(decision.get("text") or ""))


def test_legal_text_upload_returns_decoded_text(tmp_path: Path) -> None:
    payload = b"hello file\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    decision = admit_file_text_part("notes.txt", "text/plain", payload, True)
    sink: list[str] = []
    _keep(decision, sink)
    assert decision["action"] == "text"
    assert decision["admit"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert expected == "hello file"
    assert decision["kind"] == "txt"
    assert sink == [expected]
    marked = b"\xef\xbb\xbfhello file\n"
    marked_text = normalize_extracted_text(decode_text_file(marked))
    marked_decision = admit_file_text_part("notes.txt", "text/plain", marked, False)
    assert marked_decision["text"] == marked_text
    assert marked_text == "hello file"
    cache = tmp_path / "notes.txt"
    if marked_decision["cacheWrite"] is True:
        cache.write_text(marked_decision["text"], encoding="utf-8")
    assert cache.read_text(encoding="utf-8") == marked_text


def test_empty_upload_refuses_before_cache_write(tmp_path: Path) -> None:
    sink: list[str] = []
    body = admit_upload_body(0)
    _keep(body, sink)
    assert body["admit"] is False
    assert body["cacheWrite"] is False
    assert body["code"] == ErrorCode.INVALID_PAYLOAD
    assert body["status"] == 400
    assert body["message"] == "Upload body is empty"
    part = admit_file_text_part("notes.txt", "text/plain", b"", True)
    _keep(part, sink)
    assert part["action"] == "refuse"
    assert part["cacheWrite"] is False
    assert part["message"] == "Uploaded file is empty: notes.txt"
    assert part["text"] == ""
    for bad in (True, False, None, "0", 1.5, -1):
        refused = admit_upload_body(bad)
        _keep(refused, sink)
        assert refused["admit"] is False
        assert refused["cacheWrite"] is False
        assert refused["message"] == "Upload body is empty"
    missing = admit_file_text_part("notes.txt", "text/plain", None, True)
    _keep(missing, sink)
    assert missing["cacheWrite"] is False
    assert missing["message"] == "Uploaded file is empty: notes.txt"
    assert sink == []
    assert list(tmp_path.iterdir()) == []


def test_blank_text_refuses_before_cache_write(tmp_path: Path) -> None:
    payload = b" \n\t "
    expected = normalize_extracted_text(decode_text_file(payload))
    decision = admit_file_text_part("blank.txt", "text/plain", payload, True)
    sink: list[str] = []
    _keep(decision, sink)
    assert expected == ""
    assert decision["action"] == "refuse"
    assert decision["cacheWrite"] is False
    assert decision["status"] == 422
    assert decision["message"] == "No readable text found in this file"
    assert sink == []
    assert list(tmp_path.iterdir()) == []


def test_body_gate_admits_a_small_length_and_refuses_oversize() -> None:
    legal = admit_upload_body(12)
    assert legal["admit"] is True
    assert legal["cacheWrite"] is False
    assert legal["message"] == ""
    capped = admit_upload_body(MAX_UPLOAD_BYTES)
    assert capped["admit"] is True
    assert capped["cacheWrite"] is False
    refused = admit_upload_body(MAX_UPLOAD_BYTES + 1)
    assert refused["admit"] is False
    assert refused["cacheWrite"] is False
    assert refused["code"] == ErrorCode.UPLOAD_TOO_LARGE
    assert refused["status"] == 413
    assert refused["message"] == (
        "Upload body is too large. Maximum request size is "
        f"{format_upload_limit(MAX_UPLOAD_BYTES)}."
    )


def test_corrupt_image_refuses_before_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("ocr entered")

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.ocr.extract_image_ocr", boom)
    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_image_text", boom)
    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.ocr.extract_pdf_ocr", boom)
    sink: list[str] = []
    corrupt = admit_file_text_part("scan.png", "image/png", b"not-an-image", True)
    _keep(corrupt, sink)
    assert corrupt["action"] == "refuse"
    assert corrupt["cacheWrite"] is False
    assert corrupt["code"] == ErrorCode.INVALID_PAYLOAD
    assert corrupt["status"] == 400
    assert corrupt["message"] == "Uploaded image is corrupt."
    assert corrupt["text"] == ""
    short = admit_file_text_part("scan.png", "image/png", b"\x89PNG", True)
    _keep(short, sink)
    assert short["message"] == "Uploaded image is corrupt."
    assert short["cacheWrite"] is False
    typed = admit_file_text_part("scan.bin", "image/png; charset=binary", b"not-an-image", True)
    assert typed["cacheWrite"] is False
    assert typed["message"] == "Uploaded image is corrupt."
    assert sink == []


def test_legal_image_header_continues_without_cache_or_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("ocr entered")

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.ocr.extract_image_ocr", boom)
    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_image_text", boom)
    headers = (
        PNG,
        b"\xff\xd8\xff\xe0",
        b"GIF87a",
        b"GIF89a",
        b"RIFF" + b"xxxx" + b"WEBP",
        b"BMrest",
        b"II*\x00rest",
        b"MM\x00*rest",
    )
    sink: list[str] = []
    for header in headers:
        decision = admit_file_text_part("scan.png", "image/png", header, True)
        _keep(decision, sink)
        assert decision["action"] == "continue"
        assert decision["cacheWrite"] is False
        assert decision["text"] == ""
        assert decision["code"] == ""
    disabled = admit_file_text_part("scan.png", "image/png", PNG, False)
    _keep(disabled, sink)
    assert disabled["action"] == "refuse"
    assert disabled["cacheWrite"] is False
    assert disabled["code"] == ErrorCode.OCR_REQUIRED
    assert disabled["status"] == 415
    assert disabled["message"] == OCR_OFF
    for flag in (0, None, "true", "1"):
        refused = admit_file_text_part("scan.png", "image/png", PNG, flag)
        _keep(refused, sink)
        assert refused["cacheWrite"] is False
        assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert sink == []


def test_pdf_stays_on_the_existing_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("extractor entered")

    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_pdf_text", boom)
    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_html_text", boom)
    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_docx_text", boom)
    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_pptx_text", boom)
    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_xlsx_text", boom)
    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_epub_text", boom)
    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_uploaded_file", boom)
    sink: list[str] = []
    for name, payload in (
        ("bad.pdf", b"this is not a pdf"),
        ("header.pdf", b"%PDF-1.4\n"),
    ):
        decision = admit_file_text_part(name, "application/octet-stream", payload, True)
        _keep(decision, sink)
        assert decision["action"] == "continue"
        assert decision["cacheWrite"] is False
        assert decision["text"] == ""
        assert "corrupt" not in decision["message"]
    assert sink == []


_DOCX_HELLO = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>Hello DOCX</w:t></w:r></w:p></w:body>
</w:document>
"""

_DOCX_BLANK = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t> </w:t></w:r></w:p></w:body>
</w:document>
"""


def _docx_package(document_xml: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def test_legal_docx_writes_cache_only_after_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("uploaded extractor entered")

    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_uploaded_file", boom)
    payload = _docx_package(_DOCX_HELLO)
    expected = normalize_extracted_text(extract_docx_text(payload))
    decision = admit_file_text_part(
        "notes.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        payload,
        False,
    )
    sink: list[str] = []
    _keep(decision, sink)
    assert decision["action"] == "text"
    assert decision["admit"] is True
    assert decision["cacheWrite"] is True
    assert decision["kind"] == "docx"
    assert decision["text"] == expected
    assert "Hello DOCX" in expected
    assert sink == [expected]
    record = tmp_path / "cache.txt"
    if decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected


def test_invalid_docx_refuses_before_cache_write(tmp_path: Path) -> None:
    sink: list[str] = []
    with pytest.raises(AppError) as bad_zip:
        extract_docx_text(b"PK")
    refused = admit_file_text_part("notes.docx", "application/octet-stream", b"PK", True)
    _keep(refused, sink)
    assert refused["action"] == "refuse"
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert str(bad_zip.value) == "Invalid docx file"
    assert refused["message"] == str(bad_zip.value)
    broken_xml = _docx_package("not xml")
    with pytest.raises(AppError) as bad_xml:
        extract_docx_text(broken_xml)
    broken = admit_file_text_part("notes.docx", "application/octet-stream", broken_xml, True)
    _keep(broken, sink)
    assert broken["action"] == "refuse"
    assert broken["cacheWrite"] is False
    assert broken["code"] == ErrorCode.INVALID_PAYLOAD
    assert broken["status"] == 422
    assert str(bad_xml.value) == "Invalid docx XML"
    assert broken["message"] == str(bad_xml.value)
    blank_payload = _docx_package(_DOCX_BLANK)
    assert extract_docx_text(blank_payload) == ""
    blank = admit_file_text_part("notes.docx", "application/octet-stream", blank_payload, True)
    _keep(blank, sink)
    assert blank["action"] == "refuse"
    assert blank["cacheWrite"] is False
    assert blank["text"] == ""
    assert blank["code"] == ErrorCode.INVALID_PAYLOAD
    assert blank["status"] == 422
    assert blank["message"] == "No readable text found in this file"
    assert blank["message"] != "Invalid docx file"
    assert sink == []
    assert list(tmp_path.iterdir()) == []


_PPTX_HELLO = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>
    <p:sp><p:txBody><a:p><a:r><a:t>Hello PPTX</a:t></a:r></a:p></p:txBody></p:sp>
  </p:spTree></p:cSld>
</p:sld>
"""

_PPTX_BLANK = """<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:spTree>
    <p:sp><p:txBody><a:p><a:r><a:t> </a:t></a:r></a:p></p:txBody></p:sp>
  </p:spTree></p:cSld>
</p:sld>
"""


def _pptx_package(slide_xml: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt/slides/slide1.xml", slide_xml)
    return buffer.getvalue()


def test_legal_pptx_writes_cache_only_after_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("uploaded extractor entered")

    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_uploaded_file", boom)
    payload = _pptx_package(_PPTX_HELLO)
    expected = normalize_extracted_text(extract_pptx_text(payload))
    decision = admit_file_text_part(
        "deck.pptx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        payload,
        False,
    )
    sink: list[str] = []
    _keep(decision, sink)
    assert decision["action"] == "text"
    assert decision["admit"] is True
    assert decision["cacheWrite"] is True
    assert decision["kind"] == "pptx"
    assert decision["text"] == expected
    assert "Hello PPTX" in expected
    assert sink == [expected]
    record = tmp_path / "cache.txt"
    if decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected


def test_invalid_pptx_refuses_before_cache_write(tmp_path: Path) -> None:
    sink: list[str] = []
    with pytest.raises(AppError) as bad_zip:
        extract_pptx_text(b"PK")
    refused = admit_file_text_part("deck.pptx", "application/octet-stream", b"PK", True)
    _keep(refused, sink)
    assert refused["action"] == "refuse"
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert str(bad_zip.value) == "Invalid pptx file"
    assert refused["message"] == str(bad_zip.value)
    broken_xml = _pptx_package("not xml")
    with pytest.raises(AppError) as bad_xml:
        extract_pptx_text(broken_xml)
    broken = admit_file_text_part("deck.pptx", "application/octet-stream", broken_xml, True)
    _keep(broken, sink)
    assert broken["action"] == "refuse"
    assert broken["cacheWrite"] is False
    assert broken["code"] == ErrorCode.INVALID_PAYLOAD
    assert broken["status"] == 422
    assert str(bad_xml.value) == "Invalid pptx XML"
    assert broken["message"] == str(bad_xml.value)
    blank_payload = _pptx_package(_PPTX_BLANK)
    assert extract_pptx_text(blank_payload) == ""
    blank = admit_file_text_part("deck.pptx", "application/octet-stream", blank_payload, True)
    _keep(blank, sink)
    assert blank["action"] == "refuse"
    assert blank["cacheWrite"] is False
    assert blank["text"] == ""
    assert blank["code"] == ErrorCode.INVALID_PAYLOAD
    assert blank["status"] == 422
    assert blank["message"] == "No readable text found in this file"
    assert blank["message"] != "Invalid pptx file"
    assert sink == []
    assert list(tmp_path.iterdir()) == []


def _xlsx_package(value: str | None) -> bytes:
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Data"
    if value is not None:
        sheet["A1"] = value
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _xlsx_not_workbook() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", "not xml")
    return buffer.getvalue()


def test_legal_xlsx_writes_cache_only_after_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("uploaded extractor entered")

    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_uploaded_file", boom)
    payload = _xlsx_package("Hello XLSX")
    expected = normalize_extracted_text(extract_xlsx_text(payload))
    decision = admit_file_text_part(
        "book.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        payload,
        False,
    )
    sink: list[str] = []
    _keep(decision, sink)
    assert decision["action"] == "text"
    assert decision["admit"] is True
    assert decision["cacheWrite"] is True
    assert decision["kind"] == "xlsx"
    assert decision["text"] == expected
    assert "Hello XLSX" in expected
    assert sink == [expected]
    record = tmp_path / "cache.txt"
    if decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected


def test_invalid_xlsx_refuses_before_cache_write(tmp_path: Path) -> None:
    sink: list[str] = []
    with pytest.raises(AppError) as bad_zip:
        extract_xlsx_text(b"PK")
    refused = admit_file_text_part("book.xlsx", "application/octet-stream", b"PK", True)
    _keep(refused, sink)
    assert refused["action"] == "refuse"
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["code"] == bad_zip.value.code
    assert refused["status"] == bad_zip.value.status
    assert str(bad_zip.value) == "Invalid xlsx file"
    assert refused["message"] == str(bad_zip.value)
    broken_payload = _xlsx_not_workbook()
    with pytest.raises(AppError) as broken_book:
        extract_xlsx_text(broken_payload)
    broken = admit_file_text_part("book.xlsx", "application/octet-stream", broken_payload, True)
    _keep(broken, sink)
    assert broken["action"] == "refuse"
    assert broken["cacheWrite"] is False
    assert broken["code"] == broken_book.value.code
    assert broken["status"] == broken_book.value.status
    assert broken["message"] == str(broken_book.value)
    empty_payload = _xlsx_package(None)
    assert extract_xlsx_text(empty_payload) == ""
    empty = admit_file_text_part("book.xlsx", "application/octet-stream", empty_payload, True)
    _keep(empty, sink)
    assert empty["action"] == "refuse"
    assert empty["cacheWrite"] is False
    assert empty["message"] == "No readable text found in this file"
    blank_payload = _xlsx_package(" ")
    assert extract_xlsx_text(blank_payload) == ""
    blank = admit_file_text_part("book.xlsx", "application/octet-stream", blank_payload, True)
    _keep(blank, sink)
    assert blank["action"] == "refuse"
    assert blank["cacheWrite"] is False
    assert blank["message"] == "No readable text found in this file"
    assert blank["message"] != "Invalid xlsx file"
    assert sink == []
    assert list(tmp_path.iterdir()) == []


def _epub_package(files: list[tuple[str, str]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files:
            archive.writestr(name, text)
    return buffer.getvalue()


def test_legal_epub_writes_cache_only_after_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("uploaded extractor entered")

    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_uploaded_file", boom)
    payload = _epub_package(
        [
            ("b.xhtml", "<h1>Later</h1>"),
            ("nav.xhtml", "<p>SECRET NAV</p>"),
            ("a.xhtml", "<p>Hello EPUB</p><script>secret()</script>"),
        ]
    )
    expected = normalize_extracted_text(extract_epub_text(payload))
    decision = admit_file_text_part("book.epub", "application/epub+zip", payload, False)
    sink: list[str] = []
    _keep(decision, sink)
    assert decision["action"] == "text"
    assert decision["admit"] is True
    assert decision["cacheWrite"] is True
    assert decision["kind"] == "epub"
    assert decision["text"] == expected
    assert "Hello EPUB" in expected
    assert "Later" in expected
    assert "SECRET" not in expected
    assert "secret" not in expected
    assert sink == [expected]
    record = tmp_path / "cache.txt"
    if decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected


def test_invalid_epub_refuses_before_cache_write(tmp_path: Path) -> None:
    sink: list[str] = []
    with pytest.raises(AppError) as bad_zip:
        extract_epub_text(b"PK")
    refused = admit_file_text_part("book.epub", "application/epub+zip", b"PK", True)
    _keep(refused, sink)
    assert refused["action"] == "refuse"
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["code"] == bad_zip.value.code
    assert refused["status"] == bad_zip.value.status
    assert str(bad_zip.value) == "Invalid epub file"
    assert refused["message"] == str(bad_zip.value)
    for files in (
        [("nav.xhtml", "<p>SECRET NAV</p>")],
        [("c.xhtml", "<p> </p>")],
        [("c.xhtml", "<script>secret()</script>")],
    ):
        payload = _epub_package(files)
        assert extract_epub_text(payload) == ""
        blank = admit_file_text_part("book.epub", "application/epub+zip", payload, True)
        _keep(blank, sink)
        assert blank["action"] == "refuse"
        assert blank["cacheWrite"] is False
        assert blank["text"] == ""
        assert blank["code"] == ErrorCode.INVALID_PAYLOAD
        assert blank["status"] == 422
        assert blank["message"] == "No readable text found in this file"
        assert "SECRET" not in blank["message"]
        assert "secret" not in blank["message"]
    assert sink == []
    assert list(tmp_path.iterdir()) == []


def test_legal_html_writes_cache_only_after_admission(tmp_path: Path) -> None:
    from deepseek_infra.infra.rag.files import extract_html_text, normalize_extracted_text

    payload = b"<script>secret</script><p>Hello</p>"
    expected = normalize_extracted_text(extract_html_text(payload))
    decision = admit_file_text_part("page.html", "text/html", payload, False)
    sink: list[str] = []
    _keep(decision, sink)
    assert decision["action"] == "text"
    assert decision["cacheWrite"] is True
    assert decision["kind"] == "html"
    assert decision["text"] == expected
    assert "Hello" in expected
    assert "secret" not in expected
    assert sink == [expected]
    record = tmp_path / "cache.txt"
    record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    htm = admit_file_text_part("page.htm", "text/html", b"<p>Hello</p>", False)
    assert htm["cacheWrite"] is True
    assert htm["kind"] == "htm"
    assert htm["text"] == normalize_extracted_text(extract_html_text(b"<p>Hello</p>"))


def test_unreadable_html_refuses_before_cache_write(tmp_path: Path) -> None:
    sink: list[str] = []
    for name, payload in (
        ("blank.html", b"<html></html>"),
        ("empty.htm", b"<p> </p>"),
        ("script.html", b"<script>secret</script>"),
    ):
        decision = admit_file_text_part(name, "text/html", payload, True)
        _keep(decision, sink)
        assert decision["action"] == "refuse"
        assert decision["cacheWrite"] is False
        assert decision["text"] == ""
        assert decision["code"] == ErrorCode.INVALID_PAYLOAD
        assert decision["status"] == 422
        assert decision["message"] == "No readable text found in this file"
        assert "secret" not in decision["message"]
    assert sink == []
    assert list(tmp_path.iterdir()) == []


def test_rust_route_calls_admission_before_cache_and_ocr() -> None:
    upload = UPLOAD.read_text(encoding="utf-8")
    route = ROUTE.read_text(encoding="utf-8")
    ocr = OCR.read_text(encoding="utf-8")
    reader = route.split("pub(crate) fn read_multipart_upload(", 1)[1]
    reader = reader.split("pub(crate) fn declared_content_length(", 1)[0]
    assert reader.index("multipart/form-data") < reader.index("admit_upload_body(")
    assert reader.index("admit_upload_body(") < reader.index("parse_multipart(")
    assert "content_length == 0" not in reader
    assert "content_length > MAX_UPLOAD_BYTES" not in reader
    parse = route.split("fn parse_and_extract(", 1)[1]
    parse = parse.split("pub(crate) fn read_multipart_upload(", 1)[0]
    assert parse.index("read_multipart_upload(") < parse.index("extract_upload_batch(")
    admit = upload.split("pub fn admit_file_text_part(", 1)[1]
    admit = admit.split("pub fn extract_uploaded_file(", 1)[0]
    html = admit.split("HTML_EXTENSIONS.contains(&extension.as_str())", 1)[1]
    html = html.split("if is_text_file(", 1)[0]
    assert html.index("extract_html_text(") < html.index("normalize_extracted_text(")
    assert "write_cache(" not in html
    assert 'cache_write: true' in html
    docx = admit.split('extension == ".docx"', 1)[1]
    docx = docx.split('extension == ".pptx"', 1)[0]
    assert docx.index("extract_docx_text(") < docx.index("normalize_extracted_text(")
    assert docx.index("refuse_file(") < docx.index("normalize_extracted_text(")
    assert "write_cache(" not in docx
    assert "cache_write: true" in docx
    for token in (
        'Command::new("python")',
        "deepseek_infra",
        "NOT_APPLIED",
        "CONFIRMED",
        "install_authoritative_epoch",
        "install_signed_epoch",
    ):
        assert token not in docx
    continued = admit.split('extension == ".pptx"', 1)[1]
    continued = continued.split("if is_image_file(", 1)[0]
    assert ".docx" not in continued
    assert ".pdf" in continued
    pptx = admit.split('extension == ".pptx"', 1)[1]
    pptx = pptx.split('extension == ".xlsx"', 1)[0]
    assert pptx.index("extract_pptx_text(") < pptx.index("normalize_extracted_text(")
    assert pptx.index("refuse_file(") < pptx.index("normalize_extracted_text(")
    assert "write_cache(" not in pptx
    assert "cache_write: true" in pptx
    for token in (
        'Command::new("python")',
        "deepseek_infra",
        "NOT_APPLIED",
        "CONFIRMED",
        "install_authoritative_epoch",
        "install_signed_epoch",
    ):
        assert token not in pptx
    xlsx = admit.split('extension == ".xlsx"', 1)[1]
    xlsx = xlsx.split('extension == ".epub"', 1)[0]
    assert xlsx.index("extract_xlsx_text(") < xlsx.index("normalize_extracted_text(")
    assert xlsx.index("refuse_file(") < xlsx.index("normalize_extracted_text(")
    assert "write_cache(" not in xlsx
    assert "cache_write: true" in xlsx
    for token in (
        'Command::new("python")',
        "deepseek_infra",
        "NOT_APPLIED",
        "CONFIRMED",
        "install_authoritative_epoch",
        "install_signed_epoch",
    ):
        assert token not in xlsx
    epub = admit.split('extension == ".epub"', 1)[1]
    epub = epub.split('extension == ".pdf"', 1)[0]
    assert epub.index("extract_epub_text(") < epub.index("normalize_extracted_text(")
    assert epub.index("refuse_file(") < epub.index("normalize_extracted_text(")
    assert "write_cache(" not in epub
    assert "cache_write: true" in epub
    for token in (
        'Command::new("python")',
        "deepseek_infra",
        "NOT_APPLIED",
        "CONFIRMED",
        "install_authoritative_epoch",
        "install_signed_epoch",
    ):
        assert token not in epub
    text_branch = admit.split("if is_text_file(", 1)[1]
    assert text_branch.index("decode_text_file(") < text_branch.index("normalize_extracted_text(")
    assert "ocr_image(" not in admit
    assert "ocr_pdf_document(" not in admit
    assert "write_cache(" not in admit
    assert "image_magic_ok(" in admit
    magic = upload.split("fn image_magic_ok(", 1)[1].split("pub fn admit_file_text_part(", 1)[0]
    assert r"\x89PNG\r\n\x1a\n" in magic
    assert r"\xff\xd8\xff" in magic
    assert "GIF87a" in magic
    assert "GIF89a" in magic
    assert "WEBP" in magic
    assert 'b"BM"' in magic
    assert r"II*\x00" in magic
    assert r"MM\x00*" in magic
    extract = upload.split("pub fn extract_uploaded_file(", 1)[1]
    extract = extract.split("pub fn cleanup_file_cache(", 1)[0]
    assert extract.index("admit_file_text_part(") < extract.index("write_cache(")
    assert extract.index("admit_file_text_part(") < extract.index("ocr_image(")
    assert extract.index("admit_file_text_part(") < extract.index("ocr_pdf_document(")
    assert extract.index("FileTextAction::Refuse") < extract.index("write_cache(")
    batch = upload.split("pub fn extract_upload_batch(", 1)[1]
    assert batch.index("files.is_empty()") < batch.index("extract_uploaded_file(")
    body_gate = upload.split("pub fn admit_upload_body(", 1)[1]
    body_gate = body_gate.split("fn format_upload_limit(", 1)[0]
    for source in (route, admit, body_gate):
        assert "deepseek_infra" not in source
        assert 'Command::new("python")' not in source
        assert "NOT_APPLIED" not in source
        assert "CONFIRMED" not in source
        assert "install_authoritative_epoch" not in source
        assert "install_signed_epoch" not in source
    assert "pix2tex" in ocr
    assert "Uploaded PDF is corrupt." not in upload
