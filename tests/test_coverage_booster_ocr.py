"""Targeted test coverage boosters for ocr.py helpers."""

from deepseek_infra.infra.tool_runtime import ocr


def test_ocr_language_and_mode_helpers() -> None:
    assert "chi_sim" in ocr._select_lang({"chi_sim", "eng"})
    assert ocr._select_lang({"eng"}) == "eng"
    assert ocr._select_lang(set()) == "eng"

    assert ocr._normalize_ocr_mode("fast") == "fast"
    assert ocr._normalize_ocr_mode("quality") == "quality"
    assert ocr._normalize_ocr_mode("invalid_mode") == "balanced"

    assert len(ocr._tesseract_configs_for_mode("fast")) > 0
    assert len(ocr._tesseract_configs_for_mode("quality")) > 0


def test_ocr_image_and_data_helpers() -> None:
    png_header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
    jpg_header = b"\xff\xd8\xff" + b"\x00" * 10
    bmp_header = b"BM" + b"\x00" * 10
    unknown_bytes = b"random_binary_content"

    assert ocr._image_suffix(png_header) == ".png"
    assert ocr._image_suffix(jpg_header) == ".jpg"
    assert ocr._image_suffix(bmp_header) == ".bmp"
    assert ocr._image_suffix(unknown_bytes) == ".img"

    assert ocr._image_media_type(png_header) == "image/png"
    assert ocr._image_media_type(jpg_header) == "image/jpeg"
    assert ocr._image_media_type(bmp_header) == "image/bmp"

    data_url = ocr._image_data_url(png_header)
    assert data_url.startswith("data:image/png;base64,")


def test_ocr_formula_and_text_scoring() -> None:
    norm = ocr.normalize_ocr_text("Line 1\r\nLine 2\n\n\nLine 3")
    assert "Line 1" in norm

    symbols = ocr._normalize_formula_symbols("a × b ÷ c ± d")
    assert len(symbols) > 0

    assert ocr._math_symbol_count("x = y + z") >= 2
    assert ocr._looks_like_formula_line("y = mx + b") is True
    assert ocr._looks_like_formula_line("just normal text without math") is False

    assert ocr._looks_like_ocr_noise("!@#") is True
    assert ocr._looks_like_ocr_noise("!@#$%!@#$%") is True
    assert ocr._looks_like_ocr_noise("This is clean readable text.") is False

    assert ocr._ocr_text_score("This is a clean readable sentence.") > 0
    assert ocr._strip_matching_quotes('"quoted"') == "quoted"
    assert ocr._strip_matching_quotes("'quoted'") == "quoted"
    assert ocr._strip_matching_quotes("unquoted") == "unquoted"

    assert ocr._formula_output_from_json({"text": "x^2 + y^2 = r^2"}) == "x^2 + y^2 = r^2"
    assert ocr._formula_output_from_json({"latex": "\\alpha + \\beta"}) == "\\alpha + \\beta"

    assert ocr._formula_ocr_output_is_credible("E = mc^2") is True
    assert ocr._formula_ocr_output_is_credible("") is False

    cleaned = ocr._clean_formula_ocr_output("$$ E = mc^2 $$")
    assert "E = mc^2" in cleaned

    assert ocr._int_from_ocr_data("123") == 123
    assert ocr._int_from_ocr_data(None, default=5) == 5

    appended = ocr._append_formula_snippets("Base text", ["E=mc^2"])
    assert "Base text" in appended
    assert "E=mc^2" in appended
