from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import deepseek_infra.infra.tool_runtime.ocr as ocr
from deepseek_infra.core.errors import AppError, ErrorCode


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._data


class OcrTests(unittest.TestCase):
    def test_tesseract_engine_extract_labels_pdf_pages(self) -> None:
        engine = ocr.TesseractEngine.__new__(ocr.TesseractEngine)
        engine._pdf2image = SimpleNamespace(convert_from_bytes=lambda data, dpi, fmt: ["page1", "page2"])
        engine._tesseract = SimpleNamespace(image_to_string=lambda image, lang: f"text from {image}" if image == "page2" else "")
        engine._lang = "eng"

        text = engine.extract(b"%PDF")

        self.assertIn("[PDF 第 2 页 (OCR)]", text)
        self.assertIn("text from page2", text)
        self.assertNotIn("page1", text)

    def test_ocr_mode_and_tesseract_configs_are_normalized(self) -> None:
        self.assertEqual(ocr._normalize_ocr_mode("quality"), "quality")
        self.assertEqual(ocr._normalize_ocr_mode("unknown"), "balanced")
        self.assertEqual(ocr._tesseract_configs_for_mode("fast"), ("--psm 6 -c preserve_interword_spaces=1",))
        self.assertTrue(any(config.startswith("--psm 11") for config in ocr._tesseract_configs_for_mode("balanced")))
        self.assertTrue(any(config.startswith("--psm 7") for config in ocr._tesseract_configs_for_mode("balanced")))
        self.assertTrue(any(config.startswith("--psm 13") for config in ocr._tesseract_configs_for_mode("quality")))

    def test_select_lang_prefers_formula_language_when_available(self) -> None:
        self.assertEqual(ocr._select_lang({"eng", "equ"}), "eng+equ")

    def test_deepseek_api_engine_extracts_image_text(self) -> None:
        response = {
            "choices": [
                {
                    "message": {
                        "content": " API OCR text ",
                    }
                }
            ]
        }

        with patch.object(ocr.urllib.request, "urlopen", return_value=FakeResponse(response)) as urlopen:
            engine = ocr.DeepSeekApiOcrEngine(api_key="sk-test")
            text = engine.extract_image(b"\x89PNG\r\n\x1a\nimage")

        self.assertEqual(text, "API OCR text")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-test")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "deepseek-v4-pro")
        content = body["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertEqual(content[1]["type"], "image_url")
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_deepseek_api_engine_reports_empty_text(self) -> None:
        response = {"choices": [{"message": {"content": "No readable text."}}]}

        with patch.object(ocr.urllib.request, "urlopen", return_value=FakeResponse(response)):
            engine = ocr.DeepSeekApiOcrEngine(api_key="sk-test")
            with self.assertRaises(AppError) as cm:
                engine.extract_image(b"\x89PNG\r\n\x1a\nimage")

        self.assertEqual(cm.exception.code, ErrorCode.OCR_EMPTY)
        self.assertEqual(cm.exception.status, 422)

    def test_extract_image_ocr_prefers_deepseek_api_result(self) -> None:
        deepseek = SimpleNamespace(name="deepseek-api", extract_image=lambda data: " api text ")
        tesseract = SimpleNamespace(name="tesseract", extract_image=lambda data: "much longer local text")

        with patch.object(ocr, "_ocr_engine_candidates", return_value=([deepseek, tesseract], [])):
            text = ocr.extract_image_ocr(b"\x89PNG")

        self.assertEqual(text, "api text")

    def test_formula_command_args_replace_image_placeholder(self) -> None:
        args = ocr._formula_command_args('pix2tex "{image}" --json', Path(r"C:\tmp\formula image.png"))

        self.assertEqual(args, ["pix2tex", r"C:\tmp\formula image.png", "--json"])

    def test_tesseract_engine_retries_sparse_text_psm(self) -> None:
        calls: list[str] = []

        def image_to_string(image: object, lang: str, config: str = "") -> str:
            calls.append(config)
            return " sparse text " if config.startswith("--psm 11") else ""

        engine = ocr.TesseractEngine.__new__(ocr.TesseractEngine)
        engine._pdf2image = SimpleNamespace(convert_from_bytes=lambda data, dpi, fmt: ["page"])
        engine._tesseract = SimpleNamespace(image_to_string=image_to_string)
        engine._lang = "eng"

        with patch.object(ocr, "_ocr_mode", return_value="balanced"):
            text = engine.extract(b"%PDF")

        self.assertIn("sparse text", text)
        self.assertTrue(any(config.startswith("--psm 6") for config in calls))
        self.assertTrue(any(config.startswith("--psm 11") for config in calls))

    def test_extract_pdf_ocr_reports_empty_ocr_result(self) -> None:
        fake_engine = SimpleNamespace(name="fake", extract=lambda data: "")

        with patch.object(ocr, "_ocr_engine_candidates", return_value=([fake_engine], [])):
            with self.assertRaises(AppError) as cm:
                ocr.extract_pdf_ocr(b"%PDF")

        self.assertEqual(cm.exception.code, ErrorCode.OCR_EMPTY)
        self.assertEqual(cm.exception.status, 422)

    def test_extract_pdf_ocr_reports_unavailable_engine(self) -> None:
        with patch.object(ocr, "_ocr_engine_candidates", return_value=([], [])):
            with self.assertRaises(AppError) as cm:
                ocr.extract_pdf_ocr(b"%PDF")

        self.assertEqual(cm.exception.code, ErrorCode.OCR_UNAVAILABLE)
        self.assertEqual(cm.exception.status, 415)

    def test_extract_image_ocr_returns_fake_engine_text(self) -> None:
        fake_engine = SimpleNamespace(name="fake", extract_image=lambda data: " 图片文字 ")

        with patch.object(ocr, "_ocr_engine_candidates", return_value=([fake_engine], [])):
            text = ocr.extract_image_ocr(b"\x89PNG")

        self.assertEqual(text, "图片文字")

    def test_extract_image_ocr_reports_empty_result(self) -> None:
        fake_engine = SimpleNamespace(name="fake", extract_image=lambda data: "")

        with patch.object(ocr, "_ocr_engine_candidates", return_value=([fake_engine], [])):
            with self.assertRaises(AppError) as cm:
                ocr.extract_image_ocr(b"\x89PNG")

        self.assertEqual(cm.exception.code, ErrorCode.OCR_EMPTY)
        self.assertEqual(cm.exception.status, 422)

    def test_extract_image_ocr_reports_unavailable_engine(self) -> None:
        with patch.object(ocr, "_ocr_engine_candidates", return_value=([], [])):
            with self.assertRaises(AppError) as cm:
                ocr.extract_image_ocr(b"\x89PNG")

        self.assertEqual(cm.exception.code, ErrorCode.OCR_UNAVAILABLE)
        self.assertEqual(cm.exception.status, 415)

    def test_extract_image_ocr_falls_back_after_runtime_engine_failure(self) -> None:
        def fail_image(data: bytes) -> str:
            raise AppError("tesseract runtime failed", code=ErrorCode.OCR_UNAVAILABLE, status=415)

        tesseract = SimpleNamespace(name="tesseract", extract_image=fail_image)
        windows = SimpleNamespace(name="windows-ocr", extract_image=lambda data: " windows text ")

        with patch.object(ocr, "_ocr_engine_candidates", return_value=([tesseract, windows], [])):
            text = ocr.extract_image_ocr(b"\x89PNG")

        self.assertEqual(text, "windows text")

    def test_extract_pdf_ocr_falls_back_per_page(self) -> None:
        tesseract = SimpleNamespace(
            name="tesseract",
            extract_page_image=lambda image: " first page " if image == "page1" else "",
        )
        windows = SimpleNamespace(
            name="windows-ocr",
            extract_page_image=lambda image: " second page " if image == "page2" else "",
        )

        with (
            patch.object(ocr, "_ocr_engine_candidates", return_value=([tesseract, windows], [])),
            patch.object(ocr, "_pdf_page_images", return_value=["page1", "page2"]),
        ):
            text = ocr.extract_pdf_ocr(b"%PDF")

        self.assertIn("[PDF 第 1 页 (OCR)]\nfirst page", text)
        self.assertIn("[PDF 第 2 页 (OCR)]\nsecond page", text)

    def test_normalize_ocr_text_preserves_table_like_columns(self) -> None:
        text = ocr.normalize_ocr_text(" A  B\r\n\r\nC   D\x00")

        self.assertEqual(text, "A B\n\nC\tD")

    def test_normalize_ocr_text_keeps_formula_lines(self) -> None:
        text = ocr.normalize_ocr_text(" --- \n ∫_0^1   x^2 dx ＝ 1／3 \n P(A｜B)   = P(A∩B)／P(B) ")

        self.assertEqual(text, "∫_0^1 x^2 dx = 1/3\nP(A|B) = P(A∩B)/P(B)")

    def test_ocr_text_score_rewards_formula_symbols(self) -> None:
        formula_score = ocr._ocr_text_score("∑_{i=1}^n x_i = n μ")
        noise_score = ocr._ocr_text_score("____ ~~ ||||")

        self.assertGreater(formula_score, noise_score)

    def test_clean_formula_ocr_output_extracts_latex_from_json_or_fence(self) -> None:
        json_text = ocr._clean_formula_ocr_output('{"latex": "\\\\frac{x^2}{2}"}')
        fence_text = ocr._clean_formula_ocr_output("```latex\n\\int_0^1 x dx\n```")
        cli_text = ocr._clean_formula_ocr_output(r"D:\deepseek\formula.png: \frac{x}{y}")

        self.assertEqual(json_text, r"\frac{x^2}{2}")
        self.assertEqual(fence_text, r"\int_0^1 x dx")
        self.assertEqual(cli_text, r"\frac{x}{y}")

    def test_clean_formula_ocr_output_rejects_pix2tex_hallucinated_text(self) -> None:
        gibberish = (
            r"\mathrm{ighffighgfk},\X,\mathrm{ffiliffixi},\mathrm{fiffiet},"
            r"\mathrm{ffffffffffffffffffffffffffffffffffffffffffffffff}"
        )
        mixed_text_hallucination = (
            r"\begin{array}{c}"
            r"{{(7);\mathrm{ig}\rightarrow\mp\hbar\mathrm{sigh},\mathrm{ij}\;\mathrm{ij};"
            r"\mathrm{sig}\mathrm{ij}\mathrm{};\mathrm{zil}\mathrm{};"
            r"\bar{\zeta}\bar{\zeta}\bar{\zeta}\bar{\zeta}\bar{\zeta}\bar{\zeta}\bar{\zeta}\bar{\zeta}}}"
            r"\end{array}"
        )

        self.assertEqual(ocr._clean_formula_ocr_output(gibberish), "")
        self.assertEqual(ocr._clean_formula_ocr_output(mixed_text_hallucination), "")
        self.assertEqual(ocr._clean_formula_ocr_output(r"x^2+y^2=z^2"), r"x^2+y^2=z^2")
        self.assertEqual(ocr._clean_formula_ocr_output(r"\mathrm{Cov}(U,V)"), r"\mathrm{Cov}(U,V)")

    def test_formula_regions_use_tesseract_word_boxes(self) -> None:
        data: dict[str, list[Any]] = {
            "text": ["设二维随机变量", "(X,Y)", "服从", "D", "上的均匀分布", "{(x力10和xz和3,0和7和3)"],
            "left": [10, 180, 240, 300, 330, 470],
            "top": [20, 20, 20, 20, 20, 20],
            "width": [150, 48, 42, 16, 120, 260],
            "height": [28, 28, 28, 28, 28, 28],
            "block_num": [1, 1, 1, 1, 1, 1],
            "par_num": [1, 1, 1, 1, 1, 1],
            "line_num": [1, 1, 1, 1, 1, 1],
        }

        regions = ocr._formula_regions_from_tesseract_data(data, (800, 80))

        self.assertGreaterEqual(len(regions), 2)
        self.assertTrue(any(left <= 180 and right >= 228 for left, _top, right, _bottom in regions))
        self.assertTrue(any(left <= 470 and right >= 730 for left, _top, right, _bottom in regions))

    def test_append_formula_snippets_adds_local_candidates(self) -> None:
        text = ocr._append_formula_snippets("设二维随机变量 (X,Y)", [r"D=\{(x,y)\mid 0\le x\le 3\}"])

        self.assertIn("[公式候选（局部 OCR）]", text)
        self.assertIn(r"D=\{(x,y)\mid 0\le x\le 3\}", text)

    def test_tesseract_image_output_appends_formula_snippets(self) -> None:
        engine = ocr.TesseractEngine.__new__(ocr.TesseractEngine)
        engine._tesseract = SimpleNamespace(image_to_string=lambda image, lang, config="": "设二维随机变量")
        engine._lang = "chi_sim+eng"

        with patch.object(ocr, "_extract_formula_snippets_from_image", return_value=[r"U=(X+Y)^2"]):
            text = engine._recognize_image(object(), include_formula_snippets=True)

        self.assertIn("设二维随机变量", text)
        self.assertIn(r"U=(X+Y)^2", text)

    def test_formula_command_engine_uses_temp_image_file(self) -> None:
        with (
            patch.object(ocr, "_ocr_formula_command", return_value="pix2tex {image}"),
            patch.object(ocr, "_run_formula_ocr_file", return_value=r"\frac{a}{b}") as run_formula,
        ):
            engine = ocr.FormulaOcrCommandEngine()
            text = engine.extract_image(b"\x89PNG\r\n\x1a\nimage")

        self.assertEqual(text, r"\frac{a}{b}")
        temp_path = run_formula.call_args.args[0]
        self.assertFalse(temp_path.exists())

    def test_extract_image_ocr_scores_formula_engine_against_text_engine(self) -> None:
        formula = SimpleNamespace(name="formula-command", extract_image=lambda data: r"\frac{x^2}{2}")
        tesseract = SimpleNamespace(name="tesseract", extract_image=lambda data: "x 2")

        with patch.object(ocr, "_ocr_engine_candidates", return_value=([tesseract, formula], [])):
            text = ocr.extract_image_ocr(b"\x89PNG")

        self.assertEqual(text, r"\frac{x^2}{2}")

    def test_extract_image_ocr_rejects_formula_engine_noise(self) -> None:
        formula = SimpleNamespace(
            name="formula-command",
            extract_image=lambda data: r"\mathrm{ig}\mathrm{ij}\mathrm{sig}\mathrm{zil}\mathrm{}\mathrm{}\bar{\zeta}",
        )
        tesseract = SimpleNamespace(name="tesseract", extract_image=lambda data: "设二维随机变量 (X,Y)")

        with patch.object(ocr, "_ocr_engine_candidates", return_value=([formula, tesseract], [])):
            text = ocr.extract_image_ocr(b"\x89PNG")

        self.assertEqual(text, "设二维随机变量 (X,Y)")

    def test_android_mlkit_engine_uses_java_bridge(self) -> None:
        class FakeBridge:
            @staticmethod
            def isAvailable() -> bool:
                return True

            @staticmethod
            def recognizePdf(data: bytes) -> str:
                return f" pdf {len(data)} "

            @staticmethod
            def recognizeImage(data: bytes) -> str:
                return f" image {len(data)} "

        java_module = ModuleType("java")
        setattr(java_module, "jclass", lambda name: FakeBridge)

        with patch.dict(sys.modules, {"java": java_module}):
            engine = ocr.AndroidMlKitEngine()

        self.assertEqual(engine.extract(b"%PDF"), "pdf 4")
        self.assertEqual(engine.extract_image(b"\x89PNG"), "image 4")

    def test_select_ocr_engine_prefers_android_bridge_in_apk(self) -> None:
        fake_engine = SimpleNamespace(name="android")

        with (
            patch.dict(os.environ, {"DEEPSEEK_ANDROID_APP": "1"}),
            patch.object(
                ocr,
                "DeepSeekApiOcrEngine",
                side_effect=AppError("no key", code=ErrorCode.OCR_UNAVAILABLE, status=415),
            ),
            patch.object(ocr, "AndroidMlKitEngine", return_value=fake_engine),
            patch.object(ocr, "TesseractEngine") as tesseract,
        ):
            engine = ocr.select_ocr_engine()

        self.assertIs(engine, fake_engine)
        tesseract.assert_not_called()

    def test_windows_ocr_engine_uses_temp_image_file(self) -> None:
        with (
            patch.object(ocr.os, "name", "nt"),
            patch.object(ocr, "_powershell_path", return_value="powershell.exe"),
            patch.object(ocr, "_run_windows_ocr_file", return_value=" windows text ") as run_ocr,
        ):
            engine = ocr.WindowsOcrEngine()
            text = engine.extract_image(b"\x89PNG\r\n\x1a\nimage")

        self.assertEqual(text, "windows text")
        temp_path = run_ocr.call_args.args[0]
        self.assertFalse(temp_path.exists())

    def test_powershell_path_uses_windows_system_fallback(self) -> None:
        with (
            patch.object(ocr.os, "name", "nt"),
            patch.dict(ocr.os.environ, {"SystemRoot": r"C:\Windows"}, clear=True),
            patch.object(ocr.shutil, "which", return_value=None),
            patch.object(
                ocr.os.path,
                "isfile",
                lambda path: str(path).endswith(r"System32\WindowsPowerShell\v1.0\powershell.exe"),
            ),
        ):
            path = ocr._powershell_path()

        self.assertEqual(path, r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")

    def test_select_ocr_engine_falls_back_to_windows_ocr_on_desktop_windows(self) -> None:
        fake_engine = SimpleNamespace(name="windows")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(
                ocr,
                "DeepSeekApiOcrEngine",
                side_effect=AppError("no key", code=ErrorCode.OCR_UNAVAILABLE, status=415),
            ),
            patch.object(ocr.os, "name", "nt"),
            patch.object(
                ocr,
                "TesseractEngine",
                side_effect=AppError("missing tesseract", code=ErrorCode.OCR_UNAVAILABLE, status=415),
            ),
            patch.object(ocr, "WindowsOcrEngine", return_value=fake_engine),
        ):
            engine = ocr.select_ocr_engine()

        self.assertIs(engine, fake_engine)


if __name__ == "__main__":
    unittest.main()
