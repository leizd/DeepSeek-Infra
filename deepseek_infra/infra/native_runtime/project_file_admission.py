"""Admission for the Rust ``POST /api/project-files`` route.

A legal text, HTML, DOCX, PPTX, XLSX, or EPUB part is attached only when
``project_metadata_store`` has a native writer. An empty or invalid part
refuses before metadata or cache writes. While Python owns that store, both
decisions refuse and write nothing. The text comes from
``admit_file_text_part``, which calls the shipped extractor.
"""

from __future__ import annotations

from typing import Any

from deepseek_infra.core.errors import ErrorCode
from deepseek_infra.infra.native_runtime.authority import RUST_DATA_DOMAINS, RuntimeMode
from deepseek_infra.infra.native_runtime.file_text_admission import admit_file_text_part

NOT_OWNED = "NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED"
NOT_OWNED_MESSAGE = (
    "The project metadata store is still written by the Python runtime, "
    "so this gateway refuses to mutate it."
)


def admit_project_metadata_write(mode: Any) -> dict[str, Any]:
    """Allow parsing only for the native project-metadata writer.

    Parsing is not an attach. This decision never writes ``project.json``
    or the file cache.
    """
    if _native_owner(mode):
        return _idle()
    return _denied()


def admit_project_file_part(
    native_owner: Any,
    filename: Any,
    content_type: Any,
    data: Any,
    ocr_enabled: Any,
) -> dict[str, Any]:
    """Attach one part only when the native writer owns the store.

    Empty and unreadable text refuse with ``metadataWrite`` and
    ``cacheWrite`` false. A legal text, HTML, DOCX, PPTX, XLSX, or EPUB
    part carries the shipped extractor text and sets both write flags.
    PDF and a recognized image header do not invent text and do not write
    from this gate.
    """
    if native_owner is not True:
        return _denied_part()
    part = admit_file_text_part(filename, content_type, data, ocr_enabled)
    if part.get("action") == "refuse":
        return _refused(
            str(part.get("message") or ""),
            _wire_code(part.get("code")),
            _status(part.get("status")),
        )
    text = part.get("text")
    if part.get("action") == "text" and part.get("cacheWrite") is True and isinstance(text, str) and text != "":
        return {
            "attach": True,
            "metadataWrite": True,
            "cacheWrite": True,
            "code": "",
            "status": 200,
            "message": "",
            "text": text,
            "kind": str(part.get("kind") or ""),
        }
    if part.get("action") == "continue":
        return {
            "attach": True,
            "metadataWrite": False,
            "cacheWrite": False,
            "code": "",
            "status": 200,
            "message": "",
            "text": "",
            "kind": "",
        }
    return _refused("No readable text found in this file", "invalid_payload", 422)


def _wire_code(value: Any) -> str:
    if isinstance(value, ErrorCode):
        return str(value.value)
    if isinstance(value, str):
        return value
    return ""


def _status(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 400
    return value


def _native_owner(mode: Any) -> bool:
    if isinstance(mode, bool) or not isinstance(mode, str):
        return False
    if mode.strip().lower() != RuntimeMode.PYTHON_DISABLED.value:
        return False
    return "project_metadata_store" in RUST_DATA_DOMAINS


def _idle() -> dict[str, Any]:
    return {
        "allowed": True,
        "attach": False,
        "metadataWrite": False,
        "cacheWrite": False,
        "code": "",
        "status": 200,
        "message": "",
    }


def _denied() -> dict[str, Any]:
    return {
        "allowed": False,
        "attach": False,
        "metadataWrite": False,
        "cacheWrite": False,
        "code": NOT_OWNED,
        "status": 409,
        "message": NOT_OWNED_MESSAGE,
    }


def _denied_part() -> dict[str, Any]:
    denied = _denied()
    denied["text"] = ""
    denied["kind"] = ""
    return denied


def _refused(message: str, code: str, status: int) -> dict[str, Any]:
    return {
        "attach": False,
        "metadataWrite": False,
        "cacheWrite": False,
        "code": code,
        "status": status,
        "message": message,
        "text": "",
        "kind": "",
    }
