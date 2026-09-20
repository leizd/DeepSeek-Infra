"""create_mindmap parity probe, Python side.

Pins label cleaning, node normalization, layout, and the SVG bytes of
`create_mindmap` against `deepseek_policy::mindmaps`. File ids are injected so
the store envelope is comparable; the SVG itself does not contain the id.

Usage::

    python tasks/native-runtime/mindmap_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example mindmap_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from deepseek_infra.core.errors import AppError  # noqa: E402
from deepseek_infra.infra.tool_runtime import generated_files, mindmaps  # noqa: E402

SAMPLE = [
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


def error_view(exc: AppError) -> dict[str, Any]:
    return {"error": str(exc), "code": exc.code.value, "status": exc.status}


def make(title: str, nodes: Any, subtitle: str, root: Path, file_id: str = "a" * 32) -> dict[str, Any]:
    generated_files.GENERATED_DIR = root / ".generated"
    with patch("secrets.token_hex", return_value=file_id):
        result = mindmaps.create_mindmap(title, nodes, subtitle=subtitle)
    path = generated_files.resolve_generated_file(result["fileId"])
    assert path is not None
    result = dict(result)
    result["svg"] = path.read_text(encoding="utf-8")
    return result


def main() -> int:
    out: dict[str, Any] = {}
    root = Path(tempfile.mkdtemp(prefix="mindmap-parity-"))
    os.environ["DEEPSEEK_INFRA_ROOT"] = str(root)

    try:
        make("", SAMPLE, "", root)
        out["empty-title"] = "ok"
    except AppError as exc:
        out["empty-title"] = error_view(exc)
    try:
        make("Empty", [], "", root)
        out["empty-nodes"] = "ok"
    except AppError as exc:
        out["empty-nodes"] = error_view(exc)

    grown = make("Growth plan", SAMPLE, "2026", root, file_id="b" * 32)
    out["growth::format"] = grown["format"]
    out["growth::nodeCount"] = grown["nodeCount"]
    out["growth::title"] = grown["title"]
    out["growth::outline"] = grown["outline"]
    out["growth::filename"] = grown["filename"]
    out["growth::downloadUrl"] = grown["downloadUrl"]
    out["growth::svg"] = grown["svg"]

    escaped = make("<Title>", [{"label": "A & B", "children": [{"title": "Child"}]}], "sub", root, file_id="c" * 32)
    out["escape::svg"] = escaped["svg"]

    aliases = make(
        "Aliases",
        [None, "", "one", {"title": "two"}, {"name": "three"}],
        "",
        root,
        file_id="d" * 32,
    )
    out["aliases::outline"] = aliases["outline"]
    out["aliases::nodeCount"] = aliases["nodeCount"]

    shutil.rmtree(root, ignore_errors=True)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
