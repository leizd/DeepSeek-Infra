"""Compare native Skills mutations and restart state against a separate Python oracle."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterator

from skills_read_routes_e2e import CLOCK_FIELDS, REPO, TOKEN, canonical, differences, digest, request, source_context, source_digest


def normalize(value: Any, root: Path, key: str = "") -> Any:
    if isinstance(value, dict):
        return {name: normalize(item, root, name) for name, item in value.items()}
    if isinstance(value, list):
        return [normalize(item, root, key) for item in value]
    if key in CLOCK_FIELDS and isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return "<valid-epoch>"
    if isinstance(value, str):
        value = value.replace(root.as_posix(), "<root>").replace(str(root), "<root>")
    return canonical(value, key)


def state(root: Path) -> dict[str, Any]:
    replacements = revision_bindings(root)
    result: dict[str, Any] = {}
    for directory in [root / ".skills", root / ".projects"]:
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            value: Any
            if path.suffix == ".jsonl":
                value = []
                for line in text.splitlines():
                    try:
                        value.append(json.loads(line))
                    except ValueError:
                        value.append({"unparsed": line})
            elif path.suffix == ".json":
                value = json.loads(text)
            else:
                value = text
            name = path.relative_to(root).as_posix()
            for raw, bound in replacements.items():
                name = name.replace(raw, bound)
            result[name] = bind_revisions(normalize(value, root), replacements)
    return result


def compact_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def revision_bindings(root: Path) -> dict[str, str]:
    """Verify every snapshot digest before normalizing clock-dependent revision IDs."""
    result: dict[str, str] = {}
    for path in sorted((root / ".skills/history").rglob("*.json")):
        revision = json.loads(path.read_text(encoding="utf-8"))
        pack = revision["schemaVersion"] == "skill-pack-revision.v1"
        config = revision["pack" if pack else "skill"]
        metadata = revision["metadata"]
        revision_id = metadata["revisionId"]
        assert re.fullmatch(r"rev_\d{14}_[0-9a-f]{10}", revision_id), path
        expected = compact_hash({"event": metadata["event"], "config": config})
        assert revision_id.rsplit("_", 1)[-1] == expected[:10], path
        assert metadata["version"] == config["version"], path
        tools = sorted({tool for skill in config["skills"] for tool in skill.get("allowedTools", [])}) if pack else config.get("allowedTools") or []
        assert metadata["toolGrantHash"] == compact_hash(tools), path
        if pack:
            assert metadata["packHash"] == compact_hash(config), path
            assert metadata["skillIdsHash"] == compact_hash([skill["skillId"] for skill in config["skills"]]), path
            result[metadata["packHash"]] = compact_hash(normalize(config, root))
        else:
            assert metadata["promptHash"] == compact_hash(config["systemPrompt"]), path
            assert metadata["schemaHash"] == compact_hash({key: config.get(key) for key in ["inputSchema", "outputSchema"]}), path
        result[revision_id] = "rev_<verified-clock>_" + compact_hash({"event": metadata["event"], "config": normalize(config, root)})[:10]
    return result


def bind_revisions(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: bind_revisions(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [bind_revisions(item, replacements) for item in value]
    if isinstance(value, str):
        for raw, bound in replacements.items():
            value = value.replace(raw, bound)
    return value


@contextmanager
def native(gateway: Path, root: Path, mode: str) -> Iterator[int]:
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    env = dict(os.environ, DEEPSEEK_INFRA_ROOT=root.as_posix(), DEEPSEEK_INFRA_STATIC_DIR=root.as_posix(),
               DEEPSEEK_RUNTIME_MODE=mode, GATEWAY_BIND_ADDR=f"127.0.0.1:{port}", AUTH_TOKEN=TOKEN, RUST_LOG="error")
    env.pop("AUTH_DISABLED", None)
    with (root / "native.log").open("ab") as log:
        process = subprocess.Popen([str(gateway)], env=env, cwd=root, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise RuntimeError((root / "native.log").read_text(encoding="utf-8", errors="replace")[-2000:])
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("native gateway startup timed out") from None
                    time.sleep(0.05)
            yield port
        finally:
            process.terminate()
            process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=REPO / "artifacts/skills-mutation-routes-e2e.json")
    args = parser.parse_args()
    gateway = args.gateway.resolve()
    if not gateway.is_file():
        parser.error("the compiled native gateway is required")
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="deepseek-skills-writes-") as directory:
        oracle_root, native_root = Path(directory) / "oracle", Path(directory) / "native"
        oracle_root.mkdir()
        os.environ.update(DEEPSEEK_INFRA_ROOT=oracle_root.as_posix(), AUTH_TOKEN=TOKEN, DEEPSEEK_RUNTIME_MODE="python")
        os.environ.pop("AUTH_DISABLED", None)
        sys.path.insert(0, str(REPO))
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
        from fastapi.testclient import TestClient
        from deepseek_infra.core.errors import AppError
        from deepseek_infra.infra.data import projects
        from deepseek_infra.infra.skills import analytics, registry, runner
        from deepseek_infra.web.routes.skills import SkillsRouteDeps, create_skills_router

        skill = {"skillId": "tutor", "name": "学习", "description": "Test skill", "version": "1.0", "systemPrompt": "Explain the topic",
                 "inputSchema": {"type": "object", "required": ["topic"]}, "outputSchema": {"type": "object"}, "allowedTools": [],
                 "memoryPolicy": {"scope": "none"}, "artifactPolicy": {"types": []}, "projectBinding": {"enabled": False}}
        for name, value in [("skills/builtin/tutor.json", skill), ("skills/builtin/writer.json", skill | {"skillId": "writer", "name": "Writer"}),
                            ("skills/packs/learning.json", {"packId": "learning", "name": "Learning", "description": "Study pack", "version": "1.0",
                                                           "skills": [{"skillId": "tutor"}, {"skillId": "writer"}]})]:
            path = oracle_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        project = projects.create_project("Skills HTTP fixture")
        project_id = project["id"]
        registry.create_custom_skill(skill | {"skillId": "versioned", "name": "Versioned"})
        registry.update_skill("versioned", {"version": "2.0", "systemPrompt": "Review the new topic"})
        custom_pack = {"packId": "versioned-pack", "name": "Versioned Pack", "description": "Rollback fixture", "version": "1.0", "skills": ["tutor"]}
        registry.import_pack(custom_pack)
        registry.import_pack(custom_pack | {"version": "2.0"}, overwrite=True)
        for i in range(8):
            analytics.append_run({"skillRunId": f"run_{i}", "skillId": "tutor" if i % 2 else "writer", "packId": "learning",
                                  "projectId": project_id, "status": "failed" if i % 3 == 0 else "completed", "offline": True,
                                  "input": {"secret": "private input"}, "output": {"content": "private output"},
                                  "startedAt": f"2026-10-05T12:{i:02}:00+00:00", "completedAt": f"2026-10-05T12:{i:02}:01+00:00"})
        ui = oracle_root / "ui/index.html"
        ui.parent.mkdir()
        ui.write_text("<main>skills fixture</main>", encoding="utf-8")
        golden = oracle_root / "evals/golden/skills/skill_eval_cases.jsonl"
        golden.parent.mkdir(parents=True)
        shutil.copyfile(REPO / "evals/golden/skills/skill_eval_cases.jsonl", golden)
        shutil.copytree(oracle_root, native_root)
        before = digest(state(native_root))
        denied_actions = ["catalog_install", "catalog_uninstall", "catalog_refresh", "delete_run", "cleanup_runs", "redact_run", "create_eval_case", "delete_eval_case", "rollback_skill", "rollback_pack"]
        with native(gateway, native_root, "python") as port:
            denials = []
            for action in denied_actions:
                status, value = request(port, {"action": action, "itemId": "tutor", "projectId": project_id, "runId": "run_1", "caseId": "case-fenced",
                                              "case": {"caseId": "case-fenced", "skillId": "tutor"}})
                expected_code = ("NATIVE_PROJECT_METADATA_WRITE_NOT_OWNED" if action in {"catalog_install", "catalog_uninstall"}
                                 else "NATIVE_SKILLS_WRITE_NOT_OWNED")
                denials.append({"action": action, "status": status, "code": value.get("code"),
                                "ok": status == 409 and value.get("code") == expected_code})
        denials_preserve_state = digest(state(native_root)) == before
        deps = {name: getattr(registry, name) for name in SkillsRouteDeps.__dataclass_fields__ if name not in {"run_skill", "validate_pack"}}
        app = FastAPI()

        @app.exception_handler(AppError)
        async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
            return JSONResponse(exc.to_response(), status_code=exc.status)

        app.include_router(create_skills_router(SkillsRouteDeps(**deps, run_skill=runner.run_skill, validate_pack=registry.validate_pack_manifest)))
        payloads = [
            {"action": "rollback_skill", "skillId": "tutor", "version": "1.0"},
            {"action": "rollback_skill", "id": "versioned"},
            {"action": "rollback_skill", "id": "versioned", "version": "missing"},
            {"action": "rollback_skill", "id": "versioned", "version": " ", "revisionId": "1.0"},
            {"action": "rollback_skill", "id": "versioned", "revisionId": "1.0", "changeSummary": "Restore prior prompt"},
            {"action": "get", "id": "versioned"},
            {"action": "rollback_pack", "packId": "versioned-pack"},
            {"action": "rollback_pack", "packId": "versioned-pack", "version": "missing"},
            {"action": "rollback_pack", "packId": "versioned-pack", "version": "1.0", "projectId": project_id},
            {"action": "get_pack", "packId": "versioned-pack"},
            {"action": "catalog_install", "itemId": "learning", "projectId": project_id, "preview": True},
            {"action": "catalog_install", "itemId": "tutor", "projectId": project_id},
            {"action": "catalog_install", "id": "learning", "projectId": project_id, "approveSecurityReview": True},
            {"action": "catalog_install", "skillId": "tutor", "projectId": project_id, "dryRun": True},
            {"action": "catalog_uninstall", "itemId": "tutor", "projectId": project_id},
            {"action": "catalog_uninstall", "packId": "learning", "projectId": project_id},
            {"action": "catalog_uninstall", "itemId": "missing", "projectId": project_id},
            {"action": "catalog_refresh"},
            {"action": "catalog_install", "itemId": "writer", "projectId": project_id},
            {"action": "redact_run", "skillRunId": "run_1"},
            {"action": "get_run", "runId": "run_1"},
            {"action": "delete_run", "runId": "run_2"},
            {"action": "delete_run", "runId": "run_2"},
            {"action": "cleanup_runs", "status": "failed", "keepRecent": 1},
            {"action": "cleanup_runs", "skillId": "tutor", "keepRecent": "bad"},
            {"action": "redact_run", "runId": "missing"},
            {"action": "cleanup_runs", "keepRecent": "٩٩"},
            {"action": "dry_run", "skill": skill, "inputData": {"topic": "Unicode 你好"}},
            {"action": "dry_run", "config": skill, "inputs": {}},
            {"action": "list_eval_cases"},
            {"action": "create_eval_case", "case": {"id": "http-case", "skillId": "tutor", "keywords": [None, False, 0, {}, [], " 你好 "],
                                                       "jsonPaths": "output.content; mode\n content", "forbiddenContent": "secret,private",
                                                       "artifactTypes": ["docx"], "deniedTool": "python_eval", "requiredTools": "read_file_chunk",
                                                       "projectBindingRequired": "false", "input": {"topic": "subject"}, "source": " seed "}},
            {"action": "list_eval_cases"},
            {"action": "create_eval_case", "caseId": "http-case", "skillId": "writer", "input": [], "expectedKeywords": "replaced"},
            {"action": "create_eval_case", "case": {"caseId": "missing-skill", "skillId": "missing"}},
            {"action": "create_eval_case", "case": {}},
            {"action": "delete_eval_case", "id": "http-case"},
            {"action": "delete_eval_case", "caseId": "http-case"},
            {"action": "delete_eval_case"},
            {"action": "create_eval_case", "case": {"caseId": "restart-case", "skillId": "tutor", "input": {"topic": "persist"}}},
            {"action": "catalog_refresh"},
        ]
        with TestClient(app, base_url="http://127.0.0.1") as client:
            def compare(port: int, payload: dict[str, Any], restarted: bool = False) -> None:
                expected = client.post("/api/skills", json=payload, headers={"Authorization": f"Bearer {TOKEN}"})
                status, actual = request(port, payload)
                oracle = bind_revisions(normalize(expected.json(), oracle_root), revision_bindings(oracle_root))
                native_value = bind_revisions(normalize(actual, native_root), revision_bindings(native_root))
                oracle_state, native_state = state(oracle_root), state(native_root)
                results.append({"payload": payload, "afterRestart": restarted, "oracleStatus": expected.status_code, "nativeStatus": status,
                                "oracleHash": digest(oracle), "nativeHash": digest(native_value), "sample": differences(oracle, native_value),
                                "oracleStateHash": digest(oracle_state), "nativeStateHash": digest(native_state),
                                "stateSample": differences(oracle_state, native_state),
                                "ok": status == expected.status_code and oracle == native_value and oracle_state == native_state})

            with native(gateway, native_root, "python_disabled") as port:
                for payload in payloads:
                    compare(port, payload)
                fenced_before = digest(state(native_root))
                for root in [oracle_root, native_root]:
                    (root / ".workspace-restore-fence.json").write_text(json.dumps({"restoreId": "isolated-recovery"}), encoding="utf-8")
                compare(port, {"action": "delete_eval_case", "caseId": "restart-case"})
                recovery_refusal = results[-1]["ok"] and results[-1]["nativeStatus"] == 423 and fenced_before == digest(state(native_root))
                compare(port, {"action": "rollback_pack", "packId": "versioned-pack", "version": "2.0", "projectId": project_id})
                recovery_refusal = recovery_refusal and results[-1]["ok"] and results[-1]["nativeStatus"] == 423 and fenced_before == digest(state(native_root))
                for root in [oracle_root, native_root]:
                    (root / ".workspace-restore-fence.json").unlink()
                compare(port, {"action": "create_eval_case", "case": {"caseId": "resumed-case", "skillId": "writer"}})
            before_restart = digest(state(native_root))
            with native(gateway, native_root, "python_disabled") as port:
                for payload in [{"action": "get", "id": "versioned"}, {"action": "get_pack", "packId": "versioned-pack"}, {"action": "list_runs"}, {"action": "catalog_list"}, {"action": "get_run", "runId": "run_1"}, {"action": "list_eval_cases"}]:
                    compare(port, payload, restarted=True)
            restart_preserves_state = before_restart == digest(state(native_root))
        after = digest(state(native_root))
    failures = [row for row in results if not row["ok"]]
    required = set(denied_actions) | {"dry_run", "list_eval_cases", "create_eval_case", "delete_eval_case"}
    success = {row["payload"]["action"] for row in results if row["ok"] and row["nativeStatus"] == 200}
    missing = sorted(required - success)
    report = {"status": "PASS" if not failures and not missing and all(row["ok"] for row in denials) and denials_preserve_state
              and restart_preserves_state and recovery_refusal and before != after else "FAIL", "releaseQualified": False,
              **source_context(),
              "sourceSha256": source_digest(),
              "revisionHashesVerified": True,
              "probeSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "platform": platform.platform(), "command": sys.argv,
              "gatewaySha256": hashlib.sha256(gateway.read_bytes()).hexdigest(), "topology": "independent native and Python temporary stores",
              "caseCount": len(results), "differences": failures, "denials": denials, "denialsPreserveState": denials_preserve_state,
              "restartPreservesState": restart_preserves_state, "successfulActions": sorted(success), "missingSuccessfulActions": missing,
              "recoveryRefusalPreservesState": recovery_refusal,
              "storeBeforeSha256": before, "storeAfterSha256": after, "cases": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["status", "caseCount", "denialsPreserveState", "restartPreservesState"]}
                     | {"differenceCount": len(failures), "output": str(args.output)}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
