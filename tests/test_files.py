from __future__ import annotations

import json
import os
import shutil
import unittest
import uuid
import zipfile
from io import BytesIO
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import deepseek_infra.infra.rag.files as files
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.rag.files import (
    cached_file_source,
    chunk_text,
    decode_text_file,
    file_page_image,
    file_page_layout,
    file_page_search,
    file_page_text,
    file_reader_window,
    normalize_extracted_text,
    select_file_chunk_indices,
)


class FilesTests(unittest.TestCase):
    def test_decode_text_file_supports_utf8_sig_and_gb18030(self) -> None:
        self.assertEqual(decode_text_file("hello".encode("utf-8-sig")), "hello")
        self.assertEqual(decode_text_file("中文".encode("gb18030")), "中文")

    def test_normalize_extracted_text_strips_nulls_and_crlf(self) -> None:
        self.assertEqual(normalize_extracted_text("a\r\nb\x00\r\n"), "a\nb")

    def test_chunk_text_adds_line_metadata(self) -> None:
        chunks = chunk_text("first\nsecond\n" * 1000)
        self.assertTrue(chunks)
        self.assertEqual(chunks[0]["lineStart"], 1)
        self.assertGreaterEqual(chunks[0]["lineEnd"], chunks[0]["lineStart"])

    def test_chunk_short_text_returns_single_chunk(self) -> None:
        chunks = chunk_text("short")

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["text"], "short")
        self.assertEqual(chunks[0]["start"], 0)
        self.assertEqual(chunks[0]["lineStart"], 1)

    def test_chunk_overlap_preserves_context(self) -> None:
        chunks = chunk_text("x" * (files.FILE_CHUNK_CHARS * 3))

        self.assertGreaterEqual(len(chunks), 3)
        for index in range(1, len(chunks)):
            self.assertLess(chunks[index]["start"], chunks[index - 1]["end"])

    def test_chunk_line_numbers_cover_full_text(self) -> None:
        chunks = chunk_text("line1\nline2\nline3\nline4\nline5")

        self.assertEqual(chunks[0]["lineStart"], 1)
        self.assertGreaterEqual(chunks[-1]["lineEnd"], 5)

    def test_select_file_chunk_indices_prefers_query_matches(self) -> None:
        text = ("alpha\n" * 3000) + ("needle target\n" * 20) + ("omega\n" * 3000)
        chunks = chunk_text(text)
        selected = select_file_chunk_indices(chunks, "needle target", char_budget=7000)
        self.assertTrue(selected)
        selected_text = "\n".join(chunks[index]["text"] for index in selected)
        self.assertIn("needle target", selected_text)


class CachedFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cache_dir = Path.cwd() / f".test-file-cache-{uuid.uuid4().hex}"
        self.cache_dir.mkdir()
        self.cache_dir_patch = patch.object(files, "FILE_CACHE_DIR", self.cache_dir)
        self.cache_dir_patch.start()
        files._load_cached_file_cached.cache_clear()
        self.addCleanup(lambda: shutil.rmtree(self.cache_dir, ignore_errors=True))
        self.addCleanup(self.cache_dir_patch.stop)
        self.addCleanup(files._load_cached_file_cached.cache_clear)

    def write_cached_file(self, file_id: str, name: str) -> Path:
        path = self.cache_dir / f"{file_id}.json"
        path.write_text(json.dumps({"id": file_id, "name": name, "chunks": []}), encoding="utf-8")
        return path

    def test_load_cached_file_reuses_parsed_json_for_same_mtime(self) -> None:
        file_id = "a" * 32
        self.write_cached_file(file_id, "first.txt")

        with patch.object(files.json, "loads", wraps=files.json.loads) as loads:
            first = files.load_cached_file(file_id)
            second = files.load_cached_file(file_id)

        self.assertIs(first, second)
        self.assertEqual(loads.call_count, 1)

    def test_load_cached_file_invalidates_when_mtime_changes(self) -> None:
        file_id = "b" * 32
        path = self.write_cached_file(file_id, "first.txt")
        first = files.load_cached_file(file_id)

        path.write_text(json.dumps({"id": file_id, "name": "second.txt", "chunks": []}), encoding="utf-8")
        next_mtime = path.stat().st_mtime_ns + 1_000_000_000
        os.utime(path, ns=(next_mtime, next_mtime))
        second = files.load_cached_file(file_id)

        self.assertEqual(first["name"], "first.txt")
        self.assertEqual(second["name"], "second.txt")

    def test_load_cached_file_preserves_validation_errors(self) -> None:
        with self.assertRaises(AppError) as invalid:
            files.load_cached_file("not-a-cache-id")
        self.assertEqual(invalid.exception.status, 400)
        self.assertEqual(invalid.exception.code, ErrorCode.INVALID_PAYLOAD)

        with self.assertRaises(AppError) as missing:
            files.load_cached_file("c" * 32)
        self.assertEqual(missing.exception.status, 410)
        self.assertEqual(missing.exception.code, ErrorCode.FILE_INDEX_EXPIRED)

    def test_load_cached_file_rejects_path_traversal_shapes(self) -> None:
        for bad_id in ["../../../etc/passwd", "abc", "z" * 32, "ABCDEF" + "0" * 26, "0" * 32 + "extra", "../" + "0" * 29]:
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(AppError) as cm:
                    files.load_cached_file(bad_id)
                self.assertEqual(cm.exception.status, 400)

    def test_file_id_uses_full_uploaded_bytes(self) -> None:
        shared_prefix = b"a" * 200_000
        first = files.extract_uploaded_file("report.txt", "text/plain", shared_prefix + b"A")
        second = files.extract_uploaded_file("report.txt", "text/plain", shared_prefix + b"B")

        self.assertNotEqual(first["fileId"], second["fileId"])
        self.assertIn("A", files.load_cached_file(str(first["fileId"]))["chunks"][-1]["text"])
        self.assertIn("B", files.load_cached_file(str(second["fileId"]))["chunks"][-1]["text"])
        _, first_source = cached_file_source(str(first["fileId"]))
        self.assertEqual(first_source.read_bytes(), shared_prefix + b"A")
        self.assertTrue(first["sourceAvailable"])

    def test_pdf_page_count_is_cached_for_reader_preview(self) -> None:
        data = b"%PDF-1.7\n1 0 obj<</Type /Pages>>endobj\n2 0 obj<</Type /Page>>endobj\n3 0 obj<</Type /Page>>endobj"
        page_count = files.count_pdf_pages(data)
        file_id = files.cache_file_chunks(
            "two-pages.pdf",
            "application/pdf",
            len(data),
            "pdf",
            "page one\npage two",
            files.chunk_text("page one\npage two"),
            source_bytes=data,
            page_count=page_count,
        )

        self.assertEqual(page_count, 2)
        self.assertEqual(files.load_cached_file(file_id)["pageCount"], 2)
        self.assertEqual(file_reader_window(file_id)["file"]["pageCount"], 2)

    def test_pdf_page_count_handles_compressed_object_streams(self) -> None:
        # 使用对象流/压缩 xref 的 PDF，其 `/Type /Page` 不在原始字节里，仅靠字节正则
        # 会漏数（退化成 1 页）。count_pdf_pages 必须用真实解析器拿到正确总页数。
        import fitz

        document = fitz.open()
        for index in range(5):
            page = document.new_page()
            page.insert_text((72, 72), f"Page {index + 1} content")
        try:
            data = document.tobytes(garbage=4, deflate=True, use_objstms=1)
        except TypeError:  # 旧版 PyMuPDF 无 use_objstms 参数
            data = document.tobytes(garbage=4, deflate=True)
        document.close()
        # 前提：原始字节正则确实看不到 /Type /Page（否则这个回归用例没意义）
        import re

        self.assertEqual(len(re.findall(rb"/Type\s*/Page\b", data)), 0)
        self.assertEqual(files.count_pdf_pages(data), 5)

    def test_file_page_text_returns_cached_page_text(self) -> None:
        chunks = files.chunk_text("first page\nsecond page")
        file_id = files.cache_file_chunks(
            "two-pages.pdf",
            "application/pdf",
            20,
            "pdf",
            "first page\nsecond page",
            chunks,
            source_bytes=b"%PDF",
            page_count=2,
            page_texts=[{"page": 1, "text": "first page"}, {"page": 2, "text": "second page"}],
        )

        payload = file_page_text(file_id, page=2)

        self.assertEqual(payload["page"]["index"], 2)
        self.assertEqual(payload["page"]["pageCount"], 2)
        self.assertEqual(payload["page"]["text"], "second page")
        self.assertTrue(payload["page"]["hasText"])

    def test_file_page_text_falls_back_to_cached_chunks(self) -> None:
        file_id = "b" * 32
        chunks = files.chunk_text("alpha beta gamma")
        (self.cache_dir / f"{file_id}.json").write_text(
            json.dumps(
                {
                    "id": file_id,
                    "name": "text.pdf",
                    "type": "application/pdf",
                    "size": 123,
                    "kind": "pdf",
                    "pageCount": 1,
                    "charCount": 16,
                    "chunkCount": len(chunks),
                    "chunks": chunks,
                }
            ),
            encoding="utf-8",
        )

        payload = file_page_text(file_id, page=1)

        self.assertIn("alpha beta gamma", payload["page"]["text"])

    def test_file_page_image_renders_pdf_page_and_caches_png(self) -> None:
        file_id = files.cache_file_chunks(
            "two-pages.pdf",
            "application/pdf",
            4,
            "pdf",
            "page one\npage two",
            files.chunk_text("page one\npage two"),
            source_bytes=b"%PDF",
            page_count=2,
        )
        png = b"\x89PNG\r\n\x1a\nrendered"

        with patch.object(files, "render_pdf_page_png", return_value=(png, 2, 2)) as render:
            _, first_data, first_page, first_page_count = file_page_image(file_id, page=2, scale=1.4)
            _, second_data, second_page, second_page_count = file_page_image(file_id, page=2, scale=1.4)

        self.assertEqual(first_data, png)
        self.assertEqual(second_data, png)
        self.assertEqual(first_page, 2)
        self.assertEqual(second_page, 2)
        self.assertEqual(first_page_count, 2)
        self.assertEqual(second_page_count, 2)
        render.assert_called_once()
        self.assertTrue((self.cache_dir / f"{file_id}.page-2-140.png").exists())

    def test_file_page_layout_returns_pdf_word_coordinates(self) -> None:
        file_id = files.cache_file_chunks(
            "layout.pdf",
            "application/pdf",
            4,
            "pdf",
            "hello layout",
            files.chunk_text("hello layout"),
            source_bytes=b"%PDF",
            page_count=3,
        )
        layout = {
            "index": 2,
            "pageCount": 3,
            "width": 200,
            "height": 100,
            "text": "hello layout",
            "hasText": True,
            "words": [{"text": "hello", "left": 10, "top": 20, "width": 12, "height": 5}],
        }

        with patch.object(files, "render_pdf_page_layout", return_value=layout) as render:
            payload = file_page_layout(file_id, page=2)

        self.assertEqual(payload["page"]["index"], 2)
        self.assertEqual(payload["page"]["pageCount"], 3)
        self.assertEqual(payload["page"]["words"][0]["text"], "hello")
        render.assert_called_once_with(b"%PDF", 2)

    def test_file_page_search_returns_page_matches(self) -> None:
        file_id = files.cache_file_chunks(
            "search.pdf",
            "application/pdf",
            20,
            "pdf",
            "alpha beta\nsecond beta",
            files.chunk_text("alpha beta\nsecond beta"),
            source_bytes=b"%PDF",
            page_count=2,
            page_texts=[{"page": 1, "text": "alpha beta"}, {"page": 2, "text": "second beta"}],
        )

        payload = file_page_search(file_id, query="beta")

        self.assertEqual(payload["query"], "beta")
        self.assertEqual(len(payload["matches"]), 2)
        self.assertEqual(payload["matches"][0]["page"], 1)
        self.assertEqual(payload["matches"][1]["page"], 2)
        self.assertIn("beta", payload["matches"][0]["snippet"])

    def test_file_reader_window_returns_bounded_document_chunks(self) -> None:
        file_id = "d" * 32
        chunks = [
            {"index": index, "start": index * 10, "end": index * 10 + 9, "lineStart": index + 1, "lineEnd": index + 1, "text": f"chunk {index}"}
            for index in range(20)
        ]
        (self.cache_dir / f"{file_id}.json").write_text(
            json.dumps(
                {
                    "id": file_id,
                    "name": "long.txt",
                    "type": "text/plain",
                    "size": 123,
                    "kind": "text",
                    "charCount": 150,
                    "chunkCount": len(chunks),
                    "chunks": chunks,
                }
            ),
            encoding="utf-8",
        )

        window = file_reader_window(file_id, chunk_start=5, chunk_count=99)

        self.assertEqual(window["file"]["name"], "long.txt")
        self.assertEqual(window["window"]["chunkStart"], 5)
        self.assertEqual(window["window"]["chunkCount"], files.FILE_READER_MAX_CHUNKS)
        self.assertTrue(window["window"]["hasNext"])
        self.assertTrue(window["window"]["hasPrevious"])
        self.assertEqual(window["chunks"][0]["index"], 5)
        self.assertEqual(window["chunks"][0]["text"], "chunk 4")
        self.assertNotIn("vector", window["chunks"][0])

    def test_file_reader_window_rejects_invalid_start(self) -> None:
        file_id = "e" * 32
        self.write_cached_file(file_id, "empty.txt")

        with self.assertRaises(AppError) as cm:
            file_reader_window(file_id, chunk_start="bad")

        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(cm.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_cleanup_keeps_recent_files_within_budget(self) -> None:
        paths = [self.write_sized_cache_file(index, size=100, mtime_offset=-index) for index in range(5)]

        with patch.object(files, "FILE_CACHE_MAX_BYTES", 500):
            files.cleanup_file_cache()

        self.assertEqual(set(paths), set(self.cache_dir.glob("*.json")))

    def test_cleanup_evicts_oldest_when_over_budget(self) -> None:
        paths = [self.write_sized_cache_file(index, size=100, mtime_offset=-index) for index in range(6)]

        with patch.object(files, "FILE_CACHE_MAX_BYTES", 500):
            files.cleanup_file_cache()

        remaining = set(self.cache_dir.glob("*.json"))
        self.assertEqual(len(remaining), 5)
        self.assertNotIn(paths[-1], remaining)

    def test_cleanup_mixed_age_and_size_keeps_newest_budget(self) -> None:
        paths = [self.write_sized_cache_file(index, size=100, mtime_offset=-index) for index in range(6)]
        expired = int(os.path.getmtime(paths[-1])) - (files.FILE_CACHE_MAX_AGE_DAYS + 1) * 86400
        os.utime(paths[-1], (expired, expired))

        with patch.object(files, "FILE_CACHE_MAX_BYTES", 250):
            files.cleanup_file_cache()

        remaining = set(self.cache_dir.glob("*.json"))
        self.assertEqual(remaining, set(paths[:2]))

    def test_extract_docx_rejects_oversized_zip_entry(self) -> None:
        data = make_zip({"word/document.xml": b"x" * 20})

        with patch.object(files, "MAX_ZIP_ENTRY_BYTES", 10):
            with self.assertRaises(AppError) as cm:
                files.extract_docx_text(data)

        self.assertEqual(cm.exception.code, ErrorCode.UPLOAD_TOO_LARGE)
        self.assertEqual(cm.exception.status, 413)

    def test_extract_docx_rejects_oversized_zip_total(self) -> None:
        data = make_zip({"word/document.xml": b"x" * 10, "word/header1.xml": b"y" * 10})

        with patch.object(files, "MAX_ZIP_ENTRY_BYTES", 100), patch.object(files, "MAX_ZIP_TOTAL_BYTES", 15):
            with self.assertRaises(AppError) as cm:
                files.extract_docx_text(data)

        self.assertEqual(cm.exception.code, ErrorCode.UPLOAD_TOO_LARGE)
        self.assertEqual(cm.exception.status, 413)

    def test_extract_docx_rejects_unsafe_zip_compression_ratio(self) -> None:
        data = make_zip({"word/document.xml": b"x" * 1000})

        with patch.object(files, "MAX_ZIP_COMPRESSION_RATIO", 2):
            with self.assertRaises(AppError) as cm:
                files.extract_docx_text(data)

        self.assertEqual(cm.exception.code, ErrorCode.UPLOAD_TOO_LARGE)
        self.assertEqual(cm.exception.status, 413)

    def test_extract_docx_rejects_xml_entities(self) -> None:
        document = b"""<?xml version="1.0"?>
<!DOCTYPE foo [<!ENTITY boom "entity text">]>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>&boom;</w:t></w:r></w:p></w:body>
</w:document>"""
        data = make_zip({"word/document.xml": document})

        with self.assertRaises(AppError) as cm:
            files.extract_docx_text(data)

        self.assertEqual(cm.exception.status, 422)

    def test_extract_pdf_text_uses_native_selectable_text(self) -> None:
        with patch("builtins.__import__", return_value=fake_pdf_module("native text")):
            text = files.extract_pdf_text(b"%PDF")

        self.assertIn("native text", text)

    def test_extract_pdf_text_falls_back_to_pypdf2_after_parse_error(self) -> None:
        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name == "pypdf":
                return broken_pdf_module()
            if name == "PyPDF2":
                return fake_pdf_module("fallback text")
            raise ModuleNotFoundError(name)

        with patch("builtins.__import__", side_effect=fake_import):
            text = files.extract_pdf_text(b"%PDF")

        self.assertIn("fallback text", text)

    def test_extract_pdf_text_reports_parse_error_when_all_readers_fail(self) -> None:
        def fake_import(name: str, *args: object, **kwargs: object) -> object:
            if name in {"pypdf", "PyPDF2"}:
                return broken_pdf_module()
            raise ModuleNotFoundError(name)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaises(AppError) as cm:
                files.extract_pdf_text(b"%PDF")

        self.assertEqual(cm.exception.status, 422)
        self.assertEqual(cm.exception.code, ErrorCode.INVALID_PAYLOAD)
        self.assertIn("Could not extract text", str(cm.exception))

    def test_extract_pdf_text_requires_ocr_for_scanned_pdf(self) -> None:
        with patch("builtins.__import__", return_value=fake_pdf_module("")):
            with self.assertRaises(AppError) as cm:
                files.extract_pdf_text(b"%PDF", ocr_enabled=False)

        self.assertEqual(cm.exception.code, ErrorCode.OCR_REQUIRED)
        self.assertEqual(cm.exception.status, 422)

    def test_extract_pdf_text_reports_unavailable_ocr_engine(self) -> None:
        with (
            patch("builtins.__import__", return_value=fake_pdf_module("")),
            patch.object(
                files,
                "extract_pdf_ocr",
                side_effect=AppError("No OCR engine", code=ErrorCode.OCR_UNAVAILABLE, status=415),
            ),
        ):
            with self.assertRaises(AppError) as cm:
                files.extract_pdf_text(b"%PDF", ocr_enabled=True)

        self.assertEqual(cm.exception.code, ErrorCode.OCR_UNAVAILABLE)
        self.assertEqual(cm.exception.status, 415)

    def test_extract_uploaded_pdf_caches_fake_ocr_text(self) -> None:
        with (
            patch("builtins.__import__", return_value=fake_pdf_module("")),
            patch.object(files, "extract_pdf_ocr", return_value="ocr text") as extract_pdf_ocr,
        ):
            extracted = files.extract_uploaded_file(
                "scan.pdf",
                "application/pdf",
                b"%PDF",
                ocr_enabled=True,
                ocr_api_key="sk-upload",
            )

        self.assertEqual(extracted["kind"], "pdf")
        self.assertEqual(extracted["charCount"], len("ocr text"))
        self.assertTrue((self.cache_dir / f"{extracted['fileId']}.json").exists())
        extract_pdf_ocr.assert_called_once_with(b"%PDF", api_key="sk-upload")

    def test_extract_uploaded_image_requires_ocr(self) -> None:
        with self.assertRaises(AppError) as cm:
            files.extract_uploaded_file("photo.png", "image/png", b"\x89PNG", ocr_enabled=False)

        self.assertEqual(cm.exception.code, ErrorCode.OCR_REQUIRED)
        self.assertEqual(cm.exception.status, 415)

    def test_extract_uploaded_image_caches_fake_ocr_text(self) -> None:
        with patch.object(files, "extract_image_ocr", return_value="图片文字") as extract_image_ocr:
            extracted = files.extract_uploaded_file(
                "photo.png",
                "image/png",
                b"\x89PNG",
                ocr_enabled=True,
                ocr_api_key="sk-upload",
            )

        self.assertEqual(extracted["kind"], "image")
        self.assertEqual(extracted["charCount"], len("图片文字"))
        self.assertTrue((self.cache_dir / f"{extracted['fileId']}.json").exists())
        extract_image_ocr.assert_called_once_with(b"\x89PNG", api_key="sk-upload")

    def test_extract_html_strips_scripts_and_keeps_visible_text(self) -> None:
        extracted = files.extract_uploaded_file(
            "saved.html",
            "text/html",
            b"<html><body><h1>Title</h1><script>secret()</script><p>Hello&nbsp;world</p></body></html>",
        )

        self.assertEqual(extracted["kind"], "html")
        self.assertIn("Title", extracted["preview"])
        self.assertIn("Hello", extracted["preview"])
        self.assertNotIn("secret", extracted["preview"])

    def test_extract_epub_reads_html_chapters(self) -> None:
        data = make_zip(
            {
                "OPS/nav.xhtml": b"<html><body>navigation</body></html>",
                "OPS/chapter1.xhtml": b"<html><body><h1>Chapter</h1><p>Vector retrieval notes</p></body></html>",
            }
        )

        extracted = files.extract_uploaded_file("book.epub", "application/epub+zip", data)

        self.assertEqual(extracted["kind"], "epub")
        self.assertIn("Chapter", extracted["preview"])
        self.assertNotIn("navigation", extracted["preview"])

    def test_extract_pptx_reads_slide_text(self) -> None:
        data = make_zip(
            {
                "ppt/slides/slide1.xml": (
                    b'<p:sld xmlns:p="urn:p" xmlns:a="urn:a">'
                    b"<a:t>Slide title</a:t><a:t>Bullet one</a:t>"
                    b"</p:sld>"
                )
            }
        )

        extracted = files.extract_uploaded_file(
            "deck.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            data,
        )

        self.assertEqual(extracted["kind"], "pptx")
        self.assertIn("Slide title", extracted["preview"])
        self.assertIn("Bullet one", extracted["preview"])

    def test_project_cache_uses_project_directory_and_attachment_citations(self) -> None:
        project_dir = self.cache_dir.parent / ".projects"
        with patch.object(files, "PROJECTS_DIR", project_dir):
            extracted = files.extract_uploaded_file("notes.txt", "text/plain", b"alpha citation text", project_id="proj-test")
            cached = files.load_cached_file(str(extracted["fileId"]), project_id="proj-test")
            context = files.build_attachment_context(
                [
                    {
                        "fileId": extracted["fileId"],
                        "projectId": "proj-test",
                        "name": "notes.txt",
                        "kind": "text",
                    }
                ],
                "citation",
            )

        self.assertEqual(cached["name"], "notes.txt")
        self.assertFalse((self.cache_dir / f"{extracted['fileId']}.json").exists())
        self.assertTrue((project_dir / "proj-test" / "files" / f"{extracted['fileId']}.json").exists())
        self.assertIn("[^F1-2]", context)
        self.assertIn("引用ID F1-1", context)

    def test_chunks_include_local_vectors(self) -> None:
        chunks = chunk_text("database transaction isolation\n" * 700)

        self.assertTrue(chunks)
        self.assertIsInstance(chunks[0].get("vector"), list)
        self.assertEqual(len(chunks[0]["vector"]), files.VECTOR_DIMENSIONS)
        selected = select_file_chunk_indices(chunks, "transaction isolation", char_budget=2000)
        self.assertTrue(selected)

    def write_sized_cache_file(self, index: int, *, size: int, mtime_offset: int) -> Path:
        path = self.cache_dir / f"{index:032x}.json"
        path.write_bytes(b"x" * size)
        timestamp = int(os.path.getmtime(path)) + mtime_offset
        os.utime(path, (timestamp, timestamp))
        return path


def fake_pdf_module(text: str) -> object:
    class FakePage:
        def extract_text(self) -> str:
            return text

    class FakeReader:
        def __init__(self, data: object) -> None:
            self.pages = [FakePage()]

    return SimpleNamespace(PdfReader=FakeReader)


def broken_pdf_module() -> object:
    class FakeReader:
        def __init__(self, data: object) -> None:
            raise ValueError("broken pdf")

    return SimpleNamespace(PdfReader=FakeReader)


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()


