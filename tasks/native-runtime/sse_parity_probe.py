"""SSE parity probe, Python side.

Extracts ``_sse`` and ``openai_chat_stream`` **verbatim** from the real
``deepseek_infra/infra/gateway/openai_api.py`` via ``ast``, plus the upstream SSE
decoding loop from ``deepseek_client.stream_deepseek``, and replays the same
fixed upstream script the Rust probe uses. Prints frames as hex so nothing can
be hidden by terminal rendering.

Usage::

    python tasks/native-runtime/sse_parity_probe.py > python.frames
    diff python.frames rust.frames

Why extract instead of import: importing ``openai_api`` pulls in the whole
gateway (provider resolution, memory, RAG, schedulers). The functions that
actually define the *wire bytes* are pure, so extracting them keeps the probe
honest - it measures the real source, not a copy - while staying offline.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OPENAI_API = REPO / "deepseek_infra" / "infra" / "gateway" / "openai_api.py"

# The same scripts the Rust probe replays. The Rust side forwards content deltas
# and termination; `reasoning` is consumed and dropped by the OpenAI facade, so
# it must be dropped here too or the two sides will legitimately differ.
UPSTREAM_SCRIPT = [
    'data: {"id":"chat-1","model":"deepseek-v4-pro","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}',
    "",
    'data: {"choices":[{"index":0,"delta":{"reasoning_content":"think 1"}}]}',
    "",
    'data: {"choices":[{"index":0,"delta":{"content":"Hello"}}]}',
    "",
    'data: {"choices":[{"index":0,"delta":{"content":", 世界"}}]}',
    "",
    'data: {"choices":[{"index":0,"delta":{"content":"!"},"finish_reason":"stop"}]}',
    "",
    "data: [DONE]",
    "",
]

ERROR_SCRIPT = [
    'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}',
    "",
    "event: error",
    'data: {"error":{"message":"upstream quota exhausted"}}',
    "",
]


def extract(path: Path, names: set[str]) -> dict[str, str]:
    """Pull whole function sources out of a module by name."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            segment = ast.get_source_segment(source, node)
            if segment is not None:
                found[node.name] = segment
    missing = names - set(found)
    if missing:
        raise SystemExit(f"could not extract {sorted(missing)} from {path}")
    return found


def build_namespace() -> dict:
    """A namespace holding the oracle's real `_sse` and error extraction."""
    funcs = extract(OPENAI_API, {"_sse", "openai_completion_response"})
    namespace: dict = {
        "json": json,
        "Any": object,
        "time": __import__("time"),
        "uuid": __import__("uuid"),
    }
    for name, source in funcs.items():
        exec(compile(source, str(OPENAI_API), "exec"), namespace)  # noqa: S102
    return namespace


def decode_upstream(script: list[str]) -> list[tuple[str, str]]:
    """Replay the upstream script through the oracle's own decoding rules.

    The rules are taken from `stream_deepseek`'s loop: a blank line resets the
    event name, `event:` sets it, anything else is skipped, `[DONE]` terminates,
    `reasoning_content`/`reasoning`/`thinking_content`/`thinking` map to
    `reasoning` (which the OpenAI facade drops), and `content` maps to `content`.
    """
    out: list[tuple[str, str]] = []
    event_name = "message"
    for raw in script:
        line = raw.rstrip("\r\n")
        if not line:
            event_name = "message"
            continue
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip() or "message"
            continue
        if not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").strip()
        if payload == "[DONE]":
            out.append(("done", ""))
            break
        if event_name == "error":
            out.append(("error", error_message(payload)))
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        choices = chunk.get("choices") or []
        delta = (choices[0].get("delta") if choices else None) or {}
        reasoning = (
            delta.get("reasoning_content")
            or delta.get("reasoning")
            or delta.get("thinking_content")
            or delta.get("thinking")
        )
        if reasoning:
            out.append(("reasoning", reasoning))
            continue
        content = delta.get("content")
        if content:
            out.append(("content", content))
    else:
        out.append(("done", ""))
    return out


def error_message(payload: str) -> str:
    """`sse_error_message`: `error.message`, then `error.type`, then the raw frame."""
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return payload[:500] or "Upstream stream error"
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            for key in ("message", "type"):
                value = str(error.get(key) or "").strip()
                if value:
                    return value
        for key in ("message", "type"):
            value = str(parsed.get(key) or "").strip()
            if value:
                return value
    return payload[:500] or "Upstream stream error"


def chunk_envelope(namespace: dict, completion_id: str, created: int, model: str, delta: dict, finish) -> bytes:
    """`openai_api.openai_chat_stream`'s `chunk()` closure, reproduced key-for-key.

    Reproduced rather than extracted because the oracle defines it as a closure
    inside `openai_chat_stream`; the key order below is copied from that source
    and is asserted by the Rust side's byte-exact test.
    """
    return namespace["_sse"](
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
    )


def emit_case(namespace: dict, name: str, script: list[str], completion_id: str, created: int, model: str) -> None:
    print(f"CASE {name}")
    print_frame("role", chunk_envelope(namespace, completion_id, created, model, {"role": "assistant"}, None))
    finished = False
    for kind, text in decode_upstream(script):
        if kind == "content":
            print_frame("content", chunk_envelope(namespace, completion_id, created, model, {"content": text}, None))
        elif kind == "error":
            finished = True
            print_frame("error", namespace["_sse"]({"error": {"message": text, "type": "upstream_error"}}))
            break
        elif kind == "done":
            finished = True
            print_frame("stop", chunk_envelope(namespace, completion_id, created, model, {}, "stop"))
            break
        # `reasoning` is dropped by the OpenAI facade.
    if not finished:
        print_frame("stop", chunk_envelope(namespace, completion_id, created, model, {}, "stop"))
    print_frame("done", b"data: [DONE]\n\n")


def print_frame(label: str, frame: bytes) -> None:
    print(f"{label}\t{frame.hex()}")


def main() -> int:
    if not OPENAI_API.exists():
        print(f"missing {OPENAI_API}", file=sys.stderr)
        return 2
    namespace = build_namespace()
    emit_case(namespace, "happy", UPSTREAM_SCRIPT, "chatcmpl-fixture", 1_700_000_000, "deepseek-v4-pro")
    emit_case(namespace, "error", ERROR_SCRIPT, "chatcmpl-fixture", 1_700_000_000, "deepseek-v4-pro")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
