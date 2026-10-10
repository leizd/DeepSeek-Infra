#!/usr/bin/env python3
"""多 Agent DAG 端到端基准（需要本地服务在跑 + DeepSeek Key，单轮即真实长任务）。

对 ``POST /api/chat``（``agentMode=true`` NDJSON 流式）测端到端：

- 总延迟（planner → workers 同层并行 → synthesizer）；
- 每 Agent 耗时表（``diagnostics.agentDurations``）与 worker 数；
- token 用量与估算成本（``agentCostUsd`` / ``costUsd``）。

多 Agent 一轮通常要几分钟、消耗上万 token，默认只跑 1 轮::

    python benchmarks/bench_agent_dag.py
    python benchmarks/bench_agent_dag.py --task "..." --n 2 --json
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
    workers: set[str] = set()
    done_event: dict[str, Any] = {}
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8").strip()
            if not line:
                continue
            event = json.loads(line)
            kind = str(event.get("type") or "")
            if kind == "agent_output":
                workers.add(str(event.get("phase") or ""))
            elif kind == "done":
                done_event = event
                break
            elif kind == "error":
                raise RuntimeError(f"{event.get('error')} (code={event.get('code')})")
    total_ms = (time.perf_counter() - started) * 1000.0
    usage = done_event.get("usage") or {}
    diagnostics = done_event.get("diagnostics") or {}
    return {
        "totalMs": round(total_ms, 1),
        "workers": sorted(workers - {""}),
        "agentDurations": diagnostics.get("agentDurations") or {},
        "totalTokens": int(usage.get("total_tokens") or 0),
        "agentCostUsd": diagnostics.get("agentCostUsd") or diagnostics.get("costUsd"),
        "traceId": diagnostics.get("traceId") or "",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent DAG end-to-end benchmark (requires running server + key)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="")
    parser.add_argument("--api-key", default="", help="DeepSeek API Key；缺省用服务端 DEEPSEEK_API_KEY")
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--task", default="比较 SQLite 与 PostgreSQL 在本地优先应用里的取舍，并给出选型建议。")
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--timeout-minutes", type=float, default=65.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    token = resolve_token(args.token)
    rows: list[dict[str, Any]] = []
    for index in range(max(1, args.n)):
        payload: dict[str, Any] = {
            "model": args.model,
            "stream": True,
            "agentMode": True,
            "messages": [{"role": "user", "content": args.task}],
            "searchEnabled": False,
            "searchMode": "off",
        }
        if args.api_key:
            payload["apiKey"] = args.api_key
        try:
            row = run_once(args.base_url, token, payload, args.timeout_minutes * 60.0)
        except Exception as exc:
            print(f"round {index + 1} failed: {exc}", file=sys.stderr)
            print("请确认本地服务已启动且 DeepSeek Key 已配置；多 Agent 一轮可能要数分钟。", file=sys.stderr)
            return 1
        rows.append(row)
        print(f"round {index + 1}: total {row['totalMs'] / 1000.0:.1f} s · workers {len(row['workers'])} · tokens {row['totalTokens']}")
        for agent_id, duration in (row["agentDurations"] or {}).items():
            print(f"   - {agent_id}: {duration}")

    report = {
        "suite": "bench_agent_dag",
        "model": args.model,
        "rounds": len(rows),
        "totalMs": harness.latency_benchmark([row["totalMs"] for row in rows]),
        "avgTotalTokens": round(sum(row["totalTokens"] for row in rows) / len(rows), 1),
        "rows": rows,
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    total = report["totalMs"]
    print("\n=== Benchmark · Agent DAG ===")
    print(f"Model: {args.model} · rounds: {report['rounds']}")
    print(f"End-to-end: avg {total['avgMs'] / 1000.0:.1f} s · P95 {total['p95Ms'] / 1000.0:.1f} s")
    print(f"Avg total tokens: {report['avgTotalTokens']}")
    if rows and rows[-1].get("agentCostUsd") is not None:
        print(f"Last-round est. cost: ${rows[-1]['agentCostUsd']}")
    if rows and rows[-1].get("traceId"):
        print(f"Last traceId: {rows[-1]['traceId']}（应用内可打开 Trace 瀑布图）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
