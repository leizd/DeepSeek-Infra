"""Oracle-side parity probe: measure the Python normalizer against the same
inputs the Rust probe uses.

This intentionally extracts `normalize_chat_messages` and its helpers straight
out of the real source with `ast` instead of importing the module, because
`deepseek_client` pulls in `deepseek_infra.infra.rag.files`, which needs
`defusedxml`. Importing would either fail or force the whole dependency set to
be installed just to answer a question about one function.

What is stubbed: only `build_attachment_context` (the attachment/OCR path,
irrelevant to role and blank-content handling). Everything else - the role
filtering, the blank-content `continue`, the tool_call_id check - is the real
code from the repository, so the printed behavior is the real behavior.

Run from the repository root:

    python tasks/native-runtime/oracle_parity_probe.py

Compare the output against:

    cargo run -p deepseek-gateway --example oracle_parity_probe

Both probes must list the same case names; the recorded side-by-side result and
the decision taken from it live in `tasks/native-runtime/worker-execution-plan.md`.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT = REPO_ROOT / "deepseek_infra" / "infra" / "gateway" / "deepseek_client.py"
PAYLOAD = REPO_ROOT / "deepseek_infra" / "infra" / "gateway" / "chat_payload.py"

# Extracted verbatim from the real sources. `_image_content_parts` and
# `normalize_tool_calls` are dependencies of `normalize_chat_messages`.
WANTED_FROM_CLIENT = {"normalize_chat_messages", "normalize_tool_calls", "_image_content_parts"}
WANTED_FROM_PAYLOAD = {"expanded_message_content"}


def _extract(path: Path, wanted: set[str]) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            segment = ast.get_source_segment(source, node)
            assert segment is not None, f"no source segment for {node.name}"
            found.append(segment)
    missing = wanted - {name for name in wanted if any(name in seg.split("(")[0] for seg in found)}
    assert not missing, f"{path.name}: could not extract {sorted(missing)}"
    return found


def load_oracle() -> object:
    bodies = _extract(CLIENT, WANTED_FROM_CLIENT)
    bodies += _extract(PAYLOAD, WANTED_FROM_PAYLOAD)
    namespace: dict[str, object] = {"__builtins__": __builtins__, "Any": object}
    # The only stubbed dependency. Attachment context is orthogonal to the role
    # and blank-content rules this probe measures.
    namespace["build_attachment_context"] = lambda attachments, query: ""
    code = "from __future__ import annotations\n\n" + "\n\n".join(bodies)
    exec(compile(code, "<oracle-extract>", "exec"), namespace)  # noqa: S102
    return namespace["normalize_chat_messages"]


CASES: dict[str, list[object]] = {
    "A blank user then real": [
        {"role": "user", "content": "  "},
        {"role": "user", "content": "real"},
    ],
    "B only blank user": [{"role": "user", "content": "   "}],
    "C blank assistant then real": [
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "real"},
    ],
    "D content null then real": [
        {"role": "user", "content": None},
        {"role": "user", "content": "real"},
    ],
    "E system then real": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "real"},
    ],
    "F non-dict then real": ["nope", {"role": "user", "content": "real"}],
    "G tool missing id": [
        {"role": "tool", "content": "x"},
        {"role": "user", "content": "real"},
    ],
    "H assistant tool_calls blank": [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        }
    ],
    "I user content list text": [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    ],
}


def main() -> int:
    normalize = load_oracle()
    for name, messages in CASES.items():
        normalized = normalize(messages)  # type: ignore[operator]
        roles = [item["role"] for item in normalized]
        print(f"{name:30} out={roles}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
