from __future__ import annotations

import builtins
import io
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.tool_runtime import ocr


def test_tesseract_locator_and_language_fallbacks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "tesseract.exe"
    executable.write_text("stub", encoding="utf-8")
    monkeypatch.setenv("TESSERACT_CMD", str(executable))
    assert ocr._locate_tesseract() == str(executable)
    monkeypatch.setenv("TESSERACT_CMD", str(tmp_path / "missing"))
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ocr.os, "name", "posix")
    assert ocr._locate_tesseract() is None
    monkeypatch.setenv("OCR_LANG", "fra,deu")
    assert ocr._select_lang({"deu", "eng"}) == "deu"
    monkeypatch.setenv("OCR_LANG", "missing")
    assert ocr._select_lang({"zzz"}) == "zzz"
    assert ocr._select_lang(set()) == "eng"


@pytest.mark.parametrize(
    ("payload", "suffix", "media"),
    [
        (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
        (b"\xff\xd8rest", ".jpg", "image/jpeg"),
        (b"RIFFxxxxWEBPrest", ".webp", "image/webp"),
        (b"II*\x00rest", ".tif", "image/tiff"),
        (b"GIF89a", ".gif", "image/gif"),
        (b"BMrest", ".bmp", "image/bmp"),
        (b"unknown", ".img", "image/png"),
    ],
)
def test_ocr_image_signature_and_data_url(payload: bytes, suffix: str, media: str) -> None:
    assert ocr._image_suffix(payload) == suffix
    assert ocr._image_media_type(payload) == media
    assert ocr._image_data_url(payload).startswith(f"data:{media};base64,")


def test_ocr_pixel_limit_and_image_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "_ocr_max_image_pixels", lambda: 100)
    large = Image.new("P", (100, 100))
    limited = ocr._limit_pil_image_pixels(large)
    assert isinstance(limited, Image.Image)
    assert limited.size[0] * limited.size[1] <= 100
    assert ocr._limit_pil_image_pixels(object()).__class__ is object
    encoded = ocr._pil_image_png_bytes(large)
    with Image.open(io.BytesIO(encoded)) as result:
        assert result.mode in {"RGB", "RGBA", "L"}


def test_ocr_array_preprocessing_helpers_cover_failure_and_dedup() -> None:
    class Gray:
        shape = (100, 200)

    cv2 = SimpleNamespace(INTER_CUBIC=1, resize=lambda value, *_args, **_kwargs: "resized", bitwise_not=lambda value: ("inverted", value))
    assert ocr._upscale_gray_for_ocr(Gray(), cv2) == "resized"
    assert ocr._adaptive_block_size(SimpleNamespace(shape=(360, 720))) % 2 == 1
    assert ocr._white_background("pixels", cv2, SimpleNamespace(mean=lambda _value: 0)) == ("inverted", "pixels")
    assert ocr._white_background("pixels", cv2, SimpleNamespace(mean=lambda _value: 255)) == "pixels"
    assert ocr._deskew_gray(object(), object(), SimpleNamespace(column_stack=lambda _value: (_ for _ in ()).throw(RuntimeError("bad")))) is None

    class BrokenArray:
        def __getitem__(self, _key: object) -> object:
            raise RuntimeError("bad slice")

    factory = SimpleNamespace(fromarray=lambda value: ("image", value))
    one = BrokenArray()
    result = ocr._arrays_to_unique_images([one, one], factory)
    assert len(result) == 1


def test_ocr_formula_line_noise_and_credibility_edges() -> None:
    assert not ocr._looks_like_formula_line("")
    assert ocr._looks_like_formula_line(r"\frac{x}{2}")
    assert ocr._looks_like_formula_line("x_1")
    assert ocr._looks_like_formula_line("sin(x)")
    assert ocr._looks_like_ocr_noise("--")
    assert ocr._looks_like_ocr_noise("~~~~~~~~~~~~")
    assert ocr._looks_like_latex_word_noise("afffffff")
    assert ocr._looks_like_latex_word_noise("bcfffgh")
    assert not ocr._formula_ocr_output_is_credible("")
    assert not ocr._formula_ocr_output_is_credible(r"\mathrm{}+\mathrm{}")
    assert not ocr._formula_ocr_output_is_credible("x" * 9)
    assert not ocr._formula_ocr_output_is_credible("plain prose without math")
    assert not ocr._formula_ocr_output_is_credible(r"\mathrm{ab}+\mathrm{cd}+\mathrm{fg}+\mathrm{hi}+\mathrm{jk}+\mathrm{lm}")
    assert ocr._clean_formula_ocr_output("\n\n") == ""
    assert ocr._clean_formula_ocr_output("result: x^2") == "x^2"


def test_formula_batch_argument_and_output_boundaries(tmp_path: Path) -> None:
    paths = [tmp_path / "one.png", tmp_path / "two.png"]
    with pytest.raises(AppError):
        ocr._formula_command_args_for_images('"unterminated', paths)
    with pytest.raises(AppError):
        ocr._formula_command_args_for_images("", paths)
    assert ocr._formula_command_args_for_images("pix2tex --input={image}", paths) is None
    assert ocr._formula_command_args_for_images("pix2tex {image}", paths) == ["pix2tex", str(paths[0]), str(paths[1])]
    assert ocr._formula_command_args_for_images("pix2tex", paths) == ["pix2tex", str(paths[0]), str(paths[1])]
    assert ocr._split_formula_batch_output("\n", paths) == []
    assert ocr._split_formula_batch_output("x^2\ny^2", paths) == ["x^2", "y^2"]
    assert ocr._split_formula_batch_output("x^2", [paths[0]]) == ["x^2"]
    stdout = f"{paths[0]}: x^2\n{paths[1]}: y^2"
    assert ocr._split_formula_batch_output(stdout, paths) == ["x^2", "y^2"]


def test_formula_batch_runner_falls_back_and_reports_process_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = [tmp_path / "one.png", tmp_path / "two.png"]
    monkeypatch.setattr(ocr, "_formula_command_args_for_images", lambda *_args: None)
    monkeypatch.setattr(ocr, "_run_formula_ocr_file", lambda path, _command: path.stem)
    assert ocr._run_formula_ocr_files(paths, "cmd") == ["one", "two"]
    monkeypatch.setattr(ocr, "_formula_command_args_for_images", lambda *_args: ["cmd"])
    monkeypatch.setattr(ocr.subprocess, "run", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 2, "", "boom"))
    with pytest.raises(AppError):
        ocr._run_formula_ocr_files(paths, "cmd")
    monkeypatch.setattr(ocr.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("cmd", 1)))
    with pytest.raises(AppError):
        ocr._run_formula_ocr_files(paths, "cmd")


def test_deepseek_and_formula_engine_wrap_render_and_image_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = ocr.DeepSeekApiOcrEngine("key")
    monkeypatch.setattr(ocr, "_pil_image_png_bytes", lambda _image: (_ for _ in ()).throw(RuntimeError("bad image")))
    with pytest.raises(AppError) as caught:
        engine.extract_page_image(object())
    assert caught.value.code == ErrorCode.OCR_UNAVAILABLE

    formula = ocr.FormulaOcrCommandEngine.__new__(ocr.FormulaOcrCommandEngine)
    formula._command = "cmd"
    image = SimpleNamespace(save=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("save")))
    with pytest.raises(AppError) as caught:
        formula.extract_page_image(image)
    assert caught.value.code == ErrorCode.OCR_UNAVAILABLE
    monkeypatch.setattr(ocr, "_run_formula_ocr_file", lambda *_args: (_ for _ in ()).throw(AppError("tool", code=ErrorCode.OCR_EMPTY)))
    with pytest.raises(AppError) as caught:
        formula.extract_image(b"BMdata")
    assert caught.value.code == ErrorCode.OCR_EMPTY


def test_windows_and_android_engine_constructor_failure_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr.os, "name", "posix")
    with pytest.raises(AppError):
        ocr.WindowsOcrEngine()
    monkeypatch.setattr(ocr.os, "name", "nt")
    monkeypatch.setattr(ocr, "_powershell_path", lambda: None)
    with pytest.raises(AppError):
        ocr.WindowsOcrEngine()

    original_import = builtins.__import__

    def no_java(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "java":
            raise ModuleNotFoundError("java")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", no_java)
    with pytest.raises(AppError):
        ocr.AndroidMlKitEngine()


def test_pdf_page_fallback_error_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    unavailable = SimpleNamespace(
        name="down",
        extract_page_image=lambda _image: (_ for _ in ()).throw(AppError("down", code=ErrorCode.OCR_UNAVAILABLE)),
    )
    empty = SimpleNamespace(
        name="empty",
        extract_page_image=lambda _image: (_ for _ in ()).throw(AppError("empty", code=ErrorCode.OCR_EMPTY)),
    )
    broken = SimpleNamespace(name="broken", extract_page_image=lambda _image: (_ for _ in ()).throw(RuntimeError("crash")))
    monkeypatch.setattr(ocr, "_pdf_page_images", lambda *_args: ["page"])
    with pytest.raises(AppError) as caught:
        ocr._extract_pdf_with_page_fallback(b"pdf", [unavailable, empty, broken], no_engine_message="none", empty_message="empty")
    assert caught.value.code == ErrorCode.OCR_EMPTY
    hard = SimpleNamespace(name="hard", extract_page_image=lambda _image: (_ for _ in ()).throw(AppError("bad", code=ErrorCode.INVALID_PAYLOAD)))
    with pytest.raises(AppError) as caught:
        ocr._extract_pdf_with_page_fallback(b"pdf", [hard], no_engine_message="none", empty_message="empty")
    assert caught.value.code == ErrorCode.INVALID_PAYLOAD


def test_ocr_details_and_engine_names() -> None:
    assert ocr._engine_name(SimpleNamespace()) == "SimpleNamespace"
    assert ocr._with_error_details("base", ["", " one ", "two", "three", "four", "five"]) == "base Details: one; two; three; four"
    assert ocr._with_error_details("base", []) == "base"
