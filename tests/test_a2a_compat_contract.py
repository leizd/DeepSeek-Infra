from __future__ import annotations

import json
import threading
import time
from typing import Any, Iterator

import pytest

import deepseek_infra.infra.agent_runtime.a2a as a2a
from deepseek_infra.infra.agent_runtime.a2a import (
    CANCELED,
    CANCELING,
    COMPLETED,
    agent_card,
    get_task,
    handle_a2a_message,
    stream_message_events,
)


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


def message_params(text: str, message_id: str = "compat-msg-1") -> dict[str, Any]:
    return {"message": {"role": "user", "parts": [{"kind": "text", "text": text}], "messageId": message_id, "kind": "message"}}


def task_state(task: dict[str, Any]) -> str:
    status_value = task.get("status")
    status: dict[str, Any] = status_value if isinstance(status_value, dict) else {}
    return str(status.get("state") or "")


def wait_for_state(task_id: str, states: set[str], timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = get_task(task_id)
        if task_state(task) in states:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} never reached {states}; last={task_state(get_task(task_id))}")


def parse_sse(message: dict[str, Any], agent_id: str = "reasoner") -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for chunk in stream_message_events(message, agent_id=agent_id):
        line = chunk.decode("utf-8").strip()
        assert line.startswith("data: ")
        event = json.loads(line[len("data: ") :])
        assert isinstance(event, dict)
        events.append(event)
        result_value = event.get("result")
        result: dict[str, Any] = result_value if isinstance(result_value, dict) else {}
        if result.get("kind") == "status-update" and result.get("final") is True:
            break
    return events


def result_of(response: dict[str, Any] | None) -> dict[str, Any]:
    assert response is not None
    assert "error" not in response, response
    result = response.get("result")
    assert isinstance(result, dict)
    return result


def test_a2a_compat_contract_card_send_stream_and_resubscribe(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a.deepseek_client, "call_deepseek", lambda payload: {"content": "contract artifact", "usage": {"total_tokens": 4}})

    card = agent_card("reasoner", base_url="http://127.0.0.1:8000")
    assert card["url"] == "http://127.0.0.1:8000/a2a/agents/reasoner"
    assert card["capabilities"]["streaming"] is True
    assert card["skills"][0]["id"] == "reasoner.respond"

    sent = result_of(handle_a2a_message(rpc("message/send", message_params("send contract"), message_id="send-1"), agent_id="reasoner"))
    assert sent["kind"] == "task"
    sent_id = str(sent["id"])
    completed = wait_for_state(sent_id, {COMPLETED})
    assert completed["artifacts"][0]["parts"][0]["text"] == "contract artifact"
    assert [chunk["chunkIndex"] for chunk in completed["artifactChunks"]] == [0, 1]
    assert completed["artifactChunks"][1]["final"] is True

    fetched = result_of(handle_a2a_message(rpc("tasks/get", {"id": sent_id, "historyLength": 1}, message_id="get-1")))
    assert fetched["id"] == sent_id
    assert len(fetched["history"]) == 1

    stream_events = parse_sse(rpc("message/stream", message_params("stream contract", "compat-stream"), message_id="stream-1"))
    stream_results = [event["result"] for event in stream_events if isinstance(event.get("result"), dict)]
    stream_task = stream_results[0]
    stream_task_id = str(stream_task["id"])
    artifact_updates = [result for result in stream_results if result.get("kind") == "artifact-update"]
    final_updates = [result for result in stream_results if result.get("kind") == "status-update" and result.get("final") is True]
    assert stream_task["kind"] == "task"
    assert [event["id"] for event in stream_events] == ["stream-1"] * len(stream_events)
    assert [update["chunkIndex"] for update in artifact_updates] == [0, 1]
    assert all(update["artifactId"] for update in artifact_updates)
    assert artifact_updates[-1]["append"] is True
    assert artifact_updates[-1]["final"] is True
    assert artifact_updates[-1]["artifact"]["parts"][0]["text"] == "contract artifact"
    assert final_updates[-1]["status"]["state"] == COMPLETED

    resumed_events = parse_sse(rpc("tasks/resubscribe", {"id": stream_task_id, "afterChunkIndex": 0}, message_id="resume-1"))
    resumed_results = [event["result"] for event in resumed_events if isinstance(event.get("result"), dict)]
    resumed_artifacts = [result for result in resumed_results if result.get("kind") == "artifact-update"]
    assert [event["id"] for event in resumed_events] == ["resume-1"] * len(resumed_events)
    assert len(resumed_artifacts) == 1
    assert resumed_artifacts[0]["chunkIndex"] == 1
    assert resumed_artifacts[0]["artifact"]["parts"][0]["text"] == "contract artifact"


def test_a2a_compat_contract_cancel_lifecycle(tmp_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_call(payload: dict[str, Any]) -> dict[str, Any]:
        started.set()
        release.wait(5)
        return {"content": "late artifact", "usage": {"total_tokens": 2}}

    monkeypatch.setattr(a2a.deepseek_client, "call_deepseek", slow_call)

    task = result_of(handle_a2a_message(rpc("message/send", message_params("cancel contract"), message_id="cancel-send"), agent_id="reasoner"))
    task_id = str(task["id"])
    assert started.wait(5)

    cancelled = result_of(handle_a2a_message(rpc("tasks/cancel", {"id": task_id}, message_id="cancel-1"), agent_id="reasoner"))
    assert task_state(cancelled) == CANCELING
    assert cancelled["cancelRequestedAt"]

    release.set()
    final = wait_for_state(task_id, {CANCELED})
    assert task_state(final) == CANCELED
    assert final["artifacts"] == []
    assert final["artifactChunks"][0]["artifact"]["name"] == "progress"
