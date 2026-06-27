#!/usr/bin/env python3
"""Smoke checks for the optional Edge-Cloud Model Router and Ollama provider."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._smoke_common import StepResult, finish, join_url, print_step, request_json, resolve_token  # noqa: E402


def _record(steps: list[StepResult], name: str, status: str, detail: str, data: dict[str, Any] | None = None, *, as_json: bool) -> None:
    step = StepResult(name=name, status=status, detail=detail, data=data or {})
    steps.append(step)
    print_step(step, as_json=as_json)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Edge Router status and OpenAI-compatible local model exposure.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Local DeepSeek Infra service root")
    parser.add_argument("--token", default="", help="Local auth token; defaults to env or .auth-token")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--require-edge", action="store_true", help="Fail if /api/edge/status is not available=true")
    parser.add_argument("--require-ollama", action="store_true", help="Fail if /v1/models does not expose an ollama/<tag> model")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary")
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    token = resolve_token(args.token)
    steps: list[StepResult] = []

    try:
        health = request_json("GET", join_url(base_url, "/healthz"), timeout_seconds=args.timeout)
        _record(steps, "healthz", "pass", f"status={health.get('status')}", as_json=args.json)
    except Exception as exc:
        _record(steps, "healthz", "fail", str(exc), as_json=args.json)
        return finish(steps, as_json=args.json)

    try:
        edge_payload = request_json("GET", join_url(base_url, "/api/edge/status"), token=token, timeout_seconds=args.timeout)
        edge_value = edge_payload.get("edgeInference")
        edge: dict[str, Any] = edge_value if isinstance(edge_value, dict) else {}
        available = bool(edge.get("available"))
        status = "pass" if available or not args.require_edge else "fail"
        if status == "pass" and not available:
            status = "warn"
        _record(
            steps,
            "edge.status",
            status,
            f"enabled={edge.get('enabled')} provider={edge.get('provider')} available={available}",
            {"edgeInference": edge},
            as_json=args.json,
        )
    except Exception as exc:
        _record(steps, "edge.status", "fail", str(exc), as_json=args.json)

    try:
        models_payload = request_json("GET", join_url(base_url, "/v1/models"), token=token, timeout_seconds=args.timeout)
        data_value = models_payload.get("data")
        data: list[Any] = data_value if isinstance(data_value, list) else []
        model_ids = [str(item.get("id") or "") for item in data if isinstance(item, dict)]
        ollama_ids = [model_id for model_id in model_ids if model_id.startswith("ollama/")]
        status = "pass" if ollama_ids or not args.require_ollama else "fail"
        if status == "pass" and not ollama_ids:
            status = "warn"
        _record(
            steps,
            "openai.models",
            status,
            f"models={len(model_ids)} ollama={len(ollama_ids)}",
            {"models": model_ids, "ollamaModels": ollama_ids},
            as_json=args.json,
        )
    except Exception as exc:
        _record(steps, "openai.models", "fail", str(exc), as_json=args.json)

    return finish(steps, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
