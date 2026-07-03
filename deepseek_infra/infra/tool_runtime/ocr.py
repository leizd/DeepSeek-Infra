"""Optional local OCR extraction for scanned PDFs and images."""

from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from deepseek_infra.core.config import DEEPSEEK_TIMEOUT_SECONDS, DEEPSEEK_URL, settings
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import format_upstream_error

# OCR 输入预处理 / 渲染参数。提高印刷体识别率的两个关键：足够分辨率 + 二值化。
OCR_PDF_DPI = 300  # PDF 渲染 DPI（原 200 偏低；300 显著提升小字 / 公式识别）
OCR_UPSCALE_TARGET = 1600  # 预处理时把图像短边上采样到的目标像素（小图放大利于 OCR）
OCR_UPSCALE_MAX = 3.0  # 单次放大倍数上限，避免超大图爆内存
OCR_MODES = {"fast", "balanced", "quality"}
OCR_DEFAULT_MODE = "balanced"
OCR_FORMULA_LANGUAGE = "equ"
DEEPSEEK_OCR_MODEL = "deepseek-v4-pro"
DEEPSEEK_OCR_PROMPT = (
    "You are an OCR engine. Transcribe all visible text from the image exactly. "
    "Preserve line breaks, table-like spacing, labels, punctuation, and math. "
    "Use LaTeX only when it is the clearest way to preserve formulas. "
    "Return only the transcription. If there is no readable text, return an empty response."
)
DEEPSEEK_EMPTY_OCR_RESPONSES = {
    "",
    "no readable text",
    "no text",
    "no text found",
    "no text detected",
    "there is no readable text",
    "there is no readable text in the image",
    "empty",
    "none",
    "n/a",
}
MATH_SYMBOLS = set("=+-*/^_()[]{}<>|.,:;∑∏∫√∞≈≠≤≥±×÷·⋅∘∂∇∆∈∉⊂⊆⊄⊇∪∩∀∃→←↔⇒⇔∴∵′″°πθλμσΣΠΩαβγδεφψω")
_FORMULA_COMMAND_CANDIDATES = (
    ("pix2tex", "pix2tex {image}"),
    ("latexocr", "latexocr {image}"),
)
_FORMULA_SNIPPET_HEADER = "公式候选（局部 OCR）"
_ACCEPTED_MATHRM_WORDS = {
    "arg",
    "cov",
    "deg",
    "det",
    "dim",
    "dx",
    "dy",
    "dz",
    "exp",
    "gcd",
    "inf",
    "ker",
    "lcm",
    "lim",
    "ln",
    "log",
    "max",
    "min",
    "mod",
    "rank",
    "sgn",
    "sin",
    "span",
    "sup",
    "tan",
    "tr",
    "var",
}

_WINDOWS_TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    r"D:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Tesseract-OCR\tesseract.exe",
)

_WINDOWS_OCR_PS = r"""
param([string]$Path)
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrResult, Windows.Foundation, ContentType=WindowsRuntime]
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$script:asTaskMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() |
  Where-Object {
    $_.Name -eq "AsTask" -and
    $_.IsGenericMethod -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
  } |
  Select-Object -First 1
function Await-Operation($Operation, [Type]$ResultType) {
  $task = $script:asTaskMethod.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
  $task.Wait()
  return $task.Result
}
$stream = $null
try {
  $file = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
  $stream = Await-Operation ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await-Operation ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if ($null -eq $engine) {
    throw "Windows OCR engine is not available for current user languages."
  }
  $result = Await-Operation ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  [Console]::Write($result.Text)
} finally {
  if ($null -ne $stream) {
    $stream.Dispose()
  }
}
"""


def _locate_tesseract() -> str | None:
    override = os.environ.get("TESSERACT_CMD", "").strip()
    if override and Path(override).is_file():
        return override

    found = shutil.which("tesseract")
    if found:
        return found

    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "")
        candidates = list(_WINDOWS_TESSERACT_CANDIDATES)
        if local_app:
            candidates.append(str(Path(local_app) / "Programs" / "Tesseract-OCR" / "tesseract.exe"))
        for candidate in candidates:
            if Path(candidate).is_file():
                return candidate
    return None


def _select_lang(available: set[str]) -> str:
    override = os.environ.get("OCR_LANG", "").strip()
    if override:
        wanted = [part for part in override.replace(",", "+").split("+") if part]
        usable = [part for part in wanted if part in available]
        if usable:
            return "+".join(usable)

    preferred = ["chi_sim", "chi_tra", "eng", OCR_FORMULA_LANGUAGE, "jpn", "kor"]
    picked = [code for code in preferred if code in available]
    if picked:
        return "+".join(picked)
    if available:
        return next(iter(available))
    return "eng"


def _normalize_ocr_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in OCR_MODES else OCR_DEFAULT_MODE


def _ocr_settings() -> object:
    return getattr(settings, "ocr", object())


def _ocr_mode() -> str:
    return _normalize_ocr_mode(getattr(_ocr_settings(), "mode", OCR_DEFAULT_MODE))


def _ocr_pdf_dpi() -> int:
    try:
        value = int(getattr(_ocr_settings(), "pdf_dpi", OCR_PDF_DPI))
    except (TypeError, ValueError):
        value = OCR_PDF_DPI
    return min(450, max(150, value))


def _ocr_max_image_pixels() -> int:
    try:
        value = int(getattr(_ocr_settings(), "max_image_pixels", 16_000_000))
    except (TypeError, ValueError):
        value = 16_000_000
    return max(1, value)


def _ocr_formula_timeout_seconds() -> int:
    try:
        value = int(getattr(_ocr_settings(), "formula_timeout_seconds", 120))
    except (TypeError, ValueError):
        value = 120
    return min(600, max(5, value))


def _ocr_formula_command() -> str:
    override = str(getattr(_ocr_settings(), "formula_cmd", "") or "").strip()
    if override:
        return override
    for executable, template in _FORMULA_COMMAND_CANDIDATES:
        if shutil.which(executable):
            return template
    return ""


def _tesseract_configs_for_mode(mode: str | None = None) -> tuple[str, ...]:
    normalized = _normalize_ocr_mode(mode or _ocr_mode())
    preserve_spaces = "-c preserve_interword_spaces=1"
    if normalized == "fast":
        return (f"--psm 6 {preserve_spaces}",)
    if normalized == "quality":
        return (
            f"--psm 6 {preserve_spaces}",
            f"--psm 11 {preserve_spaces}",
            f"--psm 7 {preserve_spaces}",
            f"--psm 13 {preserve_spaces}",
            f"--psm 4 {preserve_spaces}",
            f"--psm 3 {preserve_spaces}",
        )
    return (
        f"--psm 6 {preserve_spaces}",
        f"--psm 11 {preserve_spaces}",
        f"--psm 7 {preserve_spaces}",
    )


def _powershell_path() -> str | None:
    found = shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh.exe") or shutil.which("pwsh")
    if found:
        return found
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows")
        for candidate in (
            system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
            system_root / "SysWOW64" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return None


def _subprocess_creationflags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def _image_suffix(image_bytes: bytes) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if image_bytes.startswith(b"\xff\xd8"):
        return ".jpg"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return ".webp"
    if image_bytes.startswith((b"II*\x00", b"MM\x00*")):
        return ".tif"
    if image_bytes.startswith(b"GIF8"):
        return ".gif"
    if image_bytes.startswith(b"BM"):
        return ".bmp"
    return ".img"


def _image_media_type(image_bytes: bytes) -> str:
    suffix = _image_suffix(image_bytes)
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".webp": "image/webp",
        ".tif": "image/tiff",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(suffix, "image/png")


def _image_data_url(image_bytes: bytes) -> str:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{_image_media_type(image_bytes)};base64,{encoded}"


def _pil_image_png_bytes(image: object) -> bytes:
    limited = _limit_pil_image_pixels(image)
    mode = str(getattr(limited, "mode", "") or "")
    if mode and mode not in {"RGB", "RGBA", "L"} and callable(getattr(limited, "convert", None)):
        limited = limited.convert("RGB")  # type: ignore[attr-defined]
    buffer = io.BytesIO()
    limited.save(buffer, "PNG")  # type: ignore[attr-defined]
    return buffer.getvalue()


def _limit_pil_image_pixels(image: object) -> object:
    try:
        from PIL import Image
    except ModuleNotFoundError:
        return image

    if not isinstance(image, Image.Image):
        return image
    width, height = image.size
    pixels = max(1, width * height)
    max_pixels = _ocr_max_image_pixels()
    if pixels <= max_pixels:
        return image

    scale = math.sqrt(max_pixels / float(pixels))
    target = (max(1, int(width * scale)), max(1, int(height * scale)))
    resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", getattr(Image, "BICUBIC", 3))
    return image.resize(target, resampling)


def _upscale_gray_for_ocr(gray: Any, cv2: Any) -> Any:
    height, width = gray.shape
    short_side = min(height, width)
    if 0 < short_side < OCR_UPSCALE_TARGET:
        scale = min(OCR_UPSCALE_MAX, OCR_UPSCALE_TARGET / short_side)
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return gray


def _white_background(binary: Any, cv2: Any, np: Any) -> Any:
    # Tesseract 期望黑字白底；若白像素不足一半（深色背景白字截图），整体反相。
    return cv2.bitwise_not(binary) if float(np.mean(binary)) < 127.0 else binary


def _adaptive_block_size(gray: Any) -> int:
    height, width = gray.shape
    block = max(15, min(height, width) // 18)
    block = min(75, block)
    return block + 1 if block % 2 == 0 else block


def _deskew_gray(gray: Any, cv2: Any, np: Any) -> Any | None:
    try:
        coords = np.column_stack(np.where(gray < 245))
        if len(coords) < 24:
            return None
        angle = float(cv2.minAreaRect(coords)[-1])
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
        if abs(angle) < 0.35 or abs(angle) > 12:
            return None
        height, width = gray.shape
        center = (width // 2, height // 2)
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        return cv2.warpAffine(gray, matrix, (width, height), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    except Exception:
        return None


def _arrays_to_unique_images(arrays: list[Any], Image: Any) -> list[Any]:
    images: list[Any] = []
    seen: set[bytes] = set()
    for array in arrays:
        try:
            key = bytes(array[:64, :64].tobytes())
        except Exception:
            key = bytes(str(id(array)), "ascii")
        if key in seen:
            continue
        seen.add(key)
        images.append(Image.fromarray(array))
    return images


def _preprocess_candidates_for_ocr(image: object, *, mode: str | None = None) -> list[object]:
    """Return local OpenCV OCR enhancement candidates for Tesseract.

    Missing optional dependencies or malformed placeholder images return an empty
    list so callers can keep using the original image.
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except ModuleNotFoundError:
        return []

    try:
        if not isinstance(image, Image.Image):
            return []
        normalized_mode = _normalize_ocr_mode(mode or _ocr_mode())
        limited = _limit_pil_image_pixels(image)
        if not isinstance(limited, Image.Image):
            return []
        gray = np.array(limited.convert("L"))
        if gray.ndim != 2 or gray.size == 0:
            return []

        gray = _upscale_gray_for_ocr(gray, cv2)
        denoised = cv2.bilateralFilter(gray, 5, 50, 50)
        candidates: list[object] = []

        _, otsu = cv2.threshold(denoised, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        candidates.append(_white_background(otsu, cv2, np))

        if normalized_mode in {"balanced", "quality"}:
            block = _adaptive_block_size(denoised)
            adaptive = cv2.adaptiveThreshold(
                denoised,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                block,
                11,
            )
            candidates.append(_white_background(adaptive, cv2, np))

            equalized = cv2.equalizeHist(gray)
            _, contrast_binary = cv2.threshold(equalized, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            candidates.append(_white_background(contrast_binary, cv2, np))

        if normalized_mode == "quality":
            deskewed = _deskew_gray(denoised, cv2, np)
            if deskewed is not None:
                _, deskewed_binary = cv2.threshold(deskewed, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                candidates.append(_white_background(deskewed_binary, cv2, np))

        return _arrays_to_unique_images(candidates, Image)
    except Exception:
        return []


def _preprocess_for_ocr(image: object) -> object | None:
    """Backward-compatible single-candidate wrapper for older tests/callers."""
    candidates = _preprocess_candidates_for_ocr(image, mode=_ocr_mode())
    return candidates[0] if candidates else None


def normalize_ocr_text(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines: list[str] = []
    blank = False
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            if lines and not blank:
                lines.append("")
                blank = True
            continue
        line = _normalize_formula_symbols(line)
        if _looks_like_formula_line(line):
            line = re.sub(r"[ \t]{2,}", " ", line)
        else:
            # Wide whitespace often indicates columns in screenshots or forms. Keep
            # it as a tab instead of collapsing table-like OCR into one sentence.
            line = re.sub(r"[ \t]{3,}", "\t", line)
            line = re.sub(r"[ \t]{2}", " ", line)
        if _looks_like_ocr_noise(line):
            continue
        lines.append(line)
        blank = False
    return "\n".join(lines).strip()


def _normalize_formula_symbols(line: str) -> str:
    if not any(char in line for char in "＝＋－＊／（）［］｛｝｜，．"):
        return line
    translations: dict[str, str | int | None] = {
        "＝": "=",
        "＋": "+",
        "－": "-",
        "＊": "*",
        "／": "/",
        "（": "(",
        "）": ")",
        "［": "[",
        "］": "]",
        "｛": "{",
        "｝": "}",
        "｜": "|",
        "，": ",",
        "．": ".",
    }
    return line.translate(str.maketrans(translations))


def _math_symbol_count(line: str) -> int:
    return sum(1 for char in line if char in MATH_SYMBOLS)


def _looks_like_formula_line(line: str) -> bool:
    value = str(line or "").strip()
    if not value:
        return False
    math_symbols = _math_symbol_count(value)
    if "\\" in value and re.search(r"\\[A-Za-z]+", value):
        return True
    if math_symbols >= 2 and re.search(r"[A-Za-z0-9\u0370-\u03ff]", value):
        return True
    if re.search(r"[A-Za-z]\s*[_^]\s*[A-Za-z0-9({]", value):
        return True
    if re.search(r"(sin|cos|tan|log|ln|lim|max|min)\s*[\(_{]", value, flags=re.IGNORECASE):
        return True
    return any(char in value for char in "∑∏∫√∞≈≠≤≥±×÷∂∇∆")


def _looks_like_ocr_noise(line: str) -> bool:
    if _looks_like_formula_line(line):
        return False
    readable = sum(1 for char in line if char.isalnum() or "\u4e00" <= char <= "\u9fff")
    math_symbols = _math_symbol_count(line)
    strong_math = any(char in line for char in "∑∏∫√∞≈≠≤≥±×÷∂∇∆")
    if readable == 0 and len(line) <= 3 and not strong_math:
        return True
    if len(line) >= 8 and (readable + math_symbols) / max(1, len(line)) < 0.12:
        return True
    return False


def _ocr_text_score(text: str) -> int:
    cleaned = normalize_ocr_text(text)
    if not cleaned:
        return 0
    readable = sum(1 for char in cleaned if char.isalnum() or "\u4e00" <= char <= "\u9fff")
    math_symbols = _math_symbol_count(cleaned)
    formula_lines = sum(1 for line in cleaned.splitlines() if _looks_like_formula_line(line))
    separators = sum(1 for char in cleaned if char in "\n\t:：,，.;；()（）[]【】")
    noise = cleaned.count("\ufffd") * 8 + len(re.findall(r"[_~^`|]{3,}", cleaned)) * 5
    return readable * 4 + math_symbols * 3 + formula_lines * 16 + separators - noise


def _run_windows_ocr_file(path: Path) -> str:
    powershell = _powershell_path()
    if not powershell:
        raise AppError("PowerShell is required for Windows OCR.", code=ErrorCode.OCR_UNAVAILABLE, status=415)

    script_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", encoding="utf-8-sig", delete=False) as script:
            script.write(_WINDOWS_OCR_PS)
            script_path = Path(script.name)
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            creationflags=_subprocess_creationflags(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError("Windows OCR timed out.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc
    finally:
        if script_path is not None:
            script_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        message = "Windows OCR failed."
        if detail:
            message = f"{message} {detail[:500]}"
        raise AppError(message, code=ErrorCode.OCR_UNAVAILABLE, status=415)
    return completed.stdout.strip()


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _formula_command_args(template: str, image_path: Path) -> list[str]:
    try:
        args = shlex.split(template, posix=os.name != "nt")
    except ValueError as exc:
        raise AppError("Formula OCR command is invalid.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc
    if not args:
        raise AppError("Formula OCR command is empty.", code=ErrorCode.OCR_UNAVAILABLE, status=415)

    image_text = str(image_path)
    has_placeholder = any("{image}" in arg for arg in args)
    replaced = [_strip_matching_quotes(arg.replace("{image}", image_text)) for arg in args]
    return replaced if has_placeholder else [*replaced, image_text]


def _formula_output_from_json(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("latex", "formula", "text", "result", "prediction", "output"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item
        for item in value.values():
            nested = _formula_output_from_json(item)
            if nested:
                return nested
    if isinstance(value, list):
        parts = [_formula_output_from_json(item).strip() for item in value]
        return "\n".join(part for part in parts if part)
    return ""


def _looks_like_latex_word_noise(value: str) -> bool:
    token = re.sub(r"[^A-Za-z]", "", value).lower()
    if len(token) < 6:
        return False
    if re.search(r"([a-z])\1{2,}", token):
        return True
    if token.count("f") >= max(4, int(len(token) * 0.45)):
        return True
    if token.count("f") >= 3 and not re.search(r"[aeou]", token):
        return True
    return False


def _formula_ocr_output_is_credible(value: str) -> bool:
    text = normalize_ocr_text(value)
    if not text:
        return False

    compact = re.sub(r"\s+", "", text)
    empty_mathrm_count = len(re.findall(r"\\mathrm\{\s*\}", text))
    if empty_mathrm_count >= 2:
        return False
    if re.search(r"([A-Za-z])(?:\s*\1){8,}", compact):
        return False

    math_symbols = _math_symbol_count(text)
    structural_commands = re.findall(
        r"\\(?:frac|dfrac|tfrac|sqrt|sum|prod|int|lim|begin|end|over|under|left|right|"
        r"alpha|beta|gamma|delta|theta|lambda|mu|sigma|pi|omega|Omega|Sigma|Pi)\b",
        text,
    )
    if not _looks_like_formula_line(text) and math_symbols < 2:
        return False

    payloads = re.findall(r"\\mathrm\{([^{}]*)\}", text)
    noisy_payloads = [payload for payload in payloads if _looks_like_latex_word_noise(payload)]
    if len(noisy_payloads) >= 2 or (noisy_payloads and len(compact) > 80):
        return False
    short_alpha_payloads: list[str] = []
    for payload in payloads:
        token = re.sub(r"[^A-Za-z]", "", payload).lower()
        if 1 <= len(token) <= 4:
            short_alpha_payloads.append(token)
    odd_short_payloads = [
        token
        for token in short_alpha_payloads
        if token not in _ACCEPTED_MATHRM_WORDS and not (len(token) == 1 and token in {"d", "e", "i"})
    ]
    if len(payloads) >= 6 and len(odd_short_payloads) >= 4:
        return False
    if len(payloads) >= 4 and len(odd_short_payloads) == len(short_alpha_payloads) and not structural_commands:
        return False
    if len(compact) > 160 and len(odd_short_payloads) >= 3 and len(structural_commands) <= 2:
        return False
    if text.count(r"\bar{") >= 8 and len(compact) > 120:
        return False

    # pix2tex tends to hallucinate long streams of \mathrm{ffff...} on full
    # screenshots that mix prose and formulas. Formatting wrappers alone are
    # weak evidence, so require stronger math structure for long outputs.
    command_names = re.findall(r"\\([A-Za-z]+)", text)
    weak_commands = {"mathrm", "mathbf", "mathit", "mathsf", "mathcal", "quad", "qquad", "text"}
    wrapper_commands = {"mathrm", "mathbf", "mathit", "mathsf", "mathcal", "bar"}
    wrapper_count = sum(1 for command in command_names if command in wrapper_commands)
    core_commands = [command for command in command_names if command not in weak_commands | {"bar", "begin", "end", "left", "right"}]
    if len(compact) > 180 and wrapper_count >= 12 and len(core_commands) <= 2:
        return False
    if len(compact) > 220 and command_names and set(command_names).issubset(weak_commands):
        return False
    if len(compact) > 300 and not structural_commands and len(payloads) >= 3:
        return False

    return True


def _clean_formula_ocr_output(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        json_text = _formula_output_from_json(parsed).strip()
        if json_text:
            text = json_text

    fence = re.fullmatch(r"```(?:latex|tex|math)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.match(r"^[A-Za-z]:[\\/].+\.(?:png|jpe?g|webp|bmp|tiff?|gif)\s*:\s+", line, flags=re.IGNORECASE):
            line = line.split(": ", 1)[1].strip()
        line = re.sub(r"^(?:latex|formula|result|prediction|output)\s*[:：]\s*", "", line, flags=re.IGNORECASE)
        if line:
            lines.append(line)
    cleaned = normalize_ocr_text("\n".join(lines))
    return cleaned if _formula_ocr_output_is_credible(cleaned) else ""


def _formula_command_args_for_images(template: str, image_paths: list[Path]) -> list[str] | None:
    try:
        args = shlex.split(template, posix=os.name != "nt")
    except ValueError as exc:
        raise AppError("Formula OCR command is invalid.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc
    if not args:
        raise AppError("Formula OCR command is empty.", code=ErrorCode.OCR_UNAVAILABLE, status=415)

    image_texts = [str(path) for path in image_paths]
    has_placeholder = any("{image}" in arg for arg in args)
    replaced: list[str] = []
    if has_placeholder:
        for arg in args:
            if arg == "{image}":
                replaced.extend(image_texts)
            elif "{image}" in arg:
                if len(image_texts) != 1:
                    return None
                replaced.append(_strip_matching_quotes(arg.replace("{image}", image_texts[0])))
            else:
                replaced.append(arg)
        return replaced
    return [*args, *image_texts]


def _split_formula_batch_output(stdout: str, image_paths: list[Path]) -> list[str]:
    path_texts = [str(path) for path in image_paths]
    by_path: dict[str, str] = {}
    loose_lines: list[str] = []
    for raw_line in str(stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        matched = False
        for path_text in path_texts:
            prefix = f"{path_text}:"
            if line.startswith(prefix):
                by_path[path_text] = line[len(prefix) :].strip()
                matched = True
                break
        if not matched:
            loose_lines.append(line)

    if by_path:
        return [_clean_formula_ocr_output(by_path.get(path_text, "")) for path_text in path_texts]
    if len(loose_lines) == len(image_paths):
        return [_clean_formula_ocr_output(line) for line in loose_lines]
    if len(image_paths) == 1:
        return [_clean_formula_ocr_output("\n".join(loose_lines))]
    return []


def _run_formula_ocr_files(paths: list[Path], command_template: str) -> list[str]:
    if not paths:
        return []
    if len(paths) == 1:
        return [_run_formula_ocr_file(paths[0], command_template)]

    args = _formula_command_args_for_images(command_template, paths)
    if args is None:
        return [_run_formula_ocr_file(path, command_template) for path in paths]
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=min(600, _ocr_formula_timeout_seconds() + 10 * len(paths)),
            creationflags=_subprocess_creationflags(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise AppError("Formula OCR command was not found.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError("Formula OCR command timed out.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        message = "Formula OCR command failed."
        if detail:
            message = f"{message} {detail[:500]}"
        raise AppError(message, code=ErrorCode.OCR_UNAVAILABLE, status=415)
    return _split_formula_batch_output(completed.stdout, paths)


def _run_formula_ocr_file(path: Path, command_template: str) -> str:
    args = _formula_command_args(command_template, path)
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_ocr_formula_timeout_seconds(),
            creationflags=_subprocess_creationflags(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise AppError("Formula OCR command was not found.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc
    except subprocess.TimeoutExpired as exc:
        raise AppError("Formula OCR command timed out.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        message = "Formula OCR command failed."
        if detail:
            message = f"{message} {detail[:500]}"
        raise AppError(message, code=ErrorCode.OCR_UNAVAILABLE, status=415)
    return _clean_formula_ocr_output(completed.stdout)


def _looks_like_formula_ocr_token(value: str) -> bool:
    text = str(value or "").strip()
    if len(text) < 1:
        return False
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_digits = len(re.findall(r"[A-Za-z0-9]", text))
    math_marks = len(re.findall(r"[=+\-*/^_(){}\[\]|,.:;<>≤≥]", text))
    if latin_digits == 0 and math_marks == 0:
        return False
    if cjk and cjk > (latin_digits + math_marks) * 2:
        return False
    if len(text) == 1 and text.isalpha() and text not in {"D", "U", "V", "X", "Y", "x", "y"}:
        return False
    return True


def _int_from_ocr_data(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def _formula_regions_from_tesseract_data(
    data: dict[str, list[object]],
    image_size: tuple[int, int],
    *,
    max_regions: int = 10,
) -> list[tuple[int, int, int, int]]:
    texts = data.get("text", [])
    width, height = image_size
    lines: dict[tuple[int, int, int], list[tuple[int, int, int, int, str]]] = {}
    for index, raw_text in enumerate(texts):
        text = str(raw_text or "").strip()
        if not _looks_like_formula_ocr_token(text):
            continue
        left = _int_from_ocr_data(data.get("left", [])[index] if index < len(data.get("left", [])) else 0)
        top = _int_from_ocr_data(data.get("top", [])[index] if index < len(data.get("top", [])) else 0)
        word_width = _int_from_ocr_data(data.get("width", [])[index] if index < len(data.get("width", [])) else 0)
        word_height = _int_from_ocr_data(data.get("height", [])[index] if index < len(data.get("height", [])) else 0)
        if word_width <= 2 or word_height <= 2:
            continue
        key = (
            _int_from_ocr_data(data.get("block_num", [])[index] if index < len(data.get("block_num", [])) else 0),
            _int_from_ocr_data(data.get("par_num", [])[index] if index < len(data.get("par_num", [])) else 0),
            _int_from_ocr_data(data.get("line_num", [])[index] if index < len(data.get("line_num", [])) else 0),
        )
        lines.setdefault(key, []).append((left, top, left + word_width, top + word_height, text))

    regions: list[tuple[int, int, int, int]] = []
    for entries in lines.values():
        entries.sort(key=lambda item: item[0])
        current: list[tuple[int, int, int, int, str]] = []
        for entry in entries:
            if not current:
                current = [entry]
                continue
            previous = current[-1]
            median_height = max(1, int(sum(item[3] - item[1] for item in current) / len(current)))
            gap = entry[0] - previous[2]
            if gap <= max(18, int(median_height * 1.3)):
                current.append(entry)
            else:
                region = _region_from_formula_words(current, image_size)
                if region is not None:
                    regions.append(region)
                current = [entry]
        region = _region_from_formula_words(current, image_size)
        if region is not None:
            regions.append(region)

    unique: list[tuple[int, int, int, int]] = []
    seen: set[tuple[int, int, int, int]] = set()
    for region in sorted(regions, key=lambda item: (item[1], item[0])):
        if region in seen:
            continue
        seen.add(region)
        box_width = region[2] - region[0]
        box_height = region[3] - region[1]
        if box_width > width * 0.82 and box_height > height * 0.25:
            continue
        unique.append(region)
        if len(unique) >= max_regions:
            break
    return unique


def _region_from_formula_words(
    entries: list[tuple[int, int, int, int, str]],
    image_size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    if not entries:
        return None
    width, height = image_size
    left = min(item[0] for item in entries)
    top = min(item[1] for item in entries)
    right = max(item[2] for item in entries)
    bottom = max(item[3] for item in entries)
    box_width = right - left
    box_height = bottom - top
    text = "".join(item[4] for item in entries)
    if box_width < 8 or box_height < 6:
        return None
    if len(text) == 1 and box_width < box_height:
        return None
    pad_x = max(4, int(box_height * 0.35))
    pad_y = max(3, int(box_height * 0.25))
    return (max(0, left - pad_x), max(0, top - pad_y), min(width, right + pad_x), min(height, bottom + pad_y))


def _extract_formula_snippets_from_image(image: object, tesseract: Any, lang: str) -> list[str]:
    command = _ocr_formula_command()
    if not command or _ocr_mode() == "fast":
        return []
    try:
        from PIL import Image
    except ModuleNotFoundError:
        return []
    if not isinstance(image, Image.Image) or not callable(getattr(tesseract, "image_to_data", None)):
        return []
    output = getattr(getattr(tesseract, "Output", object()), "DICT", None)
    if output is None:
        return []

    try:
        limited = _limit_pil_image_pixels(image)
        if not isinstance(limited, Image.Image):
            return []
        data = tesseract.image_to_data(limited, lang=lang, config="--psm 6", output_type=output)
        regions = _formula_regions_from_tesseract_data(data, limited.size)
        if not regions:
            return []

        paths: list[Path] = []
        with tempfile.TemporaryDirectory(prefix="deepseek-formula-") as tmpdir:
            tmp = Path(tmpdir)
            for index, region in enumerate(regions):
                crop = limited.crop(region)
                path = tmp / f"formula-{index}.png"
                crop.save(path, "PNG")
                paths.append(path)
            raw_snippets = _run_formula_ocr_files(paths, command)
    except Exception:
        return []

    snippets: list[str] = []
    seen: set[str] = set()
    for raw in raw_snippets:
        snippet = _clean_formula_ocr_output(raw)
        compact = re.sub(r"\s+", "", snippet)
        if not snippet or compact in seen:
            continue
        seen.add(compact)
        snippets.append(snippet)
    return snippets[:8]


def _append_formula_snippets(text: str, snippets: list[str]) -> str:
    base = normalize_ocr_text(text)
    if not snippets:
        return base
    compact_base = re.sub(r"\s+", "", base)
    lines: list[str] = []
    seen: set[str] = set()
    for snippet in snippets:
        cleaned = _clean_formula_ocr_output(snippet)
        compact = re.sub(r"\s+", "", cleaned)
        if not cleaned or compact in seen or compact in compact_base:
            continue
        seen.add(compact)
        lines.append(f"- {cleaned}")
    if not lines:
        return base
    prefix = f"{base}\n\n" if base else ""
    return f"{prefix}[{_FORMULA_SNIPPET_HEADER}]\n" + "\n".join(lines)


class OCREngine(Protocol):
    name: str

    def extract(self, pdf_bytes: bytes) -> str:
        ...

    def extract_image(self, image_bytes: bytes) -> str:
        ...


def _deepseek_ocr_body(data_url: str) -> dict[str, object]:
    return {
        "model": DEEPSEEK_OCR_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": DEEPSEEK_OCR_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        "stream": False,
    }


def _deepseek_ocr_request(api_key: str, body: dict[str, object]) -> urllib.request.Request:
    return urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )


def _deepseek_ocr_response_content(response_json: dict[str, object]) -> str:
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AppError("DeepSeek OCR returned no answer.", code=ErrorCode.OCR_UNAVAILABLE, status=502)
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ""
    message = first_choice.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(str(part["text"]))
        return "\n".join(parts)
    return ""


def _clean_deepseek_ocr_output(value: str) -> str:
    raw = str(value or "").strip()
    fence = re.fullmatch(r"```(?:[A-Za-z0-9_+-]+)?\s*\n(.*?)\n?```", raw, flags=re.DOTALL)
    if fence:
        raw = fence.group(1).strip()
    text = normalize_ocr_text(raw)
    compact = re.sub(r"[\s.。!！?？:：]+", " ", text).strip().strip("()[]{}").lower()
    return "" if compact in DEEPSEEK_EMPTY_OCR_RESPONSES else text


def _run_deepseek_ocr_image(image_bytes: bytes, api_key: str) -> str:
    request = _deepseek_ocr_request(api_key, _deepseek_ocr_body(_image_data_url(image_bytes)))
    try:
        with urllib.request.urlopen(request, timeout=DEEPSEEK_TIMEOUT_SECONDS) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AppError(
            f"DeepSeek OCR failed: {format_upstream_error(detail)}",
            code=ErrorCode.OCR_UNAVAILABLE,
            status=min(exc.code, 502),
        ) from exc
    except urllib.error.URLError as exc:
        raise AppError(f"Cannot reach DeepSeek API for OCR: {exc.reason}", code=ErrorCode.OCR_UNAVAILABLE, status=502) from exc
    except json.JSONDecodeError as exc:
        raise AppError("DeepSeek OCR returned invalid JSON.", code=ErrorCode.OCR_UNAVAILABLE, status=502) from exc

    if not isinstance(response_json, dict):
        raise AppError("DeepSeek OCR returned invalid JSON.", code=ErrorCode.OCR_UNAVAILABLE, status=502)
    text = _clean_deepseek_ocr_output(_deepseek_ocr_response_content(response_json))
    if not text:
        raise AppError("DeepSeek OCR did not recognize any text.", code=ErrorCode.OCR_EMPTY, status=422)
    return text


class DeepSeekApiOcrEngine:
    name = "deepseek-api"

    def __init__(self, api_key: str | None = None) -> None:
        api_key = str(api_key or getattr(settings, "deepseek_api_key", "") or "").strip()
        if not api_key:
            raise AppError("Missing DeepSeek API Key for OCR.", code=ErrorCode.OCR_UNAVAILABLE, status=415)
        self._api_key = api_key

    def extract(self, pdf_bytes: bytes) -> str:
        try:
            import pdf2image
        except ModuleNotFoundError as exc:
            raise AppError(
                "DeepSeek PDF OCR requires pdf2image and pdftoppm to render pages.",
                code=ErrorCode.OCR_UNAVAILABLE,
                status=415,
            ) from exc

        pages: list[str] = []
        try:
            images = pdf2image.convert_from_bytes(pdf_bytes, dpi=_ocr_pdf_dpi(), fmt="png")
            for index, image in enumerate(images, start=1):
                try:
                    text = self.extract_page_image(image)
                except AppError as exc:
                    if exc.code == ErrorCode.OCR_EMPTY:
                        continue
                    raise
                if text.strip():
                    pages.append(f"[PDF 第 {index} 页 (OCR)]\n{text.strip()}")
        except AppError:
            raise
        except Exception as exc:
            raise AppError("DeepSeek PDF OCR failed.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc
        return "\n\n".join(pages)

    def extract_page_image(self, image: object) -> str:
        try:
            return _run_deepseek_ocr_image(_pil_image_png_bytes(image), self._api_key)
        except AppError:
            raise
        except Exception as exc:
            raise AppError("DeepSeek PDF page OCR failed.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc

    def extract_image(self, image_bytes: bytes) -> str:
        return _run_deepseek_ocr_image(image_bytes, self._api_key)


class FormulaOcrCommandEngine:
    name = "formula-command"

    def __init__(self) -> None:
        command = _ocr_formula_command()
        if not command:
            raise AppError("Formula OCR command is not configured.", code=ErrorCode.OCR_UNAVAILABLE, status=415)
        self._command = command

    def extract(self, pdf_bytes: bytes) -> str:
        try:
            import pdf2image
        except ModuleNotFoundError as exc:
            raise AppError(
                "Formula PDF OCR requires pdf2image and pdftoppm.",
                code=ErrorCode.OCR_UNAVAILABLE,
                status=415,
            ) from exc

        pages: list[str] = []
        try:
            images = pdf2image.convert_from_bytes(pdf_bytes, dpi=_ocr_pdf_dpi(), fmt="png")
            for index, image in enumerate(images, start=1):
                text = self.extract_page_image(image)
                if text.strip():
                    pages.append(f"[PDF 第 {index} 页 (OCR)]\n{text.strip()}")
        except AppError:
            raise
        except Exception as exc:
            raise AppError("Formula PDF OCR failed.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc
        return "\n\n".join(pages)

    def extract_page_image(self, image: object) -> str:
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", suffix=".png", delete=False) as image_file:
                path = Path(image_file.name)
            image.save(path, "PNG")  # type: ignore[attr-defined]
            return _run_formula_ocr_file(path, self._command)
        except AppError:
            raise
        except Exception as exc:
            raise AppError("Formula image OCR failed.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def extract_image(self, image_bytes: bytes) -> str:
        suffix = _image_suffix(image_bytes)
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False) as image_file:
                image_file.write(image_bytes)
                path = Path(image_file.name)
            return _run_formula_ocr_file(path, self._command)
        except AppError:
            raise
        except Exception as exc:
            raise AppError("Formula image OCR failed.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc
        finally:
            if path is not None:
                path.unlink(missing_ok=True)


class TesseractEngine:
    name = "tesseract"
    _lang: str = "eng"

    def __init__(self) -> None:
        try:
            import pdf2image
            import pytesseract
        except ModuleNotFoundError as exc:
            raise AppError(
                "OCR dependencies are not installed. Install requirements-ocr.txt and Tesseract.",
                code=ErrorCode.OCR_UNAVAILABLE,
                status=415,
            ) from exc

        executable = _locate_tesseract()
        if executable is None:
            raise AppError(
                "Tesseract executable not found. Install it (e.g. https://github.com/UB-Mannheim/tesseract/wiki) "
                "and either add it to PATH or set the TESSERACT_CMD environment variable.",
                code=ErrorCode.OCR_UNAVAILABLE,
                status=415,
            )

        pytesseract.pytesseract.tesseract_cmd = executable

        try:
            available = set(pytesseract.get_languages(config=""))
        except Exception:
            available = set()

        self._pdf2image: Any = pdf2image
        self._tesseract = pytesseract
        self._lang = _select_lang(available)

    def extract(self, pdf_bytes: bytes) -> str:
        images = self._pdf2image.convert_from_bytes(pdf_bytes, dpi=_ocr_pdf_dpi(), fmt="png")
        pages: list[str] = []
        for index, image in enumerate(images, start=1):
            text = self.extract_page_image(image)
            if text.strip():
                pages.append(f"[PDF 第 {index} 页 (OCR)]\n{text.strip()}")
        return "\n\n".join(pages)

    def extract_page_image(self, image: object) -> str:
        return self._recognize_image(image)

    def extract_image(self, image_bytes: bytes) -> str:
        try:
            from PIL import Image, ImageOps
        except ModuleNotFoundError as exc:
            raise AppError(
                "Image OCR dependencies are not installed. Install requirements-ocr.txt and Tesseract.",
                code=ErrorCode.OCR_UNAVAILABLE,
                status=415,
            ) from exc

        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                normalized = ImageOps.exif_transpose(image)
                if normalized.mode not in {"RGB", "L"}:
                    normalized = normalized.convert("RGB")
                text = self._recognize_image(normalized, include_formula_snippets=True)
        except AppError:
            raise
        except Exception as exc:
            raise AppError("Image OCR failed.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc

        return text.strip()

    def _recognize_image(self, image: object, *, include_formula_snippets: bool = False) -> str:
        mode = _ocr_mode()
        candidates = [_limit_pil_image_pixels(image)]
        candidates.extend(_preprocess_candidates_for_ocr(image, mode=mode))

        best_text = ""
        best_score = -1
        for candidate in candidates:
            for config in _tesseract_configs_for_mode(mode):
                try:
                    text = normalize_ocr_text(self._image_to_string(candidate, config=config))
                except Exception:
                    continue
                score = _ocr_text_score(text)
                if score > best_score:
                    best_text = text
                    best_score = score
                if mode == "fast" and score > 0:
                    return text
        if include_formula_snippets:
            snippets = _extract_formula_snippets_from_image(image, self._tesseract, self._lang)
            return _append_formula_snippets(best_text, snippets)
        return best_text

    def _image_to_string(self, image: object, *, config: str) -> str:
        try:
            return str(self._tesseract.image_to_string(image, lang=self._lang, config=config))
        except TypeError:
            # Some tests and lightweight stand-ins implement the old two-argument
            # pytesseract shape; keep those compatible.
            return str(self._tesseract.image_to_string(image, lang=self._lang))


class WindowsOcrEngine:
    name = "windows-ocr"

    def __init__(self) -> None:
        if os.name != "nt":
            raise AppError("Windows OCR is only available on Windows.", code=ErrorCode.OCR_UNAVAILABLE, status=415)
        if _powershell_path() is None:
            raise AppError("PowerShell is required for Windows OCR.", code=ErrorCode.OCR_UNAVAILABLE, status=415)

    def extract(self, pdf_bytes: bytes) -> str:
        try:
            import pdf2image
        except ModuleNotFoundError as exc:
            raise AppError(
                "PDF OCR on Windows requires pdf2image and pdftoppm.",
                code=ErrorCode.OCR_UNAVAILABLE,
                status=415,
            ) from exc

        pages: list[str] = []
        try:
            images = pdf2image.convert_from_bytes(pdf_bytes, dpi=_ocr_pdf_dpi(), fmt="png")
            for index, image in enumerate(images, start=1):
                text = self.extract_page_image(image)
                if text.strip():
                    pages.append(f"[PDF 第 {index} 页 (OCR)]\n{text.strip()}")
        except AppError:
            raise
        except Exception as exc:
            raise AppError("Windows PDF OCR failed.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc
        return "\n\n".join(pages)

    def extract_page_image(self, image: object) -> str:
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", suffix=".png", delete=False) as image_file:
                path = Path(image_file.name)
            image.save(path, "PNG")  # type: ignore[attr-defined]
            return normalize_ocr_text(_run_windows_ocr_file(path))
        except AppError:
            raise
        except Exception as exc:
            raise AppError("Windows PDF page OCR failed.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    def extract_image(self, image_bytes: bytes) -> str:
        suffix = _image_suffix(image_bytes)
        path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False) as image_file:
                image_file.write(image_bytes)
                path = Path(image_file.name)
            return normalize_ocr_text(_run_windows_ocr_file(path))
        except AppError:
            raise
        except Exception as exc:
            raise AppError("Windows image OCR failed.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc
        finally:
            if path is not None:
                path.unlink(missing_ok=True)


class AndroidMlKitEngine:
    name = "android-mlkit"

    def __init__(self) -> None:
        try:
            from java import jclass
        except (ImportError, ModuleNotFoundError) as exc:
            raise AppError(
                "Android OCR bridge is not available outside the APK.",
                code=ErrorCode.OCR_UNAVAILABLE,
                status=415,
            ) from exc

        bridge = jclass("com.deepseek.mobile.AndroidOcrBridge")
        if not bool(bridge.isAvailable()):
            raise AppError(
                "Android OCR bridge is not initialized.",
                code=ErrorCode.OCR_UNAVAILABLE,
                status=415,
            )
        self._bridge = bridge

    def extract(self, pdf_bytes: bytes) -> str:
        return normalize_ocr_text(str(self._bridge.recognizePdf(pdf_bytes)))

    def extract_image(self, image_bytes: bytes) -> str:
        return normalize_ocr_text(str(self._bridge.recognizeImage(image_bytes)))


def select_ocr_engine(api_key: str | None = None) -> OCREngine | None:
    engines, _errors = _ocr_engine_candidates(api_key=api_key)
    return engines[0] if engines else None


def _ocr_engine_candidates(api_key: str | None = None) -> tuple[list[OCREngine], list[str]]:
    engines: list[OCREngine] = []
    errors: list[str] = []

    try:
        engines.append(DeepSeekApiOcrEngine(api_key=api_key))
    except AppError as exc:
        errors.append(f"deepseek-api: {exc}")

    if os.environ.get("DEEPSEEK_ANDROID_APP") == "1":
        try:
            engines.append(AndroidMlKitEngine())
            return engines, errors
        except AppError as exc:
            errors.append(f"android-mlkit: {exc}")

    try:
        engines.append(FormulaOcrCommandEngine())
    except AppError as exc:
        errors.append(f"formula-command: {exc}")

    try:
        engines.append(TesseractEngine())
    except AppError as exc:
        errors.append(f"tesseract: {exc}")

    if os.name == "nt":
        try:
            engines.append(WindowsOcrEngine())
        except AppError as exc:
            errors.append(f"windows-ocr: {exc}")

    return engines, errors


def _engine_name(engine: OCREngine) -> str:
    return str(getattr(engine, "name", engine.__class__.__name__))


def _with_error_details(message: str, details: list[str]) -> str:
    cleaned = [detail.strip() for detail in details if detail.strip()]
    if not cleaned:
        return message
    return f"{message} Details: {'; '.join(cleaned[:4])}"


def _pdf_page_images(pdf_bytes: bytes, engines: list[OCREngine]) -> list[object]:
    for engine in engines:
        engine_pdf2image = getattr(engine, "_pdf2image", None)
        if engine_pdf2image is not None:
            return list(engine_pdf2image.convert_from_bytes(pdf_bytes, dpi=_ocr_pdf_dpi(), fmt="png"))
    try:
        import pdf2image
    except ModuleNotFoundError as exc:
        raise AppError(
            "PDF OCR requires pdf2image and pdftoppm.",
            code=ErrorCode.OCR_UNAVAILABLE,
            status=415,
        ) from exc
    return list(pdf2image.convert_from_bytes(pdf_bytes, dpi=_ocr_pdf_dpi(), fmt="png"))


def _extract_pdf_with_page_fallback(
    data: bytes,
    engines: list[OCREngine],
    *,
    no_engine_message: str,
    empty_message: str,
) -> str | None:
    page_engines = [engine for engine in engines if callable(getattr(engine, "extract_page_image", None))]
    if not page_engines:
        return None

    try:
        images = _pdf_page_images(data, page_engines)
    except AppError:
        raise
    except Exception as exc:
        raise AppError("PDF OCR rendering failed.", code=ErrorCode.OCR_UNAVAILABLE, status=415) from exc

    pages: list[str] = []
    runtime_errors: list[str] = []
    saw_empty_result = False
    for index, image in enumerate(images, start=1):
        page_text = ""
        page_score = -1
        for engine in page_engines:
            name = _engine_name(engine)
            try:
                text = str(getattr(engine, "extract_page_image")(image))
            except AppError as exc:
                if exc.code == ErrorCode.OCR_EMPTY:
                    saw_empty_result = True
                    runtime_errors.append(f"{name} page {index}: {exc}")
                    continue
                if exc.code == ErrorCode.OCR_UNAVAILABLE:
                    runtime_errors.append(f"{name} page {index}: {exc}")
                    continue
                raise
            except Exception as exc:
                runtime_errors.append(f"{name} page {index}: {exc}")
                continue

            text = normalize_ocr_text(text)
            if name == "formula-command" and not _formula_ocr_output_is_credible(text):
                text = ""
            if text.strip():
                if name == "deepseek-api":
                    page_text = text.strip()
                    break
                score = _ocr_text_score(text)
                if score > page_score:
                    page_text = text.strip()
                    page_score = score
                continue
            saw_empty_result = True
            runtime_errors.append(f"{name} page {index}: empty result")

        if page_text:
            pages.append(f"[PDF 第 {index} 页 (OCR)]\n{page_text}")

    if pages:
        return "\n\n".join(pages)
    if saw_empty_result:
        raise AppError(empty_message, code=ErrorCode.OCR_EMPTY, status=422)
    raise AppError(
        _with_error_details(no_engine_message, runtime_errors),
        code=ErrorCode.OCR_UNAVAILABLE,
        status=415,
    )


def _extract_with_fallback(
    data: bytes,
    *,
    mode: str,
    no_engine_message: str,
    empty_message: str,
    api_key: str | None = None,
) -> str:
    engines, startup_errors = _ocr_engine_candidates(api_key=api_key)
    if not engines:
        raise AppError(
            _with_error_details(no_engine_message, startup_errors),
            code=ErrorCode.OCR_UNAVAILABLE,
            status=415,
        )

    if mode == "pdf":
        page_text = _extract_pdf_with_page_fallback(
            data,
            engines,
            no_engine_message=no_engine_message,
            empty_message=empty_message,
        )
        if page_text is not None:
            return page_text

    runtime_errors: list[str] = []
    saw_empty_result = False
    best_text = ""
    best_score = -1
    for engine in engines:
        name = _engine_name(engine)
        try:
            text = engine.extract(data) if mode == "pdf" else engine.extract_image(data)
        except AppError as exc:
            if exc.code == ErrorCode.OCR_EMPTY:
                saw_empty_result = True
                runtime_errors.append(f"{name}: {exc}")
                continue
            if exc.code == ErrorCode.OCR_UNAVAILABLE:
                runtime_errors.append(f"{name}: {exc}")
                continue
            raise
        except Exception as exc:
            runtime_errors.append(f"{name}: {exc}")
            continue

        text = normalize_ocr_text(text)
        if name == "formula-command" and not _formula_ocr_output_is_credible(text):
            text = ""
        if text.strip():
            if name == "deepseek-api":
                return text.strip()
            score = _ocr_text_score(text)
            if score > best_score:
                best_text = text.strip()
                best_score = score
            continue
        saw_empty_result = True
        runtime_errors.append(f"{name}: empty result")

    if best_text:
        return best_text

    if saw_empty_result:
        raise AppError(empty_message, code=ErrorCode.OCR_EMPTY, status=422)

    raise AppError(
        _with_error_details(no_engine_message, startup_errors + runtime_errors),
        code=ErrorCode.OCR_UNAVAILABLE,
        status=415,
    )


def extract_pdf_ocr(pdf_bytes: bytes, *, api_key: str | None = None) -> str:
    return _extract_with_fallback(
        pdf_bytes,
        mode="pdf",
        no_engine_message=(
            "No OCR engine is available. Set DEEPSEEK_API_KEY for DeepSeek OCR, or install "
            "requirements-ocr.txt and Tesseract to use the local fallback for scanned PDFs."
        ),
        empty_message="OCR did not recognize any text.",
        api_key=api_key,
    )


def extract_image_ocr(image_bytes: bytes, *, api_key: str | None = None) -> str:
    return _extract_with_fallback(
        image_bytes,
        mode="image",
        no_engine_message=(
            "No OCR engine is available. Set DEEPSEEK_API_KEY for DeepSeek OCR, or install "
            "requirements-ocr.txt and Tesseract to use the local fallback for image text."
        ),
        empty_message="OCR did not recognize any text in image.",
        api_key=api_key,
    )
