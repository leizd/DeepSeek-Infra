#!/usr/bin/env python3
"""Compatibility smoke runner for DeepSeek Infra's A2A Agent Mesh."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._smoke_common import (  # noqa: E402
    SmokeFailure,
    StepResult,
    bearer_headers,
    finish,
    join_url,
    jsonrpc,
    print_step,
    request_json,
    resolve_token,
    rpc_result,
)


def _message_params(text: str, message_id: str) -> dict[str, Any]:
    return {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
            "messageId": message_id,
            "kind": "message",
        }
    }


def _record(steps: list[StepResult], name: str, status: str, detail: str, data: dict[str, Any] | None = None, *, as_json: bool) -> None:
    step = StepResult(name=name, status=status, detail=detail, data=data or {})
    steps.append(step)
    print_step(step, as_json=as_json)


def _post_rpc(endpoint: str, method: str, params: dict[str, Any] | None, *, token: str, timeout: int, message_id: int) -> dict[str, Any]:
    return request_json("POST", endpoint, token=token, payload=jsonrpc(method, params, message_id), timeout_seconds=timeout)


def _read_sse_rpc(
    endpoint: str,
    method: str,
    params: dict[str, Any],
    *,
    token: str,
    timeout: int,
    message_id: int,
    max_events: int,
) -> list[dict[str, Any]]:
    payload = json.dumps(jsonrpc(method, params, message_id), ensure_ascii=False).encode("utf-8")
    headers = bearer_headers(token, accept="text/event-stream")
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
    events: list[dict[str, Any]] = []
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                parsed = json.loads(line[len("data: ") :])
                if not isinstance(parsed, dict):
                    raise SmokeFailure(f"{method} returned non-object SSE event")
                events.append(parsed)
                result_value = parsed.get("result")
                result: dict[str, Any] = result_value if isinstance(result_value, dict) else {}
                if result.get("final") is True or len(events) >= max_events:
                    break
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SmokeFailure(f"{method} returned HTTP {exc.code}: {body[:600]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SmokeFailure(f"{method} stream failed: {exc}") from exc
    return events


def _task_id_from_result(result: dict[str, Any]) -> str:
    task_id = str(result.get("id") or "")
    if not task_id:
        raise SmokeFailure("task result did not include id")
    return task_id


def _check_discovery(args: argparse.Namespace, steps: list[StepResult], token: str) -> None:
    card_url = join_url(args.base_url, "/.well-known/agent-card.json")
    try:
        card = request_json("GET", card_url, timeout_seconds=args.timeout)
        skills = card.get("skills") if isinstance(card.get("skills"), list) else []
        if not card.get("url") or not skills:
            raise SmokeFailure("Agent Card missing url or skills")
        _record(
            steps,
            "a2a.agent_card",
            "pass",
            f"name={card.get('name')} protocol={card.get('protocolVersion')}",
            {"url": card.get("url"), "skills": len(skills)},
            as_json=args.json,
        )
    except SmokeFailure as exc:
        _record(steps, "a2a.agent_card", "fail", str(exc), as_json=args.json)
        return

    try:
        agents_payload = request_json("GET", join_url(args.base_url, "/a2a/agents"), token=token, timeout_seconds=args.timeout)
        agents = agents_payload.get("agents") if isinstance(agents_payload.get("agents"), list) else []
        if not agents:
            raise SmokeFailure("/a2a/agents returned no agents")
        _record(steps, "a2a.agents_list", "pass", f"{len(agents)} agent cards", {"agentCount": len(agents)}, as_json=args.json)
    except SmokeFailure as exc:
        _record(steps, "a2a.agents_list", "fail", str(exc), as_json=args.json)


def _check_task_lifecycle(args: argparse.Namespace, steps: list[StepResult], token: str) -> str:
    endpoint = join_url(args.base_url, f"/a2a/agents/{args.agent}")
    task_id = ""
    try:
        sent = rpc_result(
            _post_rpc(endpoint, "message/send", _message_params(args.message, "compat-send-1"), token=token, timeout=args.timeout, message_id=10),
            "message/send",
        )
        task_id = _task_id_from_result(sent)
        status_value = sent.get("status")
        status: dict[str, Any] = status_value if isinstance(status_value, dict) else {}
        _record(steps, "a2a.message_send", "pass", f"task={task_id} state={status.get('state')}", {"taskId": task_id}, as_json=args.json)
    except SmokeFailure as exc:
        _record(steps, "a2a.message_send", "fail", str(exc), as_json=args.json)
        return task_id

    try:
        fetched = rpc_result(_post_rpc(endpoint, "tasks/get", {"id": task_id, "historyLength": 1}, token=token, timeout=args.timeout, message_id=11), "tasks/get")
        _record(steps, "a2a.tasks_get", "pass", f"task={fetched.get('id')}", {"taskId": fetched.get("id")}, as_json=args.json)
    except SmokeFailure as exc:
        _record(steps, "a2a.tasks_get", "fail", str(exc), as_json=args.json)
    return task_id


def _check_streaming(args: argparse.Namespace, steps: list[StepResult], token: str) -> str:
    endpoint = join_url(args.base_url, f"/a2a/agents/{args.agent}")
    stream_task_id = ""
    try:
        events = _read_sse_rpc(
            endpoint,
            "message/stream",
            _message_params(args.message, "compat-stream-1"),
            token=token,
            timeout=args.timeout,
            message_id=20,
            max_events=args.max_events,
        )
        if not events:
            raise SmokeFailure("message/stream returned no SSE events")
        results: list[dict[str, Any]] = []
        for event in events:
            result_value = event.get("result")
            if isinstance(result_value, dict):
                results.append(result_value)
        first = results[0] if results else {}
        stream_task_id = str(first.get("id") or first.get("taskId") or "")
        artifact_updates = [result for result in results if result.get("kind") == "artifact-update"]
        final_status = [result for result in results if result.get("kind") == "status-update" and result.get("final") is True]
        if not final_status:
            raise SmokeFailure("message/stream did not emit a final status-update")
        status = "pass"
        final_status_value = final_status[-1].get("status")
        final_status_body: dict[str, Any] = final_status_value if isinstance(final_status_value, dict) else {}
        detail = f"events={len(events)} artifacts={len(artifact_updates)} final={final_status_body.get('state')}"
        if args.strict_artifacts and not artifact_updates:
            raise SmokeFailure("message/stream emitted no artifact-update events")
        if not artifact_updates:
            status = "warn"
            detail += "; no artifact chunks, likely no upstream API key or upstream failed"
        _record(
            steps,
            "a2a.message_stream",
            status,
            detail,
            {"events": len(events), "artifactUpdates": len(artifact_updates), "taskId": stream_task_id},
            as_json=args.json,
        )
    except SmokeFailure as exc:
        _record(steps, "a2a.message_stream", "fail", str(exc), as_json=args.json)
    return stream_task_id


def _check_resubscribe(args: argparse.Namespace, steps: list[StepResult], token: str, task_id: str) -> None:
    if not task_id:
        _record(steps, "a2a.tasks_resubscribe", "fail", "no task id available from stream check", as_json=args.json)
        return
    endpoint = join_url(args.base_url, f"/a2a/agents/{args.agent}")
    try:
        events = _read_sse_rpc(
            endpoint,
            "tasks/resubscribe",
            {"id": task_id, "afterChunkIndex": args.after_chunk_index},
            token=token,
            timeout=args.timeout,
            message_id=30,
            max_events=args.max_events,
        )
        if not events:
            raise SmokeFailure("tasks/resubscribe returned no SSE events")
        final = [
            event.get("result")
            for event in events
            if isinstance(event.get("result"), dict) and event.get("result", {}).get("kind") == "status-update"
        ]
        if not final:
            raise SmokeFailure("tasks/resubscribe did not return status-update")
        _record(steps, "a2a.tasks_resubscribe", "pass", f"events={len(events)} task={task_id}", {"events": len(events)}, as_json=args.json)
    except SmokeFailure as exc:
        _record(steps, "a2a.tasks_resubscribe", "fail", str(exc), as_json=args.json)


def _check_cancel(args: argparse.Namespace, steps: list[StepResult], token: str) -> None:
    endpoint = join_url(args.base_url, f"/a2a/agents/{args.agent}")
    try:
        sent = rpc_result(
            _post_rpc(endpoint, "message/send", _message_params(args.message, "compat-cancel-1"), token=token, timeout=args.timeout, message_id=40),
            "message/send cancel probe",
        )
        task_id = _task_id_from_result(sent)
        cancel_response = _post_rpc(endpoint, "tasks/cancel", {"id": task_id}, token=token, timeout=args.timeout, message_id=41)
        error = cancel_response.get("error")
        if isinstance(error, dict):
            if error.get("code") == -32002:
                _record(steps, "a2a.tasks_cancel", "warn", "endpoint reachable, but task was already terminal", {"taskId": task_id}, as_json=args.json)
                return
            raise SmokeFailure(f"tasks/cancel returned JSON-RPC error {error.get('code')}: {error.get('message')}")
        result_value = cancel_response.get("result")
        result: dict[str, Any] = result_value if isinstance(result_value, dict) else {}
        status_value = result.get("status")
        status = status_value if isinstance(status_value, dict) else {}
        _record(steps, "a2a.tasks_cancel", "pass", f"task={task_id} state={status.get('state')}", {"taskId": task_id}, as_json=args.json)
    except SmokeFailure as exc:
        _record(steps, "a2a.tasks_cancel", "fail", str(exc), as_json=args.json)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run A2A compatibility smoke checks against a local DeepSeek Infra server.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Local DeepSeek Infra service root")
    parser.add_argument("--agent", default="reasoner", help="A2A agent id to target")
    parser.add_argument("--token", default="", help="Local auth token; defaults to env or .auth-token")
    parser.add_argument("--message", default="Summarize why compatibility smoke tests matter.")
    parser.add_argument("--after-chunk-index", type=int, default=-1, help="Cursor used for tasks/resubscribe")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--max-events", type=int, default=50)
    parser.add_argument("--strict-artifacts", action="store_true", help="Fail if message/stream emits no artifact-update chunks")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary")
    args = parser.parse_args(argv)

    args.base_url = args.base_url.rstrip("/")
    token = resolve_token(args.token)
    steps: list[StepResult] = []
    _check_discovery(args, steps, token)
    _check_task_lifecycle(args, steps, token)
    stream_task_id = _check_streaming(args, steps, token)
    _check_resubscribe(args, steps, token, stream_task_id)
    _check_cancel(args, steps, token)
    return finish(steps, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
