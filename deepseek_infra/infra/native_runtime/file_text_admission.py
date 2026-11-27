"""Admission for the Rust ``POST /api/file-text`` route.

The Python functions call the shipped text decoder and the shipped image and
text classifiers. They do not write ``.file-cache`` and they do not call OCR.
The Rust route calls the matching functions before ``parse_multipart``,
``ocr_image``, and ``write_cache``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.rag.files import (
    HTML_EXTENSIONS,
    decode_text_file,
    extract_docx_text,
    extract_epub_text,
    extract_html_text,
    extract_pptx_text,
    extract_xlsx_text,
    is_image_file,
    is_text_file,
    normalize_extracted_text,
)

# The Rust route constant. This is not ``settings.files.upload_max_bytes``.
MAX_UPLOAD_BYTES = 220_000_000

_CONTINUED_EXTENSIONS = {".pdf"}


def admit_upload_body(content_length: Any) -> dict[str, Any]:
    """Admit a multipart body length. The gate itself never writes a cache."""
    length = _body_length(content_length)
    if length is None or length == 0:
        return _body(False, ErrorCode.INVALID_PAYLOAD, 400, "Upload body is empty")
    if length > MAX_UPLOAD_BYTES:
        return _body(False, ErrorCode.UPLOAD_TOO_LARGE, 413, _too_large_message())
    return _body(True, "", 200, "")


def admit_file_text_part(
    filename: Any,
    content_type: Any,
    data: Any,
    ocr_enabled: Any,
) -> dict[str, Any]:
    """Admit one part. Empty and unreadable text refuse before a cache write.

    A legal text part returns the shipped decoder's normalized text and sets
    ``cacheWrite``. Image OCR that is not exactly enabled refuses with
    ``ocr_required``. Corrupt image bytes refuse before OCR. A recognized
    image header and the PDF extension continue without writing and without
    inventing OCR text. HTML text comes from ``extract_html_text``. Readable
    HTML sets ``cacheWrite``. DOCX text comes from ``extract_docx_text``.
    PPTX text comes from ``extract_pptx_text``. XLSX text comes from
    ``extract_xlsx_text``. EPUB text comes from ``extract_epub_text``. A
    legal document sets ``cacheWrite``. An invalid DOCX, PPTX, XLSX, or EPUB
    refuses before a cache write.
    """
    name = filename if isinstance(filename, str) else ""
    payload = _payload(data)
    if payload is None or payload == b"":
        return _part(
            "refuse",
            False,
            ErrorCode.INVALID_PAYLOAD,
            400,
            f"Uploaded file is empty: {name}",
        )
    extension = Path(name.lower()).suffix
    media = content_type if isinstance(content_type, str) else ""
    if extension == ".docx":
        return _admit_docx(payload)
    if extension == ".pptx":
        return _admit_pptx(payload)
    if extension == ".xlsx":
        return _admit_xlsx(payload)
    if extension == ".epub":
        return _admit_epub(payload)
    if extension in _CONTINUED_EXTENSIONS:
        return _continue()
    if is_image_file(extension, media):
        if ocr_enabled is not True:
            return _part(
                "refuse",
                False,
                ErrorCode.OCR_REQUIRED,
                415,
                "Image OCR requires OCR to be enabled. Enable OCR and retry, or set OCR_ENABLED=1.",
            )
        if not _image_magic_ok(payload):
            return _part(
                "refuse",
                False,
                ErrorCode.INVALID_PAYLOAD,
                400,
                "Uploaded image is corrupt.",
            )
        return _continue()
    if extension in HTML_EXTENSIONS:
        text = normalize_extracted_text(extract_html_text(payload))
        if text == "":
            return _part(
                "refuse",
                False,
                ErrorCode.INVALID_PAYLOAD,
                422,
                "No readable text found in this file",
            )
        kind = extension[1:] if len(extension) > 1 else "html"
        return _part("text", True, "", 200, "", text=text, kind=kind)
    if is_text_file(extension, media, payload):
        text = normalize_extracted_text(decode_text_file(payload))
        if text == "":
            return _part(
                "refuse",
                False,
                ErrorCode.INVALID_PAYLOAD,
                422,
                "No readable text found in this file",
            )
        kind = extension[1:] if extension.startswith(".") and len(extension) > 1 else "text"
        return _part("text", True, "", 200, "", text=text, kind=kind)
    return _continue()


def _admit_docx(payload: bytes) -> dict[str, Any]:
    """Return shipped DOCX text, or the existing package error, without writing."""
    try:
        raw = extract_docx_text(payload)
    except AppError as exc:
        code = exc.code if isinstance(exc.code, ErrorCode) else ErrorCode.INVALID_PAYLOAD
        status = exc.status if isinstance(exc.status, int) and not isinstance(exc.status, bool) else 422
        return _part("refuse", False, code, status, str(exc))
    text = normalize_extracted_text(raw if isinstance(raw, str) else "")
    if text == "":
        return _part(
            "refuse",
            False,
            ErrorCode.INVALID_PAYLOAD,
            422,
            "No readable text found in this file",
        )
    return _part("text", True, "", 200, "", text=text, kind="docx")


def _admit_pptx(payload: bytes) -> dict[str, Any]:
    """Return shipped PPTX text, or the existing package error, without writing."""
    try:
        raw = extract_pptx_text(payload)
    except AppError as exc:
        code = exc.code if isinstance(exc.code, ErrorCode) else ErrorCode.INVALID_PAYLOAD
        status = exc.status if isinstance(exc.status, int) and not isinstance(exc.status, bool) else 422
        return _part("refuse", False, code, status, str(exc))
    text = normalize_extracted_text(raw if isinstance(raw, str) else "")
    if text == "":
        return _part(
            "refuse",
            False,
            ErrorCode.INVALID_PAYLOAD,
            422,
            "No readable text found in this file",
        )
    return _part("text", True, "", 200, "", text=text, kind="pptx")


def _admit_xlsx(payload: bytes) -> dict[str, Any]:
    """Return shipped XLSX text, or the existing package error, without writing."""
    try:
        raw = extract_xlsx_text(payload)
    except AppError as exc:
        code = exc.code if isinstance(exc.code, ErrorCode) else ErrorCode.INVALID_PAYLOAD
        status = exc.status if isinstance(exc.status, int) and not isinstance(exc.status, bool) else 422
        return _part("refuse", False, code, status, str(exc))
    text = normalize_extracted_text(raw if isinstance(raw, str) else "")
    if text == "":
        return _part(
            "refuse",
            False,
            ErrorCode.INVALID_PAYLOAD,
            422,
            "No readable text found in this file",
        )
    return _part("text", True, "", 200, "", text=text, kind="xlsx")


def _admit_epub(payload: bytes) -> dict[str, Any]:
    """Return shipped EPUB text, or the existing package error, without writing."""
    try:
        raw = extract_epub_text(payload)
    except AppError as exc:
        code = exc.code if isinstance(exc.code, ErrorCode) else ErrorCode.INVALID_PAYLOAD
        status = exc.status if isinstance(exc.status, int) and not isinstance(exc.status, bool) else 422
        return _part("refuse", False, code, status, str(exc))
    text = normalize_extracted_text(raw if isinstance(raw, str) else "")
    if text == "":
        return _part(
            "refuse",
            False,
            ErrorCode.INVALID_PAYLOAD,
            422,
            "No readable text found in this file",
        )
    return _part("text", True, "", 200, "", text=text, kind="epub")


def _body(admit: bool, code: str, status: int, message: str) -> dict[str, Any]:
    return {
        "admit": admit,
        "cacheWrite": False,
        "code": code,
        "status": status,
        "message": message,
    }


def _part(
    action: str,
    cache_write: bool,
    code: str,
    status: int,
    message: str,
    text: str = "",
    kind: str = "",
) -> dict[str, Any]:
    return {
        "action": action,
        "admit": action != "refuse",
        "cacheWrite": cache_write,
        "code": code,
        "status": status,
        "message": message,
        "text": text,
        "kind": kind,
    }


def _continue() -> dict[str, Any]:
    return _part("continue", False, "", 200, "")


def _body_length(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def _payload(data: Any) -> bytes | None:
    if isinstance(data, bool) or not isinstance(data, (bytes, bytearray)):
        return None
    return bytes(data)


def _too_large_message() -> str:
    from deepseek_infra.web.server import format_upload_limit

    limit = format_upload_limit(MAX_UPLOAD_BYTES)
    return f"Upload body is too large. Maximum request size is {limit}."


def _image_magic_ok(data: bytes) -> bool:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return True
    if data.startswith(b"\xff\xd8\xff"):
        return True
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return True
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return True
    if data.startswith(b"BM"):
        return True
    return data.startswith(b"II*\x00") or data.startswith(b"MM\x00*")
