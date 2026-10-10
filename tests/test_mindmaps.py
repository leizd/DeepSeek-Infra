from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.tool_runtime import generated_files
from deepseek_infra.infra.tool_runtime.generated_files import resolve_generated_file, save_generated_file_to_downloads
from deepseek_infra.infra.tool_runtime import mindmaps
from deepseek_infra.infra.tool_runtime.mindmaps import create_mindmap
from deepseek_infra.infra.tool_runtime.tools import available_tool_definitions


SAMPLE_NODES = [
    {
        "label": "Market analysis",
        "children": [
            {"label": "User profile", "children": []},
            {"label": "Competition", "children": [{"label": "Pricing", "children": []}]},
        ],
    },
    {
        "label": "Product strategy",
        "children": [
            {"label": "Core features", "children": []},
            {"label": "Launch rhythm", "children": []},
        ],
    },
]


class MindMapTests(unittest.TestCase):
    def test_create_mindmap_generates_svg_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with unittest.mock.patch.object(generated_files, "GENERATED_DIR", Path(tmp)):
                result = create_mindmap("Growth plan", SAMPLE_NODES, subtitle="2026")
                self.assertEqual(result["format"], "svg")
                self.assertGreaterEqual(result["nodeCount"], 6)
                self.assertTrue(result["downloadUrl"].startswith("/api/download?id="))
                path = resolve_generated_file(result["fileId"])
                self.assertIsNotNone(path)
                assert path is not None
                self.assertEqual(path.suffix, ".svg")
                text = path.read_text(encoding="utf-8")
                self.assertIn("<svg", text)
                self.assertIn("Growth plan", text)

    def test_save_to_downloads_uses_svg_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            generated_dir = Path(tmp) / ".generated"
            downloads_dir = Path(tmp) / "Downloads"
            with unittest.mock.patch.object(generated_files, "GENERATED_DIR", generated_dir):
                result = create_mindmap("Saved mind map", SAMPLE_NODES)
                saved = save_generated_file_to_downloads(result["fileId"], filename="saved-mind-map.pdf", downloads_dir=downloads_dir)
            self.assertTrue(Path(saved["path"]).is_file())
            self.assertEqual(Path(saved["path"]).suffix, ".svg")

    def test_invalid_inputs_raise(self) -> None:
        with self.assertRaises(AppError) as cm:
            create_mindmap("", SAMPLE_NODES)
        self.assertEqual(cm.exception.code, ErrorCode.INVALID_PAYLOAD)
        with self.assertRaises(AppError):
            create_mindmap("Empty", [])

    def test_create_mindmap_registered_as_tool(self) -> None:
        tools = available_tool_definitions()
        names = [tool["function"]["name"] for tool in tools]
        self.assertIn("create_mindmap", names)
        tool = next(item for item in tools if item["function"]["name"] == "create_mindmap")
        params = tool["function"]["parameters"]["properties"]
        self.assertIn("nodes", params)
        node_schema = params["nodes"]["items"]
        self.assertIn("label", node_schema["required"])
        self.assertIn("children", node_schema["required"])


if __name__ == "__main__":
    unittest.main()


def test_mindmap_normalization_depth_limit_node_limit_and_aliases(monkeypatch) -> None:
    counter = [0]
    assert mindmaps._normalize_nodes("bad", depth=1, counter=counter) == []
    assert mindmaps._normalize_nodes(["x"], depth=mindmaps.MAX_DEPTH + 1, counter=[0]) == []
    monkeypatch.setattr(mindmaps, "MAX_NODES", 2)
    nodes = mindmaps._normalize_nodes([None, "", "one", {"title": "two"}, {"name": "three"}], depth=1, counter=[0])
    assert [node["label"] for node in nodes] == ["one", "two"]
    assert mindmaps._count_nodes([{"children": [{"children": []}]}]) == 2


def test_mindmap_empty_layout_tokenization_and_wrapping() -> None:
    assert mindmaps._layout_cluster_nodes([]) == ([], 0.0, 0.0)
    clusters, width, height = mindmaps._layout_clusters([{"label": "Empty cluster", "children": []}])
    assert clusters[0]["nodes"] == [] and width > 0 and height > 0
    assert mindmaps._is_cjk("中") is True
    assert mindmaps._is_ascii_word("word") is True
    assert mindmaps._is_ascii_word("") is False
    assert mindmaps._tokenize("hello 世界 test") == ["hello", "世", "界", "test"]
    assert mindmaps._wrap_label("", max_width=10, font_size=12) == [""]
    wrapped = mindmaps._wrap_label("one two three four five", max_width=30, font_size=12, max_lines=2)
    assert len(wrapped) == 2
    assert mindmaps._join_tokens(["中", "文"]) == "中文"
    assert mindmaps._join_tokens(["中", "word", "文"]) == "中 word 文"


def test_mindmap_render_edges_clip_title_xml_and_write(tmp_path) -> None:
    nodes = [{"label": "Root", "children": [{"label": "Child", "children": [{"label": "Leaf", "children": []}]}]}]
    clusters, width, height = mindmaps._layout_clusters(nodes)
    svg = mindmaps._render_svg("<Title>", "Subtitle", clusters, width, height)
    assert "&lt;Title&gt;" in svg
    assert "<path" in svg
    assert mindmaps._clip_title("short", 500) == "short"
    assert mindmaps._clip_title("very long title " * 20, 80)
    assert mindmaps._outline(nodes)[0]["children"][0]["label"] == "Child"
    assert mindmaps._xml('"<&') == "&quot;&lt;&amp;"
    target = tmp_path / "map.svg"
    mindmaps._write_text(target, svg)
    assert target.read_text(encoding="utf-8") == svg
