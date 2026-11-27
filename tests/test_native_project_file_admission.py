"""Oracle for the Rust ``POST /api/project-files`` writer gate.

These tests call the shipped project-file admission, which calls the shipped
text decoder. They also require the Rust route to refuse a Python-owned store
before parsing, and to refuse an empty part before ``add_project_files``.
They do not start Cargo, Go, or Docker.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.native_runtime.file_text_admission import admit_upload_body
from deepseek_infra.infra.native_runtime.project_file_admission import (
    NOT_OWNED,
    NOT_OWNED_MESSAGE,
    admit_project_file_part,
    admit_project_metadata_write,
)
from deepseek_infra.infra.rag.files import (
    decode_text_file,
    extract_docx_text,
    extract_epub_text,
    extract_html_text,
    extract_pptx_text,
    extract_xlsx_text,
    normalize_extracted_text,
)

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / "rust/crates/deepseek-gateway/src/project_files_route.rs"
PROJECTS = ROOT / "rust/crates/deepseek-policy/src/projects.rs"
UPLOAD = ROOT / "rust/crates/deepseek-gateway/src/file_text_route.rs"
POLICY = ROOT / "rust/crates/deepseek-policy/src/file_upload.rs"


def _store(decision: dict[str, Any], metadata: list[str], cache: list[str]) -> None:
    if decision.get("metadataWrite") is True and decision.get("cacheWrite") is True:
        text = str(decision.get("text") or "")
        metadata.append(text)
        cache.append(text)


def test_legal_text_attaches_only_for_the_native_writer(tmp_path: Path) -> None:
    payload = b"hello file\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["attach"] is False
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "notes.txt", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert expected == "hello file"
    assert decision["kind"] == "txt"
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected


def test_python_owner_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("project writer entered")

    monkeypatch.setattr("deepseek_infra.infra.data.projects.write_project", boom)
    monkeypatch.setattr("deepseek_infra.infra.data.projects.add_project_files", boom)
    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_uploaded_file", boom)
    secret = b"secret notes\n"
    metadata: list[str] = []
    cache: list[str] = []
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["attach"] is False
        assert ownership["metadataWrite"] is False
        assert ownership["cacheWrite"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.txt", "text/plain", secret, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert secret.decode("utf-8").strip() not in part["message"]
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_empty_upload_refuses_before_metadata_or_cache(tmp_path: Path) -> None:
    owned = admit_project_metadata_write("PYTHON_DISABLED")
    assert owned["allowed"] is True
    body = admit_upload_body(0)
    metadata: list[str] = []
    cache: list[str] = []
    _store(body, metadata, cache)
    assert body["admit"] is False
    assert body["cacheWrite"] is False
    assert body["message"] == "Upload body is empty"
    empty = admit_project_file_part(True, "notes.txt", "text/plain", b"", True)
    _store(empty, metadata, cache)
    assert empty["attach"] is False
    assert empty["metadataWrite"] is False
    assert empty["cacheWrite"] is False
    assert empty["code"] == ErrorCode.INVALID_PAYLOAD
    assert empty["message"] == "Uploaded file is empty: notes.txt"
    blank = admit_project_file_part(True, "blank.txt", "text/plain", b" \n\t ", True)
    _store(blank, metadata, cache)
    assert blank["attach"] is False
    assert blank["metadataWrite"] is False
    assert blank["cacheWrite"] is False
    assert blank["status"] == 422
    assert blank["message"] == "No readable text found in this file"
    corrupt = admit_project_file_part(True, "scan.png", "image/png", b"not-an-image", True)
    _store(corrupt, metadata, cache)
    assert corrupt["attach"] is False
    assert corrupt["cacheWrite"] is False
    assert corrupt["message"] == "Uploaded image is corrupt."
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_continued_formats_do_not_invent_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("extractor entered")

    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_uploaded_file", boom)
    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_pdf_text", boom)
    for name, payload in (
        ("bad.pdf", b"this is not a pdf"),
        ("scan.png", b"\x89PNG\r\n\x1a\nrest"),
    ):
        decision = admit_project_file_part(True, name, "application/octet-stream", payload, True)
        assert decision["attach"] is True
        assert decision["metadataWrite"] is False
        assert decision["cacheWrite"] is False
        assert decision["text"] == ""
    page = b"<p>Hello</p>"
    html = admit_project_file_part(True, "page.html", "text/html", page, False)
    expected = normalize_extracted_text(extract_html_text(page))
    assert html["attach"] is True
    assert html["metadataWrite"] is True
    assert html["cacheWrite"] is True
    assert html["text"] == expected
    assert "Hello" in expected
    blank = admit_project_file_part(True, "blank.html", "text/html", b"<script>secret</script>", True)
    assert blank["attach"] is False
    assert blank["metadataWrite"] is False
    assert blank["cacheWrite"] is False
    assert blank["text"] == ""
    assert "secret" not in blank["message"]


def _epub_package(files: list[tuple[str, str]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in files:
            archive.writestr(name, text)
    return buffer.getvalue()


def _block_project_writers(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("project writer entered")

    monkeypatch.setattr("deepseek_infra.infra.data.projects.write_project", boom)
    monkeypatch.setattr("deepseek_infra.infra.data.projects.add_project_files", boom)
    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_uploaded_file", boom)


def _refuse_epub_extractor(*_args: Any, **_kwargs: Any) -> str:
    raise AssertionError("epub extractor entered")


def _refuse_docx_extractor(*_args: Any, **_kwargs: Any) -> str:
    raise AssertionError("docx extractor entered")


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


def _refuse_pptx_extractor(*_args: Any, **_kwargs: Any) -> str:
    raise AssertionError("pptx extractor entered")


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


def _refuse_xlsx_extractor(*_args: Any, **_kwargs: Any) -> str:
    raise AssertionError("xlsx extractor entered")


def _refuse_html_extractor(*_args: Any, **_kwargs: Any) -> str:
    raise AssertionError("html extractor entered")


def _refuse_text_decoder(*_args: Any, **_kwargs: Any) -> str:
    raise AssertionError("text decoder entered")


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


def test_legal_epub_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = _epub_package(
        [
            ("b.xhtml", "<h1>Later</h1>"),
            ("nav.xhtml", "<p>SECRET NAV</p>"),
            ("a.xhtml", "<p>Hello EPUB</p><script>secret()</script>"),
        ]
    )
    expected = normalize_extracted_text(extract_epub_text(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["attach"] is False
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "book.epub", "application/epub+zip", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "epub"
    assert "Hello EPUB" in expected
    assert "Later" in expected
    assert "SECRET" not in expected
    assert "secret" not in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_epub_text",
        _refuse_epub_extractor,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "book.epub", "application/epub+zip", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello EPUB" not in part["message"]
        assert "SECRET" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_invalid_epub_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    with pytest.raises(AppError) as bad_zip:
        extract_epub_text(b"PK")
    refused = admit_project_file_part(True, "book.epub", "application/epub+zip", b"PK", True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
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
        blank = admit_project_file_part(True, "book.epub", "application/epub+zip", payload, True)
        _store(blank, metadata, cache)
        assert blank["attach"] is False
        assert blank["metadataWrite"] is False
        assert blank["cacheWrite"] is False
        assert blank["text"] == ""
        assert blank["code"] == ErrorCode.INVALID_PAYLOAD
        assert blank["status"] == 422
        assert blank["message"] == "No readable text found in this file"
        assert "SECRET" not in blank["message"]
        assert "secret" not in blank["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_epub_text",
        _refuse_epub_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_docx_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = _docx_package(_DOCX_HELLO)
    expected = normalize_extracted_text(extract_docx_text(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "notes.docx", "application/octet-stream", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "docx"
    assert "Hello DOCX" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_docx_text",
        _refuse_docx_extractor,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.docx", "application/octet-stream", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello DOCX" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_invalid_docx_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    with pytest.raises(AppError) as bad_zip:
        extract_docx_text(b"PK")
    refused = admit_project_file_part(True, "notes.docx", "application/octet-stream", b"PK", True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["code"] == bad_zip.value.code
    assert refused["status"] == bad_zip.value.status
    assert str(bad_zip.value) == "Invalid docx file"
    assert refused["message"] == str(bad_zip.value)
    broken_xml = _docx_package("not xml")
    with pytest.raises(AppError) as bad_xml:
        extract_docx_text(broken_xml)
    broken = admit_project_file_part(True, "notes.docx", "application/octet-stream", broken_xml, True)
    _store(broken, metadata, cache)
    assert broken["attach"] is False
    assert broken["metadataWrite"] is False
    assert broken["cacheWrite"] is False
    assert broken["code"] == ErrorCode.INVALID_PAYLOAD
    assert broken["status"] == 422
    assert str(bad_xml.value) == "Invalid docx XML"
    assert broken["message"] == str(bad_xml.value)
    blank_payload = _docx_package(_DOCX_BLANK)
    assert extract_docx_text(blank_payload) == ""
    blank = admit_project_file_part(True, "notes.docx", "application/octet-stream", blank_payload, True)
    _store(blank, metadata, cache)
    assert blank["attach"] is False
    assert blank["metadataWrite"] is False
    assert blank["cacheWrite"] is False
    assert blank["text"] == ""
    assert blank["code"] == ErrorCode.INVALID_PAYLOAD
    assert blank["status"] == 422
    assert blank["message"] == "No readable text found in this file"
    assert blank["message"] != "Invalid docx file"
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_docx_text",
        _refuse_docx_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_pptx_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = _pptx_package(_PPTX_HELLO)
    expected = normalize_extracted_text(extract_pptx_text(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "deck.pptx", "application/octet-stream", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "pptx"
    assert "Hello PPTX" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_pptx_text",
        _refuse_pptx_extractor,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "deck.pptx", "application/octet-stream", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello PPTX" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_invalid_pptx_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    with pytest.raises(AppError) as bad_zip:
        extract_pptx_text(b"PK")
    refused = admit_project_file_part(True, "deck.pptx", "application/octet-stream", b"PK", True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["code"] == bad_zip.value.code
    assert refused["status"] == bad_zip.value.status
    assert str(bad_zip.value) == "Invalid pptx file"
    assert refused["message"] == str(bad_zip.value)
    broken_xml = _pptx_package("not xml")
    with pytest.raises(AppError) as bad_xml:
        extract_pptx_text(broken_xml)
    broken = admit_project_file_part(True, "deck.pptx", "application/octet-stream", broken_xml, True)
    _store(broken, metadata, cache)
    assert broken["attach"] is False
    assert broken["metadataWrite"] is False
    assert broken["cacheWrite"] is False
    assert broken["code"] == ErrorCode.INVALID_PAYLOAD
    assert broken["status"] == 422
    assert str(bad_xml.value) == "Invalid pptx XML"
    assert broken["message"] == str(bad_xml.value)
    blank_payload = _pptx_package(_PPTX_BLANK)
    assert extract_pptx_text(blank_payload) == ""
    blank = admit_project_file_part(True, "deck.pptx", "application/octet-stream", blank_payload, True)
    _store(blank, metadata, cache)
    assert blank["attach"] is False
    assert blank["metadataWrite"] is False
    assert blank["cacheWrite"] is False
    assert blank["text"] == ""
    assert blank["code"] == ErrorCode.INVALID_PAYLOAD
    assert blank["status"] == 422
    assert blank["message"] == "No readable text found in this file"
    assert blank["message"] != "Invalid pptx file"
    assert blank["message"] != "Invalid pptx XML"
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_pptx_text",
        _refuse_pptx_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_xlsx_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = _xlsx_package("Hello XLSX")
    expected = normalize_extracted_text(extract_xlsx_text(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "book.xlsx", "application/octet-stream", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "xlsx"
    assert "Hello XLSX" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_xlsx_text",
        _refuse_xlsx_extractor,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "book.xlsx", "application/octet-stream", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello XLSX" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_invalid_xlsx_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    with pytest.raises(AppError) as bad_zip:
        extract_xlsx_text(b"PK")
    refused = admit_project_file_part(True, "book.xlsx", "application/octet-stream", b"PK", True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["code"] == bad_zip.value.code
    assert refused["status"] == bad_zip.value.status
    assert str(bad_zip.value) == "Invalid xlsx file"
    assert refused["message"] == str(bad_zip.value)
    broken_xml = _xlsx_not_workbook()
    with pytest.raises(AppError) as bad_book:
        extract_xlsx_text(broken_xml)
    broken = admit_project_file_part(True, "book.xlsx", "application/octet-stream", broken_xml, True)
    _store(broken, metadata, cache)
    assert broken["attach"] is False
    assert broken["metadataWrite"] is False
    assert broken["cacheWrite"] is False
    assert broken["code"] == ErrorCode.INVALID_PAYLOAD
    assert broken["status"] == 422
    assert str(bad_book.value) == "Invalid xlsx file"
    assert broken["message"] == str(bad_book.value)
    for value in (None, " "):
        payload = _xlsx_package(value)
        assert extract_xlsx_text(payload) == ""
        blank = admit_project_file_part(True, "book.xlsx", "application/octet-stream", payload, True)
        _store(blank, metadata, cache)
        assert blank["attach"] is False
        assert blank["metadataWrite"] is False
        assert blank["cacheWrite"] is False
        assert blank["text"] == ""
        assert blank["code"] == ErrorCode.INVALID_PAYLOAD
        assert blank["status"] == 422
        assert blank["message"] == "No readable text found in this file"
        assert blank["message"] != "Invalid xlsx file"
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_xlsx_text",
        _refuse_xlsx_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_htm_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = b"<script>secret</script><p>Hello HTM</p>"
    expected = normalize_extracted_text(extract_html_text(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "page.htm", "text/html", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "htm"
    assert "Hello HTM" in expected
    assert "secret" not in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.htm", "text/html", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTM" not in part["message"]
        assert "secret" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_script_only_htm_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b"<script>secret</script>"
    assert extract_html_text(payload) == ""
    refused = admit_project_file_part(True, "page.htm", "text/html", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    assert "secret" not in refused["message"]
    blank_payload = b"<p> </p>"
    assert extract_html_text(blank_payload) == ""
    blank = admit_project_file_part(True, "blank.htm", "text/html", blank_payload, True)
    _store(blank, metadata, cache)
    assert blank["attach"] is False
    assert blank["metadataWrite"] is False
    assert blank["cacheWrite"] is False
    assert blank["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_markdown_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = b"# Hello MD\n\nsecret stays\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "Notes.MD", "text/markdown", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "md"
    assert "Hello MD" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "Notes.MD", "text/markdown", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello MD" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_markdown_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.md", "text/markdown", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_csv_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = b"name,note\nAda,Hello CSV\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "sheet.CSV", "text/csv", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "csv"
    assert "Hello CSV" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "sheet.CSV", "text/csv", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello CSV" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_csv_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.csv", "text/csv", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_json_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = b'{"note":"Hello JSON"}\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "notes.JSON", "application/json", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "json"
    assert "Hello JSON" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.JSON", "application/json", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello JSON" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_json_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.json", "application/json", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_yaml_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = b"note: Hello YAML\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "config.YAML", "text/yaml", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "yaml"
    assert "Hello YAML" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "config.YAML", "text/yaml", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello YAML" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_yaml_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.yaml", "text/yaml", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_yml_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = b"note: Hello YML\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "config.YML", "text/yaml", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "yml"
    assert "Hello YML" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "config.YML", "text/yaml", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello YML" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_yml_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.yml", "text/yaml", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_markdown_extension_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = b"# Hello MARKDOWN\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "notes.MARKDOWN", "text/markdown", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "markdown"
    assert "Hello MARKDOWN" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.MARKDOWN", "text/markdown", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello MARKDOWN" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_markdown_extension_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.markdown", "text/markdown", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_tsv_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = b"name\tnote\nAda\tHello TSV\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "sheet.TSV", "text/tab-separated-values", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "tsv"
    assert "Hello TSV" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "sheet.TSV", "text/tab-separated-values", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello TSV" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_tsv_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.tsv", "text/tab-separated-values", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_jsonl_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    payload = b'{"note":"Hello JSONL"}\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "rows.JSONL", "application/jsonl", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "jsonl"
    assert "Hello JSONL" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "rows.JSONL", "application/jsonl", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello JSONL" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_jsonl_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.jsonl", "application/jsonl", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_xml_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b"<note>Hello XML</note>\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "notes.XML", "application/xml", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "xml"
    assert "<note>Hello XML</note>" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.XML", "application/xml", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello XML" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_xml_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.xml", "application/xml", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_css_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b".note { color: Hello CSS; }\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "theme.CSS", "text/css", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "css"
    assert ".note { color: Hello CSS; }" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "theme.CSS", "text/css", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello CSS" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_css_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.css", "text/css", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_js_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'const note = "Hello JS";\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "app.JS", "text/javascript", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "js"
    assert 'const note = "Hello JS";' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "app.JS", "text/javascript", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello JS" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_js_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.js", "text/javascript", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_mjs_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'export const note = "Hello MJS";\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "app.MJS", "text/javascript", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "mjs"
    assert 'export const note = "Hello MJS";' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "app.MJS", "text/javascript", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello MJS" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_mjs_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.mjs", "text/javascript", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_cjs_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'module.exports = "Hello CJS";\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "app.CJS", "text/javascript", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "cjs"
    assert 'module.exports = "Hello CJS";' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "app.CJS", "text/javascript", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello CJS" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_cjs_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.cjs", "text/javascript", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_ts_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'export const note: string = "Hello TS";\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "app.TS", "text/typescript", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "ts"
    assert 'export const note: string = "Hello TS";' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "app.TS", "text/typescript", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello TS" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_ts_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.ts", "text/typescript", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_tsx_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'export const view = "Hello TSX";\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "view.TSX", "text/typescript", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "tsx"
    assert 'export const view = "Hello TSX";' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "view.TSX", "text/typescript", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello TSX" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_tsx_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.tsx", "text/typescript", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_jsx_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'export const view = "Hello JSX";\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "view.JSX", "text/jsx", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "jsx"
    assert 'export const view = "Hello JSX";' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "view.JSX", "text/jsx", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello JSX" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_jsx_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.jsx", "text/jsx", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_py_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'def note():\n    return "Hello PY"\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "notes.PY", "text/x-python", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "py"
    assert 'return "Hello PY"' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.PY", "text/x-python", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello PY" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_py_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.py", "text/x-python", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_java_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'class Note { String text = "Hello JAVA"; }\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "Note.JAVA", "text/x-java-source", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "java"
    assert 'String text = "Hello JAVA";' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "Note.JAVA", "text/x-java-source", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello JAVA" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_java_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.java", "text/x-java-source", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_c_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'const char *note = "Hello C";\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "main.C", "text/x-c", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "c"
    assert 'const char *note = "Hello C";' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "main.C", "text/x-c", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello C" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_c_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.c", "text/x-c", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_cpp_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'const char *note = "Hello CPP";\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "main.CPP", "text/x-c++src", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "cpp"
    assert 'const char *note = "Hello CPP";' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "main.CPP", "text/x-c++src", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello CPP" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_cpp_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.cpp", "text/x-c++src", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_h_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'#define NOTE "Hello H"\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.H", "text/x-c", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "h"
    assert '#define NOTE "Hello H"' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.H", "text/x-c", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello H" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_h_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.h", "text/x-c", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_hpp_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'#define NOTE "Hello HPP"\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.HPP", "text/x-c++hdr", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "hpp"
    assert '#define NOTE "Hello HPP"' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.HPP", "text/x-c++hdr", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HPP" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_hpp_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.hpp", "text/x-c++hdr", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_cs_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'namespace Note { const string Text = "Hello CS"; }\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "Note.CS", "text/x-csharp", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "cs"
    assert 'const string Text = "Hello CS";' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "Note.CS", "text/x-csharp", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello CS" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_cs_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.cs", "text/x-csharp", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_go_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'package note\n\nconst Text = "Hello GO"\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.GO", "text/x-go", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "go"
    assert 'const Text = "Hello GO"' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.GO", "text/x-go", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello GO" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_go_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.go", "text/x-go", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_rs_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'fn note() -> &\'static str { "Hello RS" }\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.RS", "text/x-rust", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "rs"
    assert '"Hello RS"' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.RS", "text/x-rust", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello RS" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_rs_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.rs", "text/x-rust", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_php_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'<?php echo "Hello PHP";\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.PHP", "text/x-php", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "php"
    assert '<?php echo "Hello PHP";' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.PHP", "text/x-php", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello PHP" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_php_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.php", "text/x-php", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_rb_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'note = "Hello RB"\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.RB", "text/x-ruby", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "rb"
    assert 'note = "Hello RB"' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.RB", "text/x-ruby", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello RB" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_rb_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.rb", "text/x-ruby", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_swift_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'let note = "Hello SWIFT"\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.SWIFT", "text/x-swift", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "swift"
    assert 'let note = "Hello SWIFT"' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.SWIFT", "text/x-swift", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello SWIFT" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_swift_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.swift", "text/x-swift", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_kt_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'val note = "Hello KT"\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.KT", "text/x-kotlin", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "kt"
    assert 'val note = "Hello KT"' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.KT", "text/x-kotlin", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello KT" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_kt_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.kt", "text/x-kotlin", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_sql_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'SELECT note AS "Hello SQL";\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "notes.SQL", "text/x-sql", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "sql"
    assert 'SELECT note AS "Hello SQL";' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.SQL", "text/x-sql", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello SQL" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_sql_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.sql", "text/x-sql", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_sh_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'echo "Hello SH"\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "run.SH", "text/x-shellscript", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "sh"
    assert 'echo "Hello SH"' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "run.SH", "text/x-shellscript", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello SH" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_sh_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.sh", "text/x-shellscript", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_ps1_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'Write-Output "Hello PS1"\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "run.PS1", "text/x-powershell", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "ps1"
    assert 'Write-Output "Hello PS1"' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "run.PS1", "text/x-powershell", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello PS1" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_ps1_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.ps1", "text/x-powershell", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_bat_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b"@echo Hello BAT\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "run.BAT", "application/x-bat", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "bat"
    assert "@echo Hello BAT" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "run.BAT", "application/x-bat", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello BAT" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_bat_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.bat", "application/x-bat", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_log_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b"INFO Hello LOG\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "app.LOG", "text/x-log", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "log"
    assert "INFO Hello LOG" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "app.LOG", "text/x-log", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello LOG" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_log_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.log", "text/x-log", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_ini_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b"[note]\ntext = Hello INI\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "app.INI", "text/x-ini", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "ini"
    assert "text = Hello INI" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "app.INI", "text/x-ini", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello INI" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_ini_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.ini", "text/x-ini", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_toml_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b'note = "Hello TOML"\n'
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "config.TOML", "application/toml", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "toml"
    assert 'note = "Hello TOML"' in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "config.TOML", "application/toml", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello TOML" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_toml_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.toml", "application/toml", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_env_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b"NOTE=Hello ENV\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "app.ENV", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "env"
    assert "NOTE=Hello ENV" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "app.ENV", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello ENV" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_env_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.env", "text/plain", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_rtf_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b"{\\rtf1 Hello RTF}\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.RTF", "application/rtf", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "rtf"
    assert "{\\rtf1 Hello RTF}" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.RTF", "application/rtf", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello RTF" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_rtf_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.rtf", "application/rtf", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_PNG_HEADER = b"\x89PNG\r\n\x1a\nrest"
_OCR_REQUIRED_MESSAGE = (
    "Image OCR requires OCR to be enabled. Enable OCR and retry, or set OCR_ENABLED=1."
)


def _refuse_image_magic(*_args: Any, **_kwargs: Any) -> bool:
    raise AssertionError("image magic entered")


def _refuse_file_text_part(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    raise AssertionError("file text part entered")


def test_png_ocr_off_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    refused = admit_project_file_part(True, "scan.PNG", "image/png", _PNG_HEADER, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "rest" not in refused["message"]
    assert metadata == []
    assert cache == []
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.PNG", "image/png", _PNG_HEADER, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "rest" not in part["message"]
    assert metadata == []
    assert cache == []
    assert list(denied.iterdir()) == []


def test_png_header_with_ocr_continues_without_invented_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "scan.PNG", "image/png", _PNG_HEADER, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_JPEG_HEADER = b"\xff\xd8\xffMARKERjpg"


def test_jpg_ocr_off_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    refused = admit_project_file_part(True, "scan.JPG", "image/jpeg", _JPEG_HEADER, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERjpg" not in refused["message"]
    assert metadata == []
    assert cache == []
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.JPG", "image/jpeg", _JPEG_HEADER, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERjpg" not in part["message"]
    assert metadata == []
    assert cache == []
    assert list(denied.iterdir()) == []


def test_jpg_header_with_ocr_continues_without_invented_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "scan.JPG", "image/jpeg", _JPEG_HEADER, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "MARKERjpg" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_JPEG_LONG_HEADER = b"\xff\xd8\xffMARKERjpeg"


def test_jpeg_ocr_off_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    refused = admit_project_file_part(True, "scan.JPEG", "image/jpeg", _JPEG_LONG_HEADER, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERjpeg" not in refused["message"]
    assert metadata == []
    assert cache == []
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.JPEG", "image/jpeg", _JPEG_LONG_HEADER, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERjpeg" not in part["message"]
    assert metadata == []
    assert cache == []
    assert list(denied.iterdir()) == []


def test_jpeg_header_with_ocr_continues_without_invented_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "scan.JPEG", "image/jpeg", _JPEG_LONG_HEADER, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "MARKERjpeg" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_WEBP_HEADER = b"RIFF" + b"xxxx" + b"WEBP" + b"MARKERwebp"


def test_webp_ocr_off_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    refused = admit_project_file_part(True, "scan.WEBP", "image/webp", _WEBP_HEADER, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERwebp" not in refused["message"]
    assert metadata == []
    assert cache == []
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.WEBP", "image/webp", _WEBP_HEADER, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERwebp" not in part["message"]
    assert metadata == []
    assert cache == []
    assert list(denied.iterdir()) == []


def test_webp_header_with_ocr_continues_without_invented_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "scan.WEBP", "image/webp", _WEBP_HEADER, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "MARKERwebp" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_BMP_HEADER = b"BMMARKERbmp"


def test_bmp_ocr_off_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    refused = admit_project_file_part(True, "scan.BMP", "image/bmp", _BMP_HEADER, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERbmp" not in refused["message"]
    assert metadata == []
    assert cache == []
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.BMP", "image/bmp", _BMP_HEADER, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERbmp" not in part["message"]
    assert metadata == []
    assert cache == []
    assert list(denied.iterdir()) == []


def test_bmp_header_with_ocr_continues_without_invented_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "scan.BMP", "image/bmp", _BMP_HEADER, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "MARKERbmp" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_TIF_HEADER = b"II*\x00MARKERtif"


def test_tif_ocr_off_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    refused = admit_project_file_part(True, "scan.TIF", "image/tiff", _TIF_HEADER, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERtif" not in refused["message"]
    assert metadata == []
    assert cache == []
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.TIF", "image/tiff", _TIF_HEADER, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERtif" not in part["message"]
    assert metadata == []
    assert cache == []
    assert list(denied.iterdir()) == []


def test_tif_header_with_ocr_continues_without_invented_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "scan.TIF", "image/tiff", _TIF_HEADER, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "MARKERtif" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_TIFF_HEADER = b"MM\x00*MARKERtiff"


def test_tiff_ocr_off_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    refused = admit_project_file_part(True, "scan.TIFF", "image/tiff", _TIFF_HEADER, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERtiff" not in refused["message"]
    assert metadata == []
    assert cache == []
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.TIFF", "image/tiff", _TIFF_HEADER, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERtiff" not in part["message"]
    assert metadata == []
    assert cache == []
    assert list(denied.iterdir()) == []


def test_tiff_header_with_ocr_continues_without_invented_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "scan.TIFF", "image/tiff", _TIFF_HEADER, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "MARKERtiff" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_GIF_HEADER = b"GIF89aMARKERgif"


def test_gif_ocr_off_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    refused = admit_project_file_part(True, "scan.GIF", "image/gif", _GIF_HEADER, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERgif" not in refused["message"]
    assert metadata == []
    assert cache == []
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.GIF", "image/gif", _GIF_HEADER, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    assert metadata == []
    assert cache == []
    assert list(denied.iterdir()) == []


def test_gif_header_with_ocr_continues_without_invented_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "scan.GIF", "image/gif", _GIF_HEADER, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "MARKERgif" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_BIN_IMAGE_TYPE = " Image/GIF ; charset=binary"


def test_bin_image_content_type_ocr_off_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    refused = admit_project_file_part(True, "scan.BIN", _BIN_IMAGE_TYPE, _GIF_HEADER, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERgif" not in refused["message"]
    assert metadata == []
    assert cache == []
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.BIN", _BIN_IMAGE_TYPE, _GIF_HEADER, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    assert metadata == []
    assert cache == []
    assert list(denied.iterdir()) == []


def test_bin_image_content_type_with_ocr_continues_without_invented_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "scan.BIN", _BIN_IMAGE_TYPE, _GIF_HEADER, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "MARKERgif" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_GIF87_HEADER = b"GIF87aMARKERgif87"


def test_gif87_ocr_off_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    refused = admit_project_file_part(True, "scan.GIF", "image/gif", _GIF87_HEADER, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERgif87" not in refused["message"]
    assert metadata == []
    assert cache == []
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.GIF", "image/gif", _GIF87_HEADER, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif87" not in part["message"]
    assert metadata == []
    assert cache == []
    assert list(denied.iterdir()) == []


def test_gif87_header_with_ocr_continues_without_invented_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "scan.GIF", "image/gif", _GIF87_HEADER, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "MARKERgif87" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_BIN_TEXT_TYPE = "text/plain; charset=utf-8"


def test_legal_bin_text_type_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"Hello BIN\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.BIN", _BIN_TEXT_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "bin"
    assert "Hello BIN" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_TEXT_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello BIN" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_bin_text_type_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.BIN", _BIN_TEXT_TYPE, payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_OCTET_TYPE = "application/octet-stream"


def test_legal_bin_octet_sniff_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"Hello SNIFF\n"
    assert b"\0" not in payload
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.BIN", _OCTET_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "bin"
    assert "Hello SNIFF" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _OCTET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello SNIFF" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_bin_octet_sniff_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert b"\0" not in payload
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.BIN", _OCTET_TYPE, payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_bin_nul_octet_continues_and_text_prefix_still_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    prefixed = b"Hello\x00PLAIN\n"
    expected = normalize_extracted_text(decode_text_file(prefixed))
    assert "\x00" not in expected
    assert "Hello" in expected
    assert "PLAIN" in expected
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    decision = admit_project_file_part(True, "note.BIN", "text/plain", prefixed, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "bin"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    binary = b"Hello\x00SNIFF"
    continued = admit_project_file_part(True, "note.BIN", _OCTET_TYPE, binary, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "SNIFF" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        part = admit_project_file_part(False, "note.BIN", _OCTET_TYPE, binary, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "SNIFF" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "project.json"]


def test_bin_nul_at_2047_continues_without_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    inside = (b"A" * 2047) + b"\x00INSIDE"
    assert inside[2047] == 0
    assert b"\x00" not in inside[:2047]
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    continued = admit_project_file_part(True, "note.BIN", _OCTET_TYPE, inside, True)
    metadata: list[str] = []
    cache: list[str] = []
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "INSIDE" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _OCTET_TYPE, inside, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "INSIDE" not in part["message"]
    assert metadata == []
    assert cache == []
    assert list(denied.iterdir()) == []


def test_bin_nul_at_2048_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    outside = (b"B" * 2048) + b"\x00OUTSIDE"
    assert outside[2048] == 0
    assert b"\x00" not in outside[:2048]
    expected = normalize_extracted_text(decode_text_file(outside))
    assert "\x00" not in expected
    assert ("B" * 2048) in expected
    assert "OUTSIDE" in expected
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.BIN", _OCTET_TYPE, outside, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "bin"
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _OCTET_TYPE, outside, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "OUTSIDE" not in part["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_legal_dotless_name_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"Hello TEXT\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "Notes", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "text"
    assert "Hello TEXT" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "Notes", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello TEXT" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_dotless_name_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank", "text/plain", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_leading_dot_env_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"NOTE=Hello DOTENV\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, ".ENV", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "text"
    assert decision["kind"] != "env"
    assert "NOTE=Hello DOTENV" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, ".ENV", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello DOTENV" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_leading_dot_env_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, ".env", "text/plain", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_leading_dot_env_txt_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    assert Path(".env.TXT".lower()).suffix == ".txt"
    payload = b"Hello TXTSUFFIX\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, ".env.TXT", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "txt"
    assert decision["kind"] != "env"
    assert decision["kind"] != "text"
    assert "Hello TXTSUFFIX" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, ".env.TXT", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "TXTSUFFIX" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_leading_dot_env_txt_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, ".env.txt", "text/plain", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


def test_legal_trailing_dot_name_attaches_only_for_the_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    assert Path("Notes.".lower()).suffix == ""
    payload = b"Hello TRAIL\n"
    expected = normalize_extracted_text(decode_text_file(payload))
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "Notes.", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "text"
    assert decision["kind"] != "."
    assert "Hello TRAIL" in expected
    assert metadata == [expected]
    assert cache == [expected]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "Notes.", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello TRAIL" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert list(denied.iterdir()) == []


def test_blank_trailing_dot_name_refuses_before_metadata_or_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    metadata: list[str] = []
    cache: list[str] = []
    payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    refused = admit_project_file_part(True, "blank.", "text/plain", payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    assert metadata == []
    assert cache == []
    assert list(tmp_path.iterdir()) == []


_SPACED_TEXT_TYPE = " text/plain"


def test_spaced_text_type_with_nul_continues_and_exact_type_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"Hello\x00SPACE"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "\x00" not in expected
    assert "Hello" in expected
    assert "SPACE" in expected
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.BIN", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "bin"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    continued = admit_project_file_part(True, "note.BIN", _SPACED_TEXT_TYPE, payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "SPACE" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "SPACE" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _SPACED_TEXT_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "SPACE" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


_UPPER_TEXT_TYPE = "TEXT/PLAIN"


def test_upper_text_type_with_nul_continues_and_exact_type_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"Hello\x00CASE"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "\x00" not in expected
    assert "Hello" in expected
    assert "CASE" in expected
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.BIN", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "bin"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    continued = admit_project_file_part(True, "note.BIN", _UPPER_TEXT_TYPE, payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "CASE" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "CASE" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _UPPER_TEXT_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "CASE" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


_BIN_HTML_TYPE = "text/html"


def test_bin_html_content_type_decodes_without_html_extractor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<p>Hello HTMLBIN</p>"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "<p>" in expected
    assert "HTMLBIN" in expected
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.BIN", _BIN_HTML_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "bin"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    blank_payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(blank_payload)) == ""
    refused = admit_project_file_part(True, "note.BIN", _BIN_HTML_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "HTMLBIN" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "HTMLBIN" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_xhtml_name_decodes_without_html_extractor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<p>Hello XHTML</p>"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "<p>" in expected
    assert "XHTML" in expected
    assert b"\x00" not in payload
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "page.XHTML", "application/octet-stream", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "xhtml"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    blank_payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(blank_payload)) == ""
    refused = admit_project_file_part(True, "blank.xhtml", "application/octet-stream", blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.XHTML", "application/octet-stream", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "XHTML" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.XHTML", "application/octet-stream", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "XHTML" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_xhtml_html_content_type_still_decodes_without_extractor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<p>Hello XHTMLTYPE</p>"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "<p>" in expected
    assert "XHTMLTYPE" in expected
    assert b"\x00" not in payload
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "page.XHTML", "text/html", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "xhtml"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    blank_payload = b" \n\t "
    assert normalize_extracted_text(decode_text_file(blank_payload)) == ""
    refused = admit_project_file_part(True, "blank.xhtml", "text/html", blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.XHTML", "text/html", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "XHTMLTYPE" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.XHTML", "text/html", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "XHTMLTYPE" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_xhtml_text_prefix_with_nul_attaches_and_octet_nul_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<p>Hello\x00XHTMLNUL</p>"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "\x00" not in expected
    assert "<p>" in expected
    assert "Hello" in expected
    assert "XHTMLNUL" in expected
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "page.XHTML", "text/html", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "xhtml"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    continued = admit_project_file_part(True, "page.XHTML", "application/octet-stream", payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "XHTMLNUL" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.XHTML", "text/html", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "XHTMLNUL" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.XHTML", "application/octet-stream", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "XHTMLNUL" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


_SPACED_HTML_TYPE = " text/html"


def test_spaced_html_type_on_xhtml_with_nul_continues_and_exact_type_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<p>Hello\x00XHTMLSPACE</p>"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "\x00" not in expected
    assert "<p>" in expected
    assert "Hello" in expected
    assert "XHTMLSPACE" in expected
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "page.XHTML", "text/html", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "xhtml"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    continued = admit_project_file_part(True, "page.XHTML", _SPACED_HTML_TYPE, payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "XHTMLSPACE" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.XHTML", "text/html", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "XHTMLSPACE" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.XHTML", _SPACED_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "XHTMLSPACE" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


_UPPER_HTML_TYPE = "TEXT/HTML"


def test_upper_html_type_on_xhtml_with_nul_continues_and_exact_type_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<p>Hello\x00XHTMLCASE</p>"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "\x00" not in expected
    assert "<p>" in expected
    assert "Hello" in expected
    assert "XHTMLCASE" in expected
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "page.XHTML", "text/html", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "xhtml"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    continued = admit_project_file_part(True, "page.XHTML", _UPPER_HTML_TYPE, payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "XHTMLCASE" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.XHTML", "text/html", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "XHTMLCASE" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.XHTML", _UPPER_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "XHTMLCASE" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


_HTML_CHARSET_TYPE = "text/html; charset=utf-8"
_XHTML_XML_TYPE = "application/xhtml+xml"


def test_html_charset_with_nul_attaches_and_xhtml_xml_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<p>Hello\x00XHTMLPARAM</p>"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "\x00" not in expected
    assert "<p>" in expected
    assert "Hello" in expected
    assert "XHTMLPARAM" in expected
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "page.XHTML", _HTML_CHARSET_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "xhtml"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    continued = admit_project_file_part(True, "page.XHTML", _XHTML_XML_TYPE, payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "XHTMLPARAM" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.XHTML", _HTML_CHARSET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "XHTMLPARAM" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.XHTML", _XHTML_XML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "XHTMLPARAM" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_pdf_extension_continues_before_text_content_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"Hello PDFTYPE"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "PDFTYPE" in expected
    assert b"\x00" not in payload
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.BIN", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "bin"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    continued = admit_project_file_part(True, "notes.PDF", "text/plain", payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "PDFTYPE" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.PDF", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "PDFTYPE" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "PDFTYPE" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_gif_extension_beats_text_plain_and_bin_still_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    real_magic = text_admission._image_magic_ok
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = _GIF_HEADER
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "MARKERgif" in expected
    assert b"GIF89a" in payload
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.BIN", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "bin"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    refused = admit_project_file_part(True, "scan.GIF", "text/plain", payload, False)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERgif" not in refused["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        real_magic,
    )
    continued = admit_project_file_part(True, "scan.GIF", "text/plain", payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "MARKERgif" not in continued["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.GIF", "text/plain", payload, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_corrupt_gif_with_text_plain_does_not_fall_through_to_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b"NOTGIF CORRUPTTEXT"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "CORRUPTTEXT" in expected
    assert not payload.startswith(b"GIF87a")
    assert not payload.startswith(b"GIF89a")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decision = admit_project_file_part(True, "note.BIN", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == expected
    assert decision["kind"] == "bin"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.rag.files.decode_text_file",
        _refuse_text_decoder,
    )
    corrupt = admit_project_file_part(True, "scan.GIF", "text/plain", payload, True)
    _store(corrupt, metadata, cache)
    assert corrupt["attach"] is False
    assert corrupt["metadataWrite"] is False
    assert corrupt["cacheWrite"] is False
    assert corrupt["text"] == ""
    assert corrupt["kind"] == ""
    assert corrupt["code"] == ErrorCode.INVALID_PAYLOAD
    assert corrupt["status"] == 400
    assert corrupt["message"] == "Uploaded image is corrupt."
    assert "CORRUPTTEXT" not in corrupt["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    refused = admit_project_file_part(True, "scan.GIF", "text/plain", payload, False)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "CORRUPTTEXT" not in refused["message"]
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.GIF", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "CORRUPTTEXT" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "CORRUPTTEXT" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_html_extension_extracts_before_text_plain_and_bin_decodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<script>secret</script><p>Hello HTMLPAGE</p>"
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "Hello HTMLPAGE" in html_text
    assert "secret" not in html_text
    assert "<p>" not in html_text
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert html_text != bin_text
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == bin_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    decision = admit_project_file_part(True, "page.HTML", "text/plain", payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "html"
    assert "secret" not in decision["text"]
    record = tmp_path / "project.json"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.html", "text/plain", blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTML", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLPAGE" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLPAGE" not in part["message"]
    assert metadata == [bin_text, html_text]
    assert cache == [bin_text, html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_html_extension_extracts_octet_stream_and_bin_decodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<script>secret</script><p>Hello HTMLOCTET</p>"
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "Hello HTMLOCTET" in html_text
    assert "secret" not in html_text
    assert "<p>" not in html_text
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert html_text != bin_text
    assert b"\x00" not in payload
    assert not _OCTET_TYPE.startswith("text/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", _OCTET_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == bin_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    decision = admit_project_file_part(True, "page.HTML", _OCTET_TYPE, payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "html"
    assert "secret" not in decision["text"]
    assert "<p>" not in decision["text"]
    record = tmp_path / "project.json"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.html", _OCTET_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTML", _OCTET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLOCTET" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _OCTET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLOCTET" not in part["message"]
    assert metadata == [bin_text, html_text]
    assert cache == [bin_text, html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_html_extension_ignores_nul_and_bin_octet_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"<script>secret</script><p>Hello HTMLNUL</p>\x00"
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "Hello HTMLNUL" in html_text
    assert "secret" not in html_text
    assert "<p>" not in html_text
    assert "\x00" not in html_text
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert html_text != bin_text
    assert payload.index(b"\x00") < 2048
    assert not _OCTET_TYPE.startswith("text/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    continued = admit_project_file_part(True, "note.BIN", _OCTET_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["text"] != bin_text
    assert "Hello HTMLNUL" not in continued["message"]
    assert "secret" not in continued["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    decision = admit_project_file_part(True, "page.HTML", _OCTET_TYPE, payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "html"
    assert "secret" not in decision["text"]
    assert "\x00" not in decision["text"]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    assert stored.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert b"\x00" not in blank_payload
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.html", _OCTET_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTML", _OCTET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLNUL" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _OCTET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLNUL" not in part["message"]
    assert metadata == [html_text]
    assert cache == [html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_image_content_type_beats_html_extension_and_bin_still_decodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_magic = text_admission._image_magic_ok
    real_decoder = text_admission.decode_text_file
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<script>secret</script><p>Hello HTMLIMAGE</p>"
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert "Hello HTMLIMAGE" in bin_text
    assert b"\x00" not in payload
    assert not payload.startswith(b"GIF87a")
    assert not payload.startswith(b"GIF89a")
    assert not _BIN_IMAGE_TYPE.startswith("image/")
    assert not _OCTET_TYPE.startswith("text/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    refused = admit_project_file_part(True, "page.HTML", _BIN_IMAGE_TYPE, payload, False)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "Hello HTMLIMAGE" not in refused["message"]
    assert "secret" not in refused["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        real_magic,
    )
    corrupt = admit_project_file_part(True, "page.HTML", _BIN_IMAGE_TYPE, payload, True)
    _store(corrupt, metadata, cache)
    assert corrupt["attach"] is False
    assert corrupt["metadataWrite"] is False
    assert corrupt["cacheWrite"] is False
    assert corrupt["text"] == ""
    assert corrupt["kind"] == ""
    assert corrupt["code"] == ErrorCode.INVALID_PAYLOAD
    assert corrupt["status"] == 400
    assert corrupt["message"] == "Uploaded image is corrupt."
    assert "Hello HTMLIMAGE" not in corrupt["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        real_decoder,
    )
    decoded = admit_project_file_part(True, "note.BIN", _OCTET_TYPE, payload, False)
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        record.write_text(decoded["text"], encoding="utf-8")
        stored.write_text(decoded["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == bin_text
    assert stored.read_text(encoding="utf-8") == bin_text
    blank = admit_project_file_part(True, "blank.html", _BIN_IMAGE_TYPE, b" \n\t ", False)
    _store(blank, metadata, cache)
    assert blank["attach"] is False
    assert blank["metadataWrite"] is False
    assert blank["cacheWrite"] is False
    assert blank["text"] == ""
    assert blank["code"] == ErrorCode.OCR_REQUIRED
    assert blank["status"] == 415
    assert blank["message"] == _OCR_REQUIRED_MESSAGE
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTML", _BIN_IMAGE_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLIMAGE" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _OCTET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLIMAGE" not in part["message"]
    assert metadata == [bin_text]
    assert cache == [bin_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_recognized_gif_on_html_name_continues_and_bin_decodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_magic = text_admission._image_magic_ok
    real_decoder = text_admission.decode_text_file
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = _GIF_HEADER
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert payload.startswith(b"GIF89a")
    assert b"\x00" not in payload
    assert "MARKERgif" in bin_text
    assert not _BIN_IMAGE_TYPE.startswith("image/")
    assert not _OCTET_TYPE.startswith("text/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    refused = admit_project_file_part(True, "page.HTML", _BIN_IMAGE_TYPE, payload, False)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERgif" not in refused["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        real_magic,
    )
    continued = admit_project_file_part(True, "page.HTML", _BIN_IMAGE_TYPE, payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != bin_text
    assert "MARKERgif" not in continued["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        real_decoder,
    )
    decoded = admit_project_file_part(True, "note.BIN", _OCTET_TYPE, payload, False)
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    assert "MARKERgif" in decoded["text"]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        record.write_text(decoded["text"], encoding="utf-8")
        stored.write_text(decoded["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == bin_text
    assert stored.read_text(encoding="utf-8") == bin_text
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTML", _BIN_IMAGE_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _OCTET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    assert metadata == [bin_text]
    assert cache == [bin_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_html_name_with_text_plain_extracts_gif_header_without_image_magic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = _GIF_HEADER
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert payload.startswith(b"GIF89a")
    assert b"\x00" not in payload
    assert "MARKERgif" in html_text
    assert "MARKERgif" in bin_text
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == bin_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    decision = admit_project_file_part(True, "page.HTML", "text/plain", payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "html"
    assert "MARKERgif" in decision["text"]
    record = tmp_path / "project.json"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.html", "text/plain", blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTML", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    assert metadata == [bin_text, html_text]
    assert cache == [bin_text, html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_htm_name_with_text_plain_extracts_gif_header_without_image_magic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = _GIF_HEADER
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert payload.startswith(b"GIF89a")
    assert b"\x00" not in payload
    assert "MARKERgif" in html_text
    assert "MARKERgif" in bin_text
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == bin_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    decision = admit_project_file_part(True, "page.HTM", "text/plain", payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "htm"
    assert decision["kind"] != "html"
    assert "MARKERgif" in decision["text"]
    record = tmp_path / "project.json"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.HTM", "text/plain", blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTM", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    assert metadata == [bin_text, html_text]
    assert cache == [bin_text, html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_image_content_type_beats_htm_extension_and_bin_decodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_magic = text_admission._image_magic_ok
    real_decoder = text_admission.decode_text_file
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = _GIF_HEADER
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert payload.startswith(b"GIF89a")
    assert b"\x00" not in payload
    assert "MARKERgif" in bin_text
    assert not _BIN_IMAGE_TYPE.startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    refused = admit_project_file_part(True, "page.HTM", _BIN_IMAGE_TYPE, payload, False)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERgif" not in refused["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        real_magic,
    )
    continued = admit_project_file_part(True, "page.HTM", _BIN_IMAGE_TYPE, payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != bin_text
    assert "MARKERgif" not in continued["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        real_decoder,
    )
    decoded = admit_project_file_part(True, "note.BIN", "text/plain", payload, False)
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    assert "MARKERgif" in decoded["text"]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        record.write_text(decoded["text"], encoding="utf-8")
        stored.write_text(decoded["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == bin_text
    assert stored.read_text(encoding="utf-8") == bin_text
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTM", _BIN_IMAGE_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    assert metadata == [bin_text]
    assert cache == [bin_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_corrupt_htm_image_type_does_not_fall_through_to_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_magic = text_admission._image_magic_ok
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<script>secret</script><p>Hello HTMCORRUPT</p>"
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert "Hello HTMCORRUPT" in bin_text
    assert b"\x00" not in payload
    assert not payload.startswith(b"GIF87a")
    assert not payload.startswith(b"GIF89a")
    assert not _BIN_IMAGE_TYPE.startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", "text/plain", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        record.write_text(decoded["text"], encoding="utf-8")
        stored.write_text(decoded["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == bin_text
    assert stored.read_text(encoding="utf-8") == bin_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        real_magic,
    )
    corrupt = admit_project_file_part(True, "page.HTM", _BIN_IMAGE_TYPE, payload, True)
    _store(corrupt, metadata, cache)
    assert corrupt["attach"] is False
    assert corrupt["metadataWrite"] is False
    assert corrupt["cacheWrite"] is False
    assert corrupt["text"] == ""
    assert corrupt["kind"] == ""
    assert corrupt["code"] == ErrorCode.INVALID_PAYLOAD
    assert corrupt["status"] == 400
    assert corrupt["message"] == "Uploaded image is corrupt."
    assert "Hello HTMCORRUPT" not in corrupt["message"]
    assert "secret" not in corrupt["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    blank = admit_project_file_part(True, "blank.HTM", _BIN_IMAGE_TYPE, b" \n\t ", False)
    _store(blank, metadata, cache)
    assert blank["attach"] is False
    assert blank["metadataWrite"] is False
    assert blank["cacheWrite"] is False
    assert blank["text"] == ""
    assert blank["kind"] == ""
    assert blank["code"] == ErrorCode.OCR_REQUIRED
    assert blank["status"] == 415
    assert blank["message"] == _OCR_REQUIRED_MESSAGE
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTM", _BIN_IMAGE_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMCORRUPT" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMCORRUPT" not in part["message"]
    assert metadata == [bin_text]
    assert cache == [bin_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_htm_text_html_extracts_gif_header_and_bin_decodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = _GIF_HEADER
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert payload.startswith(b"GIF89a")
    assert b"\x00" not in payload
    assert "MARKERgif" in html_text
    assert "MARKERgif" in bin_text
    assert _BIN_HTML_TYPE == "text/html"
    assert _BIN_HTML_TYPE.startswith("text/")
    assert not _BIN_HTML_TYPE.startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", _BIN_HTML_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    assert "MARKERgif" in decoded["text"]
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == bin_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    decision = admit_project_file_part(True, "page.HTM", _BIN_HTML_TYPE, payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "htm"
    assert decision["kind"] != "html"
    assert "MARKERgif" in decision["text"]
    record = tmp_path / "project.json"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.HTM", _BIN_HTML_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTM", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    assert metadata == [bin_text, html_text]
    assert cache == [bin_text, html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_html_text_html_extracts_gif_header_and_bin_decodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = _GIF_HEADER
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert payload.startswith(b"GIF89a")
    assert b"\x00" not in payload
    assert "MARKERgif" in html_text
    assert "MARKERgif" in bin_text
    assert _BIN_HTML_TYPE == "text/html"
    assert _BIN_HTML_TYPE.startswith("text/")
    assert not _BIN_HTML_TYPE.startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", _BIN_HTML_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    assert "MARKERgif" in decoded["text"]
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == bin_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    decision = admit_project_file_part(True, "page.HTML", _BIN_HTML_TYPE, payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "html"
    assert "MARKERgif" in decision["text"]
    record = tmp_path / "project.json"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.html", _BIN_HTML_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTML", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    assert metadata == [bin_text, html_text]
    assert cache == [bin_text, html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_html_upper_type_with_nul_extracts_and_bin_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"<script>secret</script><p>Hello HTMLCASE</p>\x00"
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "Hello HTMLCASE" in html_text
    assert "secret" not in html_text
    assert "<p>" not in html_text
    assert "\x00" not in html_text
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert "Hello HTMLCASE" in bin_text
    assert html_text != bin_text
    assert payload.index(b"\x00") < 2048
    assert _UPPER_HTML_TYPE == "TEXT/HTML"
    assert not _UPPER_HTML_TYPE.startswith("text/")
    assert not _UPPER_HTML_TYPE.startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    continued = admit_project_file_part(True, "note.BIN", _UPPER_HTML_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != bin_text
    assert "Hello HTMLCASE" not in continued["message"]
    assert "secret" not in continued["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    decision = admit_project_file_part(True, "page.HTML", _UPPER_HTML_TYPE, payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "html"
    assert "secret" not in decision["text"]
    assert "\x00" not in decision["text"]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    assert stored.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert b"\x00" not in blank_payload
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.html", _UPPER_HTML_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTML", _UPPER_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLCASE" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _UPPER_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLCASE" not in part["message"]
    assert metadata == [html_text]
    assert cache == [html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_html_spaced_type_with_nul_extracts_and_bin_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"<script>secret</script><p>Hello HTMLSPACE</p>\x00"
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "Hello HTMLSPACE" in html_text
    assert "secret" not in html_text
    assert "<p>" not in html_text
    assert "\x00" not in html_text
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert "Hello HTMLSPACE" in bin_text
    assert html_text != bin_text
    assert payload.index(b"\x00") < 2048
    assert _SPACED_HTML_TYPE == " text/html"
    assert _SPACED_HTML_TYPE.lstrip() == "text/html"
    assert not _SPACED_HTML_TYPE.startswith("text/")
    assert not _SPACED_HTML_TYPE.startswith("image/")
    assert not _SPACED_HTML_TYPE.split(";", 1)[0].strip().lower().startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    continued = admit_project_file_part(True, "note.BIN", _SPACED_HTML_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != bin_text
    assert "Hello HTMLSPACE" not in continued["message"]
    assert "secret" not in continued["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    decision = admit_project_file_part(True, "page.HTML", _SPACED_HTML_TYPE, payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "html"
    assert "secret" not in decision["text"]
    assert "\x00" not in decision["text"]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    assert stored.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert b"\x00" not in blank_payload
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.html", _SPACED_HTML_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTML", _SPACED_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLSPACE" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _SPACED_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLSPACE" not in part["message"]
    assert metadata == [html_text]
    assert cache == [html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_html_charset_with_nul_extracts_and_bin_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<script>secret</script><p>Hello HTMLPARAM</p>\x00"
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "Hello HTMLPARAM" in html_text
    assert "secret" not in html_text
    assert "<p>" not in html_text
    assert "\x00" not in html_text
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert "Hello HTMLPARAM" in bin_text
    assert html_text != bin_text
    assert payload.index(b"\x00") < 2048
    assert _HTML_CHARSET_TYPE == "text/html; charset=utf-8"
    assert _HTML_CHARSET_TYPE.startswith("text/")
    assert ";" in _HTML_CHARSET_TYPE
    assert not _HTML_CHARSET_TYPE.startswith("image/")
    assert not _HTML_CHARSET_TYPE.split(";", 1)[0].strip().lower().startswith("image/")
    assert _XHTML_XML_TYPE == "application/xhtml+xml"
    assert not _XHTML_XML_TYPE.startswith("text/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", _HTML_CHARSET_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    assert "secret" in decoded["text"]
    assert "<p>" in decoded["text"]
    assert "\x00" not in decoded["text"]
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == bin_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    decision = admit_project_file_part(True, "page.HTML", _HTML_CHARSET_TYPE, payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "html"
    assert "secret" not in decision["text"]
    assert "\x00" not in decision["text"]
    record = tmp_path / "project.json"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    continued = admit_project_file_part(True, "note.BIN", _XHTML_XML_TYPE, payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "Hello HTMLPARAM" not in continued["message"]
    assert "secret" not in continued["message"]
    blank_payload = b" \n\t "
    assert b"\x00" not in blank_payload
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.html", _HTML_CHARSET_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTML", _HTML_CHARSET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLPARAM" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _HTML_CHARSET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLPARAM" not in part["message"]
    assert metadata == [bin_text, html_text]
    assert cache == [bin_text, html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_htm_upper_type_with_nul_extracts_and_bin_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"<script>secret</script><p>Hello HTMCASE</p>\x00"
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "Hello HTMCASE" in html_text
    assert "secret" not in html_text
    assert "<p>" not in html_text
    assert "\x00" not in html_text
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert "Hello HTMCASE" in bin_text
    assert html_text != bin_text
    assert payload.index(b"\x00") < 2048
    assert _UPPER_HTML_TYPE == "TEXT/HTML"
    assert not _UPPER_HTML_TYPE.startswith("text/")
    assert not _UPPER_HTML_TYPE.startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    continued = admit_project_file_part(True, "note.BIN", _UPPER_HTML_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != bin_text
    assert "Hello HTMCASE" not in continued["message"]
    assert "secret" not in continued["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    decision = admit_project_file_part(True, "page.HTM", _UPPER_HTML_TYPE, payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "htm"
    assert decision["kind"] != "html"
    assert "secret" not in decision["text"]
    assert "\x00" not in decision["text"]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    assert stored.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert b"\x00" not in blank_payload
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.HTM", _UPPER_HTML_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTM", _UPPER_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMCASE" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _UPPER_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMCASE" not in part["message"]
    assert metadata == [html_text]
    assert cache == [html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_htm_spaced_type_with_nul_extracts_and_bin_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"<script>secret</script><p>Hello HTMSPACE</p>\x00"
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "Hello HTMSPACE" in html_text
    assert "secret" not in html_text
    assert "<p>" not in html_text
    assert "\x00" not in html_text
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert "Hello HTMSPACE" in bin_text
    assert html_text != bin_text
    assert payload.index(b"\x00") < 2048
    assert _SPACED_HTML_TYPE == " text/html"
    assert _SPACED_HTML_TYPE.lstrip() == "text/html"
    assert not _SPACED_HTML_TYPE.startswith("text/")
    assert not _SPACED_HTML_TYPE.startswith("image/")
    assert not _SPACED_HTML_TYPE.split(";", 1)[0].strip().lower().startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    continued = admit_project_file_part(True, "note.BIN", _SPACED_HTML_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != bin_text
    assert "Hello HTMSPACE" not in continued["message"]
    assert "secret" not in continued["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    decision = admit_project_file_part(True, "page.HTM", _SPACED_HTML_TYPE, payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "htm"
    assert decision["kind"] != "html"
    assert "secret" not in decision["text"]
    assert "\x00" not in decision["text"]
    record = tmp_path / "project.json"
    stored = tmp_path / "cache.txt"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
        stored.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    assert stored.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert b"\x00" not in blank_payload
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.HTM", _SPACED_HTML_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTM", _SPACED_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMSPACE" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _SPACED_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMSPACE" not in part["message"]
    assert metadata == [html_text]
    assert cache == [html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_htm_charset_with_nul_extracts_and_bin_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<script>secret</script><p>Hello HTMPARAM</p>\x00"
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "Hello HTMPARAM" in html_text
    assert "secret" not in html_text
    assert "<p>" not in html_text
    assert "\x00" not in html_text
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert "Hello HTMPARAM" in bin_text
    assert html_text != bin_text
    assert payload.index(b"\x00") < 2048
    assert _HTML_CHARSET_TYPE == "text/html; charset=utf-8"
    assert _HTML_CHARSET_TYPE.startswith("text/")
    assert ";" in _HTML_CHARSET_TYPE
    assert not _HTML_CHARSET_TYPE.startswith("image/")
    assert not _HTML_CHARSET_TYPE.split(";", 1)[0].strip().lower().startswith("image/")
    assert _XHTML_XML_TYPE == "application/xhtml+xml"
    assert not _XHTML_XML_TYPE.startswith("text/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", _HTML_CHARSET_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    assert "secret" in decoded["text"]
    assert "<p>" in decoded["text"]
    assert "\x00" not in decoded["text"]
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == bin_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    decision = admit_project_file_part(True, "page.HTM", _HTML_CHARSET_TYPE, payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "htm"
    assert decision["kind"] != "html"
    assert "secret" not in decision["text"]
    assert "\x00" not in decision["text"]
    record = tmp_path / "project.json"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    continued = admit_project_file_part(True, "note.BIN", _XHTML_XML_TYPE, payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "Hello HTMPARAM" not in continued["message"]
    assert "secret" not in continued["message"]
    blank_payload = b" \n\t "
    assert b"\x00" not in blank_payload
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.HTM", _HTML_CHARSET_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTM", _HTML_CHARSET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMPARAM" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _HTML_CHARSET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMPARAM" not in part["message"]
    assert metadata == [bin_text, html_text]
    assert cache == [bin_text, html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_html_exact_text_html_with_nul_extracts_and_bin_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<script>secret</script><p>Hello HTMLEXACT</p>\x00"
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "Hello HTMLEXACT" in html_text
    assert "secret" not in html_text
    assert "<p>" not in html_text
    assert "\x00" not in html_text
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert "Hello HTMLEXACT" in bin_text
    assert html_text != bin_text
    assert payload.index(b"\x00") < 2048
    assert _BIN_HTML_TYPE == "text/html"
    assert _BIN_HTML_TYPE.startswith("text/")
    assert ";" not in _BIN_HTML_TYPE
    assert not _BIN_HTML_TYPE.startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", _BIN_HTML_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    assert "secret" in decoded["text"]
    assert "<p>" in decoded["text"]
    assert "\x00" not in decoded["text"]
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == bin_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    decision = admit_project_file_part(True, "page.HTML", _BIN_HTML_TYPE, payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "html"
    assert "secret" not in decision["text"]
    assert "\x00" not in decision["text"]
    record = tmp_path / "project.json"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert b"\x00" not in blank_payload
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.html", _BIN_HTML_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTML", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLEXACT" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMLEXACT" not in part["message"]
    assert metadata == [bin_text, html_text]
    assert cache == [bin_text, html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_htm_exact_text_html_with_nul_extracts_and_bin_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    _block_project_writers(monkeypatch)
    real_html = text_admission.extract_html_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"<script>secret</script><p>Hello HTMEXACT</p>\x00"
    html_text = normalize_extracted_text(extract_html_text(payload))
    bin_text = normalize_extracted_text(decode_text_file(payload))
    assert "Hello HTMEXACT" in html_text
    assert "secret" not in html_text
    assert "<p>" not in html_text
    assert "\x00" not in html_text
    assert "<p>" in bin_text
    assert "secret" in bin_text
    assert "Hello HTMEXACT" in bin_text
    assert html_text != bin_text
    assert payload.index(b"\x00") < 2048
    assert _BIN_HTML_TYPE == "text/html"
    assert _BIN_HTML_TYPE.startswith("text/")
    assert ";" not in _BIN_HTML_TYPE
    assert not _BIN_HTML_TYPE.startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", _BIN_HTML_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == bin_text
    assert decoded["kind"] == "bin"
    assert "secret" in decoded["text"]
    assert "<p>" in decoded["text"]
    assert "\x00" not in decoded["text"]
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == bin_text
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        real_html,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    decision = admit_project_file_part(True, "page.HTM", _BIN_HTML_TYPE, payload, False)
    _store(decision, metadata, cache)
    assert decision["attach"] is True
    assert decision["metadataWrite"] is True
    assert decision["cacheWrite"] is True
    assert decision["text"] == html_text
    assert decision["kind"] == "htm"
    assert decision["kind"] != "html"
    assert "secret" not in decision["text"]
    assert "\x00" not in decision["text"]
    record = tmp_path / "project.json"
    if decision["metadataWrite"] is True and decision["cacheWrite"] is True:
        record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == html_text
    blank_payload = b" \n\t "
    assert b"\x00" not in blank_payload
    assert extract_html_text(blank_payload) == ""
    refused = admit_project_file_part(True, "blank.HTM", _BIN_HTML_TYPE, blank_payload, True)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "page.HTM", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMEXACT" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello HTMEXACT" not in part["message"]
    assert metadata == [bin_text, html_text]
    assert cache == [bin_text, html_text]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_gif_extension_beats_exact_text_html_and_bin_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    real_magic = text_admission._image_magic_ok
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = _GIF_HEADER + b"\x00"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert payload.startswith(b"GIF89a")
    assert "MARKERgif" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert _BIN_HTML_TYPE == "text/html"
    assert _BIN_HTML_TYPE.startswith("text/")
    assert not _BIN_HTML_TYPE.startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", _BIN_HTML_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == expected
    assert decoded["kind"] == "bin"
    assert "MARKERgif" in decoded["text"]
    assert "\x00" not in decoded["text"]
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    refused = admit_project_file_part(True, "scan.GIF", _BIN_HTML_TYPE, payload, False)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERgif" not in refused["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        real_magic,
    )
    continued = admit_project_file_part(True, "scan.GIF", _BIN_HTML_TYPE, payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "MARKERgif" not in continued["message"]
    record = tmp_path / "project.json"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        record.write_text(decoded["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.GIF", _BIN_HTML_TYPE, payload, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_corrupt_gif_with_exact_text_html_does_not_fall_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    payload = b"<script>secret</script><p>Hello CORRUPTHTML</p>"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "CORRUPTHTML" in expected
    assert "secret" in expected
    assert "<p>" in expected
    assert not payload.startswith(b"GIF87a")
    assert not payload.startswith(b"GIF89a")
    assert b"\x00" not in payload
    assert _BIN_HTML_TYPE == "text/html"
    assert _BIN_HTML_TYPE.startswith("text/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", _BIN_HTML_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == expected
    assert decoded["kind"] == "bin"
    assert "secret" in decoded["text"]
    stored = tmp_path / "cache.txt"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    corrupt = admit_project_file_part(True, "scan.GIF", _BIN_HTML_TYPE, payload, True)
    _store(corrupt, metadata, cache)
    assert corrupt["attach"] is False
    assert corrupt["metadataWrite"] is False
    assert corrupt["cacheWrite"] is False
    assert corrupt["text"] == ""
    assert corrupt["kind"] == ""
    assert corrupt["code"] == ErrorCode.INVALID_PAYLOAD
    assert corrupt["status"] == 400
    assert corrupt["message"] == "Uploaded image is corrupt."
    assert "CORRUPTHTML" not in corrupt["message"]
    assert "secret" not in corrupt["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    refused = admit_project_file_part(True, "scan.GIF", _BIN_HTML_TYPE, payload, False)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "CORRUPTHTML" not in refused["message"]
    record = tmp_path / "project.json"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        record.write_text(decoded["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected
    pdf = admit_project_file_part(True, "notes.pdf", "application/pdf", b"%PDF-1.4\n", False)
    _store(pdf, metadata, cache)
    assert pdf["attach"] is True
    assert pdf["metadataWrite"] is False
    assert pdf["cacheWrite"] is False
    assert pdf["text"] == ""
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "scan.GIF", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "CORRUPTHTML" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "CORRUPTHTML" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_pdf_continues_before_exact_text_html_with_nul_and_bin_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"Hello PDFHTML\x00"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "PDFHTML" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert _BIN_HTML_TYPE == "text/html"
    assert _BIN_HTML_TYPE.startswith("text/")
    assert not _BIN_HTML_TYPE.startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", _BIN_HTML_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == expected
    assert decoded["kind"] == "bin"
    assert "PDFHTML" in decoded["text"]
    assert "\x00" not in decoded["text"]
    stored = tmp_path / "cache.txt"
    record = tmp_path / "project.json"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
        record.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == expected
    assert record.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    continued = admit_project_file_part(True, "notes.PDF", _BIN_HTML_TYPE, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "PDFHTML" not in continued["message"]
    lower = admit_project_file_part(True, "notes.pdf", _BIN_HTML_TYPE, payload, False)
    _store(lower, metadata, cache)
    assert lower["attach"] is True
    assert lower["metadataWrite"] is False
    assert lower["cacheWrite"] is False
    assert lower["text"] == ""
    assert lower["kind"] == ""
    assert lower["message"] == ""
    assert "PDFHTML" not in lower["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.PDF", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "PDFHTML" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "PDFHTML" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_pdf_and_bin_continue_for_upper_html_type_with_nul(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"Hello PDFCASE\x00"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "PDFCASE" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert _UPPER_HTML_TYPE == "TEXT/HTML"
    assert not _UPPER_HTML_TYPE.startswith("text/")
    assert not _UPPER_HTML_TYPE.startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "notes.PDF", _UPPER_HTML_TYPE, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "PDFCASE" not in continued["message"]
    lower = admit_project_file_part(True, "notes.pdf", _UPPER_HTML_TYPE, payload, False)
    _store(lower, metadata, cache)
    assert lower["attach"] is True
    assert lower["metadataWrite"] is False
    assert lower["cacheWrite"] is False
    assert lower["text"] == ""
    assert lower["kind"] == ""
    assert lower["message"] == ""
    assert "PDFCASE" not in lower["message"]
    skipped = admit_project_file_part(True, "note.BIN", _UPPER_HTML_TYPE, payload, False)
    _store(skipped, metadata, cache)
    assert skipped["attach"] is True
    assert skipped["metadataWrite"] is False
    assert skipped["cacheWrite"] is False
    assert skipped["text"] == ""
    assert skipped["kind"] == ""
    assert skipped["message"] == ""
    assert skipped["text"] != expected
    assert "PDFCASE" not in skipped["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.PDF", _UPPER_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "PDFCASE" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _UPPER_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "PDFCASE" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_pdf_and_bin_continue_for_spaced_html_type_with_nul(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"Hello PDFSPACE\x00"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "PDFSPACE" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert _SPACED_HTML_TYPE == " text/html"
    assert _SPACED_HTML_TYPE.lstrip() == "text/html"
    assert not _SPACED_HTML_TYPE.startswith("text/")
    assert not _SPACED_HTML_TYPE.startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "notes.PDF", _SPACED_HTML_TYPE, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "PDFSPACE" not in continued["message"]
    lower = admit_project_file_part(True, "notes.pdf", _SPACED_HTML_TYPE, payload, False)
    _store(lower, metadata, cache)
    assert lower["attach"] is True
    assert lower["metadataWrite"] is False
    assert lower["cacheWrite"] is False
    assert lower["text"] == ""
    assert lower["kind"] == ""
    assert lower["message"] == ""
    assert "PDFSPACE" not in lower["message"]
    skipped = admit_project_file_part(True, "note.BIN", _SPACED_HTML_TYPE, payload, False)
    _store(skipped, metadata, cache)
    assert skipped["attach"] is True
    assert skipped["metadataWrite"] is False
    assert skipped["cacheWrite"] is False
    assert skipped["text"] == ""
    assert skipped["kind"] == ""
    assert skipped["message"] == ""
    assert skipped["text"] != expected
    assert "PDFSPACE" not in skipped["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.PDF", _SPACED_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "PDFSPACE" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _SPACED_HTML_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "PDFSPACE" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_pdf_continues_before_html_charset_with_nul_and_bin_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    payload = b"Hello PDFPARAM\x00"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "PDFPARAM" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert _HTML_CHARSET_TYPE == "text/html; charset=utf-8"
    assert _HTML_CHARSET_TYPE.startswith("text/")
    assert ";" in _HTML_CHARSET_TYPE
    assert _HTML_CHARSET_TYPE.split(";", 1)[0] == "text/html"
    assert not _HTML_CHARSET_TYPE.startswith("image/")
    assert _XHTML_XML_TYPE == "application/xhtml+xml"
    assert not _XHTML_XML_TYPE.startswith("text/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "note.BIN", _HTML_CHARSET_TYPE, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == expected
    assert decoded["kind"] == "bin"
    assert "PDFPARAM" in decoded["text"]
    assert "\x00" not in decoded["text"]
    stored = tmp_path / "cache.txt"
    record = tmp_path / "project.json"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
        record.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == expected
    assert record.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    continued = admit_project_file_part(True, "notes.PDF", _HTML_CHARSET_TYPE, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "PDFPARAM" not in continued["message"]
    lower = admit_project_file_part(True, "notes.pdf", _HTML_CHARSET_TYPE, payload, False)
    _store(lower, metadata, cache)
    assert lower["attach"] is True
    assert lower["metadataWrite"] is False
    assert lower["cacheWrite"] is False
    assert lower["text"] == ""
    assert lower["kind"] == ""
    assert lower["message"] == ""
    assert "PDFPARAM" not in lower["message"]
    skipped = admit_project_file_part(True, "note.BIN", _XHTML_XML_TYPE, payload, True)
    _store(skipped, metadata, cache)
    assert skipped["attach"] is True
    assert skipped["metadataWrite"] is False
    assert skipped["cacheWrite"] is False
    assert skipped["text"] == ""
    assert skipped["kind"] == ""
    assert skipped["message"] == ""
    assert skipped["text"] != expected
    assert "PDFPARAM" not in skipped["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.PDF", _HTML_CHARSET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "PDFPARAM" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _HTML_CHARSET_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "PDFPARAM" not in part["message"]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_pdf_continues_before_image_content_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = _GIF_HEADER + b"\x00"
    assert payload.startswith(b"GIF89a")
    assert b"MARKERgif" in payload
    assert payload.index(b"\x00") < 2048
    assert _BIN_IMAGE_TYPE == " Image/GIF ; charset=binary"
    assert not _BIN_IMAGE_TYPE.startswith("image/")
    assert _BIN_IMAGE_TYPE.split(";", 1)[0].strip().lower().startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "notes.PDF", _BIN_IMAGE_TYPE, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["status"] != 415
    assert "MARKERgif" not in continued["message"]
    assert continued["message"] != _OCR_REQUIRED_MESSAGE
    lower = admit_project_file_part(True, "notes.pdf", _BIN_IMAGE_TYPE, payload, False)
    _store(lower, metadata, cache)
    assert lower["attach"] is True
    assert lower["metadataWrite"] is False
    assert lower["cacheWrite"] is False
    assert lower["text"] == ""
    assert lower["kind"] == ""
    assert lower["message"] == ""
    assert "MARKERgif" not in lower["message"]
    enabled = admit_project_file_part(True, "notes.PDF", _BIN_IMAGE_TYPE, payload, True)
    _store(enabled, metadata, cache)
    assert enabled["attach"] is True
    assert enabled["metadataWrite"] is False
    assert enabled["cacheWrite"] is False
    assert enabled["text"] == ""
    assert enabled["kind"] == ""
    assert enabled["message"] == ""
    assert "MARKERgif" not in enabled["message"]
    refused = admit_project_file_part(True, "note.BIN", _BIN_IMAGE_TYPE, payload, False)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "MARKERgif" not in refused["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.PDF", _BIN_IMAGE_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_IMAGE_TYPE, payload, False)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "MARKERgif" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_pdf_continues_when_image_type_bytes_are_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    real_magic = text_admission._image_magic_ok
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"<script>secret</script><p>Hello PDFCORRUPT</p>"
    assert not payload.startswith(b"GIF87a")
    assert not payload.startswith(b"GIF89a")
    assert b"\x00" not in payload
    assert not real_magic(payload)
    assert _BIN_IMAGE_TYPE == " Image/GIF ; charset=binary"
    assert not _BIN_IMAGE_TYPE.startswith("image/")
    assert _BIN_IMAGE_TYPE.split(";", 1)[0].strip().lower().startswith("image/")
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "notes.PDF", _BIN_IMAGE_TYPE, payload, True)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["message"] != "Uploaded image is corrupt."
    assert "PDFCORRUPT" not in continued["message"]
    assert "secret" not in continued["message"]
    lower = admit_project_file_part(True, "notes.pdf", _BIN_IMAGE_TYPE, payload, True)
    _store(lower, metadata, cache)
    assert lower["attach"] is True
    assert lower["metadataWrite"] is False
    assert lower["cacheWrite"] is False
    assert lower["text"] == ""
    assert lower["kind"] == ""
    assert lower["message"] == ""
    assert "PDFCORRUPT" not in lower["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        real_magic,
    )
    corrupt = admit_project_file_part(True, "note.BIN", _BIN_IMAGE_TYPE, payload, True)
    _store(corrupt, metadata, cache)
    assert corrupt["attach"] is False
    assert corrupt["metadataWrite"] is False
    assert corrupt["cacheWrite"] is False
    assert corrupt["text"] == ""
    assert corrupt["kind"] == ""
    assert corrupt["code"] == ErrorCode.INVALID_PAYLOAD
    assert corrupt["status"] == 400
    assert corrupt["message"] == "Uploaded image is corrupt."
    assert "PDFCORRUPT" not in corrupt["message"]
    assert "secret" not in corrupt["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    refused = admit_project_file_part(True, "note.BIN", _BIN_IMAGE_TYPE, payload, False)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 415
    assert refused["message"] == _OCR_REQUIRED_MESSAGE
    assert "PDFCORRUPT" not in refused["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.PDF", _BIN_IMAGE_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "PDFCORRUPT" not in part["message"]
        assert "secret" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_IMAGE_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "PDFCORRUPT" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_empty_pdf_refuses_before_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    refused = admit_project_file_part(True, "notes.PDF", "application/pdf", payload, False)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 400
    assert refused["message"] == "Uploaded file is empty: notes.PDF"
    lower = admit_project_file_part(True, "notes.pdf", "application/pdf", payload, False)
    _store(lower, metadata, cache)
    assert lower["attach"] is False
    assert lower["metadataWrite"] is False
    assert lower["cacheWrite"] is False
    assert lower["text"] == ""
    assert lower["kind"] == ""
    assert lower["code"] == ErrorCode.INVALID_PAYLOAD
    assert lower["status"] == 400
    assert lower["message"] == "Uploaded file is empty: notes.pdf"
    image_named = admit_project_file_part(True, "notes.PDF", _BIN_IMAGE_TYPE, payload, True)
    _store(image_named, metadata, cache)
    assert image_named["attach"] is False
    assert image_named["metadataWrite"] is False
    assert image_named["cacheWrite"] is False
    assert image_named["text"] == ""
    assert image_named["status"] == 400
    assert image_named["message"] == "Uploaded file is empty: notes.PDF"
    assert image_named["message"] != "Uploaded image is corrupt."
    assert image_named["message"] != _OCR_REQUIRED_MESSAGE
    binary = admit_project_file_part(True, "note.BIN", _BIN_IMAGE_TYPE, payload, True)
    _store(binary, metadata, cache)
    assert binary["attach"] is False
    assert binary["metadataWrite"] is False
    assert binary["cacheWrite"] is False
    assert binary["text"] == ""
    assert binary["code"] == ErrorCode.INVALID_PAYLOAD
    assert binary["status"] == 400
    assert binary["message"] == "Uploaded file is empty: note.BIN"
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.PDF", "application/pdf", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert part["message"] == NOT_OWNED_MESSAGE
        assert "empty" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", _BIN_IMAGE_TYPE, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert part["message"] == NOT_OWNED_MESSAGE
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_whitespace_pdf_continues_while_bin_text_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    real_decoder = text_admission.decode_text_file
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b" \n\t "
    assert payload != b""
    assert b"\x00" not in payload
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "notes.PDF", "text/plain", payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["message"] != "No readable text found in this file"
    lower = admit_project_file_part(True, "notes.pdf", "text/plain", payload, False)
    _store(lower, metadata, cache)
    assert lower["attach"] is True
    assert lower["metadataWrite"] is False
    assert lower["cacheWrite"] is False
    assert lower["text"] == ""
    assert lower["kind"] == ""
    assert lower["message"] == ""
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        real_decoder,
    )
    refused = admit_project_file_part(True, "note.BIN", "text/plain", payload, False)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.PDF", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "readable" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "readable" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_whitespace_pdf_continues_when_content_type_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    real_decoder = text_admission.decode_text_file
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b" \n\t "
    assert payload != b""
    assert b"\x00" not in payload
    assert normalize_extracted_text(decode_text_file(payload)) == ""
    media_line = (ROOT / "deepseek_infra/infra/native_runtime/file_text_admission.py").read_text(encoding="utf-8")
    media_line = media_line.split("media = content_type", 1)[1].split("\n", 1)[0]
    assert media_line == ' if isinstance(content_type, str) else ""'
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "notes.PDF", None, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["message"] != "No readable text found in this file"
    lower = admit_project_file_part(True, "notes.pdf", None, payload, False)
    _store(lower, metadata, cache)
    assert lower["attach"] is True
    assert lower["metadataWrite"] is False
    assert lower["cacheWrite"] is False
    assert lower["text"] == ""
    assert lower["kind"] == ""
    assert lower["message"] == ""
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        real_decoder,
    )
    refused = admit_project_file_part(True, "note.BIN", None, payload, False)
    _store(refused, metadata, cache)
    assert refused["attach"] is False
    assert refused["metadataWrite"] is False
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.INVALID_PAYLOAD
    assert refused["status"] == 422
    assert refused["message"] == "No readable text found in this file"
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.PDF", None, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "readable" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "note.BIN", None, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "readable" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_missing_filename_attaches_text_only_for_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    real_decoder = text_admission.decode_text_file
    seen: list[bytes] = []

    def _count_decoder(data: bytes) -> str:
        seen.append(data)
        return real_decoder(data)

    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _count_decoder,
    )
    payload = b"Hello NONAME"
    assert b"\x00" not in payload
    expected = normalize_extracted_text(decode_text_file(payload))
    assert expected == "Hello NONAME"
    assert "NONAME" in expected
    source = (ROOT / "deepseek_infra/infra/native_runtime/file_text_admission.py").read_text(encoding="utf-8")
    assert 'name = filename if isinstance(filename, str) else ""' in source
    assert Path("").suffix == ""
    text_kind = source.split("if is_text_file(", 1)[1]
    text_kind = text_kind.split('return _part("text"', 1)[0]
    assert 'else "text"' in text_kind
    rust_text = POLICY.read_text(encoding="utf-8")
    rust_kind = rust_text.split("if is_text_file(&extension, content_type, data)", 1)[1]
    rust_kind = rust_kind.split("continue_file()", 1)[0]
    assert "stripped.is_empty()" in rust_kind
    assert '"text".to_string()' in rust_kind
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, None, None, payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == expected
    assert decoded["kind"] == "text"
    assert "NONAME" in decoded["text"]
    assert seen == [payload]
    stored = tmp_path / "cache.txt"
    record = tmp_path / "project.json"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
        record.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == expected
    assert record.read_text(encoding="utf-8") == expected
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, None, None, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert part["kind"] == ""
        assert "NONAME" not in part["message"]
    assert seen == [payload]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, None, None, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "NONAME" not in part["message"]
    assert seen == [payload]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_empty_filename_attaches_text_only_for_native_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    real_decoder = text_admission.decode_text_file
    seen: list[bytes] = []

    def _count_decoder(data: bytes) -> str:
        seen.append(data)
        return real_decoder(data)

    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _count_decoder,
    )
    payload = b"Hello EMPTYNAME"
    assert b"\x00" not in payload
    expected = normalize_extracted_text(decode_text_file(payload))
    assert expected == "Hello EMPTYNAME"
    assert "EMPTYNAME" in expected
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    decoded = admit_project_file_part(True, "", "", payload, False)
    metadata: list[str] = []
    cache: list[str] = []
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == expected
    assert decoded["kind"] == "text"
    assert "EMPTYNAME" in decoded["text"]
    assert seen == [payload]
    stored = tmp_path / "cache.txt"
    record = tmp_path / "project.json"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
        record.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == expected
    assert record.read_text(encoding="utf-8") == expected
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", "", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert part["kind"] == ""
        assert "EMPTYNAME" not in part["message"]
    assert seen == [payload]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", "", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYNAME" not in part["message"]
    assert seen == [payload]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_empty_filename_with_nul_continues_unless_text_plain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    real_decoder = text_admission.decode_text_file
    seen: list[bytes] = []

    def _count_decoder(data: bytes) -> str:
        seen.append(data)
        return real_decoder(data)

    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"Hello EMPTYNUL\x00"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "EMPTYNUL" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert not "".startswith("text/")
    assert "text/plain".startswith("text/")
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "", "", payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "EMPTYNUL" not in continued["message"]
    assert seen == []
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _count_decoder,
    )
    decoded = admit_project_file_part(True, "", "text/plain", payload, False)
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == expected
    assert decoded["kind"] == "text"
    assert "EMPTYNUL" in decoded["text"]
    assert "\x00" not in decoded["text"]
    assert seen == [payload]
    stored = tmp_path / "cache.txt"
    record = tmp_path / "project.json"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
        record.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == expected
    assert record.read_text(encoding="utf-8") == expected
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        blank = admit_project_file_part(False, "", "", payload, True)
        _store(blank, metadata, cache)
        assert blank["attach"] is False
        assert blank["metadataWrite"] is False
        assert blank["cacheWrite"] is False
        assert blank["text"] == ""
        assert "EMPTYNUL" not in blank["message"]
        plain = admit_project_file_part(False, "", "text/plain", payload, True)
        _store(plain, metadata, cache)
        assert plain["attach"] is False
        assert plain["metadataWrite"] is False
        assert plain["cacheWrite"] is False
        assert plain["text"] == ""
        assert "EMPTYNUL" not in plain["message"]
    assert seen == [payload]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", "text/plain", payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYNUL" not in part["message"]
    assert seen == [payload]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_empty_filename_with_upper_plain_and_nul_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"Hello EMPTYCASE\x00"
    content_type = "TEXT/PLAIN"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "EMPTYCASE" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert content_type == "TEXT/PLAIN"
    assert not content_type.startswith("text/")
    assert not content_type.startswith("image/")
    assert content_type.lower().startswith("text/")
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "", content_type, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "EMPTYCASE" not in continued["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYCASE" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYCASE" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_empty_filename_with_spaced_plain_and_nul_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"Hello EMPTYSPACE\x00"
    content_type = " text/plain"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "EMPTYSPACE" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert content_type == " text/plain"
    assert content_type.lstrip() == "text/plain"
    assert not content_type.startswith("text/")
    assert not content_type.startswith("image/")
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "", content_type, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "EMPTYSPACE" not in continued["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYSPACE" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYSPACE" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_empty_filename_with_plain_charset_and_nul_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    real_decoder = text_admission.decode_text_file
    seen: list[bytes] = []

    def _count_decoder(data: bytes) -> str:
        seen.append(data)
        return real_decoder(data)

    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _count_decoder,
    )
    payload = b"Hello EMPTYPARAM\x00"
    content_type = "text/plain; charset=utf-8"
    octet = "application/octet-stream"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "EMPTYPARAM" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert content_type.startswith("text/")
    assert ";" in content_type
    assert content_type.split(";", 1)[0] == "text/plain"
    assert not content_type.startswith("image/")
    assert not octet.startswith("text/")
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    decoded = admit_project_file_part(True, "", content_type, payload, False)
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == expected
    assert decoded["kind"] == "text"
    assert "EMPTYPARAM" in decoded["text"]
    assert "\x00" not in decoded["text"]
    assert seen == [payload]
    stored = tmp_path / "cache.txt"
    record = tmp_path / "project.json"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
        record.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == expected
    assert record.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    continued = admit_project_file_part(True, "", octet, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "EMPTYPARAM" not in continued["message"]
    assert seen == [payload]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYPARAM" not in part["message"]
        skipped = admit_project_file_part(False, "", octet, payload, True)
        _store(skipped, metadata, cache)
        assert skipped["attach"] is False
        assert skipped["metadataWrite"] is False
        assert skipped["cacheWrite"] is False
        assert skipped["text"] == ""
        assert "EMPTYPARAM" not in skipped["message"]
    assert seen == [payload]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYPARAM" not in part["message"]
    assert seen == [payload]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_empty_filename_with_spaced_plain_charset_and_nul_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"Hello EMPTYLEAD\x00"
    content_type = " text/plain; charset=utf-8"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "EMPTYLEAD" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert content_type == " text/plain; charset=utf-8"
    assert content_type.lstrip() == "text/plain; charset=utf-8"
    assert content_type.lstrip().startswith("text/")
    assert not content_type.startswith("text/")
    assert ";" in content_type
    assert content_type.split(";", 1)[0] == " text/plain"
    assert not content_type.split(";", 1)[0].startswith("text/")
    assert content_type.lstrip().split(";", 1)[0] == "text/plain"
    assert not content_type.startswith("image/")
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "", content_type, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "EMPTYLEAD" not in continued["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYLEAD" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYLEAD" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_empty_filename_with_upper_plain_charset_and_nul_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"Hello EMPTYCASEPARAM\x00"
    content_type = "TEXT/PLAIN; charset=utf-8"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "EMPTYCASEPARAM" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert content_type == "TEXT/PLAIN; charset=utf-8"
    assert content_type.lower().startswith("text/")
    assert not content_type.startswith("text/")
    assert ";" in content_type
    assert content_type.split(";", 1)[0] == "TEXT/PLAIN"
    assert not content_type.split(";", 1)[0].startswith("text/")
    assert content_type.split(";", 1)[0].lower() == "text/plain"
    assert not content_type.startswith("image/")
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "", content_type, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "EMPTYCASEPARAM" not in continued["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYCASEPARAM" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYCASEPARAM" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_empty_filename_with_tab_plain_and_nul_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"Hello EMPTYTAB\x00"
    content_type = "\ttext/plain"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "EMPTYTAB" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert content_type == "\ttext/plain"
    assert content_type.lstrip() == "text/plain"
    assert content_type.lstrip().startswith("text/")
    assert not content_type.startswith("text/")
    assert not content_type.startswith("image/")
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "", content_type, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "EMPTYTAB" not in continued["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYTAB" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYTAB" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_empty_filename_with_trailing_tab_plain_and_nul_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    real_decoder = text_admission.decode_text_file
    seen: list[bytes] = []

    def _count_decoder(data: bytes) -> str:
        seen.append(data)
        return real_decoder(data)

    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _count_decoder,
    )
    payload = b"Hello EMPTYTRAIL\x00"
    content_type = "text/plain\t"
    octet = "application/octet-stream"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "EMPTYTRAIL" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert content_type == "text/plain\t"
    assert content_type.startswith("text/")
    assert content_type.rstrip() == "text/plain"
    assert content_type != "text/plain"
    assert not content_type.startswith("image/")
    assert not octet.startswith("text/")
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    decoded = admit_project_file_part(True, "", content_type, payload, False)
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == expected
    assert decoded["kind"] == "text"
    assert "EMPTYTRAIL" in decoded["text"]
    assert "\x00" not in decoded["text"]
    assert seen == [payload]
    stored = tmp_path / "cache.txt"
    record = tmp_path / "project.json"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
        record.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == expected
    assert record.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    continued = admit_project_file_part(True, "", octet, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "EMPTYTRAIL" not in continued["message"]
    assert seen == [payload]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYTRAIL" not in part["message"]
        skipped = admit_project_file_part(False, "", octet, payload, True)
        _store(skipped, metadata, cache)
        assert skipped["attach"] is False
        assert skipped["metadataWrite"] is False
        assert skipped["cacheWrite"] is False
        assert skipped["text"] == ""
        assert "EMPTYTRAIL" not in skipped["message"]
    assert seen == [payload]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYTRAIL" not in part["message"]
    assert seen == [payload]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_empty_filename_with_exact_text_prefix_and_nul_attaches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import deepseek_infra.infra.native_runtime.file_text_admission as text_admission

    real_decoder = text_admission.decode_text_file
    seen: list[bytes] = []

    def _count_decoder(data: bytes) -> str:
        seen.append(data)
        return real_decoder(data)

    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _count_decoder,
    )
    payload = b"Hello EMPTYPREFIX\x00"
    content_type = "text/"
    octet = "application/octet-stream"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "EMPTYPREFIX" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert content_type == "text/"
    assert content_type.startswith("text/")
    assert content_type.split("/", 1)[1] == ""
    assert not content_type.startswith("image/")
    assert not octet.startswith("text/")
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    decoded = admit_project_file_part(True, "", content_type, payload, False)
    _store(decoded, metadata, cache)
    assert decoded["attach"] is True
    assert decoded["metadataWrite"] is True
    assert decoded["cacheWrite"] is True
    assert decoded["text"] == expected
    assert decoded["kind"] == "text"
    assert "EMPTYPREFIX" in decoded["text"]
    assert "\x00" not in decoded["text"]
    assert seen == [payload]
    stored = tmp_path / "cache.txt"
    record = tmp_path / "project.json"
    if decoded["metadataWrite"] is True and decoded["cacheWrite"] is True:
        stored.write_text(decoded["text"], encoding="utf-8")
        record.write_text(decoded["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == expected
    assert record.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    continued = admit_project_file_part(True, "", octet, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert "EMPTYPREFIX" not in continued["message"]
    assert seen == [payload]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYPREFIX" not in part["message"]
        skipped = admit_project_file_part(False, "", octet, payload, True)
        _store(skipped, metadata, cache)
        assert skipped["attach"] is False
        assert skipped["metadataWrite"] is False
        assert skipped["cacheWrite"] is False
        assert skipped["text"] == ""
        assert "EMPTYPREFIX" not in skipped["message"]
    assert seen == [payload]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYPREFIX" not in part["message"]
    assert seen == [payload]
    assert metadata == [expected]
    assert cache == [expected]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied", "project.json"]


def test_empty_filename_with_text_and_no_slash_and_nul_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"Hello EMPTYNOSLASH\x00"
    content_type = "text"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "EMPTYNOSLASH" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert content_type == "text"
    assert content_type.startswith("text")
    assert not content_type.startswith("text/")
    assert (content_type + "/").startswith("text/")
    assert not content_type.startswith("image/")
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "", content_type, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "EMPTYNOSLASH" not in continued["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYNOSLASH" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYNOSLASH" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_empty_filename_with_title_text_prefix_and_nul_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"Hello EMPTYTITLE\x00"
    content_type = "Text/"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "EMPTYTITLE" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert content_type == "Text/"
    assert content_type.lower() == "text/"
    assert content_type.lower().startswith("text/")
    assert not content_type.startswith("text/")
    assert content_type.startswith("Text/")
    assert not content_type.startswith("image/")
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "", content_type, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "EMPTYTITLE" not in continued["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYTITLE" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYTITLE" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_empty_filename_with_upper_text_prefix_and_nul_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _block_project_writers(monkeypatch)
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.extract_html_text",
        _refuse_html_extractor,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission._image_magic_ok",
        _refuse_image_magic,
    )
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.file_text_admission.decode_text_file",
        _refuse_text_decoder,
    )
    payload = b"Hello EMPTYUPPERPREFIX\x00"
    content_type = "TEXT/"
    expected = normalize_extracted_text(decode_text_file(payload))
    assert "EMPTYUPPERPREFIX" in expected
    assert "\x00" not in expected
    assert payload.index(b"\x00") < 2048
    assert content_type == "TEXT/"
    assert content_type.lower() == "text/"
    assert content_type.lower().startswith("text/")
    assert not content_type.startswith("text/")
    assert "/" in content_type
    assert content_type.split("/", 1)[1] == ""
    assert not content_type.startswith("image/")
    assert Path("").suffix == ""
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    metadata: list[str] = []
    cache: list[str] = []
    continued = admit_project_file_part(True, "", content_type, payload, False)
    _store(continued, metadata, cache)
    assert continued["attach"] is True
    assert continued["metadataWrite"] is False
    assert continued["cacheWrite"] is False
    assert continued["text"] == ""
    assert continued["kind"] == ""
    assert continued["message"] == ""
    assert continued["text"] != expected
    assert "EMPTYUPPERPREFIX" not in continued["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYUPPERPREFIX" not in part["message"]
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.project_file_admission.admit_file_text_part",
        _refuse_file_text_part,
    )
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        _store(ownership, metadata, cache)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "", content_type, payload, True)
        _store(part, metadata, cache)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "EMPTYUPPERPREFIX" not in part["message"]
    assert metadata == []
    assert cache == []
    assert sorted(path.name for path in tmp_path.iterdir()) == ["denied"]


def test_rust_route_gates_project_writes() -> None:
    route = ROUTE.read_text(encoding="utf-8")
    projects = PROJECTS.read_text(encoding="utf-8")
    upload = UPLOAD.read_text(encoding="utf-8")
    handler = route.split("pub(crate) async fn api_project_files(", 1)[1]
    handler = handler.split("fn root(", 1)[0]
    assert handler.index('may_write_native_store("project_metadata_store")') < handler.index(
        "admit_project_metadata_write("
    )
    assert handler.index("admit_project_metadata_write(") < handler.index("if !ownership.allowed")
    assert handler.index("if !ownership.allowed") < handler.index("read_multipart_upload(")
    assert handler.index("read_multipart_upload(") < handler.index("files.is_empty()")
    assert handler.index("files.is_empty()") < handler.index("admit_project_file_part(")
    assert handler.index("if !decision.attach") < handler.index("add_project_files(")
    reader = upload.split("pub(crate) fn read_multipart_upload(", 1)[1]
    reader = reader.split("pub(crate) fn declared_content_length(", 1)[0]
    assert reader.index("admit_upload_body(") < reader.index("parse_multipart(")
    owner = projects.split("pub fn admit_project_metadata_write(", 1)[1]
    owner = owner.split("pub struct ProjectFilePartAdmission", 1)[0]
    part = projects.split("pub fn admit_project_file_part(", 1)[1]
    part = part.split("pub fn add_project_files(", 1)[0]
    assert "admit_file_text_part(" in part
    assert "write_project(" not in owner
    assert "write_project(" not in part
    assert "extract_uploaded_file(" not in part
    text_arm = part.split("FileTextAction::Text { text, kind }", 1)[1]
    text_arm = text_arm.split("FileTextAction::Continue", 1)[0]
    assert "cache_write && !text.is_empty()" in text_arm
    assert text_arm.index("metadata_write: true") < text_arm.index("cache_write: true")
    policy = POLICY.read_text(encoding="utf-8")
    epub = policy.split('if extension == ".epub"', 1)[1]
    epub = epub.split('if extension == ".pdf"', 1)[0]
    assert epub.index("extract_epub_text(") < epub.index("normalize_extracted_text(")
    assert "cache_write: true" in epub
    assert "write_cache(" not in epub
    assert "write_project(" not in epub
    docx = policy.split('if extension == ".docx"', 1)[1]
    docx = docx.split('if extension == ".pptx"', 1)[0]
    assert docx.index("extract_docx_text(") < docx.index("normalize_extracted_text(")
    assert "cache_write: true" in docx
    assert "write_cache(" not in docx
    assert "write_project(" not in docx
    pptx = policy.split('if extension == ".pptx"', 1)[1]
    pptx = pptx.split('if extension == ".xlsx"', 1)[0]
    assert pptx.index("extract_pptx_text(") < pptx.index("normalize_extracted_text(")
    assert "cache_write: true" in pptx
    assert "write_cache(" not in pptx
    assert "write_project(" not in pptx
    xlsx = policy.split('if extension == ".xlsx"', 1)[1]
    xlsx = xlsx.split('if extension == ".epub"', 1)[0]
    assert xlsx.index("extract_xlsx_text(") < xlsx.index("normalize_extracted_text(")
    assert "cache_write: true" in xlsx
    assert "write_cache(" not in xlsx
    assert "write_project(" not in xlsx
    html = policy.split("HTML_EXTENSIONS.contains(&extension.as_str())", 1)[1]
    html = html.split("if is_text_file(", 1)[0]
    assert html.index("extract_html_text(") < html.index("normalize_extracted_text(")
    assert "trim_start_matches('.')" in html
    assert "content_type" not in html
    assert "decode_text_file(" not in html
    assert "take(2048)" not in html
    assert "image_magic_ok(" not in html
    assert "cache_write: true" in html
    assert "write_cache(" not in html
    assert "write_project(" not in html
    text = policy.split("if is_text_file(", 1)[1]
    text = text.split("continue_file()", 1)[0]
    assert text.index("decode_text_file(") < text.index("normalize_extracted_text(")
    assert "image_magic_ok(" not in text
    assert "extract_html_text(" not in text
    assert "stripped.is_empty()" in text
    assert '"text".to_string()' in text
    assert "trim_start_matches('.')" in text
    python_admit = (ROOT / "deepseek_infra/infra/native_runtime/file_text_admission.py").read_text(encoding="utf-8")
    python_text = python_admit.split("if is_text_file(", 1)[1]
    python_text = python_text.split("def _admit_docx(", 1)[0]
    assert 'else "text"' in python_text
    assert "Path(name.lower()).suffix" in python_admit
    extension_fn = policy.split("fn extension_of(", 1)[1]
    extension_fn = extension_fn.split("fn chars_of(", 1)[0]
    assert "rfind('.')" in extension_fn
    assert "index > 0" in extension_fn
    assert "String::new()" in extension_fn
    extensions = policy.split("const TEXT_EXTENSIONS", 1)[1].split("const HTML_EXTENSIONS", 1)[0]
    assert '".md"' in extensions
    assert '".csv"' in extensions
    assert '".json"' in extensions
    assert '".yaml"' in extensions
    assert '".yml"' in extensions
    assert '".markdown"' in extensions
    assert '".tsv"' in extensions
    assert '".jsonl"' in extensions
    assert '".xml"' in extensions
    assert '".css"' in extensions
    assert '".js",' in extensions
    assert '".mjs",' in extensions
    assert '".cjs",' in extensions
    assert '".ts",' in extensions
    assert '".tsx",' in extensions
    assert '".jsx",' in extensions
    assert '".py",' in extensions
    assert '".java",' in extensions
    assert '".c",' in extensions
    assert '".cpp",' in extensions
    assert '".h",' in extensions
    assert '".hpp",' in extensions
    assert '".cs",' in extensions
    assert '".go",' in extensions
    assert '".rs",' in extensions
    assert '".php",' in extensions
    assert '".rb",' in extensions
    assert '".swift",' in extensions
    assert '".kt",' in extensions
    assert '".sql",' in extensions
    assert '".sh",' in extensions
    assert '".ps1",' in extensions
    assert '".bat",' in extensions
    assert '".log",' in extensions
    assert '".ini",' in extensions
    assert '".toml",' in extensions
    assert '".env",' in extensions
    assert '".txt",' in extensions
    assert '".rtf",' in extensions
    assert '".png",' not in extensions
    assert '".jpg",' not in extensions
    assert '".jpeg",' not in extensions
    assert '".webp",' not in extensions
    assert '".bmp",' not in extensions
    assert '".tif",' not in extensions
    assert '".tiff",' not in extensions
    assert '".gif",' not in extensions
    assert '".bin",' not in extensions
    html_extensions = policy.split("const HTML_EXTENSIONS", 1)[1].split("const IMAGE_EXTENSIONS", 1)[0]
    assert '".html",' in html_extensions
    assert '".htm"]' in html_extensions
    assert '".xhtml"' not in html_extensions
    assert '".xml"' not in html_extensions
    assert '".css"' not in html_extensions
    assert '".js"' not in html_extensions
    assert '".mjs"' not in html_extensions
    assert '".cjs"' not in html_extensions
    assert '".ts"' not in html_extensions
    assert '".tsx"' not in html_extensions
    assert '".jsx"' not in html_extensions
    assert '".py"' not in html_extensions
    assert '".java"' not in html_extensions
    assert '".c"' not in html_extensions
    assert '".cpp"' not in html_extensions
    assert '".h",' not in html_extensions
    assert '".hpp"' not in html_extensions
    assert '".cs",' not in html_extensions
    assert '".go",' not in html_extensions
    assert '".rs",' not in html_extensions
    assert '".php",' not in html_extensions
    assert '".rb",' not in html_extensions
    assert '".swift",' not in html_extensions
    assert '".kt",' not in html_extensions
    assert '".sql",' not in html_extensions
    assert '".sh",' not in html_extensions
    assert '".ps1",' not in html_extensions
    assert '".bat",' not in html_extensions
    assert '".log",' not in html_extensions
    assert '".ini",' not in html_extensions
    assert '".toml",' not in html_extensions
    assert '".env",' not in html_extensions
    assert '".rtf",' not in html_extensions
    assert '".png",' not in html_extensions
    assert '".jpg",' not in html_extensions
    assert '".jpeg",' not in html_extensions
    assert '".webp",' not in html_extensions
    assert '".bmp",' not in html_extensions
    assert '".tif",' not in html_extensions
    assert '".tiff",' not in html_extensions
    assert '".gif",' not in html_extensions
    images = policy.split("const IMAGE_EXTENSIONS", 1)[1].split("];", 1)[0]
    assert '".png",' in images
    assert '".jpg",' in images
    assert '".jpeg",' in images
    assert '".webp",' in images
    assert '".bmp",' in images
    assert '".tif",' in images
    assert '".tiff",' in images
    assert '".gif",' in images
    assert '".bin",' not in images
    classifier = policy.split("pub fn is_image_file(", 1)[1]
    classifier = classifier.split("pub fn is_text_file(", 1)[0]
    assert classifier.index("split(';')") < classifier.index("trim()")
    assert classifier.index("trim()") < classifier.index("to_ascii_lowercase()")
    assert classifier.index("to_ascii_lowercase()") < classifier.index('starts_with("image/")')
    magic = policy.split("fn image_magic_ok(", 1)[1]
    magic = magic.split("pub fn admit_file_text_part(", 1)[0]
    assert 'b"GIF87a"' in magic
    assert 'b"GIF89a"' in magic
    sniff = policy.split("pub fn is_text_file(", 1)[1]
    sniff = sniff.split("fn extension_of(", 1)[0]
    assert sniff.index('starts_with("text/")') < sniff.index("take(2048)")
    assert "take(2047)" not in sniff
    assert "take(2049)" not in sniff
    assert "split(';')" not in sniff
    pdf_arm = policy.split('if extension == ".pdf"', 1)[1]
    pdf_arm = pdf_arm.split("if is_text_file(", 1)[0]
    assert "continue_file()" in pdf_arm
    assert pdf_arm.index("continue_file()") < pdf_arm.index("if is_image_file(")
    assert "image_magic_ok(" not in pdf_arm.split("if is_image_file(", 1)[0]
    assert "decode_text_file(" not in pdf_arm
    empty_prefix = policy.split("pub fn admit_file_text_part(", 1)[1]
    empty_prefix = empty_prefix.split('if extension == ".pdf"', 1)[0]
    assert "data.is_empty()" in empty_prefix
    assert "Uploaded file is empty: {filename}" in empty_prefix
    assert "is_image_file(" not in empty_prefix
    text_prefix = sniff.split('starts_with("text/")', 1)[0]
    assert "trim()" not in text_prefix
    assert ".strip(" not in text_prefix
    assert "to_ascii_lowercase()" not in text_prefix
    admit_prefix = policy.split("pub fn admit_file_text_part(", 1)[1]
    admit_prefix = admit_prefix.split("if is_text_file(", 1)[0]
    assert "to_ascii_lowercase()" not in admit_prefix
    python_files = (ROOT / "deepseek_infra/infra/rag/files.py").read_text(encoding="utf-8")
    python_sniff = python_files.split("def is_text_file(", 1)[1]
    python_sniff = python_sniff.split("def decode_text_file(", 1)[0]
    assert "sample = data[:2048]" in python_sniff
    assert "data[:2047]" not in python_sniff
    assert "data[:2049]" not in python_sniff
    assert python_sniff.index('startswith("text/")') < python_sniff.index('b"\\0" not in sample')
    assert 'split(";")' not in python_sniff
    assert "split(';')" not in python_sniff
    python_continued = python_admit.split("if extension in _CONTINUED_EXTENSIONS", 1)[1]
    python_continued = python_continued.split("if is_text_file(", 1)[0]
    assert "_continue()" in python_continued
    assert python_continued.index("_continue()") < python_continued.index("if is_image_file(")
    assert "_image_magic_ok(" not in python_continued.split("if is_image_file(", 1)[0]
    assert "decode_text_file(" not in python_continued
    python_empty = python_admit.split("def admit_file_text_part(", 1)[1]
    python_empty = python_empty.split("if extension in _CONTINUED_EXTENSIONS", 1)[0]
    assert 'payload == b""' in python_empty
    assert "Uploaded file is empty: {name}" in python_empty
    assert "if is_image_file(" not in python_empty
    python_prefix = python_sniff.split('startswith("text/")', 1)[0]
    assert ".strip(" not in python_prefix
    assert ".trim(" not in python_prefix
    assert ".lower(" not in python_prefix
    media_assign = python_admit.split("media = content_type", 1)[1]
    media_assign = media_assign.split("if is_text_file(", 1)[0]
    assert ".lower(" not in media_assign
    assert ".strip(" not in media_assign
    html_arm = python_admit.split("if extension in HTML_EXTENSIONS", 1)[1]
    html_arm = html_arm.split("if is_text_file(", 1)[0]
    assert "extract_html_text(" in html_arm
    assert "content_type" not in html_arm
    assert "decode_text_file(" not in html_arm
    assert 'b"\\0"' not in html_arm
    assert "_image_magic_ok(" not in html_arm
    python_html_set = python_files.split("HTML_EXTENSIONS = ", 1)[1].split("\n", 1)[0]
    assert '".html"' in python_html_set
    assert '".htm"}' in python_html_set
    assert ".xhtml" not in python_html_set
    assert 'b"\\0" not in sample' in python_sniff
    image = policy.split("if is_image_file(&extension, content_type)", 1)[1]
    image = image.split("if HTML_EXTENSIONS.contains", 1)[0]
    assert image.index("!ocr_enabled") < image.index("!image_magic_ok(")
    assert image.index("!image_magic_ok(") < image.index("continue_file()")
    assert "ocr_image(" not in image
    assert "decode_text_file(" not in image
    assert "Uploaded image is corrupt." in image
    admit_fn = policy.split("pub fn admit_file_text_part(", 1)[1]
    admit_fn = admit_fn.split("pub fn extract_uploaded_file(", 1)[0]
    assert admit_fn.index("if is_image_file(") < admit_fn.index("if is_text_file(")
    assert admit_fn.index("if is_image_file(") < admit_fn.index("HTML_EXTENSIONS.contains")
    python_image = python_admit.split("if is_image_file(", 1)[1]
    python_image = python_image.split("if is_text_file(", 1)[0]
    assert "ocr_enabled is not True" in python_image
    assert python_image.index("ocr_enabled is not True") < python_image.index("_image_magic_ok(")
    assert python_admit.index("if is_image_file(") < python_admit.index("if extension in HTML_EXTENSIONS")
    assert "decode_text_file(" not in python_image
    assert "Uploaded image is corrupt." in python_image
    assert "cache_write: true" in text
    assert "write_cache(" not in text
    assert "write_project(" not in text
    writer = projects.split("pub fn add_project_files(", 1)[1]
    writer = writer.split("fn project_document_from_extracted(", 1)[0]
    assert writer.index("extract_uploaded_file(") < writer.index("write_project(")
    for source in (handler, owner, part):
        assert "deepseek_infra" not in source
        assert 'Command::new("python")' not in source
        assert "NOT_APPLIED" not in source
        assert "CONFIRMED" not in source
        assert "install_authoritative_epoch" not in source
        assert "install_signed_epoch" not in source
    assert "NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED" in owner
    assert "The project metadata store is still written by the Python runtime, " in projects
