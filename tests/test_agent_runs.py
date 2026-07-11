from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import deepseek_infra.infra.agent_runtime.agent_runs as agent_runs


def valid_payload(**extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "apiKey": "secret-key",
        "tavilyApiKey": "secret-search",
        "model": "deepseek-v4-pro",
        "stream": True,
        "messages": [{"role": "user", "content": "请分析这个任务"}],
    }
    payload.update(extra)
    return payload


def test_create_run_sanitizes_private_keys_and_appends_indexed_events(tmp_settings) -> None:
    run = agent_runs.create_run(valid_payload(nested={"apiKey": "nested-secret"}), conversation_id="c1", message_id="m1")
    run_id = run["runId"]

    assert "requestPayload" not in run
    path = agent_runs.AGENT_RUNS_DIR / f"{run_id}.json"
    raw = path.read_text(encoding="utf-8")
    assert "secret-key" not in raw
    assert "secret-search" not in raw
    assert "nested-secret" not in raw

    first = agent_runs.append_event(run_id, {"type": "content", "text": "hello"})
    second = agent_runs.append_event(run_id, {"type": "done", "content": "hello", "diagnostics": {"ok": True}})
    stored = agent_runs.load_run(run_id)

    assert first["index"] == 0
    assert second["index"] == 1
    assert stored["nextIndex"] == 2
    assert stored["finalAnswer"] == "hello"
    assert stored["status"] == "done"
    assert stored["diagnostics"] == {"ok": True}
    assert [event["index"] for event in agent_runs.events_after(run_id, 0)] == [1]


def test_reset_events_keep_snapshots_derived_from_event_log(tmp_settings) -> None:
    run_id = agent_runs.create_run(valid_payload())["runId"]

    agent_runs.append_event(run_id, {"type": "agent_delta", "phase": "coder", "name": "Coder", "text": "old output"})
    agent_runs.append_event(run_id, {"type": "content", "text": "old final"})
    assert agent_runs.load_run(run_id)["agentOutputs"]["coder"]["content"] == "old output"
    assert agent_runs.load_run(run_id)["finalAnswer"] == "old final"

    agent_reset = agent_runs.append_event(run_id, {"type": "agent_reset", "phase": "coder", "reason": "rerun_agent"})
    final_reset = agent_runs.append_event(run_id, {"type": "final_reset", "scope": "final_answer", "reason": "rerun_synthesizer"})
    stored = agent_runs.load_run(run_id)

    assert agent_reset["runId"] == run_id
    assert final_reset["scope"] == "final_answer"
    assert "coder" not in stored["agentOutputs"]
    assert stored["finalAnswer"] == ""


def test_replace_with_retry_handles_transient_windows_lock(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    source = agent_runs.AGENT_RUNS_DIR / "retry-source.tmp"
    target = agent_runs.AGENT_RUNS_DIR / "retry-target.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text('{"ok": true}', encoding="utf-8")
    attempts = {"count": 0}
    original_replace = Path.replace

    def flaky_replace(self: Path, target_path: Path) -> Path:
        if Path(self) == source and Path(target_path) == target and attempts["count"] < 2:
            attempts["count"] += 1
            raise PermissionError("locked")
        return original_replace(self, target_path)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    agent_runs.replace_with_retry(source, target, delays=(0.0, 0.0, 0.0))

    assert attempts["count"] == 2
    assert not source.exists()
    assert target.read_text(encoding="utf-8") == '{"ok": true}'


def test_parallel_event_writes_leave_no_shared_tmp_files(tmp_settings) -> None:
    run_id = agent_runs.create_run(valid_payload())["runId"]
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            agent_runs.append_event(run_id, {"type": "agent_note", "phase": f"worker-{index}", "text": f"note {index}"})
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    stored = agent_runs.load_run(run_id)

    assert errors == []
    assert stored["nextIndex"] == 12
    assert [event["index"] for event in stored["events"]] == list(range(12))
    assert not list(agent_runs.AGENT_RUNS_DIR.glob("*.tmp"))


def test_registry_prevents_duplicate_start_and_allows_waiters_to_attach() -> None:
    registry = agent_runs.AgentRunRegistry()
    entered = threading.Event()
    release = threading.Event()

    def target() -> None:
        entered.set()
        release.wait(timeout=2)

    assert registry.ensure_started("run_test12345678", target) is True
    assert entered.wait(timeout=1)
    assert registry.ensure_started("run_test12345678", target) is False

    notified = threading.Event()

    def waiter() -> None:
        registry.wait_for_event("run_test12345678", timeout=1)
        notified.set()

    wait_thread = threading.Thread(target=waiter)
    wait_thread.start()
    registry.notify_event("run_test12345678")
    wait_thread.join(timeout=2)
    release.set()

    assert notified.is_set()


def test_startup_marks_leftover_running_runs_as_orphaned(tmp_settings) -> None:
    run_id = agent_runs.create_run(valid_payload())["runId"]
    agent_runs.append_status(run_id, "running")

    assert agent_runs.mark_orphan_runs_on_startup() == 1

    stored = agent_runs.load_run(run_id)
    assert stored["status"] == "orphaned"
    assert stored["events"][-1]["reason"] == "server_restart"


def test_confirm_plan_policy_prefers_fast_path_unless_requested_or_auto() -> None:
    payload = valid_payload()

    assert agent_runs.should_confirm_plan(payload, confirm_plan=False, agent_preset="full") is False
    assert agent_runs.should_confirm_plan(payload, confirm_plan=True, agent_preset="full") is True
    assert agent_runs.should_confirm_plan(payload, confirm_plan=False, agent_preset="auto") is True


def test_auto_signal_categories_detects_each_intent() -> None:
    assert agent_runs.auto_signal_categories("帮我修这个 bug，代码报错了") == {"code"}
    assert agent_runs.auto_signal_categories("查一下最新新闻和资料来源") == {"research"}
    assert agent_runs.auto_signal_categories("这个架构方案怎么权衡") == {"reason"}
    assert agent_runs.auto_signal_categories("用代码实现一个能搜索最新新闻的接口") == {"code", "research"}
    assert agent_runs.auto_signal_categories("你好呀") == set()


def test_agent_run_static_presets_include_explicit_dependencies() -> None:
    payload = valid_payload()

    full, _ = agent_runs.plan_for_preset(payload, "full", lambda _event: None)
    code, _ = agent_runs.plan_for_preset(payload, "code", lambda _event: None)
    research, _ = agent_runs.plan_for_preset(payload, "research", lambda _event: None)
    reason, _ = agent_runs.plan_for_preset(payload, "reason", lambda _event: None)

    full_by_id = {item["id"]: item for item in full}
    assert full_by_id["coder"]["depends_on"] == ["researcher"]
    assert full_by_id["reasoner"]["depends_on"] == ["researcher"]
    assert full_by_id["critic"]["depends_on"] == ["researcher", "coder", "reasoner"]
    assert code[-1]["depends_on"] == ["coder", "reasoner"]
    assert research[-1]["depends_on"] == ["researcher"]
    assert reason[-1]["depends_on"] == ["reasoner"]


def test_start_planned_run_confirmation_snapshot_keeps_preset_dependencies(tmp_settings) -> None:
    run_id = agent_runs.create_run(valid_payload(), confirm_plan=True, agent_preset="code")["runId"]

    agent_runs.start_planned_run(run_id, valid_payload(), confirm_plan=True, agent_preset="code")

    stored = agent_runs.load_run(run_id)
    assert stored["status"] == "awaiting_plan"
    assert stored["plan"][-1]["id"] == "critic"
    assert stored["plan"][-1]["depends_on"] == ["coder", "reasoner"]
    assert any(event.get("type") == "agent_plan" and event.get("plan", [])[-1].get("depends_on") for event in stored["events"])


def test_auto_agent_plan_single_signal_uses_preset_without_llm() -> None:
    payload = {"messages": [{"role": "user", "content": "帮我修这个 bug，代码报错了"}]}
    with patch.object(agent_runs, "plan_agents") as mock_plan:
        plan, label = agent_runs.auto_agent_plan(payload, lambda _event: None)

    # 单一明确信号走静态 preset，绝不触发 LLM planner（保持 auto 的廉价快路径）
    mock_plan.assert_not_called()
    assert [item["id"] for item in plan] == ["coder", "reasoner", "critic"]
    assert plan[-1]["depends_on"] == ["coder", "reasoner"]


def test_auto_agent_plan_no_signal_delegates_to_llm_planner() -> None:
    payload = {"messages": [{"role": "user", "content": "你好呀"}]}
    fake_plan = [{"id": "reasoner", "task": "拆解"}]
    with patch.object(agent_runs, "plan_agents", return_value=fake_plan) as mock_plan:
        plan, label = agent_runs.auto_agent_plan(payload, lambda _event: None)

    mock_plan.assert_called_once()
    assert plan == fake_plan
    assert label == "Leader 自动拆解"


def test_auto_agent_plan_conflicting_signals_delegate_to_llm_planner() -> None:
    payload = {"messages": [{"role": "user", "content": "用代码实现一个能搜索最新新闻的接口"}]}
    fake_plan = [{"id": "researcher", "task": "查"}, {"id": "coder", "task": "写"}]
    with patch.object(agent_runs, "plan_agents", return_value=fake_plan) as mock_plan:
        plan, label = agent_runs.auto_agent_plan(payload, lambda _event: None)

    # 多个冲突信号时旧的 first-match-wins 不可靠，改交给 LLM 真正拆解
    mock_plan.assert_called_once()
    assert plan == fake_plan


def test_rerun_agent_resets_phase_and_final_answer_without_cascading(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = agent_runs.create_run(valid_payload())["runId"]
    agent_runs.append_event(run_id, {"type": "agent_plan", "plan": [{"id": "coder", "task": "检查代码"}]})
    agent_runs.append_event(
        run_id,
        {
            "type": "agent_output",
            "phase": "coder",
            "output": {"id": "coder", "name": "Coder", "task": "检查代码", "content": "old", "full_output": "old"},
        },
    )
    agent_runs.append_event(run_id, {"type": "content", "text": "old final"})
    agent_runs.append_status(run_id, "done")

    def fake_run_agent(*args, **kwargs):
        return {
            "id": "coder",
            "name": "Coder",
            "task": "检查代码",
            "content": "new coder output",
            "summary": "new",
            "evidence": "",
            "risks": "",
            "full_output": "new coder output",
            "usage": {},
        }

    def fake_synthesis(*args, emit_event, **kwargs) -> None:
        emit_event({"type": "content", "text": "new final"})
        emit_event({"type": "done", "content": "new final", "diagnostics": {"agentCount": 1}})

    monkeypatch.setattr(agent_runs, "run_agent", fake_run_agent)
    monkeypatch.setattr(agent_runs, "stream_synthesis_for_outputs", fake_synthesis)

    agent_runs.rerun_agent(run_id, valid_payload(), agent_id="coder", resynthesize=True)
    stored = agent_runs.load_run(run_id)
    event_types = [event["type"] for event in stored["events"]]

    assert "agent_reset" in event_types
    assert "final_reset" in event_types
    assert stored["agentOutputs"]["coder"]["content"] == "new coder output"
    assert stored["finalAnswer"] == "new final"
    assert stored["status"] == "done"
    assert any("未自动重跑" in event.get("text", "") for event in stored["events"])


def test_rerun_agent_prior_outputs_follow_dag_layers_not_plan_order(tmp_settings) -> None:
    run_id = agent_runs.create_run(valid_payload())["runId"]
    agent_runs.append_event(
        run_id,
        {
            "type": "agent_plan",
            "plan": [
                {"id": "coder", "task": "写实现", "depends_on": ["reasoner"]},
                {"id": "reasoner", "task": "先推理"},
            ],
        },
    )
    agent_runs.append_event(
        run_id,
        {
            "type": "agent_output",
            "phase": "reasoner",
            "output": {"id": "reasoner", "name": "Reasoner", "task": "先推理", "content": "reasoned"},
        },
    )
    agent_runs.append_event(
        run_id,
        {
            "type": "agent_output",
            "phase": "coder",
            "output": {"id": "coder", "name": "Coder", "task": "写实现", "content": "coded"},
        },
    )

    prior = agent_runs.prior_outputs_for_agent(agent_runs.load_run(run_id), "coder")

    assert [item["id"] for item in prior] == ["reasoner"]


def test_append_event_persists_node_state_snapshot(tmp_settings) -> None:
    run_id = agent_runs.create_run(valid_payload())["runId"]
    plan = [
        {"id": "researcher", "task": "find"},
        {"id": "coder", "task": "code", "depends_on": ["researcher"]},
    ]
    agent_runs.append_event(run_id, {"type": "agent_plan", "plan": plan})
    agent_runs.append_event(
        run_id,
        {"type": "agent_output", "phase": "researcher", "output": {"id": "researcher", "name": "R", "content": "ok"}},
    )

    nodes = agent_runs.load_run(run_id)["nodes"]
    assert nodes["researcher"]["state"] == "succeeded"
    # coder's dependency is now satisfied -> queued (but not yet running).
    assert nodes["coder"]["state"] == "queued"


def _seed_run_with_outputs(run_id: str, *, coder_failed: bool) -> None:
    plan = [
        {"id": "researcher", "task": "find"},
        {"id": "coder", "task": "code", "depends_on": ["researcher"]},
        {"id": "critic", "task": "review", "depends_on": ["researcher", "coder"]},
    ]
    agent_runs.append_event(run_id, {"type": "agent_plan", "plan": plan})
    agent_runs.append_event(
        run_id,
        {"type": "agent_output", "phase": "researcher", "output": {"id": "researcher", "name": "R", "content": "facts"}},
    )
    if coder_failed:
        agent_runs.append_event(run_id, {"type": "agent", "phase": "coder", "status": "error"})
        agent_runs.append_event(
            run_id,
            {"type": "agent_output", "phase": "coder", "output": {"id": "coder", "name": "C", "content": "boom", "failed": True}},
        )


def test_resume_run_skips_completed_and_reruns_incomplete(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = agent_runs.create_run(valid_payload())["runId"]
    _seed_run_with_outputs(run_id, coder_failed=True)
    agent_runs.append_status(run_id, "orphaned", reason="server_restart")

    captured: dict[str, object] = {}

    def fake_stream_agent_plan(payload, plan, *, emit_event, completed_outputs=None, **kwargs):
        captured["completed"] = [item.get("id") for item in (completed_outputs or [])]
        captured["plan"] = [item.get("id") for item in plan]
        emit_event({"type": "content", "text": "resumed final"})
        emit_event({"type": "done", "content": "resumed final", "diagnostics": {"agentCount": 3}})

    monkeypatch.setattr(agent_runs, "stream_agent_plan", fake_stream_agent_plan)

    agent_runs.resume_run(run_id, valid_payload())
    stored = agent_runs.load_run(run_id)

    # Only the already-succeeded node is fed back as completed; failed/never-run re-run.
    assert captured["completed"] == ["researcher"]
    event_types = [event["type"] for event in stored["events"]]
    assert event_types.count("agent_reset") == 2  # coder + critic reopened
    reset_phases = sorted(event.get("phase") for event in stored["events"] if event.get("type") == "agent_reset")
    assert reset_phases == ["coder", "critic"]
    assert stored["finalAnswer"] == "resumed final"
    assert stored["status"] == "done"
    assert any(event.get("reason") == "resume" for event in stored["events"] if event.get("type") == "run_status")


def test_resume_run_resynthesizes_when_all_nodes_done_but_no_final_answer(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = agent_runs.create_run(valid_payload())["runId"]
    plan = [{"id": "researcher", "task": "find"}, {"id": "critic", "task": "review", "depends_on": ["researcher"]}]
    agent_runs.append_event(run_id, {"type": "agent_plan", "plan": plan})
    for agent_id in ("researcher", "critic"):
        agent_runs.append_event(
            run_id,
            {"type": "agent_output", "phase": agent_id, "output": {"id": agent_id, "name": agent_id, "content": "done"}},
        )
    agent_runs.append_status(run_id, "orphaned")

    calls: list[str] = []

    def fake_resynth(rid, payload, *, selected_model, user_query):
        calls.append("resynth")
        agent_runs.append_event(rid, {"type": "content", "text": "synth final"})
        agent_runs.append_event(rid, {"type": "done", "content": "synth final", "diagnostics": {}})

    def fail_stream(*args, **kwargs):  # pragma: no cover - asserted not called
        raise AssertionError("stream_agent_plan must not run when all nodes already succeeded")

    monkeypatch.setattr(agent_runs, "resynthesize_outputs", fake_resynth)
    monkeypatch.setattr(agent_runs, "stream_agent_plan", fail_stream)

    agent_runs.resume_run(run_id, valid_payload())
    stored = agent_runs.load_run(run_id)

    assert calls == ["resynth"]
    assert stored["finalAnswer"] == "synth final"
    assert stored["status"] == "done"


def test_resume_run_marks_done_when_already_complete(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = agent_runs.create_run(valid_payload())["runId"]
    plan = [{"id": "researcher", "task": "find"}]
    agent_runs.append_event(run_id, {"type": "agent_plan", "plan": plan})
    agent_runs.append_event(
        run_id, {"type": "agent_output", "phase": "researcher", "output": {"id": "researcher", "name": "R", "content": "x"}}
    )
    agent_runs.append_event(run_id, {"type": "content", "text": "existing answer"})
    agent_runs.append_status(run_id, "orphaned")

    def fail_stream(*args, **kwargs):  # pragma: no cover - asserted not called
        raise AssertionError("nothing should re-run")

    monkeypatch.setattr(agent_runs, "stream_agent_plan", fail_stream)
    monkeypatch.setattr(agent_runs, "resynthesize_outputs", fail_stream)

    agent_runs.resume_run(run_id, valid_payload())
    stored = agent_runs.load_run(run_id)

    assert stored["status"] == "done"
    assert stored["finalAnswer"] == "existing answer"


def test_resume_orphaned_runs_is_opt_in(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = agent_runs.create_run(valid_payload())["runId"]
    agent_runs.append_status(run_id, "orphaned", reason="server_restart")

    started: list[str] = []

    def fake_ensure_started(rid: str, *args: object, **kwargs: object) -> bool:
        started.append(rid)
        return True

    monkeypatch.setattr(agent_runs.registry, "ensure_started", fake_ensure_started)

    monkeypatch.setattr(agent_runs, "AGENT_RUNTIME_AUTO_RESUME", False)
    assert agent_runs.resume_orphaned_runs() == 0
    assert started == []

    monkeypatch.setattr(agent_runs, "AGENT_RUNTIME_AUTO_RESUME", True)
    assert agent_runs.resume_orphaned_runs() == 1
    assert started == [run_id]


def test_run_files_are_valid_json_snapshots(tmp_settings) -> None:
    run_id = agent_runs.create_run(valid_payload())["runId"]
    agent_runs.append_event(run_id, {"type": "agent_plan", "plan": [{"id": "critic", "task": "复核"}]})

    snapshot = json.loads((agent_runs.AGENT_RUNS_DIR / f"{run_id}.json").read_text(encoding="utf-8"))

    assert snapshot["runId"] == run_id
    assert snapshot["plan"] == [{"id": "critic", "task": "复核"}]
    assert snapshot["events"][0]["createdAt"].endswith("Z")


def test_agent_run_storage_rejects_invalid_missing_and_corrupt_snapshots(tmp_settings) -> None:
    with pytest.raises(agent_runs.AppError):
        agent_runs.validate_run_id("../bad")
    with pytest.raises(agent_runs.AppError):
        agent_runs.load_run("run_missing123456")

    run_id = "run_corrupt123456"
    path = agent_runs.AGENT_RUNS_DIR / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(agent_runs.AppError, match="corrupted"):
        agent_runs.load_run(run_id)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(agent_runs.AppError, match="corrupted"):
        agent_runs.load_run(run_id)


def test_replace_with_retry_raises_after_exhausting_windows_locks(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    source = agent_runs.AGENT_RUNS_DIR / "locked.tmp"
    target = agent_runs.AGENT_RUNS_DIR / "locked.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(PermissionError, match="locked"):
        agent_runs.replace_with_retry(source, target, delays=(0.0,))


def test_public_run_and_runtime_payload_handle_optional_shapes() -> None:
    assert agent_runs.merge_runtime_payload({"a": 1}, None) == {"a": 1}
    assert agent_runs.merge_runtime_payload({"a": 1}, {"a": 2}) == {"a": 2}
    assert agent_runs.public_run({"requestPayload": {"secret": 1}, "events": [1]}, include_events=False) == {}


def test_start_and_continue_plan_record_cancel_and_internal_failures(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled_id = agent_runs.create_run(valid_payload())["runId"]
    monkeypatch.setattr(agent_runs, "validate_deepseek_payload", lambda _payload: (_ for _ in ()).throw(agent_runs.RequestCancelled()))
    agent_runs.start_planned_run(cancelled_id, valid_payload(), confirm_plan=False, agent_preset="full")
    assert agent_runs.load_run(cancelled_id)["status"] == "cancelled"

    failed_id = agent_runs.create_run(valid_payload())["runId"]
    monkeypatch.setattr(agent_runs, "validate_deepseek_payload", lambda _payload: (_ for _ in ()).throw(RuntimeError("invalid runtime")))
    agent_runs.continue_with_plan(failed_id, valid_payload(), [])
    stored = agent_runs.load_run(failed_id)
    assert stored["events"][-1]["type"] == "error"


def test_rerun_agent_unknown_and_worker_failure_are_persisted(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    unknown_id = agent_runs.create_run(valid_payload())["runId"]
    agent_runs.rerun_agent(unknown_id, valid_payload(), agent_id="unknown")
    assert agent_runs.load_run(unknown_id)["events"][-1]["type"] == "error"

    run_id = agent_runs.create_run(valid_payload())["runId"]
    agent_runs.append_event(run_id, {"type": "agent_plan", "plan": [{"id": "coder", "task": "code"}]})
    monkeypatch.setattr(agent_runs, "run_agent", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("worker crashed")))
    agent_runs.rerun_agent(run_id, valid_payload(), agent_id="coder", resynthesize=False)
    stored = agent_runs.load_run(run_id)
    assert stored["agentOutputs"]["coder"]["failed"] is True
    assert stored["status"] == "done"


def test_resynthesize_requires_at_least_one_agent_output(tmp_settings) -> None:
    run_id = agent_runs.create_run(valid_payload())["runId"]
    with pytest.raises(agent_runs.AppError, match="No Agent outputs"):
        agent_runs.resynthesize_outputs(run_id, valid_payload(), selected_model="model", user_query="q")


def test_orphan_scans_skip_corrupt_unrelated_and_write_failures(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = agent_runs.AGENT_RUNS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run_bad.json").write_text("not json", encoding="utf-8")
    (directory / "run_done.json").write_text(json.dumps({"runId": "run_done", "status": "done"}), encoding="utf-8")
    (directory / "run_active.json").write_text(json.dumps({"runId": "run_active", "status": "running"}), encoding="utf-8")
    monkeypatch.setattr(agent_runs, "append_status", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")))
    assert agent_runs.mark_orphan_runs_on_startup() == 0
