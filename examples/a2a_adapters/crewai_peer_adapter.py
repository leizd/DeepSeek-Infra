#!/usr/bin/env python3
"""Skeleton for exposing a CrewAI crew as an A2A peer.

This is a template, not a certified third-party integration. Fill in
``run_crewai_crew`` with your crew kickoff call, then validate the served peer
with ``scripts/smoke_a2a_external_peer.py`` before updating compatibility docs.
"""

from __future__ import annotations

from typing import Any


def run_crewai_crew(message: str) -> str:
    """Replace this stub with ``crew.kickoff`` or equivalent CrewAI execution."""
    raise NotImplementedError("Wire this function to a real CrewAI crew before using it as evidence.")


def task_artifacts_from_answer(task_id: str, answer: str) -> list[dict[str, Any]]:
    """Convert a CrewAI answer into ordered A2A artifact chunks."""
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
        "framework": "CrewAI",
        "status": "adapter-skeleton",
        "evidence": "Run scripts/smoke_a2a_external_peer.py --peer-url <adapter-root> --peer-type adapter",
    }
