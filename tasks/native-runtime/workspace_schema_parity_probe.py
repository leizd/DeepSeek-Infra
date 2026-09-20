"""Workspace Core schema parity probe, Python side.

Covers `deepseek_infra/infra/workspace/schema.py` — the shared normalisation and
redaction layer every Workspace store (projects, saved items, artifacts,
conversations) normalises through, and from which `infra/memory/schema.py` imports
`normalize_source_ref`.

**Why the clock is injected.** `timestamp_ms_to_iso` is pure over its argument, so no
clock is needed; `new_id` uses `secrets.token_hex`, so the probe reports the id's
*shape* (the prefix, then 16 lowercase hex characters) rather than its value.

**Platform note.** `runtime_relative_path` branches on `Path.is_absolute()`, which
disagrees between POSIX and Windows for a path like `/etc/passwd` (no drive letter).
The probe reports what the *running* platform does and the Rust side asserts the same
split, so the comparison is valid on either host.

Usage::

    $env:PYTHONIOENCODING = "utf-8"; $env:PYTHONUTF8 = "1"
    python tasks/native-runtime/workspace_schema_parity_probe.py > python.json
    cd rust; cargo run -p deepseek-policy --example workspace_schema_parity_probe > ../rust.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")

TITLE_CASES: list[object] = [
    "  a   b  ",
    "",
    "   ",
    None,
    "a\r\nb",
    "z" * 200,
    "z" * 160,
    5,
    True,
    ["x"],
    {"a": 1},
    "中文  标题",
]

CONTENT_CASES: list[object] = [
    "  a\r\nb  ",
    "a\nb",
    "a\rb",
    "",
    None,
    "z" * 200_001,
    "z" * 200_000,
    "  spaced  ",
    5,
    ["x"],
]

TAGS_CASES: list[object] = [
    ["Rust", "rust", " RUST ", "Go"],
    ["", "  ", "ok"],
    "not-a-list",
    None,
    [1, 2, 3],
    [None, "a"],
    ["a" * 50],
    ["a" * 40],
    [f"t{i}" for i in range(40)],
    ["中文", "中文"],
    [],
]

SOURCE_REF_CASES: list[object] = [
    {},
    {"bad key!": "x", "ok": 1},
    {"nested": {"ok": 1, "drop me!": 2}},
    {"emptyNested": {"!!!": 1}},
    {"list": [1, None, "two"]},
    {"emptyList": [None]},
    {"nothing": None, "flag": True, "zero": 0},
    {"!!!": 1},
    {"a" * 100: "x"},
    {"deep": {"deeper": {"deepest": [1, 2, 3]}}},
    {"many": list(range(30))},
    "not-a-dict",
    None,
    [1, 2],
]

SAVED_TYPE_CASES: list[object] = [
    "chat_snippet",
    " Chat_Snippet ",
    "nonsense",
    "",
    None,
    5,
]

SAVED_PURPOSE_CASES: list[object] = [
    "reference",
    "export_fragment",
    " MEMORY_CANDIDATE ",
    "nonsense",
    "",
    None,
]

ARTIFACT_TYPE_CASES: list[tuple[object, str]] = [
    ("md", ""),
    (".svg", ""),
    ("PPTX", ""),
    (None, "a/b.md"),
    (None, "a/b.svg"),
    (None, "a/b.MD"),
    (None, "a/b"),
    (None, ""),
    ("", "a/b.json"),
    ("nonsense", "a.md"),
    ("markdown", ""),
    (5, ""),
]

EXPORT_FORMAT_CASES: list[object] = [
    None,
    "",
    "md",
    ".JSON",
    "zip",
    "nonsense",
    5,
]

TIMESTAMP_CASES: list[object] = [
    0,
    -1,
    1_500,
    1_789_815_737_000,
    "1789815737000",
    "abc",
    "",
    None,
    True,
    False,
    1.9,
]

PATH_CASES: list[str] = [
    "a/b.md",
    "../etc/passwd",
    "a/../../etc",
    "",
    ".",
    "a\\b.md",
    "/etc/passwd",
    "a/./b",
    "a//b",
    "..",
]

SAFE_FILENAME_CASES: list[str] = [
    "my file!.txt",
    "...",
    "",
    "中文文档",
    "a" * 200,
    "a/b",
    "  spaced  ",
]

REDACT_CASES: list[str] = [
    "Authorization: Bearer abcdefghijklmnop",
    "key sk-abcdefghijklmno here",
    "see ?api_key=supersecret&x=1",
    "api_key=supersecret",
    "password=abc",
    "nothing sensitive here",
    "Bearer abcdefghijklmnop",
    "token=abcdefgh",
    "refresh_token=abcdefgh&y=2",
]

REDACT_VALUE_CASES: list[object] = [
    {"apiKey": {"nested": "secret"}, "name": "ok"},
    {"list": ["sk-abcdefghijklmno"]},
    {"cookie": "x", "Authorization": "y"},
    "sk-abcdefghijklmno",
    [{"password": "p"}],
    None,
    5,
]

CONTAINS_SECRET_CASES: list[str] = [
    "sk-abcdefghijklmno",
    "Bearer abcdefghijklmnop",
    "password=supersecret",
    "?token=abcdefgh",
    "just a normal document",
    "",
]

ID_CASES: list[str] = ["save", "!!!", "My-ID", "art", "", "_x_", "a b"]


def main() -> int:
    workspace = tempfile.mkdtemp(prefix="workspace-schema-probe-")
    os.environ["DEEPSEEK_INFRA_ROOT"] = workspace

    from deepseek_infra.core import config
    from deepseek_infra.infra.workspace import schema

    out: dict = {}

    for index, value in enumerate(TITLE_CASES):
        out[f"title::{index}"] = schema.normalize_title(value, default="Untitled")

    for index, value in enumerate(CONTENT_CASES):
        out[f"content::{index}"] = schema.normalize_content(value)
        out[f"description::{index}"] = schema.normalize_description(value)

    for index, value in enumerate(TAGS_CASES):
        out[f"tags::{index}"] = schema.normalize_tags(value)

    for index, value in enumerate(SOURCE_REF_CASES):
        out[f"source_ref::{index}"] = schema.normalize_source_ref(value)

    for index, value in enumerate(SAVED_TYPE_CASES):
        try:
            out[f"saved_type::{index}"] = {"value": schema.normalize_saved_type(value)}
        except Exception as exc:  # noqa: BLE001 - the probe records the refusal
            code = getattr(getattr(exc, "code", None), "value", None)
            out[f"saved_type::{index}"] = {"refused": True, "code": code}

    for index, value in enumerate(SAVED_PURPOSE_CASES):
        out[f"saved_purpose::{index}"] = schema.normalize_saved_purpose(value)

    for index, (value, path) in enumerate(ARTIFACT_TYPE_CASES):
        try:
            out[f"artifact_type::{index}"] = {"value": schema.normalize_artifact_type(value, path=path)}
        except Exception as exc:  # noqa: BLE001
            code = getattr(getattr(exc, "code", None), "value", None)
            out[f"artifact_type::{index}"] = {"refused": True, "code": code}

    for index, value in enumerate(EXPORT_FORMAT_CASES):
        try:
            out[f"export_format::{index}"] = {"value": schema.normalize_export_format(value)}
        except Exception as exc:  # noqa: BLE001
            code = getattr(getattr(exc, "code", None), "value", None)
            out[f"export_format::{index}"] = {"refused": True, "code": code}

    for index, value in enumerate(TIMESTAMP_CASES):
        out[f"timestamp::{index}"] = schema.timestamp_ms_to_iso(value)

    for index, value in enumerate(PATH_CASES):
        try:
            out[f"runtime_path::{index}"] = {"value": schema.runtime_relative_path(value)}
        except Exception as exc:  # noqa: BLE001
            code = getattr(getattr(exc, "code", None), "value", None)
            out[f"runtime_path::{index}"] = {"refused": True, "code": code, "message": str(exc)}

    for index, value in enumerate(SAFE_FILENAME_CASES):
        out[f"safe_filename::{index}"] = schema.safe_filename(value, default="item")

    for index, value in enumerate(REDACT_CASES):
        out[f"redact::{index}"] = schema.redact_sensitive_text(value)

    for index, value in enumerate(REDACT_VALUE_CASES):
        out[f"redact_value::{index}"] = schema.redact_value(value)

    for index, value in enumerate(CONTAINS_SECRET_CASES):
        out[f"contains_secret::{index}"] = schema.contains_secret(value)

    for index, prefix in enumerate(ID_CASES):
        generated = schema.new_id(prefix)
        head, _, tail = generated.rpartition("_")
        out[f"new_id::{index}"] = {
            "prefix": head,
            "tail": "<hex16>" if HEX16.match(tail) else tail,
        }

    # --- the file helpers, against the real temp root ---------------------------
    root = Path(workspace)
    missing = root / "nope.json"
    out["read_json::missing"] = schema.read_json_file(missing, default={"items": []})

    malformed = root / "bad.json"
    malformed.write_text("not json", encoding="utf-8")
    out["read_json::malformed"] = schema.read_json_file(malformed, default={"items": []})

    scalar = root / "scalar.json"
    scalar.write_text("[]", encoding="utf-8")
    out["read_json::scalar"] = schema.read_json_file(scalar, default={"items": []})

    good = root / "good.json"
    good.write_text('{"items":[1]}', encoding="utf-8")
    out["read_json::good"] = schema.read_json_file(good, default={})

    target = root / "project.json"
    schema.write_json_atomic(target, {"id": "p1", "nested": {"a": 1}})
    out["write_json::text"] = target.read_text(encoding="utf-8")
    out["write_json::temp_left"] = (root / "project.json.tmp").exists()
    out["write_json::generation"] = (root / ".workspace-generation").read_text(encoding="utf-8").strip()

    # `resolve_runtime_path` needs the configured roots; report them relative to the
    # probe root so the two sides compare the same shape.
    out["resolve::generated"] = schema.resolve_runtime_path(f"{config.GENERATED_DIR.name}/a.svg").as_posix().replace(
        Path(workspace).as_posix(), "<root>"
    )
    out["resolve::projects"] = schema.resolve_runtime_path(f"{config.PROJECTS_DIR.name}/p1/x.json").as_posix().replace(
        Path(workspace).as_posix(), "<root>"
    )
    out["resolve::other"] = schema.resolve_runtime_path("other/x").as_posix().replace(
        Path(workspace).as_posix(), "<root>"
    )

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
