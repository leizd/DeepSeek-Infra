"""Projects-store parity probe, Python side.

Covers the read path of `deepseek_infra/infra/data/projects.py`: the id validator,
the normaliser family, `read_project`, `public_project` and `list_projects`.

Pinned: `secrets.token_hex` (a counter) and `utc_now_iso`, because `read_project`
**mints a fresh id on every read** for a skill run or saved item that has none — see
`docs/PROJECTS_STORE.md`.

Usage::

    python tasks/native-runtime/projects_parity_probe.py > python.json
    cd rust && cargo run -p deepseek-policy --example projects_parity_probe > ../rust.json
    diff <(tr -d '\\r' < python.json) <(tr -d '\\r' < rust.json)
"""

from __future__ import annotations

import ast
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROJECTS = REPO / "deepseek_infra" / "infra" / "data" / "projects.py"
ERRORS = REPO / "deepseek_infra" / "core" / "errors.py"
UTILS = REPO / "deepseek_infra" / "core" / "utils.py"

FUNCTIONS = (
    "validate_project_id",
    "normalize_project_name",
    "normalize_documents",
    "normalize_project_skills",
    "normalize_project_pack_versions",
    "normalize_pack_id_for_project",
    "normalize_skill_id_for_project",
    "normalize_skill_runs",
    "normalize_skill_run",
    "normalize_saved_items",
    "normalize_project_artifacts",
    "normalize_project_artifact",
    "unique_strings",
    "_safe_int",
    "read_project",
    "require_project",
    "list_projects",
    "public_project",
)

ID_CASES = [
    "abcd", "abc", "a-b_c9", "ABC_1234", "ab cd", "abcd!", "", "x" * 65,
    "  abcd  ", "项目名称", "a" * 64,
]

NAME_CASES = [
    "My Project", "  padded  ", "line\nbreak", "", None, 42, "x" * 100,
]

DOC_CASES = [
    ("no-dict", ["x"]),
    ("missing-file-id", [{"projectId": "abcd"}]),
    ("bad-file-id", [{"fileId": "ABC", "projectId": "abcd"}]),
    ("uppercase-file-id", [{"fileId": "A" * 32, "projectId": "abcd"}]),
    ("empty-project-id", [{"fileId": "a" * 32, "projectId": ""}]),
    ("minimal", [{"fileId": "a" * 32, "projectId": "abcd"}]),
    ("full", [{"id": "d1", "fileId": "b" * 32, "projectId": "abcd", "name": "Doc",
               "type": "text/plain", "size": 12, "kind": "pdf", "sourceAvailable": True,
               "preview": "p" * 2000, "pageCount": 3, "charCount": 40, "chunkCount": 2,
               "chunked": True, "createdAt": 99}]),
    ("not-a-list", {"a": 1}),
]

SAFE_INT_CASES = [
    ("none", None, 7), ("int", 5, 0), ("negative", -3, 0), ("numeric-string", "12", 0),
    ("float-string", "12.7", 0), ("spaces", "  8  ", 0), ("garbage", "abc", 4),
    ("bool-true", True, 0), ("bool-false", False, 0), ("underscore", "1_0", 0),
    ("plus", "+9", 0), ("empty-string", "", 5), ("float", 5.5, 3),
]

SKILL_CASES = [
    ("empty", None),
    ("empty-dict", {}),
    ("packs-only", {"enabledPacks": ["aaa", "bbb"]}),
    ("bad-pack", {"enabledPacks": ["ab", "aaa"]}),
    ("versions", {"enabledPackVersions": [{"packId": "aaa", "version": "1.0"}],
                  "enabledPacks": ["bbb"]}),
    ("pack-strings", {"enabledPackVersions": ["ccc", {"packId": "ddd"}]}),
    ("skills", {"enabledSkills": ["skill:a", "skill:a", "x"], "recentSkills": ["skill:b"]}),
    ("default-not-enabled", {"enabledSkills": ["skill:a"], "defaultSkill": "skill:z"}),
    ("default-enabled", {"enabledSkills": ["skill:a"], "defaultSkill": "skill:a"}),
]

RUN_CASES = [
    ("minimal", {}),
    ("with-id", {"skillRunId": "r1"}),
    ("with-run-id", {"runId": "r2"}),
    ("full", {"skillRunId": "r3", "skillId": "skill:a", "skillVersion": "1.0",
              "packId": {"packId": "pack:a"}, "status": "failed", "projectId": "abcd",
              "input": {"k": "v"}, "inputSummary": "in", "outputSummary": "out",
              "artifactIds": ["a1", "a1", "a2"], "savedItemIds": ["s1"],
              "traceId": "t1", "startedAt": "2026-01-01", "completedAt": "2026-01-02",
              "latencyMs": 100, "offline": True, "model": "m", "errorReason": "e",
              "failureCategory": "f", "diagnosticSuggestion": "d",
              "runSecurityLevel": "l", "securityReviewId": "s", "trustedAtRun": True,
              "toolGrantHashAtRun": "h", "blockedReason": "b", "approvalRequired": True}),
    ("non-dict-input", {"input": ["x"]}),
    ("bad-skill-id", {"skillId": "ab"}),
]

UNIQUE_CASES = [
    ("list", ["a", "a", "b", "  c  ", "", "a"]),
    ("string", "abc"),
    ("dict", {"k1": 1, "k2": 2}),
    ("scalar", 5),
    ("none", None),
]

PROJECT_FIXTURE = {
    "name": "Fixture\nProject",
    "documents": [
        {"fileId": "a" * 32, "projectId": "abcd", "name": "Doc A", "chunkCount": 3},
        {"fileId": "BAD", "projectId": "abcd"},
    ],
    "skills": {"enabledPacks": ["pack:a"], "enabledSkills": ["skill:a"]},
    "skillRuns": [{"runId": "r1", "status": "completed"}],
    "savedItems": [{"title": "Note"}],
    "artifacts": [{"artifactId": "art1", "filename": "f.txt"}],
    "createdAt": 1,
    "updatedAt": 2,
}

RAW_CASES = [
    ("not-json", "{not json"),
    ("scalar", "42"),
    ("list", "[]"),
    ("empty-dict", "{}"),
    ("full", None),  # written from the fixture
]


def _extract(source: str, name: str) -> str:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            segment = ast.get_source_segment(source, node)
            if node.decorator_list:
                decorators = "\n".join(
                    "@" + ast.unparse(decorator) for decorator in node.decorator_list
                )
                return decorators + "\n" + segment
            return segment
    raise SystemExit(f"could not extract {name}")


def build_namespace(root: Path) -> dict:
    namespace: dict = {}
    exec(compile(ERRORS.read_text(encoding="utf-8"), str(ERRORS), "exec"), namespace)  # noqa: S102

    import datetime as datetime_module
    import re
    import secrets
    import time
    from pathlib import Path as _Path
    from typing import Any

    projects_dir = root / ".projects"

    class _Config:
        PROJECTS_DIR = projects_dir

    namespace.update(
        {
            "PROJECTS_DIR": projects_dir,
            "MAX_PROJECTS": 40,
            "MAX_PROJECT_DOCUMENTS": 120,
            "MAX_PROJECT_SKILL_RUNS": 200,
            "MAX_PROJECT_SAVED_ITEMS": 200,
            "MAX_PROJECT_ARTIFACTS": 200,
            "json": json,
            "re": re,
            "secrets": secrets,
            "time": time,
            "datetime_module": datetime_module,
            "Path": _Path,
            "Any": Any,
            "utc_now_iso": lambda: "2025-10-09T08:53:20+00:00",
        }
    )

    source = PROJECTS.read_text(encoding="utf-8")
    for name in FUNCTIONS:
        exec(compile(_extract(source, name), str(PROJECTS), "exec"), namespace)  # noqa: S102

    # Pin the id source so the read path's generated ids are comparable.
    counter = {"n": 0}

    class _FixedSecrets:
        @staticmethod
        def token_hex(width: int) -> str:
            counter["n"] += 1
            return f"{counter['n']:016x}"

    namespace["secrets"] = _FixedSecrets
    return namespace


def outcome_no_code(call) -> dict:
    """The oracle raises a bare `TypeError` for a non-iterable, so there is no
    `AppError` code to compare — only the message. This port's error type is
    `AppError`, so comparing codes here would demand a value the oracle cannot
    produce. See `docs/PROJECTS_STORE.md`."""
    try:
        return {"ok": True, "result": call()}
    except Exception as exc:  # noqa: BLE001 - the probe reports the shape
        return {"ok": False, "error": str(exc)}


def outcome(call) -> dict:
    try:
        return {"ok": True, "result": call()}
    except Exception as exc:  # noqa: BLE001 - the probe reports the shape
        return {
            "ok": False,
            "error": str(exc),
            "code": getattr(getattr(exc, "code", None), "value", None),
        }


def main() -> int:
    for path in (PROJECTS, ERRORS):
        if not path.exists():
            print(f"missing {path}", file=sys.stderr)
            return 2

    root = Path(tempfile.mkdtemp(prefix="projects-parity-"))
    out: dict = {}
    try:
        ns = build_namespace(root)
        directory = ns["PROJECTS_DIR"]

        # --- ids --------------------------------------------------------------
        for index, value in enumerate(ID_CASES):
            out[f"id::{index}"] = outcome(lambda value=value: ns["validate_project_id"](value))

        # --- names ------------------------------------------------------------
        for index, value in enumerate(NAME_CASES):
            out[f"name::{index}"] = ns["normalize_project_name"](value)

        # --- documents --------------------------------------------------------
        for label, value in DOC_CASES:
            out[f"documents::{label}"] = ns["normalize_documents"](value)

        # --- safe int ---------------------------------------------------------
        for label, value, default in SAFE_INT_CASES:
            out[f"safe-int::{label}"] = ns["_safe_int"](value, default=default)

        # --- unique strings ---------------------------------------------------
        for label, value in UNIQUE_CASES:
            out[f"unique::{label}"] = outcome_no_code(lambda value=value: ns["unique_strings"](value))

        # --- skills -----------------------------------------------------------
        for label, value in SKILL_CASES:
            out[f"skills::{label}"] = outcome(lambda value=value: ns["normalize_project_skills"](value))

        # --- skill runs -------------------------------------------------------
        for label, value in RUN_CASES:
            out[f"run::{label}"] = outcome(lambda value=value: ns["normalize_skill_run"](value))

        # --- saved items and artifacts ----------------------------------------
        out["saved::minimal"] = ns["normalize_saved_items"]([{"title": "N"}])
        out["saved::with-id"] = ns["normalize_saved_items"]([{"id": "s1", "title": "N"}])
        out["saved::not-a-list"] = ns["normalize_saved_items"]("x")
        out["artifacts::minimal"] = ns["normalize_project_artifacts"]([{"artifactId": "a"}])
        out["artifacts::not-a-list"] = ns["normalize_project_artifacts"]({})

        # --- read_project -----------------------------------------------------
        def write_project(project_id: str, raw: str) -> None:
            target = directory / project_id
            target.mkdir(parents=True, exist_ok=True)
            (target / "project.json").write_text(raw, encoding="utf-8")

        # Missing, and every malformed shape.
        out["read::missing"] = ns["read_project"]("abcd")
        for label, raw in RAW_CASES:
            if raw is None:
                continue
            write_project("abcd", raw)
            out[f"read::{label}"] = outcome(lambda: ns["read_project"]("abcd"))

        write_project("abcd", json.dumps(PROJECT_FIXTURE, ensure_ascii=False, indent=2))
        out["read::fixture"] = ns["read_project"]("abcd")
        out["require::hit"] = ns["require_project"]("abcd")
        out["require::miss"] = outcome(lambda: ns["require_project"]("zzzz"))

        # --- list_projects ----------------------------------------------------
        write_project("efgh", json.dumps({"name": "Second", "updatedAt": 9}, indent=2))
        write_project("ijkl", json.dumps({"name": "Third", "updatedAt": 5}, indent=2))
        (directory / "not-a-project").mkdir(exist_ok=True)
        (directory / "a-file.txt").write_text("x", encoding="utf-8")
        out["pack-versions::direct"] = ns["normalize_project_pack_versions"](
            [{"packId": "aaa", "version": "1"}, {"packId": "aaa", "version": "2"},
             {"packId": "bbb"}]
        )
        listed = ns["list_projects"]()
        out["list::ids"] = [item.get("id") for item in listed]
        out["list::count"] = len(listed)
        out["list::first"] = listed[0] if listed else None
    finally:
        shutil.rmtree(root, ignore_errors=True)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
