"""Local A2A peer loopback demo.

Start a peer DeepSeek Infra instance first, for example:

    set DEFAULT_PORT=8001
    python -m deepseek_infra.app

Then stream a task into that peer:

    python examples/a2a_peer_demo.py --peer http://127.0.0.1:8001/a2a/agents/reasoner --token <local-token>

The script prints the initial Task, every artifact-update chunk, and the final
status-update. It can also resume an existing task with --task-id and
--after-chunk-index.
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from deepseek_infra.infra.agent_runtime.a2a import A2AClient


def _text_part(event: dict[str, Any]) -> str:
    artifact_value = event.get("artifact")
    artifact: dict[str, Any] = artifact_value if isinstance(artifact_value, dict) else {}
    parts_value = artifact.get("parts")
    parts: list[Any] = parts_value if isinstance(parts_value, list) else []
    texts = [str(part.get("text") or "") for part in parts if isinstance(part, dict)]
    return "".join(texts)


def _print_event(event: dict[str, Any]) -> None:
    result_value = event.get("result")
    result: dict[str, Any] = result_value if isinstance(result_value, dict) else {}
    kind = str(result.get("kind") or "")
    if kind == "task":
        status_value = result.get("status")
        status: dict[str, Any] = status_value if isinstance(status_value, dict) else {}
        print(f"task {result.get('id')} state={status.get('state') or ''}")
        return
    if kind == "artifact-update":
        print(
            "artifact "
            f"id={result.get('artifactId')} "
            f"chunk={result.get('chunkIndex')} "
            f"append={result.get('append')} "
            f"final={result.get('final')} "
            f"text={_text_part(result)!r}"
        )
        return
    if kind == "status-update":
        status_value = result.get("status")
        status = status_value if isinstance(status_value, dict) else {}
        print(f"status state={status.get('state')} final={result.get('final')}")
        return
    print(json.dumps(event, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Stream or resume an A2A task against a local DeepSeek Infra peer.")
    parser.add_argument("--peer", default="http://127.0.0.1:8001/a2a/agents/reasoner", help="A2A JSON-RPC endpoint")
    parser.add_argument("--token", default=os.environ.get("DEEPSEEK_INFRA_TOKEN", ""), help="Bearer token for local auth")
    parser.add_argument("--message", default="Summarize why artifact streaming matters for agent interop.")
    parser.add_argument("--task-id", default="", help="Existing task id to resume instead of sending a new message")
    parser.add_argument("--after-chunk-index", type=int, default=-1, help="Resume chunks after this chunk index")
    args = parser.parse_args()

    client = A2AClient(args.peer, timeout_seconds=120, auth_token=args.token)
    stream = (
        client.resubscribe(args.task_id, after_chunk_index=args.after_chunk_index)
        if args.task_id
        else client.message_stream(args.message)
    )
    for event in stream:
        _print_event(event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
