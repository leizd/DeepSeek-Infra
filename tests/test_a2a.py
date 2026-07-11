from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterator
from unittest.mock import patch

import pytest

import deepseek_infra.infra.agent_runtime.a2a as a2a
from deepseek_infra.core.errors import AppError
from deepseek_infra.infra.agent_runtime.a2a import (
    A2A_PROTOCOL_VERSION,
    CANCELING,
    CANCELED,
    COMPLETED,
    FAILED,
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    TASK_NOT_CANCELABLE,
    TASK_NOT_FOUND,
    A2AClient,
    a2a_status,
    agent_card,
    agent_cards,
    get_task,
    handle_a2a_message,
    stream_message_events,
)
from deepseek_infra.infra.observability.metrics import render_prometheus
from deepseek_infra.infra.observability.observability import metrics_snapshot


@pytest.fixture(autouse=True)
def clean_task_store() -> Iterator[None]:
    with a2a._TASK_LOCK:
        a2a._TASKS.clear()
        a2a._TASK_CONDITIONS.clear()
        a2a._TASK_CANCEL_EVENTS.clear()
        a2a._STREAM_DISCONNECTS_TOTAL = 0
    yield
    with a2a._TASK_LOCK:
        a2a._TASKS.clear()
        a2a._TASK_CONDITIONS.clear()
        a2a._TASK_CANCEL_EVENTS.clear()
        a2a._STREAM_DISCONNECTS_TOTAL = 0


def rpc(method: str, params: dict[str, Any] | None = None, message_id: Any = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def send_params(text: str) -> dict[str, Any]:
    return {"message": {"role": "user", "parts": [{"kind": "text", "text": text}], "messageId": "msg_1", "kind": "message"}}


def task_state(task: dict[str, Any]) -> str:
    return str((task.get("status") or {}).get("state") or "")


def wait_for_state(task_id: str, states: set[str], timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = get_task(task_id)
        if task_state(task) in states:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} never reached {states}; last={task_state(get_task(task_id))}")


def test_agent_cards_cover_orchestrator_and_worker_roles() -> None:
    cards = agent_cards(base_url="http://127.0.0.1:8000")
    names = {card["url"].rsplit("/", 1)[-1] for card in cards}
    assert names == {"orchestrator", "researcher", "coder", "reasoner", "critic"}
    for card in cards:
        assert card["protocolVersion"] == A2A_PROTOCOL_VERSION
        assert card["capabilities"]["streaming"] is True
        assert card["skills"]
    researcher = agent_card("researcher", base_url="http://127.0.0.1:8000")
    assert "web_search" in researcher["skills"][0]["tags"]
    assert researcher["url"].endswith("/a2a/agents/researcher")
    with pytest.raises(AppError):
        agent_card("nope")


def test_message_send_executes_task_to_completion(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_call(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {"content": "四十二", "usage": {"prompt_tokens": 7, "completion_tokens": 3}}

    monkeypatch.setattr(a2a.deepseek_client, "call_deepseek", fake_call)
    response = handle_a2a_message(rpc("message/send", send_params("生命、宇宙以及一切的答案是什么？")), agent_id="reasoner")
    assert response is not None and "error" not in response
    task = response["result"]
    assert task["kind"] == "task"
    assert task["agentId"] == "reasoner"

    done = wait_for_state(str(task["id"]), {COMPLETED, FAILED})
    assert task_state(done) == COMPLETED
    assert done["artifacts"][0]["parts"][0]["text"] == "四十二"
    # History keeps the user message plus the agent answer.
    roles = [str(item.get("role")) for item in done["history"]]
    assert roles == ["user", "agent"]
    # The worker ran with the reasoner's capability slice (no tools) and profile.
    assert captured["capability"] == "reasoner"
    assert captured["allowedTools"] == []
    assert captured["toolsEnabled"] is False
    # Task snapshot persisted to the .a2a directory.
    assert (tmp_settings / ".a2a" / f"{task['id']}.json").is_file()


def test_message_send_rejects_empty_message() -> None:
    response = handle_a2a_message(rpc("message/send", {"message": {"parts": []}}), agent_id="reasoner")
    assert response is not None and response["error"]["code"] == INVALID_PARAMS


def test_tasks_get_honors_history_length(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a.deepseek_client, "call_deepseek", lambda payload: {"content": "ok", "usage": {}})
    task = handle_a2a_message(rpc("message/send", send_params("hi")), agent_id="coder")["result"]  # type: ignore[index]
    wait_for_state(str(task["id"]), {COMPLETED})
    trimmed = handle_a2a_message(rpc("tasks/get", {"id": task["id"], "historyLength": 1}))
    assert trimmed is not None
    assert len(trimmed["result"]["history"]) == 1
    full = handle_a2a_message(rpc("tasks/get", {"id": task["id"]}))
    assert full is not None
    assert len(full["result"]["history"]) == 2


def test_tasks_cancel_running_task_then_not_cancelable(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_call(payload: dict[str, Any]) -> dict[str, Any]:
        started.set()
        release.wait(5)
        return {"content": "late", "usage": {}}

    monkeypatch.setattr(a2a.deepseek_client, "call_deepseek", slow_call)
    task = handle_a2a_message(rpc("message/send", send_params("slow")), agent_id="reasoner")["result"]  # type: ignore[index]
    assert started.wait(5)
    cancelled = handle_a2a_message(rpc("tasks/cancel", {"id": task["id"]}))
    assert cancelled is not None
    assert task_state(cancelled["result"]) == CANCELING
    assert cancelled["result"]["cancelRequestedAt"]
    release.set()
    final = wait_for_state(str(task["id"]), {CANCELED})
    assert final["cancelRequestedAt"] == cancelled["result"]["cancelRequestedAt"]
    assert final["artifacts"] == []
    assert [chunk["artifact"]["name"] for chunk in final["artifactChunks"]] == ["progress"]
    again = handle_a2a_message(rpc("tasks/cancel", {"id": task["id"]}))
    assert again is not None and again["error"]["code"] == TASK_NOT_CANCELABLE


def test_task_errors_and_unknown_method() -> None:
    missing = handle_a2a_message(rpc("tasks/get", {"id": "task_does_not_exist"}))
    assert missing is not None and missing["error"]["code"] == TASK_NOT_FOUND
    unknown = handle_a2a_message(rpc("nope/method"))
    assert unknown is not None and unknown["error"]["code"] == METHOD_NOT_FOUND
    card = handle_a2a_message(rpc("agent/getAuthenticatedExtendedCard"), agent_id="critic", base_url="http://h")
    assert card is not None and card["result"]["url"].endswith("/a2a/agents/critic")


def test_upstream_failure_marks_task_failed(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(payload: dict[str, Any]) -> dict[str, Any]:
        raise AppError("Missing API key", status=401)

    monkeypatch.setattr(a2a.deepseek_client, "call_deepseek", boom)
    task = handle_a2a_message(rpc("message/send", send_params("hi")), agent_id="orchestrator")["result"]  # type: ignore[index]
    failed = wait_for_state(str(task["id"]), {FAILED})
    assert "Missing API key" in failed["status"]["message"]["parts"][0]["text"]


def test_restart_marks_disk_task_failed(tmp_settings) -> None:
    a2a.A2A_TASKS_DIR.mkdir(parents=True, exist_ok=True)
    stale = {"id": "task_stale01", "kind": "task", "status": {"state": "working", "timestamp": "t"}, "history": []}
    (a2a.A2A_TASKS_DIR / "task_stale01.json").write_text(json.dumps(stale), encoding="utf-8")
    recovered = get_task("task_stale01")
    assert task_state(recovered) == FAILED


def test_message_stream_emits_task_then_final_status(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a.deepseek_client, "call_deepseek", lambda payload: {"content": "answer", "usage": {}})
    events = []
    for chunk in stream_message_events(rpc("message/stream", send_params("hi")), agent_id="reasoner"):
        line = chunk.decode("utf-8").strip()
        assert line.startswith("data: ")
        events.append(json.loads(line[len("data: ") :]))
        if len(events) > 20:
            break
    first = events[0]["result"]
    assert first["kind"] == "task"
    kinds = [event["result"].get("kind") for event in events[1:]]
    assert "status-update" in kinds
    final_updates = [event["result"] for event in events if event["result"].get("final") is True]
    assert final_updates and final_updates[-1]["status"]["state"] == COMPLETED
    artifact_updates = [event["result"] for event in events if event["result"].get("kind") == "artifact-update"]
    assert len(artifact_updates) >= 2
    assert [update["chunkIndex"] for update in artifact_updates] == sorted(update["chunkIndex"] for update in artifact_updates)
    assert artifact_updates[0]["append"] is True
    assert artifact_updates[-1]["artifact"]["parts"][0]["text"] == "answer"
    assert artifact_updates[-1]["final"] is True


def test_tasks_resubscribe_replays_artifact_chunks_after_cursor(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a.deepseek_client, "call_deepseek", lambda payload: {"content": "answer", "usage": {}})
    task = handle_a2a_message(rpc("message/send", send_params("hi")), agent_id="reasoner")["result"]  # type: ignore[index]
    done = wait_for_state(str(task["id"]), {COMPLETED})
    assert [chunk["chunkIndex"] for chunk in done["artifactChunks"]] == [0, 1]

    events = []
    for chunk in stream_message_events(
        rpc("tasks/resubscribe", {"id": task["id"], "afterChunkIndex": 0}),
        agent_id="reasoner",
    ):
        events.append(json.loads(chunk.decode("utf-8").strip()[len("data: ") :]))
        if events[-1]["result"].get("final") is True:
            break

    artifact_updates = [event["result"] for event in events if event["result"].get("kind") == "artifact-update"]
    assert len(artifact_updates) == 1
    assert artifact_updates[0]["chunkIndex"] == 1
    assert artifact_updates[0]["artifactId"] == done["artifacts"][0]["artifactId"]
    assert artifact_updates[0]["artifact"]["parts"][0]["text"] == "answer"


def test_a2a_client_roundtrip_against_local_mesh(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a.deepseek_client, "call_deepseek", lambda payload: {"content": "delegated", "usage": {}})

    class _FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

        def __enter__(self) -> "_FakeResponse":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def loopback(request: Any, timeout: float = 0) -> _FakeResponse:
        message = json.loads(request.data.decode("utf-8"))
        response = handle_a2a_message(message, agent_id="orchestrator")
        return _FakeResponse(json.dumps(response).encode("utf-8"))

    client = A2AClient("http://127.0.0.1:9/a2a")
    with patch("urllib.request.urlopen", side_effect=loopback):
        task = client.send_message("请帮我评审这段代码")
        assert task["kind"] == "task"
        done = None
        for _ in range(200):
            done = client.get_task(str(task["id"]))
            if task_state(done) in {COMPLETED, FAILED}:
                break
            time.sleep(0.02)
        assert done is not None and task_state(done) == COMPLETED
        with pytest.raises(AppError):
            client.cancel_task(str(task["id"]))  # already terminal -> JSON-RPC error -> AppError


def test_a2a_client_stream_roundtrip_against_local_mesh(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a.deepseek_client, "call_deepseek", lambda payload: {"content": "streamed", "usage": {}})

    class _FakeStreamResponse:
        def __init__(self, payload: bytes) -> None:
            self._lines = payload.splitlines(keepends=True)

        def __iter__(self) -> Iterator[bytes]:
            return iter(self._lines)

        def __enter__(self) -> "_FakeStreamResponse":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    def loopback(request: Any, timeout: float = 0) -> _FakeStreamResponse:
        message = json.loads(request.data.decode("utf-8"))
        payload = b"".join(stream_message_events(message, agent_id="reasoner"))
        return _FakeStreamResponse(payload)

    client = A2AClient("http://127.0.0.1:9/a2a/agents/reasoner")
    with patch("urllib.request.urlopen", side_effect=loopback):
        events = list(client.message_stream("please stream"))
    artifact_updates = [event["result"] for event in events if event["result"].get("kind") == "artifact-update"]
    assert artifact_updates[-1]["artifact"]["parts"][0]["text"] == "streamed"
    assert artifact_updates[-1]["chunkIndex"] == 1


def test_a2a_status_shape(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a.deepseek_client, "call_deepseek", lambda payload: {"content": "x", "usage": {}})
    task = handle_a2a_message(rpc("message/send", send_params("hi")), agent_id="critic")["result"]  # type: ignore[index]
    wait_for_state(str(task["id"]), {COMPLETED})
    status = a2a_status()
    assert status["enabled"] is True
    assert status["protocolVersion"] == A2A_PROTOCOL_VERSION
    assert set(status["agents"]) == {"orchestrator", "researcher", "coder", "reasoner", "critic"}
    assert status["tasksByState"].get(COMPLETED) == 1
    assert status["activeTasks"] == 0
    assert status["streamDisconnectsTotal"] == 0
    assert status["agentCardPath"] == "/.well-known/agent-card.json"


def test_a2a_trace_and_prometheus_metrics(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a.deepseek_client, "call_deepseek", lambda payload: {"content": "metric", "usage": {"total_tokens": 3}})
    task = handle_a2a_message(rpc("message/send", send_params("hi")), agent_id="critic")["result"]  # type: ignore[index]
    wait_for_state(str(task["id"]), {COMPLETED})
    deadline = time.time() + 5
    snapshot = metrics_snapshot()
    while snapshot["a2a_tasks_total"] < 1 and time.time() < deadline:
        time.sleep(0.02)
        snapshot = metrics_snapshot()
    assert snapshot["a2a_tasks_total"] >= 1
    assert snapshot["a2a_task_latency_ms_avg"] >= 0
    prometheus = render_prometheus()
    assert "ai_a2a_tasks_total" in prometheus
    assert "ai_a2a_active_tasks" in prometheus


def test_a2a_disk_and_persistence_failures_are_best_effort(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = "task_corrupt01"
    path = a2a.A2A_TASKS_DIR / f"{task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert a2a._load_task_from_disk(task_id) is None
    path.write_text("[]", encoding="utf-8")
    assert a2a._load_task_from_disk(task_id) is None

    monkeypatch.setattr(a2a, "_task_path", lambda _task_id: path)
    monkeypatch.setattr(path.__class__, "write_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    a2a._persist_task({"id": task_id})


def test_a2a_task_eviction_removes_only_old_terminal_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a, "A2A_MAX_TASKS", 2)
    with a2a._TASK_LOCK:
        a2a._TASKS.update(
            {
                "task_old": {"createdAt": "1", "status": {"state": COMPLETED}},
                "task_active": {"createdAt": "2", "status": {"state": "working"}},
                "task_new": {"createdAt": "3", "status": {"state": FAILED}},
            }
        )
        a2a._TASK_CONDITIONS["task_old"] = threading.Condition()
        a2a._TASK_CANCEL_EVENTS["task_old"] = threading.Event()
    a2a._evict_old_tasks()
    assert "task_old" not in a2a._TASKS
    assert "task_active" in a2a._TASKS


def test_a2a_artifact_chunk_handles_canceling_and_corrupt_chunk_list(tmp_settings) -> None:
    task_id = "task_chunks01"
    with a2a._TASK_LOCK:
        a2a._TASKS[task_id] = {"id": task_id, "status": {"state": CANCELING}, "artifactChunks": "bad"}
    assert a2a._append_artifact_chunk(task_id, artifact_id="a", name="n", text="x", skip_if_canceling=True) == {}
    chunk = a2a._append_artifact_chunk(task_id, artifact_id="a", name="n", text="x", skip_if_canceling=False)
    assert chunk["chunkIndex"] == 0


def test_a2a_message_text_and_public_history_boundaries() -> None:
    assert a2a._text_from_message(None) == ""
    assert a2a._text_from_message({"parts": "bad"}) == ""
    assert a2a._text_from_message({"parts": [None, {"kind": "data", "text": "skip"}, {"type": "text", "text": "one"}]}) == "one"
    task = {"_secret": 1, "history": [1, 2, 3]}
    assert a2a.public_task(task, history_length=0) == {"history": []}
    assert a2a.public_task(task, history_length=2)["history"] == [2, 3]


def test_a2a_resubscribe_invalid_and_missing_task_errors() -> None:
    invalid = _decoded_sse(a2a._stream_resubscribe_events({"id": 1, "params": {}}))
    assert invalid[0]["error"]["code"] == INVALID_PARAMS
    missing = _decoded_sse(a2a._stream_resubscribe_events({"id": 2, "params": {"id": "task_missing"}}))
    assert missing[0]["error"]["code"] == TASK_NOT_FOUND


def _decoded_sse(events: Iterator[bytes]) -> list[dict[str, Any]]:
    return [json.loads(item.decode("utf-8").removeprefix("data: ").strip()) for item in events]


class _RpcResponse:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def __enter__(self) -> "_RpcResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.value

    def __iter__(self) -> Iterator[bytes]:
        return iter(self.value.splitlines(keepends=True))


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b'{"error":{"code":-1,"message":"bad"}}'])
def test_a2a_client_rpc_rejects_invalid_and_error_responses(payload: bytes) -> None:
    client = A2AClient("http://peer", auth_token="secret")
    assert client._headers("json")["Authorization"] == "Bearer secret"
    with patch("urllib.request.urlopen", return_value=_RpcResponse(payload)), pytest.raises(AppError):
        client.get_task("task")


@pytest.mark.parametrize(
    "payload",
    [
        b"ignored\n",
        b"data: []\n",
        b'data: {"error":{"code":-1,"message":"bad"}}\n',
        b"data: not-json\n",
    ],
)
def test_a2a_client_stream_rejects_invalid_events(payload: bytes) -> None:
    client = A2AClient("http://peer")
    with patch("urllib.request.urlopen", return_value=_RpcResponse(payload)):
        if payload == b"ignored\n":
            assert list(client.message_stream("x")) == []
        else:
            with pytest.raises((AppError, json.JSONDecodeError)):
                list(client.message_stream("x"))


def test_a2a_peer_client_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a, "A2A_PEERS", ("http://one", "http://two"))
    assert [client.base_url for client in a2a.peer_clients()] == ["http://one", "http://two"]
