"""create_pptx parity probe, Python side.

Pins title/slide refusals, bullet fallback from `content`, MD5 deck theme,
layout picker, agenda insertion and the outline envelope of
`create_presentation`. The `.pptx` *bytes* are not a frozen protocol
(python-pptx fingerprints); the native writer produces a valid OOXML zip
with the same titles/layouts, proven by unit tests rather than this byte diff.

Usage::

    python tasks/native-runtime/pptx_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example pptx_parity_probe > ../rust.json
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
from deepseek_infra.infra.tool_runtime import presentations  # noqa: E402

ROADMAP = [
    {"title": "核心观点", "bullets": ["把复杂流程拆成三条主线"]},
    {"title": "关键能力", "bullets": ["洞察：统一指标", "执行：标准流程", "反馈：闭环复盘"]},
    {"title": "实施流程", "bullets": ["调研", "试点", "推广", "复盘"]},
    {"title": "方案对比", "bullets": ["自建：控制力强", "采购：上线快", "混合：风险均衡"]},
    {"title": "总结与下一步", "bullets": ["先跑 MVP", "两周后复盘", "明确负责人"]},
]


def error_view(exc: AppError) -> dict[str, Any]:
    return {"error": str(exc), "code": exc.code.value, "status": exc.status}


def plan(title: str, slides: Any, subtitle: str) -> Any:
    try:
        clean_title = str(title or "").strip()
        if not clean_title:
            raise AppError("演示文稿需要一个标题（title）。")
        if not isinstance(slides, list) or not slides:
            raise AppError("演示文稿至少需要一页内容（slides）。")
        normalized: list[dict[str, Any]] = []
        for item in slides[: presentations.MAX_SLIDES]:
            if not isinstance(item, dict):
                continue
            slide_title = (str(item.get("title") or "").strip() or f"第 {len(normalized) + 1} 页")[:120]
            bullets = presentations._normalize_bullets(item)[: presentations.MAX_BULLETS_PER_SLIDE]
            layout = presentations._layout_for_slide(item, slide_title, bullets, len(normalized), len(slides))
            normalized.append({"title": slide_title, "bullets": bullets, "layout": layout})
        if not normalized:
            raise AppError("没有解析到有效的幻灯片内容。")
        theme = presentations._deck_theme_for(clean_title)
        slide_count = 1 + (1 if len(normalized) >= 4 else 0) + len(normalized)
        return {
            "title": clean_title,
            "subtitle": str(subtitle or "").strip(),
            "theme": theme,
            "slideCount": slide_count,
            "hasAgenda": len(normalized) >= 4,
            "outline": [
                {"page": index + 1, "title": item["title"], "bullets": item["bullets"], "layout": item["layout"]}
                for index, item in enumerate(normalized)
            ],
        }
    except AppError as exc:
        return error_view(exc)


def main() -> int:
    out: dict[str, Any] = {
        "empty-title": plan("", [{"title": "x", "bullets": ["a"]}], ""),
        "empty-slides": plan("有标题", [], ""),
        "skip-non-dict": plan("T", [None, "bad", {"title": "页", "bullets": ["a"]}], ""),
        "content-fallback": plan("标题", [{"title": "页", "content": "第一行\n第二行"}], "副标题"),
        "default-title": plan("T", [{"bullets": ["only"]}], ""),
        "requested-layout": plan("T", [{"title": "X", "bullets": ["a", "b", "c"], "layout": "PROCESS"}], ""),
        "roadmap": plan("Product Roadmap", ROADMAP, ""),
        "two-slides": plan(
            "测试标题",
            [{"title": "第一页", "bullets": ["要点 A", "要点 B"]}, {"title": "第二页", "bullets": ["要点 C"]}],
            "副标题",
        ),
    }
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
