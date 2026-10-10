#!/usr/bin/env python3
"""聊天延迟基准（需要本地服务在跑 + DeepSeek Key，会真实消耗 token）。

对 ``POST /api/chat``（NDJSON 流式）测 N 轮：

- **TTFT**：发出请求到第一个 ``reasoning`` / ``content`` 事件；
- **总延迟**：到 ``done`` 事件；
- token 用量与语义缓存命中分布（同一 prompt 重复跑时第二轮起可能直接命中本地缓存）。

运行::

    python benchmarks/bench_chat_latency.py --n 3
    python benchmarks/bench_chat_latency.py --n 5 --unique-prompts --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from deepseek_infra.infra.evaluation import harness  # noqa: E402


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


def run_once(base_url: str, token: str, payload: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    started = time.perf_counter()
    first_token_ms = 0.0
    done_event: dict[str, Any] = {}
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            event = json.loads(line)
            kind = str(event.get("type") or "")
            if kind in {"reasoning", "content"} and not first_token_ms:
                first_token_ms = (time.perf_counter() - started) * 1000.0
            elif kind == "done":
                done_event = event
                break
            elif kind == "error":
                raise RuntimeError(f"{event.get('error')} (code={event.get('code')})")
    total_ms = (time.perf_counter() - started) * 1000.0
    usage = done_event.get("usage") or {}
    diagnostics = done_event.get("diagnostics") or {}
    cache = diagnostics.get("semanticCache") or {}
    return {
        "ttftMs": round(first_token_ms or total_ms, 1),
        "totalMs": round(total_ms, 1),
        "promptTokens": int(usage.get("prompt_tokens") or 0),
        "completionTokens": int(usage.get("completion_tokens") or 0),
        "cacheHit": bool(cache.get("hit")),
        "cacheHitTokens": int(diagnostics.get("cacheHitTokens") or 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Streaming chat latency benchmark (requires running server + key)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="")
    parser.add_argument("--api-key", default="", help="DeepSeek API Key；缺省用服务端 DEEPSEEK_API_KEY")
    parser.add_argument("--model", default="deepseek-v4-flash")
    parser.add_argument("--prompt", default="用 100 字以内说明什么是幂等接口。")
    parser.add_argument("--n", type=int, default=3)
    parser.add_argument("--unique-prompts", action="store_true", help="每轮在 prompt 末尾加序号，绕开语义缓存测纯上游延迟")
    parser.add_argument("--thinking", action="store_true", help="开启深度思考（默认关，专测响应路径）")
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    token = resolve_token(args.token)
    rows: list[dict[str, Any]] = []
    for index in range(max(1, args.n)):
        prompt = f"{args.prompt}（第 {index + 1} 问）" if args.unique_prompts else args.prompt
        payload: dict[str, Any] = {
            "model": args.model,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
            "searchEnabled": False,
            "searchMode": "off",
            "thinkingEnabled": bool(args.thinking),
        }
        if args.api_key:
            payload["apiKey"] = args.api_key
        try:
            row = run_once(args.base_url, token, payload, args.timeout_seconds)
        except Exception as exc:
            print(f"round {index + 1} failed: {exc}", file=sys.stderr)
            print("请确认本地服务已启动且 DeepSeek Key 已配置。", file=sys.stderr)
            return 1
        rows.append(row)
        cache_mark = " (semantic cache hit)" if row["cacheHit"] else ""
        print(f"round {index + 1}: TTFT {row['ttftMs']:.0f} ms · total {row['totalMs']:.0f} ms · tokens {row['promptTokens']}+{row['completionTokens']}{cache_mark}")

    report = {
        "suite": "bench_chat_latency",
        "model": args.model,
        "rounds": len(rows),
        "ttftMs": harness.latency_benchmark([row["ttftMs"] for row in rows]),
        "totalMs": harness.latency_benchmark([row["totalMs"] for row in rows]),
        "avgPromptTokens": round(sum(row["promptTokens"] for row in rows) / len(rows), 1),
        "avgCompletionTokens": round(sum(row["completionTokens"] for row in rows) / len(rows), 1),
        "semanticCacheHits": sum(1 for row in rows if row["cacheHit"]),
        "rows": rows,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    ttft = report["ttftMs"]
    total = report["totalMs"]
    print("\n=== Benchmark · Chat latency ===")
    print(f"Model: {args.model} · rounds: {report['rounds']} · thinking: {'on' if args.thinking else 'off'}")
    print(f"TTFT:  avg {ttft['avgMs']:.0f} ms · P50 {ttft['p50Ms']:.0f} ms · P95 {ttft['p95Ms']:.0f} ms")
    print(f"Total: avg {total['avgMs']:.0f} ms · P50 {total['p50Ms']:.0f} ms · P95 {total['p95Ms']:.0f} ms")
    print(f"Tokens: avg prompt {report['avgPromptTokens']} · completion {report['avgCompletionTokens']}")
    print(f"Semantic cache hits: {report['semanticCacheHits']}/{report['rounds']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
