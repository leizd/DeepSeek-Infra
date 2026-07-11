from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest
from PIL import Image

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.tool_runtime import ocr


class Response:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.data


def png_bytes(mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (8, 6), color=1).save(buffer, "PNG")
    return buffer.getvalue()


def test_ocr_settings_boundaries_and_engine_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ocr,
        "_ocr_settings",
        lambda: SimpleNamespace(mode="quality", pdf_dpi="900", max_image_pixels="bad", formula_timeout_seconds=1, formula_cmd=" custom {image} "),
    )
    assert ocr._ocr_mode() == "quality"
    assert ocr._ocr_pdf_dpi() == 450
    assert ocr._ocr_max_image_pixels() == 16_000_000
    assert ocr._ocr_formula_timeout_seconds() == 5
    assert ocr._ocr_formula_command() == "custom {image}"

    monkeypatch.setattr(ocr, "_ocr_settings", lambda: SimpleNamespace(pdf_dpi="bad", max_image_pixels=-5, formula_timeout_seconds="bad", formula_cmd=""))
    monkeypatch.setattr(ocr.shutil, "which", lambda name: name if name == "latexocr" else None)
    assert ocr._ocr_pdf_dpi() == ocr.OCR_PDF_DPI
    assert ocr._ocr_max_image_pixels() == 1
    assert ocr._ocr_formula_timeout_seconds() == 120
    assert ocr._ocr_formula_command() == "latexocr {image}"


def test_tesseract_location_language_and_powershell_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = tmp_path / "tesseract.exe"
    executable.write_bytes(b"")
    monkeypatch.setenv("TESSERACT_CMD", str(executable))
    assert ocr._locate_tesseract() == str(executable)
    monkeypatch.setenv("TESSERACT_CMD", str(tmp_path / "missing"))
    monkeypatch.setattr(ocr.shutil, "which", lambda name: "/usr/bin/tesseract" if name == "tesseract" else None)
    assert ocr._locate_tesseract() == "/usr/bin/tesseract"

    monkeypatch.setenv("OCR_LANG", "fra,eng,missing")
    assert ocr._select_lang({"eng", "fra"}) == "fra+eng"
    monkeypatch.delenv("OCR_LANG")
    assert ocr._select_lang({"deu"}) == "deu"
    assert ocr._select_lang(set()) == "eng"

    monkeypatch.setattr(ocr.shutil, "which", lambda name: "pwsh" if name == "pwsh" else None)
    assert ocr._powershell_path() == "pwsh"
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    monkeypatch.setattr(ocr.os, "name", "posix")
    assert ocr._powershell_path() is None
    assert ocr._subprocess_creationflags() == 0


@pytest.mark.parametrize(
    ("data", "suffix", "media_type"),
    [
        (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
        (b"\xff\xd8x", ".jpg", "image/jpeg"),
        (b"RIFF0000WEBP", ".webp", "image/webp"),
        (b"II*\x00", ".tif", "image/tiff"),
        (b"GIF89a", ".gif", "image/gif"),
        (b"BMdata", ".bmp", "image/bmp"),
        (b"broken", ".img", "image/png"),
    ],
)
def test_image_type_detection(data: bytes, suffix: str, media_type: str) -> None:
    assert ocr._image_suffix(data) == suffix
    assert ocr._image_media_type(data) == media_type
    assert ocr._image_data_url(data).startswith(f"data:{media_type};base64,")


def test_pil_conversion_pixel_limit_and_invalid_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    image = Image.new("P", (20, 10))
    monkeypatch.setattr(ocr, "_ocr_max_image_pixels", lambda: 25)
    limited = ocr._limit_pil_image_pixels(image)
    assert isinstance(limited, Image.Image)
    assert limited.width * limited.height <= 25
    assert ocr._limit_pil_image_pixels(object()).__class__ is object
    data = ocr._pil_image_png_bytes(image)
    assert data.startswith(b"\x89PNG")
    assert ocr._adaptive_block_size(SimpleNamespace(shape=(100, 200))) % 2 == 1


def test_windows_ocr_process_success_failure_timeout_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "_powershell_path", lambda: "powershell")
    monkeypatch.setattr(ocr, "_subprocess_creationflags", lambda: 0)
    scripts: list[Path] = []

    def completed(*args: Any, **kwargs: Any) -> SimpleNamespace:
        scripts.append(Path(args[0][5]))
        return SimpleNamespace(returncode=0, stdout=" recognized ", stderr="")

    monkeypatch.setattr(ocr.subprocess, "run", completed)
    assert ocr._run_windows_ocr_file(tmp_path / "image.png") == "recognized"
    assert not scripts[0].exists()

    monkeypatch.setattr(ocr.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="backend failed"))
    with pytest.raises(AppError, match="backend failed"):
        ocr._run_windows_ocr_file(tmp_path / "image.png")

    monkeypatch.setattr(ocr.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("ocr", 90)))
    with pytest.raises(AppError, match="timed out"):
        ocr._run_windows_ocr_file(tmp_path / "image.png")

    monkeypatch.setattr(ocr, "_powershell_path", lambda: None)
    with pytest.raises(AppError, match="PowerShell"):
        ocr._run_windows_ocr_file(tmp_path / "image.png")


def test_formula_argument_json_batch_parsing_and_credibility(tmp_path: Path) -> None:
    one = tmp_path / "one.png"
    two = tmp_path / "two.png"
    assert ocr._strip_matching_quotes('"value"') == "value"
    assert ocr._formula_command_args("pix2tex --json", one)[-1] == str(one)
    with pytest.raises(AppError, match="invalid"):
        ocr._formula_command_args('pix2tex "', one)
    with pytest.raises(AppError, match="empty"):
        ocr._formula_command_args("", one)

    assert ocr._formula_output_from_json("x") == "x"
    assert ocr._formula_output_from_json({"nested": {"latex": "x^2"}}) == "x^2"
    assert ocr._formula_output_from_json([{"formula": "a"}, "b", 3]) == "a\nb"
    assert ocr._formula_output_from_json(3) == ""
    assert ocr._looks_like_latex_word_noise("ffffffff") is True
    assert ocr._looks_like_latex_word_noise("bcdfgff") is True
    assert ocr._looks_like_latex_word_noise("short") is False
    assert ocr._formula_ocr_output_is_credible("") is False
    assert ocr._formula_ocr_output_is_credible("plain prose") is False
    assert ocr._formula_ocr_output_is_credible(r"x^2+y^2=z^2") is True

    assert ocr._formula_command_args_for_images("pix2tex {image}", [one, two]) == ["pix2tex", str(one), str(two)]
    assert ocr._formula_command_args_for_images("pix2tex --input={image}", [one, two]) is None
    assert ocr._formula_command_args_for_images("pix2tex", [one, two]) == ["pix2tex", str(one), str(two)]
    with pytest.raises(AppError):
        ocr._formula_command_args_for_images('pix2tex "', [one])
    with pytest.raises(AppError):
        ocr._formula_command_args_for_images("", [one])

    assert ocr._split_formula_batch_output(f"{one}: x^2\n{two}: y^2", [one, two]) == ["x^2", "y^2"]
    assert ocr._split_formula_batch_output("x^2\ny^2", [one, two]) == ["x^2", "y^2"]
    assert ocr._split_formula_batch_output("x^2\n+ y", [one]) == ["x^2\n+ y"]
    assert ocr._split_formula_batch_output("one", [one, two]) == []


def test_formula_process_batch_and_single_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = [tmp_path / "one.png", tmp_path / "two.png"]
    for path in paths:
        path.write_bytes(b"png")
    monkeypatch.setattr(ocr, "_subprocess_creationflags", lambda: 0)
    monkeypatch.setattr(ocr, "_ocr_formula_timeout_seconds", lambda: 5)
    monkeypatch.setattr(ocr.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="x^2\ny^2", stderr=""))
    assert ocr._run_formula_ocr_files([], "pix2tex") == []
    assert ocr._run_formula_ocr_files(paths, "pix2tex") == ["x^2", "y^2"]
    assert ocr._run_formula_ocr_file(paths[0], "pix2tex") == "x^2\ny^2"

    monkeypatch.setattr(ocr, "_formula_command_args_for_images", lambda template, current: None)
    monkeypatch.setattr(ocr, "_run_formula_ocr_file", lambda path, template: path.stem)
    assert ocr._run_formula_ocr_files(paths, "pix2tex") == ["one", "two"]

    for exception, fragment in [(FileNotFoundError(), "not found"), (subprocess.TimeoutExpired("ocr", 5), "timed out")]:
        monkeypatch.setattr(ocr, "_formula_command_args_for_images", lambda template, current: ["pix2tex"])
        monkeypatch.setattr(ocr.subprocess, "run", lambda *args, error=exception, **kwargs: (_ for _ in ()).throw(error))
        with pytest.raises(AppError, match=fragment):
            ocr._run_formula_ocr_files(paths, "pix2tex")

    monkeypatch.setattr(ocr.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="", stderr="bad command"))
    with pytest.raises(AppError, match="bad command"):
        ocr._run_formula_ocr_files(paths, "pix2tex")


def test_formula_regions_reject_bad_boxes_and_limit_results() -> None:
    assert ocr._int_from_ocr_data("bad", 9) == 9
    assert ocr._region_from_formula_words([], (100, 100)) is None
    assert ocr._region_from_formula_words([(0, 0, 2, 2, "x")], (100, 100)) is None
    assert ocr._region_from_formula_words([(0, 0, 5, 20, "x")], (100, 100)) is None
    data: dict[str, list[object]] = {
        "text": ["x^2", "y^2", "noise", "z^2"],
        "left": [0, 20, 50, 90],
        "top": [0, 0, 0, 0],
        "width": [15, 15, 1, 15],
        "height": [10, 10, 1, 10],
        "block_num": [1, 1, 1, 1],
        "par_num": [1, 1, 1, 1],
        "line_num": [1, 1, 1, 1],
    }
    regions = ocr._formula_regions_from_tesseract_data(data, (200, 100), max_regions=1)
    assert len(regions) == 1
    assert ocr._looks_like_formula_ocr_token("") is False
    assert ocr._looks_like_formula_ocr_token("中文") is False
    assert ocr._looks_like_formula_ocr_token("a") is False


def test_extract_formula_snippets_deduplicates_and_handles_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    image = Image.new("RGB", (100, 40), "white")
    data = {
        "text": ["x^2"],
        "left": [10],
        "top": [10],
        "width": [30],
        "height": [15],
        "block_num": [1],
        "par_num": [1],
        "line_num": [1],
    }
    tesseract = SimpleNamespace(Output=SimpleNamespace(DICT="dict"), image_to_data=lambda *args, **kwargs: data)
    monkeypatch.setattr(ocr, "_ocr_formula_command", lambda: "pix2tex {image}")
    monkeypatch.setattr(ocr, "_ocr_mode", lambda: "quality")
    monkeypatch.setattr(ocr, "_run_formula_ocr_files", lambda paths, command: ["x^2", " x^2 "])
    assert ocr._extract_formula_snippets_from_image(image, tesseract, "eng") == ["x^2"]
    assert ocr._extract_formula_snippets_from_image(object(), tesseract, "eng") == []
    monkeypatch.setattr(ocr, "_ocr_mode", lambda: "fast")
    assert ocr._extract_formula_snippets_from_image(image, tesseract, "eng") == []
    monkeypatch.setattr(ocr, "_ocr_mode", lambda: "quality")
    monkeypatch.setattr(tesseract, "image_to_data", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad metadata")))
    assert ocr._extract_formula_snippets_from_image(image, tesseract, "eng") == []
    assert ocr._append_formula_snippets("x^2", ["x^2", "", "y^2", "y^2"]).endswith("- y^2")


def test_deepseek_response_shapes_and_network_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError, match="no answer"):
        ocr._deepseek_ocr_response_content({})
    assert ocr._deepseek_ocr_response_content({"choices": ["bad"]}) == ""
    assert ocr._deepseek_ocr_response_content({"choices": [{"message": "bad"}]}) == ""
    assert ocr._deepseek_ocr_response_content({"choices": [{"message": {"content": ["one", {"text": "two"}, 3]}}]}) == "one\ntwo"
    assert ocr._deepseek_ocr_response_content({"choices": [{"message": {"content": 3}}]}) == ""
    assert ocr._clean_deepseek_ocr_output("```text\n hello \n```") == "hello"
    assert ocr._clean_deepseek_ocr_output("No readable text.") == ""

    http_error = urllib.error.HTTPError("https://api", 429, "limited", Message(), io.BytesIO(b'{"error":"rate limited"}'))
    monkeypatch.setattr(ocr.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(http_error))
    with pytest.raises(AppError, match="DeepSeek OCR failed") as exc:
        ocr._run_deepseek_ocr_image(png_bytes(), "key")
    assert exc.value.status == 429

    monkeypatch.setattr(ocr.urllib.request, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    with pytest.raises(AppError, match="Cannot reach"):
        ocr._run_deepseek_ocr_image(png_bytes(), "key")
    monkeypatch.setattr(ocr.urllib.request, "urlopen", lambda *args, **kwargs: Response(b"not-json"))
    with pytest.raises(AppError, match="invalid JSON"):
        ocr._run_deepseek_ocr_image(png_bytes(), "key")
    monkeypatch.setattr(ocr.urllib.request, "urlopen", lambda *args, **kwargs: Response(json.dumps([]).encode()))
    with pytest.raises(AppError, match="invalid JSON"):
        ocr._run_deepseek_ocr_image(png_bytes(), "key")


def test_deepseek_pdf_engine_empty_page_and_render_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = ocr.DeepSeekApiOcrEngine("key")
    pdf2image = ModuleType("pdf2image")
    pdf2image.convert_from_bytes = lambda *args, **kwargs: ["empty", "text"]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pdf2image", pdf2image)

    def extract(image: object) -> str:
        if image == "empty":
            raise AppError("empty", code=ErrorCode.OCR_EMPTY)
        return "page text"

    monkeypatch.setattr(engine, "extract_page_image", extract)
    assert "page text" in engine.extract(b"pdf")
    monkeypatch.setattr(pdf2image, "convert_from_bytes", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("corrupt pdf")))
    with pytest.raises(AppError, match="DeepSeek PDF OCR failed"):
        engine.extract(b"broken")
    monkeypatch.setattr(ocr, "_pil_image_png_bytes", lambda image: (_ for _ in ()).throw(RuntimeError("bad image")))
    with pytest.raises(AppError, match="page OCR failed"):
        ocr.DeepSeekApiOcrEngine.extract_page_image(engine, object())


def test_formula_windows_and_tesseract_engines_use_stub_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    pdf2image = ModuleType("pdf2image")
    pdf2image.convert_from_bytes = lambda *args, **kwargs: [SimpleNamespace(save=lambda path, fmt: Path(path).write_bytes(b"png"))]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pdf2image", pdf2image)

    formula = ocr.FormulaOcrCommandEngine.__new__(ocr.FormulaOcrCommandEngine)
    formula._command = "pix2tex {image}"
    monkeypatch.setattr(ocr, "_run_formula_ocr_file", lambda path, command: "x^2")
    assert "x^2" in formula.extract(b"pdf")
    assert formula.extract_page_image(SimpleNamespace(save=lambda path, fmt: Path(path).write_bytes(b"png"))) == "x^2"
    monkeypatch.setattr(ocr, "_run_formula_ocr_file", lambda path, command: (_ for _ in ()).throw(RuntimeError("backend")))
    with pytest.raises(AppError, match="Formula image OCR failed"):
        formula.extract_image(png_bytes())

    windows = ocr.WindowsOcrEngine.__new__(ocr.WindowsOcrEngine)
    monkeypatch.setattr(ocr, "_run_windows_ocr_file", lambda path: " windows text ")
    assert "windows text" in windows.extract(b"pdf")
    assert windows.extract_image(png_bytes()) == "windows text"
    monkeypatch.setattr(ocr, "_run_windows_ocr_file", lambda path: (_ for _ in ()).throw(RuntimeError("backend")))
    with pytest.raises(AppError, match="Windows image OCR failed"):
        windows.extract_image(png_bytes())

    pytesseract = ModuleType("pytesseract")
    pytesseract.pytesseract = SimpleNamespace(tesseract_cmd="")  # type: ignore[attr-defined]
    pytesseract.get_languages = lambda config: (_ for _ in ()).throw(RuntimeError("languages unavailable"))  # type: ignore[attr-defined]
    pytesseract.image_to_string = lambda image, lang, config="": "text"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pytesseract", pytesseract)
    monkeypatch.setattr(ocr, "_locate_tesseract", lambda: "tesseract")
    tess = ocr.TesseractEngine()
    assert tess._lang == "eng"
    assert tess.extract_image(png_bytes("RGBA")) == "text"


def test_ocr_backend_unavailable_and_candidate_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AppError, match="Missing DeepSeek API Key"):
        ocr.DeepSeekApiOcrEngine("")
    monkeypatch.setattr(ocr, "_ocr_formula_command", lambda: "")
    with pytest.raises(AppError, match="not configured"):
        ocr.FormulaOcrCommandEngine()
    monkeypatch.setattr(ocr.os, "name", "posix")
    with pytest.raises(AppError, match="only available"):
        ocr.WindowsOcrEngine()

    java = ModuleType("java")
    java.jclass = lambda name: SimpleNamespace(isAvailable=lambda: False)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "java", java)
    with pytest.raises(AppError, match="not initialized"):
        ocr.AndroidMlKitEngine()

    failure = AppError("unavailable", code=ErrorCode.OCR_UNAVAILABLE)
    monkeypatch.setattr(ocr, "DeepSeekApiOcrEngine", lambda api_key=None: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(ocr, "FormulaOcrCommandEngine", lambda: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(ocr, "TesseractEngine", lambda: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(ocr.os, "name", "nt")
    monkeypatch.setattr(ocr, "WindowsOcrEngine", lambda: (_ for _ in ()).throw(failure))
    engines, errors = ocr._ocr_engine_candidates()
    assert engines == []
    assert len(errors) == 4
    assert ocr.select_ocr_engine() is None


def test_fallback_reports_runtime_exception_empty_and_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    unavailable = SimpleNamespace(name="unavailable", extract_image=lambda data: (_ for _ in ()).throw(AppError("down", code=ErrorCode.OCR_UNAVAILABLE)))
    broken = SimpleNamespace(name="broken", extract_image=lambda data: (_ for _ in ()).throw(RuntimeError("crash")))
    empty = SimpleNamespace(name="empty", extract_image=lambda data: "")
    monkeypatch.setattr(ocr, "_ocr_engine_candidates", lambda api_key=None: ([unavailable, broken, empty], ["startup"] ))
    with pytest.raises(AppError) as exc:
        ocr.extract_image_ocr(b"broken")
    assert exc.value.code == ErrorCode.OCR_EMPTY

    monkeypatch.setattr(ocr, "_ocr_engine_candidates", lambda api_key=None: ([unavailable, broken], ["startup"]))
    with pytest.raises(AppError, match="Details") as exc:
        ocr.extract_image_ocr(b"broken")
    assert exc.value.code == ErrorCode.OCR_UNAVAILABLE
    assert ocr._with_error_details("message", []) == "message"
    assert ocr._engine_name(cast(ocr.OCREngine, object())) == "object"


def test_windows_tesseract_candidate_and_preprocess_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None)
    monkeypatch.setattr(ocr.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(ocr.Path, "is_file", lambda path: str(path).endswith("tesseract.exe"))
    assert ocr._locate_tesseract().endswith("tesseract.exe")  # type: ignore[union-attr]

    class FakeArray:
        ndim = 2
        size = 100
        shape = (100, 200)

        def __getitem__(self, key: object) -> "FakeArray":
            return self

        def __lt__(self, value: object) -> "FakeArray":
            return self

        def tobytes(self) -> bytes:
            return b"array"

    cv2 = ModuleType("cv2")
    cv2.INTER_CUBIC = 1  # type: ignore[attr-defined]
    cv2.THRESH_BINARY = 2  # type: ignore[attr-defined]
    cv2.THRESH_OTSU = 4  # type: ignore[attr-defined]
    cv2.ADAPTIVE_THRESH_GAUSSIAN_C = 8  # type: ignore[attr-defined]
    cv2.resize = lambda value, *args, **kwargs: value  # type: ignore[attr-defined]
    cv2.bilateralFilter = lambda value, *args: value  # type: ignore[attr-defined]
    cv2.threshold = lambda value, *args: (0, FakeArray())  # type: ignore[attr-defined]
    cv2.bitwise_not = lambda value: value  # type: ignore[attr-defined]
    cv2.adaptiveThreshold = lambda value, *args: FakeArray()  # type: ignore[attr-defined]
    cv2.equalizeHist = lambda value: value  # type: ignore[attr-defined]
    numpy = ModuleType("numpy")
    numpy.array = lambda value: FakeArray()  # type: ignore[attr-defined]
    numpy.mean = lambda value: 255.0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    monkeypatch.setitem(sys.modules, "numpy", numpy)
    monkeypatch.setattr(Image, "fromarray", lambda value: value)
    candidates = ocr._preprocess_candidates_for_ocr(Image.new("RGB", (20, 10)), mode="balanced")
    assert len(candidates) == 1
    assert ocr._preprocess_for_ocr(Image.new("RGB", (20, 10))) is not None

    class BadSlice(FakeArray):
        def __getitem__(self, key: object) -> "FakeArray":
            raise RuntimeError("slice")

    unique = ocr._arrays_to_unique_images([BadSlice(), BadSlice()], SimpleNamespace(fromarray=lambda value: value))
    assert len(unique) == 2


def test_deskew_success_skip_and_exception() -> None:
    coords = [1] * 30
    numpy = SimpleNamespace(column_stack=lambda value: coords, where=lambda value: value)
    cv2 = SimpleNamespace(
        minAreaRect=lambda value: (None, None, -5.0),
        getRotationMatrix2D=lambda center, angle, scale: "matrix",
        warpAffine=lambda *args, **kwargs: "deskewed",
        INTER_CUBIC=1,
        BORDER_REPLICATE=2,
    )
    class Comparable:
        shape = (100, 200)
        def __lt__(self, value: object) -> object:
            return object()

    assert ocr._deskew_gray(Comparable(), cv2, numpy) == "deskewed"
    cv2.minAreaRect = lambda value: (None, None, 0.1)
    assert ocr._deskew_gray(Comparable(), cv2, numpy) is None
    numpy.where = lambda value: (_ for _ in ()).throw(RuntimeError("bad"))
    assert ocr._deskew_gray(Comparable(), cv2, numpy) is None


@pytest.mark.parametrize(
    "value",
    [
        r"\mathrm{}\mathrm{}x^2",
        "aaaaaaaaaa^2",
        r"\mathrm{ffffff}\mathrm{gggggg}x^2",
        r"\mathrm{aa}\mathrm{bb}\mathrm{cc}\mathrm{zz}x^2",
        (r"\mathrm{zz}" * 20) + "x^2",
        (r"\bar{x}" * 20) + "x^2",
        (r"\mathrm{x}" * 40) + "x^2",
    ],
)
def test_formula_credibility_rejects_hallucinated_wrappers(value: str) -> None:
    assert ocr._formula_ocr_output_is_credible(value) is False


def test_formula_single_process_missing_timeout_and_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "formula.png"
    path.write_bytes(b"png")
    monkeypatch.setattr(ocr, "_ocr_formula_timeout_seconds", lambda: 5)
    monkeypatch.setattr(ocr, "_subprocess_creationflags", lambda: 0)
    for error, text in [(FileNotFoundError(), "not found"), (subprocess.TimeoutExpired("ocr", 5), "timed out")]:
        monkeypatch.setattr(ocr.subprocess, "run", lambda *args, current=error, **kwargs: (_ for _ in ()).throw(current))
        with pytest.raises(AppError, match=text):
            ocr._run_formula_ocr_file(path, "pix2tex")
    monkeypatch.setattr(ocr.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="details", stderr=""))
    with pytest.raises(AppError, match="details"):
        ocr._run_formula_ocr_file(path, "pix2tex")


def test_formula_snippet_missing_output_regions_and_base_only(monkeypatch: pytest.MonkeyPatch) -> None:
    image = Image.new("RGB", (10, 10))
    monkeypatch.setattr(ocr, "_ocr_formula_command", lambda: "pix2tex {image}")
    monkeypatch.setattr(ocr, "_ocr_mode", lambda: "quality")
    assert ocr._extract_formula_snippets_from_image(image, SimpleNamespace(Output=object()), "eng") == []
    tesseract = SimpleNamespace(Output=SimpleNamespace(DICT="dict"), image_to_data=lambda *args, **kwargs: {"text": []})
    assert ocr._extract_formula_snippets_from_image(image, tesseract, "eng") == []
    assert ocr._append_formula_snippets("x^2", ["x^2"]) == "x^2"
    assert ocr.OCREngine.extract(None, b"") is None  # type: ignore[arg-type]
    assert ocr.OCREngine.extract_image(None, b"") is None  # type: ignore[arg-type]


def test_engine_dependency_and_corrupt_input_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    with pytest.raises(AppError, match="dependencies"):
        ocr.TesseractEngine()

    pytesseract = ModuleType("pytesseract")
    pytesseract.pytesseract = SimpleNamespace(tesseract_cmd="")  # type: ignore[attr-defined]
    pytesseract.get_languages = lambda config: []  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pytesseract", pytesseract)
    monkeypatch.setattr(ocr, "_locate_tesseract", lambda: None)
    with pytest.raises(AppError, match="executable not found"):
        ocr.TesseractEngine()

    engine = ocr.TesseractEngine.__new__(ocr.TesseractEngine)
    engine._tesseract = SimpleNamespace(image_to_string=lambda *args, **kwargs: "")
    engine._lang = "eng"
    with pytest.raises(AppError, match="Image OCR failed"):
        engine.extract_image(b"corrupt image")
    monkeypatch.setattr(engine, "_image_to_string", lambda image, config: (_ for _ in ()).throw(RuntimeError("backend")))
    monkeypatch.setattr(ocr, "_ocr_mode", lambda: "fast")
    assert engine._recognize_image(object()) == ""


def test_pdf_page_render_and_fallback_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    renderer = SimpleNamespace(convert_from_bytes=lambda *args, **kwargs: ["page"])
    assert ocr._pdf_page_images(b"pdf", [SimpleNamespace(_pdf2image=renderer)]) == ["page"]

    pdf2image = ModuleType("pdf2image")
    pdf2image.convert_from_bytes = lambda *args, **kwargs: ["fallback"]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pdf2image", pdf2image)
    assert ocr._pdf_page_images(b"pdf", [cast(ocr.OCREngine, object())]) == ["fallback"]
    monkeypatch.setattr(pdf2image, "convert_from_bytes", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("render")))
    engine = SimpleNamespace(name="page", extract_page_image=lambda image: "")
    with pytest.raises(AppError, match="rendering failed"):
        ocr._extract_pdf_with_page_fallback(b"pdf", [engine], no_engine_message="none", empty_message="empty")

    monkeypatch.setattr(ocr, "_pdf_page_images", lambda data, engines: ["page"])
    unavailable = SimpleNamespace(name="unavailable", extract_page_image=lambda image: (_ for _ in ()).throw(AppError("down", code=ErrorCode.OCR_UNAVAILABLE)))
    broken = SimpleNamespace(name="broken", extract_page_image=lambda image: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(AppError) as exc:
        ocr._extract_pdf_with_page_fallback(b"pdf", [unavailable, broken], no_engine_message="none", empty_message="empty")
    assert exc.value.code == ErrorCode.OCR_UNAVAILABLE
    empty = SimpleNamespace(name="empty", extract_page_image=lambda image: "")
    with pytest.raises(AppError) as exc:
        ocr._extract_pdf_with_page_fallback(b"pdf", [empty], no_engine_message="none", empty_message="empty")
    assert exc.value.code == ErrorCode.OCR_EMPTY


def test_tesseract_locator_prefers_override_then_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    override = tmp_path / "tesseract-custom"
    override.write_text("", encoding="utf-8")
    monkeypatch.setenv("TESSERACT_CMD", str(override))
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: "ignored")
    assert ocr._locate_tesseract() == str(override)

    monkeypatch.delenv("TESSERACT_CMD")
    monkeypatch.setattr(ocr.shutil, "which", lambda _name: "/usr/bin/tesseract")
    assert ocr._locate_tesseract() == "/usr/bin/tesseract"

    monkeypatch.setattr(ocr.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ocr.os, "name", "posix")
    assert ocr._locate_tesseract() is None


def test_ocr_engine_candidate_order_collects_startup_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    class Api:
        name = "deepseek-api"

        def __init__(self, api_key: str | None = None) -> None:
            raise AppError("missing key", code=ErrorCode.OCR_UNAVAILABLE)

    class Formula:
        def __init__(self) -> None:
            raise AppError("missing command", code=ErrorCode.OCR_UNAVAILABLE)

    class Tesseract:
        name = "tesseract"

    class Windows:
        def __init__(self) -> None:
            raise AppError("missing windows engine", code=ErrorCode.OCR_UNAVAILABLE)

    monkeypatch.setattr(ocr, "DeepSeekApiOcrEngine", Api)
    monkeypatch.setattr(ocr, "FormulaOcrCommandEngine", Formula)
    monkeypatch.setattr(ocr, "TesseractEngine", Tesseract)
    monkeypatch.setattr(ocr, "WindowsOcrEngine", Windows)
    monkeypatch.setattr(ocr.os, "name", "nt")
    monkeypatch.delenv("DEEPSEEK_ANDROID_APP", raising=False)
    engines, errors = ocr._ocr_engine_candidates()
    assert [ocr._engine_name(engine) for engine in engines] == ["tesseract"]
    assert len(errors) == 3


def test_android_ocr_candidate_returns_early_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    api = SimpleNamespace(name="deepseek-api")
    android = SimpleNamespace(name="android-mlkit")
    monkeypatch.setattr(ocr, "DeepSeekApiOcrEngine", lambda api_key=None: api)
    monkeypatch.setattr(ocr, "AndroidMlKitEngine", lambda: android)
    monkeypatch.setenv("DEEPSEEK_ANDROID_APP", "1")
    engines, errors = ocr._ocr_engine_candidates()
    assert engines == [api, android] and errors == []


def test_extract_fallback_prefers_deepseek_and_best_local_result(monkeypatch: pytest.MonkeyPatch) -> None:
    deepseek = SimpleNamespace(name="deepseek-api", extract_image=lambda data: "cloud text")
    skipped = SimpleNamespace(name="local", extract_image=lambda data: (_ for _ in ()).throw(AssertionError("must not run")))
    monkeypatch.setattr(ocr, "_ocr_engine_candidates", lambda api_key=None: ([deepseek, skipped], []))
    assert ocr._extract_with_fallback(b"image", mode="image", no_engine_message="none", empty_message="empty") == "cloud text"

    short = SimpleNamespace(name="short", extract_image=lambda data: "x")
    long = SimpleNamespace(name="long", extract_image=lambda data: "a much longer readable sentence")
    unavailable = SimpleNamespace(name="down", extract_image=lambda data: (_ for _ in ()).throw(AppError("down", code=ErrorCode.OCR_UNAVAILABLE)))
    broken = SimpleNamespace(name="broken", extract_image=lambda data: (_ for _ in ()).throw(RuntimeError("crash")))
    monkeypatch.setattr(ocr, "_ocr_engine_candidates", lambda api_key=None: ([unavailable, broken, short, long], []))
    assert "longer" in ocr._extract_with_fallback(b"image", mode="image", no_engine_message="none", empty_message="empty")


def test_extract_fallback_distinguishes_no_engine_empty_and_hard_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "_ocr_engine_candidates", lambda api_key=None: ([], ["startup failed"]))
    with pytest.raises(AppError) as caught:
        ocr._extract_with_fallback(b"x", mode="image", no_engine_message="none", empty_message="empty")
    assert caught.value.code == ErrorCode.OCR_UNAVAILABLE and "startup failed" in str(caught.value)

    empty = SimpleNamespace(name="empty", extract_image=lambda data: "")
    monkeypatch.setattr(ocr, "_ocr_engine_candidates", lambda api_key=None: ([empty], []))
    with pytest.raises(AppError) as caught:
        ocr._extract_with_fallback(b"x", mode="image", no_engine_message="none", empty_message="empty")
    assert caught.value.code == ErrorCode.OCR_EMPTY

    hard = SimpleNamespace(name="hard", extract_image=lambda data: (_ for _ in ()).throw(AppError("bad", code=ErrorCode.INVALID_PAYLOAD)))
    monkeypatch.setattr(ocr, "_ocr_engine_candidates", lambda api_key=None: ([hard], []))
    with pytest.raises(AppError) as caught:
        ocr._extract_with_fallback(b"x", mode="image", no_engine_message="none", empty_message="empty")
    assert caught.value.code == ErrorCode.INVALID_PAYLOAD


def test_pdf_page_fallback_prefers_deepseek_and_best_scored_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, "_pdf_page_images", lambda data, engines: ["one", "two"])
    local = SimpleNamespace(name="local", extract_page_image=lambda image: "short")
    cloud = SimpleNamespace(name="deepseek-api", extract_page_image=lambda image: f"cloud {image}")
    result = ocr._extract_pdf_with_page_fallback(b"pdf", [local, cloud], no_engine_message="none", empty_message="empty")
    assert "cloud one" in str(result) and "cloud two" in str(result)
