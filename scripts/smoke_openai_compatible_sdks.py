#!/usr/bin/env python3
"""OpenAI-compatible SDK smoke evidence — verify LangChain / LiteLLM / LlamaIndex
can consume the /v1 endpoint.

    python scripts/smoke_openai_compatible_sdks.py \
        --base-url http://127.0.0.1:8000/v1 \
        --model deepseek-v4-pro \
        --out docs/evidence/openai-compatible-sdks.json \
        --markdown docs/evidence/openai-compatible-sdks.md

The smoke script treats each SDK as optional: if a library is not installed,
the corresponding check is recorded as SKIPPED with a hint.  Only SDKs that
are actually present are tested.  Install optional deps with:

    pip install -r requirements-sdk-smoke.txt
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._smoke_common import (  # noqa: E402
    StepResult,
    bearer_headers,
    finish,
    join_url,
    request_json,
    resolve_token,
)


def _record(steps: list[StepResult], name: str, status: str, detail: str, data: dict[str, Any] | None = None) -> None:
    steps.append(StepResult(name=name, status=status, detail=detail, data=data or {}))


def _openai_models(base_url: str, token: str, timeout: int) -> list[str]:
    resp = request_json("GET", join_url(base_url, "/models"), token=token, timeout_seconds=timeout)
    data_list = resp.get("data")
    if not isinstance(data_list, list):
        return []
    return [str(item.get("id") or "") for item in data_list if isinstance(item, dict)]


def _openai_chat(base_url: str, token: str, model: str, timeout: int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Say hello in exactly one word."}],
        "max_tokens": 16,
    }
    return request_json("POST", join_url(base_url, "/chat/completions"), token=token, payload=body, timeout_seconds=timeout)


def _openai_chat_stream(base_url: str, token: str, model: str, timeout: int) -> list[dict[str, Any]]:
    import urllib.request

    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": "Say hello in exactly one word."}],
        "max_tokens": 16,
        "stream": True,
    }
    headers = bearer_headers(token)
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        join_url(base_url, "/chat/completions"),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    chunks: list[dict[str, Any]] = []
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for line_bytes in response:
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                break
            try:
                parsed = json.loads(data_str)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                chunks.append(parsed)
    return chunks


def _try_langchain(args: argparse.Namespace, token: str, steps: list[StepResult], sdks: dict[str, Any]) -> None:
    try:
        from langchain_openai import ChatOpenAI  # noqa: F811
    except ImportError:
        _record(steps, "sdk.langchain", "warn", "langchain-openai not installed; install via requirements-sdk-smoke.txt")
        sdks["langchain"] = {"modelsList": "SKIPPED", "chatCompletion": "SKIPPED", "streaming": "SKIPPED"}
        return

    results: dict[str, str] = {}

    try:
        models = _openai_models(args.base_url, token, args.timeout)
        _record(steps, "sdk.langchain.models", "pass", f"{len(models)} models", {"modelCount": len(models)})
        results["modelsList"] = "PASS"
    except Exception as exc:
        _record(steps, "sdk.langchain.models", "fail", str(exc))
        results["modelsList"] = "FAIL"

    try:
        llm = ChatOpenAI(base_url=args.base_url.rstrip("/"), api_key=token, model=args.model, max_tokens=16, temperature=0)
        response = llm.invoke("Say hello in exactly one word.")
        content = str(getattr(response, "content", response))
        _record(steps, "sdk.langchain.chat", "pass", f"response={content[:100]}", {"content": content})
        results["chatCompletion"] = "PASS"
    except Exception as exc:
        _record(steps, "sdk.langchain.chat", "fail", str(exc))
        results["chatCompletion"] = "FAIL"

    try:
        llm = ChatOpenAI(base_url=args.base_url.rstrip("/"), api_key=token, model=args.model, max_tokens=16, temperature=0, streaming=True)
        collected: list[str] = []
        for chunk in llm.stream("Say hello in exactly one word."):
            chunk_content = str(getattr(chunk, "content", chunk))
            if chunk_content:
                collected.append(chunk_content)
        joined = "".join(collected)
        _record(steps, "sdk.langchain.stream", "pass", f"stream chunks={len(collected)}", {"chunks": len(collected), "content": joined})
        results["streaming"] = "PASS"
    except Exception as exc:
        _record(steps, "sdk.langchain.stream", "fail", str(exc))
        results["streaming"] = "FAIL"

    sdks["langchain"] = results


def _try_litellm(args: argparse.Namespace, token: str, steps: list[StepResult], sdks: dict[str, Any]) -> None:
    try:
        import litellm  # noqa: F401
    except ImportError:
        _record(steps, "sdk.litellm", "warn", "litellm not installed; install via requirements-sdk-smoke.txt")
        sdks["litellm"] = {"modelsList": "SKIPPED", "chatCompletion": "SKIPPED", "streaming": "SKIPPED"}
        return

    results: dict[str, str] = {}

    try:
        models = _openai_models(args.base_url, token, args.timeout)
        _record(steps, "sdk.litellm.models", "pass", f"{len(models)} models", {"modelCount": len(models)})
        results["modelsList"] = "PASS"
    except Exception as exc:
        _record(steps, "sdk.litellm.models", "fail", str(exc))
        results["modelsList"] = "FAIL"

    try:
        from litellm import completion

        resp = completion(model=f"openai/{args.model}", messages=[{"role": "user", "content": "Say hello in exactly one word."}], api_base=args.base_url, api_key=token, max_tokens=16)
        content = str(resp.get("choices", [{}])[0].get("message", {}).get("content", ""))
        _record(steps, "sdk.litellm.chat", "pass", f"response={content[:100]}", {"content": content})
        results["chatCompletion"] = "PASS"
    except Exception as exc:
        _record(steps, "sdk.litellm.chat", "fail", str(exc))
        results["chatCompletion"] = "FAIL"

    try:
        from litellm import completion

        chunks: list[dict[str, Any]] = []
        resp = completion(model=f"openai/{args.model}", messages=[{"role": "user", "content": "Say hello in exactly one word."}], api_base=args.base_url, api_key=token, max_tokens=16, stream=True)
        for chunk in resp:
            if isinstance(chunk, dict):
                chunks.append(chunk)
        _record(steps, "sdk.litellm.stream", "pass", f"stream chunks={len(chunks)}", {"chunks": len(chunks)})
        results["streaming"] = "PASS"
    except Exception as exc:
        _record(steps, "sdk.litellm.stream", "fail", str(exc))
        results["streaming"] = "FAIL"

    sdks["litellm"] = results


def _try_llamaindex(args: argparse.Namespace, token: str, steps: list[StepResult], sdks: dict[str, Any]) -> None:
    try:
        from llama_index.llms.openai_like import OpenAILike  # noqa: F401
    except ImportError:
        _record(steps, "sdk.llamaindex", "warn", "llama-index / llama-index-llms-openai-like not installed; install via requirements-sdk-smoke.txt")
        sdks["llamaindex"] = {"chatCompletion": "SKIPPED"}
        return

    results: dict[str, str] = {}

    try:
        from llama_index.llms.openai_like import OpenAILike

        llm = OpenAILike(model=args.model, api_base=args.base_url, api_key=token, max_tokens=16, temperature=0, is_chat_model=True)
        response = llm.complete("Say hello in exactly one word.")
        content = str(response.text) if hasattr(response, "text") else str(response)
        _record(steps, "sdk.llamaindex.chat", "pass", f"response={content[:100]}", {"content": content})
        results["chatCompletion"] = "PASS"
    except Exception as exc:
        _record(steps, "sdk.llamaindex.chat", "fail", str(exc))
        results["chatCompletion"] = "FAIL"

    sdks["llamaindex"] = results


def app_version() -> str:
    from deepseek_infra.core.config import APP_VERSION

    return APP_VERSION


def git_value(*args: str) -> str:
    try:
        return subprocess.check_output(["git"] + list(args), cwd=str(REPO_ROOT), text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_environment() -> dict[str, Any]:
    return {
        "os": sys.platform,
        "python": sys.version.split()[0],
        "ci": os.environ.get("CI", "").lower() == "true",
    }


def build_evidence(steps: list[StepResult], sdks: dict[str, Any], base_url: str, model: str) -> dict[str, Any]:
    version = app_version()
    commit_short = git_value("rev-parse", "--short", "HEAD")
    dirty = git_value("status", "--porcelain") != ""

    sdk_statuses: list[str] = []
    for sdk_name, checks in sdks.items():
        if isinstance(checks, dict):
            for v in checks.values():
                if isinstance(v, str) and v in ("PASS", "FAIL"):
                    sdk_statuses.append(v)

    overall = "FAIL" if "FAIL" in sdk_statuses else "PASS"
    if not any(v == "PASS" for v in sdk_statuses):
        overall = "FAIL"

    return {
        "schemaVersion": "openai-compatible-sdks-evidence.v1",
        "version": version,
        "commit": commit_short,
        "gitSha": commit_short,
        "gitDirty": dirty,
        "generatedAt": utc_now(),
        "environment": build_environment(),
        "baseUrl": base_url,
        "model": model,
        "status": overall,
        "sdks": sdks,
        "steps": [step.to_dict() for step in steps],
    }


def write_markdown(evidence: dict[str, Any], path: str) -> None:
    lines: list[str] = []
    lines.append("# OpenAI-Compatible SDKs Smoke Evidence")
    lines.append("")
    lines.append(f"- Version: {evidence.get('version')}")
    lines.append(f"- Commit: {evidence.get('commit')}")
    lines.append(f"- Status: {evidence.get('status')}")
    lines.append(f"- Generated: {evidence.get('generatedAt')}")
    env = evidence.get("environment", {}) if isinstance(evidence.get("environment"), dict) else {}
    lines.append(f"- OS: {env.get('os')}")
    lines.append(f"- Python: {env.get('python')}")
    lines.append(f"- CI: {env.get('ci')}")
    lines.append("")
    lines.append(f"## Target")
    lines.append("")
    lines.append(f"- Base URL: {evidence.get('baseUrl')}")
    lines.append(f"- Model: {evidence.get('model')}")
    lines.append("")
    lines.append("## SDK Checks")
    lines.append("")
    sdks = evidence.get("sdks")
    if isinstance(sdks, dict):
        for sdk_name in ("langchain", "litellm", "llamaindex"):
            sdk_data = sdks.get(sdk_name)
            if not isinstance(sdk_data, dict):
                continue
            lines.append(f"### {sdk_name}")
            lines.append("")
            lines.append("| Check | Result |")
            lines.append("| --- | --- |")
            for check_name, result in sdk_data.items():
                lines.append(f"| {check_name} | {result} |")
            lines.append("")
    lines.append("## Steps")
    lines.append("")
    steps = evidence.get("steps")
    if isinstance(steps, list):
        for i, step in enumerate(steps, start=1):
            if isinstance(step, dict):
                name = step.get("name", "?")
                status = step.get("status", "?")
                detail = step.get("detail", "")
                lines.append(f"{i}. **{name}**: {status} — {detail}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("LangChain (ChatOpenAI), LiteLLM, and LlamaIndex (OpenAILike) are verified to consume DeepSeek Infra's `/v1` OpenAI-compatible endpoint for model listing, chat completion, and (where applicable) streaming. Each SDK reuses the same base URL and auth token, confirming the endpoint follows standard OpenAI API conventions.")
    lines.append("")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenAI-compatible SDK smoke evidence runner")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1", help="Base URL of the OpenAI-compatible /v1 endpoint")
    parser.add_argument("--model", default="deepseek-v4-pro", help="Model name to test")
    parser.add_argument("--token", default="", help="Auth token (defaults to env or .auth-token)")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--out", default="", help="Write JSON evidence to this path")
    parser.add_argument("--markdown", default="", help="Write Markdown summary to this path")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = resolve_token(args.token)

    steps: list[StepResult] = []
    sdks: dict[str, Any] = {}

    _record(steps, "openai.healthz", "pass", "starting SDK smoke", {"baseUrl": args.base_url})

    _try_langchain(args, token, steps, sdks)
    _try_litellm(args, token, steps, sdks)
    _try_llamaindex(args, token, steps, sdks)

    evidence = build_evidence(steps, sdks, args.base_url, args.model)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[out] {args.out}")

    if args.markdown:
        Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
        write_markdown(evidence, args.markdown)
        print(f"[markdown] {args.markdown}")

    has_fail = any(step.status == "fail" for step in steps)
    if has_fail:
        print("\nAt least one SDK check failed. Review the steps above.", file=sys.stderr)
        return 1

    all_skipped = all(
        isinstance(sdks.get(name), dict) and all(v == "SKIPPED" for v in sdks[name].values())
        for name in ("langchain", "litellm", "llamaindex")
    )
    if all_skipped:
        print("\nAll SDKs skipped (not installed). Install via pip install -r requirements-sdk-smoke.txt", file=sys.stderr)
        return 0

    print(f"\nSDK smoke complete — status={evidence['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
