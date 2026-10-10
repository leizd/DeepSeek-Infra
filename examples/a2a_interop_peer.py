#!/usr/bin/env python3
"""Standalone A2A peer for interop verification with DeepSeek Infra.

This is an **independent-process A2A peer** — a separate HTTP server with its
own Agent Card, JSON-RPC endpoint, and task store. It uses only the Python
standard library (``http.server``), so no third-party packages are required.

It is NOT a third-party ecosystem A2A implementation. Its purpose is to verify
that DeepSeek Infra's ``A2AClient`` can interoperate with an external,
independent A2A server that follows the same JSON-RPC + SSE contract.

Supported methods::

    GET  /.well-known/agent-card.json   → Agent Card discovery
    POST /a2a/agents/interop-peer       → message/send, message/stream,
                                          tasks/get, tasks/cancel, tasks/list

Run it as a standalone process on port 8002::

    python examples/a2a_interop_peer.py --port 8002

Then point DeepSeek Infra's A2AClient at it::

    python -c "
    from deepseek_infra.infra.agent_runtime.a2a import A2AClient
    c = A2AClient('http://127.0.0.1:8002/a2a/agents/interop-peer')
    task = c.send_message('Hello from DeepSeek Infra')
    print(task)
    "

Or use the smoke runner against a running DeepSeek Infra that has this peer
configured via ``A2A_PEERS``.
"""

from __future__ import annotations

import argparse
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

_PROTOCOL_VERSION = "0.3.0"
_AGENT_ID = "interop-peer"
_ARTIFACT_ID = "artifact_" + secrets.token_hex(6)

_LOCK = threading.RLock()
_TASKS: dict[str, dict[str, Any]] = {}
_CANCEL_EVENTS: dict[str, threading.Event] = {}
_PROCESS_DELAY = 1.5


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _status(state: str, *, message: str = "") -> dict[str, Any]:
    s: dict[str, Any] = {"state": state, "timestamp": _utc_now()}
    if message:
        s["message"] = {"role": "agent", "parts": [{"kind": "text", "text": message}], "messageId": "msg_" + secrets.token_hex(6), "kind": "message"}
    return s


def _text_from_message(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict) and str(p.get("kind") or "text") == "text"]
    return "\n".join(texts).strip()


def _public_task(task: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in task.items() if not str(k).startswith("_")}


def _create_task(params: dict[str, Any]) -> dict[str, Any]:
    raw_message = params.get("message")
    message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
    text = _text_from_message(message)
    task_id = "task_" + secrets.token_hex(12)
    incoming: dict[str, Any] = dict(message)
    incoming.setdefault("messageId", "msg_" + secrets.token_hex(6))
    incoming.setdefault("kind", "message")
    incoming["taskId"] = task_id
    task: dict[str, Any] = {
        "id": task_id,
        "contextId": str(params.get("contextId") or "ctx_" + secrets.token_hex(8)),
        "kind": "task",
        "agentId": _AGENT_ID,
        "createdAt": _utc_now(),
        "status": _status("submitted"),
        "history": [incoming],
        "artifacts": [],
        "artifactChunks": [],
        "_text": text,
    }
    with _LOCK:
        _TASKS[task_id] = task
        _CANCEL_EVENTS[task_id] = threading.Event()
    return task


def _process_task(task_id: str) -> None:
    """Simulate task execution: emit two artifact chunks then complete."""
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        text = str(task.get("_text") or "")
        task["status"] = _status("working")

    cancel_event = _CANCEL_EVENTS.get(task_id)
    artifact_id = "artifact_" + secrets.token_hex(6)

    # Chunk 1: progress
    if cancel_event is not None and cancel_event.is_set():
        _set_canceled(task_id)
        return
    _append_chunk(task_id, artifact_id, "progress", "Interop peer accepted the task.", final=False)
    time.sleep(_PROCESS_DELAY)

    # Chunk 2: answer (final)
    if cancel_event is not None and cancel_event.is_set():
        _set_canceled(task_id)
        return
    answer = f"Interop peer processed ({len(text)} chars): {text[:200]}"
    _append_chunk(task_id, artifact_id, "answer", answer, final=True)

    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        state = str((task.get("status") or {}).get("state") or "")
        if state in ("canceling", "canceled"):
            return
        task["artifacts"] = [{"artifactId": artifact_id, "name": "answer", "parts": [{"kind": "text", "text": answer}]}]
        task["status"] = _status("completed")


def _append_chunk(task_id: str, artifact_id: str, name: str, text: str, *, final: bool) -> dict[str, Any]:
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return {}
        chunks = task.setdefault("artifactChunks", [])
        chunk_index = len(chunks)
        chunk = {
            "taskId": task_id,
            "contextId": task.get("contextId"),
            "artifactId": artifact_id,
            "chunkIndex": chunk_index,
            "append": True,
            "final": final,
            "createdAt": _utc_now(),
            "artifact": {"artifactId": artifact_id, "name": name, "parts": [{"kind": "text", "text": text}]},
        }
        chunks.append(chunk)
        return chunk


def _set_canceled(task_id: str) -> None:
    with _LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            return
        state = str((task.get("status") or {}).get("state") or "")
        if state not in ("completed", "failed", "canceled"):
            task["cancelRequestedAt"] = _utc_now()
            task["status"] = _status("canceled", message="Cancellation requested by peer client.")


def _agent_card(base_url: str) -> dict[str, Any]:
    return {
        "protocolVersion": _PROTOCOL_VERSION,
        "name": "A2A Interop Peer",
        "description": "Standalone A2A peer for interop verification. Not a third-party ecosystem implementation.",
        "url": f"{base_url}/a2a/agents/{_AGENT_ID}",
        "preferredTransport": "JSONRPC",
        "version": "0.1.0",
        "capabilities": {"streaming": True, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{"id": "interop-peer.respond", "name": "A2A Interop Peer", "description": "Echoes messages with artifact streaming.", "tags": ["interop-peer"]}],
    }


def _result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _sse(data: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _handle_rpc(message: dict[str, Any]) -> dict[str, Any] | None:
    msg_id = message.get("id")
    method = str(message.get("method") or "")
    raw_params = message.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    if method == "message/send":
        task = _create_task(params)
        thread = threading.Thread(target=_process_task, args=(str(task["id"]),), daemon=True)
        thread.start()
        time.sleep(0.05)  # let the task transition to working
        with _LOCK:
            current = _TASKS.get(str(task["id"]), task)
            return _result(msg_id, _public_task(current))
    if method == "tasks/get":
        task_id = str(params.get("id") or "").strip()
        with _LOCK:
            found: dict[str, Any] | None = _TASKS.get(task_id)
        if found is None:
            return _error(msg_id, -32001, "Task not found")
        return _result(msg_id, _public_task(found))
    if method == "tasks/cancel":
        task_id = str(params.get("id") or "").strip()
        with _LOCK:
            found = _TASKS.get(task_id)
        if found is None:
            return _error(msg_id, -32001, "Task not found")
        state = str((found.get("status") or {}).get("state") or "")
        if state in ("completed", "failed", "canceled"):
            return _error(msg_id, -32002, f"Task is already {state}")
        event = _CANCEL_EVENTS.get(task_id)
        if event is not None:
            event.set()
        with _LOCK:
            found["cancelRequestedAt"] = _utc_now()
            found["status"] = _status("canceling", message="Cancellation requested.")
            return _result(msg_id, _public_task(found))
    if method == "tasks/list":
        with _LOCK:
            tasks = [_public_task(t) for t in list(_TASKS.values())[-20:]]
        return _result(msg_id, {"tasks": tasks})
    return _error(msg_id, -32601, f"Method not found: {method}")


def _stream_events(message: dict[str, Any]) -> list[bytes]:
    msg_id = message.get("id")
    method = str(message.get("method") or "")
    raw_params = message.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    task_id = ""
    if method == "message/stream":
        task = _create_task(params)
        task_id = str(task["id"])
        thread = threading.Thread(target=_process_task, args=(task_id,), daemon=True)
        thread.start()
    elif method == "tasks/resubscribe":
        task_id = str(params.get("id") or "").strip()
        if not task_id:
            return [_sse(_error(msg_id, -32602, "id is required"))]
    else:
        return [_sse(_error(msg_id, -32601, f"Method not found: {method}"))]

    events: list[bytes] = []
    with _LOCK:
        current: dict[str, Any] | None = _TASKS.get(task_id)
        if current is None:
            return [_sse(_error(msg_id, -32001, "Task not found"))]
        events.append(_sse(_result(msg_id, _public_task(current))))

    last_chunk = -1
    last_state = ""
    deadline = time.time() + 30
    while time.time() < deadline:
        with _LOCK:
            current = _TASKS.get(task_id)
            if current is None:
                break
            state = str((current.get("status") or {}).get("state") or "")
            raw_chunks = current.get("artifactChunks")
            chunks: list[Any] = raw_chunks if isinstance(raw_chunks, list) else []
        for chunk in chunks:
            ci = int(chunk.get("chunkIndex") or 0)
            if ci > last_chunk:
                last_chunk = ci
                raw_artifact = chunk.get("artifact")
                artifact: dict[str, Any] = raw_artifact if isinstance(raw_artifact, dict) else {}
                events.append(_sse(_result(msg_id, {
                    "taskId": task_id,
                    "contextId": current.get("contextId"),
                    "kind": "artifact-update",
                    "artifactId": str(chunk.get("artifactId") or ""),
                    "chunkIndex": ci,
                    "append": bool(chunk.get("append", True)),
                    "final": bool(chunk.get("final")),
                    "artifact": artifact,
                })))
        if state != last_state:
            last_state = state
            final = state in ("completed", "failed", "canceled")
            events.append(_sse(_result(msg_id, {
                "taskId": task_id,
                "contextId": current.get("contextId") if current else None,
                "kind": "status-update",
                "status": current.get("status") if current else None,
                "final": final,
            })))
            if final:
                break
        time.sleep(0.1)
    return events


class _PeerHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send_json(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        server_address = cast(tuple[str, int], self.server.server_address)
        host = str(server_address[0])
        if host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        base = f"http://{host}:{server_address[1]}"
        if self.path == "/.well-known/agent-card.json":
            self._send_json(200, json.dumps(_agent_card(base), ensure_ascii=False, indent=2).encode("utf-8"))
            return
        if self.path == "/a2a/agents":
            self._send_json(200, json.dumps({"agents": [_agent_card(base)]}, ensure_ascii=False).encode("utf-8"))
            return
        self._send_json(404, json.dumps({"error": "not found"}).encode("utf-8"))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            message = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}).encode("utf-8"))
            return
        accept = str(self.headers.get("Accept") or "")
        method = str(message.get("method") or "") if isinstance(message, dict) else ""
        if method in ("message/stream", "tasks/resubscribe") or "text/event-stream" in accept:
            events = _stream_events(message if isinstance(message, dict) else {})
            body = b"".join(events)
            self._send_json(200, body, content_type="text/event-stream")
            return
        if isinstance(message, dict):
            response = _handle_rpc(message)
            if response is not None:
                self._send_json(200, json.dumps(response, ensure_ascii=False).encode("utf-8"))
                return
        self._send_json(400, json.dumps({"error": "bad request"}).encode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Standalone A2A interop peer server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between progress and answer chunks (allows cancel testing)")
    args = parser.parse_args(argv)
    global _PROCESS_DELAY
    _PROCESS_DELAY = max(0.0, float(args.delay))
    server = ThreadingHTTPServer((args.host, args.port), _PeerHandler)
    print(f"A2A interop peer starting on http://{args.host}:{args.port}", flush=True)
    print(f"Agent Card: http://{args.host}:{args.port}/.well-known/agent-card.json", flush=True)
    print(f"JSON-RPC:   http://{args.host}:{args.port}/a2a/agents/{_AGENT_ID}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
