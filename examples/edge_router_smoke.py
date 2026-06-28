#!/usr/bin/env python3
"""Smoke checks for the optional Edge-Cloud Model Router and Ollama provider."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._smoke_common import StepResult, finish, join_url, print_step, request_json, resolve_token  # noqa: E402

SCHEMA_VERSION = "edge-router-smoke-evidence.v1"
DEFAULT_EVIDENCE_PATH = REPO_ROOT / "docs" / "evidence" / "edge-router-smoke.json"
DEFAULT_MARKDOWN_PATH = REPO_ROOT / "docs" / "evidence" / "edge-router-smoke.md"
REQUIRED_CHECKS = (
    "ollamaModelsListed",
    "openaiCompatibleLocalCall",
    "edgeStatusEndpoint",
    "fallbackReady",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def git_value(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def app_version() -> str:
    from deepseek_infra.core.config import APP_VERSION

    return APP_VERSION


def build_environment() -> dict[str, Any]:
    return {
        "os": platform.system(),
        "python": platform.python_version(),
        "ci": bool(os.environ.get("CI")),
        "ollamaEnabled": os.environ.get("OLLAMA_ENABLED", ""),
        "ollamaBaseUrl": os.environ.get("OLLAMA_BASE_URL", ""),
        "ollamaProviderNote": os.environ.get("EDGE_ROUTER_SMOKE_PROVIDER_NOTE", ""),
        "edgeInferenceEnabled": os.environ.get("EDGE_INFERENCE_ENABLED", ""),
    }


def _record(steps: list[StepResult], name: str, status: str, detail: str, data: dict[str, Any] | None = None, *, as_json: bool) -> None:
    step = StepResult(name=name, status=status, detail=detail, data=data or {})
    steps.append(step)
    print_step(step, as_json=as_json)


def _check_label(status: str) -> str:
    return {"pass": "PASS", "warn": "WARNING", "fail": "FAIL"}.get(status, str(status).upper())


def checks_from_steps(steps: list[dict[str, Any]]) -> dict[str, str]:
    by_name = {str(step.get("name")): step for step in steps}
    edge_step = by_name.get("edge.status") or {}
    models_step = by_name.get("openai.models") or {}
    chat_step = by_name.get("openai.chat") or {}
    edge_data = edge_step.get("data") if isinstance(edge_step.get("data"), dict) else {}
    edge_payload = edge_data.get("edgeInference") if isinstance(edge_data, dict) else {}
    edge = edge_payload if isinstance(edge_payload, dict) else {}
    local_call_passed = chat_step.get("status") == "pass"
    edge_available = bool(edge.get("available"))
    checks = {
        "ollamaModelsListed": _check_label(str(models_step.get("status") or "fail")),
        "openaiCompatibleLocalCall": _check_label(str(chat_step.get("status") or "fail")),
        "edgeStatusEndpoint": "PASS" if edge_step.get("status") in {"pass", "warn"} else "FAIL",
        "fallbackReady": "PASS" if edge_available or local_call_passed else ("FAIL" if edge_step.get("status") == "fail" else "WARNING"),
    }
    return checks


def build_evidence(steps: list[dict[str, Any]], *, base_url: str, token_used: bool) -> dict[str, Any]:
    checks = checks_from_steps(steps)
    failed = [step for step in steps if step.get("status") == "fail"]
    status = "FAIL" if failed else ("PASS" if all(checks[name] == "PASS" for name in REQUIRED_CHECKS) else "WARNING")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "version": app_version(),
        "commit": git_value("rev-parse", "--short", "HEAD") or "unknown",
        "generatedAt": utc_now(),
        "environment": build_environment(),
        "gitSha": git_value("rev-parse", "--short", "HEAD") or "unknown",
        "gitDirty": bool(git_value("status", "--short")),
        "status": status,
        "baseUrl": base_url,
        "auth": "token" if token_used else "disabled-or-not-provided",
        "checks": checks,
        "steps": steps,
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def render_markdown(evidence: dict[str, Any]) -> str:
    lines = [
        "# Edge Router Smoke Evidence",
        "",
        f"- Version: {evidence.get('version')}",
        f"- Generated: {evidence.get('generatedAt')}",
        f"- Status: {evidence.get('status')}",
        f"- Base URL: `{evidence.get('baseUrl')}`",
        "",
        "## Environment",
        "",
        "| Key | Value |",
        "| --- | --- |",
    ]
    env_raw = evidence.get("environment")
    env: dict[str, Any] = env_raw if isinstance(env_raw, dict) else {}
    for key in ("os", "python", "ci", "ollamaEnabled", "ollamaBaseUrl", "ollamaProviderNote", "edgeInferenceEnabled"):
        lines.append(f"| {key} | `{env.get(key, '')}` |")
    lines.extend([
        "",
        "| Check | Status |",
        "| --- | --- |",
    ])
    checks_raw = evidence.get("checks")
    checks: dict[str, Any] = checks_raw if isinstance(checks_raw, dict) else {}
    for name in REQUIRED_CHECKS:
        lines.append(f"| {name} | {checks.get(name, 'MISSING')} |")
    lines.extend(["", "## Steps", "", "| Step | Status | Detail |", "| --- | --- | --- |"])
    steps_raw = evidence.get("steps")
    steps: list[Any] = steps_raw if isinstance(steps_raw, list) else []
    for step in steps:
        if not isinstance(step, dict):
            continue
        detail = str(step.get("detail") or "").replace("|", "\\|")
        lines.append(f"| {step.get('name')} | {step.get('status')} | {detail} |")
    lines.append("")
    return "\n".join(lines)


def write_markdown(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(evidence), encoding="utf-8")


def _chat_payload(model: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Say hello in one short sentence."}],
        "temperature": 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Edge Router status and OpenAI-compatible local model exposure.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Local DeepSeek Infra service root")
    parser.add_argument("--token", default="", help="Local auth token; defaults to env or .auth-token")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--require-edge", action="store_true", help="Fail if /api/edge/status is not available=true")
    parser.add_argument("--require-ollama", action="store_true", help="Fail if /v1/models does not expose an ollama/<tag> model")
    parser.add_argument("--skip-local-call", action="store_true", help="Do not call /v1/chat/completions for the first ollama/<tag> model.")
    parser.add_argument("--out", type=Path, default=None, help="Write structured evidence JSON.")
    parser.add_argument("--markdown", type=Path, default=None, help="Write Markdown evidence summary.")
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
        evidence = build_evidence([step.to_dict() for step in steps], base_url=base_url, token_used=bool(token))
        if args.out:
            write_evidence(args.out, evidence)
        if args.markdown:
            write_markdown(args.markdown, evidence)
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

    model_step = next((step for step in steps if step.name == "openai.models"), None)
    model_data = model_step.data if model_step is not None else {}
    ollama_models_raw = model_data.get("ollamaModels") if isinstance(model_data, dict) else []
    ollama_models = [str(model) for model in ollama_models_raw] if isinstance(ollama_models_raw, list) else []
    if args.skip_local_call:
        _record(steps, "openai.chat", "warn", "skipped by --skip-local-call", as_json=args.json)
    elif ollama_models:
        model = ollama_models[0]
        try:
            chat_payload = request_json(
                "POST",
                join_url(base_url, "/v1/chat/completions"),
                token=token,
                payload=_chat_payload(model),
                timeout_seconds=args.timeout,
            )
            choices = chat_payload.get("choices")
            choice_count = len(choices) if isinstance(choices, list) else 0
            status = "pass" if choice_count else "fail"
            _record(steps, "openai.chat", status, f"model={model} choices={choice_count}", {"model": model, "choiceCount": choice_count}, as_json=args.json)
        except Exception as exc:
            _record(steps, "openai.chat", "fail" if args.require_ollama else "warn", str(exc), as_json=args.json)
    else:
        _record(steps, "openai.chat", "fail" if args.require_ollama else "warn", "no ollama/<tag> model available for local call", as_json=args.json)

    evidence = build_evidence([step.to_dict() for step in steps], base_url=base_url, token_used=bool(token))
    if args.out:
        write_evidence(args.out, evidence)
    if args.markdown:
        write_markdown(args.markdown, evidence)
    if args.json:
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    else:
        if args.out:
            print(f"Edge Router evidence: {evidence['status']} ({len(evidence['steps'])} steps)")
            print(f"Wrote {args.out}")
        if args.markdown:
            print(f"Wrote {args.markdown}")
    return 1 if evidence["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
