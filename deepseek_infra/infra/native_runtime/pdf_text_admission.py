"""Admission for selectable text on the Rust ``POST /api/file-text`` PDF path.

The part gate still leaves ``.pdf`` on ``extract_uploaded_file``. That function
calls ``extract_pdf_document`` and returns its parse error before
``write_cache``. This oracle calls the shipped ``extract_pdf_text`` with OCR
disabled, so a scanned PDF is not sent to an OCR engine from this decision.
"""

from __future__ import annotations

from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.rag.files import extract_pdf_text

PARSE_ERROR = "Could not extract text from this PDF"


def admit_selectable_pdf(data: Any) -> dict[str, Any]:
    """Return shipped PDF text, or the existing parse error, without writing.

    A non-empty extraction sets ``cacheWrite``. A parse failure and a
    textless PDF keep ``cacheWrite`` false. This function does not call OCR
    and does not write ``.file-cache``.
    """
    payload = data if isinstance(data, (bytes, bytearray)) else b""
    try:
        text = extract_pdf_text(bytes(payload), ocr_enabled=False)
    except AppError as exc:
        return _refused(str(exc), _wire_code(exc.code), _status(exc.status))
    if not isinstance(text, str) or text == "":
        return _idle()
    return {
        "action": "text",
        "cacheWrite": True,
        "code": "",
        "status": 200,
        "message": "",
        "text": text,
        "kind": "pdf",
    }


def _wire_code(value: Any) -> str:
    if isinstance(value, ErrorCode):
        return str(value.value)
    if isinstance(value, str):
        return value
    return ""


def _status(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 422
    return value


def _idle() -> dict[str, Any]:
    return {
        "action": "continue",
        "cacheWrite": False,
        "code": "",
        "status": 200,
        "message": "",
        "text": "",
        "kind": "",
    }


def _refused(message: str, code: str, status: int) -> dict[str, Any]:
    return {
        "action": "refuse",
        "cacheWrite": False,
        "code": code,
        "status": status,
        "message": message,
        "text": "",
        "kind": "",
    }
