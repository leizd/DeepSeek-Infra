from __future__ import annotations

import json
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.tool_runtime import ocr, ocr_trace
from deepseek_infra.infra.tool_runtime.ocr_trace import OcrTrace


class IllegalStateException(Exception):
    """Stand-in for the JVM exception Chaquopy surfaces from the OCR bridge."""


class IllegalArgumentException(Exception):
    """Stand-in for the JVM exception Chaquopy surfaces from the OCR bridge."""


def _android_engine(monkeypatch: pytest.MonkeyPatch, bridge: Any) -> ocr.AndroidMlKitEngine:
    java_module = ModuleType("java")
    setattr(java_module, "jclass", lambda name: bridge)
    monkeypatch.setitem(sys.modules, "java", java_module)
    return ocr.AndroidMlKitEngine()


def test_sanitize_correlation_id_keeps_log_safe_values_only() -> None:
    assert ocr_trace.sanitize_correlation_id("Abc-123_xyz.4:1") == "Abc-123_xyz.4:1"
    assert ocr_trace.sanitize_correlation_id("  abc123  ") == "abc123"
    assert ocr_trace.sanitize_correlation_id("") == ""
    assert ocr_trace.sanitize_correlation_id(None) == ""
    assert ocr_trace.sanitize_correlation_id("bad id") == ""
    assert ocr_trace.sanitize_correlation_id("bad\nid") == ""
    assert ocr_trace.sanitize_correlation_id('"quoted"') == ""
    assert ocr_trace.sanitize_correlation_id("a" * (ocr_trace.MAX_CORRELATION_ID_CHARS + 1)) == ""
    assert len(ocr_trace.sanitize_correlation_id("a" * ocr_trace.MAX_CORRELATION_ID_CHARS)) == ocr_trace.MAX_CORRELATION_ID_CHARS


def test_derive_correlation_id_stays_bounded_and_replaces_unusable_bases() -> None:
    base = "a" * ocr_trace.MAX_CORRELATION_ID_CHARS
    derived = ocr_trace.derive_ocr_correlation_id(base, 12)
    assert len(derived) <= ocr_trace.MAX_CORRELATION_ID_CHARS
    assert derived.endswith(".12")
    assert ocr_trace.sanitize_correlation_id(derived) == derived

    assert ocr_trace.derive_ocr_correlation_id(base, 0).endswith(".1")
    fallback = ocr_trace.derive_ocr_correlation_id("not safe!", 3)
    assert ocr_trace.sanitize_correlation_id(fallback) == fallback
    assert "." not in fallback


def test_ocr_trace_always_exposes_a_usable_correlation_id() -> None:
    assert ocr_trace.sanitize_correlation_id(OcrTrace().correlation_id)
    assert ocr_trace.sanitize_correlation_id(OcrTrace(correlation_id="bad id").correlation_id)
    assert OcrTrace(correlation_id="given-id").correlation_id == "given-id"


def test_stage_accumulates_and_clamps_negative_durations() -> None:
    trace = OcrTrace()
    with trace.stage(ocr_trace.OCR_STAGE_SELECT):
        pass
    trace.add_stage(ocr_trace.OCR_STAGE_SELECT, 120)
    trace.add_stage(ocr_trace.OCR_STAGE_TRANSPORT, -50)

    assert trace.stages_us[ocr_trace.OCR_STAGE_SELECT] >= 120
    assert trace.stages_us[ocr_trace.OCR_STAGE_TRANSPORT] == 0
    assert trace.total_stage_us() == trace.stages_us[ocr_trace.OCR_STAGE_SELECT]
    assert ocr_trace.OCR_STAGE_NORMALIZE in trace.to_metadata()["timingsUs"]


def test_record_backend_accepts_known_keys_and_ignores_malformed_values() -> None:
    trace = OcrTrace()
    assert trace.record_backend({"decodeUs": 10, "renderUs": 20, "recognizeUs": 30, "overheadUs": 5, "pages": 2, "timeouts": 0}) is True
    assert trace.record_backend({"decodeUs": True, "renderUs": "5", "pages": True}) is False
    assert trace.record_backend(None) is False
    assert trace.record_backend("nope") is False

    trace.record_backend({"pages": 1})
    payload = trace.to_metadata()
    assert payload["backendTimingsUs"] == {"decode": 10, "render": 20, "recognize": 30, "overhead": 5}
    assert payload["pages"] == 2


def test_note_attempt_dedupes_and_details_omit_empty_fields() -> None:
    trace = OcrTrace()
    trace.note_attempt("tesseract", "empty")
    trace.note_attempt("tesseract", "empty")
    trace.note_attempt("tesseract", "ok")

    assert trace.attempts == ["tesseract:empty", "tesseract:ok"]
    assert trace.details() == {"correlationId": trace.correlation_id, "attempts": ["tesseract:empty", "tesseract:ok"]}

    trace.engine = "tesseract"
    assert trace.details()["engine"] == "tesseract"


def test_recorded_tracks_observed_ocr_work_only() -> None:
    trace = OcrTrace()
    assert trace.recorded() is False
    assert OcrTrace().to_metadata()["attempts"] == []

    trace.add_stage(ocr_trace.OCR_STAGE_SELECT, 1)
    assert trace.recorded() is True

    attempted = OcrTrace()
    attempted.note_attempt("tesseract", "ok")
    assert attempted.recorded() is True


def test_trace_metadata_carries_no_content() -> None:
    trace = OcrTrace(mode="image", input_bytes=42, engine="android-mlkit")
    trace.note_attempt("android-mlkit", "unavailable")

    payload = trace.to_metadata()
    assert set(payload) == {"correlationId", "mode", "engine", "inputBytes", "pages", "timingsUs", "totalUs", "attempts"}
    assert payload["correlationId"] == trace.correlation_id
    assert payload["inputBytes"] == 42
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "sk-" not in serialized


def test_null_trace_helpers_are_no_ops() -> None:
    with ocr_trace.ocr_stage(None, ocr_trace.OCR_STAGE_TRANSPORT):
        pass
    ocr_trace.note_attempt(None, "tesseract", "ok")
    assert ocr_trace.record_backend_timings(None, SimpleNamespace(last_backend_timings={"decodeUs": 1})) is False


def test_android_engine_maps_timeout_to_ocr_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    class Bridge:
        isAvailable = staticmethod(lambda: True)
        takeLastTimingsJson = staticmethod(
            lambda: '{"decodeUs": 3, "renderUs": 0, "recognizeUs": 60000000, "overheadUs": 1, "pages": 1, "timeouts": 1}'
        )

        @staticmethod
        def recognizeImage(data: bytes) -> str:
            raise IllegalStateException("Android OCR timed out.")

    engine = _android_engine(monkeypatch, Bridge)

    with pytest.raises(AppError) as excinfo:
        engine.extract_image(b"\x89PNG")

    assert excinfo.value.code is ErrorCode.OCR_UNAVAILABLE
    assert excinfo.value.status == 415
    assert excinfo.value.details == {"engine": "android-mlkit", "stage": "recognizeImage", "reason": "timeout"}
    assert engine.last_backend_timings["timeouts"] == 1


def test_android_engine_maps_decode_failure_and_tolerates_missing_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    class Bridge:
        isAvailable = staticmethod(lambda: True)

        @staticmethod
        def recognizeImage(data: bytes) -> str:
            raise IllegalArgumentException("Image bytes cannot be decoded.")

    engine = _android_engine(monkeypatch, Bridge)

    with pytest.raises(AppError) as excinfo:
        engine.extract_image(b"\x89PNG")

    assert excinfo.value.code is ErrorCode.OCR_UNAVAILABLE
    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "decode"
    assert excinfo.value.details["stage"] == "recognizeImage"
    assert engine.last_backend_timings == {}


def test_android_engine_maps_unknown_failure_and_ignores_malformed_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    class Bridge:
        isAvailable = staticmethod(lambda: True)
        takeLastTimingsJson = staticmethod(lambda: "not json")

        @staticmethod
        def recognizePdf(data: bytes) -> str:
            raise IllegalStateException("something else went wrong")

    engine = _android_engine(monkeypatch, Bridge)

    with pytest.raises(AppError) as excinfo:
        engine.extract(b"%PDF")

    assert excinfo.value.details is not None
    assert excinfo.value.details["reason"] == "bridge"
    assert excinfo.value.details["stage"] == "recognizePdf"
    assert engine.last_backend_timings == {}


def test_android_engine_adopts_bridge_timings_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class Bridge:
        isAvailable = staticmethod(lambda: True)
        recognizeImage = staticmethod(lambda data: " 识别文本 ")
        takeLastTimingsJson = staticmethod(
            lambda: '{"decodeUs": 7, "renderUs": 0, "recognizeUs": 9, "overheadUs": 1, "pages": 1, "timeouts": 0}'
        )

    engine = _android_engine(monkeypatch, Bridge)
    trace = OcrTrace(correlation_id="req-ok")

    with patch.object(ocr, "_ocr_engine_candidates", return_value=([engine], [])):
        text = ocr.extract_image_ocr(b"\x89PNG", trace=trace)

    assert text == "识别文本"
    assert trace.mode == "image"
    assert trace.input_bytes == 4
    assert trace.engine == "android-mlkit"
    assert trace.to_metadata()["backendTimingsUs"]["recognize"] == 9
    assert "android-mlkit:candidate" in trace.attempts


def test_extract_without_trace_keeps_error_details_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_ANDROID_APP", raising=False)

    with patch.object(ocr, "_ocr_engine_candidates", return_value=([], ["tesseract: nope"])):
        with pytest.raises(AppError) as excinfo:
            ocr.extract_image_ocr(b"\x89PNG")

    assert excinfo.value.code is ErrorCode.OCR_UNAVAILABLE
    assert excinfo.value.details is None


def test_extract_with_trace_attaches_correlation_id_when_no_engine_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_ANDROID_APP", raising=False)
    trace = OcrTrace(correlation_id="req-none")

    with patch.object(ocr, "_ocr_engine_candidates", return_value=([], ["tesseract: nope"])):
        with pytest.raises(AppError) as excinfo:
            ocr.extract_image_ocr(b"\x89PNG", trace=trace)

    assert excinfo.value.details is not None
    assert excinfo.value.details["correlationId"] == "req-none"
    assert ocr_trace.OCR_STAGE_SELECT in trace.stages_us


def test_engine_error_details_survive_the_fallback_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    class Bridge:
        isAvailable = staticmethod(lambda: True)

        @staticmethod
        def recognizeImage(data: bytes) -> str:
            raise IllegalStateException("Android OCR timed out.")

    engine = _android_engine(monkeypatch, Bridge)
    trace = OcrTrace(correlation_id="req-fail")

    with patch.object(ocr, "_ocr_engine_candidates", return_value=([engine], [])):
        with pytest.raises(AppError) as excinfo:
            ocr.extract_image_ocr(b"\x89PNG", trace=trace)

    details = excinfo.value.details
    assert details is not None
    assert details["reason"] == "timeout"
    assert details["correlationId"] == "req-fail"
    assert details["engine"] == "android-mlkit"
    assert "android-mlkit:unavailable" in details["attempts"]


def test_empty_engine_result_raises_ocr_empty_with_correlation_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_ANDROID_APP", raising=False)
    engine = SimpleNamespace(name="tesseract", extract_image=lambda data: "   ")
    trace = OcrTrace(correlation_id="req-empty")

    with patch.object(ocr, "_ocr_engine_candidates", return_value=([engine], [])):
        with pytest.raises(AppError) as excinfo:
            ocr.extract_image_ocr(b"\x89PNG", trace=trace)

    assert excinfo.value.code is ErrorCode.OCR_EMPTY
    assert excinfo.value.details is not None
    assert excinfo.value.details["correlationId"] == "req-empty"
    assert trace.attempts == ["tesseract:empty"]


def test_pdf_page_fallback_records_pages_score_and_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = SimpleNamespace(
        name="tesseract",
        _pdf2image=SimpleNamespace(convert_from_bytes=lambda data, dpi, fmt: ["page-1", "page-2"]),
        extract_page_image=lambda image: f"text {image}",
    )
    trace = OcrTrace(correlation_id="req-pdf")

    with patch.object(ocr, "_ocr_engine_candidates", return_value=([engine], [])):
        text = ocr.extract_pdf_ocr(b"%PDF", trace=trace)

    assert "[PDF 第 2 页 (OCR)]" in text
    assert trace.pages == 2
    assert trace.mode == "pdf"
    assert trace.input_bytes == 4
    assert trace.engine == "tesseract"
    assert ocr_trace.OCR_STAGE_SCORE in trace.stages_us
    assert ocr_trace.OCR_STAGE_TRANSPORT in trace.stages_us


def test_pdf_page_fallback_reports_engine_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(image: object) -> str:
        raise AppError("engine down", code=ErrorCode.OCR_UNAVAILABLE, status=415)

    engine = SimpleNamespace(
        name="tesseract",
        _pdf2image=SimpleNamespace(convert_from_bytes=lambda data, dpi, fmt: ["page-1"]),
        extract_page_image=explode,
    )
    trace = OcrTrace(correlation_id="req-pdf-fail")

    with patch.object(ocr, "_ocr_engine_candidates", return_value=([engine], [])):
        with pytest.raises(AppError) as excinfo:
            ocr.extract_pdf_ocr(b"%PDF", trace=trace)

    assert excinfo.value.code is ErrorCode.OCR_UNAVAILABLE
    assert excinfo.value.details is not None
    assert excinfo.value.details["correlationId"] == "req-pdf-fail"
    assert trace.attempts == ["tesseract:unavailable"]


def test_pdf_page_fallback_prefers_deepseek_and_keeps_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    deepseek = SimpleNamespace(
        name="deepseek-api",
        _pdf2image=SimpleNamespace(convert_from_bytes=lambda data, dpi, fmt: ["page-1"]),
        extract_page_image=lambda image: "cloud page text",
    )
    skipped = SimpleNamespace(
        name="tesseract",
        extract_page_image=lambda image: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    trace = OcrTrace(correlation_id="req-page-cloud")

    with patch.object(ocr, "_ocr_engine_candidates", return_value=([deepseek, skipped], [])):
        text = ocr.extract_pdf_ocr(b"%PDF", trace=trace)

    assert "cloud page text" in text
    assert trace.engine == "deepseek-api"
    assert trace.attempts == ["deepseek-api:ok"]


def test_pdf_page_fallback_keeps_the_best_scoring_page(monkeypatch: pytest.MonkeyPatch) -> None:
    strong = SimpleNamespace(
        name="strong",
        _pdf2image=SimpleNamespace(convert_from_bytes=lambda data, dpi, fmt: ["page-1"]),
        extract_page_image=lambda image: "a much longer readable sentence",
    )
    weak = SimpleNamespace(name="weak", extract_page_image=lambda image: "x")
    trace = OcrTrace(correlation_id="req-page-best")

    with patch.object(ocr, "_ocr_engine_candidates", return_value=([strong, weak], [])):
        text = ocr.extract_pdf_ocr(b"%PDF", trace=trace)

    assert "longer" in text
    assert trace.engine == "strong"
    assert trace.attempts == ["strong:candidate", "weak:candidate"]


def test_extract_fallback_records_engine_reported_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    def report_empty(data: bytes) -> str:
        raise AppError("no text", code=ErrorCode.OCR_EMPTY, status=422)

    engine = SimpleNamespace(name="tesseract", extract_image=report_empty)
    trace = OcrTrace(correlation_id="req-engine-empty")

    with patch.object(ocr, "_ocr_engine_candidates", return_value=([engine], [])):
        with pytest.raises(AppError) as excinfo:
            ocr.extract_image_ocr(b"\x89PNG", trace=trace)

    assert excinfo.value.code is ErrorCode.OCR_EMPTY
    assert excinfo.value.details is not None
    assert excinfo.value.details["correlationId"] == "req-engine-empty"
    assert trace.attempts == ["tesseract:empty"]


def test_extract_fallback_returns_deepseek_result_with_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    deepseek = SimpleNamespace(name="deepseek-api", extract_image=lambda data: "cloud text")
    skipped = SimpleNamespace(
        name="local",
        extract_image=lambda data: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    trace = OcrTrace(correlation_id="req-cloud")

    with patch.object(ocr, "_ocr_engine_candidates", return_value=([deepseek, skipped], [])):
        text = ocr.extract_image_ocr(b"\x89PNG", trace=trace)

    assert text == "cloud text"
    assert trace.engine == "deepseek-api"
    assert trace.attempts == ["deepseek-api:ok"]


def test_extract_fallback_keeps_the_best_scoring_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    strong = SimpleNamespace(name="strong", extract_image=lambda data: "a much longer readable sentence")
    weak = SimpleNamespace(name="weak", extract_image=lambda data: "x")
    trace = OcrTrace(correlation_id="req-best")

    with patch.object(ocr, "_ocr_engine_candidates", return_value=([strong, weak], [])):
        text = ocr.extract_image_ocr(b"\x89PNG", trace=trace)

    assert text == "a much longer readable sentence"
    assert trace.engine == "strong"
    assert trace.attempts == ["strong:candidate", "weak:candidate"]
