"""Freeze A2A message coercion from the actual Python implementation."""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "rust/crates/deepseek-gateway/tests/fixtures/a2a_message_oracle.json"


def generate() -> str:
    source = ROOT / "deepseek_infra/infra/agent_runtime/a2a.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_text_from_message")
    namespace: dict[str, Any] = {"Any": Any}
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, str(source), "exec"), namespace)
    values: list[Any] = [None, False, True, 0, 42, -2, 1.5, "", " \t", "\x1c\x1f", "你好", [], {},
                         ["a", None, True], {"a": False, "b": [1, "two"]}]
    messages: list[Any] = [None, [], {}, {"parts": None}, {"parts": [None, "text"]}]
    messages += [{"parts": [{"kind": "text", "text": value}]} for value in values]
    kinds: list[Any] = [None, "", False, 0, [], {}, True, 1, ["text"], "image"]
    messages += [{"parts": [{"kind": kind, "type": "text", "text": "fallback"}]} for kind in kinds]
    messages += [{"parts": [{"type": "text", "text": " first "}, {"kind": "text", "text": "second"}]}]
    cases = [{"message": message, "text": namespace["_text_from_message"](message)} for message in messages]
    return json.dumps(cases, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    expected = generate()
    if args.write:
        FIXTURE.write_text(expected, encoding="utf-8")
    if not FIXTURE.exists() or FIXTURE.read_text(encoding="utf-8") != expected:
        raise SystemExit("A2A message oracle drift; review and regenerate with --write")
    print(f"A2A message oracle: {len(json.loads(expected))} cases verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
