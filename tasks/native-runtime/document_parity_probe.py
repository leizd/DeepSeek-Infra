"""create_document parity probe, Python side.

Pins format aliases, section/table normalization, the MD5 theme, refusals and
the download envelope of `create_document`. The Office/PDF *bytes* are not a
frozen protocol (python-docx / reportlab fingerprints); the native writer
produces valid files with the same text, proven by unit tests rather than this
byte diff.

Usage::

    python tasks/native-runtime/document_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example document_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import sys
from typing import Any

REPO = __import__("pathlib").Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.tool_runtime import documents  # noqa: E402

SAMPLE = [
    {
        "heading": "概述",
        "body": ["这是第一段正文，用于介绍背景。", "这是第二段，给出本文目标。"],
        "bullets": ["要点一", "要点二 <含特殊字符 & 符号>"],
        "table": {"headers": ["列 A", "列 B"], "rows": [["1", "2"], ["3", "4"]]},
    },
    {
        "heading": "结论",
        "body": ["总结性段落，给出下一步建议。"],
        "bullets": [],
        "table": {"headers": [], "rows": []},
    },
]


def error_view(exc: AppError) -> dict[str, Any]:
    return {"error": str(exc), "code": exc.code.value, "status": exc.status}


def plan(fmt: str, title: str, sections: Any, subtitle: str) -> Any:
    try:
        normalized_fmt = documents._normalize_format(fmt)
        clean_title = str(title or "").strip()
        if not clean_title:
            raise AppError("文档需要一个标题（title）。")
        normalized = documents._normalize_sections(sections)
        theme = documents._theme_for(clean_title)
        return {
            "format": normalized_fmt,
            "title": clean_title,
            "subtitle": documents._clean_text(subtitle, limit=300),
            "theme": theme,
            "sections": normalized,
        }
    except AppError as exc:
        return error_view(exc)


def main() -> int:
    out: dict[str, Any] = {
        "alias::word": plan("word", "标题", SAMPLE, ""),
        "alias::pdf": plan("PDF", "标题", SAMPLE, ""),
        "alias::txt": plan("txt", "标题", SAMPLE, ""),
        "empty-title": plan("docx", "  ", SAMPLE, ""),
        "empty-sections": plan("docx", "标题", [], ""),
        "sample": plan("docx", "季度产品报告", SAMPLE, "2026 Q3"),
        "ragged": plan(
            "docx",
            "表格测试",
            [{"heading": "数据", "body": [], "bullets": [], "table": {"headers": ["A", "B", "C"], "rows": [["1"], ["1", "2", "3", "4", "5"]]}}],
            "",
        ),
        "skip-empty-section": plan(
            "docx",
            "T",
            [{"heading": "", "body": [], "bullets": []}, {"heading": "Keep", "body": ["x"]}],
            "",
        ),
        "default-heading": plan("docx", "T", [{"body": ["only body"]}], ""),
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
