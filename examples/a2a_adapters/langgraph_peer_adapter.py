#!/usr/bin/env python3
"""Skeleton for exposing a LangGraph app as an A2A peer.

This is a template, not a certified third-party integration. Fill in
``run_langgraph_agent`` with your graph invocation, then expose the JSON-RPC and
SSE methods with the same contract used by ``examples/a2a_interop_peer.py``.
"""

from __future__ import annotations

from typing import Any


def run_langgraph_agent(message: str) -> str:
    """Replace this stub with ``graph.invoke`` or ``graph.astream`` output."""
    raise NotImplementedError("Wire this function to a real LangGraph graph before using it as evidence.")


def task_artifacts_from_answer(task_id: str, answer: str) -> list[dict[str, Any]]:
    """Convert a LangGraph answer into ordered A2A artifact chunks."""
    artifact_id = f"artifact_{task_id}"
    return [
        {
            "taskId": task_id,
            "artifactId": artifact_id,
            "chunkIndex": 0,
            "append": True,
            "final": True,
            "artifact": {
                "artifactId": artifact_id,
                "name": "answer",
                "parts": [{"kind": "text", "text": answer}],
            },
        }
    ]


def adapter_notes() -> dict[str, str]:
    return {
        "framework": "LangGraph",
        "status": "adapter-skeleton",
        "evidence": "Run scripts/smoke_a2a_external_peer.py --peer-url <adapter-root> --peer-type adapter",
    }
