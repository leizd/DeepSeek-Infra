"""Oracle for selectable PDF text on the native file-text extractor path.

A legal PDF is built with pypdf and read back through the shipped
``extract_pdf_text`` wrapper. A corrupt payload keeps the existing parse
error and does not write a cache file. The Rust extractor is not executed.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.native_runtime.pdf_text_admission import PARSE_ERROR, admit_selectable_pdf
from deepseek_infra.infra.native_runtime.project_file_admission import (
    NOT_OWNED,
    NOT_OWNED_MESSAGE,
    admit_project_file_part,
    admit_project_metadata_write,
)
from deepseek_infra.infra.rag import files as rag_files
from deepseek_infra.infra.rag.files import extract_pdf_text, render_pdf_page_layout

ROOT = Path(__file__).resolve().parents[1]

# The native gateway is a prebuilt Rust binary. A developer checkout and the
# Linux CI runner do not share a path, so resolve it from an explicit override,
# then the conventional build outputs, and skip the module when none exists. A
# missing binary must never be reported as a product failure.
GATEWAY_BINARY_ENV = "DEEPSEEK_GATEWAY_BINARY"
GATEWAY_BINARY_CANDIDATES = (
    ROOT / "bin" / "deepseek-gateway",
    ROOT / "bin" / "deepseek-gateway.exe",
    ROOT / "rust" / "target" / "release" / "deepseek-gateway",
    ROOT / "rust" / "target" / "release" / "deepseek-gateway.exe",
)


def _resolve_native_gateway() -> Path | None:
    override = os.environ.get(GATEWAY_BINARY_ENV, "").strip()
    candidates = (Path(override),) if override else GATEWAY_BINARY_CANDIDATES
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# Kept separate so the skip decision can test for absence while NATIVE_GATEWAY
# stays a plain Path for the call sites and the type checker.
_GATEWAY_BINARY = _resolve_native_gateway()
NATIVE_GATEWAY = _GATEWAY_BINARY or Path("deepseek-gateway-not-built")

pytestmark = pytest.mark.skipif(
    _GATEWAY_BINARY is None,
    reason=(
        "prebuilt native gateway is required; set "
        f"{GATEWAY_BINARY_ENV} or build rust/ -p deepseek-gateway --release"
    ),
)

OCR_REQUIRED_MESSAGE = (
    "Scanned/image-only PDF requires OCR. Enable OCR and retry, "
    "or convert it to selectable text first."
)

UPLOAD = ROOT / "rust/crates/deepseek-policy/src/file_upload.rs"
PDF_TEXT = ROOT / "rust/crates/deepseek-policy/src/pdf_text.rs"


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _hello_pdf() -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    page[NameObject("/Resources")] = resources
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 100 Td (Hello PDF) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _keep(decision: dict[str, Any], sink: list[str]) -> None:
    if decision.get("cacheWrite") is True and decision.get("action") == "text":
        sink.append(str(decision.get("text") or ""))


def test_legal_pdf_returns_shipped_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("ocr entered")

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.ocr.extract_pdf_ocr", boom)
    payload = _hello_pdf()
    expected = extract_pdf_text(payload, ocr_enabled=False)
    decision = admit_selectable_pdf(payload)
    sink: list[str] = []
    _keep(decision, sink)
    assert decision["action"] == "text"
    assert decision["cacheWrite"] is True
    assert decision["kind"] == "pdf"
    assert decision["text"] == expected
    assert "Hello PDF" in expected
    assert sink == [expected]
    record = tmp_path / "cache.txt"
    record.write_text(decision["text"], encoding="utf-8")
    assert record.read_text(encoding="utf-8") == expected


def test_textless_pdf_refuses_ocr_while_project_files_continue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_ocr(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("ocr entered")

    def _refuse_pdf_text(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("pdf text entered")

    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_pdf_ocr", _refuse_ocr)
    blank = _blank_pdf()
    selectable = _hello_pdf()
    assert blank.startswith(b"%PDF-")
    assert selectable.startswith(b"%PDF-")
    assert blank != selectable
    refused = admit_selectable_pdf(blank)
    assert refused["action"] == "refuse"
    assert refused["cacheWrite"] is False
    assert refused["text"] == ""
    assert refused["kind"] == ""
    assert refused["code"] == ErrorCode.OCR_REQUIRED
    assert refused["status"] == 422
    assert refused["message"] == OCR_REQUIRED_MESSAGE
    assert "Hello PDF" not in refused["message"]
    expected = extract_pdf_text(selectable, ocr_enabled=False)
    assert "Hello PDF" in expected
    admitted = admit_selectable_pdf(selectable)
    assert admitted["action"] == "text"
    assert admitted["cacheWrite"] is True
    assert admitted["kind"] == "pdf"
    assert admitted["text"] == expected
    stored = tmp_path / "cache.txt"
    if admitted["cacheWrite"] is True and admitted["action"] == "text":
        stored.write_text(admitted["text"], encoding="utf-8")
    assert stored.read_text(encoding="utf-8") == expected
    monkeypatch.setattr(
        "deepseek_infra.infra.native_runtime.pdf_text_admission.extract_pdf_text",
        _refuse_pdf_text,
    )
    monkeypatch.setattr("deepseek_infra.infra.rag.files.extract_pdf_text", _refuse_pdf_text)
    owned = admit_project_metadata_write(" python_disabled ")
    assert owned["allowed"] is True
    assert owned["metadataWrite"] is False
    assert owned["cacheWrite"] is False
    for name in ("notes.PDF", "notes.pdf"):
        continued = admit_project_file_part(True, name, "application/pdf", selectable, False)
        assert continued["attach"] is True
        assert continued["metadataWrite"] is False
        assert continued["cacheWrite"] is False
        assert continued["text"] == ""
        assert continued["kind"] == ""
        assert continued["message"] == ""
        assert "Hello PDF" not in continued["message"]
    denied = tmp_path / "denied"
    denied.mkdir()
    for mode in ("", "python_authoritative", "go_authoritative", "shadow", True, None, "python_disable"):
        ownership = admit_project_metadata_write(mode)
        assert ownership["allowed"] is False
        assert ownership["code"] == NOT_OWNED
        assert ownership["status"] == 409
        assert ownership["message"] == NOT_OWNED_MESSAGE
        part = admit_project_file_part(False, "notes.PDF", "application/pdf", selectable, True)
        assert part["attach"] is False
        assert part["metadataWrite"] is False
        assert part["cacheWrite"] is False
        assert part["text"] == ""
        assert "Hello PDF" not in part["message"]
    assert not (tmp_path / "project.json").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["cache.txt", "denied"]


def test_pdf_page_layout_returns_shipped_geometry_and_corrupt_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_png(*_args: Any, **_kwargs: Any) -> tuple[bytes, int, int]:
        raise AssertionError("png renderer entered")

    monkeypatch.setattr("deepseek_infra.infra.rag.files.render_pdf_page_png", _refuse_png)
    payload = _hello_pdf()
    layout = render_pdf_page_layout(payload, 1)
    assert layout["index"] == 1
    assert layout["pageCount"] == 1
    assert layout["width"] == 200.0
    assert layout["height"] == 200.0
    assert isinstance(layout["words"], list)
    assert layout["hasText"] is bool(layout["words"])
    if layout["hasText"]:
        visible = " ".join(str(word["text"]) for word in layout["words"])
        assert "Hello" in layout["text"] or "Hello" in visible
    clamped = render_pdf_page_layout(payload, 5)
    assert clamped["index"] == 1
    assert clamped["pageCount"] == 1
    assert clamped["width"] == layout["width"]
    assert clamped["height"] == layout["height"]
    with pytest.raises(AppError) as caught:
        render_pdf_page_layout(b"this is not a pdf", 1)
    assert caught.value.status == 415
    assert caught.value.code == ErrorCode.UNSUPPORTED_FILE
    assert str(caught.value).startswith("PDF page text layout is unavailable")
    assert list(tmp_path.iterdir()) == []


def test_corrupt_pdf_refuses_before_cache_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("ocr entered")

    monkeypatch.setattr("deepseek_infra.infra.tool_runtime.ocr.extract_pdf_ocr", boom)
    sink: list[str] = []
    for payload in (b"this is not a pdf", b"%PDF-1.4\n", None, True):
        decision = admit_selectable_pdf(payload)
        _keep(decision, sink)
        assert decision["action"] == "refuse"
        assert decision["cacheWrite"] is False
        assert decision["text"] == ""
        assert decision["code"] == ErrorCode.INVALID_PAYLOAD
        assert decision["status"] == 422
        assert decision["message"] == PARSE_ERROR
        assert "Uploaded PDF is corrupt." not in decision["message"]
    assert sink == []
    assert list(tmp_path.iterdir()) == []


def test_rust_extractor_parses_before_the_cache_write() -> None:
    upload = UPLOAD.read_text(encoding="utf-8")
    pdf_text = PDF_TEXT.read_text(encoding="utf-8")
    extract = upload.split("pub fn extract_uploaded_file(", 1)[1]
    extract = extract.split("pub fn cleanup_file_cache(", 1)[0]
    pdf_arm = extract.split('extension == ".pdf"', 1)[1]
    pdf_arm = pdf_arm.split('extension == ".epub"', 1)[0]
    assert extract.index('extension == ".pdf"') < extract.index("extract_pdf_document(")
    assert extract.index("extract_pdf_document(") < extract.index("write_cache(")
    assert "extracted.text.is_empty()" in pdf_arm
    assert pdf_arm.index("extracted.text.is_empty()") < pdf_arm.index("ocr_pdf_document(")
    parser = pdf_text.split("pub fn extract_pdf_document(", 1)[1]
    parser = parser.split("fn could_not_extract(", 1)[0]
    assert parser.index("Document::load_mem(") < parser.index("could_not_extract()")
    error_fn = pdf_text.split("fn could_not_extract(", 1)[1].split("struct TextState", 1)[0]
    assert 'message: "Could not extract text from this PDF"' in error_fn
    assert "Uploaded PDF is corrupt." not in upload
    assert "Uploaded PDF is corrupt." not in pdf_text
    for source in (pdf_arm, parser):
        assert "deepseek_infra" not in source
        assert 'Command::new("python")' not in source
        assert "NOT_APPLIED" not in source
        assert "CONFIRMED" not in source
        assert "install_authoritative_epoch" not in source
        assert "install_signed_epoch" not in source


def _native_layout_pdf() -> bytes:
    """The one-page Helvetica PDF the native layout owner measures."""
    stream = b"BT\n/F1 12 Tf\n72 720 Td\n(Hello pdf) Tj\n0 -16 Td\n(World) Tj\nET\n"
    objects = [
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"",
        b"<< /Type /Catalog /Pages 2 0 R >>",
    ]
    content = f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"endstream"
    objects.append(content)
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 1 0 R >> >> >>"
    )
    objects[1] = b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>"
    parts = [b"%PDF-1.4\n"]
    offsets = [0]
    for index, obj in enumerate(objects):
        offsets.append(sum(len(part) for part in parts))
        parts.append(f"{index + 1} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = sum(len(part) for part in parts)
    trailer = bytearray(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        trailer += f"{offset:010} 00000 n \n".encode()
    trailer += f"trailer\n<< /Size 6 /Root 3 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    parts.append(bytes(trailer))
    return b"".join(parts)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gateway_layout(port: int, file_id: str, page: int) -> tuple[int, dict[str, Any]]:
    url = f"http://127.0.0.1:{port}/api/file-page-layout?fileId={file_id}&page={page}"
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status = response.status
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        payload = exc.read()
    body = json.loads(payload.decode("utf-8"))
    assert isinstance(body, dict)
    return status, body


def test_cached_file_page_layout_uses_the_native_gateway(
    tmp_settings: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_python_layout(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("python layout entered")

    monkeypatch.setattr(rag_files, "file_page_layout", _refuse_python_layout)
    monkeypatch.setattr(rag_files, "render_pdf_page_layout", _refuse_python_layout)
    monkeypatch.setattr(rag_files, "render_pdf_page_png", _refuse_python_layout)
    text_bytes = b"not a pdf LAYOUTREFUSE"
    pdf_bytes = _native_layout_pdf()
    text_id = rag_files.cache_file_chunks(
        "notes.txt",
        "text/plain",
        len(text_bytes),
        "text",
        "",
        [],
        source_bytes=text_bytes,
        page_count=0,
    )
    pdf_id = rag_files.cache_file_chunks(
        "hello.pdf",
        "application/pdf",
        len(pdf_bytes),
        "pdf",
        "",
        [],
        source_bytes=pdf_bytes,
        page_count=1,
    )
    bad_id = rag_files.cache_file_chunks(
        "bad.pdf",
        "application/pdf",
        len(text_bytes),
        "pdf",
        "",
        [],
        source_bytes=text_bytes,
        page_count=1,
    )
    cache_dir = tmp_settings / ".file-cache"
    before = sorted(path.name for path in cache_dir.iterdir())
    assert f"{text_id}.json" in before
    assert f"{text_id}.source" in before
    assert f"{pdf_id}.source" in before
    assert (cache_dir / f"{pdf_id}.source").read_bytes() == pdf_bytes
    port = _free_port()
    log_path = tmp_settings / "gateway-layout.log"
    log_handle = log_path.open("wb")
    env = os.environ.copy()
    env["AUTH_DISABLED"] = "1"
    env["DEEPSEEK_INFRA_ROOT"] = str(tmp_settings)
    env["DEEPSEEK_INFRA_STATIC_DIR"] = str(ROOT / "static")
    env["GATEWAY_BIND_ADDR"] = f"127.0.0.1:{port}"
    env.pop("AUTH_TOKEN", None)
    process = subprocess.Popen(
        [str(NATIVE_GATEWAY)],
        cwd=str(NATIVE_GATEWAY.parent),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 20
        listening = False
        while time.time() < deadline:
            if process.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    listening = True
                    break
            except OSError:
                time.sleep(0.1)
        assert listening, log_path.read_text(encoding="utf-8", errors="replace")
        refused_status, refused = _gateway_layout(port, text_id, 5)
        assert refused_status == 415
        assert refused["error"] == "Page text layout is only available for PDF files"
        assert refused["code"] == "unsupported_file"
        assert "LAYOUTREFUSE" not in refused["error"]
        assert "executionEpoch" not in refused
        layout_status, layout = _gateway_layout(port, pdf_id, 5)
        assert layout_status == 200
        assert layout["ok"] is True
        assert layout["file"]["name"] == "hello.pdf"
        assert layout["file"]["kind"] == "pdf"
        assert layout["file"]["fileId"] == pdf_id
        assert layout["file"]["sourceAvailable"] is True
        page = layout["page"]
        assert page["index"] == 1
        assert page["pageCount"] == 1
        assert page["width"] == 612.0
        assert page["height"] == 792.0
        assert page["text"] == "Hello pdf\nWorld"
        assert page["hasText"] is True
        words = page["words"]
        assert isinstance(words, list)
        assert [word["text"] for word in words] == ["Hello", "pdf", "World"]
        assert abs(float(words[0]["left"]) - 11.7647) < 0.0001
        assert abs(float(words[0]["top"]) - 7.4621) < 0.0001
        assert abs(float(words[0]["width"]) - 4.4667) < 0.0001
        assert abs(float(words[0]["height"]) - 2.0818) < 0.0001
        assert words[2]["text"] == "World"
        assert abs(float(words[2]["left"]) - 11.7647) < 0.0001
        assert "executionEpoch" not in layout
        bad_status, bad = _gateway_layout(port, bad_id, 1)
        assert bad_status == 415
        assert str(bad["error"]).startswith("PDF page text layout is unavailable")
        assert bad["code"] == "unsupported_file"
        assert bad["error"] != refused["error"]
    finally:
        process.kill()
        process.wait(timeout=10)
        log_handle.close()
    after = sorted(path.name for path in cache_dir.iterdir())
    assert after == before
    assert all(not name.endswith(".png") for name in after)
    assert (cache_dir / f"{pdf_id}.source").read_bytes() == pdf_bytes
    assert (cache_dir / f"{text_id}.source").read_bytes() == text_bytes
