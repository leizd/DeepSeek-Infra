"""Measure: does an incoming user-supplied `system` turn survive to the upstream body?

Runs the REAL build_deepseek_request from source (ast-extracted), stubbing only
orthogonal deps, and prints the resulting messages array.
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass as _dataclass
from pathlib import Path

SOURCE = Path(r"D:\deepseek\deepseek_infra\infra\gateway\deepseek_client.py")
source_text = SOURCE.read_text(encoding="utf-8")
module_ast = ast.parse(source_text)

WANTED = {
    "normalize_chat_messages",
    "normalize_tool_calls",
    "stable_tool_call_id",
    "canonical_tool_arguments",
    "_image_content_parts",
    "_has_image_content",
    "_validate_request_messages",
    "validate_deepseek_payload",
    "build_deepseek_request",
    "append_context_to_latest_user",
    "format_current_time_context",
    "build_dynamic_turn_context",
    "format_context_summary_context",
}

segments = []
for node in module_ast.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANTED:
        segment = ast.get_source_segment(source_text, node)
        if segment is None:
            raise SystemExit(f"could not extract {node.name}")
        segments.append(segment)

code = "\n\n".join(segments)
code = code.replace("from __future__ import annotations\n", "")

ns: dict = {}

# --- orthogonal stubs -------------------------------------------------------
def expanded_message_content(message):
    return str(message.get("content") or "").strip()

def empty_memory_state(payload):
    return {"enabled": False, "context": "", "hitCount": 0}

def build_attachment_context(attachments, content):
    return ""

def format_context_summary_context(summary):
    return "[Context summary]\n" + str(summary)

class AppError(Exception):
    def __init__(self, message, code=None, status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status

class _Code:
    MISSING_API_KEY = "missing_api_key"
    INVALID_PAYLOAD = "invalid_payload"
    SUPPORTED_MODELS = None
    INVALID_MESSAGE_CONTENT = "invalid_message_content"

class _ErrorCode:
    MISSING_API_KEY = "missing_api_key"
    INVALID_PAYLOAD = "invalid_payload"
    CONTEXT_COMPRESSION_REQUIRED = "context_compression_required"

class _Settings:
    deepseek_api_key = "server-secret"


class _Router:
    @staticmethod
    def is_auto_request(payload):
        return False
    @staticmethod
    def route_request(payload):
        raise AssertionError("not expected")

class _Budget:
    @staticmethod
    def budget_policy_from_payload(payload):
        class P:
            downgrade = False
            policy = "none"
            def to_dict(self):
                return {}
        return P()

class _ModelName:
    pass

ns.update({
    "Any": object,
    "dataclass": __import__("dataclasses").dataclass,
    "datetime": __import__("datetime").datetime,
    "timezone": __import__("datetime").timezone,
    "json": json,
    "AppError": AppError,
    "ErrorCode": _ErrorCode,
    "settings": _Settings(),
    "DEFAULT_MODEL": "deepseek-v4-pro",
    "SUPPORTED_MODELS": {"deepseek-v4-pro", "deepseek-v4-flash"},
    "MESSAGE_HARD_LIMIT": 40,
    "TOOL_PARALLEL_SYSTEM_HINT": "[parallel tools hint]",
    "TOOL_BUDGET_EXHAUSTED_PROMPT": "answer now",
    "CURRENT_TIME_CONTEXT_HEADER": "[Current time]",
    "expanded_message_content": expanded_message_content,
    "empty_memory_state": empty_memory_state,
    "build_attachment_context": build_attachment_context,
    "format_context_summary_context": format_context_summary_context,
    "model_router": _Router(),
    "budget_manager": _Budget(),
    "normalize_model_name": lambda value: str(value or "").strip(),
    "tools_for_payload": lambda payload: [],
    "forced_artifact_tool_name": lambda payload, tools: None,
    "prepare_memory_state": lambda payload: {"enabled": False, "context": "", "hitCount": 0},
    "normalize_reasoning_effort": lambda value: str(value or "high"),
    "count_payload_attachments": lambda messages: 0,
    "manage_request_body": lambda body, allow_sliding_window=False: (body, {}),
    "merge_context_manager_diagnostics": lambda diagnostics, update: diagnostics,
    "context_taint": type("_T", (), {"build_taint_report": staticmethod(lambda body: None)})(),
})


@_dataclass(frozen=True)
class PreparedDeepSeekRequest:
    api_key: str
    body: dict
    diagnostics: dict


ns["PreparedDeepSeekRequest"] = PreparedDeepSeekRequest

exec(compile(code, "<oracle>", "exec"), ns)  # noqa: S102

build = ns["build_deepseek_request"]
normalize = ns["normalize_chat_messages"]


def show(label: str, payload: dict) -> None:
    print(f"--- {label} ---")
    normalized = normalize(payload["messages"])
    print("  normalize_chat_messages ->", json.dumps(normalized, ensure_ascii=False))
    try:
        prepared = build(payload, stream=False)
    except Exception as exc:  # noqa: BLE001
        print(f"  build_deepseek_request  -> RAISED {type(exc).__name__}: {exc}")
        return
    body = prepared.body
    print("  upstream messages       ->")
    for message in body["messages"]:
        content = message.get("content")
        if isinstance(content, str) and len(content) > 70:
            content = content[:67] + "..."
        print(f"      role={message.get('role'):<10} content={content!r}")


BASE = {
    "apiKey": "server-secret",
    "model": "deepseek-v4-pro",
    "toolsEnabled": False,
    "memoryEnabled": False,
    "searchEnabled": False,
    "semanticCacheEnabled": False,
}

show("CASE 1: incoming system turn + user turn, no systemPrompt", {
    **BASE,
    "messages": [
        {"role": "system", "content": "be concise"},
        {"role": "user", "content": "hello"},
    ],
})

show("CASE 2: incoming system turn + user turn, WITH systemPrompt", {
    **BASE,
    "systemPrompt": "you are a helpful assistant",
    "messages": [
        {"role": "system", "content": "be concise"},
        {"role": "user", "content": "hello"},
    ],
})

show("CASE 3: only incoming system turn (no user)", {
    **BASE,
    "messages": [{"role": "system", "content": "be concise"}],
})

show("CASE 4: trailing system becomes dynamic context (round-trip)", {
    **BASE,
    "messages": [
        {"role": "user", "content": "hello"},
        {"role": "system", "content": "[Current time]\n...\n[parallel tools hint]"},
    ],
})
