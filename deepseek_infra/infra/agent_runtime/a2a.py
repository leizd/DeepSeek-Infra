"""A2A-style Agent Mesh: expose local agents to other agents, and delegate out.

MCP connects an agent to *tools*; A2A connects an agent to *other agents*. This
module gives every local Seek/agent role an **Agent Card** (discovery) plus an
A2A task lifecycle over JSON-RPC 2.0::

    message/send      submit a message -> Task (executed in the background)
    message/stream    same, but stream status updates over SSE
    tasks/get         poll a task (optional historyLength)
    tasks/cancel      request cancellation
    tasks/list        local convenience: recent tasks
    agent/getAuthenticatedExtendedCard

Tasks run through the existing gateway (``call_deepseek``) with the role's
capability slice and system profile, so A2A peers get the same policy-gated
tool surface as internal workers - never more. Outbound delegation to external
A2A agents goes through :class:`A2AClient` against ``A2A_PEERS``.

Task records persist as JSON under ``.a2a/`` (credentials are never stored);
non-terminal tasks found on disk after a restart are reported ``failed``.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Generator

from deepseek_infra.core.config import (
    APP_VERSION,
    A2A_DEFAULT_AGENT,
    A2A_ENABLED,
    A2A_HISTORY_LIMIT,
    A2A_MAX_TASKS,
    A2A_PEERS,
    A2A_TASKS_DIR,
    DEFAULT_MODEL,
    settings,
)
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.agent_runtime.multi_agent import AGENT_PROFILES, agent_model_for, model_supports_thinking
from deepseek_infra.infra.gateway import deepseek_client
from deepseek_infra.infra.native_runtime.authority import assert_python_writer_allowed
from deepseek_infra.infra.observability.observability import finish_trace, start_span, start_trace
from deepseek_infra.infra.tool_runtime.tool_policy import capability_tools

logger = logging.getLogger("deepseek_infra.a2a")

A2A_PROTOCOL_VERSION = "0.3.0"

# A2A TaskState values used by this mesh (subset of the spec's enum).
SUBMITTED = "submitted"
WORKING = "working"
COMPLETED = "completed"
FAILED = "failed"
CANCELING = "canceling"
CANCELED = "canceled"
TERMINAL_STATES = {COMPLETED, FAILED, CANCELED}

# JSON-RPC + A2A-specific error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002

ORCHESTRATOR_ID = "orchestrator"
ORCHESTRATOR_PROFILE = {
    "name": "DeepSeek Infra Orchestrator",
    "system": "You are DeepSeek Infra's general-purpose assistant Agent with the full local tool surface.",
}

_STREAM_POLL_SECONDS = 15.0


def known_agent_ids() -> list[str]:
    return [ORCHESTRATOR_ID, *AGENT_PROFILES.keys()]


def _agent_profile(agent_id: str) -> dict[str, str]:
    if agent_id == ORCHESTRATOR_ID:
        return ORCHESTRATOR_PROFILE
    profile = AGENT_PROFILES.get(agent_id)
    if profile is None:
        raise AppError(f"Unknown agent: {agent_id}", code=ErrorCode.NOT_FOUND, status=404)
    return profile


def resolve_agent_id(value: str) -> str:
    agent_id = str(value or "").strip() or ORCHESTRATOR_ID
    if agent_id not in known_agent_ids():
        raise AppError(f"Unknown agent: {agent_id}", code=ErrorCode.NOT_FOUND, status=404)
    return agent_id


# --- Agent Cards ------------------------------------------------------------------

def agent_card(agent_id: str, *, base_url: str = "") -> dict[str, Any]:
    """One A2A Agent Card: identity, endpoint, capabilities and skills."""
    resolved = resolve_agent_id(agent_id)
    profile = _agent_profile(resolved)
    base = str(base_url or "").rstrip("/") or "http://127.0.0.1:8000"
    tools = capability_tools(resolved) if resolved != ORCHESTRATOR_ID else ["full_tool_surface"]
    skills = [
        {
            "id": f"{resolved}.respond",
            "name": str(profile.get("name") or resolved),
            "description": str(profile.get("system") or ""),
            "tags": [resolved, *tools] if tools else [resolved],
        }
    ]
    return {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": str(profile.get("name") or resolved),
        "description": str(profile.get("system") or ""),
        "url": f"{base}/a2a/agents/{resolved}",
        "preferredTransport": "JSONRPC",
        "version": APP_VERSION,
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
    }


def agent_cards(*, base_url: str = "") -> list[dict[str, Any]]:
    return [agent_card(agent_id, base_url=base_url) for agent_id in known_agent_ids()]


# --- Task store --------------------------------------------------------------------

_TASK_LOCK = threading.RLock()
_TASKS: dict[str, dict[str, Any]] = {}
_TASK_CONDITIONS: dict[str, threading.Condition] = {}
_TASK_CANCEL_EVENTS: dict[str, threading.Event] = {}
_STREAM_DISCONNECTS_TOTAL = 0


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _make_task_id() -> str:
    return "task_" + secrets.token_hex(12)


def _condition_for(task_id: str) -> threading.Condition:
    with _TASK_LOCK:
        condition = _TASK_CONDITIONS.get(task_id)
        if condition is None:
            condition = threading.Condition()
            _TASK_CONDITIONS[task_id] = condition
        return condition


def _notify_task(task_id: str) -> None:
    condition = _condition_for(task_id)
    with condition:
        condition.notify_all()


def _task_path(task_id: str) -> Any:
    return A2A_TASKS_DIR / f"{task_id}.json"


def _persist_task(task: dict[str, Any]) -> None:
    """Best-effort JSON snapshot; the in-memory record stays authoritative."""
    assert_python_writer_allowed("a2a_task_lifecycle")
    try:
        A2A_TASKS_DIR.mkdir(parents=True, exist_ok=True)
        _task_path(str(task.get("id") or "")).write_text(
            json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("a2a_task_persist_failed: %s", exc)


def _load_task_from_disk(task_id: str) -> dict[str, Any] | None:
    path = _task_path(task_id)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    # A non-terminal task on disk means the process died mid-flight.
    state = str(((data.get("status") or {}) if isinstance(data.get("status"), dict) else {}).get("state") or "")
    if state not in TERMINAL_STATES:
        data["status"] = _status(FAILED, message="Service restarted before this task finished; please submit it again.")
    return data


def _evict_old_tasks() -> None:
    """Cap the in-memory store: drop the oldest *terminal* tasks beyond the limit."""
    with _TASK_LOCK:
        if len(_TASKS) <= A2A_MAX_TASKS:
            return
        terminal = [
            task_id
            for task_id, task in sorted(_TASKS.items(), key=lambda item: str(item[1].get("createdAt") or ""))
            if str((task.get("status") or {}).get("state") or "") in TERMINAL_STATES
        ]
        for task_id in terminal[: max(0, len(_TASKS) - A2A_MAX_TASKS)]:
            _TASKS.pop(task_id, None)
            _TASK_CONDITIONS.pop(task_id, None)
            _TASK_CANCEL_EVENTS.pop(task_id, None)


def _status(state: str, *, message: str = "") -> dict[str, Any]:
    status: dict[str, Any] = {"state": state, "timestamp": _utc_timestamp()}
    if message:
        status["message"] = _agent_text_message(message)
    return status


def _agent_text_message(text: str) -> dict[str, Any]:
    return {
        "role": "agent",
        "parts": [{"kind": "text", "text": str(text or "")}],
        "messageId": "msg_" + secrets.token_hex(8),
        "kind": "message",
    }


def get_task(task_id: str) -> dict[str, Any]:
    value = str(task_id or "").strip()
    with _TASK_LOCK:
        task = _TASKS.get(value)
    if task is None:
        task = _load_task_from_disk(value)
        if task is not None:
            with _TASK_LOCK:
                _TASKS.setdefault(value, task)
    if task is None:
        raise AppError("Task not found", code=ErrorCode.NOT_FOUND, status=404)
    return task


def _update_task(task_id: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    assert_python_writer_allowed("a2a_task_lifecycle")
    with _TASK_LOCK:
        task = _TASKS.get(task_id)
        if task is None:
            raise AppError("Task not found", code=ErrorCode.NOT_FOUND, status=404)
        mutate(task)
        snapshot = json.loads(json.dumps(task, ensure_ascii=False))
    _persist_task(snapshot)
    _notify_task(task_id)
    return snapshot


def public_task(task: dict[str, Any], *, history_length: int | None = None) -> dict[str, Any]:
    result = {key: value for key, value in task.items() if not str(key).startswith("_")}
    history = result.get("history")
    if isinstance(history, list):
        limit = A2A_HISTORY_LIMIT if history_length is None else max(0, int(history_length))
        result["history"] = history[-limit:] if limit else []
    return result


def _append_artifact_chunk(
    task_id: str,
    *,
    artifact_id: str,
    name: str,
    text: str,
    final: bool = False,
    append: bool = True,
    skip_if_canceling: bool = False,
) -> dict[str, Any]:
    """Append one resumable artifact chunk and notify SSE subscribers."""
    appended = False

    def mutate(task: dict[str, Any]) -> None:
        nonlocal appended
        if skip_if_canceling and str((task.get("status") or {}).get("state") or "") == CANCELING:
            return
        chunks = task.setdefault("artifactChunks", [])
        if not isinstance(chunks, list):
            chunks = []
            task["artifactChunks"] = chunks
        chunk_index = len(chunks)
        artifact = {
            "artifactId": artifact_id,
            "name": name,
            "parts": [{"kind": "text", "text": str(text or "")}],
        }
        chunks.append(
            {
                "taskId": task_id,
                "contextId": task.get("contextId"),
                "artifactId": artifact_id,
                "chunkIndex": chunk_index,
                "append": bool(append),
                "final": bool(final),
                "createdAt": _utc_timestamp(),
                "artifact": artifact,
            }
        )
        appended = True

    snapshot = _update_task(task_id, mutate)
    if not appended:
        return {}
    chunks = snapshot.get("artifactChunks") if isinstance(snapshot.get("artifactChunks"), list) else []
    return chunks[-1] if chunks else {}


def _artifact_update_from_chunk(task: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
    artifact_value = chunk.get("artifact")
    artifact: dict[str, Any] = artifact_value if isinstance(artifact_value, dict) else {}
    return {
        "taskId": str(task.get("id") or chunk.get("taskId") or ""),
        "contextId": task.get("contextId") or chunk.get("contextId"),
        "artifact": artifact,
        "artifactId": str(chunk.get("artifactId") or artifact.get("artifactId") or ""),
        "chunkIndex": int(chunk.get("chunkIndex") or 0),
        "append": bool(chunk.get("append", True)),
        "final": bool(chunk.get("final")),
        "kind": "artifact-update",
    }


def list_tasks(limit: int = 20) -> list[dict[str, Any]]:
    capped = max(1, min(int(limit or 20), 200))
    with _TASK_LOCK:
        tasks = sorted(_TASKS.values(), key=lambda item: str(item.get("createdAt") or ""), reverse=True)
        return [public_task(task) for task in tasks[:capped]]


# --- Message handling / execution ---------------------------------------------------

def _text_from_message(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts")
    if not isinstance(parts, list):
        return ""
    texts = []
    for part in parts:
        if isinstance(part, dict) and str(part.get("kind") or part.get("type") or "") == "text":
            text = str(part.get("text") or "")
            if text:
                texts.append(text)
    return "\n".join(texts).strip()


def _execution_payload(agent_id: str, text: str) -> dict[str, Any]:
    """Build the gateway payload for one A2A task (capability-scoped, no streaming)."""
    profile = _agent_profile(agent_id)
    tools = capability_tools(agent_id) if agent_id != ORCHESTRATOR_ID else None
    model = agent_model_for(agent_id) if agent_id != ORCHESTRATOR_ID else DEFAULT_MODEL
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "systemPrompt": str(profile.get("system") or ""),
        "capability": agent_id if agent_id != ORCHESTRATOR_ID else "full",
        "thinkingEnabled": model_supports_thinking(model),
        "memoryEnabled": False,
    }
    if settings.deepseek_api_key:
        payload["apiKey"] = settings.deepseek_api_key
    if tools is not None:
        payload["allowedTools"] = tools
        payload["toolsEnabled"] = bool(tools)
        payload["searchEnabled"] = "web_search" in tools
        payload["searchMode"] = "auto" if "web_search" in tools else "off"
    return payload


def _execute_task(task_id: str, agent_id: str, text: str) -> None:
    cancel_event = _TASK_CANCEL_EVENTS.get(task_id)
    trace_id = start_trace(
        kind="a2a",
        title=text[:120],
        metadata={"taskId": task_id, "agentId": agent_id, "protocol": "a2a"},
    )
    task_span = start_span(
        trace_id,
        name="A2A task",
        kind="a2a_task",
        input_data={"taskId": task_id, "agentId": agent_id, "textChars": len(text)},
    )
    answer_artifact_id = "artifact_" + secrets.token_hex(8)
    progress_artifact_id = "artifact_" + secrets.token_hex(8)
    discarded_result = False

    def set_working(task: dict[str, Any]) -> None:
        task["_traceId"] = trace_id
        task["_answerArtifactId"] = answer_artifact_id
        task["_progressArtifactId"] = progress_artifact_id
        if str((task.get("status") or {}).get("state") or "") != CANCELING:
            task["status"] = _status(WORKING)

    try:
        _update_task(task_id, set_working)
        if cancel_event is not None and cancel_event.is_set():
            raise deepseek_client.RequestCancelled()
        _append_artifact_chunk(
            task_id,
            artifact_id=progress_artifact_id,
            name="progress",
            text="A2A worker accepted the task.",
            final=False,
        )
        # Cancellation is best-effort: checked before and after the upstream call.
        # A cancel that lands mid-call lets the call finish and discards the result.
        payload = _execution_payload(agent_id, text)
        if trace_id:
            payload["traceId"] = trace_id
        try:
            result = deepseek_client.call_deepseek(payload, parent_span_id=task_span.span_id)
        except TypeError:
            result = deepseek_client.call_deepseek(payload)
        if cancel_event is not None and cancel_event.is_set():
            discarded_result = True
            raise deepseek_client.RequestCancelled()
        content = str(result.get("content") or "").strip() or "(empty response)"
        final_chunk = _append_artifact_chunk(
            task_id,
            artifact_id=answer_artifact_id,
            name="answer",
            text=content,
            final=True,
            skip_if_canceling=True,
        )
        if not final_chunk or (cancel_event is not None and cancel_event.is_set()):
            discarded_result = True
            raise deepseek_client.RequestCancelled()

        def complete(task: dict[str, Any]) -> None:
            if str((task.get("status") or {}).get("state") or "") in TERMINAL_STATES:
                return
            task["artifacts"] = [
                {
                    "artifactId": answer_artifact_id,
                    "name": "answer",
                    "parts": [{"kind": "text", "text": content}],
                }
            ]
            history = task.setdefault("history", [])
            if isinstance(history, list):
                history.append(_agent_text_message(content))
            task["_usage"] = result.get("usage") or {}
            task["status"] = _status(COMPLETED)

        completed = _update_task(task_id, complete)
        task_span.finish(
            status="ok",
            output_data={"state": COMPLETED},
            usage=result.get("usage") if isinstance(result.get("usage"), dict) else {},
            diagnostics={"artifactChunks": len(completed.get("artifactChunks") or []), "discardedResult": False},
        )
        finish_trace(trace_id, status="completed", metadata={"taskId": task_id, "agentId": agent_id})
    except deepseek_client.RequestCancelled:
        def cancel(task: dict[str, Any]) -> None:
            if str((task.get("status") or {}).get("state") or "") not in TERMINAL_STATES:
                task["status"] = _status(CANCELED)

        cancelled = _update_task(task_id, cancel)
        task_span.finish(
            status="canceled",
            output_data={"state": CANCELED},
            diagnostics={
                "artifactChunks": len(cancelled.get("artifactChunks") or []),
                "discardedResult": discarded_result,
                "cancelRequestedAt": cancelled.get("cancelRequestedAt") or "",
            },
        )
        finish_trace(
            trace_id,
            status="canceled",
            metadata={"taskId": task_id, "agentId": agent_id, "discardedResult": discarded_result},
        )
    except AppError as exc:
        _fail_task(task_id, str(exc))
        task_span.finish(status="error", error=str(exc), diagnostics={"discardedResult": discarded_result})
        finish_trace(trace_id, status="error", metadata={"taskId": task_id, "agentId": agent_id}, error=str(exc))
    except Exception as exc:  # pragma: no cover - defensive boundary
        logger.exception("a2a_task_failed", extra={"taskId": task_id})
        _fail_task(task_id, f"Task execution failed: {exc}")
        task_span.finish(status="error", error=str(exc), diagnostics={"discardedResult": discarded_result})
        finish_trace(trace_id, status="error", metadata={"taskId": task_id, "agentId": agent_id}, error=str(exc))


def _fail_task(task_id: str, message: str) -> None:
    def fail(task: dict[str, Any]) -> None:
        if str((task.get("status") or {}).get("state") or "") not in TERMINAL_STATES:
            task["status"] = _status(FAILED, message=message)

    try:
        _update_task(task_id, fail)
    except AppError:
        pass


def submit_message(params: dict[str, Any], *, agent_id: str) -> dict[str, Any]:
    """``message/send``: create a task and execute it in the background."""
    assert_python_writer_allowed("a2a_task_lifecycle")
    resolved = resolve_agent_id(agent_id)
    message = params.get("message")
    text = _text_from_message(message)
    if not text:
        raise AppError("message.parts must contain non-empty text", code=ErrorCode.INVALID_PAYLOAD)
    task_id = _make_task_id()
    now = _utc_timestamp()
    incoming = dict(message) if isinstance(message, dict) else {}
    incoming.setdefault("messageId", "msg_" + secrets.token_hex(8))
    incoming.setdefault("kind", "message")
    incoming["taskId"] = task_id
    task = {
        "id": task_id,
        "contextId": str(params.get("contextId") or incoming.get("contextId") or "ctx_" + secrets.token_hex(8)),
        "kind": "task",
        "agentId": resolved,
        "createdAt": now,
        "status": _status(SUBMITTED),
        "history": [incoming],
        "artifacts": [],
        "artifactChunks": [],
    }
    with _TASK_LOCK:
        _TASKS[task_id] = task
        _TASK_CANCEL_EVENTS[task_id] = threading.Event()
    _evict_old_tasks()
    _persist_task(task)
    thread = threading.Thread(target=_execute_task, args=(task_id, resolved, text), name=f"a2a-{task_id}", daemon=True)
    thread.start()
    return get_task(task_id)


def cancel_task(task_id: str) -> dict[str, Any]:
    assert_python_writer_allowed("a2a_task_lifecycle")
    task = get_task(task_id)
    state = str((task.get("status") or {}).get("state") or "")
    if state in TERMINAL_STATES:
        raise _A2AError(TASK_NOT_CANCELABLE, f"Task is already {state}")
    event = _TASK_CANCEL_EVENTS.get(str(task.get("id")))
    if event is not None:
        event.set()

    def cancel(record: dict[str, Any]) -> None:
        if str((record.get("status") or {}).get("state") or "") not in TERMINAL_STATES:
            record["cancelRequestedAt"] = record.get("cancelRequestedAt") or _utc_timestamp()
            record["status"] = _status(CANCELING, message="Cancellation requested; pending upstream boundary.")

    return _update_task(str(task.get("id")), cancel)


# --- JSON-RPC dispatch ----------------------------------------------------------------

class _A2AError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _result(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def handle_a2a_message(message: Any, *, agent_id: str = "", base_url: str = "") -> dict[str, Any] | None:
    """Dispatch one A2A JSON-RPC message (``message/stream`` is routed separately)."""
    if not isinstance(message, dict):
        return _error(None, INVALID_REQUEST, "Request must be a JSON object")
    message_id = message.get("id")
    if message.get("jsonrpc") != "2.0":
        return _error(message_id, INVALID_REQUEST, "jsonrpc must be '2.0'")
    method = str(message.get("method") or "")
    raw_params = message.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    try:
        if method == "message/send":
            task = submit_message(params, agent_id=agent_id)
            return _result(message_id, public_task(task))
        if method == "tasks/get":
            task_id = str(params.get("id") or "").strip()
            if not task_id:
                return _error(message_id, INVALID_PARAMS, "id is required")
            history_length = params.get("historyLength")
            length = int(history_length) if isinstance(history_length, int) else None
            return _result(message_id, public_task(get_task(task_id), history_length=length))
        if method == "tasks/cancel":
            task_id = str(params.get("id") or "").strip()
            if not task_id:
                return _error(message_id, INVALID_PARAMS, "id is required")
            return _result(message_id, public_task(cancel_task(task_id)))
        if method == "tasks/list":
            return _result(message_id, {"tasks": list_tasks(int(params.get("limit") or 20))})
        if method == "agent/getAuthenticatedExtendedCard":
            return _result(message_id, agent_card(agent_id or ORCHESTRATOR_ID, base_url=base_url))
        return _error(message_id, METHOD_NOT_FOUND, f"Method not found: {method}")
    except _A2AError as exc:
        return _error(message_id, exc.code, str(exc))
    except AppError as exc:
        if exc.code is ErrorCode.NOT_FOUND:
            return _error(message_id, TASK_NOT_FOUND, str(exc))
        return _error(message_id, INVALID_PARAMS, str(exc))
    except Exception:  # pragma: no cover - defensive boundary
        logger.exception("a2a_method_failed", extra={"method": method})
        return _error(message_id, INTERNAL_ERROR, "Internal error")


def is_stream_request(message: Any) -> bool:
    return isinstance(message, dict) and str(message.get("method") or "") in {"message/stream", "tasks/resubscribe"}


def _sse(data: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def _record_stream_disconnect() -> None:
    global _STREAM_DISCONNECTS_TOTAL
    with _TASK_LOCK:
        _STREAM_DISCONNECTS_TOTAL += 1


def _chunk_index(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _task_chunks_after(task: dict[str, Any], after_chunk_index: int) -> list[dict[str, Any]]:
    chunks_value = task.get("artifactChunks")
    chunks: list[Any] = chunks_value if isinstance(chunks_value, list) else []
    result = []
    for chunk in chunks:
        if isinstance(chunk, dict) and _chunk_index(chunk.get("chunkIndex")) > after_chunk_index:
            result.append(chunk)
    return result


def _stream_task_events(
    message_id: Any,
    task_id: str,
    *,
    after_chunk_index: int = -1,
    include_initial: bool = True,
) -> Generator[bytes, None, None]:
    try:
        task = get_task(task_id)
        if include_initial:
            yield _sse(_result(message_id, public_task(task)))
        last_state = str((task.get("status") or {}).get("state") or "")
        last_chunk_index = after_chunk_index
        while True:
            current = get_task(task_id)
            for chunk in _task_chunks_after(current, last_chunk_index):
                last_chunk_index = _chunk_index(chunk.get("chunkIndex"))
                yield _sse(_result(message_id, _artifact_update_from_chunk(current, chunk)))
            state = str((current.get("status") or {}).get("state") or "")
            if state != last_state or state in TERMINAL_STATES:
                last_state = state
                final = state in TERMINAL_STATES
                yield _sse(
                    _result(
                        message_id,
                        {
                            "taskId": task_id,
                            "contextId": current.get("contextId"),
                            "status": current.get("status"),
                            "kind": "status-update",
                            "final": final,
                        },
                    )
                )
                if final:
                    return
            condition = _condition_for(task_id)
            with condition:
                condition.wait(timeout=_STREAM_POLL_SECONDS)
    except GeneratorExit:
        _record_stream_disconnect()
        raise


def _stream_resubscribe_events(message: dict[str, Any]) -> Generator[bytes, None, None]:
    message_id = message.get("id")
    raw_params = message.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    task_id = str(params.get("id") or "").strip()
    if not task_id:
        yield _sse(_error(message_id, INVALID_PARAMS, "id is required"))
        return
    after_chunk_index = _chunk_index(params.get("afterChunkIndex"))
    try:
        yield from _stream_task_events(message_id, task_id, after_chunk_index=after_chunk_index, include_initial=True)
    except AppError as exc:
        if exc.code is ErrorCode.NOT_FOUND:
            yield _sse(_error(message_id, TASK_NOT_FOUND, str(exc)))
        else:
            yield _sse(_error(message_id, INVALID_PARAMS, str(exc)))


def stream_message_events(message: dict[str, Any], *, agent_id: str = "") -> Generator[bytes, None, None]:
    """Stream ``message/stream`` or ``tasks/resubscribe`` JSON-RPC responses as SSE."""
    message_id = message.get("id")
    method = str(message.get("method") or "")
    if method == "tasks/resubscribe":
        yield from _stream_resubscribe_events(message)
        return
    raw_params = message.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
    try:
        task = submit_message(params, agent_id=agent_id)
    except AppError as exc:
        yield _sse(_error(message_id, INVALID_PARAMS, str(exc)))
        return
    task_id = str(task.get("id") or "")
    yield from _stream_task_events(message_id, task_id, after_chunk_index=-1, include_initial=True)


# --- Outbound delegation (A2A client) ---------------------------------------------------

class A2AClient:
    """Minimal JSON-RPC client for delegating a task to an external A2A agent."""

    def __init__(self, base_url: str, *, timeout_seconds: int = 60, auth_token: str = "") -> None:
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.auth_token = str(auth_token or "")
        self._next_id = 0

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": accept}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        self._next_id += 1
        trace_id = start_trace(
            kind="a2a_peer",
            title=method,
            metadata={"method": method, "baseUrl": self.base_url, "protocol": "a2a"},
        )
        span = start_span(
            trace_id,
            name="A2A peer call",
            kind="a2a_peer_call",
            input_data={"method": method, "baseUrl": self.base_url},
        )
        request = urllib.request.Request(
            self.base_url,
            data=json.dumps({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}, ensure_ascii=False).encode("utf-8"),
            headers=self._headers("application/json"),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            span.finish(status="error", error=str(exc), diagnostics={"method": method})
            finish_trace(trace_id, status="error", metadata={"method": method, "baseUrl": self.base_url}, error=str(exc))
            raise AppError(f"A2A peer is unreachable: {exc}", code=ErrorCode.UPSTREAM_FAILURE, status=502) from exc
        if not isinstance(parsed, dict):
            span.finish(status="error", error="invalid response", diagnostics={"method": method})
            finish_trace(trace_id, status="error", metadata={"method": method, "baseUrl": self.base_url}, error="invalid response")
            raise AppError("A2A peer returned an invalid response", code=ErrorCode.UPSTREAM_FAILURE, status=502)
        error = parsed.get("error")
        if isinstance(error, dict):
            message = f"A2A peer error {error.get('code')}: {error.get('message')}"
            span.finish(status="error", error=message, diagnostics={"method": method, "errorCode": error.get("code")})
            finish_trace(trace_id, status="error", metadata={"method": method, "baseUrl": self.base_url}, error=message)
            raise AppError(f"A2A peer error {error.get('code')}: {error.get('message')}", code=ErrorCode.UPSTREAM_FAILURE, status=502)
        span.finish(status="ok", output_data={"method": method})
        finish_trace(trace_id, status="completed", metadata={"method": method, "baseUrl": self.base_url})
        return parsed.get("result")

    def _stream_rpc(self, method: str, params: dict[str, Any]) -> Generator[dict[str, Any], None, None]:
        self._next_id += 1
        trace_id = start_trace(
            kind="a2a_peer",
            title=method,
            metadata={"method": method, "baseUrl": self.base_url, "protocol": "a2a", "stream": True},
        )
        span = start_span(
            trace_id,
            name="A2A peer stream",
            kind="a2a_peer_call",
            input_data={"method": method, "baseUrl": self.base_url, "stream": True},
        )
        request = urllib.request.Request(
            self.base_url,
            data=json.dumps({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}, ensure_ascii=False).encode("utf-8"),
            headers=self._headers("text/event-stream"),
            method="POST",
        )
        event_count = 0
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    parsed = json.loads(line[len("data: ") :])
                    if not isinstance(parsed, dict):
                        raise AppError("A2A peer returned an invalid stream event", code=ErrorCode.UPSTREAM_FAILURE, status=502)
                    error = parsed.get("error")
                    if isinstance(error, dict):
                        raise AppError(
                            f"A2A peer error {error.get('code')}: {error.get('message')}",
                            code=ErrorCode.UPSTREAM_FAILURE,
                            status=502,
                        )
                    event_count += 1
                    yield parsed
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, UnicodeDecodeError, AppError) as exc:
            span.finish(status="error", error=str(exc), diagnostics={"method": method, "events": event_count})
            finish_trace(trace_id, status="error", metadata={"method": method, "baseUrl": self.base_url, "events": event_count}, error=str(exc))
            raise
        span.finish(status="ok", output_data={"method": method, "events": event_count})
        finish_trace(trace_id, status="completed", metadata={"method": method, "baseUrl": self.base_url, "events": event_count})

    def send_message(self, text: str, *, context_id: str = "") -> dict[str, Any]:
        params: dict[str, Any] = {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": str(text or "")}],
                "messageId": "msg_" + secrets.token_hex(8),
                "kind": "message",
            }
        }
        if context_id:
            params["contextId"] = context_id
        result = self._rpc("message/send", params)
        return result if isinstance(result, dict) else {}

    def get_task(self, task_id: str) -> dict[str, Any]:
        result = self._rpc("tasks/get", {"id": str(task_id or "")})
        return result if isinstance(result, dict) else {}

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        result = self._rpc("tasks/cancel", {"id": str(task_id or "")})
        return result if isinstance(result, dict) else {}

    def message_stream(self, text: str, *, context_id: str = "") -> Generator[dict[str, Any], None, None]:
        params: dict[str, Any] = {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": str(text or "")}],
                "messageId": "msg_" + secrets.token_hex(8),
                "kind": "message",
            }
        }
        if context_id:
            params["contextId"] = context_id
        yield from self._stream_rpc("message/stream", params)

    def resubscribe(self, task_id: str, *, after_chunk_index: int = -1) -> Generator[dict[str, Any], None, None]:
        yield from self._stream_rpc(
            "tasks/resubscribe",
            {"id": str(task_id or ""), "afterChunkIndex": int(after_chunk_index)},
        )


def peer_clients() -> list[A2AClient]:
    return [A2AClient(url) for url in A2A_PEERS]


# --- Status ------------------------------------------------------------------------------

def a2a_status() -> dict[str, Any]:
    with _TASK_LOCK:
        states: dict[str, int] = {}
        for task in _TASKS.values():
            state = str((task.get("status") or {}).get("state") or "unknown")
            states[state] = states.get(state, 0) + 1
        active_tasks = sum(count for state, count in states.items() if state not in TERMINAL_STATES)
        stream_disconnects = _STREAM_DISCONNECTS_TOTAL
    return {
        "enabled": A2A_ENABLED,
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "agents": known_agent_ids(),
        "defaultAgent": A2A_DEFAULT_AGENT,
        "endpoint": "/a2a",
        "agentCardPath": "/.well-known/agent-card.json",
        "tasksByState": states,
        "activeTasks": active_tasks,
        "streamDisconnectsTotal": stream_disconnects,
        "peers": len(A2A_PEERS),
    }
