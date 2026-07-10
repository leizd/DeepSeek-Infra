from __future__ import annotations

import json
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

import deepseek_infra.infra.rag.files as files
from deepseek_infra.core.errors import AppError, ErrorCode


@pytest.fixture
def tmp_files_dir(monkeypatch):
    base = Path("C:/Users/12393/AppData/Local/Temp/opencode")
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as tmp_dir:
        root = Path(tmp_dir)
        file_cache_dir = root / ".file-cache"
        file_cache_dir.mkdir()
        projects_dir = root / ".projects"
        monkeypatch.setattr(files, "FILE_CACHE_DIR", file_cache_dir)
        monkeypatch.setattr(files, "PROJECTS_DIR", projects_dir)
        files._load_cached_file_cached.cache_clear()
        yield root
        files._load_cached_file_cached.cache_clear()


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class TestAttachmentContext:
    def test_build_attachment_context_empty(self) -> None:
        assert files.build_attachment_context([], "query") == ""

    def test_build_attachment_context_legacy_text(self) -> None:
        context = files.build_attachment_context(
            [{"text": "hello world", "name": "note.txt", "kind": "text"}], "q"
        )
        assert "[用户上传文件上下文]" in context
        assert "hello world" in context

    def test_build_attachment_context_ignores_non_dict_items(self) -> None:
        context = files.build_attachment_context(["bad", {"text": "ok"}], "q")
        assert "ok" in context
        assert "bad" not in context

    def test_build_attachment_context_budget_exhausted(self) -> None:
        with patch.object(files, "FILE_CONTEXT_CHAR_BUDGET", 1):
            context = files.build_attachment_context(
                [
                    {"text": "hello world", "name": "note1.txt"},
                    {"text": "second", "name": "note2.txt"},
                ],
                "q",
            )
        assert "预算不足" in context

    def test_build_attachment_context_load_failure(self, tmp_files_dir: Path) -> None:
        context = files.build_attachment_context(
            [{"fileId": "0" * 32, "projectId": "", "name": "missing.txt"}], "q"
        )
        assert "文件索引读取失败" in context


class TestFormatCachedFileContext:
    def test_format_cached_file_context_no_chunks(self) -> None:
        cached = {"name": "empty.txt", "kind": "text", "charCount": 0, "chunks": [], "id": "", "projectId": ""}
        result = files.format_cached_file_context(1, cached, "q")
        assert "未找到可用文本片段" in result

    def test_format_cached_file_context_with_line_numbers(self) -> None:
        cached = {
            "name": "doc.txt",
            "kind": "text",
            "charCount": 20,
            "chunks": [
                {"index": 0, "start": 0, "end": 5, "lineStart": 1, "lineEnd": 2, "text": "hello"},
            ],
            "id": "",
            "projectId": "",
        }
        result = files.format_cached_file_context(1, cached, "q", char_budget=1000)
        assert "hello" in result
        assert "行 1-2" in result

    def test_format_chunk_locator(self) -> None:
        assert "行 3-5" in files.format_chunk_locator({"lineStart": 3, "lineEnd": 5}, 1, 1, 0, 10)


class TestChunkSelectionHeuristics:
    def test_is_broad_file_query(self) -> None:
        assert files.is_broad_file_query("总结一下全文")
        assert files.is_broad_file_query("give me an outline")
        assert not files.is_broad_file_query("specific number")

    def test_select_file_chunk_indices_empty(self) -> None:
        assert files.select_file_chunk_indices([], "q") == []

    def test_select_file_chunk_indices_small_text_returns_all(self) -> None:
        chunks = files.chunk_text("small")
        assert files.select_file_chunk_indices(chunks, "q", char_budget=10000) == [0]

    def test_select_file_chunk_indices_broad_query(self) -> None:
        chunks = files.chunk_text("alpha\n" * 2000)
        indices = files.select_file_chunk_indices(chunks, "全文总结", char_budget=10000)
        assert 0 in indices


class TestExtractUploadedFile:
    def test_extract_uploaded_file_rejects_empty(self) -> None:
        with pytest.raises(AppError) as cm:
            files.extract_uploaded_file("empty.txt", "text/plain", b"")
        assert cm.value.status == 400

    def test_extract_uploaded_file_rejects_unsupported_type(self) -> None:
        with pytest.raises(AppError) as cm:
            files.extract_uploaded_file("file.bin", "application/octet-stream", b"\x00\x01\x02\x03")
        assert cm.value.code == ErrorCode.UNSUPPORTED_FILE

    def test_extract_uploaded_xlsx(self, tmp_files_dir: Path) -> None:
        pytest.importorskip("openpyxl")
        from openpyxl import Workbook
        buffer = BytesIO()
        workbook = Workbook()
        worksheet = workbook.active
        assert worksheet is not None
        worksheet.append(["a", "b"])
        workbook.save(buffer)
        extracted = files.extract_uploaded_file("book.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer.getvalue())
        assert extracted["kind"] == "xlsx"


class TestPageHelpers:
    def test_infer_original_page_count(self) -> None:
        assert files.infer_original_page_count("pdf", b"%PDF") >= 0
        assert files.infer_original_page_count("image", b"png") == 1
        assert files.infer_original_page_count("text", b"x") == 0

    def test_fallback_page_texts_from_text(self) -> None:
        pages = files.fallback_page_texts_from_text("a\nb\nc", page_count=2)
        assert len(pages) == 2
        assert pages[0]["page"] == 1

    def test_chunk_text_empty(self) -> None:
        assert files.chunk_text("") == []

    def test_extract_image_text_requires_ocr(self) -> None:
        with pytest.raises(AppError) as cm:
            files.extract_image_text(b"\x89PNG", ocr_enabled=False)
        assert cm.value.code == ErrorCode.OCR_REQUIRED

    def test_normalized_page_texts(self) -> None:
        assert files.normalized_page_texts([{"page": 1, "text": " hello "}]) == [{"page": 1, "text": "hello"}]
        assert files.normalized_page_texts([{"page": 0, "text": "x"}]) == []
        assert files.normalized_page_texts("bad") == []

    def test_page_text_for_index(self) -> None:
        assert files.page_text_for_index([{"page": 2, "text": "t"}], 2) == "t"
        assert files.page_text_for_index([{"page": 1, "text": "t"}], 2) == ""

    def test_page_text_from_cached_chunks(self) -> None:
        cached = {"chunks": [{"text": "page one"}, {"text": "page two"}]}
        assert "page one" in files.page_text_from_cached_chunks(cached, requested_page=1, page_count=1)
        assert "page two" in files.page_text_from_cached_chunks(cached, requested_page=2, page_count=2)


class TestCacheIO:
    def test_load_cached_file_with_project_id(self, tmp_files_dir: Path) -> None:
        extracted = files.extract_uploaded_file("a.txt", "text/plain", b"hello", project_id="proj-1")
        cached = files.load_cached_file(str(extracted["fileId"]), project_id="proj-1")
        assert cached["name"] == "a.txt"

    def test_cached_file_source_missing_source(self, tmp_files_dir: Path) -> None:
        extracted = files.extract_uploaded_file("a.txt", "text/plain", b"hello")
        file_id = str(extracted["fileId"])
        source_path = files.FILE_CACHE_DIR / f"{file_id}.source"
        source_path.unlink()
        with pytest.raises(AppError) as cm:
            files.cached_file_source(file_id)
        assert cm.value.status == 410

    def test_cleanup_when_cache_dir_missing(self, tmp_files_dir: Path) -> None:
        files.FILE_CACHE_DIR.rmdir()
        files.cleanup_file_cache()

    def test_project_file_cache_dir_invalid(self) -> None:
        with pytest.raises(AppError) as cm:
            files.project_file_cache_dir("bad")
        assert cm.value.status == 400


class TestReaderHelpers:
    def test_file_reader_window_empty_chunks(self, tmp_files_dir: Path) -> None:
        file_id = "d" * 32
        cache_dir = Path(files.FILE_CACHE_DIR)
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{file_id}.json").write_text(
            json.dumps({"id": file_id, "name": "empty.txt", "type": "text/plain", "size": 0, "kind": "text", "charCount": 0, "chunkCount": 0, "chunks": []}),
            encoding="utf-8",
        )
        result = files.file_reader_window(file_id)
        assert result["window"]["totalChunks"] == 0

    def test_file_page_image_rejects_non_pdf(self, tmp_files_dir: Path) -> None:
        extracted = files.extract_uploaded_file("a.txt", "text/plain", b"hello")
        with pytest.raises(AppError) as cm:
            files.file_page_image(str(extracted["fileId"]), page=1)
        assert cm.value.status == 415

    def test_file_page_layout_rejects_non_pdf(self, tmp_files_dir: Path) -> None:
        extracted = files.extract_uploaded_file("a.txt", "text/plain", b"hello")
        with pytest.raises(AppError) as cm:
            files.file_page_layout(str(extracted["fileId"]), page=1)
        assert cm.value.status == 415

    def test_file_page_search_rejects_empty_query(self, tmp_files_dir: Path) -> None:
        extracted = files.extract_uploaded_file("a.txt", "text/plain", b"hello")
        with pytest.raises(AppError) as cm:
            files.file_page_search(str(extracted["fileId"]), query="")
        assert cm.value.status == 400

    def test_reader_positive_int(self) -> None:
        assert files._reader_positive_int("5", "err", default=1) == 5
        assert files._reader_positive_int(None, "err", default=3) == 3
        assert files._reader_positive_int("0", "err", default=1) == 1
        with pytest.raises(AppError):
            files._reader_positive_int("bad", "err", default=1)

    def test_reader_scale_float(self) -> None:
        assert files._reader_scale_float("2.0", "err", default=1.0) == 2.0
        assert files._reader_scale_float(None, "err", default=1.0) == 1.0
        with pytest.raises(AppError):
            files._reader_scale_float("-1", "err", default=1.0)
        with pytest.raises(AppError):
            files._reader_scale_float("bad", "err", default=1.0)
        assert files._reader_scale_float("10.0", "err", default=1.0) == files.FILE_PAGE_IMAGE_MAX_SCALE

    def test_reader_chunk_payload(self) -> None:
        payload = files._reader_chunk_payload({"index": "x", "text": "hello"}, fallback_index=5)
        assert payload["index"] == 6
        assert payload["text"] == "hello"


class TestFileTypeDetection:
    def test_is_text_file(self) -> None:
        assert files.is_text_file(".txt", "text/plain", b"hello")
        assert files.is_text_file(".py", "application/octet-stream", b"import os\n")
        assert not files.is_text_file(".bin", "application/octet-stream", b"\x00\x01")

    def test_decode_text_file_fallback(self) -> None:
        assert files.decode_text_file(b"\xff\xfe") != ""


class TestHtmlExtractor:
    def test_extract_html_text_strips_script_and_style(self) -> None:
        html = b"<html><style>css</style><body>text</body></html>"
        assert files.extract_html_text(html) == "text"

    def test_extract_html_text_adds_newlines(self) -> None:
        html = b"<p>one</p><div>two</div>"
        assert "one" in files.extract_html_text(html)
        assert "two" in files.extract_html_text(html)


class TestEpubExtractor:
    def test_extract_epub_text_bad_zip(self) -> None:
        with pytest.raises(AppError) as cm:
            files.extract_epub_text(b"not a zip")
        assert cm.value.status == 422

    def test_extract_epub_text_skips_nav(self) -> None:
        data = make_zip(
            {
                "OPS/nav.xhtml": b"<html><body>nav</body></html>",
                "OPS/chapter.xhtml": b"<html><body><p>chapter</p></body></html>",
            }
        )
        text = files.extract_epub_text(data)
        assert "chapter" in text
        assert "nav" not in text


class TestPptxExtractor:
    def test_extract_pptx_text_bad_zip(self) -> None:
        with pytest.raises(AppError) as cm:
            files.extract_pptx_text(b"not a zip")
        assert cm.value.status == 422

    def test_extract_pptx_text_parse_error(self) -> None:
        data = make_zip({"ppt/slides/slide1.xml": b"not xml"})
        with pytest.raises(AppError) as cm:
            files.extract_pptx_text(data)
        assert cm.value.status == 422

    def test_extract_presentation_xml_text(self) -> None:
        xml = b'<p:sld xmlns:p="urn"><p:t>hello</p:t></p:sld>'
        assert files.extract_presentation_xml_text(xml) == "hello"

    def test_slide_sort_key(self) -> None:
        assert files.slide_sort_key("ppt/slides/slide10.xml") == 10


class TestZipValidation:
    def test_validate_zip_size_compression_ratio(self) -> None:
        data = make_zip({"x": b"a" * 100})
        with zipfile.ZipFile(BytesIO(data)) as archive, patch.object(files, "MAX_ZIP_COMPRESSION_RATIO", 1):
            with pytest.raises(AppError) as cm:
                files.validate_zip_size(archive)
            assert cm.value.code == ErrorCode.UPLOAD_TOO_LARGE

    def test_safe_zip_read_missing_entry(self) -> None:
        data = make_zip({"x": b"y"})
        with zipfile.ZipFile(BytesIO(data)) as archive:
            with pytest.raises(AppError) as cm:
                files.safe_zip_read(archive, "missing")
            assert cm.value.status == 422


class TestDocxExtractor:
    def test_extract_docx_text_bad_zip(self) -> None:
        with pytest.raises(AppError) as cm:
            files.extract_docx_text(b"not a zip")
        assert cm.value.status == 422

    def test_extract_docx_text_parse_error(self) -> None:
        data = make_zip({"word/document.xml": b"not xml"})
        with pytest.raises(AppError) as cm:
            files.extract_docx_text(data)
        assert cm.value.status == 422

    def test_extract_word_xml_text_with_table(self) -> None:
        namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xml = f"""<?xml version="1.0"?>
<w:document xmlns:w="{namespace}">
  <w:body>
    <w:p><w:r><w:t>paragraph</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>cell</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
  </w:body>
</w:document>""".encode()
        text = files.extract_word_xml_text(xml)
        assert "paragraph" in text
        assert "cell" in text


class TestXlsxExtractor:
    def test_extract_xlsx_text_fallback(self) -> None:
        data = make_zip(
            {
                "xl/workbook.xml": b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="Sheet1" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets></workbook>',
                "xl/_rels/workbook.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
                "xl/worksheets/sheet1.xml": b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><row r="1"><c r="A1" t="inlineStr"><is><t>hello</t></is></c></row></worksheet>',
            }
        )
        with patch.dict("sys.modules", {"openpyxl": None}):
            text = files.extract_xlsx_text(data)
        assert "hello" in text

    def test_read_xlsx_sheet_entries(self) -> None:
        data = make_zip(
            {
                "xl/workbook.xml": b'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="S1" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets></workbook>',
                "xl/_rels/workbook.xml.rels": b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
                "xl/worksheets/sheet1.xml": b"<worksheet/>",
            }
        )
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = files.read_xlsx_sheet_entries(archive)
        assert entries[0][0] == "S1"

    def test_read_xlsx_shared_strings(self) -> None:
        data = make_zip(
            {
                "xl/sharedStrings.xml": b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>shared</t></si></sst>',
            }
        )
        with zipfile.ZipFile(BytesIO(data)) as archive:
            strings = files.read_xlsx_shared_strings(archive)
        assert strings == ["shared"]

    def test_read_xlsx_sheet(self) -> None:
        xml = b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><row r="1"><c r="A1" t="s"><v>0</v></c></row></worksheet>'
        text = files.read_xlsx_sheet(xml, ["shared"])
        assert "A1=shared" in text


class TestPdfTextExtraction:
    def test_extract_pdf_page_texts_native_no_pdf_library(self) -> None:
        def fake_import(name: str, *args, **kwargs):
            raise ModuleNotFoundError(name)
        with patch("builtins.__import__", side_effect=fake_import):
            with pytest.raises(AppError) as cm:
                files.extract_pdf_page_texts_native(b"%PDF")
            assert cm.value.status == 415
