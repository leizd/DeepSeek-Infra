"""Offline admission oracle for native Agent runtime updates.

The public Rust edge and the Go driver apply the same rules to the contract in
``rust/crates/deepseek-policy/src/agent_runtime_admission_v1.json``. This module
does not start a provider call, write a run, or advance an execution epoch.
``continue_retained_tools`` runs calls already retained on that effect through
the tool dispatcher. A supplied workspace root is used for that call only.
It does not mint an operation or append ``CONFIRMED``. ``resume_stored_tools``
copies ``toolResults`` already on a public event for resume and rerun. It does
not call the tool. A request with no stored result stays awaiting tools.
``retained_driver_tool_results`` returns those stored results to the driver.
It does not call the tool. A missing result stays pending.
``next_model_turn`` reads those stored results into the next provider body.
It does not call the tool. A missing result does not start that turn.
``published_model_answer`` publishes a saved follow-up that has no further
tool calls. A missing answer does not publish.
``settle_published_answer`` admits settlement of an answer already published
on that request. A missing answer or a larger presented epoch does not settle
and does not advance the installed epoch.
``published_dependency_output`` copies a direct answer or a published
follow-up. ``dependency_provider_body`` places that output into the worker
and synthesis bodies through the existing message builders. A missing answer
does not build those bodies.
``execution_send_lease`` admits the provider send only while that lease is
still ahead of the clock. A deadline that has arrived does not start the send.
``admitted_provider_send`` forwards the shaped body only for that live lease.
``provider_exchange_unknown`` classifies a send that returned no receipt.
A timeout, a missing object, and an absent log do not store an effect.
``follow_up_exchange`` applies that lease to the second send. A lost receipt
does not store the follow-up and does not mark the confirmed row unknown.
``workspace_tool_lease`` passes the live tool flag only for that same lease.
A deadline that has arrived or a non-positive clock passes the expired flag.
``agent_node_send_deadline`` returns the minimum of the grant expiry, the
claim lease, and the writer lease. An elapsed or non-positive minimum is
refused by ``execution_send_lease`` before a provider send or a live flag.
``execution_send_budget`` arms the provider timeout only for the remaining
whole seconds of that live minimum. An elapsed or non-positive remainder
does not arm that timeout.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

_INT64_MAX = (1 << 63) - 1
_UINT64_MAX = (1 << 64) - 1

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "rust/crates/deepseek-policy/src/agent_runtime_admission_v1.json"
)


@lru_cache(maxsize=1)
def contract() -> dict[str, Any]:
    loaded = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
    if loaded.get("schema") != "agent-runtime-admission-v1" or loaded.get("schemaVersion") != 1:
        raise ValueError("agent runtime admission contract is invalid")
    return loaded


def vault_revision_allowed(sealed_max: int | None, requested: int) -> bool:
    """First seal may use the metadata revision. The next seal is exactly +1."""
    if not isinstance(requested, int) or isinstance(requested, bool) or requested <= 0:
        return False
    if sealed_max is None:
        return True
    if not isinstance(sealed_max, int) or isinstance(sealed_max, bool) or sealed_max <= 0:
        return False
    return requested == sealed_max or requested == sealed_max + 1


def current_runtime_revision(events: list[Mapping[str, Any]]) -> int:
    current = 1
    for event in events:
        if event.get("type") != "runtime_payload":
            continue
        revision = event.get("runtimeRevision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision != current + 1:
            raise ValueError("agent runtime revision history is corrupt")
        current = revision
    return current


_EFFECT_DONE = "done"
_EFFECT_WAIT = "wait"
_EFFECT_OPEN = "open"
_EFFECT_STICKY = "sticky"
_SETTLED_STATES = {"CONFIRMED", "SETTLED_FAILURE"}
_NONE_STATES = {"NONE", "none"}
_WAIT_STATES = {"AWAITING_TOOLS", "awaiting_tools"}


def _remember(nodes: dict[str, str], node: str, kind: str) -> None:
    if nodes.get(node) == _EFFECT_STICKY and kind != _EFFECT_DONE:
        return
    nodes[node] = kind


def _node_id(event: Mapping[str, Any]) -> str:
    phase = event.get("phase")
    if isinstance(phase, str) and phase:
        return phase
    node_id = event.get("nodeId")
    if isinstance(node_id, str) and node_id:
        return node_id
    output = event.get("output")
    if isinstance(output, Mapping):
        identity = output.get("id")
        if isinstance(identity, str) and identity:
            return identity
    return ""


def _apply_effect_state(nodes: dict[str, str], node: str, state: object) -> None:
    if not isinstance(state, str):
        _remember(nodes, node, _EFFECT_STICKY)
        return
    if state in _WAIT_STATES:
        _remember(nodes, node, _EFFECT_WAIT)
        return
    if state in _SETTLED_STATES:
        _remember(nodes, node, _EFFECT_DONE)
        return
    if state in _NONE_STATES:
        if nodes.get(node) not in {_EFFECT_STICKY, _EFFECT_OPEN, _EFFECT_WAIT}:
            _remember(nodes, node, _EFFECT_DONE)
        return
    _remember(nodes, node, _EFFECT_STICKY)


def _tool_waiting(event: Mapping[str, Any]) -> str:
    continuation = event.get("toolContinuation")
    if isinstance(continuation, Mapping) and "toolCalls" in continuation:
        calls = continuation.get("toolCalls")
        if not isinstance(calls, list):
            return _EFFECT_STICKY
        if any(isinstance(item, Mapping) and len(item) > 0 for item in calls):
            return _EFFECT_WAIT
    count = event.get("toolCount")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        return _EFFECT_STICKY
    return _EFFECT_WAIT


def _finished_public_answer(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a tool-free public answer, or nothing when the shape is unfinished."""
    if event.get("type") != "agent_output":
        return None
    phase = event.get("phase")
    output = event.get("output")
    if not isinstance(phase, str) or not phase or not isinstance(output, Mapping):
        return None
    identity = output.get("id")
    if identity is not None and identity != phase:
        return None
    content = output.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    calls = event.get("toolCalls")
    if isinstance(calls, list) and any(isinstance(item, Mapping) and item for item in calls):
        return None
    return {"type": "agent_output", "phase": phase, "output": dict(output)}


def _request_published_answer(event: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = event.get("publishedAnswer")
    if not isinstance(raw, Mapping):
        return None
    answer = _finished_public_answer(raw)
    if answer is None or answer["phase"] != event.get("phase"):
        return None
    return answer


def retained_effect(events: Sequence[Any]) -> dict[str, Any]:
    """Classify the retained public stream for resume, rerun, and payload update.

    A complete stream with no outstanding effect is ``none``. An unknown,
    unconfirmed, missing, timed-out, or absent record is ``effect_unknown``
    and is not treated as not applied. An open tool request is
    ``awaiting_tools`` and stays on that same effect.
    """
    nodes: dict[str, str] = {}
    for event in events:
        if not isinstance(event, Mapping):
            _remember(nodes, "", _EFFECT_STICKY)
            continue
        kind = event.get("type")
        node = _node_id(event)
        if kind in {"node_effect", "agent_node_effect"}:
            if "effect" in event:
                _apply_effect_state(nodes, node, event.get("effect"))
            elif "state" in event:
                _apply_effect_state(nodes, node, event.get("state"))
            if "observation" in event:
                _remember(nodes, node, _EFFECT_STICKY)
            continue
        if kind == "agent_tool_request":
            if _request_published_answer(event) is not None:
                _remember(nodes, node, _EFFECT_DONE)
            else:
                _remember(nodes, node, _tool_waiting(event))
            continue
        if kind == "agent_output":
            _remember(nodes, node, _EFFECT_DONE)
            continue
        if kind == "agent" and event.get("status") in {"done", "error"}:
            _remember(nodes, node, _EFFECT_DONE)
            continue
        if kind == "agent" and event.get("status") == "running":
            _remember(nodes, node, _EFFECT_OPEN)
            continue
        text = event.get("text")
        if kind == "content" and isinstance(text, str) and text.strip():
            _remember(nodes, "synthesizer", _EFFECT_DONE)
    effect = "none"
    for kind in nodes.values():
        if kind in {_EFFECT_STICKY, _EFFECT_OPEN}:
            effect = "effect_unknown"
            break
        if kind == _EFFECT_WAIT:
            effect = "awaiting_tools"
    return {"effect": effect, "sameEffect": effect == "awaiting_tools"}


_JOURNAL_STATE_DOCUMENT = '{"type":"node_effect","phase":"%s","status":"%s","state":"%s"}'
_JOURNAL_OBSERVATION_DOCUMENT = '{"type":"node_effect","phase":"%s","scope":"%s","observation":"%s"}'
_JOURNAL_OBSERVATIONS = ("missing", "timeout", "absent_log", "unavailable", "not_applied")
_JOURNAL_STICKY = {
    "EFFECT_UNKNOWN": "EFFECT_UNKNOWN",
    "effect_unknown": "EFFECT_UNKNOWN",
    "DISPATCHING": "DISPATCHING",
    "dispatching": "DISPATCHING",
    "": "",
}
_JOURNAL_WAITING = {"AWAITING_TOOLS": "AWAITING_TOOLS", "awaiting_tools": "AWAITING_TOOLS"}
_JOURNAL_SETTLED = {
    "CONFIRMED": "CONFIRMED",
    "confirmed": "CONFIRMED",
    "SETTLED_FAILURE": "SETTLED_FAILURE",
    "settled_failure": "SETTLED_FAILURE",
    "NONE": "NONE",
    "none": "NONE",
}
_JOURNAL_UNREADABLE_STATE = {"NOT_APPLIED": "not_applied", "not_applied": "not_applied"}


def _safe_journal_token(value: str) -> bool:
    if len(value) > 64:
        return False
    return all(character.isascii() and (character.isalnum() or character in "_.-:") for character in value)


def _apply_journal_template(template: str, values: tuple[str, ...]) -> str:
    text = template
    for value in values:
        text = text.replace("%s", value, 1)
    return text


def journal_observation_bytes(phase: str, status: str, scope: str) -> bytes | None:
    """Canonical node_effect bytes. Rust put() and the Go reference hash this text."""
    if not _safe_journal_token(phase) or not _safe_journal_token(status) or not _safe_journal_token(scope):
        return None
    if scope in _JOURNAL_OBSERVATIONS:
        text = _apply_journal_template(_JOURNAL_OBSERVATION_DOCUMENT, (phase, scope, scope))
    else:
        text = _apply_journal_template(_JOURNAL_STATE_DOCUMENT, (phase, status, status))
    return text.encode("utf-8")


def _journal_query_observation(query_error: object) -> str | None:
    if query_error is None:
        return None
    if not isinstance(query_error, str) or query_error == "":
        return "unavailable"
    lowered = query_error.lower()
    if (
        query_error == "timeout"
        or "deadline exceeded" in lowered
        or "deadlineexceeded" in lowered
        or "context canceled" in lowered
        or "context cancelled" in lowered
        or "timeout" in lowered
    ):
        return "timeout"
    if "ABSENT_LOG" in query_error or "absent_log" in lowered:
        return "absent_log"
    return "unavailable"


def _journal_document(phase: str, status: str, scope: str, *, append: bool, kind: str) -> dict[str, Any]:
    raw = journal_observation_bytes(phase, status, scope)
    if raw is None:
        if (phase, status, scope) != ("", "", "unavailable"):
            return _journal_document("", "", "unavailable", append=True, kind="unreadable")
        raw = b""
    event: dict[str, Any] = {"type": "node_effect", "phase": phase}
    public_status = ""
    public_scope = ""
    if scope in _JOURNAL_OBSERVATIONS:
        public_scope = scope
        event["scope"] = scope
        event["observation"] = scope
    else:
        public_status = status
        event["status"] = status
        event["state"] = status
    return {
        "kind": kind,
        "append": append,
        "phase": phase,
        "status": public_status,
        "scope": public_scope,
        "event": event,
        "bytes": raw,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "length": len(raw),
    }


def _journal_phase(node_id: object) -> str | None:
    if isinstance(node_id, str) and node_id != "" and _safe_journal_token(node_id):
        return node_id
    return None


def project_journal_observation(node_id: object, found: object, state: object, query_error: object) -> dict[str, Any]:
    """Project one journal read onto the public stream retained_effect already classifies.

    Unknown and waiting reads are appended. A settled read is not, so a confirmed
    effect can still complete. A missing row, timeout, or absent log is an
    observation and is not emitted as state NOT_APPLIED.
    """
    phase = _journal_phase(node_id)
    if phase is None or not isinstance(found, bool):
        return _journal_document("", "", "unavailable", append=True, kind="unreadable")
    observation = _journal_query_observation(query_error)
    if observation is not None:
        return _journal_document(phase, "", observation, append=True, kind="unreadable")
    if found is not True:
        return _journal_document(phase, "", "missing", append=True, kind="unreadable")
    if not isinstance(state, str):
        return _journal_document(phase, "", "unavailable", append=True, kind="unreadable")
    if state in _JOURNAL_UNREADABLE_STATE:
        return _journal_document(phase, "", _JOURNAL_UNREADABLE_STATE[state], append=True, kind="unreadable")
    if state in _JOURNAL_WAITING:
        return _journal_document(phase, _JOURNAL_WAITING[state], "", append=True, kind="awaiting")
    if state in _JOURNAL_SETTLED:
        return _journal_document(phase, _JOURNAL_SETTLED[state], "", append=False, kind="settled")
    if state in _JOURNAL_STICKY:
        return _journal_document(phase, _JOURNAL_STICKY[state], "", append=True, kind="unknown")
    if _safe_journal_token(state):
        return _journal_document(phase, state, "", append=True, kind="unknown")
    return _journal_document(phase, "", "unavailable", append=True, kind="unreadable")


def _refuse(status: int, code: str, message: str) -> dict[str, Any]:
    return {
        "admitted": False,
        "httpStatus": status,
        "code": code,
        "message": message,
        "dispatch": "none",
        "started": False,
        "bumpsRevision": False,
        "requestedRevision": 0,
        "events": [],
    }


def _allow(dispatch: str, started: bool, requested: int, events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "admitted": True,
        "httpStatus": 200,
        "code": "",
        "message": "",
        "dispatch": dispatch,
        "started": started,
        "bumpsRevision": True,
        "requestedRevision": requested,
        "events": events,
    }


def _status(status: str, reason: str) -> dict[str, str]:
    return {"type": "run_status", "status": status, "reason": reason}


def _reset(phase: str, reason: str) -> dict[str, str]:
    return {"type": "agent_reset", "phase": phase, "reason": reason}


def _final(reason: str) -> dict[str, str]:
    return {"type": "final_reset", "scope": "final_answer", "reason": reason}


def _incomplete(plan: list[str], nodes: list[Mapping[str, Any]]) -> list[str]:
    by_id = {str(node.get("id")): node for node in nodes}
    pending: list[str] = []
    for node_id in plan:
        node = by_id.get(node_id)
        if node is None or node.get("failed") is True or node.get("state") != "succeeded":
            pending.append(node_id)
    return pending


def admit(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Decide a retained runtime operation. Refusal never includes a dispatch."""
    rules = contract()
    operation = fields.get("operation")
    if operation not in rules["operations"]:
        return _refuse(400, "AGENT_RUN_REQUEST_INVALID", "任务请求无效")
    if fields.get("identityValid") is not True:
        return _refuse(403, "INVALID_IDENTITY", "Agent runtime identity is invalid")
    if fields.get("lease") != "live":
        return _refuse(409, "LEASE_NOT_HELD", "Agent runtime lease is not live")
    epoch = fields.get("epoch")
    if epoch == "stale":
        return _refuse(409, "STALE_EXECUTION_EPOCH", "Agent runtime epoch is stale")
    if epoch != "match":
        return _refuse(409, "FENCE_MISMATCH", "Agent runtime fence does not match")
    if fields.get("effect") == "effect_unknown":
        return _refuse(409, "EFFECT_UNKNOWN", "Agent runtime effect is unknown and was not retried")
    current = fields.get("runtimeRevision")
    requested = fields.get("requestedRevision")
    if not isinstance(current, int) or isinstance(current, bool) or not isinstance(requested, int) or isinstance(requested, bool):
        return _refuse(409, "AGENT_RUNTIME_REVISION_DENIED", "Agent runtime revision is not the next admitted revision")
    continuation = operation == "tool_continuation"
    revision_ok = requested == current and current >= 1 if continuation else current >= 1 and requested == current + 1
    if not revision_ok:
        return _refuse(409, "AGENT_RUNTIME_REVISION_DENIED", "Agent runtime revision is not the next admitted revision")
    if continuation:
        agent_id = str(fields.get("agentId") or "")
        plan = fields.get("plan")
        effect_waiting = fields.get("effect") == "awaiting_tools" and fields.get("sameEffect") is True
        status_running = fields.get("status") == "running"
        if not effect_waiting or not status_running or not isinstance(plan, list):
            return _refuse(409, "AGENT_TOOL_CONTINUATION_REQUIRED", "Tool continuation requires the original awaiting effect")
        if agent_id not in rules["knownAgents"] or agent_id not in plan:
            return _refuse(400, "AGENT_RUN_REQUEST_INVALID", "Unknown Agent")
        return {
            "admitted": True,
            "httpStatus": 200,
            "code": "",
            "message": "",
            "dispatch": "same-effect",
            "started": False,
            "bumpsRevision": False,
            "requestedRevision": current,
            "events": [],
        }
    if fields.get("effect") == "awaiting_tools":
        return _refuse(409, "AGENT_TOOL_CONTINUATION_REQUIRED", "Confirm tool continuation before changing this run")
    status = fields.get("status")
    if status in rules["activeStatuses"]:
        return _refuse(409, "NATIVE_AGENT_ACTION_CONFLICT", "Agent run is still running")
    if status == "awaiting_plan":
        return _refuse(409, "NATIVE_AGENT_ACTION_CONFLICT", "Confirm the plan before resuming")
    if status not in rules["resumableStatuses"]:
        return _refuse(409, "NATIVE_AGENT_ACTION_CONFLICT", "Agent run is not ready for this action")
    if operation == "runtime_update":
        return _allow("none", False, requested, [])
    plan = fields.get("plan")
    nodes = fields.get("nodes")
    if operation == "rerun":
        agent_id = str(fields.get("agentId") or "")
        if agent_id in rules["synthesizerAliases"]:
            agent_id = "synthesizer"
        if not agent_id:
            return _refuse(400, "AGENT_RUN_REQUEST_INVALID", "agentId is required")
        events: list[dict[str, str]] = [_status("running", "rerun")]
        if agent_id == "synthesizer":
            events.append(_final("rerun_synthesizer"))
            return _allow("synthesis-only", True, requested, events)
        if agent_id not in rules["knownAgents"]:
            return _refuse(400, "AGENT_RUN_REQUEST_INVALID", "Unknown Agent")
        events.append(_reset(agent_id, "rerun_agent"))
        if fields.get("resynthesize") is not False:
            events.append(_final("rerun_agent"))
        return _allow("rerun-node", True, requested, events)
    if not isinstance(plan, list) or not all(isinstance(item, str) for item in plan) or not isinstance(nodes, list):
        return _refuse(400, "AGENT_RUN_REQUEST_INVALID", "任务请求无效")
    pending = _incomplete(plan, nodes)
    if not pending and fields.get("hasFinalAnswer") is True:
        return _allow("none", False, requested, [_status("running", "resume"), _status("done", "resume")])
    events = [_status("running", "resume")]
    if not pending:
        events.append(_final("resume"))
        return _allow("synthesis-only", True, requested, events)
    events.extend(_reset(node_id, "resume") for node_id in pending)
    events.append(_final("resume"))
    return _allow("reset-incomplete", True, requested, events)


def _result_key(event: Mapping[str, Any]) -> tuple[str, str]:
    phase = event.get("phase")
    call_id = event.get("toolCallId")
    return (
        phase if isinstance(phase, str) else "",
        call_id if isinstance(call_id, str) else "",
    )


def _fresh_tool_results(events: Sequence[Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Return whether a request holds tool results, and the copies not already events."""
    seen: set[tuple[str, str]] = set()
    for event in events:
        if isinstance(event, Mapping) and event.get("type") == "agent_tool_result":
            seen.add(_result_key(event))
    fresh: list[dict[str, Any]] = []
    had = False
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "agent_tool_request":
            continue
        results = event.get("toolResults")
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, Mapping) or item.get("type") != "agent_tool_result":
                continue
            had = True
            key = _result_key(item)
            if key in seen:
                continue
            seen.add(key)
            fresh.append(dict(item))
    return had, fresh


def resume_stored_tools(fields: Mapping[str, Any], events: Sequence[Any]) -> dict[str, Any]:
    """Copy stored tool results for resume or rerun.

    The retained stream chooses the effect, so a caller cannot hide an unknown
    record. Other operations use ``admit`` with that effect. A public
    ``agent_tool_request`` that already holds ``agent_tool_result`` objects is
    copied. This does not call the tool and does not append a confirmed effect.
    A request with no stored result stays on the awaiting-tools refusal. A
    result already present as its own event is not copied again.
    """
    retained = retained_effect(events)
    probed = dict(fields)
    probed["effect"] = retained["effect"]
    probed["sameEffect"] = retained["sameEffect"]
    if fields.get("operation") not in {"resume", "rerun"}:
        return admit(probed)
    if retained["effect"] != "awaiting_tools":
        return admit(probed)
    had, fresh = _fresh_tool_results(events)
    if not had:
        return admit(probed)
    probe = admit(probed)
    if probe["code"] != "AGENT_TOOL_CONTINUATION_REQUIRED":
        return probe
    return {
        "admitted": True,
        "httpStatus": 200,
        "code": "",
        "message": "",
        "dispatch": "none",
        "started": False,
        "bumpsRevision": False,
        "requestedRevision": fields.get("requestedRevision"),
        "events": fresh,
    }


def _retained_tool_calls(events: Sequence[Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "agent_tool_request":
            continue
        continuation = event.get("toolContinuation")
        if not isinstance(continuation, Mapping):
            continue
        raw = continuation.get("toolCalls")
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, Mapping) and len(item) > 0:
                calls.append(dict(item))
    return calls


def continue_retained_tools(
    fields: Mapping[str, Any],
    events: Sequence[Any],
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return the dispatcher envelope for one admitted same-effect continuation.

    Calls come only from a retained ``toolContinuation``. A missing row, a
    timeout, or an absent log is ``effect_unknown`` and produces no tool
    result and no state ``NOT_APPLIED``. A later ``agent_output`` still settles
    the phase. This does not append ``CONFIRMED`` or change the operation.
    ``workspace_root`` points the shipped tool call at that root for this call
    only. ``None`` keeps the process stores.
    """
    retained = retained_effect(events)
    if retained["effect"] == "effect_unknown":
        return _refuse(409, "EFFECT_UNKNOWN", "Agent runtime effect is unknown and was not retried")
    decision = admit(fields)
    if decision["admitted"] is not True or decision["dispatch"] != "same-effect":
        return decision
    if retained["effect"] != "awaiting_tools" or retained["sameEffect"] is not True:
        return _refuse(
            409,
            "AGENT_TOOL_CONTINUATION_REQUIRED",
            "Tool continuation requires the original awaiting effect",
        )
    calls = _retained_tool_calls(events)
    if not calls:
        return decision
    from deepseek_infra.infra.tool_runtime.tools import MAX_TOOL_CALLS_PER_RESPONSE, execute_tool_call

    agent_id = str(fields.get("agentId") or "")
    results: list[dict[str, Any]] = []
    for call in calls[:MAX_TOOL_CALLS_PER_RESPONSE]:
        raw_id = call.get("id")
        if workspace_root is None:
            output = execute_tool_call(call)
        else:
            output = _execute_retained_tool(call, workspace_root)
        results.append({
            "type": "agent_tool_result",
            "phase": agent_id,
            "toolCallId": raw_id if isinstance(raw_id, str) else "",
            "output": output,
        })
    decision["events"] = results
    decision["started"] = False
    decision["bumpsRevision"] = False
    return decision


def _execute_retained_tool(call: Mapping[str, Any], workspace_root: str | Path) -> dict[str, Any]:
    """Run the shipped tool call with the reminders file pointed at ``workspace_root``.

    The binding is restored before this function returns, including when the
    tool raises. This does not write the repository reminders file.
    """
    import deepseek_infra.infra.data.reminders as reminders
    from deepseek_infra.infra.tool_runtime.tools import execute_tool_call

    root = Path(workspace_root)
    target = root / ".reminders" / "reminders.json"
    previous_file = reminders.REMINDERS_FILE
    previous_dir = reminders.REMINDERS_DIR
    reminders.REMINDERS_FILE = target
    reminders.REMINDERS_DIR = target.parent
    try:
        return execute_tool_call(dict(call))
    finally:
        reminders.REMINDERS_FILE = previous_file
        reminders.REMINDERS_DIR = previous_dir


_RETAINED_TOOL_RESULT_SCOPE = "retained_tool_results"


def _event_body_ref(event: Mapping[str, Any]) -> tuple[str, int]:
    ref = event.get("bodyRef")
    if not isinstance(ref, Mapping):
        ref = event.get("body")
    if not isinstance(ref, Mapping):
        return "", 0
    sha = ref.get("sha256")
    length = ref.get("length")
    if not isinstance(sha, str) or isinstance(length, bool) or not isinstance(length, int):
        return "", 0
    if len(sha) != 64 or length <= 0 or any(character not in "0123456789abcdef" for character in sha):
        return "", 0
    return sha, length


def _projected_driver_results(events: Sequence[Any], public: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Return result headers bound to this public artifact.

    A lone result header is not a stored file. The request marker must name
    the same artifact the effect already published.
    """
    if not isinstance(public, Mapping):
        return []
    want = _event_body_ref({"bodyRef": public})
    if not want[0]:
        return []
    phase = None
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "agent_tool_request":
            continue
        if event.get("scope") != _RETAINED_TOOL_RESULT_SCOPE or _event_body_ref(event) != want:
            continue
        raw = event.get("phase")
        phase = raw if isinstance(raw, str) else ""
    if not phase:
        return []
    fresh: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "agent_tool_result":
            continue
        if event.get("phase") != phase or not _event_body_ref(event)[0]:
            continue
        raw_call = event.get("toolCallId")
        if isinstance(raw_call, str) and raw_call:
            call = raw_call
        else:
            raw_scope = event.get("scope")
            call = raw_scope if isinstance(raw_scope, str) else ""
        key = (phase, call, _event_body_ref(event)[0])
        if key in seen:
            continue
        seen.add(key)
        fresh.append(dict(event))
    return fresh


def _stored_result_copies(events: Sequence[Any]) -> list[dict[str, Any]]:
    """Return standalone result events that match a stored tool result."""
    wanted: set[tuple[str, str]] = set()
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "agent_tool_request":
            continue
        results = event.get("toolResults")
        if not isinstance(results, list):
            continue
        for item in results:
            if isinstance(item, Mapping) and item.get("type") == "agent_tool_result":
                wanted.add(_result_key(item))
    copies: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "agent_tool_result":
            continue
        key = _result_key(event)
        if key not in wanted or key in seen:
            continue
        seen.add(key)
        copies.append(dict(event))
    return copies


def retained_driver_tool_results(
    state: str,
    events: Sequence[Any],
    public: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return stored tool results for one waiting driver observation.

    A public ``agent_tool_request`` that already holds ``agent_tool_result``
    objects is copied. Header copies are returned only when a request marker
    names the same public artifact. This does not call the tool and does not
    settle the node. A waiting effect with no stored result stays pending.
    """
    if state not in {"AWAITING_TOOLS", "awaiting_tools"}:
        return {"returned": False, "code": "", "events": []}
    had, fresh = _fresh_tool_results(events)
    if had:
        copies = fresh if fresh else _stored_result_copies(events)
        if copies:
            return {"returned": True, "code": "", "events": copies}
        return {"returned": False, "code": "AGENT_DRIVER_TOOLS_PENDING", "events": []}
    projected = _projected_driver_results(events, public)
    if projected:
        return {"returned": True, "code": "", "events": projected}
    return {"returned": False, "code": "AGENT_DRIVER_TOOLS_PENDING", "events": []}


def _canonical_token(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or value == "" or len(value) > limit or "\n" in value or "\r" in value:
        return None
    return value


def successor_operation_id(operation_id: str, action_id: str, epoch: int, node_id: str, reset_index: int) -> str:
    """Deterministic successor. The material is the same string Rust and Go hash."""
    material = f"agent-node-successor-v1\n{operation_id}\n{action_id}\n{epoch}\n{node_id}\n{reset_index}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return "agent-successor-" + digest


def successor_generation(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Mint a successor id only for a settled effect whose reset shares the fence.

    The retained id is the original operation. An unknown effect never receives
    a successor, and a different existing successor is not replaced.
    """
    operation_id = fields.get("operationId")
    retained = operation_id if isinstance(operation_id, str) else ""

    def refuse(code: str, message: str) -> dict[str, Any]:
        return {
            "admitted": False,
            "code": code,
            "message": message,
            "retainedOperationId": retained,
            "successorOperationId": "",
            "dispatch": "none",
            "replayed": False,
        }

    if fields.get("identityValid") is not True:
        return refuse("INVALID_IDENTITY", "Agent runtime identity is invalid")
    if fields.get("lease") != "live":
        return refuse("LEASE_NOT_HELD", "Agent runtime lease is not live")
    epoch = fields.get("epoch")
    if epoch == "stale":
        return refuse("STALE_EXECUTION_EPOCH", "Agent runtime epoch is stale")
    if epoch != "match":
        return refuse("FENCE_MISMATCH", "Agent runtime fence does not match")
    effect = fields.get("effect")
    if effect == "AWAITING_TOOLS":
        return refuse("AGENT_DRIVER_TOOLS_PENDING", "Tool continuation stays on the original effect")
    if effect not in {"CONFIRMED", "SETTLED_FAILURE"}:
        return refuse("EFFECT_UNKNOWN", "Agent runtime effect is unknown and was not retried")
    if fields.get("resetAdmitted") is not True or fields.get("resetFenceMatches") is not True:
        return refuse("AGENT_RUNTIME_RESET_REQUIRES_GENERATION", "Agent reset requires a settled effect on the same fence")
    action_id = _canonical_token(fields.get("actionId"), 256)
    node_id = _canonical_token(fields.get("nodeId"), 64)
    old_id = _canonical_token(operation_id, 256)
    execution_epoch = fields.get("executionEpoch")
    reset_index = fields.get("resetIndex")
    if (
        old_id is None
        or action_id is None
        or node_id is None
        or not isinstance(execution_epoch, int)
        or isinstance(execution_epoch, bool)
        or not isinstance(reset_index, int)
        or isinstance(reset_index, bool)
        or execution_epoch <= 0
        or execution_epoch > _UINT64_MAX
        or reset_index < 0
        or reset_index > _INT64_MAX
    ):
        return refuse("AGENT_RUNTIME_RESET_REQUIRES_GENERATION", "Agent reset requires a settled effect on the same fence")
    successor = successor_operation_id(old_id, action_id, execution_epoch, node_id, reset_index)
    if successor == old_id:
        return refuse("AGENT_RUNTIME_GENERATION_CONFLICT", "Agent successor generation does not replace a different operation")
    existing = fields.get("existingSuccessor", "")
    if existing is None:
        existing = ""
    if not isinstance(existing, str):
        return refuse("AGENT_RUNTIME_GENERATION_CONFLICT", "Agent successor generation does not replace a different operation")
    if existing != "" and existing != successor:
        return refuse("AGENT_RUNTIME_GENERATION_CONFLICT", "Agent successor generation does not replace a different operation")
    return {
        "admitted": True,
        "code": "",
        "message": "",
        "retainedOperationId": old_id,
        "successorOperationId": successor,
        "dispatch": "successor",
        "replayed": existing == successor,
    }


def successor_binding(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Record the minted id beside the original operation. Never replace it.

    A refusal, including EFFECT_UNKNOWN, writes nothing. A different id that is
    already stored stays stored.
    """
    decision = successor_generation(fields)
    existing = fields.get("existingSuccessor", "")
    if not isinstance(existing, str):
        existing = ""
    retained = decision["retainedOperationId"]
    if decision["admitted"] is not True:
        return {
            "persisted": False,
            "replacedOperationId": False,
            "operationId": retained,
            "successorOperationId": existing,
            "resetIndex": 0,
            "code": decision["code"],
            "replayed": False,
        }
    return {
        "persisted": True,
        "replacedOperationId": False,
        "operationId": retained,
        "successorOperationId": decision["successorOperationId"],
        "resetIndex": fields.get("resetIndex"),
        "code": "",
        "replayed": decision["replayed"],
    }


def _u64(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > _UINT64_MAX:
        return None
    return value


def successor_execution(fields: Mapping[str, Any]) -> dict[str, Any]:
    """Execute a persisted successor only after authority issues a newer epoch.

    The worker does not install that epoch. A promoted, stale, or unknown
    effect returns no operation id and no epoch to install.
    """
    operation_id = fields.get("operationId")
    retained = operation_id if isinstance(operation_id, str) else ""
    original = _u64(fields.get("executionEpoch"))
    retained_epoch = original if original is not None else 0

    def refuse(code: str, message: str) -> dict[str, Any]:
        return {
            "admitted": False,
            "execute": False,
            "code": code,
            "message": message,
            "retainedOperationId": retained,
            "retainedEpoch": retained_epoch,
            "operationId": "",
            "executionEpoch": 0,
            "promoted": False,
        }

    if fields.get("workerPromoted") is True:
        return refuse("FENCE_MISMATCH", "Worker cannot promote its own execution epoch")
    if fields.get("identityValid") is not True:
        return refuse("INVALID_IDENTITY", "Agent runtime identity is invalid")
    if fields.get("lease") != "live":
        return refuse("LEASE_NOT_HELD", "Agent runtime lease is not live")
    authority_epoch = _u64(fields.get("authorityEpoch"))
    presented_epoch = _u64(fields.get("presentedEpoch"))
    if fields.get("epoch") == "stale" or (
        authority_epoch is not None and presented_epoch is not None and presented_epoch < authority_epoch
    ):
        return refuse("STALE_EXECUTION_EPOCH", "Agent runtime epoch is stale")
    if (
        fields.get("epoch") != "match"
        or authority_epoch is None
        or presented_epoch is None
        or authority_epoch == 0
        or presented_epoch == 0
        or presented_epoch != authority_epoch
    ):
        return refuse("FENCE_MISMATCH", "Agent runtime fence does not match")
    effect = fields.get("effect")
    if effect == "AWAITING_TOOLS":
        return refuse("AGENT_DRIVER_TOOLS_PENDING", "Tool continuation stays on the original effect")
    if effect not in {"CONFIRMED", "SETTLED_FAILURE"}:
        return refuse("EFFECT_UNKNOWN", "Agent runtime effect is unknown and was not retried")
    if fields.get("resetAdmitted") is not True or fields.get("resetFenceMatches") is not True:
        return refuse("AGENT_RUNTIME_RESET_REQUIRES_GENERATION", "Agent reset requires a settled effect on the same fence")
    if fields.get("authorityIssued") is not True:
        return refuse("FENCE_MISMATCH", "Only claim or takeover can issue the next execution epoch")
    action_id = _canonical_token(fields.get("actionId"), 256)
    node_id = _canonical_token(fields.get("nodeId"), 64)
    old_id = _canonical_token(operation_id, 256)
    reset_index = fields.get("resetIndex")
    if (
        old_id is None
        or action_id is None
        or node_id is None
        or original is None
        or original == 0
        or isinstance(reset_index, bool)
        or not isinstance(reset_index, int)
        or reset_index < 0
        or reset_index > _INT64_MAX
    ):
        return refuse("AGENT_RUNTIME_RESET_REQUIRES_GENERATION", "Agent reset requires a settled effect on the same fence")
    stored_operation = fields.get("storedOperationId")
    if not isinstance(stored_operation, str) or stored_operation != old_id:
        return refuse("AGENT_RUNTIME_GENERATION_CONFLICT", "Agent successor generation does not replace a different operation")
    if authority_epoch <= original:
        return refuse("AGENT_RUNTIME_SUCCESSOR_HELD", "Successor generation stays on the original fence until takeover")
    successor = successor_operation_id(old_id, action_id, original, node_id, reset_index)
    stored = fields.get("storedSuccessor", "")
    if not isinstance(stored, str) or stored == "" or stored != successor or stored == old_id:
        return refuse("AGENT_RUNTIME_SUCCESSOR_HELD", "Successor generation is not persisted on the original intent")
    return {
        "admitted": True,
        "execute": True,
        "code": "",
        "message": "",
        "retainedOperationId": old_id,
        "retainedEpoch": original,
        "operationId": stored,
        "executionEpoch": authority_epoch,
        "promoted": False,
    }


def dispatch_allowed(lease: str, epoch: str, effect: str) -> tuple[str, bool]:
    """Refuse a new provider dispatch. Tool waits stay on the original effect."""
    if lease != "live":
        return ("LEASE_NOT_HELD", False)
    if epoch == "stale":
        return ("STALE_EXECUTION_EPOCH", False)
    if epoch != "match":
        return ("FENCE_MISMATCH", False)
    if effect in {"EFFECT_UNKNOWN", ""}:
        return ("EFFECT_UNKNOWN", False)
    if effect == "AWAITING_TOOLS":
        return ("AGENT_DRIVER_TOOLS_PENDING", False)
    if effect in {"CONFIRMED", "SETTLED_FAILURE", "NONE"}:
        return ("", True)
    return ("EFFECT_UNKNOWN", False)


def _model_turn_idle(code: str) -> dict[str, Any]:
    return {"started": False, "code": code, "body": None, "events": []}


def _stored_outputs(events: Sequence[Any]) -> dict[str, dict[str, Any]]:
    """Map a tool call id to the output object already stored on the request."""
    found: dict[str, dict[str, Any]] = {}
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "agent_tool_request":
            continue
        results = event.get("toolResults")
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, Mapping) or item.get("type") != "agent_tool_result":
                continue
            call_id = item.get("toolCallId")
            output = item.get("output")
            if not isinstance(call_id, str) or not call_id or not isinstance(output, Mapping):
                continue
            found.setdefault(call_id, dict(output))
    return found


def _continuation_turn(events: Sequence[Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    assistant: dict[str, Any] = {}
    calls: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "agent_tool_request":
            continue
        continuation = event.get("toolContinuation")
        if not isinstance(continuation, Mapping):
            continue
        raw_calls = continuation.get("toolCalls")
        if isinstance(raw_calls, list):
            calls = [dict(item) for item in raw_calls if isinstance(item, Mapping) and item]
        raw_assistant = continuation.get("assistant")
        assistant = dict(raw_assistant) if isinstance(raw_assistant, Mapping) else {}
    return assistant, calls


def next_model_turn(body: Mapping[str, Any], events: Sequence[Any]) -> dict[str, Any]:
    """Build the next provider body from tool results already stored.

    The assistant turn and each tool message come from the retained request.
    This does not call the tool. A waiting request with no stored result does
    not start the turn. The decision appends no effect.
    """
    retained = retained_effect(events)
    if retained["effect"] == "effect_unknown":
        return _model_turn_idle("EFFECT_UNKNOWN")
    if retained["effect"] != "awaiting_tools":
        return _model_turn_idle("")
    if not isinstance(body, Mapping):
        return _model_turn_idle("AGENT_DRIVER_TOOLS_PENDING")
    from deepseek_infra.infra.tool_runtime.tools import MAX_TOOL_CALLS_PER_RESPONSE, tool_result_message

    assistant, calls = _continuation_turn(events)
    selected = calls[:MAX_TOOL_CALLS_PER_RESPONSE]
    outputs = _stored_outputs(events)
    if not selected:
        return _model_turn_idle("AGENT_DRIVER_TOOLS_PENDING")
    tool_messages: list[dict[str, Any]] = []
    for call in selected:
        call_id = call.get("id")
        if not isinstance(call_id, str) or call_id not in outputs:
            return _model_turn_idle("AGENT_DRIVER_TOOLS_PENDING")
        tool_messages.append(tool_result_message(dict(call), outputs[call_id]))
    content = assistant.get("content")
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    else:
        text = str(content)
    payload: dict[str, Any] = {"role": "assistant", "content": text, "tool_calls": selected}
    reasoning = assistant.get("reasoning_content") or assistant.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        payload["reasoning_content"] = reasoning
    next_body = dict(body)
    prior = body.get("messages")
    messages: list[Any] = []
    if isinstance(prior, list):
        messages.extend(dict(item) if isinstance(item, Mapping) else item for item in prior)
    messages.append(payload)
    messages.extend(tool_messages)
    next_body["messages"] = messages
    if isinstance(next_body.get("tool_choice"), Mapping):
        next_body["tool_choice"] = "auto"
    return {"started": True, "code": "", "body": next_body, "events": []}


def _answer_idle(code: str) -> dict[str, Any]:
    return {"published": False, "code": code, "event": None}


def _envelope_has_calls(saved: Mapping[str, Any]) -> bool:
    continuation = saved.get("toolContinuation")
    if not isinstance(continuation, Mapping):
        return False
    calls = continuation.get("toolCalls")
    if not isinstance(calls, list):
        return False
    return any(isinstance(item, Mapping) and item for item in calls)


def _waiting_phase(events: Sequence[Any]) -> str:
    phase = ""
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "agent_tool_request":
            continue
        raw_phase = event.get("phase")
        if isinstance(raw_phase, str):
            phase = raw_phase
    return phase


def published_model_answer(events: Sequence[Any], saved: Any) -> dict[str, Any]:
    """Publish a saved follow-up that has no further tool calls.

    A missing file does not publish and is not a new effect state.
    """
    retained = retained_effect(events)
    if retained["effect"] == "effect_unknown":
        return _answer_idle("EFFECT_UNKNOWN")
    if retained["effect"] != "awaiting_tools":
        return _answer_idle("")
    if not isinstance(saved, Mapping) or _envelope_has_calls(saved):
        return _answer_idle("")
    nested = saved.get("publicEvent")
    candidate = nested if isinstance(nested, Mapping) else saved
    if not isinstance(candidate, Mapping):
        return _answer_idle("")
    answer = _finished_public_answer(candidate)
    if answer is None or answer["phase"] != _waiting_phase(events):
        return _answer_idle("")
    return {"published": True, "code": "", "event": answer}


def _settlement_idle(code: str) -> dict[str, Any]:
    return {"settled": False, "code": code, "event": None, "output": None}


def _stored_epoch(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > _UINT64_MAX:
        return None
    return value


def _settlement_counter(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > _INT64_MAX:
        return None
    return value


def _strip_published_answer(events: Sequence[Any]) -> tuple[list[Any], Any]:
    stripped: list[Any] = []
    attached: Any = None
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "agent_tool_request":
            stripped.append(event)
            continue
        if "publishedAnswer" not in event:
            stripped.append(event)
            continue
        attached = event.get("publishedAnswer")
        clone = dict(event)
        clone.pop("publishedAnswer", None)
        stripped.append(clone)
    return stripped, attached


def settle_published_answer(
    events: Sequence[Any],
    saved: Any,
    presented_epoch: Any,
    installed_epoch: Any,
    intent_task: str,
) -> dict[str, Any]:
    """Admit settlement of an answer already published on the tool request.

    The presented epoch must already be the installed epoch. A larger value
    is a fence mismatch and is not installed here. A missing answer does not
    settle and does not invent a receipt.
    """
    presented = _stored_epoch(presented_epoch)
    installed = _stored_epoch(installed_epoch)
    if presented is None or installed is None:
        return _settlement_idle("FENCE_MISMATCH")
    if presented > installed:
        return _settlement_idle("FENCE_MISMATCH")
    if presented < installed:
        return _settlement_idle("STALE_EXECUTION_EPOCH")
    stripped, attached = _strip_published_answer(events)
    if not isinstance(attached, Mapping):
        return _settlement_idle("")
    decision = published_model_answer(stripped, saved)
    if not decision["published"]:
        return _settlement_idle(decision["code"])
    event = decision["event"]
    if event != dict(attached):
        return _settlement_idle("")
    if not isinstance(event, Mapping):
        return _settlement_idle("")
    output = event.get("output")
    if not isinstance(output, Mapping) or output.get("id") != event.get("phase"):
        return _settlement_idle("")
    task = output.get("task")
    if not isinstance(task, str) or not task or task != intent_task:
        return _settlement_idle("")
    latency = _settlement_counter(output.get("latencyMs"))
    if latency is None or "latencyMs" not in output:
        return _settlement_idle("")
    usage = output.get("usage")
    if usage is None:
        usage = {}
    if not isinstance(usage, Mapping):
        return _settlement_idle("")
    prompt = _settlement_counter(usage["prompt_tokens"]) if "prompt_tokens" in usage else 0
    completion = _settlement_counter(usage["completion_tokens"]) if "completion_tokens" in usage else 0
    if prompt is None or completion is None:
        return _settlement_idle("")
    failed = output.get("failed", False)
    if not isinstance(failed, bool):
        return _settlement_idle("")
    return {
        "settled": True,
        "code": "",
        "event": event,
        "output": {
            "promptTokens": prompt,
            "completionTokens": completion,
            "latencyMs": latency,
            "failed": failed,
            "task": task,
        },
    }


def _dependency_idle(code: str) -> dict[str, Any]:
    return {"ready": False, "code": code, "output": None}


def published_dependency_output(
    envelope: Any,
    saved: Any,
    dependency: str,
    task: str,
    run_id: str,
) -> dict[str, Any]:
    """Return a dependency output already stored on the row or the saved answer.

    A direct answer is copied from the row. A tool request contributes the
    saved follow-up only when that follow-up publishes. A missing follow-up
    does not invent an output and does not call the tool.
    """
    if not isinstance(envelope, Mapping) or not dependency or not task:
        return _dependency_idle("")
    if envelope.get("runId") != run_id:
        return _dependency_idle("")
    event = envelope.get("publicEvent")
    if not isinstance(event, Mapping):
        return _dependency_idle("")
    kind = event.get("type")
    if kind == "agent_output":
        output = event.get("output")
        if event.get("phase") != dependency or not isinstance(output, Mapping):
            return _dependency_idle("")
        if output.get("id") != dependency or output.get("task") != task:
            return _dependency_idle("")
        return {"ready": True, "code": "", "output": dict(output)}
    if kind != "agent_tool_request" or event.get("phase") != dependency:
        return _dependency_idle("")
    decision = published_model_answer([event], saved)
    if not decision["published"]:
        code = decision["code"] if isinstance(decision["code"], str) else ""
        return _dependency_idle(code)
    answer = decision["event"]
    if not isinstance(answer, Mapping):
        return _dependency_idle("")
    output = answer.get("output")
    if not isinstance(output, Mapping) or output.get("id") != dependency or output.get("task") != task:
        return _dependency_idle("")
    return {"ready": True, "code": "", "output": dict(output)}


def _provider_body_idle(code: str) -> dict[str, Any]:
    return {"built": False, "code": code, "worker": None, "synthesis": None}


def dependency_provider_body(
    envelope: Any,
    saved: Any,
    dependency: str,
    task: str,
    run_id: str,
    payload: Any,
    role: str,
    assignment: str,
    model: str,
    query: str,
) -> dict[str, Any]:
    """Place one published dependency output into the worker and synthesis bodies.

    A missing answer does not build either body. An unknown observation keeps
    its code and does not build either body.
    """
    decision = published_dependency_output(envelope, saved, dependency, task, run_id)
    output = decision.get("output")
    if not decision.get("ready") or not isinstance(output, Mapping):
        code = decision.get("code")
        if not isinstance(code, str) or not code:
            code = "AGENT_DEPENDENCY_UNPUBLISHED"
        return _provider_body_idle(code)
    if (
        not isinstance(payload, Mapping)
        or not isinstance(model, str)
        or not model
        or not assignment
        or not isinstance(query, str)
        or not query.strip()
    ):
        return _provider_body_idle("")
    from deepseek_infra.infra.agent_runtime import multi_agent as agent_shape

    if role not in agent_shape.AGENT_PROFILES:
        return _provider_body_idle("")
    copied: dict[str, Any] = dict(output)
    request: dict[str, Any] = dict(payload)
    prior = agent_shape.build_prior_context([copied])
    worker = agent_shape.agent_messages(request, assignment, agent_id=role, prior_context=prior)
    synthesis = agent_shape.synthesis_messages(request, query, [copied])
    return {"built": True, "code": "", "worker": worker, "synthesis": synthesis}


def _clock_second(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value <= 0 or value > _INT64_MAX:
        return None
    return value


def _send_lease_idle() -> dict[str, Any]:
    return {
        "allowed": False,
        "code": "AGENT_EXECUTION_LEASE_DENIED",
        "markUnknown": True,
        "providerEffect": False,
    }


def execution_send_lease(deadline: Any, now: Any) -> dict[str, Any]:
    """Admit a provider send only while the execution lease is still ahead.

    A deadline that has arrived does not start the provider call and does not
    store an effect. The caller marks the admitted node unknown.
    """
    left = _clock_second(deadline)
    right = _clock_second(now)
    if left is None or right is None or left <= right:
        return _send_lease_idle()
    return {"allowed": True, "code": "", "markUnknown": False, "providerEffect": False}


def admitted_provider_send(lease: Any) -> dict[str, Any]:
    """Forward the shaped body only for a clean live execution lease.

    Any other lease result denies the send. The denial stores no effect.
    """
    live = (
        isinstance(lease, Mapping)
        and set(lease) == {"allowed", "code", "markUnknown", "providerEffect"}
        and lease.get("allowed") is True
        and lease.get("code") == ""
        and lease.get("markUnknown") is False
        and lease.get("providerEffect") is False
    )
    if not live:
        return _send_lease_idle()
    return {"allowed": True, "code": "", "markUnknown": False, "providerEffect": False}


def _exchange_unknown() -> dict[str, Any]:
    return {
        "code": "AGENT_PROVIDER_RESULT_UNKNOWN",
        "markUnknown": True,
        "providerEffect": False,
    }


def provider_exchange_unknown(receipt_present: Any) -> dict[str, Any]:
    """Classify an admitted send before any provider effect is stored.

    Only an explicit receipt flag is withheld from the unknown code. A
    timeout, a missing object, an absent log, and any other observation stay
    unknown. This decision does not store the effect.
    """
    if receipt_present is True:
        return {"code": "", "markUnknown": False, "providerEffect": False}
    return _exchange_unknown()


def follow_up_exchange(lease_allowed: Any, receipt_present: Any) -> dict[str, Any]:
    """Store a follow-up only after a live lease and a present receipt.

    A dead lease does not start the send. A lost receipt does not store the
    answer and does not mark the confirmed tool request unknown.
    """
    if lease_allowed is not True:
        return {
            "store": False,
            "code": "AGENT_EXECUTION_LEASE_DENIED",
            "markUnknown": False,
            "providerEffect": False,
        }
    if receipt_present is not True:
        return {
            "store": False,
            "code": "AGENT_PROVIDER_RESULT_UNKNOWN",
            "markUnknown": False,
            "providerEffect": False,
        }
    return {"store": True, "code": "", "markUnknown": False, "providerEffect": False}


def workspace_tool_lease(deadline: Any, now: Any) -> dict[str, Any]:
    """Pass a live workspace tool flag only for a clean execution lease.

    The flag is the allowed bit from ``admitted_provider_send`` of
    ``execution_send_lease``. A deadline that has arrived or a non-positive
    clock does not pass the live flag.
    """
    admit = admitted_provider_send(execution_send_lease(deadline, now))
    if (
        admit.get("allowed") is True
        and admit.get("code") == ""
        and admit.get("markUnknown") is False
        and admit.get("providerEffect") is False
    ):
        return {"leaseLive": True, "lease": "live", "code": "", "providerEffect": False}
    code = admit.get("code")
    if not isinstance(code, str) or not code:
        code = "AGENT_EXECUTION_LEASE_DENIED"
    return {"leaseLive": False, "lease": "expired", "code": code, "providerEffect": False}


_INT64_MIN = -_INT64_MAX - 1


def _lease_instant(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < _INT64_MIN or value > _INT64_MAX:
        return None
    return value


def agent_node_send_deadline(grant_expires: Any, claim_lease: Any, writer_lease: Any) -> dict[str, Any]:
    """Return the minimum of the grant expiry, the claim lease, and the writer lease.

    A bool or a value outside the signed 64-bit range produces no deadline.
    The returned number is not a send. The caller passes it through
    ``execution_send_lease`` before a provider send or a live workspace flag.
    """
    grant = _lease_instant(grant_expires)
    claim = _lease_instant(claim_lease)
    writer = _lease_instant(writer_lease)
    if grant is None or claim is None or writer is None:
        return {"deadline": None, "code": "AGENT_EXECUTION_LEASE_DENIED", "providerEffect": False}
    return {"deadline": min(grant, claim, writer), "code": "", "providerEffect": False}


def _send_budget_idle() -> dict[str, Any]:
    return {
        "armed": False,
        "seconds": None,
        "code": "AGENT_EXECUTION_LEASE_DENIED",
        "providerEffect": False,
    }


def execution_send_budget(deadline: Any, now: Any, cap_seconds: Any) -> dict[str, Any]:
    """Arm a provider timeout only for the remaining seconds of a live lease.

    ``execution_send_lease`` runs first. An elapsed or non-positive minimum
    returns no timeout. A live minimum returns the smaller of the remaining
    whole seconds and a positive cap.
    """
    admit = admitted_provider_send(execution_send_lease(deadline, now))
    live = (
        admit.get("allowed") is True
        and admit.get("code") == ""
        and admit.get("markUnknown") is False
        and admit.get("providerEffect") is False
    )
    left = _clock_second(deadline)
    right = _clock_second(now)
    cap = _clock_second(cap_seconds)
    if not live or left is None or right is None or cap is None or left <= right:
        return _send_budget_idle()
    remaining = left - right
    seconds = remaining if remaining < cap else cap
    if seconds <= 0:
        return _send_budget_idle()
    return {"armed": True, "seconds": seconds, "code": "", "providerEffect": False}
