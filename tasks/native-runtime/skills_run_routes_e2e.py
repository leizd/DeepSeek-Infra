"""Exercise real native offline Skills routes, files and SQLite against an isolated oracle."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import gc
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import sys
import tempfile
from typing import Any

from skills_mutation_routes_e2e import bind_revisions, native, normalize, revision_bindings
from skills_read_routes_e2e import CLOCK_FIELDS, REPO, TOKEN, differences, digest, source_context, source_digest


def post(port: int, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("POST", path, json.dumps(payload).encode(),
                           {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def traces(root: Path) -> dict[str, Any]:
    path = root / ".traces/traces.sqlite3"
    if not path.is_file():
        return {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        result = {}
        for table in ["trace_runs", "trace_spans"]:
            rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for row in rows:
                for name in ["metadata", "input_json", "output_json", "usage_json", "diagnostics_json"]:
                    if name in row:
                        row[name] = json.loads(row[name])
            result[table] = rows
        return result


def stored(root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in [".skills", ".projects", ".generated"]:
        for path in sorted((root / name).rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            value: Any = text
            if path.suffix == ".json":
                value = json.loads(text)
            elif path.suffix == ".jsonl":
                value = [json.loads(line) for line in text.splitlines() if line.strip()]
            result[path.relative_to(root).as_posix()] = value
    result[".traces/logical"] = traces(root)
    return result


def identity_bindings(root: Path, state: dict[str, Any]) -> dict[str, str]:
    """Check IDs and their durable references before replacing independent entropy."""
    result: dict[str, str] = {}
    counts: dict[str, int] = {}

    def identity(raw: str, kind: str, pattern: str) -> None:
        assert re.fullmatch(pattern, raw), (kind, raw)
        if raw not in result:
            counts[kind] = counts.get(kind, 0) + 1
            result[raw] = f"<{kind}-{counts[kind]}>"

    for row in state.get(".skills/runs/runs.jsonl", []):
        identity(row["skillRunId"], "run", r"run-[0-9a-f]{16}")
    trace_state = state[".traces/logical"]
    for row in trace_state.get("trace_runs", []):
        identity(row["trace_id"], "trace", r"[0-9a-f]{32}")
        identity(row["metadata"]["skillRunId"], "run", r"run-[0-9a-f]{16}")
    for row in trace_state.get("trace_spans", []):
        assert row["trace_id"] in result, row
        identity(row["span_id"], "span", r"[0-9a-f]{32}")
    for row in state.get(".generated/artifacts.json", []):
        identity(row["fileId"], "file", r"[0-9a-f]{32}")
        assert row["artifactId"] == "art-" + row["fileId"][:16], row
        assert row["downloadUrl"] == "/api/download?id=" + row["fileId"], row
        assert (root / ".generated" / f"{row['fileId']}.{row['type']}").is_file(), row
        assert row["source"]["skillRunId"] in result, row
        result[row["artifactId"]] = result[row["fileId"]].replace("<file-", "<artifact-")

    def saved(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("id"), str) and value["id"].startswith("saved-"):
                identity(value["id"], "saved", r"saved-[0-9a-f]{16}")
            for name, item in value.items():
                # Context and generated Markdown include timestamps from prior
                # persisted runs. Bind only independently validated clock fields.
                if name in CLOCK_FIELDS | {"started_at", "completed_at", "lastRunAt"} and isinstance(item, str) and item:
                    datetime.fromisoformat(item)
                    result[item] = "<valid-clock>"
                saved(item)
        elif isinstance(value, list):
            for item in value:
                saved(item)

    saved(state)
    return result


def canonical_run(value: Any, root: Path, bindings: dict[str, str], key: str = "") -> Any:
    if isinstance(value, dict):
        return {name: canonical_run(item, root, bindings, name) for name, item in value.items()}
    if isinstance(value, list):
        return [canonical_run(item, root, bindings, key) for item in value]
    if key in {"latencyMs", "duration_ms"} and value is not None:
        assert isinstance(value, int) and not isinstance(value, bool) and value >= 0, (key, value)
        return "<valid-duration>"
    if key in {"started_epoch", "completed_epoch"} and value is not None:
        assert isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 and math.isfinite(value), (key, value)
        return "<valid-epoch>"
    if key in {"started_at", "completed_at", "lastRunAt"} and value:
        datetime.fromisoformat(value)
        return "<valid-clock>"
    value = bind_revisions(normalize(value, root, key), bindings)
    return value


def snapshot(root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    raw = stored(root)
    bindings = identity_bindings(root, raw) | revision_bindings(root)
    return {bind_revisions(name, bindings): canonical_run(value, root, bindings) for name, value in raw.items()}, bindings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=REPO / "artifacts/skills-run-routes-e2e.json")
    args = parser.parse_args()
    gateway = args.gateway.resolve()
    if not gateway.is_file():
        parser.error("the compiled native gateway is required")
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="deepseek-skills-offline-") as directory:
        oracle_root, native_root = Path(directory) / "oracle", Path(directory) / "native"
        oracle_root.mkdir()
        os.environ.update(DEEPSEEK_INFRA_ROOT=oracle_root.as_posix(), AUTH_TOKEN=TOKEN, DEEPSEEK_RUNTIME_MODE="python", TRACE_ENABLED="true")
        for name in ["AUTH_DISABLED", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY_SECONDARY", "TAVILY_API_KEY"]:
            os.environ.pop(name, None)
        sys.path.insert(0, str(REPO))
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
        from fastapi.testclient import TestClient
        from deepseek_infra.core.errors import AppError
        from deepseek_infra.infra.data import projects
        from deepseek_infra.infra.skills import registry, runner, security
        from deepseek_infra.web.routes.skills import SkillsRouteDeps, create_skills_router

        plain = {"skillId": "plain", "name": "原生离线验证", "description": "Isolated offline test", "version": "1.0",
                 "systemPrompt": "Explain the topic", "inputSchema": {"type": "object", "required": ["topic"]},
                 "outputSchema": {"type": "object", "required": ["content", "mode"]}, "allowedTools": [],
                 "memoryPolicy": {"scope": "none"}, "artifactPolicy": {"types": [], "autoSave": False}, "projectBinding": {"enabled": False}}
        variants = [plain, plain | {"skillId": "bound", "artifactPolicy": {"types": ["md"], "autoSave": True}, "projectBinding": {"enabled": True}},
                    plain | {"skillId": "bad-output", "outputSchema": {"type": "object", "required": ["model"]}},
                    plain | {"skillId": "disabled"}]
        for skill in variants:
            path = oracle_root / f"skills/builtin/{skill['skillId']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(skill, ensure_ascii=False), encoding="utf-8")
        project_id = projects.create_project("HTTP 离线项目")["id"]
        registry.set_skill_disabled("disabled", True)
        registry.create_custom_skill(plain | {"skillId": "blocked", "name": "Blocked custom"})
        security.block_skill("blocked", reason="Offline security fixture")
        ui = oracle_root / "ui/index.html"
        ui.parent.mkdir()
        ui.write_text("<main>offline skills fixture</main>", encoding="utf-8")
        shutil.copytree(oracle_root, native_root)
        before, _ = snapshot(native_root)
        with native(gateway, native_root, "python") as port:
            status, value = post(port, "/api/skills", {"action": "run", "skillId": "plain", "input": {"topic": "Rust"}, "offline": True})
        denial_preserves_state = status == 409 and value.get("code") == "NATIVE_SKILLS_WRITE_NOT_OWNED" and digest(snapshot(native_root)[0]) == digest(before)
        deps = {name: getattr(registry, name) for name in SkillsRouteDeps.__dataclass_fields__ if name not in {"run_skill", "validate_pack"}}
        app = FastAPI()

        @app.exception_handler(AppError)
        async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
            return JSONResponse(exc.to_response(), status_code=exc.status)

        app.include_router(create_skills_router(SkillsRouteDeps(**deps, run_skill=runner.run_skill, validate_pack=registry.validate_pack_manifest)))
        with TestClient(app, base_url="http://127.0.0.1", headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            def compare(port: int, payload: dict[str, Any], path: str = "/api/skills", restarted: bool = False) -> None:
                expected = client.post(path, json=payload)
                actual_status, actual = post(port, path, payload)
                expected_state, expected_ids = snapshot(oracle_root)
                actual_state, actual_ids = snapshot(native_root)
                expected_value = canonical_run(expected.json(), oracle_root, expected_ids)
                actual_value = canonical_run(actual, native_root, actual_ids)
                diff = differences(expected_value, actual_value)
                state_diff = differences(expected_state, actual_state)
                results.append({"path": path, "payload": payload, "restarted": restarted, "oracleStatus": expected.status_code,
                                "nativeStatus": actual_status, "ok": expected.status_code == actual_status and not diff and not state_diff,
                                "responseDifferences": diff, "stateDifferences": state_diff})

            with native(gateway, native_root, "python_disabled") as port:
                cases = [
                    {"action": "run", "skillId": "plain", "input": {"topic": "Rust", "apiKey": "fixture-secret"}, "offline": True},
                    {"action": "run", "id": "bound", "inputData": {"topic": "Go"}, "projectId": project_id, "offline": True},
                    {"action": "run", "skillId": "bound", "inputs": {"topic": "无持久化"}, "projectId": project_id, "offline": True, "persist": False},
                    {"action": "run", "skillId": "bound", "input": {"topic": "独立文档"}, "offline": "true"},
                    {"action": "run", "skillId": "plain", "input": {} , "offline": True},
                    {"action": "run", "skillId": "bound", "input": {"topic": "missing"}, "projectId": "proj-missing", "offline": True},
                    {"action": "run", "skillId": "disabled", "input": {"topic": "disabled"}, "offline": True},
                    {"action": "run", "skillId": "missing", "input": {"topic": "missing"}, "offline": True},
                    {"action": "run", "skillId": "blocked", "input": {"topic": "blocked"}, "offline": True},
                    {"action": "run", "skillId": "blocked", "input": {"topic": "blocked"}, "offline": True, "securityApproved": True},
                    {"action": "run", "skillId": "bad-output", "input": {"topic": "output validation"}, "offline": True},
                    {"action": "run", "skillId": "bad-output", "input": {"topic": "transient validation"}, "offline": True, "persist": False},
                    {"action": "run", "offline": True},
                ]
                for payload in cases:
                    compare(port, payload)
                compare(port, {"input": {"topic": "路径入口"}, "offline": True}, "/api/skills/plain/run")
                compare(port, {"input": None, "inputs": {"topic": "路径别名"}, "offline": True, "persist": False}, "/api/skills/plain/run")
                compare(port, {"action": "list_runs"})
                for root in [oracle_root, native_root]:
                    (root / ".workspace-restore-fence.json").write_text(json.dumps({"schemaVersion": "workspace-restore-fence.v1", "state": "restore_commit_started"}), encoding="utf-8")
                fenced_before = digest(snapshot(native_root)[0])
                compare(port, {"action": "run", "skillId": "plain", "input": {"topic": "fenced"}, "offline": True, "persist": False})
                recovery_refusal = results[-1]["ok"] and results[-1]["nativeStatus"] == 423 and digest(snapshot(native_root)[0]) == fenced_before
                for root in [oracle_root, native_root]:
                    (root / ".workspace-restore-fence.json").unlink()
                compare(port, {"action": "run", "skillId": "bound", "input": {"topic": "恢复后"}, "projectId": project_id, "offline": True})
                online_before = digest(snapshot(native_root)[0])
                online_status, online = post(port, "/api/skills/plain/run", {"input": {"topic": "online"}, "apiKey": "offline-only-provider-fixture"})
                online_fail_closed = online_status == 501 and online.get("code") == "NATIVE_SKILLS_ACTION_NOT_READY" and digest(snapshot(native_root)[0]) == online_before
            restart_before = digest(snapshot(native_root)[0])
            with native(gateway, native_root, "python_disabled") as port:
                compare(port, {"action": "list_runs"}, restarted=True)
                rows = stored(native_root)[".skills/runs/runs.jsonl"]
                target = next(row for row in rows if row["status"] == "completed")
                oracle_rows = stored(oracle_root)[".skills/runs/runs.jsonl"]
                oracle_target = next(row for row in oracle_rows if row["status"] == "completed")
                expected = client.post("/api/skills", json={"action": "get_run", "runId": oracle_target["skillRunId"]})
                actual_status, actual = post(port, "/api/skills", {"action": "get_run", "runId": target["skillRunId"]})
                _, expected_ids = snapshot(oracle_root)
                _, actual_ids = snapshot(native_root)
                diff = differences(canonical_run(expected.json(), oracle_root, expected_ids), canonical_run(actual, native_root, actual_ids))
                results.append({"payload": {"action": "get_run"}, "restarted": True, "nativeStatus": actual_status,
                                "ok": expected.status_code == actual_status and not diff, "responseDifferences": diff})
            restart_preserves_state = digest(snapshot(native_root)[0]) == restart_before
        after, _ = snapshot(native_root)
        trace_state = stored(native_root)[".traces/logical"]
        generated_count = len(list((native_root / ".generated").glob("*.md")))
        completed_count = sum(row["status"] == "completed" for row in stored(native_root)[".skills/runs/runs.jsonl"])
        # The reference tracing layer uses connection transaction contexts;
        # release its cyclic SQLite objects before Windows removes the fixture.
        gc.collect()
    failures = [row for row in results if not row["ok"]]
    report = {"status": "PASS" if not failures and denial_preserves_state and recovery_refusal and restart_preserves_state
              and online_fail_closed and generated_count >= 3 and completed_count >= 5 else "FAIL", "releaseQualified": False,
              **source_context(),
              "sourceSha256": source_digest(), "probeSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "gatewaySha256": hashlib.sha256(gateway.read_bytes()).hexdigest(), "platform": platform.platform(), "command": sys.argv,
              "topology": "independent native Rust process and Python offline oracle; no provider credentials",
              "caseCount": len(results), "differences": failures, "denialPreservesState": denial_preserves_state,
              "recoveryRefusalPreservesState": recovery_refusal, "restartPreservesState": restart_preserves_state,
              "onlineFailsClosedWithoutWrites": online_fail_closed, "generatedMarkdownCount": generated_count,
              "completedPersistentRunCount": completed_count, "traceRunCount": len(trace_state["trace_runs"]),
              "idsAndRevisionHashesVerified": True, "storeBeforeSha256": digest(before), "storeAfterSha256": digest(after), "cases": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["status", "caseCount", "generatedMarkdownCount", "completedPersistentRunCount"]}
                     | {"differenceCount": len(failures), "output": str(args.output)}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
