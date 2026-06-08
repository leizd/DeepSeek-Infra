"""Local tool execution for DeepSeek function calling."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import csv
import io
import json
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

from deepseek_infra.core.config import FILE_CACHE_DIR, PROJECTS_DIR, SEARCH_CACHE_DIR, SEARCH_CACHE_MAX_AGE_SECONDS, TAVILY_TIMEOUT_SECONDS
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import query_tokens, score_chunk
from deepseek_infra.infra.tool_runtime.documents import create_document
from deepseek_infra.infra.rag.files import cosine_similarity, extract_html_text, load_cached_file, local_text_vector
from deepseek_infra.infra.rag import local_rag
from deepseek_infra.infra.data.memory import build_memory_suggestion, delete_memories_by_query, normalize_memory_scope, retrieve_memories
from deepseek_infra.infra.tool_runtime.mindmaps import create_mindmap
from deepseek_infra.infra.tool_runtime.presentations import create_presentation
from deepseek_infra.infra.data.projects import list_projects, read_project
from deepseek_infra.infra.data.reminders import create_reminder as create_local_reminder, load_reminders
from deepseek_infra.infra.tool_runtime.slides_skill import SLIDES_SKILL_DESCRIPTION, SLIDES_SKILL_NAME

MAX_TOOL_CALLS_PER_RESPONSE = 6
MAX_TOOL_ROUNDS = 3
MAX_TOOL_RESULT_CHARS = 12_000
MAX_FETCH_BYTES = 2_000_000
MAX_FETCH_REDIRECTS = 5
PYTHON_EVAL_TIMEOUT_SECONDS = 8
URL_FETCH_CACHE_PREFIX = "fetch-url-"
FETCH_URL_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SERIAL_TOOL_NAMES = {"create_reminder", "forget_memory", "suggest_memory", "web_search", "compare_search_results"}


PYTHON_EVAL_RUNNER = r"""
import ast
import json
import math

payload = json.loads(input())
expression = str(payload.get("expression") or "")[:1000]
allowed_names = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "pow": pow,
    "len": len,
    "factorial": math.factorial,
    "comb": math.comb,
    "perm": math.perm,
    "gcd": math.gcd,
    "lcm": math.lcm,
    "sqrt": math.sqrt,
    "log": math.log,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "pi": math.pi,
    "e": math.e,
    "math": math,
}
allowed_nodes = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.Tuple,
    ast.List,
    ast.Dict,
    ast.Set,
    ast.Subscript,
    ast.Slice,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.And,
    ast.Or,
    ast.Attribute,
)


def validate(node):
    if not isinstance(node, allowed_nodes):
        raise ValueError(f"Unsupported syntax: {type(node).__name__}")
    if isinstance(node, ast.Name) and node.id not in allowed_names:
        raise ValueError(f"Unknown name: {node.id}")
    if isinstance(node, ast.Attribute):
        if not isinstance(node.value, ast.Name) or node.value.id != "math" or node.attr.startswith("_") or not hasattr(math, node.attr):
            raise ValueError("Only math.<function> attributes are allowed")
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            if func.id not in allowed_names or not callable(allowed_names[func.id]):
                raise ValueError(f"Function is not allowed: {func.id}")
        elif isinstance(func, ast.Attribute):
            validate(func)
        else:
            raise ValueError("Unsupported function call")
        if len(node.args) > 12 or node.keywords:
            raise ValueError("Too many arguments or keyword arguments are not allowed")
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and abs(node.value) > 10**12:
        raise ValueError("Integer literal is too large")
    for child in ast.iter_child_nodes(node):
        validate(child)


try:
    parsed = ast.parse(expression, mode="eval")
    validate(parsed)
    value = eval(compile(parsed, "<python_eval>", "eval"), {"__builtins__": {}}, allowed_names)
    print(json.dumps({"ok": True, "result": repr(value)[:4000]}, ensure_ascii=False))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)[:500]}, ensure_ascii=False))
"""


@dataclass(frozen=True, slots=True)
class PublicUrlTarget:
    url: str
    scheme: str
    host: str
    port: int
    host_header: str
    request_target: str
    address: str


def mindmap_node_schema(depth: int = 0, max_depth: int = 4) -> dict[str, Any]:
    child_items: dict[str, Any]
    if depth >= max_depth:
        child_items = {"type": "object", "properties": {}, "additionalProperties": False}
    else:
        child_items = mindmap_node_schema(depth + 1, max_depth)
    return {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "Node label."},
            "children": {
                "type": "array",
                "items": child_items,
                "description": "Child nodes. Use an empty array when there are no children.",
            },
        },
        "required": ["label", "children"],
        "additionalProperties": False,
    }


def available_tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "strict": True,
                "description": (
                    "Search the public web via Tavily and return ranked results. "
                    "Use this when the user's question may require fresh, external, official, or source-backed information. "
                    "In auto search mode, decide yourself whether searching is needed before answering. "
                    "You may call this multiple times across the same turn with refined queries after reading prior results. "
                    "Use concrete queries in the user's language with product names, versions, dates, or source names when relevant. "
                    "Each result includes a cite field like [^W1]. When you reference a fact from a result, insert that exact marker right after the claim. "
                    "Do NOT invent citation ids; use only the markers present in tool results. "
                    "Do NOT use for pure translation, simple arithmetic, local file search, or when the user explicitly asks not to search. "
                    "If a search returns an error, try a simpler query once, then proceed with your best answer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Concrete web search query. Be specific and do not add site: filters unless the user asked for a specific site.",
                        },
                        "intent": {
                            "type": "string",
                            "enum": ["fresh", "shopping", "technical", "official", "compare", "general"],
                            "description": "Search intent used for ranking and filtering.",
                        },
                    },
                    "required": ["query", "intent"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "python_eval",
                "strict": True,
                "description": (
                    "Evaluate a side-effect-free Python math expression. "
                    "Use this for: factorial / combinatorics / non-trivial arithmetic / "
                    "verifying numeric results when precision matters. "
                    "Do NOT use for: simple arithmetic the model can compute mentally "
                    "(e.g. 12+34), symbolic algebra, or anything requiring imports."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "Python expression, for example factorial(23) or math.sqrt(2). No imports, files, network, or mutation.",
                        }
                    },
                    "required": ["expression"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_files",
                "strict": True,
                "description": (
                    "Search locally cached uploaded files and project documents. "
                    "Use when the user asks to find, compare, cite, or check something in their notes, uploaded files, or project document library. "
                    "Do NOT use for general web facts or URLs; use fetch_url only for a specific public page."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query, for example: project deadline, invoice total, or API authentication."},
                        "limit": {"type": "integer", "description": "Maximum number of matching chunks, default 5 and maximum 10."},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "fetch_url",
                "strict": True,
                "description": (
                    "Fetch readable text from one public http(s) URL. "
                    "Use when you need the full content of a specific page that a previous search result only summarized. "
                    "Call once per URL; do not retry the same URL."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Public http or https URL to read."},
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "suggest_memory",
                "strict": True,
                "description": (
                    "Suggest a long-term memory only when the user reveals a durable preference, project fact, or todo that may help future chats. "
                    "Never include secrets, API keys, passwords, tokens, identity numbers, or one-off conversation details. The user must confirm before saving."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "Short memory statement to ask the user to save."},
                        "category": {"type": "string", "enum": ["preference", "project", "todo", "fact"]},
                        "scope": {
                            "type": "string",
                            "description": "Optional scope. Use global, project:<id>, or seek:<id>. Defaults to the current chat scope when omitted.",
                            "pattern": "^(global|project:[A-Za-z0-9_-]{1,64}|seek:[A-Za-z0-9_-]{1,64})$",
                        },
                    },
                    "required": ["content", "category"],
                    "additionalProperties": False,
                },
            },
        },
        *additional_tool_definitions(),
    ]


def additional_tool_definitions() -> list[dict[str, Any]]:
    memory_scope_schema = {
        "type": "string",
        "description": "Optional scope. Use global, project:<id>, or seek:<id>. Defaults to global plus the current chat scope.",
        "pattern": "^(global|project:[A-Za-z0-9_.:-]{1,80}|seek:[A-Za-z0-9_.:-]{1,80})$",
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "create_reminder",
                "strict": True,
                "description": "Create a local reminder when the user explicitly asks to be reminded. dueAt must be an ISO datetime.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Short reminder title."},
                        "content": {"type": "string", "description": "Reminder details."},
                        "dueAt": {"type": "string", "description": "ISO datetime, with timezone when known."},
                    },
                    "required": ["title", "content", "dueAt"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_reminders",
                "strict": True,
                "description": "List local reminders so the user can review upcoming, notified, or all reminders.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["active", "notified", "all"], "description": "Which reminders to list. Defaults to active."},
                    },
                    "required": ["status"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recall_memory",
                "strict": True,
                "description": "Search user-approved local long-term memories relevant to the current request.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Memory search query."}, "scope": memory_scope_schema},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "forget_memory",
                "strict": True,
                "description": "Delete local long-term memories only when the user explicitly asks to forget/delete them.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Exact substring to delete from memory."}, "scope": memory_scope_schema},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_project_files",
                "strict": True,
                "description": "List documents in the local project library, optionally limited to one project id.",
                "parameters": {
                    "type": "object",
                    "properties": {"projectId": {"type": "string", "description": "Optional project id."}},
                    "required": ["projectId"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file_chunk",
                "strict": True,
                "description": "Read one cached uploaded-file or project-document chunk by fileId and 1-based chunkIndex.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "fileId": {"type": "string", "description": "32-character cached file id."},
                        "chunkIndex": {"type": "integer", "description": "1-based chunk index."},
                        "projectId": {"type": "string", "description": "Optional project id for project documents."},
                    },
                    "required": ["fileId", "chunkIndex", "projectId"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "data_transform",
                "strict": True,
                "description": "Run safe, whitelisted text/data transformations. No code execution, imports, files, or network.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["extract_regex", "json_path", "csv_summary", "number_summary"]},
                        "input": {"type": "string", "description": "Input text, JSON, CSV, or numbers."},
                        "pattern": {"type": "string", "description": "Regex for extract_regex."},
                        "path": {"type": "string", "description": "Simple JSON path like $.items[0].name."},
                        "delimiter": {"type": "string", "description": "CSV delimiter, default comma."},
                    },
                    "required": ["operation", "input"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "generate_chart",
                "strict": True,
                "description": "Validate chart data and return a markdownTable. Put markdownTable in the final answer so the UI can render chart buttons.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["bar", "line", "pie"]},
                        "title": {"type": "string", "description": "Chart title."},
                        "data": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"label": {"type": "string"}, "value": {"type": "number"}},
                                "required": ["label", "value"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["type", "title", "data"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_mindmap",
                "strict": True,
                "description": (
                    "Generate a downloadable SVG diagram (a clustered top-down flowchart) from a structured node tree. "
                    "Use when the user asks to draw, create, make, organize, or export a mind map, concept map, "
                    "comparison diagram, or grouped flowchart. "
                    "STRUCTURE controls the look: each TOP-LEVEL node becomes a titled, colored group container "
                    "(its label is the container title); that node's nested children are laid out top-to-bottom inside the "
                    "container and connected by downward arrows (parent → child). So make each major group / option / "
                    "category / phase a top-level node, and put its steps, details, or sub-points as nested children to show flow. "
                    "Aim for 2-6 groups; nest children as deep as the content needs. "
                    "Do not answer with only Mermaid or plain Markdown when the user asks for a real diagram file. "
                    "The result contains downloadUrl; include it as a Markdown image (![](downloadUrl)) or link in the final answer."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Diagram title, rendered as a centered header above the groups."},
                        "subtitle": {"type": "string", "description": "Optional short subtitle; pass an empty string when absent."},
                        "nodes": {
                            "type": "array",
                            "description": "Top-level groups; each becomes a titled container, and its nested children flow top-down with arrows inside it.",
                            "items": mindmap_node_schema(),
                        },
                    },
                    "required": ["title", "subtitle", "nodes"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "compare_search_results",
                "strict": True,
                "description": "Run up to two related web_search queries, deduplicate URLs, and return citation-ready comparison results.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "queries": {"type": "array", "items": {"type": "string"}, "description": "One or two concrete web search queries."},
                        "intent": {"type": "string", "enum": ["fresh", "shopping", "technical", "official", "compare", "general"]},
                    },
                    "required": ["queries", "intent"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_pptx",
                "strict": True,
                "description": (
                    f"这是 DeepSeek Infra 的 `{SLIDES_SKILL_NAME}` skill 本地执行入口：{SLIDES_SKILL_DESCRIPTION} "
                    "生成一个可下载的 PowerPoint (.pptx) 演示文稿。只要用户要求做 PPT / 幻灯片 / 演示文稿，"
                    "就必须调用本工具生成真实文件，绝不要用 Marp / Markdown 幻灯片大纲文本来代替。"
                    "传入标题和分页大纲；按 `slides` skill 组织页面标题、要点和视觉辅助内容。"
                    "优先生成 6-10 页、每页 3-6 个短要点，并为关键页面选择 layout（cards/process/comparison/summary 等），"
                    "避免只做单调 bullet 列表。"
                    "返回的 result 含 downloadUrl，你必须在最终回复里用 Markdown 链接"
                    "（例如 [下载 PPT](downloadUrl)）把它交给用户，并简述每页内容。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "演示文稿主标题（用于封面页）。"},
                        "subtitle": {"type": "string", "description": "封面副标题；没有就传空字符串。"},
                        "slides": {
                            "type": "array",
                            "description": "内容页大纲，按顺序排列。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string", "description": "本页标题。"},
                                    "bullets": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "本页要点列表，每个元素一条要点。",
                                    },
                                    "layout": {
                                        "type": "string",
                                        "enum": ["auto", "cards", "process", "timeline", "comparison", "quote", "summary", "bullets"],
                                        "description": "可选视觉版式。优先用 cards/process/comparison/summary 等丰富页面；不确定传 auto。",
                                    },
                                },
                                "required": ["title", "bullets", "layout"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["title", "subtitle", "slides"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "create_document",
                "strict": True,
                "description": (
                    "生成一个可下载的精排文档：Word (.docx) 或 PDF。"
                    "当用户要求写 / 做 / 生成 Word、Word 文档、.docx、PDF、PDF 文档、报告、说明书、方案、"
                    "信函、简历、论文、合同、手册等成文文件时，必须调用本工具生成真实文件，"
                    "不要只在聊天里贴正文或用 Markdown 代替。"
                    "用 format 选择格式：用户说 Word 用 docx，说 PDF 用 pdf；没指定时正式文档/报告优先 docx。"
                    "把内容组织成有层级的章节：每个 section 一个 heading，配 body 正文段落、bullets 要点列表，"
                    "以及可选的 table 表格（适合放对比、指标、清单等结构化数据，能显著提升美观度）。"
                    "优先写成结构清晰、段落充实的正文，而不是只有零散要点。"
                    "返回的 result 含 downloadUrl，你必须在最终回复里用 Markdown 链接"
                    "（例如 [下载文档](downloadUrl)）把它交给用户，并简述文档的标题与章节结构。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "format": {
                            "type": "string",
                            "enum": ["docx", "pdf"],
                            "description": "文档格式：docx = Word 文档，pdf = PDF 文档。",
                        },
                        "title": {"type": "string", "description": "文档主标题（用于标题块和文件名）。"},
                        "subtitle": {"type": "string", "description": "副标题 / 署名 / 日期等；没有就传空字符串。"},
                        "sections": {
                            "type": "array",
                            "description": "正文章节，按顺序排列。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "heading": {"type": "string", "description": "本章节标题。"},
                                    "body": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "正文段落，每个元素一段；不需要就传空数组。",
                                    },
                                    "bullets": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "要点列表，每个元素一条；不需要就传空数组。",
                                    },
                                    "table": {
                                        "type": "object",
                                        "description": "可选表格；不需要时 headers 和 rows 都传空数组。",
                                        "properties": {
                                            "headers": {
                                                "type": "array",
                                                "items": {"type": "string"},
                                                "description": "表头单元格文本；没有表格就传空数组。",
                                            },
                                            "rows": {
                                                "type": "array",
                                                "items": {"type": "array", "items": {"type": "string"}},
                                                "description": "数据行，每行是一组单元格文本；没有表格就传空数组。",
                                            },
                                        },
                                        "required": ["headers", "rows"],
                                        "additionalProperties": False,
                                    },
                                },
                                "required": ["heading", "body", "bullets", "table"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["format", "title", "subtitle", "sections"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def execute_tool_call(
    tool_call: dict[str, Any],
    *,
    memory_suggestion_callback: Callable[[dict[str, Any]], None] | None = None,
    default_memory_scope: str = "global",
    web_search_callback: Callable[[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    raw_function = tool_call.get("function")
    function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else {}
    name = str(function.get("name") or tool_call.get("name") or "").strip()
    arguments = parse_tool_arguments(function.get("arguments"))
    try:
        if name == "python_eval":
            result = python_eval(str(arguments.get("expression") or ""))
        elif name == "search_files":
            result = search_files(str(arguments.get("query") or ""), limit=safe_limit(arguments.get("limit"), default=5, maximum=10))
        elif name == "fetch_url":
            result = fetch_url(str(arguments.get("url") or ""))
        elif name == "web_search":
            if web_search_callback is None:
                raise AppError("web_search is not enabled for this request", code=ErrorCode.INVALID_PAYLOAD)
            result = web_search_callback(
                str(arguments.get("query") or ""),
                str(arguments.get("intent") or "general"),
            )
        elif name == "compare_search_results":
            if web_search_callback is None:
                raise AppError("compare_search_results is not enabled for this request", code=ErrorCode.INVALID_PAYLOAD)
            result = compare_search_results(arguments.get("queries"), str(arguments.get("intent") or "general"), web_search_callback)
        elif name == "suggest_memory":
            scope = normalize_memory_scope(arguments.get("scope") or default_memory_scope)
            result = build_memory_suggestion(str(arguments.get("content") or ""), category=str(arguments.get("category") or ""), scope=scope)
            if memory_suggestion_callback:
                memory_suggestion_callback(result)
        elif name == "create_reminder":
            result = create_reminder_tool(str(arguments.get("title") or ""), str(arguments.get("content") or ""), str(arguments.get("dueAt") or ""))
        elif name == "list_reminders":
            result = list_reminders_tool(str(arguments.get("status") or "active"))
        elif name == "recall_memory":
            result = recall_memory_tool(str(arguments.get("query") or ""), scope=str(arguments.get("scope") or ""), default_scope=default_memory_scope)
        elif name == "forget_memory":
            result = forget_memory_tool(str(arguments.get("query") or ""), scope=str(arguments.get("scope") or ""), default_scope=default_memory_scope)
        elif name == "list_project_files":
            result = list_project_files_tool(str(arguments.get("projectId") or ""))
        elif name == "read_file_chunk":
            result = read_file_chunk_tool(
                str(arguments.get("fileId") or ""),
                chunk_index=safe_limit(arguments.get("chunkIndex"), default=1, maximum=1_000_000),
                project_id=str(arguments.get("projectId") or ""),
            )
        elif name == "data_transform":
            result = data_transform(
                str(arguments.get("operation") or ""),
                str(arguments.get("input") or ""),
                pattern=str(arguments.get("pattern") or ""),
                path=str(arguments.get("path") or ""),
                delimiter=str(arguments.get("delimiter") or ","),
            )
        elif name == "generate_chart":
            result = generate_chart(str(arguments.get("type") or "bar"), str(arguments.get("title") or ""), arguments.get("data"))
        elif name == "create_mindmap":
            result = create_mindmap(
                str(arguments.get("title") or ""),
                arguments.get("nodes"),
                subtitle=str(arguments.get("subtitle") or ""),
            )
        elif name == "create_pptx":
            result = create_presentation(
                str(arguments.get("title") or ""),
                arguments.get("slides"),
                subtitle=str(arguments.get("subtitle") or ""),
            )
        elif name == "create_document":
            result = create_document(
                str(arguments.get("format") or "docx"),
                str(arguments.get("title") or ""),
                arguments.get("sections"),
                subtitle=str(arguments.get("subtitle") or ""),
            )
        else:
            raise AppError(f"Unsupported tool: {name}", code=ErrorCode.INVALID_PAYLOAD)
        return {"ok": True, "tool": name, "result": result}
    except AppError as exc:
        return {"ok": False, "tool": name or "unknown", "error": str(exc), "code": exc.code.value}
    except Exception as exc:  # pragma: no cover - defensive boundary
        return {"ok": False, "tool": name or "unknown", "error": str(exc), "code": ErrorCode.INTERNAL.value}


def execute_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    memory_suggestion_callback: Callable[[dict[str, Any]], None] | None = None,
    default_memory_scope: str = "global",
    web_search_callback: Callable[[str, str], dict[str, Any]] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    selected = tool_calls[:MAX_TOOL_CALLS_PER_RESPONSE]
    outputs: list[dict[str, Any] | None] = [None] * len(selected)
    parallel_batch: list[tuple[int, dict[str, Any]]] = []

    def is_cancelled() -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    def cancelled_output(call: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "tool": tool_call_name(call) or "unknown",
            "error": "Request cancelled before tool execution completed",
            "code": ErrorCode.INTERNAL.value,
        }

    def run_call(call: dict[str, Any]) -> dict[str, Any]:
        if is_cancelled():
            return cancelled_output(call)
        return execute_tool_call(
            call,
            memory_suggestion_callback=memory_suggestion_callback,
            default_memory_scope=default_memory_scope,
            web_search_callback=web_search_callback,
        )

    def flush_parallel_batch() -> None:
        nonlocal parallel_batch
        if not parallel_batch:
            return
        if len(parallel_batch) == 1:
            index, call = parallel_batch[0]
            outputs[index] = run_call(call)
        else:
            pool = ThreadPoolExecutor(max_workers=min(len(parallel_batch), MAX_TOOL_CALLS_PER_RESPONSE))
            try:
                futures = {pool.submit(run_call, call): index for index, call in parallel_batch}
                for future in as_completed(futures):
                    if is_cancelled():
                        for pending in futures:
                            pending.cancel()
                        break
                    outputs[futures[future]] = future.result()
            finally:
                pool.shutdown(wait=not is_cancelled(), cancel_futures=True)
        parallel_batch = []

    for index, tool_call in enumerate(selected):
        if is_cancelled():
            outputs[index] = cancelled_output(tool_call)
            continue
        if is_parallel_safe_tool(tool_call):
            parallel_batch.append((index, tool_call))
            continue
        flush_parallel_batch()
        outputs[index] = run_call(tool_call)
    flush_parallel_batch()

    # v1.2.8：并行 batch 启动后中途 cancel，被 cancel_futures 中断的 slot 会留 None。
    # 这里在最终组装前再做一次取消判定，把"运行到一半被打断"的 slot 统一成 cancelled
    # 错误，而不是退化到通用 "Tool did not run"——cancel 语义在前后端各层保持一致。
    results = []
    for tool_call, output in zip(selected, outputs):
        if output is None and is_cancelled():
            output = cancelled_output(tool_call)
        output = output or {
            "ok": False,
            "tool": tool_call_name(tool_call) or "unknown",
            "error": "Tool did not run",
            "code": ErrorCode.INTERNAL.value,
        }
        results.append(tool_result_message(tool_call, output))
    return results


def tool_result_message(tool_call: dict[str, Any], output: dict[str, Any]) -> dict[str, str]:
    return {
        "role": "tool",
        "tool_call_id": str(tool_call.get("id") or ""),
        "name": str(output.get("tool") or ""),
        "content": json.dumps(stable_tool_output_for_model(output), ensure_ascii=False, sort_keys=True, separators=(",", ":"))[
            :MAX_TOOL_RESULT_CHARS
        ],
    }


def stable_tool_output_for_model(output: dict[str, Any]) -> dict[str, Any]:
    tool_name = str(output.get("tool") or "")
    if tool_name in {"web_search", "compare_search_results"}:
        return strip_volatile_tool_fields(output)
    if tool_name in {"create_pptx", "create_document", "create_mindmap"}:
        return compact_artifact_tool_output(output)
    return output


def compact_artifact_tool_output(output: dict[str, Any]) -> dict[str, Any]:
    if output.get("ok") is not True:
        return output
    result = output.get("result")
    if not isinstance(result, dict):
        return output
    tool_name = str(output.get("tool") or "")
    compact: dict[str, Any] = {}
    for key in ("fileId", "filename", "downloadUrl", "title"):
        if result.get(key) not in (None, ""):
            compact[key] = result[key]
    if tool_name == "create_pptx":
        if result.get("slideCount") not in (None, ""):
            compact["slideCount"] = result["slideCount"]
        outline = result.get("outline")
        if isinstance(outline, list):
            compact["outline"] = [
                {
                    key: item[key]
                    for key in ("page", "title", "layout")
                    if isinstance(item, dict) and item.get(key) not in (None, "")
                }
                for item in outline[:20]
                if isinstance(item, dict)
            ]
    elif tool_name == "create_document":
        for key in ("format", "sectionCount"):
            if result.get(key) not in (None, ""):
                compact[key] = result[key]
        outline = result.get("outline")
        if isinstance(outline, list):
            compact["outline"] = [
                {
                    key: item[key]
                    for key in ("index", "heading", "hasTable")
                    if isinstance(item, dict) and item.get(key) not in (None, "")
                }
                for item in outline[:40]
                if isinstance(item, dict)
            ]
    elif tool_name == "create_mindmap":
        for key in ("format", "nodeCount"):
            if result.get(key) not in (None, ""):
                compact[key] = result[key]
        outline = result.get("outline")
        if isinstance(outline, list):
            compact["outline"] = _compact_mindmap_outline(outline)
    return {"ok": True, "tool": tool_name, "result": compact}


def _compact_mindmap_outline(nodes: list[Any], *, depth: int = 0) -> list[dict[str, Any]]:
    if depth >= 4:
        return []
    result: list[dict[str, Any]] = []
    for item in nodes[:30]:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {"label": str(item.get("label") or "")[:80]}
        children = item.get("children")
        if isinstance(children, list) and children:
            entry["children"] = _compact_mindmap_outline(children, depth=depth + 1)
        result.append(entry)
    return result


def strip_volatile_tool_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: strip_volatile_tool_fields(item) for key, item in value.items() if key not in {"cached"}}
    if isinstance(value, list):
        return [strip_volatile_tool_fields(item) for item in value]
    return value


def tool_call_name(tool_call: dict[str, Any]) -> str:
    raw_function = tool_call.get("function")
    function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else {}
    return str(function.get("name") or tool_call.get("name") or "").strip()


def is_parallel_safe_tool(tool_call: dict[str, Any]) -> bool:
    name = tool_call_name(tool_call)
    return bool(name) and name not in SERIAL_TOOL_NAMES


def parse_tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def safe_limit(value: Any, *, default: int, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def python_eval(expression: str) -> dict[str, Any]:
    expression = str(expression or "").strip()
    if not expression:
        raise AppError("python_eval expression is empty", code=ErrorCode.INVALID_PAYLOAD)
    if len(expression) > 1000:
        raise AppError("python_eval expression is too long", code=ErrorCode.INVALID_PAYLOAD)
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", PYTHON_EVAL_RUNNER],
            input=json.dumps({"expression": expression}, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=PYTHON_EVAL_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise AppError("python_eval timed out", code=ErrorCode.UPSTREAM_TIMEOUT, status=408) from exc
    output = completed.stdout.strip()
    if not output:
        raise AppError("python_eval produced no output", code=ErrorCode.INTERNAL, status=500)
    data = json.loads(output)
    if not data.get("ok"):
        raise AppError(str(data.get("error") or "python_eval failed"), code=ErrorCode.INVALID_PAYLOAD)
    return {"expression": expression, "result": str(data.get("result") or "")}


def create_reminder_tool(title: str, content: str, due_at: str) -> dict[str, Any]:
    return create_local_reminder({"title": title or "提醒", "content": content, "dueAt": due_at})


def list_reminders_tool(status: str = "active") -> dict[str, Any]:
    normalized = str(status or "active").strip().lower()
    if normalized not in {"active", "notified", "all"}:
        normalized = "active"
    reminders = load_reminders()
    if normalized == "active":
        reminders = [item for item in reminders if not bool(item.get("notified"))]
    elif normalized == "notified":
        reminders = [item for item in reminders if bool(item.get("notified"))]
    return {"status": normalized, "reminders": reminders[:50], "count": len(reminders)}


def memory_tool_scopes(scope: str, default_scope: str) -> list[str]:
    requested = normalize_memory_scope(scope or "")
    if scope and requested != "global":
        return [requested]
    if scope and requested == "global":
        return ["global"]
    current = normalize_memory_scope(default_scope or "global")
    return ["global", current] if current != "global" else ["global"]


def recall_memory_tool(query: str, *, scope: str = "", default_scope: str = "global") -> dict[str, Any]:
    cleaned = str(query or "").strip() or "memory"
    scopes = memory_tool_scopes(scope, default_scope)
    memories = retrieve_memories(cleaned, scopes=scopes)
    return {
        "query": cleaned,
        "scopes": scopes,
        "memories": [
            {
                "id": str(item.get("id") or ""),
                "content": str(item.get("content") or ""),
                "category": str(item.get("category") or "fact"),
                "scope": normalize_memory_scope(item.get("scope") or "global"),
                "updatedAt": str(item.get("updatedAt") or item.get("createdAt") or ""),
            }
            for item in memories
        ],
    }


def forget_memory_tool(query: str, *, scope: str = "", default_scope: str = "global") -> dict[str, Any]:
    cleaned = str(query or "").strip()
    if not cleaned:
        raise AppError("forget_memory query is required", code=ErrorCode.INVALID_PAYLOAD)
    scopes = memory_tool_scopes(scope, default_scope)
    deleted = delete_memories_by_query(cleaned, scopes=scopes)
    return {"query": cleaned, "scopes": scopes, "deleted": deleted}


def list_project_files_tool(project_id: str = "") -> dict[str, Any]:
    safe_project_id = str(project_id or "").strip()
    if safe_project_id:
        project = read_project(safe_project_id)
        if project is None:
            raise AppError("Project not found", code=ErrorCode.NOT_FOUND, status=404)
        projects = [project]
    else:
        projects = list_projects()
    payload = []
    for project in projects[:40]:
        documents = []
        for document in (project.get("documents") or [])[:120]:
            if not isinstance(document, dict):
                continue
            documents.append(project_document_for_tool(document))
        payload.append({"id": str(project.get("id") or ""), "name": str(project.get("name") or ""), "files": documents})
    return {"projects": payload, "count": sum(len(project["files"]) for project in payload)}


def project_document_for_tool(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(document.get("name") or ""),
        "fileId": str(document.get("fileId") or ""),
        "projectId": str(document.get("projectId") or ""),
        "kind": str(document.get("kind") or "text"),
        "pageCount": int(document.get("pageCount") or 0),
        "charCount": int(document.get("charCount") or 0),
        "chunkCount": int(document.get("chunkCount") or 0),
        "preview": str(document.get("preview") or "")[:500],
    }


def read_file_chunk_tool(file_id: str, *, chunk_index: int, project_id: str = "") -> dict[str, Any]:
    cached = load_cached_file(str(file_id or ""), project_id=str(project_id or "").strip() or None)
    raw_chunks = cached.get("chunks")
    chunks = raw_chunks if isinstance(raw_chunks, list) else []
    index = max(1, int(chunk_index or 1)) - 1
    if index >= len(chunks):
        raise AppError("Chunk not found", code=ErrorCode.NOT_FOUND, status=404)
    chunk = chunks[index]
    if not isinstance(chunk, dict):
        raise AppError("Chunk not found", code=ErrorCode.NOT_FOUND, status=404)
    return {
        "file": {
            "name": str(cached.get("name") or ""),
            "kind": str(cached.get("kind") or "text"),
            "fileId": str(cached.get("id") or file_id),
            "projectId": str(cached.get("projectId") or project_id or ""),
            "chunkCount": len(chunks),
        },
        "chunk": {
            "index": index + 1,
            "lineStart": int(chunk.get("lineStart") or 0),
            "lineEnd": int(chunk.get("lineEnd") or 0),
            "text": str(chunk.get("text") or "")[:6000],
        },
    }


def data_transform(operation: str, input_text: str, *, pattern: str = "", path: str = "", delimiter: str = ",") -> dict[str, Any]:
    op = str(operation or "").strip()
    text = str(input_text or "")[:50_000]
    if op == "extract_regex":
        return transform_extract_regex(text, pattern)
    if op == "json_path":
        return transform_json_path(text, path or "$")
    if op == "csv_summary":
        return transform_csv_summary(text, delimiter)
    if op == "number_summary":
        return transform_number_summary(text)
    raise AppError("Unsupported data_transform operation", code=ErrorCode.INVALID_PAYLOAD)


def transform_extract_regex(text: str, pattern: str) -> dict[str, Any]:
    if not pattern:
        raise AppError("Regex pattern is required", code=ErrorCode.INVALID_PAYLOAD)
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise AppError(f"Invalid regex: {exc}", code=ErrorCode.INVALID_PAYLOAD) from exc
    matches = []
    for match in compiled.finditer(text):
        matches.append({"match": match.group(0), "groups": list(match.groups()), "start": match.start(), "end": match.end()})
        if len(matches) >= 100:
            break
    return {"operation": "extract_regex", "count": len(matches), "matches": matches}


def transform_json_path(text: str, path: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AppError(f"Invalid JSON: {exc}", code=ErrorCode.INVALID_PAYLOAD) from exc
    selected = read_simple_json_path(value, path)
    return {"operation": "json_path", "path": path, "value": compact_json_value(selected)}


def read_simple_json_path(value: Any, path: str) -> Any:
    current = value
    expression = str(path or "$").strip()
    if expression in {"", "$"}:
        return current
    if expression.startswith("$."):
        expression = expression[2:]
    elif expression.startswith("$"):
        expression = expression[1:]
    parts = [part for part in re.split(r"\.(?![^\[]*\])", expression) if part]
    for part in parts:
        match = re.fullmatch(r"([A-Za-z0-9_-]+)(?:\[(\d+)])?", part)
        if not match:
            raise AppError("Unsupported JSON path", code=ErrorCode.INVALID_PAYLOAD)
        key, index_text = match.groups()
        if not isinstance(current, dict) or key not in current:
            raise AppError("JSON path not found", code=ErrorCode.NOT_FOUND, status=404)
        current = current[key]
        if index_text is not None:
            index = int(index_text)
            if not isinstance(current, list) or index >= len(current):
                raise AppError("JSON path not found", code=ErrorCode.NOT_FOUND, status=404)
            current = current[index]
    return current


def compact_json_value(value: Any) -> Any:
    encoded = json.dumps(value, ensure_ascii=False)
    if len(encoded) <= 4000:
        return value
    return encoded[:4000]


def transform_csv_summary(text: str, delimiter: str = ",") -> dict[str, Any]:
    dialect_delimiter = (delimiter or ",")[0]
    rows = list(csv.reader(io.StringIO(text), delimiter=dialect_delimiter))[:501]
    if not rows:
        return {"operation": "csv_summary", "rows": 0, "columns": [], "numericColumns": []}
    headers = [str(cell or f"col{index + 1}")[:80] for index, cell in enumerate(rows[0])]
    numeric_columns = []
    for column_index, header in enumerate(headers):
        values = []
        for row in rows[1:]:
            if column_index >= len(row):
                continue
            try:
                values.append(float(str(row[column_index]).replace(",", "").strip()))
            except ValueError:
                continue
        if values:
            numeric_columns.append(number_summary_payload(header, values))
    return {"operation": "csv_summary", "rows": max(0, len(rows) - 1), "columns": headers, "numericColumns": numeric_columns}


def transform_number_summary(text: str) -> dict[str, Any]:
    values = [float(item) for item in re.findall(r"[-+]?(?:\d+\.\d+|\d+|\.\d+)", text)]
    return {"operation": "number_summary", **number_summary_payload("numbers", values)}


def number_summary_payload(label: str, values: list[float]) -> dict[str, Any]:
    if not values:
        return {"label": label, "count": 0}
    return {
        "label": label,
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "sum": sum(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
    }


def generate_chart(chart_type: str, title: str, data: Any) -> dict[str, Any]:
    normalized_type = chart_type if chart_type in {"bar", "line", "pie"} else "bar"
    if not isinstance(data, list) or not data:
        raise AppError("Chart data must be a non-empty list", code=ErrorCode.INVALID_PAYLOAD)
    points = []
    for item in data[:12]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:80]
        raw_value = item.get("value")
        if raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if label:
            points.append({"label": label, "value": value})
    if not points:
        raise AppError("Chart data has no valid points", code=ErrorCode.INVALID_PAYLOAD)
    return {
        "type": normalized_type,
        "title": str(title or "Chart")[:120],
        "data": points,
        "markdownTable": chart_markdown_table(points),
    }


def chart_markdown_table(points: list[dict[str, Any]]) -> str:
    lines = ["| label | value |", "|---|---:|"]
    for point in points:
        label = str(point["label"]).replace("|", "\\|")
        lines.append(f"| {label} | {point['value']} |")
    return "\n".join(lines)


def compare_search_results(queries: Any, intent: str, web_search_callback: Callable[[str, str], dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(queries, list):
        raise AppError("queries must be a list", code=ErrorCode.INVALID_PAYLOAD)
    cleaned_queries = []
    for query in queries:
        cleaned = re.sub(r"\s+", " ", str(query or "")).strip()
        if cleaned and cleaned not in cleaned_queries:
            cleaned_queries.append(cleaned[:500])
        if len(cleaned_queries) >= 2:
            break
    if not cleaned_queries:
        raise AppError("At least one query is required", code=ErrorCode.INVALID_PAYLOAD)

    rounds = []
    results = []
    seen_urls: set[str] = set()
    for query in cleaned_queries:
        round_result = web_search_callback(query, intent or "general")
        rounds.append(round_result)
        for item in round_result.get("results") or []:
            if not isinstance(item, dict):
                continue
            key = search_result_key(str(item.get("url") or ""))
            if not key or key in seen_urls:
                continue
            seen_urls.add(key)
            results.append(item)
    return {"queries": cleaned_queries, "intent": intent or "general", "rounds": rounds, "results": results[:20]}


def search_result_key(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url.strip().lower()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def search_files(query: str, *, limit: int = 5) -> dict[str, Any]:
    query = str(query or "").strip()
    if not query:
        raise AppError("search_files query is empty", code=ErrorCode.INVALID_PAYLOAD)
    tokens = query_tokens(query)
    query_vector = local_text_vector(query)
    paths = iter_cached_file_paths()
    for path, project_id in paths:
        cached = read_cached_file(path)
        if not cached:
            continue
        local_rag.index_file_payload(cached, project_id=project_id)

    matches_by_key: dict[tuple[str, str, int], dict[str, Any]] = {}

    for result in local_rag.search_files_index(query, limit=max(limit * 4, limit)):
        chunk_index = int(result.chunk_index) + 1
        key = (result.source_id, result.project_id, chunk_index)
        matches_by_key[key] = {
            "score": result.score,
            "fileId": result.source_id,
            "projectId": result.project_id,
            "name": result.name,
            "kind": result.kind,
            "chunkIndex": chunk_index,
            "lineStart": int(result.metadata.get("lineStart") or 0),
            "lineEnd": int(result.metadata.get("lineEnd") or 0),
            "snippet": compact_snippet(result.text, query),
            "lineage": local_rag.chunk_lineage(result),
            "retrieval": {
                "source": "local_rag",
                "vectorScore": round(result.vector_score, 4),
                "keywordScore": result.keyword_score,
            },
        }

    for path, project_id in paths:
        cached = read_cached_file(path)
        if not cached:
            continue
        chunks = cached.get("chunks")
        if not isinstance(chunks, list):
            continue
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            text = str(chunk.get("text") or "")
            keyword_score = score_chunk(text, tokens)
            vector = chunk.get("vector")
            vector_score = cosine_similarity(query_vector, vector if isinstance(vector, list) else local_text_vector(text))
            score = keyword_score * 10 + int(vector_score * 100)
            if score <= 0:
                continue
            file_id = str(cached.get("id") or path.stem)
            chunk_index = int(chunk.get("index") or 0) + 1
            key = (file_id, project_id, chunk_index)
            existing = matches_by_key.get(key)
            if existing and int(existing.get("score") or 0) >= score:
                continue
            matches_by_key[key] = {
                "score": score,
                "fileId": file_id,
                "projectId": project_id,
                "name": str(cached.get("name") or path.name),
                "kind": str(cached.get("kind") or "text"),
                "chunkIndex": chunk_index,
                "lineStart": int(chunk.get("lineStart") or 0),
                "lineEnd": int(chunk.get("lineEnd") or 0),
                "snippet": compact_snippet(text, query),
                "retrieval": {"source": "json_hybrid", "vectorScore": round(vector_score, 4), "keywordScore": keyword_score},
            }
    matches = list(matches_by_key.values())
    matches.sort(key=lambda item: (-int(item["score"]), item["name"], int(item["chunkIndex"])))
    return {"query": query, "matches": matches[:limit], "searchedFiles": len({item["fileId"] for item in matches})}


def iter_cached_file_paths() -> list[tuple[Path, str]]:
    paths: list[tuple[Path, str]] = []
    if FILE_CACHE_DIR.exists():
        paths.extend((path, "") for path in FILE_CACHE_DIR.glob("*.json"))
    if PROJECTS_DIR.exists():
        for path in PROJECTS_DIR.glob("*/files/*.json"):
            project_id = path.parent.parent.name
            paths.append((path, project_id))
    return paths


def read_cached_file(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def compact_snippet(text: str, query: str, *, limit: int = 700) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(value) <= limit:
        return value
    tokens = query_tokens(query)
    lowered = value.lower()
    positions = [lowered.find(token.lower()) for token in tokens if token and lowered.find(token.lower()) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - limit // 3)
    end = min(len(value), start + limit)
    return value[start:end].strip()


def fetch_url(url: str) -> dict[str, Any]:
    target = resolve_public_url(url)
    safe_url = target.url
    cached = load_fetch_url_cache(safe_url)
    if cached:
        return {**cached, "cached": True}
    raw, content_type, final_url = fetch_public_url(target)
    if len(raw) > MAX_FETCH_BYTES:
        raise AppError("Fetched page is too large", code=ErrorCode.UPLOAD_TOO_LARGE, status=413)
    text = extract_readable_text(raw, content_type)
    result = {"url": final_url, "contentType": content_type, "text": text[:20_000], "charCount": len(text)}
    save_fetch_url_cache(safe_url, result)
    return result


def validate_public_url(url: str) -> str:
    return resolve_public_url(url).url


def resolve_public_url(url: str) -> PublicUrlTarget:
    raw = str(url or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise AppError("Invalid URL", code=ErrorCode.INVALID_PAYLOAD) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AppError("Only public http(s) URLs are supported", code=ErrorCode.INVALID_PAYLOAD)
    if parsed.username or parsed.password:
        raise AppError("URL credentials are not allowed", code=ErrorCode.INVALID_PAYLOAD)
    host = normalize_url_host(parsed.hostname or "")
    if not host or host == "localhost" or host.endswith(".local"):
        raise AppError("Local URLs are not allowed", code=ErrorCode.FORBIDDEN, status=403)
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise AppError("Invalid URL port", code=ErrorCode.INVALID_PAYLOAD) from exc
    port = parsed_port or (443 if parsed.scheme == "https" else 80)
    host_header = format_host_header(host, parsed_port)
    addresses = resolve_public_host(host, port)
    request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    safe_url = urlunsplit((parsed.scheme, host_header, parsed.path or "/", parsed.query, ""))
    return PublicUrlTarget(
        url=safe_url,
        scheme=parsed.scheme,
        host=host,
        port=port,
        host_header=host_header,
        request_target=request_target,
        address=addresses[0],
    )


def ensure_public_host(host: str) -> None:
    resolve_public_host(normalize_url_host(host), None)


def normalize_url_host(host: str) -> str:
    value = str(host or "").strip().rstrip(".").lower()
    if not value or "%" in value:
        return value
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise AppError("Invalid URL host", code=ErrorCode.INVALID_PAYLOAD) from exc


def format_host_header(host: str, port: int | None) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}" if port is not None else host


def resolve_public_host(host: str, port: int | None) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise AppError("URL host could not be resolved", code=ErrorCode.UPSTREAM_FAILURE, status=502) from exc
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        ensure_public_address(address)
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise AppError("URL host could not be resolved", code=ErrorCode.UPSTREAM_FAILURE, status=502)
    return addresses


def ensure_public_address(address: str) -> None:
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError as exc:
        raise AppError("URL host resolved to an invalid address", code=ErrorCode.UPSTREAM_FAILURE, status=502) from exc
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise AppError("Private or local URL targets are not allowed", code=ErrorCode.FORBIDDEN, status=403)


class LockedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, target: PublicUrlTarget, timeout: float) -> None:
        super().__init__(target.host, target.port, timeout=timeout)
        self.resolved_address = target.address

    def connect(self) -> None:
        # `source_address` / `_tunnel_host` / `_tunnel` 是 CPython HTTPConnection 的内部成员，
        # typeshed 未对外暴露；用 Any 别名访问以保留原始连接逻辑（含代理隧道兜底）。
        internals: Any = self
        self.sock = socket.create_connection((self.resolved_address, self.port), self.timeout, internals.source_address)
        if internals._tunnel_host:
            internals._tunnel()


class LockedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, target: PublicUrlTarget, timeout: float) -> None:
        super().__init__(target.host, target.port, timeout=timeout, context=ssl.create_default_context())
        self.resolved_address = target.address

    def connect(self) -> None:
        internals: Any = self
        self.sock = socket.create_connection((self.resolved_address, self.port), self.timeout, internals.source_address)
        if internals._tunnel_host:
            internals._tunnel()
        self.sock = internals._context.wrap_socket(self.sock, server_hostname=self.host)


def public_http_connection(target: PublicUrlTarget, timeout: float) -> http.client.HTTPConnection:
    if target.scheme == "https":
        return LockedHTTPSConnection(target, timeout)
    return LockedHTTPConnection(target, timeout)


def fetch_public_url(target: PublicUrlTarget) -> tuple[bytes, str, str]:
    current = target
    for redirect_count in range(MAX_FETCH_REDIRECTS + 1):
        connection = public_http_connection(current, TAVILY_TIMEOUT_SECONDS)
        try:
            connection.request("GET", current.request_target, headers=fetch_url_headers(current))
            response = connection.getresponse()
            location = response.getheader("Location", "")
            if response.status in FETCH_URL_REDIRECT_STATUSES and location:
                if redirect_count >= MAX_FETCH_REDIRECTS:
                    raise AppError("Too many URL redirects", code=ErrorCode.UPSTREAM_FAILURE, status=502)
                current = resolve_public_url(urljoin(current.url, location))
                continue
            if response.status >= 400:
                raise AppError(f"URL fetch failed: HTTP {response.status}", code=ErrorCode.UPSTREAM_FAILURE, status=min(response.status, 502))
            return response.read(MAX_FETCH_BYTES + 1), response.getheader("Content-Type", ""), current.url
        except AppError:
            raise
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            reason = str(exc) or exc.__class__.__name__
            code = ErrorCode.UPSTREAM_TIMEOUT if "timed out" in reason.lower() else ErrorCode.UPSTREAM_FAILURE
            raise AppError(f"Cannot fetch URL: {reason}", code=code, status=502) from exc
        finally:
            connection.close()
    raise AppError("Too many URL redirects", code=ErrorCode.UPSTREAM_FAILURE, status=502)


def fetch_url_headers(target: PublicUrlTarget) -> dict[str, str]:
    return {
        "User-Agent": "DeepSeekMobile/0.7 local fetch-url",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        "Host": target.host_header,
    }


def extract_readable_text(raw: bytes, content_type: str) -> str:
    try:
        import trafilatura as trafilatura_module
    except ModuleNotFoundError:
        trafilatura_module = None
    if trafilatura_module is not None:
        extracted = trafilatura_module.extract(raw.decode("utf-8", errors="replace"), include_comments=False, include_tables=True)
        if extracted:
            return normalize_text(extracted)
    if "html" in content_type.lower() or b"<html" in raw[:1000].lower():
        return normalize_text(extract_html_text(raw))
    return normalize_text(raw.decode("utf-8", errors="replace"))


def normalize_text(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", str(value or ""))).strip()


def fetch_url_cache_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return SEARCH_CACHE_DIR / f"{URL_FETCH_CACHE_PREFIX}{digest}.json"


def load_fetch_url_cache(url: str) -> dict[str, Any] | None:
    path = fetch_url_cache_path(url)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    fetched_at = float(data.get("fetchedAt") or 0)
    if time.time() - fetched_at > SEARCH_CACHE_MAX_AGE_SECONDS:
        return None
    result = data.get("result")
    return result if isinstance(result, dict) else None


def save_fetch_url_cache(url: str, result: dict[str, Any]) -> None:
    SEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = fetch_url_cache_path(url)
    path.write_text(json.dumps({"url": url, "fetchedAt": time.time(), "result": result}, ensure_ascii=False), encoding="utf-8")
