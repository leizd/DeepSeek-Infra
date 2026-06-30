"""Offline Skill quality evaluation and regression helpers."""

from __future__ import annotations

import copy
import json
import platform
import re
import time
from pathlib import Path
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.core.utils import utc_now_iso
from deepseek_infra.infra.data import projects
from deepseek_infra.infra.skills import evidence, registry
from deepseek_infra.infra.skills.pack import pack_skill_ids
from deepseek_infra.infra.skills.permissions import evaluate_skill_tool, skill_allowed_tools
from deepseek_infra.infra.skills.runner import run_skill

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDEN_CASES = REPO_ROOT / "evals" / "golden" / "skills" / "skill_eval_cases.jsonl"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def user_eval_cases_path() -> Path:
    return registry.SKILLS_DIR / "eval_cases.jsonl"


def load_case_file(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    cases: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            cases.append(normalize_eval_case(data))
    return [case for case in cases if case.get("caseId") and case.get("skillId")]


def load_eval_cases(*, include_user: bool = True, golden_path: Path = GOLDEN_CASES) -> list[dict[str, Any]]:
    cases = load_case_file(golden_path)
    if include_user:
        cases.extend(load_case_file(user_eval_cases_path()))
    return _dedupe_cases(cases)


def save_eval_case(case: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_eval_case(case)
    if not normalized.get("caseId"):
        raise AppError("caseId is required", code=ErrorCode.INVALID_PAYLOAD)
    if not normalized.get("skillId"):
        raise AppError("skillId is required", code=ErrorCode.INVALID_PAYLOAD)
    registry.get_skill(str(normalized["skillId"]), include_disabled=True)
    path = user_eval_cases_path()
    cases = [item for item in load_case_file(path) if item.get("caseId") != normalized["caseId"]]
    normalized["source"] = "user"
    normalized["updatedAt"] = utc_now_iso()
    cases.append(normalized)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in cases) + "\n", encoding="utf-8")
    return normalized


def delete_eval_case(case_id: str) -> dict[str, Any]:
    normalized = str(case_id or "").strip()
    if not normalized:
        raise AppError("caseId is required", code=ErrorCode.INVALID_PAYLOAD)
    path = user_eval_cases_path()
    cases = load_case_file(path)
    kept = [case for case in cases if case.get("caseId") != normalized]
    if len(kept) == len(cases):
        raise AppError("Skill eval case not found", code=ErrorCode.NOT_FOUND, status=404)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in kept) + ("\n" if kept else ""), encoding="utf-8")
    return {"ok": True, "deleted": normalized}


def build_skill_eval_report(
    *,
    version: str,
    scope: str = "all",
    skill_id: str = "",
    pack_id: str = "",
    baseline: dict[str, Any] | None = None,
    cases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected_skills = _selected_skill_ids(scope=scope, skill_id=skill_id, pack_id=pack_id)
    loaded_cases = _cases_for_skills(selected_skills, cases if cases is not None else load_eval_cases())
    pack_map = _pack_membership()
    case_results = [_run_case(case, pack_map=pack_map) for case in loaded_cases]
    skill_results = _skill_results(case_results, selected_skills, pack_map)
    pack_results = _pack_results(case_results, pack_map, selected_skills)
    regression = compare_reports(_comparison_view(skill_results, pack_results), baseline or {})
    failed_cases = [case for case in case_results if case["status"] != "PASS"]
    pass_rate = _ratio(len(case_results) - len(failed_cases), len(case_results))
    overall_score = round(sum(float(case.get("overallScore") or 0.0) for case in case_results) / len(case_results), 2) if case_results else 0.0
    status = "PASS" if case_results and not failed_cases and regression["regressionCount"] == 0 else "FAIL"
    return {
        "version": version,
        "commit": evidence.git_commit(),
        "generatedAt": utc_now_iso(),
        "environment": {"os": platform.system() or platform.platform(), "python": platform.python_version(), "ci": False},
        "status": status,
        "summary": {
            "scope": scope,
            "skillCount": len(skill_results),
            "packCount": len(pack_results),
            "caseCount": len(case_results),
            "passRate": pass_rate,
            "overallScore": overall_score,
            "failedCases": len(failed_cases),
            "regressionCount": regression["regressionCount"],
        },
        "checks": {
            "skillEvalCases": "PASS" if case_results else "FAIL",
            "schemaScoring": "PASS" if all(case["metrics"]["schemaPass"] for case in case_results) else "FAIL",
            "toolPolicyScoring": "PASS" if all(case["metrics"]["toolPolicyPass"] for case in case_results) else "FAIL",
            "artifactScoring": "PASS" if all(case["metrics"]["artifactPass"] for case in case_results) else "FAIL",
            "projectBindingScoring": "PASS" if all(case["metrics"]["projectBindingPass"] for case in case_results) else "FAIL",
            "contentScoring": "PASS" if all(case["metrics"]["contentPass"] for case in case_results) else "FAIL",
            "packLevelEval": "PASS" if pack_results else "FAIL",
            "regressionCompare": "PASS" if regression["regressionCount"] == 0 else "FAIL",
        },
        "skillResults": skill_results,
        "packResults": pack_results,
        "caseResults": case_results,
        "regression": regression,
    }


def compare_reports(current: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    baseline_view = _comparison_view_from_report(baseline)
    current_skills = {item["skillId"]: item for item in current.get("skillResults", []) if isinstance(item, dict)}
    baseline_skills = {item["skillId"]: item for item in baseline_view.get("skillResults", []) if isinstance(item, dict)}
    current_packs = {item["packId"]: item for item in current.get("packResults", []) if isinstance(item, dict)}
    baseline_packs = {item["packId"]: item for item in baseline_view.get("packResults", []) if isinstance(item, dict)}

    new_failures: list[dict[str, Any]] = []
    fixed_failures: list[dict[str, Any]] = []
    score_drops: list[dict[str, Any]] = []
    improved: list[dict[str, Any]] = []
    stable: list[dict[str, Any]] = []

    for key, item in sorted(current_skills.items()):
        before = baseline_skills.get(key)
        _compare_item(key, "skill", item, before, new_failures, fixed_failures, score_drops, improved, stable)
    for key, item in sorted(current_packs.items()):
        before = baseline_packs.get(key)
        _compare_item(key, "pack", item, before, new_failures, fixed_failures, score_drops, improved, stable)

    regression_count = len(new_failures) + len(score_drops)
    return {
        "status": "PASS" if regression_count == 0 else "FAIL",
        "baselineVersion": str(baseline.get("version") or ""),
        "currentVersion": str(current.get("version") or ""),
        "regressionCount": regression_count,
        "newFailures": new_failures,
        "fixedFailures": fixed_failures,
        "scoreDrops": score_drops,
        "improved": improved,
        "stable": stable,
    }


def normalize_eval_case(case: dict[str, Any]) -> dict[str, Any]:
    data = copy.deepcopy(case)
    case_id = str(data.get("caseId") or data.get("id") or "").strip()
    skill_id = str(data.get("skillId") or "").strip()
    expected_keywords = _string_list(data.get("expectedKeywords") or data.get("keywords"))
    required_paths = _string_list(data.get("requiredOutputPaths") or data.get("jsonPaths") or data.get("requiredFields"))
    forbidden = _string_list(data.get("forbidden") or data.get("forbiddenContent"))
    expected_artifacts = _string_list(data.get("expectedArtifactTypes") or data.get("artifactTypes"))
    denied_tools = _string_list(data.get("deniedTools") or ([data.get("deniedTool")] if data.get("deniedTool") else []))
    required_tools = _string_list(data.get("requiredTools"))
    normalized: dict[str, Any] = {
        "caseId": case_id,
        "skillId": skill_id,
        "packId": str(data.get("packId") or "").strip(),
        "name": str(data.get("name") or case_id or skill_id).strip(),
        "input": data.get("input") if isinstance(data.get("input"), dict) else {},
        "expectedKeywords": expected_keywords,
        "requiredOutputPaths": required_paths,
        "forbidden": forbidden,
        "expectedArtifactTypes": expected_artifacts,
        "deniedTools": denied_tools,
        "requiredTools": required_tools,
        "projectBindingRequired": bool(data.get("projectBindingRequired")),
        "source": str(data.get("source") or "golden"),
    }
    return normalized


def markdown_summary(report: dict[str, Any]) -> str:
    summary = _as_dict(report.get("summary"))
    lines = [
        "# Skill Eval Report",
        "",
        f"- Version: {report.get('version')}",
        f"- Status: {report.get('status')}",
        f"- Overall score: {summary.get('overallScore')}",
        f"- Pass rate: {summary.get('passRate')}",
        f"- Cases: {summary.get('caseCount')}",
        f"- Regressions: {summary.get('regressionCount')}",
        "",
        "| Skill | Score | Pass Rate | Cases | Failed |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for skill in report.get("skillResults") or []:
        if isinstance(skill, dict):
            lines.append(
                f"| {skill.get('skillId')} | {skill.get('overallScore')} | {skill.get('passRate')} | {skill.get('caseCount')} | {len(skill.get('failedCases') or [])} |"
            )
    lines.extend(["", "| Pack | Score | Pass Rate | Cases | Failed |", "| --- | ---: | ---: | ---: | ---: |"])
    for pack in report.get("packResults") or []:
        if isinstance(pack, dict):
            lines.append(
                f"| {pack.get('packId')} | {pack.get('overallScore')} | {pack.get('passRate')} | {pack.get('caseCount')} | {len(pack.get('failedCases') or [])} |"
            )
    return "\n".join(lines).strip() + "\n"


def _run_case(case: dict[str, Any], *, pack_map: dict[str, list[str]]) -> dict[str, Any]:
    skill_id = str(case.get("skillId") or "")
    skill = registry.get_skill(skill_id, include_disabled=True)
    project_binding = _as_dict(skill.get("projectBinding"))
    needs_project = bool(case.get("projectBindingRequired")) or bool(project_binding.get("enabled")) or bool(case.get("expectedArtifactTypes"))
    project_id = ""
    if needs_project:
        project = projects.create_project(f"Skill Eval {skill_id}")
        project_id = str(project["id"])
    started = time.perf_counter()
    output: dict[str, Any] = {}
    artifacts: list[dict[str, Any]] = []
    saved_items: list[dict[str, Any]] = []
    error = ""
    schema_pass = False
    project_binding_pass = not bool(case.get("projectBindingRequired"))
    case_input = _as_dict(case.get("input"))
    try:
        result = run_skill(skill_id, case_input, project_id=project_id, offline=True, persist=True)
        output = _as_dict(result.get("output"))
        artifacts = [item for item in (result.get("artifacts") or []) if isinstance(item, dict)]
        saved_items = [item for item in (result.get("savedItems") or []) if isinstance(item, dict)]
        schema_pass = True
        if case.get("projectBindingRequired"):
            exported = projects.export_project(project_id)
            project_binding_pass = any(item.get("skillRunId") == result.get("skillRunId") for item in exported.get("skillRuns") or [])
    except Exception as exc:  # pragma: no cover - exercised through report assertions
        error = str(exc)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    metrics = {
        "schemaPass": schema_pass,
        "toolPolicyPass": _tool_policy_pass(skill, case),
        "artifactPass": _artifact_pass(skill, case, artifacts, saved_items),
        "projectBindingPass": project_binding_pass,
        "contentPass": _content_pass(output, case),
        "latencyMs": latency_ms,
    }
    scored = [value for key, value in metrics.items() if key.endswith("Pass")]
    score = round(100.0 * sum(1 for value in scored if value) / len(scored), 2) if scored else 0.0
    return {
        "caseId": case.get("caseId"),
        "skillId": skill_id,
        "packIds": pack_map.get(skill_id, []),
        "name": case.get("name") or case.get("caseId"),
        "status": "PASS" if score >= 100.0 and not error else "FAIL",
        "overallScore": score,
        "metrics": metrics,
        "input": case_input,
        "expected": {
            "keywords": case.get("expectedKeywords") or [],
            "requiredOutputPaths": case.get("requiredOutputPaths") or [],
            "artifactTypes": case.get("expectedArtifactTypes") or [],
            "projectBindingRequired": bool(case.get("projectBindingRequired")),
        },
        "artifactTypes": sorted({str(item.get("type") or "") for item in artifacts if item.get("type")}),
        "savedItemCount": len(saved_items),
        "error": error,
        "lastRunAt": utc_now_iso(),
    }


def _tool_policy_pass(skill: dict[str, Any], case: dict[str, Any]) -> bool:
    allowed = set(skill_allowed_tools(skill))
    for tool in case.get("requiredTools") or []:
        if tool not in allowed:
            return False
    for tool in case.get("deniedTools") or []:
        decision = evaluate_skill_tool(skill, str(tool), {})
        if decision.allowed:
            return False
    return True


def _artifact_pass(skill: dict[str, Any], case: dict[str, Any], artifacts: list[dict[str, Any]], saved_items: list[dict[str, Any]]) -> bool:
    expected = [str(item) for item in (case.get("expectedArtifactTypes") or [])]
    policy = _as_dict(skill.get("artifactPolicy"))
    policy_types = {str(item) for item in (policy.get("types") or [])}
    artifact_types = {str(item.get("type") or "") for item in artifacts}
    if expected:
        return all(item in artifact_types or item in policy_types for item in expected) and bool(artifacts or saved_items or policy_types)
    if policy.get("autoSave"):
        return bool(artifacts or saved_items or policy_types)
    return True


def _content_pass(output: dict[str, Any], case: dict[str, Any]) -> bool:
    content = json.dumps(output, ensure_ascii=False)
    lowered = content.lower()
    for keyword in case.get("expectedKeywords") or []:
        if str(keyword).lower() not in lowered:
            return False
    for pattern in case.get("forbidden") or []:
        if re.search(str(pattern), content, flags=re.IGNORECASE):
            return False
    for path in case.get("requiredOutputPaths") or []:
        if _json_path(output, str(path)) is None:
            return False
    return True


def _json_path(value: Any, path: str) -> Any:
    node = value
    for part in [item for item in path.strip("$.").split(".") if item]:
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit():
            index = int(part)
            node = node[index] if 0 <= index < len(node) else None
        else:
            return None
        if node is None:
            return None
    return node


def _selected_skill_ids(*, scope: str, skill_id: str, pack_id: str) -> list[str]:
    scope = str(scope or "all").lower()
    if scope == "skill":
        return [registry.get_skill(skill_id, include_disabled=True)["skillId"]]
    if scope == "pack":
        pack = registry.get_pack(pack_id)
        return pack_skill_ids(registry.export_pack(pack["packId"]))
    return [item["skillId"] for item in registry.list_skills(include_disabled=True)]


def _pack_membership() -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for pack in registry.list_packs():
        try:
            skill_ids = pack_skill_ids(registry.export_pack(str(pack.get("packId") or "")))
        except AppError:
            skill_ids = pack_skill_ids(pack)
        for skill_id in skill_ids:
            mapping.setdefault(skill_id, []).append(str(pack.get("packId") or ""))
    return {key: sorted(set(value)) for key, value in mapping.items()}


def _cases_for_skills(skill_ids: list[str], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_skill: dict[str, list[dict[str, Any]]] = {skill_id: [] for skill_id in skill_ids}
    for case in cases:
        skill_id = str(case.get("skillId") or "")
        if skill_id in by_skill:
            by_skill[skill_id].append(case)
    for skill_id in skill_ids:
        if not by_skill[skill_id]:
            by_skill[skill_id].append(_synthetic_case(skill_id))
    return [case for skill_id in skill_ids for case in by_skill[skill_id]]


def _synthetic_case(skill_id: str) -> dict[str, Any]:
    skill = registry.get_skill(skill_id, include_disabled=True)
    examples = _as_list(skill.get("exampleInputs"))
    input_schema = _as_dict(skill.get("inputSchema"))
    artifact_policy = _as_dict(skill.get("artifactPolicy"))
    sample = examples[0] if examples and isinstance(examples[0], dict) else _sample_input(input_schema)
    return normalize_eval_case(
        {
            "caseId": f"synthetic-{skill_id}",
            "skillId": skill_id,
            "name": f"Synthetic smoke for {skill_id}",
            "input": sample,
            "expectedKeywords": ["Offline Skill run completed", skill_id],
            "requiredOutputPaths": ["content"],
            "expectedArtifactTypes": ["md"] if "md" in _string_list(artifact_policy.get("types")) else [],
            "projectBindingRequired": bool(_as_dict(skill.get("projectBinding")).get("enabled")),
            "source": "synthetic",
        }
    )


def _sample_input(schema: dict[str, Any]) -> dict[str, Any]:
    properties = _as_dict(schema.get("properties"))
    required = _as_list(schema.get("required")) if isinstance(schema.get("required"), list) else list(properties)
    sample: dict[str, Any] = {}
    for key in required:
        prop = _as_dict(properties.get(key))
        enum_values = _as_list(prop.get("enum"))
        if enum_values:
            sample[str(key)] = enum_values[0]
        elif prop.get("type") in {"integer", "number"}:
            sample[str(key)] = 1
        elif prop.get("type") == "boolean":
            sample[str(key)] = True
        else:
            sample[str(key)] = f"sample {key}"
    return sample


def _skill_results(case_results: list[dict[str, Any]], skill_ids: list[str], pack_map: dict[str, list[str]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for skill_id in skill_ids:
        cases = [case for case in case_results if case.get("skillId") == skill_id]
        skill = registry.get_skill(skill_id, include_disabled=True)
        results.append(_aggregate_result("skillId", skill_id, str(skill.get("name") or skill_id), cases, {"packIds": pack_map.get(skill_id, [])}))
    return results


def _pack_results(case_results: list[dict[str, Any]], pack_map: dict[str, list[str]], selected_skills: list[str]) -> list[dict[str, Any]]:
    packs: dict[str, list[dict[str, Any]]] = {}
    selected = set(selected_skills)
    for skill_id, pack_ids in pack_map.items():
        if skill_id not in selected:
            continue
        for pack_id in pack_ids:
            packs.setdefault(pack_id, []).extend([case for case in case_results if case.get("skillId") == skill_id])
    results: list[dict[str, Any]] = []
    for pack_id, cases in sorted(packs.items()):
        try:
            pack = registry.get_pack(pack_id)
            name = str(pack.get("name") or pack_id)
        except AppError:
            name = pack_id
        results.append(_aggregate_result("packId", pack_id, name, _dedupe_case_results(cases), {}))
    return results


def _aggregate_result(id_key: str, item_id: str, name: str, cases: list[dict[str, Any]], extra: dict[str, Any]) -> dict[str, Any]:
    failed = [case for case in cases if case.get("status") != "PASS"]
    score = round(sum(float(case.get("overallScore") or 0.0) for case in cases) / len(cases), 2) if cases else 0.0
    result = {
        id_key: item_id,
        "name": name,
        "status": "PASS" if cases and not failed else "FAIL",
        "overallScore": score,
        "passRate": _ratio(len(cases) - len(failed), len(cases)),
        "caseCount": len(cases),
        "failedCases": [str(case.get("caseId") or "") for case in failed],
        "lastRunAt": max((str(case.get("lastRunAt") or "") for case in cases), default=""),
    }
    result.update(extra)
    return result


def _comparison_view(skill_results: list[dict[str, Any]], pack_results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"skillResults": skill_results, "packResults": pack_results}


def _comparison_view_from_report(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {"skillResults": [], "packResults": []}
    return {
        "skillResults": report.get("skillResults") if isinstance(report.get("skillResults"), list) else [],
        "packResults": report.get("packResults") if isinstance(report.get("packResults"), list) else [],
    }


def _compare_item(
    item_id: str,
    kind: str,
    current: dict[str, Any],
    before: dict[str, Any] | None,
    new_failures: list[dict[str, Any]],
    fixed_failures: list[dict[str, Any]],
    score_drops: list[dict[str, Any]],
    improved: list[dict[str, Any]],
    stable: list[dict[str, Any]],
) -> None:
    current_status = str(current.get("status") or "")
    current_score = float(current.get("overallScore") or 0.0)
    if before is None:
        if current_status != "PASS":
            new_failures.append({"id": item_id, "kind": kind, "status": current_status})
        return
    before_status = str(before.get("status") or "")
    before_score = float(before.get("overallScore") or 0.0)
    delta = round(current_score - before_score, 2)
    item = {"id": item_id, "kind": kind, "before": before_score, "current": current_score, "delta": delta}
    if before_status == "PASS" and current_status != "PASS":
        new_failures.append(item)
    elif before_status != "PASS" and current_status == "PASS":
        fixed_failures.append(item)
    elif delta < -5.0:
        score_drops.append(item)
    elif delta > 0:
        improved.append(item)
    else:
        stable.append(item)


def _dedupe_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for case in cases:
        seen[str(case.get("caseId") or "")] = case
    return list(seen.values())


def _dedupe_case_results(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for case in cases:
        seen[str(case.get("caseId") or "")] = case
    return list(seen.values())


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,;\n]", value) if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _ratio(numerator: int, denominator: int) -> float:
    return round(float(numerator) / float(denominator), 4) if denominator else 0.0
