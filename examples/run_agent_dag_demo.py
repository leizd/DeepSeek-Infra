#!/usr/bin/env python3
"""多 Agent DAG demo：流式驱动 planner → workers（同层并行）→ synthesizer，实时打印事件。

需要本地服务在跑、且服务端或 --api-key 提供 DeepSeek Key（会真实消耗 token）::

    python examples/run_agent_dag_demo.py
    python examples/run_agent_dag_demo.py --task "对比 SQLite 与 DuckDB 的适用场景" --max-minutes 20

事件流是 `/api/chat` 的 NDJSON：`agent`（worker 卡片状态）、`agent_output`（worker
产出）、`reasoning` / `content`（synthesizer 流式正文）、`done`（usage + 每 Agent 耗时表）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any


def resolve_token(explicit: str) -> str:
    if explicit:
        return explicit
    for env_name in ("DEEPSEEK_INFRA_TOKEN", "AUTH_TOKEN"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    token_file = Path(".auth-token")
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return "local"


def shorten(text: str, limit: int = 96) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def print_done_summary(event: dict[str, Any]) -> None:
    usage = event.get("usage") or {}
    diagnostics = event.get("diagnostics") or {}
    print("\n\n=== done ===")
    print(f"model: {event.get('model')}")
    print(f"usage: prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')} total={usage.get('total_tokens')}")
    durations = diagnostics.get("agentDurations")
    if isinstance(durations, dict) and durations:
        print("agent durations:")
        for agent_id, duration in durations.items():
            print(f"  - {agent_id}: {duration}")
    for key in ("agentCostUsd", "costUsd", "traceId"):
        if diagnostics.get(key):
            print(f"{key}: {diagnostics[key]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent DAG runtime streaming demo")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="")
    parser.add_argument("--api-key", default="", help="DeepSeek API Key；缺省用服务端 DEEPSEEK_API_KEY")
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--task", default="用 3 个要点比较 BM25 与稠密向量检索的取舍，最后给出一个混合检索的落地建议。")
    parser.add_argument("--search", action="store_true", help="允许 researcher 联网搜索（需要 Tavily Key）")
    parser.add_argument("--max-minutes", type=float, default=30.0, help="socket idle 超时（分钟）")
    args = parser.parse_args()

    payload: dict[str, Any] = {
        "model": args.model,
        "stream": True,
        "agentMode": True,
        "messages": [{"role": "user", "content": args.task}],
        "searchEnabled": bool(args.search),
        "searchMode": "auto" if args.search else "off",
    }
    if args.api_key:
        payload["apiKey"] = args.api_key

    request = urllib.request.Request(
        args.base_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {resolve_token(args.token)}"},
        method="POST",
    )
    print(f"task: {args.task}\n")
    reasoning_chars = 0
    streamed_content = False
    try:
        with urllib.request.urlopen(request, timeout=max(60.0, args.max_minutes * 60.0)) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                event = json.loads(line)
                kind = str(event.get("type") or "")
                if kind == "agent":
                    name = event.get("name") or event.get("phase")
                    note = shorten(str(event.get("text") or ""))
                    print(f"\n[agent] {name} → {event.get('status')}" + (f" · {note}" if note else ""))
                elif kind == "agent_output":
                    output = event.get("output") or {}
                    summary = output.get("summary") or output.get("content") or ""
                    print(f"[output] {event.get('phase')} 产出 {len(str(summary))} 字符：{shorten(str(summary))}")
                elif kind == "system_note":
                    print(f"[note] {shorten(str(event.get('text') or ''))}")
                elif kind == "reasoning":
                    reasoning_chars += len(str(event.get("text") or ""))
                elif kind == "content":
                    if not streamed_content:
                        print("\n--- 综合回答（流式） ---")
                        streamed_content = True
                    sys.stdout.write(str(event.get("text") or ""))
                    sys.stdout.flush()
                elif kind == "done":
                    if reasoning_chars:
                        print(f"\n[reasoning] 共 {reasoning_chars} 字符（已折叠）")
                    print_done_summary(event)
                    return 0
                elif kind == "error":
                    print(f"\n[error] {event.get('error')}（code={event.get('code')}）", file=sys.stderr)
                    return 1
    except Exception as exc:
        print(f"请求失败：{exc}", file=sys.stderr)
        print("请确认本地服务已启动、DeepSeek Key 已配置（服务端环境变量或 --api-key）。", file=sys.stderr)
        return 1
    print("\n流意外结束（未收到 done 事件）", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
