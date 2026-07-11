from __future__ import annotations

import io
import sys
import types
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.rag import files


class _Rect:
    width = 200.0
    height = 100.0


class _PdfPage:
    rect = _Rect()

    def __init__(self, words: list[Any] | None = None) -> None:
        self.words = words or []

    def get_text(self, _: str) -> list[Any]:
        return self.words

    def get_pixmap(self, *, matrix: Any, alpha: bool) -> Any:
        assert matrix
        assert alpha is False
        return types.SimpleNamespace(tobytes=lambda _: b"png")


class _Document:
    def __init__(self, page_count: int, words: list[Any] | None = None) -> None:
        self.page_count = page_count
        self.words = words
        self.closed = False

    def __len__(self) -> int:
        return self.page_count

    def load_page(self, index: int) -> _PdfPage:
        assert 0 <= index < self.page_count
        return _PdfPage(self.words)

    def close(self) -> None:
        self.closed = True


def _fitz(document: _Document) -> types.ModuleType:
    module = types.ModuleType("fitz")
    module.open = lambda **_: document  # type: ignore[attr-defined]
    module.Matrix = lambda x, y: (x, y)  # type: ignore[attr-defined]
    return module


def test_pdf_layout_normalizes_coordinates_and_skips_corrupt_words() -> None:
    document = _Document(
        2,
        [
            (10, 5, 50, 20, " Hello ", 0, 0, 0),
            (60, 5, 250, 20, "world", 0, 0, 1),
            ("bad",),
            (1, 1, 2, 2, "", 1, 1, 0),
        ],
    )
    with patch.dict(sys.modules, {"fitz": _fitz(document)}):
        layout = files._render_pdf_page_layout_pymupdf(b"pdf", 99)
    assert layout["index"] == 2
    assert layout["text"] == "Hello world"
    assert layout["words"][1]["width"] == 70.0
    assert document.closed


def test_pdf_layout_rejects_empty_document_and_wraps_backend_crash() -> None:
    document = _Document(0)
    with patch.dict(sys.modules, {"fitz": _fitz(document)}), pytest.raises(AppError) as caught:
        files._render_pdf_page_layout_pymupdf(b"pdf", 1)
    assert caught.value.code == ErrorCode.UNSUPPORTED_FILE
    assert document.closed

    with patch.object(files, "_render_pdf_page_layout_pymupdf", side_effect=RuntimeError("renderer crashed")), pytest.raises(AppError):
        files.render_pdf_page_layout(b"pdf", 1)


def test_pdf_png_pymupdf_clamps_page_and_closes_document() -> None:
    document = _Document(3)
    with patch.dict(sys.modules, {"fitz": _fitz(document)}):
        png, page, count = files._render_pdf_page_png_pymupdf(b"pdf", -5, 1.5)
    assert (png, page, count) == (b"png", 1, 3)
    assert document.closed


def test_pdf_png_uses_second_backend_and_reports_total_failure() -> None:
    with (
        patch.object(files, "_render_pdf_page_png_pymupdf", side_effect=RuntimeError("missing fitz")),
        patch.object(files, "_render_pdf_page_png_pdf2image", return_value=(b"fallback", 2, 0)),
    ):
        assert files.render_pdf_page_png(b"pdf", 2, 1.0) == (b"fallback", 2, 0)

    with (
        patch.object(files, "_render_pdf_page_png_pymupdf", side_effect=RuntimeError("missing fitz")),
        patch.object(files, "_render_pdf_page_png_pdf2image", side_effect=RuntimeError("missing poppler")),
        pytest.raises(AppError, match="missing poppler"),
    ):
        files.render_pdf_page_png(b"pdf", 1, 1.0)


def test_pdf2image_empty_and_success_paths() -> None:
    module = types.ModuleType("pdf2image")
    module.convert_from_bytes = lambda *_args, **_kwargs: []  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"pdf2image": module}), pytest.raises(AppError):
        files._render_pdf_page_png_pdf2image(b"pdf", 0, 100)

    image = MagicMock()

    def save(output: io.BytesIO, *, format: str) -> None:
        assert format == "PNG"
        output.write(b"encoded")

    image.save.side_effect = save
    module.convert_from_bytes = lambda *_args, **_kwargs: [image]  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"pdf2image": module}):
        assert files._render_pdf_page_png_pdf2image(b"pdf", 4, 10) == (b"encoded", 4, 0)


def test_page_text_normalization_and_fallback_boundaries() -> None:
    assert files.normalized_page_texts("bad") == []
    assert files.normalized_page_texts([None, {"page": "bad", "text": "x"}, {"page": 0, "text": "x"}, {"page": 2, "text": "  hello  "}]) == [
        {"page": 2, "text": "hello"}
    ]
    assert files.page_text_for_index([{"page": 2, "text": "hello"}], 1) == ""
    assert files.page_text_from_cached_chunks({}, requested_page=1, page_count=1) == ""
    assert files.page_text_from_cached_chunks({"chunks": [{"text": "abcdefgh"}]}, requested_page=2, page_count=2) == "efgh"


def test_xlsx_sheet_reader_handles_inline_shared_and_invalid_indices() -> None:
    xml = b'''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
      <row r="1"><c r="A1" t="inlineStr"><is><t>inline</t></is></c><c r="B1" t="s"><v>0</v></c></row>
      <row><c t="s"><v>99</v></c><c><v>42</v></c></row>
    </sheetData></worksheet>'''
    text = files.read_xlsx_sheet(xml, ["shared"])
    assert "A1=inline" in text
    assert "B1=shared" in text
    assert "99" in text and "42" in text


def test_xlsx_sheet_entry_fallback_when_relationship_files_are_missing() -> None:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("xl/worksheets/sheet2.xml", "<x/>")
        archive.writestr("xl/worksheets/sheet1.xml", "<x/>")
    with zipfile.ZipFile(io.BytesIO(output.getvalue())) as archive:
        assert files.read_xlsx_sheet_entries(archive) == [
            ("Sheet 1", "xl/worksheets/sheet1.xml"),
            ("Sheet 2", "xl/worksheets/sheet2.xml"),
        ]


def test_cleanup_file_cache_tolerates_directory_and_file_io_errors(tmp_settings: Path) -> None:
    files.FILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with patch.object(Path, "glob", side_effect=OSError("denied")):
        files.cleanup_file_cache()

    cached = files.FILE_CACHE_DIR / ("a" * 32 + ".json")
    cached.write_text("{}", encoding="utf-8")
    original_stat = Path.stat

    def selective_stat(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == cached:
            raise OSError("gone")
        return original_stat(path, *args, **kwargs)

    with patch.object(Path, "stat", selective_stat):
        files.cleanup_file_cache()
