from __future__ import annotations

from pathlib import Path

import pytest

from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.media import processors


def test_unsupported_media_type_and_image_ocr_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError, match="Unsupported media type"):
        processors.extract_segments({"type": "unknown"})

    image = tmp_path / "image.png"
    image.write_bytes(b"image")
    monkeypatch.setattr(processors.library, "media_file_path", lambda media: image)
    monkeypatch.setattr(processors.rag_files, "extract_image_text", lambda *args, **kwargs: (_ for _ in ()).throw(AppError("ocr failed")))
    with pytest.raises(AppError, match="ocr failed"):
        processors.image_segments({"title": "scan"}, ocr_enabled=True)

    segments = processors.image_segments({"title": "scan", "metadata": {"ocrText": "existing", "caption": "existing"}}, ocr_enabled=True)
    assert segments == [{"type": "ocr_text", "text": "existing", "page": 1, "confidence": 1.0}]
    assert processors.image_segments({"title": "scan", "metadata": {}})[0]["type"] == "caption"


def test_pdf_native_ocr_fallback_text_and_corrupt_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"%PDF broken")
    monkeypatch.setattr(processors.library, "media_file_path", lambda media: pdf)
    monkeypatch.setattr(processors.rag_files, "extract_pdf_page_texts_native", lambda data: (_ for _ in ()).throw(AppError("corrupt")))
    monkeypatch.setattr(processors.rag_files, "extract_pdf_text", lambda data, **kwargs: "fallback text")
    monkeypatch.setattr(processors.rag_files, "count_pdf_pages", lambda data: 2)
    monkeypatch.setattr(
        processors.rag_files,
        "fallback_page_texts_from_text",
        lambda text, page_count: [{"page": index + 1, "text": text if index == 0 else ""} for index in range(page_count)],
    )

    segments = processors.pdf_segments({"metadata": {}}, ocr_enabled=True)
    assert segments == [{"type": "page_text", "text": "fallback text", "page": 1, "confidence": 1.0}]
    with pytest.raises(AppError, match="corrupt"):
        processors.pdf_segments({"metadata": {}}, ocr_enabled=False)

    monkeypatch.setattr(processors.library, "media_file_path", lambda media: tmp_path / "missing.pdf")
    assert processors.pdf_segments({"metadata": {"text": "metadata text", "pageCount": 1}})[0]["text"] == "metadata text"
    assert processors._page_texts_from_metadata({"pageTexts": "not-json"}) == []
    assert processors._page_texts_from_metadata({"pages": {"bad": True}}) == []
    assert processors._page_texts_from_metadata({"pages": [" first ", {"page": 4, "text": " fourth "}, ""]}) == [
        {"page": 1, "text": "first"},
        {"page": 4, "text": "fourth"},
    ]


def test_webpage_empty_source_and_html_extraction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "missing.html"
    monkeypatch.setattr(processors.library, "media_file_path", lambda media: missing)
    assert processors.webpage_segments({"metadata": {}}) == []

    page = tmp_path / "page.html"
    page.write_bytes(b"<h1>Title</h1>")
    monkeypatch.setattr(processors.library, "media_file_path", lambda media: page)
    monkeypatch.setattr(processors.rag_files, "extract_html_text", lambda data: "Title")
    assert processors.webpage_segments({"metadata": {}})[0]["text"] == "Title"


def test_imported_transcripts_sort_ignore_empty_and_preserve_confidence() -> None:
    raw = [
        {"text": "later", "startSec": "8", "endSec": "9", "confidence": 0.5},
        " plain text ",
        {"transcript": "earlier", "timeRange": [1, 2]},
        {"text": ""},
    ]
    result = processors.imported_transcript_segments(raw)

    assert [item["text"] for item in result] == ["earlier", "later", "plain text"]
    assert result[1]["confidence"] == 0.5
    assert [item["index"] for item in result] == [0, 1, 2]
    assert processors.imported_transcript_segments("bad") == []
    assert processors._segment_sort_key({"timeRange": ["bad"], "_sourceIndex": 2}) == (float("inf"), 2)
    assert processors.metadata_float({"duration": "bad"}, "duration") == 0.0
    assert processors.metadata_float({"duration": -2}, "duration") == 0.0


def test_audio_video_frames_mixed_metadata_and_no_text() -> None:
    assert processors.audio_segments({"metadata": {}}) == []
    audio = processors.audio_segments({"metadata": {"transcript": "one two", "durationSec": 4}})
    assert audio[0]["timeRange"] == [0.0, 4.0]

    video = processors.video_segments(
        {
            "metadata": {
                "transcript": "spoken",
                "frames": [
                    " frame caption ",
                    {"caption": "timed", "startSec": 3, "endSec": 4, "framePath": "frames/3.png"},
                    {"caption": ""},
                ],
            }
        }
    )
    assert [item["type"] for item in video] == ["transcript", "frame", "frame"]
    assert video[1]["text"] == "timed"
    assert video[1]["framePath"] == "frames/3.png"
    assert processors.video_segments({"metadata": {"frames": "bad"}}) == []


def test_transcript_chunking_long_words_blank_and_small_limits() -> None:
    assert processors.transcript_segments("", media_type="audio") == []
    assert processors.chunk_transcript_text("\r\n  \n") == []
    long_word = "x" * (processors.TRANSCRIPT_CHUNK_CHARS + 10)
    pieces = processors._transcript_pieces(f"short paragraph\n{long_word} tail")
    assert pieces == ["short paragraph", long_word, "tail"]
    assert processors.chunk_transcript_text("one two three four", max_chars=7) == ["one two", "three", "four"]
    assert processors._metadata({"metadata": "bad"}) == {}
