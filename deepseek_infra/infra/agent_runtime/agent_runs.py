"""Persistent Agent Run storage, replay, and background execution."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from deepseek_infra.core.config import AGENT_RUNS_DIR, AGENT_RUNTIME_AUTO_RESUME, DEFAULT_MODEL
from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import latest_user_query, normalize_model_name
from deepseek_infra.infra.gateway.deepseek_client import RequestCancelled, SearchBudget, validate_deepseek_payload
from deepseek_infra.infra.agent_runtime.agent_state import (
    completed_node_ids,
    incomplete_plan_nodes,
    reduce_node_states,
)
from deepseek_infra.infra.agent_runtime.multi_agent import (
    AGENT_PROFILES,
    leader_done_text,
    layered_plan,
    MULTI_AGENT_PER_AGENT_SEARCH_LIMIT,
    MULTI_AGENT_TOTAL_SEARCH_LIMIT,
    default_agent_plan,
    emit_agent_event,
    failed_agent_output,
    new_agent_search_budget,
    new_agent_token_budget,
    parse_structured_agent_output,
    plan_agents,
    run_agent,
    safe_agent_plan,
    stream_agent_plan,
    stream_synthesis_for_outputs,
)

RUN_STATUSES = {"created", "planning", "awaiting_plan", "running", "done", "failed", "cancelled", "orphaned"}
TERMINAL_STATUSES = {"done", "failed", "cancelled", "orphaned"}
ORPHANABLE_STATUSES = {"created", "planning", "running"}
SENSITIVE_PAYLOAD_KEYS = {"apiKey", "tavilyApiKey"}
RUN_ID_RE = re.compile(r"^run_[A-Za-z0-9_-]{8,80}$")
REGISTRY_WAIT_SECONDS = 30.0
RUN_WRITE_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.35)

_RUN_LOCK = threading.RLock()


class AgentRunRegistry:
    """In-memory coordination for background run threads and attached streams."""

    def __init__(self) -> None:
        self._threads: dict[str, threading.Thread] = {}
        self._conditions: dict[str, threading.Condition] = {}
        self._lock = threading.RLock()

    def ensure_started(self, run_id: str, target: Callable[..., None], *args: Any, **kwargs: Any) -> bool:
        with self._lock:
            existing = self._threads.get(run_id)
            if existing and existing.is_alive():
                return False
            thread = threading.Thread(target=target, args=args, kwargs=kwargs, name=f"agent-run-{run_id}", daemon=True)
            self._threads[run_id] = thread
            thread.start()
            return True

    def condition_for(self, run_id: str) -> threading.Condition:
        with self._lock:
            condition = self._conditions.get(run_id)
            if condition is None:
                condition = threading.Condition()
                self._conditions[run_id] = condition
            return condition

    def notify_event(self, run_id: str) -> None:
        condition = self.condition_for(run_id)
        with condition:
            condition.notify_all()

    def wait_for_event(self, run_id: str, timeout: float = REGISTRY_WAIT_SECONDS) -> None:
        condition = self.condition_for(run_id)
        with condition:
            condition.wait(timeout=timeout)


registry = AgentRunRegistry()


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def make_run_id() -> str:
    return "run_" + secrets.token_urlsafe(16).replace("-", "_")


def validate_run_id(run_id: str) -> str:
    value = str(run_id or "").strip()
    if not RUN_ID_RE.match(value):
        raise AppError("Agent run not found", code=ErrorCode.NOT_FOUND, status=404)
    return value


def run_path(run_id: str) -> Path:
    return AGENT_RUNS_DIR / f"{validate_run_id(run_id)}.json"


def sanitize_payload(value: Any) -> Any:
    """Remove API credentials before writing request context into `.agent-runs`."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if str(key) in SENSITIVE_PAYLOAD_KEYS:
                continue
            cleaned[str(key)] = sanitize_payload(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value


def merge_runtime_payload(stored_payload: dict[str, Any], runtime_payload: Any = None) -> dict[str, Any]:
    merged = dict(stored_payload)
    if isinstance(runtime_payload, dict):
        merged.update(runtime_payload)
    return merged


def create_run(
    payload: dict[str, Any],
    *,
    confirm_plan: bool = False,
    agent_preset: str = "full",
    conversation_id: str = "",
    message_id: str = "",
) -> dict[str, Any]:
    now = utc_timestamp()
    run_id = make_run_id()
    safe_payload = sanitize_payload(payload)
    run = {
        "runId": run_id,
        "status": "created",
        "createdAt": now,
        "updatedAt": now,
        "nextIndex": 0,
        "requestMeta": {
            "conversationId": str(conversation_id or ""),
            "messageId": str(message_id or ""),
            "model": str(payload.get("model") or DEFAULT_MODEL),
            "agentPreset": normalize_agent_preset(agent_preset),
            "confirmPlan": bool(confirm_plan),
        },
        "requestPayload": safe_payload,
        "plan": [],
        "agentOutputs": {},
        "finalAnswer": "",
        "diagnostics": {},
        # events 是恢复 UI 的事实源；finalAnswer / agentOutputs / diagnostics
        # 只是为了快速读取的派生快照，任何修复都应优先重放 events。
        "events": [],
    }
    with _RUN_LOCK:
        AGENT_RUNS_DIR.mkdir(parents=True, exist_ok=True)
        write_run(run)
    return public_run(run)


def load_run(run_id: str) -> dict[str, Any]:
    path = run_path(run_id)
    if not path.is_file():
        raise AppError("Agent run not found", code=ErrorCode.NOT_FOUND, status=404)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AppError("Agent run is corrupted", code=ErrorCode.INTERNAL, status=500) from exc
    if not isinstance(data, dict):
        raise AppError("Agent run is corrupted", code=ErrorCode.INTERNAL, status=500)
    return data


def write_run(run: dict[str, Any]) -> None:
    AGENT_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = run_path(str(run.get("runId") or ""))
    # Use a unique temp file per write. Windows can briefly lock either the
    # previous target or a shared `.tmp` path, so fixed temp names can fail with
    # WinError 5 when multiple Agent events are persisted in quick succession.
    tmp_path = path.with_name(f"{path.name}.{threading.get_ident()}.{secrets.token_urlsafe(6)}.tmp")
    try:
        tmp_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_with_retry(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def replace_with_retry(source: Path, target: Path, *, delays: tuple[float, ...] = RUN_WRITE_RETRY_DELAYS) -> None:
    """Atomically replace a run file, tolerating short Windows file locks."""
    attempts = (0.0, *delays)
    last_error: PermissionError | None = None
    for delay in attempts:
        if delay:
            time.sleep(delay)
        try:
            source.replace(target)
            return
        except PermissionError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error


def public_run(run: dict[str, Any], *, include_events: bool = True) -> dict[str, Any]:
    hidden = {"requestPayload"}
    result = {key: value for key, value in run.items() if key not in hidden}
    if not include_events:
        result.pop("events", None)
    return result


def events_after(run_id: str, after: int = -1) -> list[dict[str, Any]]:
    run = load_run(run_id)
    cursor = int(after)
    return [event for event in run.get("events", []) if isinstance(event, dict) and int(event.get("index", -1)) > cursor]


def append_event(run_id: str, event: dict[str, Any]) -> dict[str, Any]:
    with _RUN_LOCK:
        run = load_run(run_id)
        index = int(run.get("nextIndex") or 0)
        stamped = sanitize_payload(dict(event))
        stamped["runId"] = run_id
        stamped["index"] = index
        stamped["createdAt"] = utc_timestamp()
        run.setdefault("events", []).append(stamped)
        run["nextIndex"] = index + 1
        apply_event_snapshot(run, stamped)
        # Durable Agent Runtime: rebuild the node state machine purely from the
        # plan + event log so the persisted snapshot always matches a replay.
        run["nodes"] = reduce_node_states(run.get("plan") or [], run.get("events") or [])
        run["updatedAt"] = stamped["createdAt"]
        write_run(run)
    registry.notify_event(run_id)
    return stamped


def append_status(run_id: str, status: str, **extra: Any) -> dict[str, Any]:
    if status not in RUN_STATUSES:
        raise ValueError(f"Unknown Agent Run status: {status}")
    return append_event(run_id, {"type": "run_status", "status": status, **extra})


def apply_event_snapshot(run: dict[str, Any], event: dict[str, Any]) -> None:
    event_type = event.get("type")
    if event_type == "run_status":
        status = str(event.get("status") or "")
        if status in RUN_STATUSES:
            run["status"] = status
        return
    if event_type == "agent_plan":
        plan = event.get("plan")
        run["plan"] = safe_agent_plan({"agents": plan}) if isinstance(plan, list) else []
        return
    if event_type == "final_reset" and event.get("scope") == "final_answer":
        run["finalAnswer"] = ""
        return
    if event_type == "content":
        run["finalAnswer"] = str(run.get("finalAnswer") or "") + str(event.get("text") or "")
        return
    if event_type == "done":
        run["status"] = "done"
        if isinstance(event.get("diagnostics"), dict):
            run["diagnostics"] = event["diagnostics"]
        return
    if event_type == "error":
        run["status"] = "failed"
        existing_diagnostics = run.get("diagnostics")
        run["diagnostics"] = {**(existing_diagnostics if isinstance(existing_diagnostics, dict) else {}), "error": str(event.get("error") or "")}
        return
    if event_type == "agent_reset":
        phase = str(event.get("phase") or "")
        if phase:
            run.setdefault("agentOutputs", {}).pop(phase, None)
        return
    if event_type == "agent_output":
        output = event.get("output")
        phase = str(event.get("phase") or (output or {}).get("id") or "")
        if phase and isinstance(output, dict):
            run.setdefault("agentOutputs", {})[phase] = sanitize_payload(output)
        return
    if event_type in {"agent", "agent_delta", "agent_reasoning", "agent_note"}:
        update_agent_output_snapshot(run, event)


def update_agent_output_snapshot(run: dict[str, Any], event: dict[str, Any]) -> None:
    phase = str(event.get("phase") or "")
    if not phase or phase == "leader":
        return
    outputs = run.setdefault("agentOutputs", {})
    item = outputs.setdefault(
        phase,
        {
            "id": phase,
            "name": str(event.get("name") or AGENT_PROFILES.get(phase, {}).get("name") or phase),
            "task": task_for_agent(run.get("plan") or [], phase),
            "content": "",
            "summary": "",
            "evidence": "",
            "risks": "",
            "full_output": "",
        },
    )
    if event.get("type") == "agent":
        item["name"] = str(event.get("name") or item.get("name") or phase)
        item["status"] = str(event.get("status") or item.get("status") or "")
        item["text"] = str(event.get("text") or item.get("text") or "")
        if "durationMs" in event:
            item["duration_ms"] = event.get("durationMs")
    elif event.get("type") == "agent_delta":
        item["content"] = str(item.get("content") or "") + str(event.get("text") or "")
        parsed = parse_structured_agent_output(str(item.get("content") or ""))
        item.update(parsed)
    elif event.get("type") == "agent_reasoning":
        item["reasoning"] = str(item.get("reasoning") or "") + str(event.get("text") or "")
    elif event.get("type") == "agent_note":
        notes = item.get("notes") if isinstance(item.get("notes"), list) else []
        notes.append(str(event.get("text") or ""))
        item["notes"] = notes[-20:]


def normalize_agent_preset(value: Any) -> str:
    preset = str(value or "full").strip().lower()
    return preset if preset in {"full", "auto", "code", "research", "reason", "critic", "leader"} else "full"


def plan_for_preset(payload: dict[str, Any], agent_preset: str, emit_event: Callable[[dict[str, Any]], None]) -> tuple[list[dict[str, Any]], str]:
    preset = normalize_agent_preset(agent_preset)
    if preset == "leader":
        return plan_agents(payload, emit_event), "Leader 自动拆解"
    if preset == "auto":
        return auto_agent_plan(payload, emit_event)
    if preset == "code":
        return [
            {"id": "coder", "task": "检查代码、实现路径和工程风险"},
            {"id": "reasoner", "task": "分析边界条件和架构取舍"},
            {"id": "critic", "task": "复核漏洞、遗漏和反例", "depends_on": ["coder", "reasoner"]},
        ], "自动启用 Coder + Reasoner + Critic"
    if preset == "research":
        return [
            {"id": "researcher", "task": "检索资料、事实和来源"},
            {"id": "critic", "task": "复核来源可靠性和不确定点", "depends_on": ["researcher"]},
        ], "自动启用 Researcher + Critic"
    if preset == "reason":
        return [
            {"id": "reasoner", "task": "梳理推理链路、边界条件和方案权衡"},
            {"id": "critic", "task": "检查风险、遗漏和反例", "depends_on": ["reasoner"]},
        ], "自动启用 Reasoner + Critic"
    if preset == "critic":
        return [{"id": "critic", "task": "审查现有想法的风险、漏洞和遗漏"}], "仅启用 Critic"
    return default_agent_plan(), "完整 4-Agent"


_AUTO_CODE_TOKENS = ("```", "bug", "报错", "代码", "实现", "接口", "项目", "函数", "class ")
_AUTO_RESEARCH_TOKENS = ("最新", "新闻", "搜索", "资料", "来源", "网页", "引用", "今天")
_AUTO_REASON_TOKENS = ("方案", "架构", "权衡", "复杂", "规划")


def auto_signal_categories(query: str) -> set[str]:
    """Cheap keyword heuristic: which intent categories does the query signal?"""
    lowered = query.lower()
    categories: set[str] = set()
    if any(token in lowered for token in _AUTO_CODE_TOKENS):
        categories.add("code")
    if any(token in lowered for token in _AUTO_RESEARCH_TOKENS):
        categories.add("research")
    if len(lowered) > 500 or any(token in lowered for token in _AUTO_REASON_TOKENS):
        categories.add("reason")
    return categories


def auto_agent_plan(
    payload: dict[str, Any],
    emit_event: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Pick an Agent plan for the "auto" preset.

    A single clear keyword signal maps straight to its static preset (cheap, no
    extra LLM call — the original fast path). When the query gives *no* signal or
    *conflicting* signals (e.g. looks like both code and research), the old
    first-match-wins guess was unreliable and the no-signal fallback was a lone
    critic with nothing to critique; instead we defer to the LLM planner
    (:func:`plan_agents`) to actually decompose the task.
    """
    categories = auto_signal_categories(latest_user_query(payload))
    if len(categories) == 1:
        return plan_for_preset(payload, next(iter(categories)), lambda _event: None)
    plan = plan_agents(payload, emit_event)
    return plan, "Leader 自动拆解"


def should_confirm_plan(payload: dict[str, Any], *, confirm_plan: bool, agent_preset: str) -> bool:
    if confirm_plan:
        return True
    if normalize_agent_preset(agent_preset) == "auto":
        return True
    query = latest_user_query(payload)
    return len(query) > 1800


def _event_emitter(run_id: str) -> Callable[[dict[str, Any]], None]:
    """包一个返回 None 的事件回调；append_event 返回 dict，直接当 emit_event 传会与
    Callable[..., None] 形参不符。这里只触发副作用、丢弃返回值。"""

    def emit(event: dict[str, Any]) -> None:
        append_event(run_id, event)

    return emit


def start_planned_run(run_id: str, runtime_payload: dict[str, Any], *, confirm_plan: bool, agent_preset: str) -> None:
    try:
        validate_deepseek_payload(runtime_payload)
        append_status(run_id, "planning")
        selected_model = normalize_model_name(runtime_payload.get("model") or DEFAULT_MODEL)
        user_query = latest_user_query(runtime_payload)
        leader_plan_started = time.monotonic()
        append_agent_event(run_id, phase="leader", status="running", name="Leader", text="正在拆解问题并分配 Agent...")
        plan, label = plan_for_preset(runtime_payload, agent_preset, _event_emitter(run_id))
        append_event(run_id, {"type": "agent_plan", "plan": plan, "label": label})
        append_agent_event(
            run_id,
            phase="leader",
            status="done",
            name="Leader",
            text=leader_done_text(plan),
            duration_ms=elapsed_ms(leader_plan_started),
        )
        if should_confirm_plan(runtime_payload, confirm_plan=confirm_plan, agent_preset=agent_preset):
            append_status(run_id, "awaiting_plan", label=label)
            return
        append_status(run_id, "running")
        execute_plan(run_id, runtime_payload, plan, selected_model=selected_model, user_query=user_query)
    except RequestCancelled:
        append_status(run_id, "cancelled")
    except Exception as exc:
        append_event(run_id, {"type": "error", "error": str(exc), "code": ErrorCode.INTERNAL.value})


def continue_with_plan(run_id: str, runtime_payload: dict[str, Any], plan: list[dict[str, Any]] | None = None) -> None:
    try:
        validate_deepseek_payload(runtime_payload)
        selected_model = normalize_model_name(runtime_payload.get("model") or DEFAULT_MODEL)
        user_query = latest_user_query(runtime_payload)
        approved_plan = safe_agent_plan({"agents": plan or load_run(run_id).get("plan") or default_agent_plan()})
        append_event(run_id, {"type": "agent_plan", "plan": approved_plan, "label": "用户确认计划"})
        reset_final_answer(run_id, "confirm_plan")
        append_status(run_id, "running")
        execute_plan(run_id, runtime_payload, approved_plan, selected_model=selected_model, user_query=user_query)
    except Exception as exc:
        append_event(run_id, {"type": "error", "error": str(exc), "code": ErrorCode.INTERNAL.value})


def execute_plan(
    run_id: str,
    runtime_payload: dict[str, Any],
    plan: list[dict[str, Any]],
    *,
    selected_model: str,
    user_query: str,
) -> None:
    search_budget = new_agent_search_budget()
    token_budget = new_agent_token_budget()
    stream_agent_plan(
        runtime_payload,
        plan,
        selected_model=selected_model,
        user_query=user_query,
        search_budget=search_budget,
        emit_event=_event_emitter(run_id),
        token_budget=token_budget,
    )


RESUMABLE_STATUSES = {"orphaned", "failed", "cancelled", "done"}


def resumable_completed_outputs(run: dict[str, Any], plan: list[dict[str, Any]], node_states: dict[str, Any]) -> list[dict[str, Any]]:
    """Durable outputs of nodes that already succeeded, in plan order, for resume."""
    raw_outputs = run.get("agentOutputs")
    outputs = raw_outputs if isinstance(raw_outputs, dict) else {}
    result: list[dict[str, Any]] = []
    for node_id in completed_node_ids(plan, node_states):
        output = outputs.get(node_id)
        if isinstance(output, dict):
            result.append(output)
    return result


def resume_run(run_id: str, runtime_payload: dict[str, Any]) -> None:
    """Resume an interrupted run from its last checkpoint (断点续跑).

    Succeeded nodes are re-derived from the event log and skipped (idempotent);
    only the incomplete DAG nodes re-run, then synthesis runs once. If every
    planned node already succeeded, only the final synthesis is (re)done.
    """
    try:
        validate_deepseek_payload(runtime_payload)
        run = load_run(run_id)
        selected_model = normalize_model_name(runtime_payload.get("model") or DEFAULT_MODEL)
        user_query = latest_user_query(runtime_payload)
        plan = safe_agent_plan({"agents": run.get("plan") or default_agent_plan()})
        node_states = reduce_node_states(plan, run.get("events") or [])
        incomplete = incomplete_plan_nodes(plan, node_states)
        append_status(run_id, "running", reason="resume")
        if not incomplete:
            if str(run.get("finalAnswer") or "").strip():
                append_status(run_id, "done")
                return
            reset_final_answer(run_id, "resume")
            resynthesize_outputs(run_id, runtime_payload, selected_model=selected_model, user_query=user_query)
            return
        completed_outputs = resumable_completed_outputs(run, plan, node_states)
        # Re-open incomplete nodes so a replay shows them retrying, then re-run only those.
        for item in incomplete:
            append_event(run_id, {"type": "agent_reset", "phase": item["id"], "reason": "resume"})
        reset_final_answer(run_id, "resume")
        search_budget = new_agent_search_budget()
        token_budget = new_agent_token_budget()
        stream_agent_plan(
            runtime_payload,
            plan,
            selected_model=selected_model,
            user_query=user_query,
            search_budget=search_budget,
            emit_event=_event_emitter(run_id),
            token_budget=token_budget,
            completed_outputs=completed_outputs,
        )
    except RequestCancelled:
        append_status(run_id, "cancelled")
    except Exception as exc:
        append_event(run_id, {"type": "error", "error": str(exc), "code": ErrorCode.INTERNAL.value})


def rerun_agent(run_id: str, runtime_payload: dict[str, Any], *, agent_id: str, resynthesize: bool = True) -> None:
    try:
        validate_deepseek_payload(runtime_payload)
        selected_model = normalize_model_name(runtime_payload.get("model") or DEFAULT_MODEL)
        user_query = latest_user_query(runtime_payload)
        agent_id = "synthesizer" if agent_id in {"synth", "leader", "synthesizer"} else agent_id
        append_status(run_id, "running", reason="rerun")
        if agent_id == "synthesizer":
            reset_final_answer(run_id, "rerun_synthesizer")
            resynthesize_outputs(run_id, runtime_payload, selected_model=selected_model, user_query=user_query)
            return
        if agent_id not in AGENT_PROFILES:
            raise AppError("Unknown Agent", code=ErrorCode.INVALID_PAYLOAD)

        run = load_run(run_id)
        plan = safe_agent_plan({"agents": run.get("plan") or default_agent_plan()})
        task = task_for_agent(plan, agent_id) or "重新运行该 Agent 并给出公开摘要"
        prior_outputs = prior_outputs_for_agent(run, agent_id)
        append_event(run_id, {"type": "agent_reset", "phase": agent_id, "reason": "rerun_agent"})
        profile = AGENT_PROFILES[agent_id]
        started = time.monotonic()
        append_agent_event(run_id, phase=agent_id, status="running", name=profile["name"], text=f"正在重新运行：{task}")
        search_budget = new_agent_search_budget()
        try:
            output = run_agent(
                runtime_payload,
                agent_id=agent_id,
                task=task,
                search_budget=search_budget,
                prior_outputs=prior_outputs,
                emit_event=_event_emitter(run_id),
            )
            output["duration_ms"] = elapsed_ms(started)
            append_event(run_id, {"type": "agent_output", "phase": agent_id, "output": output})
            append_agent_event(run_id, phase=agent_id, status="done", name=output["name"], text="已完成重新运行", duration_ms=output["duration_ms"])
        except Exception as exc:
            output = failed_agent_output(agent_id, task, exc)
            output["duration_ms"] = elapsed_ms(started)
            append_event(run_id, {"type": "agent_output", "phase": agent_id, "output": output})
            append_agent_event(run_id, phase=agent_id, status="error", name=profile["name"], text=output["content"], duration_ms=output["duration_ms"])
        append_event(
            run_id,
            {
                "type": "agent_note",
                "phase": agent_id,
                "name": profile["name"],
                "text": "其他 Agent 未自动重跑；最终回答将基于最新该 Agent 与现有其他 Agent 输出重新综合。",
            },
        )
        if resynthesize:
            reset_final_answer(run_id, "rerun_agent")
            resynthesize_outputs(run_id, runtime_payload, selected_model=selected_model, user_query=user_query)
        else:
            append_status(run_id, "done")
    except Exception as exc:
        append_event(run_id, {"type": "error", "error": str(exc), "code": ErrorCode.INTERNAL.value})


def reset_final_answer(run_id: str, reason: str) -> None:
    append_event(run_id, {"type": "final_reset", "scope": "final_answer", "reason": reason})


def resynthesize_outputs(run_id: str, runtime_payload: dict[str, Any], *, selected_model: str, user_query: str) -> None:
    run = load_run(run_id)
    agent_outputs = outputs_in_plan_order(run)
    if not agent_outputs:
        agent_outputs = list((run.get("agentOutputs") or {}).values())
    if not agent_outputs:
        raise AppError("No Agent outputs available for synthesis", code=ErrorCode.INVALID_PAYLOAD)
    search_budget = SearchBudget(
        total_limit=MULTI_AGENT_TOTAL_SEARCH_LIMIT,
        per_key_limit=MULTI_AGENT_PER_AGENT_SEARCH_LIMIT,
    )
    stream_synthesis_for_outputs(
        runtime_payload,
        selected_model,
        user_query,
        agent_outputs,
        search_budget=search_budget,
        emit_event=_event_emitter(run_id),
    )


def append_agent_event(run_id: str, **kwargs: Any) -> None:
    emit_agent_event(_event_emitter(run_id), **kwargs)


def task_for_agent(plan: list[Any], agent_id: str) -> str:
    for item in plan:
        if isinstance(item, dict) and item.get("id") == agent_id:
            return str(item.get("task") or "")
    return ""


def outputs_in_plan_order(run: dict[str, Any]) -> list[dict[str, Any]]:
    raw_outputs = run.get("agentOutputs")
    outputs = raw_outputs if isinstance(raw_outputs, dict) else {}
    plan = safe_agent_plan({"agents": run.get("plan") or []})
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in plan:
        agent_id = item["id"]
        output = outputs.get(agent_id)
        if isinstance(output, dict):
            ordered.append(output)
            seen.add(agent_id)
    for agent_id, output in outputs.items():
        if agent_id not in seen and isinstance(output, dict):
            ordered.append(output)
    return ordered


def prior_outputs_for_agent(run: dict[str, Any], agent_id: str) -> list[dict[str, Any]]:
    plan = safe_agent_plan({"agents": run.get("plan") or []})
    agent_outputs = run.get("agentOutputs")
    outputs = agent_outputs if isinstance(agent_outputs, dict) else {}
    prior_ids: list[str] = []
    for layer in layered_plan(plan):
        layer_ids = [str(item.get("id") or "") for item in layer]
        if agent_id in layer_ids:
            return [outputs[aid] for aid in prior_ids if isinstance(outputs.get(aid), dict)]
        prior_ids.extend(aid for aid in layer_ids if aid)

    # Target is not in the current plan snapshot; preserve the old plan-order fallback.
    ordered = outputs_in_plan_order(run)
    prior: list[dict[str, Any]] = []
    for output in ordered:
        if output.get("id") == agent_id:
            break
        prior.append(output)
    return prior


def mark_orphan_runs_on_startup() -> int:
    count = 0
    if not AGENT_RUNS_DIR.exists():
        return count
    for path in AGENT_RUNS_DIR.glob("run_*.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(run, dict) or run.get("status") not in ORPHANABLE_STATUSES:
            continue
        try:
            append_status(str(run.get("runId") or path.stem), "orphaned", reason="server_restart")
            count += 1
        except Exception:
            continue
    return count


def resume_orphaned_runs() -> int:
    """Opt-in auto-resume of orphaned runs on startup (AGENT_RUNTIME_AUTO_RESUME).

    Default OFF: returns 0 without touching anything, so a restart never silently
    spends upstream tokens. When enabled, each orphaned run is resumed from its
    last checkpoint using its (credential-free) stored payload — which requires a
    server-side ``DEEPSEEK_API_KEY``; runs missing one surface an error event.
    """
    if not AGENT_RUNTIME_AUTO_RESUME:
        return 0
    count = 0
    if not AGENT_RUNS_DIR.exists():
        return count
    for path in AGENT_RUNS_DIR.glob("run_*.json"):
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(run, dict) or run.get("status") != "orphaned":
            continue
        run_id = str(run.get("runId") or path.stem)
        stored_payload = run.get("requestPayload")
        runtime_payload = stored_payload if isinstance(stored_payload, dict) else {}
        if registry.ensure_started(run_id, resume_run, run_id, runtime_payload):
            count += 1
    return count


def elapsed_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))
