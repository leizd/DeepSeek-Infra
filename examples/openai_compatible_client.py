#!/usr/bin/env python3
"""OpenAI 兼容网关 demo：任意 OpenAI SDK 把 base_url 指向本机 /v1 即可复用整套运行时。

先启动本地服务（任选其一）::

    AUTH_DISABLED=1 python app.py          # 开发模式，无需 token
    python app.py                          # 正式模式，token 在 .auth-token / 终端打印

然后::

    python examples/openai_compatible_client.py --prompt "用一句话介绍这个项目"

`api_key` 传的是**本地访问 token**（鉴权用），上游 DeepSeek Key 由服务端
`DEEPSEEK_API_KEY` 提供。装了 `openai` 包就走官方 SDK，没装则用 stdlib 直接
POST /v1/chat/completions——两条路径打到完全相同的端点，证明协议兼容。
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
    """Local access token: --token > env > the .auth-token file the server writes."""
    if explicit:
        return explicit
    for env_name in ("DEEPSEEK_INFRA_TOKEN", "AUTH_TOKEN"):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    token_file = Path(".auth-token")
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return "local"  # AUTH_DISABLED=1 时服务端不校验，占位即可


def chat_via_openai_sdk(base_url: str, token: str, model: str, prompt: str) -> tuple[str, dict[str, Any]]:
    from openai import OpenAI

    client = OpenAI(base_url=base_url, api_key=token)
    models = [item.id for item in client.models.list().data]
    print(f"GET /v1/models → {models}")
    response = client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}])
    usage = response.usage.model_dump() if response.usage is not None else {}
    return response.choices[0].message.content or "", usage


def chat_via_stdlib(base_url: str, token: str, model: str, prompt: str) -> tuple[str, dict[str, Any]]:
    def call(path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST" if body is not None else "GET",
        )
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read().decode("utf-8"))

    models = [item.get("id") for item in call("/models").get("data", [])]
    print(f"GET /v1/models → {models}")
    completion = call("/chat/completions", {"model": model, "messages": [{"role": "user", "content": prompt}]})
    message = (completion.get("choices") or [{}])[0].get("message") or {}
    return str(message.get("content") or ""), dict(completion.get("usage") or {})


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenAI-compatible /v1 gateway demo")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--token", default="", help="本地访问 token（默认读 DEEPSEEK_INFRA_TOKEN / AUTH_TOKEN / .auth-token）")
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--prompt", default="用一句话介绍 local-first AI infrastructure 的核心价值。")
    args = parser.parse_args()

    token = resolve_token(args.token)
    try:
        content, usage = chat_via_openai_sdk(args.base_url, token, args.model, args.prompt)
        transport = "openai SDK"
    except ImportError:
        content, usage = chat_via_stdlib(args.base_url, token, args.model, args.prompt)
        transport = "stdlib urllib（未安装 openai 包，端点与 SDK 完全相同）"
    except Exception as exc:  # 网络 / 鉴权 / 上游错误：给出可操作的提示而不是堆栈
        print(f"请求失败：{exc}", file=sys.stderr)
        print("请确认本地服务已启动（python app.py），且 token 正确或使用 AUTH_DISABLED=1。", file=sys.stderr)
        return 1

    print(f"\n[transport] {transport}")
    print(f"[model] {args.model}")
    print(f"[usage] {usage}")
    print(f"\n{content}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
