"""Targeted test coverage boosters for OCR and formula extraction helpers."""

from __future__ import annotations

from pathlib import Path

from deepseek_infra.infra.tool_runtime import ocr


def test_ocr_utility_helpers(tmp_settings: Path) -> None:
    # 1. Mode normalization
    assert ocr._normalize_ocr_mode("fast") == "fast"
    assert ocr._normalize_ocr_mode("QUALITY") == "quality"
    assert ocr._normalize_ocr_mode("unknown") == ocr.OCR_DEFAULT_MODE

    # 2. Language selection
    assert "chi_sim" in ocr._select_lang({"chi_sim", "eng"})
    assert ocr._select_lang({"eng"}) == "eng"
    assert ocr._select_lang({"fra"}) == "fra"
    assert ocr._select_lang(set()) == "eng"

    # 3. Tesseract configs
    cfg_fast = ocr._tesseract_configs_for_mode("fast")
    assert len(cfg_fast) >= 1
    cfg_qual = ocr._tesseract_configs_for_mode("quality")
    assert len(cfg_qual) >= 1

    # 4. Image type detection & Data URL
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00"
    jpg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF"
    gif_bytes = b"GIF89a"
    bmp_bytes = b"BM"
    unknown_bytes = b"random_binary_data"

    assert ocr._image_suffix(png_bytes) == ".png"
    assert ocr._image_suffix(jpg_bytes) == ".jpg"
    assert ocr._image_suffix(gif_bytes) == ".gif"
    assert ocr._image_suffix(bmp_bytes) == ".bmp"
    assert ocr._image_suffix(unknown_bytes) == ".img"

    assert ocr._image_media_type(png_bytes) == "image/png"
    assert ocr._image_media_type(jpg_bytes) == "image/jpeg"
    assert ocr._image_media_type(gif_bytes) == "image/gif"
    assert ocr._image_media_type(bmp_bytes) == "image/bmp"

    data_url = ocr._image_data_url(png_bytes)
    assert data_url.startswith("data:image/png;base64,")

    # 5. Text normalization & scoring
    norm_text = ocr.normalize_ocr_text("Hello   World  \n\n\n\nTest\r\n")
    assert "Test" in norm_text

    assert ocr._math_symbol_count("E = mc^2") > 0
    assert ocr._looks_like_formula_line(r"f(x) = \int_0^1 x dx") is True
    assert ocr._looks_like_formula_line("This is normal English text.") is False

    assert ocr._looks_like_ocr_noise("...") is True
    assert ocr._looks_like_ocr_noise("??????????????") is True
    assert ocr._looks_like_ocr_noise("Valid text") is False

    assert ocr._ocr_text_score("A complete sentence with many words.") > ocr._ocr_text_score("noise")

    # 6. Formula helpers
    assert ocr._strip_matching_quotes('"test"') == "test"
    assert ocr._strip_matching_quotes("'test'") == "test"
    assert ocr._strip_matching_quotes("test") == "test"

    assert ocr._formula_output_from_json({"text": "a+b=c"}) == "a+b=c"
    assert ocr._formula_output_from_json({"formula": "x^2"}) == "x^2"
    assert ocr._formula_output_from_json({"result": "y=mx+b"}) == "y=mx+b"

    assert ocr._formula_ocr_output_is_credible(r"\int_0^\infty e^{-x} dx") is True
    assert ocr._formula_ocr_output_is_credible("a") is False

    clean_formula = ocr._clean_formula_ocr_output("```latex\nx + y = z\n```")
    assert clean_formula == "x + y = z"

    # 7. DeepSeek OCR body payload
    body = ocr._deepseek_ocr_body(data_url)
    assert body.get("model") == ocr.DEEPSEEK_OCR_MODEL
    messages = body.get("messages")
    assert isinstance(messages, list)
    assert len(messages) > 0
