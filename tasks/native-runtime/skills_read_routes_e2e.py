"""Compare real native Skills HTTP reads with the isolated Python route oracle."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
TOKEN = "skills-read-route-probe"
CLOCK_FIELDS = {"generatedAt", "lastSecurityReviewAt", "createdAt", "updatedAt", "startedAt", "completedAt", "installedAt"}


def canonical(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {name: canonical(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [canonical(item, key) for item in value]
    if key in CLOCK_FIELDS and isinstance(value, str) and value:
        from datetime import datetime
        datetime.fromisoformat(value)
        return "<valid-clock>"
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_digest() -> str:
    roots = [REPO / name for name in ["rust/crates", "deepseek_infra", "tasks/native-runtime", "skills", "evals/golden/skills"]]
    files = [path for root in roots for path in root.rglob("*") if path.is_file() and path.suffix in {".rs", ".py", ".toml", ".json", ".jsonl"}]
    files += [REPO / name for name in ["rust/Cargo.toml", "rust/Cargo.lock", "VERSION", "release/native_runtime_ownership_v1.json"]]
    return digest({path.relative_to(REPO).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)})


def source_context() -> dict[str, Any]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=REPO, stderr=subprocess.PIPE)
    try:
        valid = Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() == REPO.resolve()
    except subprocess.CalledProcessError:
        valid = False
    if not valid:
        return {"head": None, "dirty": None, "trackedDiffSha256": None, "gitContextValid": False}
    return {"head": git("rev-parse", "HEAD").decode().strip(), "dirty": bool(git("status", "--porcelain")),
            "trackedDiffSha256": hashlib.sha256(git("diff", "--binary")).hexdigest(), "gitContextValid": True}


def differences(expected: Any, actual: Any, path: str = "$") -> list[dict[str, Any]]:
    if isinstance(expected, dict) and isinstance(actual, dict):
        return [row for key in sorted(expected.keys() | actual.keys()) for row in
                differences(expected.get(key, "<absent>"), actual.get(key, "<absent>"), f"{path}.{key}")][:8]
    if isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        return [row for i, (left, right) in enumerate(zip(expected, actual)) for row in differences(left, right, f"{path}[{i}]")][:8]
    if expected == actual:
        return []
    return [{"path": path, "expected": repr(expected)[:180], "actual": repr(actual)[:180]}]


def store_digest(root: Path) -> str:
    return digest({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                   for p in sorted(root.rglob("*")) if p.is_file() and p.name != "native.log"})


def request(port: int, payload: dict[str, Any], authenticated: bool = True) -> tuple[int, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["Authorization"] = f"Bearer {TOKEN}"
    try:
        connection.request("POST", "/api/skills", json.dumps(payload).encode(), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def cases() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = [{"action": action} for action in ["catalog_list", "catalog_export", "security_summary", "list_runs", "export_runs", "analytics_summary"]]
    result += [{"action": "catalog_get", key: "tutor"} for key in ["itemId", "skillId", "packId", "id"]]
    result += [{"action": "catalog_get"}, {"action": "catalog_get", "itemId": "missing"},
               {"action": "catalog_get", "itemId": " ", "skillId": "tutor"}]
    result += [{"action": "catalog_search", "query": query, "filters": filters}
               for query, filters in [("学习", {}), ("", {"kind": "pack"}), ("", {"trusted": True}), ("missing", {}), ("", None)]]
    result += [{"action": "security_summary", "scope": scope} for scope in ["all", "skills", "packs", "unknown"]]
    result += [{"action": "list_runs", "limit": value} for value in [None, True, False, -1, 0, 1, 1.0, 2.7, "  +2 ", "1_0", "bad", "１２３", "٩٩", "9" * 80, "-" + "9" * 80, "9" * 4300, "9" * 4301, "²", "1__0", "_10", "10_"]]
    import unicodedata
    result += [{"action": "list_runs", "limit": chr(point + 1) + chr(point + 2)}
               for point in range(0x110000) if unicodedata.category(chr(point)) == "Nd" and unicodedata.decimal(chr(point)) == 0]
    result += [{"action": "list_runs", field: value} for field, value in [("skillId", "tutor"), ("packId", "learning"), ("projectId", "proj_a"), ("status", "failed"), ("skillId", "missing")]]
    result += [{"action": "get_run", key: "run_1"} for key in ["skillRunId", "runId"]]
    result += [{"action": "get_run"}, {"action": "get_run", "runId": "missing"}]
    result += [{"action": "analytics_summary", "scope": scope, "skillId": "tutor", "packId": "learning", "projectId": "proj_a", "days": days}
               for scope, days in [("all", 0), ("skill", 1), ("pack", 30), ("project", 1000), ("unknown", "bad")]]
    result += [{"action": "list_versions", "skillId": "tutor"}, {"action": "list_versions", "id": "missing"},
               {"action": "list_pack_versions", "packId": "learning"}, {"action": "list_pack_versions", "id": "missing"},
               {"action": "migration_plan", "skillId": "tutor"}, {"action": "migration_plan", "id": "tutor", "fromVersion": " current ", "to": " "},
               {"action": "migration_plan", "skillId": "tutor", "from": "missing"}]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=REPO / "artifacts/skills-read-routes-e2e.json")
    args = parser.parse_args()
    gateway = args.gateway.resolve()
    if not gateway.is_file():
        parser.error("the compiled native gateway is required")
    with tempfile.TemporaryDirectory(prefix="deepseek-skills-reads-") as directory:
        root = Path(directory)
        os.environ.update(DEEPSEEK_INFRA_ROOT=root.as_posix(), AUTH_TOKEN=TOKEN, DEEPSEEK_RUNTIME_MODE="python")
        sys.path.insert(0, str(REPO))
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
        from fastapi.testclient import TestClient
        from deepseek_infra.core.errors import AppError
        from deepseek_infra.infra.skills import analytics, registry, runner
        from deepseek_infra.web.routes.skills import SkillsRouteDeps, create_skills_router

        skill = {"skillId": "tutor", "name": "学习", "description": "Test skill", "version": "1.0", "systemPrompt": "Explain the topic",
                 "inputSchema": {"type": "object"}, "outputSchema": {"type": "object"}, "allowedTools": [], "memoryPolicy": {"scope": "none"},
                 "artifactPolicy": {"types": []}, "projectBinding": {"enabled": False}}
        for name, value in [("skills/builtin/tutor.json", skill), ("skills/packs/learning.json", {"packId": "learning", "name": "Learning", "description": "Study pack", "version": "1.0", "skills": [{"skillId": "tutor"}]})]:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        runs = [analytics.normalize_run({"skillRunId": f"run_{i}", "skillId": "tutor", "packId": "learning", "projectId": "proj_a" if i % 2 else "proj_b",
                "status": "failed" if i % 3 == 0 else "completed", "startedAt": f"2026-10-05T12:{i % 60:02}:00+00:00",
                "completedAt": "2026-10-05T12:59:00+00:00", "latencyMs": i, "offline": True}) for i in range(600)]
        path = root / ".skills/runs/runs.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("\n".join(json.dumps(run) for run in runs) + "\ninvalid json\n{}\n", encoding="utf-8")
        ui = root / "ui/index.html"
        ui.parent.mkdir()
        ui.write_text("<main>skills fixture</main>", encoding="utf-8")
        deps = {name: getattr(registry, name) for name in SkillsRouteDeps.__dataclass_fields__ if name not in {"run_skill", "validate_pack"}}
        app = FastAPI()

        @app.exception_handler(AppError)
        async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
            return JSONResponse(exc.to_response(), status_code=exc.status)

        app.include_router(create_skills_router(SkillsRouteDeps(**deps, run_skill=runner.run_skill, validate_pack=registry.validate_pack_manifest)))
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        env = dict(os.environ, GATEWAY_BIND_ADDR=f"127.0.0.1:{port}", DEEPSEEK_INFRA_STATIC_DIR=root.as_posix(), RUST_LOG="error")
        env.pop("AUTH_DISABLED", None)
        before = store_digest(root)
        results = []
        with (root / "native.log").open("wb") as log:
            process = subprocess.Popen([str(gateway)], env=env, cwd=root, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise RuntimeError((root / "native.log").read_text(encoding="utf-8", errors="replace"))
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("native gateway startup timed out") from None
                        time.sleep(0.05)
                with TestClient(app, base_url="http://127.0.0.1") as client:
                    for authenticated, payload in [(False, {"action": "catalog_list"})] + [(True, payload) for payload in cases()]:
                        headers = {"Authorization": f"Bearer {TOKEN}"} if authenticated else {}
                        expected = client.post("/api/skills", json=payload, headers=headers)
                        status, actual = request(port, payload, authenticated)
                        oracle = canonical(expected.json())
                        native = canonical(actual)
                        results.append({"payload": payload, "authenticated": authenticated, "oracleStatus": expected.status_code, "nativeStatus": status,
                                        "oracleHash": digest(oracle), "nativeHash": digest(native), "sample": differences(oracle, native),
                                        "ok": status == expected.status_code and oracle == native})
            finally:
                process.terminate()
                process.wait(timeout=10)
        after = store_digest(root)
    failures = [row for row in results if not row["ok"]]
    required_actions = {"catalog_list", "catalog_get", "catalog_search", "catalog_export", "security_summary", "list_runs", "get_run", "export_runs", "analytics_summary", "list_versions", "list_pack_versions", "migration_plan"}
    successful_actions = {row["payload"]["action"] for row in results if row["ok"] and row["nativeStatus"] == 200}
    missing_success = sorted(required_actions - successful_actions)
    report = {"status": "PASS" if not failures and before == after and not missing_success else "FAIL", **source_context(), "releaseQualified": False,
              "platform": platform.platform(), "command": sys.argv, "gatewaySha256": hashlib.sha256(gateway.read_bytes()).hexdigest(),
              "sourceSha256": source_digest(), "probeSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "topology": "native gateway process on loopback; isolated Python route oracle", "caseCount": len(results), "differences": failures,
              "storeBeforeSha256": before, "storeAfterSha256": after, "readsPreserveStore": before == after,
              "successfulActions": sorted(successful_actions), "missingSuccessfulActions": missing_success, "cases": results}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ["status", "caseCount", "readsPreserveStore"]} | {"differenceCount": len(failures), "output": str(args.output)}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
