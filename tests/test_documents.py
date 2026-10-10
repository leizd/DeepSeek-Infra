from __future__ import annotations

import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.tool_runtime import generated_files
from deepseek_infra.infra.tool_runtime.documents import create_document
from deepseek_infra.infra.tool_runtime.generated_files import resolve_generated_file, save_generated_file_to_downloads
from deepseek_infra.infra.tool_runtime.tools import available_tool_definitions

SAMPLE_SECTIONS = [
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


class DocumentTests(unittest.TestCase):
    def test_create_word_document_generates_valid_docx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(generated_files, "GENERATED_DIR", Path(tmp)):
                result = create_document("docx", "季度产品报告", SAMPLE_SECTIONS, subtitle="2026 Q3")
                self.assertEqual(result["format"], "docx")
                self.assertEqual(result["sectionCount"], 2)
                self.assertTrue(result["filename"].endswith(".docx"))
                self.assertTrue(result["downloadUrl"].startswith("/api/download?id="))
                path = resolve_generated_file(result["fileId"])
                self.assertIsNotNone(path)
                assert path is not None
                self.assertEqual(path.suffix, ".docx")
                # 合法的 .docx 是一个含 word/document.xml 的 zip 容器
                with zipfile.ZipFile(path) as archive:
                    self.assertIn("word/document.xml", archive.namelist())

    def test_create_pdf_document_generates_valid_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(generated_files, "GENERATED_DIR", Path(tmp)):
                result = create_document("pdf", "季度产品报告", SAMPLE_SECTIONS)
                self.assertEqual(result["format"], "pdf")
                path = resolve_generated_file(result["fileId"])
                self.assertIsNotNone(path)
                assert path is not None
                self.assertEqual(path.suffix, ".pdf")
                self.assertEqual(path.read_bytes()[:5], b"%PDF-")

    def test_format_alias_word_maps_to_docx(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(generated_files, "GENERATED_DIR", Path(tmp)):
                result = create_document("word", "标题", SAMPLE_SECTIONS)
        self.assertEqual(result["format"], "docx")
        self.assertTrue(result["filename"].endswith(".docx"))

    def test_cjk_title_preserved_in_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(generated_files, "GENERATED_DIR", Path(tmp)):
                result = create_document("docx", "中文标题报告", SAMPLE_SECTIONS)
        self.assertTrue(result["filename"].startswith("中文标题报告"))

    def test_resolve_finds_docx_and_pdf_cross_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(generated_files, "GENERATED_DIR", Path(tmp)):
                docx_result = create_document("docx", "甲文档", SAMPLE_SECTIONS)
                pdf_result = create_document("pdf", "乙文档", SAMPLE_SECTIONS)
                docx_path = resolve_generated_file(docx_result["fileId"])
                pdf_path = resolve_generated_file(pdf_result["fileId"])
                assert docx_path is not None and pdf_path is not None
                self.assertEqual(docx_path.suffix, ".docx")
                self.assertEqual(pdf_path.suffix, ".pdf")

    def test_save_to_downloads_uses_real_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            generated_dir = Path(tmp) / ".generated"
            downloads_dir = Path(tmp) / "Downloads"
            with unittest.mock.patch.object(generated_files, "GENERATED_DIR", generated_dir):
                result = create_document("docx", "保存测试", SAMPLE_SECTIONS)
                # 前端可能误把链接文字当成 .pptx，后端必须以真实文件后缀为准改成 .docx
                saved = save_generated_file_to_downloads(result["fileId"], filename="保存测试.pptx", downloads_dir=downloads_dir)
            self.assertTrue(Path(saved["path"]).is_file())
            self.assertEqual(Path(saved["path"]).name, "保存测试.docx")

    def test_invalid_inputs_raise(self) -> None:
        with self.assertRaises(AppError) as cm:
            create_document("docx", "", SAMPLE_SECTIONS)
        self.assertEqual(cm.exception.code, ErrorCode.INVALID_PAYLOAD)
        with self.assertRaises(AppError):
            create_document("docx", "标题", [])
        with self.assertRaises(AppError):
            create_document("txt", "标题", SAMPLE_SECTIONS)

    def test_ragged_table_rows_do_not_crash(self) -> None:
        sections = [
            {
                "heading": "数据",
                "body": [],
                "bullets": [],
                "table": {"headers": ["A", "B", "C"], "rows": [["1"], ["1", "2", "3", "4", "5"]]},
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(generated_files, "GENERATED_DIR", Path(tmp)):
                for fmt in ("docx", "pdf"):
                    result = create_document(fmt, "表格测试", sections)
                    self.assertEqual(result["sectionCount"], 1)
                    self.assertTrue(result["outline"][0]["hasTable"])

    def test_create_document_registered_as_tool(self) -> None:
        tools = available_tool_definitions()
        names = [tool["function"]["name"] for tool in tools]
        self.assertIn("create_document", names)
        tool = next(item for item in tools if item["function"]["name"] == "create_document")
        params = tool["function"]["parameters"]["properties"]
        self.assertEqual(params["format"]["enum"], ["docx", "pdf"])
        section_schema = params["sections"]["items"]
        for key in ("heading", "body", "bullets", "table"):
            self.assertIn(key, section_schema["properties"])
            self.assertIn(key, section_schema["required"])


if __name__ == "__main__":
    unittest.main()
