from __future__ import annotations

import unittest
import unittest.mock
import tempfile
from pathlib import Path

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.tool_runtime import generated_files, presentations
from deepseek_infra.infra.tool_runtime.presentations import (
    create_presentation,
    infer_presentation_title,
    resolve_generated_file,
    save_generated_file_to_downloads,
    slides_from_outline_text,
)
from deepseek_infra.infra.tool_runtime.tools import available_tool_definitions


class PresentationTests(unittest.TestCase):
    def test_create_presentation_generates_file_and_download_url(self) -> None:
        result = create_presentation(
            "测试标题",
            [{"title": "第一页", "bullets": ["要点 A", "要点 B"]}, {"title": "第二页", "bullets": ["要点 C"]}],
            subtitle="副标题",
        )
        # 封面 + 2 内容页
        self.assertEqual(result["slideCount"], 3)
        self.assertTrue(result["downloadUrl"].startswith("/api/download?id="))
        self.assertTrue(result["filename"].endswith(".pptx"))
        path = resolve_generated_file(result["fileId"])
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(path.is_file())
        path.unlink(missing_ok=True)

    def test_content_field_falls_back_to_bullets(self) -> None:
        result = create_presentation("标题", [{"title": "页", "content": "第一行\n第二行"}])
        path = resolve_generated_file(result["fileId"])
        self.assertIsNotNone(path)
        assert path is not None
        path.unlink(missing_ok=True)

    def test_larger_deck_gets_agenda_and_rich_layouts(self) -> None:
        result = create_presentation(
            "Product Roadmap",
            [
                {"title": "核心观点", "bullets": ["把复杂流程拆成三条主线"]},
                {"title": "关键能力", "bullets": ["洞察：统一指标", "执行：标准流程", "反馈：闭环复盘"]},
                {"title": "实施流程", "bullets": ["调研", "试点", "推广", "复盘"]},
                {"title": "方案对比", "bullets": ["自建：控制力强", "采购：上线快", "混合：风险均衡"]},
                {"title": "总结与下一步", "bullets": ["先跑 MVP", "两周后复盘", "明确负责人"]},
            ],
        )

        self.assertEqual(result["slideCount"], 7)
        self.assertIn("layout", result["outline"][0])
        self.assertEqual(result["outline"][2]["layout"], "process")
        path = resolve_generated_file(result["fileId"])
        self.assertIsNotNone(path)
        assert path is not None
        path.unlink(missing_ok=True)

    def test_resolve_blocks_path_traversal_and_bad_ids(self) -> None:
        self.assertIsNone(resolve_generated_file("../../etc/passwd"))
        self.assertIsNone(resolve_generated_file("not-hex-id"))
        self.assertIsNone(resolve_generated_file(""))
        self.assertIsNone(resolve_generated_file("0" * 31))  # 长度不足 32

    def test_save_generated_file_to_downloads_returns_exact_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            generated_dir = Path(tmp) / ".generated"
            downloads_dir = Path(tmp) / "Downloads"
            with unittest.mock.patch.object(generated_files, "GENERATED_DIR", generated_dir):
                result = create_presentation("路径测试", [{"title": "页", "bullets": ["内容"]}])
                saved = save_generated_file_to_downloads(result["fileId"], filename="路径测试.pptx", downloads_dir=downloads_dir)

            self.assertTrue(Path(saved["path"]).is_file())
            self.assertEqual(Path(saved["path"]).name, "路径测试.pptx")

    def test_create_presentation_requires_title_and_slides(self) -> None:
        with self.assertRaises(AppError) as cm:
            create_presentation("", [{"title": "x", "bullets": ["a"]}])
        self.assertEqual(cm.exception.code, ErrorCode.INVALID_PAYLOAD)
        with self.assertRaises(AppError):
            create_presentation("有标题", [])

    def test_create_pptx_registered_as_tool(self) -> None:
        tools = available_tool_definitions()
        names = [tool["function"]["name"] for tool in tools]
        self.assertIn("create_pptx", names)
        create_pptx = next(tool for tool in tools if tool["function"]["name"] == "create_pptx")
        self.assertIn("slides", create_pptx["function"]["description"])
        self.assertIn("PowerPoint-style presentations", create_pptx["function"]["description"])
        slide_schema = create_pptx["function"]["parameters"]["properties"]["slides"]["items"]
        self.assertIn("layout", slide_schema["properties"])
        self.assertIn("layout", slide_schema["required"])

    def test_text_fallback_marks_slides_skill_route(self) -> None:
        with unittest.mock.patch.object(presentations, "create_presentation", return_value={"ok": True}) as mocked:
            result = presentations.create_presentation_from_text("帮我做一个 Git PPT", "1. 封面 - Git\n2. 工作流")

        self.assertEqual(result, {"ok": True})
        self.assertIn("slides skill", mocked.call_args.kwargs["subtitle"])

    def test_outline_text_can_seed_presentation_slides(self) -> None:
        title = infer_presentation_title("帮我做一个介绍 git 的 PPT")
        slides = slides_from_outline_text(
            """
            关于 Git 的 PPT 大纲：
            1. 封面 - Git 介绍
            2. 什么是版本控制？
            3. Git 的核心概念
            4. 常用命令
            """,
            topic=title,
        )

        self.assertEqual(title, "Git 介绍")
        self.assertEqual(slides[0]["title"], "什么是版本控制？")
        self.assertGreaterEqual(len(slides), 3)

    def test_outline_text_accepts_markdown_and_chinese_slide_variants(self) -> None:
        slides = slides_from_outline_text(
            """
            ## PPT 大纲
            **幻灯片 1：封面**
            - AI 产品路线图
            幻灯片 2：市场背景
            1、用户需要更快的原型验证
            2、团队需要统一交付节奏
            ## 解决方案
            建立从需求到演示的自动化生成流程
            - 保留人工编辑入口
            """,
            topic="AI 产品路线图",
        )

        self.assertEqual([slide["title"] for slide in slides[:2]], ["市场背景", "解决方案"])
        self.assertEqual(slides[0]["bullets"][:2], ["用户需要更快的原型验证", "团队需要统一交付节奏"])
        self.assertIn("建立从需求到演示的自动化生成流程", slides[1]["bullets"])
        self.assertIn("保留人工编辑入口", slides[1]["bullets"])


if __name__ == "__main__":
    unittest.main()


def test_presentation_parser_and_layout_boundaries() -> None:
    assert presentations._normalize_bullets({}) == []
    assert presentations.infer_presentation_title("", "Product introduction")
    slides = presentations.slides_from_outline_text(
        "## Presentation outline\n## Slide 1: Overview\nplain body line\n  indented detail\n1. numbered detail\n```ignored",
        topic="Product",
    )
    assert slides
    assert presentations._looks_like_body_line("") is False
    assert presentations._looks_like_body_line("PPT 大纲") is False
    assert presentations._looks_like_body_line("| table |") is False
    assert presentations._drop_duplicate_cover_slide([{"title": "only"}], "only") == [{"title": "only"}]
    assert presentations._default_slides("Git")
    assert presentations._default_slides("")
    assert presentations._fit_font_size("short", base=20, small=10) == 20
    assert presentations._fit_font_size("x" * 500, base=20, small=10) == 10
    assert presentations._fit_font_size("x" * 70, base=20, small=10) == 17


def test_presentation_all_rich_layouts_render_detail_paragraphs() -> None:
    result = presentations.create_presentation(
        "RC Readiness",
        [
            {"title": "Agenda", "layout": "agenda", "bullets": ["Coverage: reach 95%", "Readiness: strict rehearsal"]},
            {"title": "Capabilities", "layout": "cards", "bullets": ["Tests: boundary paths", "CI: all green"]},
            {"title": "Process", "layout": "process", "bullets": ["Measure: baseline", "Cover: failures", "Verify: twice"]},
            {"title": "Comparison", "layout": "comparison", "bullets": ["Before: 90% gate", "After: 95% gate"]},
            {"title": "Principle", "layout": "quote", "bullets": ["Evidence: prove readiness"]},
            {"title": "Summary", "layout": "summary", "bullets": ["Coverage: buffered", "Tag: deferred"]},
            {"title": "Content", "layout": "content", "bullets": ["Lead: detailed explanation"]},
        ],
    )
    assert result["slideCount"] == 9
    path = presentations.resolve_generated_file(result["fileId"])
    assert path is not None and path.is_file()
    path.unlink(missing_ok=True)


def test_presentation_invalid_entries_leave_no_valid_slides() -> None:
    with unittest.TestCase().assertRaises(AppError):
        presentations.create_presentation("Title", [None, "bad"])
