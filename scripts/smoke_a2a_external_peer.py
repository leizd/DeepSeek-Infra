#!/usr/bin/env python3
"""Headless smoke runner for an external A2A peer.

The runner can target any already-running A2A peer with ``--peer-url``. If no
peer URL is supplied, it starts the bundled independent-process peer from
``examples/a2a_interop_peer.py`` on an ephemeral local port, then validates the
same external HTTP boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts._smoke_common import SmokeFailure, bearer_headers, join_url, jsonrpc, request_json, rpc_result  # noqa: E402

SCHEMA_VERSION = "a2a-external-peer-evidence.v1"
DEFAULT_EVIDENCE_PATH = REPO_ROOT / "docs" / "evidence" / "a2a-external-peer.json"
DEFAULT_MESSAGE = "Validate A2A external peer compatibility with artifact streaming."
REQUIRED_CHECKS = (
    "agentCard",
    "messageSend",
    "messageStream",
    "tasksGet",
    "tasksCancel",
    "tasksList",
    "artifactChunks",
    "sseFinalEvent",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def git_value(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, check=False, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def app_version() -> str:
    from deepseek_infra.core.config import APP_VERSION

    return APP_VERSION


def record(steps: list[dict[str, Any]], name: str, status: str, detail: str, data: dict[str, Any] | None = None) -> None:
    steps.append({"name": name, "status": status, "detail": detail, "data": data or {}})


def _message_params(text: str, message_id: str) -> dict[str, Any]:
    return {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
            "messageId": message_id,
            "kind": "message",
        }
    }


def _base_from_peer_url(peer_url: str) -> str:
    url = str(peer_url or "").strip().rstrip("/")
    if not url:
        raise SmokeFailure("peer URL is required")
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.rstrip("/")
    marker = "/a2a/agents/"
    if marker in path:
        base_path = path.split(marker, 1)[0]
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, base_path.rstrip("/"), "", ""))
    return url


def _endpoint_from_card(card: dict[str, Any], fallback_peer_url: str) -> str:
    card_url = str(card.get("url") or "").strip()
    if card_url:
        return card_url.rstrip("/")
    parsed = urllib.parse.urlsplit(fallback_peer_url.rstrip("/"))
    if "/a2a/agents/" in parsed.path:
        return fallback_peer_url.rstrip("/")
    raise SmokeFailure("Agent Card missing url")


def validate_agent_card(card: dict[str, Any]) -> None:
    if not str(card.get("name") or "").strip():
        raise SmokeFailure("Agent Card missing name")
    if not str(card.get("url") or "").strip():
        raise SmokeFailure("Agent Card missing url")
    if not str(card.get("protocolVersion") or "").strip():
        raise SmokeFailure("Agent Card missing protocolVersion")
    skills = card.get("skills")
    if not isinstance(skills, list) or not skills:
        raise SmokeFailure("Agent Card missing skills")


def _post_rpc(endpoint: str, method: str, params: dict[str, Any] | None, *, timeout_seconds: int, message_id: int) -> dict[str, Any]:
    return request_json("POST", endpoint, payload=jsonrpc(method, params, message_id), timeout_seconds=timeout_seconds)


def _read_sse_rpc(
    endpoint: str,
    method: str,
    params: dict[str, Any],
    *,
    timeout_seconds: int,
    message_id: int,
    max_events: int,
) -> list[dict[str, Any]]:
    payload = json.dumps(jsonrpc(method, params, message_id), ensure_ascii=False).encode("utf-8")
    headers = bearer_headers("", accept="text/event-stream")
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
    events: list[dict[str, Any]] = []
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                parsed = json.loads(line[len("data:") :].strip())
                if not isinstance(parsed, dict):
                    raise SmokeFailure(f"{method} returned non-object SSE event")
                events.append(parsed)
                result_value = parsed.get("result")
                result: dict[str, Any] = result_value if isinstance(result_value, dict) else {}
                if (result.get("kind") == "status-update" and result.get("final") is True) or len(events) >= max_events:
                    break
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SmokeFailure(f"{method} returned HTTP {exc.code}: {body[:600]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SmokeFailure(f"{method} stream failed: {exc}") from exc
    return events


def task_id_from_result(result: dict[str, Any]) -> str:
    task_id = str(result.get("id") or result.get("taskId") or "").strip()
    if not task_id:
        raise SmokeFailure("task result did not include task id")
    return task_id


def summarize_stream_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        raise SmokeFailure("message/stream returned no SSE events")
    results: list[dict[str, Any]] = []
    for event in events:
        error = event.get("error")
        if isinstance(error, dict):
            raise SmokeFailure(f"message/stream returned JSON-RPC error {error.get('code')}: {error.get('message')}")
        result_value = event.get("result")
        if isinstance(result_value, dict):
            results.append(result_value)
    final_events = [result for result in results if result.get("kind") == "status-update" and result.get("final") is True]
    if not final_events:
        raise SmokeFailure("message/stream did not emit a final status-update")
    artifact_updates = [result for result in results if result.get("kind") == "artifact-update"]
    if not artifact_updates:
        raise SmokeFailure("message/stream emitted no artifact-update chunks")
    indices: list[int] = []
    for update in artifact_updates:
        chunk_index = update.get("chunkIndex")
        if chunk_index is None:
            raise SmokeFailure("artifact-update missing numeric chunkIndex")
        try:
            indices.append(int(chunk_index))
        except (TypeError, ValueError) as exc:
            raise SmokeFailure("artifact-update missing numeric chunkIndex") from exc
    if indices != list(range(len(indices))):
        raise SmokeFailure(f"artifact chunks were not sequential from 0: {indices}")
    if artifact_updates[-1].get("final") is not True:
        raise SmokeFailure("last artifact chunk was not marked final")
    first = results[0] if results else {}
    task_id = str(first.get("id") or first.get("taskId") or artifact_updates[0].get("taskId") or "")
    final_status_value = final_events[-1].get("status")
    final_status: dict[str, Any] = final_status_value if isinstance(final_status_value, dict) else {}
    return {
        "events": len(events),
        "artifactUpdates": len(artifact_updates),
        "chunkIndices": indices,
        "taskId": task_id,
        "finalState": final_status.get("state"),
    }


def validate_cancel_result(result: dict[str, Any]) -> str:
    status_value = result.get("status")
    status: dict[str, Any] = status_value if isinstance(status_value, dict) else {}
    state = str(status.get("state") or "")
    if state not in {"canceling", "canceled"}:
        raise SmokeFailure(f"tasks/cancel returned unexpected state: {state or '<missing>'}")
    return state


def validate_tasks_list(result: dict[str, Any], expected_task_id: str) -> int:
    tasks_value = result.get("tasks")
    tasks: list[Any] = tasks_value if isinstance(tasks_value, list) else []
    if not tasks:
        raise SmokeFailure("tasks/list returned no tasks")
    if expected_task_id and not any(isinstance(task, dict) and str(task.get("id") or "") == expected_task_id for task in tasks):
        raise SmokeFailure(f"tasks/list did not include expected task id: {expected_task_id}")
    return len(tasks)


def checks_from_steps(steps: list[dict[str, Any]]) -> dict[str, str]:
    mapping = {
        "a2a.agent_card": "agentCard",
        "a2a.message_send": "messageSend",
        "a2a.message_stream": "messageStream",
        "a2a.tasks_get": "tasksGet",
        "a2a.tasks_cancel": "tasksCancel",
        "a2a.tasks_list": "tasksList",
        "a2a.artifact_chunks": "artifactChunks",
        "a2a.sse_final_event": "sseFinalEvent",
    }
    checks = {name: "fail" for name in REQUIRED_CHECKS}
    for step in steps:
        key = mapping.get(str(step.get("name") or ""))
        if key:
            checks[key] = "pass" if step.get("status") == "pass" else "fail"
    return checks


def run_smoke(peer_url: str, *, timeout_seconds: int, max_events: int, message: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    peer: dict[str, Any] = {"url": peer_url.rstrip("/"), "endpoint": "", "name": "", "protocolVersion": ""}
    endpoint = ""
    sent_task_id = ""
    try:
        base_url = _base_from_peer_url(peer_url)
        card_url = join_url(base_url, "/.well-known/agent-card.json")
        card = request_json("GET", card_url, timeout_seconds=timeout_seconds)
        validate_agent_card(card)
        endpoint = _endpoint_from_card(card, peer_url)
        peer.update(
            {
                "name": str(card.get("name") or ""),
                "url": base_url,
                "endpoint": endpoint,
                "protocolVersion": str(card.get("protocolVersion") or ""),
            }
        )
        skills_value = card.get("skills")
        skills = skills_value if isinstance(skills_value, list) else []
        record(steps, "a2a.agent_card", "pass", f"name={card.get('name')} protocol={card.get('protocolVersion')}", {"skills": len(skills), "endpoint": endpoint})
    except SmokeFailure as exc:
        record(steps, "a2a.agent_card", "fail", str(exc))
        return steps, peer

    try:
        sent = rpc_result(_post_rpc(endpoint, "message/send", _message_params(message, "external-send-1"), timeout_seconds=timeout_seconds, message_id=10), "message/send")
        sent_task_id = task_id_from_result(sent)
        status_value = sent.get("status")
        status: dict[str, Any] = status_value if isinstance(status_value, dict) else {}
        record(steps, "a2a.message_send", "pass", f"task={sent_task_id} state={status.get('state')}", {"taskId": sent_task_id})
    except SmokeFailure as exc:
        record(steps, "a2a.message_send", "fail", str(exc))

    try:
        fetched = rpc_result(_post_rpc(endpoint, "tasks/get", {"id": sent_task_id, "historyLength": 1}, timeout_seconds=timeout_seconds, message_id=11), "tasks/get")
        fetched_id = task_id_from_result(fetched)
        if fetched_id != sent_task_id:
            raise SmokeFailure(f"tasks/get returned task {fetched_id}, expected {sent_task_id}")
        record(steps, "a2a.tasks_get", "pass", f"task={fetched_id}", {"taskId": fetched_id})
    except SmokeFailure as exc:
        record(steps, "a2a.tasks_get", "fail", str(exc))

    try:
        events = _read_sse_rpc(
            endpoint,
            "message/stream",
            _message_params(message, "external-stream-1"),
            timeout_seconds=timeout_seconds,
            message_id=20,
            max_events=max_events,
        )
        stream = summarize_stream_events(events)
        record(steps, "a2a.message_stream", "pass", f"events={stream['events']} final={stream.get('finalState')}", stream)
        record(steps, "a2a.artifact_chunks", "pass", f"chunks={stream['artifactUpdates']} indices={stream['chunkIndices']}", stream)
        record(steps, "a2a.sse_final_event", "pass", f"final={stream.get('finalState')}", stream)
    except SmokeFailure as exc:
        record(steps, "a2a.message_stream", "fail", str(exc))

    try:
        listed = rpc_result(_post_rpc(endpoint, "tasks/list", {"limit": 20}, timeout_seconds=timeout_seconds, message_id=30), "tasks/list")
        count = validate_tasks_list(listed, sent_task_id)
        record(steps, "a2a.tasks_list", "pass", f"{count} tasks listed", {"taskCount": count, "taskId": sent_task_id})
    except SmokeFailure as exc:
        record(steps, "a2a.tasks_list", "fail", str(exc))

    try:
        cancel_probe = rpc_result(_post_rpc(endpoint, "message/send", _message_params(message, "external-cancel-1"), timeout_seconds=timeout_seconds, message_id=40), "message/send cancel probe")
        cancel_task_id = task_id_from_result(cancel_probe)
        cancelled = rpc_result(_post_rpc(endpoint, "tasks/cancel", {"id": cancel_task_id}, timeout_seconds=timeout_seconds, message_id=41), "tasks/cancel")
        cancel_state = validate_cancel_result(cancelled)
        record(steps, "a2a.tasks_cancel", "pass", f"task={cancel_task_id} state={cancel_state}", {"taskId": cancel_task_id, "state": cancel_state})
    except SmokeFailure as exc:
        record(steps, "a2a.tasks_cancel", "fail", str(exc))

    return steps, peer


def build_evidence(steps: list[dict[str, Any]], *, peer: dict[str, Any], peer_type: str) -> dict[str, Any]:
    checks = checks_from_steps(steps)
    failed = [step for step in steps if step.get("status") == "fail"]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "version": app_version(),
        "generatedAt": utc_now(),
        "gitSha": git_value("rev-parse", "--short", "HEAD") or "unknown",
        "gitDirty": bool(git_value("status", "--short")),
        "peer": {
            "name": peer.get("name") or "external-peer",
            "url": peer.get("url") or "",
            "endpoint": peer.get("endpoint") or "",
            "type": peer_type,
            "protocolVersion": peer.get("protocolVersion") or "",
        },
        "checks": checks,
        "status": "FAIL" if failed or any(checks[name] != "pass" for name in REQUIRED_CHECKS) else "PASS",
        "steps": steps,
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_example_peer(timeout_seconds: int) -> tuple[str, subprocess.Popen[str]]:
    port = find_free_port()
    peer_url = f"http://127.0.0.1:{port}"
    command = [
        sys.executable,
        str(REPO_ROOT / "examples" / "a2a_interop_peer.py"),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--delay",
        "1.0",
    ]
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    process = subprocess.Popen(command, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise SmokeFailure(f"example A2A peer exited early: {stderr.strip()}")
        try:
            request_json("GET", join_url(peer_url, "/.well-known/agent-card.json"), timeout_seconds=2)
            return peer_url, process
        except SmokeFailure as exc:
            last_error = str(exc)
            time.sleep(0.2)
    process.kill()
    raise SmokeFailure(f"example A2A peer did not become ready: {last_error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run external A2A peer compatibility smoke and write structured evidence.")
    parser.add_argument("--peer-url", default="", help="External A2A peer service root or JSON-RPC endpoint. Omit to start the bundled independent-process peer.")
    parser.add_argument("--peer-type", default="independent-process", choices=("independent-process", "third-party", "adapter"), help="Evidence classification for the peer.")
    parser.add_argument("--out", type=Path, default=DEFAULT_EVIDENCE_PATH, help="Evidence JSON output path.")
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--max-events", type=int, default=50)
    parser.add_argument("--json", action="store_true", help="Print the full evidence JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    process: subprocess.Popen[str] | None = None
    try:
        peer_url = str(args.peer_url or "").strip()
        if not peer_url:
            peer_url, process = start_example_peer(args.timeout)
        steps, peer = run_smoke(peer_url, timeout_seconds=args.timeout, max_events=args.max_events, message=args.message)
        if process is not None:
            peer["type"] = "independent-process"
        evidence = build_evidence(steps, peer=peer, peer_type=str(args.peer_type or "independent-process"))
        write_evidence(args.out, evidence)
        if args.json:
            print(json.dumps(evidence, ensure_ascii=False, indent=2))
        else:
            print(f"A2A external peer evidence: {evidence['status']} ({len(evidence['steps'])} steps)")
            print(f"Wrote {args.out}")
        return 0 if evidence["status"] == "PASS" else 1
    except SmokeFailure as exc:
        steps = [{"name": "a2a.external_peer", "status": "fail", "detail": str(exc), "data": {}}]
        evidence = build_evidence(steps, peer={"url": str(args.peer_url or "")}, peer_type=str(args.peer_type or "independent-process"))
        write_evidence(args.out, evidence)
        if args.json:
            print(json.dumps(evidence, ensure_ascii=False, indent=2))
        else:
            print(f"A2A external peer evidence: FAIL ({exc})")
            print(f"Wrote {args.out}")
        return 1
    finally:
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                process.kill()


if __name__ == "__main__":
    raise SystemExit(main())
