"""Page image and layout parity probe.

Compares the native renderer with the unmodified ``render_pdf_page_layout`` and
``render_pdf_page_png``. Layout JSON is compared numerically. PNG bytes come from
``pdftoppm`` on the native side and from PyMuPDF on the oracle side, so the probe
checks the page, the PNG signature and that the pixel size stays within four
pixels of MuPDF's ``ceil(points * scale)`` pixmap. It does not require the two
encoders to emit the same bytes.

Usage::

    python tasks/native-runtime/file_page_render_probe.py \\
        --rust-example rust/target/debug/examples/file_page_render_probe.exe
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def _simple_pdf() -> bytes:
    stream = b"BT\n/F1 12 Tf\n72 720 Td\n(Hello pdf) Tj\n0 -16 Td\n(World) Tj\nET\n"
    objects: list[bytes] = [
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"",
        b"<< /Type /Catalog /Pages 2 0 R >>",
    ]
    content = b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream"
    objects.append(content)
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 1 0 R >> >> >>"
    )
    objects[1] = b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>"
    parts = [b"%PDF-1.4\n"]
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(sum(len(part) for part in parts))
        parts.append(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = sum(len(part) for part in parts)
    body = [b"xref\n0 6\n", b"0000000000 65535 f \n"]
    for offset in offsets[1:]:
        body.append(f"{offset:010d} 00000 n \n".encode())
    trailer = (
        b"trailer\n<< /Size 6 /Root 3 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
    )
    return b"".join(parts + body + [trailer])


def _drawn_pdf() -> bytes:
    import fitz

    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Hello pdf")
    page.insert_text((72, 100), "Second line")
    data = document.tobytes()
    document.close()
    return data


def _two_page_pdf() -> bytes:
    import fitz

    document = fitz.open()
    first = document.new_page()
    first.insert_text((72, 72), "Page one")
    second = document.new_page()
    second.insert_text((72, 72), "Page two")
    data = document.tobytes()
    document.close()
    return data


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[12:16] != b"IHDR":
        return (0, 0)
    return struct.unpack(">II", data[16:24])


def _round(value: Any, places: int) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    factor = 10**places
    return round(float(value) * factor) / factor


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, float):
        return _round(value, 4)
    return value


def _oracle(data: bytes) -> dict[str, Any]:
    from deepseek_infra.infra.rag.files import render_pdf_page_layout, render_pdf_page_png
    from deepseek_infra.core.errors import AppError

    def layout(page: int) -> dict[str, Any]:
        try:
            return {"ok": render_pdf_page_layout(data, page)}
        except AppError as exc:
            return {"error": {"message": str(exc), "code": exc.code.value, "status": exc.status}}

    try:
        png, page, count = render_pdf_page_png(data, 1, 1.6)
        width, height = _png_size(png)
        image: dict[str, Any] = {
            "page": page,
            "pageCount": count,
            "png": png.startswith(b"\x89PNG") and len(png) > 0,
            "width": width,
            "height": height,
        }
    except AppError as exc:
        image = {"error": {"message": str(exc), "code": exc.code.value, "status": exc.status}}
    return {"layout": layout(1), "second": layout(2), "png": image}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rust-example", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    directory = Path(tempfile.mkdtemp(prefix="file-page-render-"))
    try:
        (directory / "simple.pdf").write_bytes(_simple_pdf())
        (directory / "drawn.pdf").write_bytes(_drawn_pdf())
        (directory / "two.pdf").write_bytes(_two_page_pdf())
        completed = subprocess.run(
            [str(args.rust_example)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={**os.environ, "FILE_PAGE_RENDER_DIR": str(directory)},
        )
        if completed.returncode != 0:
            print(completed.stderr, file=sys.stderr)
            return 2
        native = json.loads(completed.stdout)
        problems: list[str] = []
        for name in ("simple.pdf", "drawn.pdf", "two.pdf"):
            oracle = _oracle((directory / name).read_bytes())
            got = native[name]
            if _normalize(got["layout"]) != _normalize(oracle["layout"]):
                problems.append(f"{name} layout oracle={oracle['layout']!r} native={got['layout']!r}")
            if _normalize(got["second"]) != _normalize(oracle["second"]):
                problems.append(f"{name} second oracle={oracle['second']!r} native={got['second']!r}")
            oracle_png = oracle["png"]
            native_png = got["png"]
            if "error" in oracle_png or "error" in native_png:
                problems.append(f"{name} png oracle={oracle_png!r} native={native_png!r}")
                continue
            if oracle_png["page"] != native_png["page"] or not native_png["png"]:
                problems.append(f"{name} png meta oracle={oracle_png!r} native={native_png!r}")
            if abs(int(oracle_png["width"]) - int(native_png["width"])) > 4:
                problems.append(f"{name} width oracle={oracle_png['width']} native={native_png['width']}")
            if abs(int(oracle_png["height"]) - int(native_png["height"])) > 4:
                problems.append(f"{name} height oracle={oracle_png['height']} native={native_png['height']}")
            if name == "two.pdf" and oracle_png["pageCount"] != 0:
                # PyMuPDF reports the real page count. pdftoppm still renders page 1.
                if native_png["pageCount"] != oracle_png["pageCount"]:
                    problems.append(
                        f"{name} count oracle={oracle_png['pageCount']} native={native_png['pageCount']}"
                    )
        report = {"cases_compared": 3, "problems": problems, "result": "PASS" if not problems else "FAIL"}
    finally:
        import shutil

        shutil.rmtree(directory, ignore_errors=True)
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
