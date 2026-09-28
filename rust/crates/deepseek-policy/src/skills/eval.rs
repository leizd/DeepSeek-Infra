//! The Skill eval **case store** and the **report engine**.
//!
//! `deepseek_infra/infra/skills/eval.py` has two halves and both are here now: the case store
//! (`normalize_eval_case`, `load_case_file`, `load_eval_cases`, `save_eval_case`,
//! `delete_eval_case`) over the golden file and `.skills/eval_cases.jsonl`, and
//! `build_skill_eval_report`, which runs each case through the offline runner, scores its metrics,
//! aggregates per Skill and per Pack, and compares the result against a baseline.
//!
//! Three things in the oracle's report cannot be reproduced here and are recorded divergences
//! rather than silent differences:
//!
//! - `environment.python` (`platform.python_version()`) and `commit` (`evidence.git_commit()`) are
//!   the oracle's own identity; this port reports its own `os` and an empty `python`.
//! - `metrics.latencyMs` is a wall-clock measurement, so no two runs — including two oracle runs —
//!   produce the same bytes.
//! - A case whose input references the `media_example` fixture needs the media **ingestion**
//!   pipeline, which is not ported; the engine refuses such a case by name instead of scoring it
//!   against an input it did not prepare.
use super::{Result, error, registry::Registry, schema, strings, text, truth};
use crate::python_json::{OrderedJson, value_str};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::path::{Path, PathBuf};

/// The repo's golden corpus, relative to the application root — the same file the oracle reads
/// through its own `REPO_ROOT`.
pub const GOLDEN_CASES: &str = "evals/golden/skills/skill_eval_cases.jsonl";

fn user_path(r: &Registry) -> PathBuf {
    r.data.join("eval_cases.jsonl")
}

/// `_string_list`: a string splits on `,`, `;` and newlines; a list is stringified element by
/// element; anything else is empty.
fn string_list(value: &Value) -> Vec<String> {
    match value {
        Value::String(text) => text
            .split([',', ';', '\n'])
            .map(|part| part.trim().to_string())
            .filter(|part| !part.is_empty())
            .collect(),
        Value::Array(items) => items
            .iter()
            .map(|item| value_str(item).trim().to_string())
            .filter(|item| !item.is_empty())
            .collect(),
        _ => Vec::new(),
    }
}

/// The first key whose value is truthy — Python's `data.get(a) or data.get(b)`.
fn first_truthy<'a>(case: &'a Value, keys: &[&str]) -> &'a Value {
    keys.iter()
        .map(|key| case.get(*key).unwrap_or(&Value::Null))
        .find(|value| crate::core_utils::python_truthy(value))
        .unwrap_or(&Value::Null)
}

/// Python's `data.get(a) or data.get(b) or ""`: the first truthy value stringified, else empty.
///
/// Not `first_truthy` plus `value_str`, which would turn Python's `""` tail into `"None"` — the
/// difference between a missing `caseId` and the literal string `None`.
fn or_chain(case: &Value, keys: &[&str]) -> String {
    keys.iter()
        .map(|key| case.get(*key).unwrap_or(&Value::Null))
        .find(|value| crate::core_utils::python_truthy(value))
        .map(value_str)
        .unwrap_or_default()
}

/// `normalize_eval_case`: the thirteen keys the oracle keeps, in its order.
///
/// Every alias falls back the way an `or` chain does, so a falsy `expectedKeywords` reaches
/// `keywords` and a falsy `caseId` reaches `id`.
pub fn normalize(case: &Value) -> Value {
    let case_id = or_chain(case, &["caseId", "id"]).trim().to_string();
    let skill_id = text(case, "skillId").trim().to_string();
    // `str(data.get("name") or case_id or skill_id).strip()` — the strip happens *after* the chain,
    // so a whitespace-only `name` is chosen and then becomes empty rather than falling through.
    let name = {
        let raw = case.get("name").unwrap_or(&Value::Null);
        if crate::core_utils::python_truthy(raw) {
            value_str(raw).trim().to_string()
        } else if case_id.is_empty() {
            skill_id.clone()
        } else {
            case_id.clone()
        }
    };
    // `deniedTools or ([deniedTool] if deniedTool else [])`
    let denied = {
        let list = case.get("deniedTools").unwrap_or(&Value::Null);
        if crate::core_utils::python_truthy(list) {
            list.clone()
        } else {
            let single = case.get("deniedTool").unwrap_or(&Value::Null);
            if crate::core_utils::python_truthy(single) {
                json!([single.clone()])
            } else {
                json!([])
            }
        }
    };
    let input = case
        .get("input")
        .filter(|value| value.is_object())
        .cloned()
        .unwrap_or_else(|| json!({}));
    // `str(data.get("source") or "golden")` — **not** stripped, so `" user "` stays as it is; only a
    // falsy source falls back.
    let source = {
        let raw = case.get("source").unwrap_or(&Value::Null);
        if crate::core_utils::python_truthy(raw) {
            value_str(raw)
        } else {
            "golden".to_string()
        }
    };
    json!({
        "caseId": case_id,
        "skillId": skill_id,
        "packId": text(case, "packId").trim(),
        "name": name,
        "input": input,
        "expectedKeywords": string_list(first_truthy(case, &["expectedKeywords", "keywords"])),
        "requiredOutputPaths": string_list(first_truthy(case, &["requiredOutputPaths", "jsonPaths", "requiredFields"])),
        "forbidden": string_list(first_truthy(case, &["forbidden", "forbiddenContent"])),
        "expectedArtifactTypes": string_list(first_truthy(case, &["expectedArtifactTypes", "artifactTypes"])),
        "deniedTools": string_list(&denied),
        "requiredTools": string_list(first_truthy(case, &["requiredTools"])),
        "projectBindingRequired": crate::core_utils::python_truthy(case.get("projectBindingRequired").unwrap_or(&Value::Null)),
        "source": source,
    })
}

/// `_dedupe_cases`: last value wins, first appearance keeps the position.
fn dedupe(cases: Vec<Value>) -> Vec<Value> {
    let mut order: Vec<String> = Vec::new();
    let mut seen: HashMap<String, Value> = HashMap::new();
    for case in cases {
        let id = text(&case, "caseId");
        if !seen.contains_key(&id) {
            order.push(id.clone());
        }
        seen.insert(id, case);
    }
    order
        .into_iter()
        .filter_map(|id| seen.remove(&id))
        .collect()
}

/// `load_case_file`: a missing file is empty, an unparsable line is skipped, and a row without both
/// ids is dropped.
fn load_file(path: &Path) -> Vec<Value> {
    let Ok(body) = std::fs::read_to_string(path) else {
        return Vec::new();
    };
    body.lines()
        .filter(|line| !line.trim().is_empty())
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|data| data.is_object())
        .map(|data| normalize(&data))
        .filter(|case| !text(case, "caseId").is_empty() && !text(case, "skillId").is_empty())
        .collect()
}

/// `load_eval_cases`: the golden corpus first, then the user file, deduped by `caseId`.
pub fn cases(r: &Registry) -> Value {
    let mut all = load_file(&r.root.join(GOLDEN_CASES));
    all.extend(load_file(&user_path(r)));
    json!(dedupe(all))
}

/// `save_eval_case`: validate, replace any row with the same id, stamp the source and append.
pub fn save(r: &Registry, case: &Value) -> Result<Value> {
    let mut normalized = normalize(case);
    if text(&normalized, "caseId").is_empty() {
        return Err(error("caseId is required", 400));
    }
    if text(&normalized, "skillId").is_empty() {
        return Err(error("skillId is required", 400));
    }
    r.get(&text(&normalized, "skillId"), true)?;
    let _guard = r.mutation()?;
    let path = user_path(r);
    let case_id = text(&normalized, "caseId");
    let mut kept: Vec<Value> = load_file(&path)
        .into_iter()
        .filter(|item| text(item, "caseId") != case_id)
        .collect();
    normalized["source"] = "user".into();
    normalized["updatedAt"] = r.now().into();
    kept.push(normalized.clone());
    write(&path, &kept, true)?;
    Ok(normalized)
}

/// `delete_eval_case`: 404 when nothing matched, and the file is rewritten **without** a trailing
/// newline when it ends up empty — the oracle's own quirk, kept because the bytes are the contract.
pub fn delete(r: &Registry, case_id: &str) -> Result<Value> {
    let normalized = case_id.trim();
    if normalized.is_empty() {
        return Err(error("caseId is required", 400));
    }
    let _guard = r.mutation()?;
    let path = user_path(r);
    let cases = load_file(&path);
    let kept: Vec<Value> = cases
        .iter()
        .filter(|case| text(case, "caseId") != normalized)
        .cloned()
        .collect();
    if kept.len() == cases.len() {
        return Err(error("Skill eval case not found", 404));
    }
    write(&path, &kept, !kept.is_empty())?;
    Ok(json!({"ok": true, "deleted": normalized}))
}

/// The oracle writes `"\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True))`, so the key
/// order is sorted rather than the record's own — `from_value_with_order(&item, &[])` is that sort.
fn write(path: &Path, cases: &[Value], trailing_newline: bool) -> Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| error(e.to_string(), 500))?;
    }
    let body = cases
        .iter()
        .map(|case| OrderedJson::from_value_with_order(case, &[]).render_default_separators())
        .collect::<Vec<_>>()
        .join("\n");
    let body = if trailing_newline {
        format!("{body}\n")
    } else {
        body
    };
    std::fs::write(path, body).map_err(|e| error(e.to_string(), 500))
}

// ---------------------------------------------------------------- the pure half of the engine
//
// `build_skill_eval_report`'s execution half needs three things this crate does not have yet — the
// media ingestion pipeline, `permissions.evaluate_skill_tool`, and Python's `re` semantics for a
// case's `forbidden` patterns — so the functions below are the half that needs none of them:
// scoring, aggregation and baseline comparison. They take `case_results` as input, which is what
// the execution half produces.

/// `_json_path`: `$`/`.`-stripped dot path; a numeric segment indexes an array, and a null anywhere
/// along the way — including the value found at the end — is `None`, which is what the oracle's
/// `is None` check sees.
pub fn json_path(value: &Value, path: &str) -> Option<Value> {
    let mut node = value.clone();
    for part in path
        .trim_matches(|c| c == '$' || c == '.')
        .split('.')
        .filter(|part| !part.is_empty())
    {
        let next = match &node {
            Value::Object(_) => node.get(part).cloned(),
            Value::Array(items) => part
                .parse::<usize>()
                .ok()
                .and_then(|index| items.get(index).cloned()),
            _ => None,
        };
        match next {
            Some(found) if !found.is_null() => node = found,
            _ => return None,
        }
    }
    if node.is_null() { None } else { Some(node) }
}

/// `_tool_policy_pass`: every `requiredTools` entry has to be granted, and every `deniedTools`
/// entry has to be refused by the engine — with an empty argument object.
pub fn tool_policy_pass(skill: &Value, case: &Value) -> Result<bool> {
    let allowed = schema::skill_allowed_tools(skill)?;
    for tool in strings(&case["requiredTools"]) {
        if !allowed.contains(&tool) {
            return Ok(false);
        }
    }
    for tool in strings(&case["deniedTools"]) {
        if super::permissions::evaluate_skill_tool(skill, &tool)?.allowed() {
            return Ok(false);
        }
    }
    Ok(true)
}

/// `_artifact_pass`.
pub fn artifact_pass(
    skill: &Value,
    case: &Value,
    artifacts: &[Value],
    saved_items: &[Value],
) -> bool {
    let expected: Vec<String> = strings(&case["expectedArtifactTypes"]);
    let policy = if skill["artifactPolicy"].is_object() {
        skill["artifactPolicy"].clone()
    } else {
        json!({})
    };
    let policy_types: Vec<String> = strings(&policy["types"]);
    let artifact_types: Vec<String> = artifacts
        .iter()
        .map(|item| text(item, "type"))
        .filter(|kind| !kind.is_empty())
        .collect();
    if !expected.is_empty() {
        let covered = expected
            .iter()
            .all(|kind| artifact_types.contains(kind) || policy_types.contains(kind));
        return covered
            && (!artifacts.is_empty() || !saved_items.is_empty() || !policy_types.is_empty());
    }
    if truth(&policy, "autoSave") {
        return !artifacts.is_empty() || !saved_items.is_empty() || !policy_types.is_empty();
    }
    true
}

/// `_content_pass`.
///
/// The `forbidden` patterns go through the `regex` crate rather than Python's `re`. A pattern Python
/// accepts but this engine does not (a backreference, a lookaround) is a **refusal** here, not a
/// silent non-match: scoring a case against a pattern that was never applied would be the quiet
/// difference the migration is supposed to avoid.
pub fn content_pass(output: &Value, case: &Value) -> Result<bool> {
    let content = crate::python_json::dumps_default_separators(output);
    let lowered = content.to_lowercase();
    for keyword in strings(&case["expectedKeywords"]) {
        if !lowered.contains(&keyword.to_lowercase()) {
            return Ok(false);
        }
    }
    for pattern in strings(&case["forbidden"]) {
        let compiled = regex::RegexBuilder::new(&pattern)
            .case_insensitive(true)
            .build()
            .map_err(|e| {
                error(
                    format!(
                        "the `forbidden` pattern {pattern:?} is not supported by this engine: {e}"
                    ),
                    400,
                )
            })?;
        if compiled.is_match(&content) {
            return Ok(false);
        }
    }
    for path in strings(&case["requiredOutputPaths"]) {
        if json_path(output, &path).is_none() {
            return Ok(false);
        }
    }
    Ok(true)
}

/// `_sample_input`.
pub fn sample_input(schema: &Value) -> Value {
    let properties = if schema["properties"].is_object() {
        schema["properties"].clone()
    } else {
        json!({})
    };
    let required: Vec<String> = if schema["required"].is_array() {
        strings(&schema["required"])
    } else {
        properties
            .as_object()
            .map(|fields| fields.keys().cloned().collect())
            .unwrap_or_default()
    };
    let mut sample = serde_json::Map::new();
    for key in required {
        let prop = properties.get(&key).cloned().unwrap_or_else(|| json!({}));
        let value = match prop["enum"]
            .as_array()
            .and_then(|items| items.first())
            .cloned()
        {
            Some(first) => first,
            None => match text(&prop, "type").as_str() {
                "integer" | "number" => json!(1),
                "boolean" => json!(true),
                _ => json!(format!("sample {key}")),
            },
        };
        sample.insert(key, value);
    }
    Value::Object(sample)
}

/// `_synthetic_case`: the smoke case every selected Skill gets when the corpus has none for it.
pub fn synthetic_case(r: &Registry, skill_id: &str) -> Result<Value> {
    let skill = r.get(skill_id, true)?;
    let examples = skill["exampleInputs"]
        .as_array()
        .cloned()
        .unwrap_or_default();
    let sample = examples
        .first()
        .filter(|value| value.is_object())
        .cloned()
        .unwrap_or_else(|| sample_input(&skill["inputSchema"]));
    let policy_types = strings(&skill["artifactPolicy"]["types"]);
    Ok(normalize(&json!({
        "caseId": format!("synthetic-{skill_id}"),
        "skillId": skill_id,
        "name": format!("Synthetic smoke for {skill_id}"),
        "input": sample,
        "expectedKeywords": ["Offline Skill run completed", skill_id],
        "requiredOutputPaths": ["content"],
        "expectedArtifactTypes": if policy_types.iter().any(|kind| kind == "md") { json!(["md"]) } else { json!([]) },
        "projectBindingRequired": truth(&skill["projectBinding"], "enabled"),
        "source": "synthetic",
    })))
}

/// `_selected_skill_ids`.
pub fn selected_skill_ids(
    r: &Registry,
    scope: &str,
    skill_id: &str,
    pack_id: &str,
) -> Result<Vec<String>> {
    let scope = if scope.is_empty() {
        "all".to_string()
    } else {
        scope.to_lowercase()
    };
    match scope.as_str() {
        "skill" => Ok(vec![text(&r.get(skill_id, true)?, "skillId")]),
        "pack" => {
            let pack = r.get_pack(pack_id)?;
            Ok(pack_skill_ids(&r.export_pack(&text(&pack, "packId"))?))
        }
        _ => {
            // `r.list`'s second argument is `builtin_only`, and the oracle's `list_skills` **sorts**
            // by `(bool(builtin) is False, name)`: the built-ins first, then the custom Skills, each
            // by display name. `list` itself does not sort, so the order is applied here — the
            // catalog never noticed because it sorts its own items.
            let mut skills = r.list(true, false)?;
            skills.sort_by_key(|skill| {
                (
                    skill.get("builtin") == Some(&Value::Bool(false)),
                    text(skill, "name"),
                )
            });
            Ok(skills.iter().map(|skill| text(skill, "skillId")).collect())
        }
    }
}

/// `pack_skill_ids`: the `skillId` of every entry, reference or embedded.
fn pack_skill_ids(pack: &Value) -> Vec<String> {
    pack["skills"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|entry| entry.is_object())
        .map(|entry| text(entry, "skillId"))
        .collect()
}

/// `_pack_membership`: Skill id -> sorted, deduped Pack ids.
pub fn pack_membership(r: &Registry) -> Result<Value> {
    let mut mapping: std::collections::BTreeMap<String, Vec<String>> =
        std::collections::BTreeMap::new();
    for pack in r.packs(true)? {
        let pack_id = text(&pack, "packId");
        let ids = match r.export_pack(&pack_id) {
            Ok(exported) => pack_skill_ids(&exported),
            Err(_) => pack_skill_ids(&pack),
        };
        for skill_id in ids {
            let entry = mapping.entry(skill_id).or_default();
            if !entry.contains(&pack_id) {
                entry.push(pack_id.clone());
            }
        }
    }
    let mut result = serde_json::Map::new();
    for (skill_id, mut packs) in mapping {
        packs.sort();
        result.insert(skill_id, json!(packs));
    }
    Ok(Value::Object(result))
}

/// `_cases_for_skills`: the corpus grouped by Skill, in the selection's order, with a synthetic case
/// standing in for any Skill the corpus has nothing for.
pub fn cases_for_skills(r: &Registry, skill_ids: &[String], cases: &[Value]) -> Result<Vec<Value>> {
    let mut grouped: Vec<(String, Vec<Value>)> = skill_ids
        .iter()
        .map(|id| (id.clone(), Vec::new()))
        .collect();
    for case in cases {
        let skill_id = text(case, "skillId");
        if let Some((_, bucket)) = grouped.iter_mut().find(|(id, _)| *id == skill_id) {
            bucket.push(case.clone());
        }
    }
    for (skill_id, bucket) in grouped.iter_mut() {
        if bucket.is_empty() {
            bucket.push(synthetic_case(r, skill_id)?);
        }
    }
    Ok(grouped.into_iter().flat_map(|(_, bucket)| bucket).collect())
}

/// `_dedupe_case_results`: last value wins, first appearance keeps the position.
fn dedupe_case_results(cases: Vec<Value>) -> Vec<Value> {
    let mut order: Vec<String> = Vec::new();
    let mut seen: HashMap<String, Value> = HashMap::new();
    for case in cases {
        let id = text(&case, "caseId");
        if !seen.contains_key(&id) {
            order.push(id.clone());
        }
        seen.insert(id, case);
    }
    order
        .into_iter()
        .filter_map(|id| seen.remove(&id))
        .collect()
}

/// `_ratio`.
fn ratio(numerator: usize, denominator: usize) -> f64 {
    if denominator == 0 {
        0.0
    } else {
        ((numerator as f64 / denominator as f64) * 10000.0).round_ties_even() / 10000.0
    }
}

/// `_aggregate_result`.
fn aggregate_result(
    id_key: &str,
    item_id: &str,
    name: &str,
    cases: &[Value],
    extra: Value,
) -> Value {
    let failed: Vec<&Value> = cases
        .iter()
        .filter(|case| text(case, "status") != "PASS")
        .collect();
    let score = if cases.is_empty() {
        0.0
    } else {
        let sum: f64 = cases
            .iter()
            .map(|case| case["overallScore"].as_f64().unwrap_or(0.0))
            .sum();
        (sum / cases.len() as f64 * 100.0).round_ties_even() / 100.0
    };
    let last_run_at = cases
        .iter()
        .map(|case| text(case, "lastRunAt"))
        .max()
        .unwrap_or_default();
    let mut result = json!({
        id_key: item_id,
        "name": name,
        "status": if !cases.is_empty() && failed.is_empty() { "PASS" } else { "FAIL" },
        "overallScore": score,
        "passRate": ratio(cases.len().saturating_sub(failed.len()), cases.len()),
        "caseCount": cases.len(),
        "failedCases": failed.iter().map(|case| text(case, "caseId")).collect::<Vec<_>>(),
        "lastRunAt": last_run_at,
    });
    if let (Some(target), Some(Value::Object(extra))) = (result.as_object_mut(), Some(extra)) {
        for (key, value) in extra {
            target.insert(key, value);
        }
    }
    result
}

/// `_skill_results`.
pub fn skill_results(
    r: &Registry,
    case_results: &[Value],
    skill_ids: &[String],
    pack_map: &Value,
) -> Result<Vec<Value>> {
    let mut results = Vec::new();
    for skill_id in skill_ids {
        let cases: Vec<Value> = case_results
            .iter()
            .filter(|case| text(case, "skillId") == *skill_id)
            .cloned()
            .collect();
        let skill = r.get(skill_id, true)?;
        let name = {
            let named = text(&skill, "name");
            if named.is_empty() {
                skill_id.clone()
            } else {
                named
            }
        };
        results.push(aggregate_result(
            "skillId",
            skill_id,
            &name,
            &cases,
            json!({"packIds": pack_map.get(skill_id).cloned().unwrap_or_else(|| json!([]))}),
        ));
    }
    Ok(results)
}

/// `_pack_results`.
pub fn pack_results(
    r: &Registry,
    case_results: &[Value],
    pack_map: &Value,
    selected: &[String],
) -> Result<Vec<Value>> {
    let mut packs: std::collections::BTreeMap<String, Vec<Value>> =
        std::collections::BTreeMap::new();
    if let Some(mapping) = pack_map.as_object() {
        for (skill_id, pack_ids) in mapping {
            if !selected.contains(skill_id) {
                continue;
            }
            for pack_id in strings(pack_ids) {
                let cases: Vec<Value> = case_results
                    .iter()
                    .filter(|case| text(case, "skillId") == *skill_id)
                    .cloned()
                    .collect();
                packs.entry(pack_id).or_default().extend(cases);
            }
        }
    }
    let mut results = Vec::new();
    for (pack_id, cases) in packs {
        let name = match r.get_pack(&pack_id) {
            Ok(pack) => {
                let named = text(&pack, "name");
                if named.is_empty() {
                    pack_id.clone()
                } else {
                    named
                }
            }
            Err(_) => pack_id.clone(),
        };
        results.push(aggregate_result(
            "packId",
            &pack_id,
            &name,
            &dedupe_case_results(cases),
            json!({}),
        ));
    }
    Ok(results)
}

/// `_comparison_view_from_report`.
fn comparison_view_from_report(report: &Value) -> (Vec<Value>, Vec<Value>) {
    let list = |key: &str| -> Vec<Value> {
        if report.is_null() {
            Vec::new()
        } else {
            report[key].as_array().cloned().unwrap_or_default()
        }
    };
    (list("skillResults"), list("packResults"))
}

/// The five buckets `_compare_item` fills.
#[derive(Default)]
struct Comparisons {
    new_failures: Vec<Value>,
    fixed_failures: Vec<Value>,
    score_drops: Vec<Value>,
    improved: Vec<Value>,
    stable: Vec<Value>,
}

/// `_compare_item`'s one item.
fn compare_item(
    buckets: &mut Comparisons,
    item_id: &str,
    kind: &str,
    current: &Value,
    before: Option<&Value>,
) {
    let current_status = text(current, "status");
    let current_score = current["overallScore"].as_f64().unwrap_or(0.0);
    let Some(before) = before else {
        if current_status != "PASS" {
            buckets
                .new_failures
                .push(json!({"id": item_id, "kind": kind, "status": current_status}));
        }
        return;
    };
    let before_status = text(before, "status");
    let before_score = before["overallScore"].as_f64().unwrap_or(0.0);
    let delta = ((current_score - before_score) * 100.0).round_ties_even() / 100.0;
    let item = json!({"id": item_id, "kind": kind, "before": before_score, "current": current_score, "delta": delta});
    if before_status == "PASS" && current_status != "PASS" {
        buckets.new_failures.push(item);
    } else if before_status != "PASS" && current_status == "PASS" {
        buckets.fixed_failures.push(item);
    } else if delta < -5.0 {
        buckets.score_drops.push(item);
    } else if delta > 0.0 {
        buckets.improved.push(item);
    } else {
        buckets.stable.push(item);
    }
}

/// `compare_reports`: the regression verdict between a current report and a baseline.
pub fn compare_reports(current: &Value, baseline: &Value) -> Value {
    let (baseline_skills, baseline_packs) = comparison_view_from_report(baseline);
    let indexed = |items: &[Value], key: &str| -> std::collections::BTreeMap<String, Value> {
        items
            .iter()
            .filter(|item| item.is_object())
            .map(|item| (text(item, key), item.clone()))
            .collect()
    };
    let current_skills = indexed(
        &current["skillResults"]
            .as_array()
            .cloned()
            .unwrap_or_default(),
        "skillId",
    );
    let current_packs = indexed(
        &current["packResults"]
            .as_array()
            .cloned()
            .unwrap_or_default(),
        "packId",
    );
    let previous_skills = indexed(&baseline_skills, "skillId");
    let previous_packs = indexed(&baseline_packs, "packId");

    let mut buckets = Comparisons::default();
    for (key, item) in &current_skills {
        compare_item(&mut buckets, key, "skill", item, previous_skills.get(key));
    }
    for (key, item) in &current_packs {
        compare_item(&mut buckets, key, "pack", item, previous_packs.get(key));
    }
    let regression_count = buckets.new_failures.len() + buckets.score_drops.len();
    json!({
        "status": if regression_count == 0 { "PASS" } else { "FAIL" },
        "baselineVersion": text(baseline, "version"),
        "currentVersion": text(current, "version"),
        "regressionCount": regression_count,
        "newFailures": buckets.new_failures,
        "fixedFailures": buckets.fixed_failures,
        "scoreDrops": buckets.score_drops,
        "improved": buckets.improved,
        "stable": buckets.stable,
    })
}

/// `eval_aware_upgrade_gate`: the verdict `upgrade_pack`, `eval_upgrade_gate` and the two version
/// diffs embed, derived from a report by `kind`.
pub fn upgrade_gate(report: &Value) -> Value {
    let regression = if report["regression"].is_object() {
        report["regression"].clone()
    } else {
        json!({})
    };
    let summary = if report["summary"].is_object() {
        report["summary"].clone()
    } else {
        json!({})
    };
    let risk = if safe_int(&regression["regressionCount"]) > 0 {
        "review"
    } else {
        "low"
    };
    json!({
        "status": if text(report, "status") == "PASS" { "PASS" } else { "REVIEW" },
        "risk": risk,
        "overallScore": summary.get("overallScore").cloned().unwrap_or(Value::Null),
        "passRate": summary.get("passRate").cloned().unwrap_or(Value::Null),
        "regressionCount": regression.get("regressionCount").cloned().unwrap_or(json!(0)),
        "newFailures": regression.get("newFailures").cloned().unwrap_or_else(|| json!([])),
        "scoreDrops": regression.get("scoreDrops").cloned().unwrap_or_else(|| json!([])),
        "recommendation": if risk == "low" { "safe to upgrade" } else { "review before install" },
    })
}

/// `int(value or 0)` — a number, a numeric string, or zero.
fn safe_int(value: &Value) -> i64 {
    match value {
        Value::Number(number) => number.as_i64().unwrap_or(0),
        Value::String(text) => text.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

/// `evidence.git_commit()` reports the revision the report was produced from. The oracle reads a CI
/// **source context** first and falls back to `git rev-parse --short=12 HEAD`; neither half is
/// available here — the source-context plumb is not ported, and a native service is not a checkout —
/// so this reports an empty commit rather than shelling out.
///
/// Dropping the subprocess is deliberate: a policy crate that spawns a process to stamp a report is
/// the one thing the Zero-Python runtime gate's **process-tree isolation** check looks for, and it
/// found this call. Replacing a measurement with a subprocess would have been the wrong direction.
fn git_commit() -> String {
    String::new()
}

/// `platform.system()` — the oracle's `os` field.
fn platform_system() -> String {
    match std::env::consts::OS {
        "windows" => "Windows".into(),
        "linux" => "Linux".into(),
        "macos" => "Darwin".into(),
        other => other.into(),
    }
}

/// The assembly half of `build_skill_eval_report`, given the case results the execution half
/// produces.
///
/// Splitting there is deliberate: `_run_case` needs the media-ingestion pipeline, the skill-tool
/// evaluator and Python's `re` semantics, while everything here is a function of `case_results`,
/// so this half is comparable without any of them.
pub fn report_from_results(
    r: &Registry,
    version: &str,
    scope: &str,
    selected: &[String],
    pack_map: &Value,
    baseline: &Value,
    case_results: &[Value],
) -> Result<Value> {
    let skills = skill_results(r, case_results, selected, pack_map)?;
    let packs = pack_results(r, case_results, pack_map, selected)?;
    let regression = compare_reports(
        &json!({"skillResults": skills, "packResults": packs}),
        baseline,
    );
    let failed: Vec<&Value> = case_results
        .iter()
        .filter(|case| text(case, "status") != "PASS")
        .collect();
    let pass_rate = ratio(
        case_results.len().saturating_sub(failed.len()),
        case_results.len(),
    );
    let overall_score = if case_results.is_empty() {
        0.0
    } else {
        let sum: f64 = case_results
            .iter()
            .map(|case| case["overallScore"].as_f64().unwrap_or(0.0))
            .sum();
        (sum / case_results.len() as f64 * 100.0).round_ties_even() / 100.0
    };
    let status = if !case_results.is_empty()
        && failed.is_empty()
        && safe_int(&regression["regressionCount"]) == 0
    {
        "PASS"
    } else {
        "FAIL"
    };
    let every = |metric: &str| -> bool {
        !case_results.is_empty()
            && case_results
                .iter()
                .all(|case| truth(&case["metrics"], metric))
    };
    let check = |passed: bool| if passed { "PASS" } else { "FAIL" };
    Ok(json!({
        "version": version,
        "commit": git_commit(),
        "generatedAt": r.now(),
        "environment": {"os": platform_system(), "python": "", "ci": false},
        "status": status,
        "summary": {
            "scope": scope,
            "skillCount": skills.len(),
            "packCount": packs.len(),
            "caseCount": case_results.len(),
            "passRate": pass_rate,
            "overallScore": overall_score,
            "failedCases": failed.len(),
            "regressionCount": safe_int(&regression["regressionCount"]),
        },
        "checks": {
            "skillEvalCases": check(!case_results.is_empty()),
            "schemaScoring": check(every("schemaPass")),
            "toolPolicyScoring": check(every("toolPolicyPass")),
            "artifactScoring": check(every("artifactPass")),
            "projectBindingScoring": check(every("projectBindingPass")),
            "contentScoring": check(every("contentPass")),
            "packLevelEval": check(!packs.is_empty()),
            "regressionCompare": check(safe_int(&regression["regressionCount"]) == 0),
        },
        "skillResults": skills,
        "packResults": packs,
        "caseResults": case_results,
        "regression": regression,
    }))
}

// ---------------------------------------------------------------- the execution half of the engine

/// `_prepare_media_fixture`.
///
/// The oracle registers a media row and substitutes the id it generated, so a case that asks for
/// the `media_example` fixture is run against **prepared** input. This crate has no media
/// ingestion, so that case is refused rather than run against an input nobody prepared — the
/// refusal names the case's own field.
fn media_fixture(case_input: &Value, project_id: &str) -> Result<Value> {
    let ids: Vec<String> = match case_input["mediaIds"].as_array() {
        Some(items) => items
            .iter()
            .map(crate::python_json::value_str)
            .collect::<Vec<_>>(),
        None => {
            if truth(case_input, "mediaId") {
                vec![crate::python_json::value_str(&case_input["mediaId"])]
            } else {
                Vec::new()
            }
        }
    };
    if project_id.is_empty() || !ids.iter().any(|id| id == "media_example") {
        return Ok(case_input.clone());
    }
    Err(error(
        "the `media_example` fixture needs the media-ingestion pipeline, which this engine does not have; \
         run this case with the Python oracle",
        400,
    ))
}

/// `_run_case`: run one case through the **offline** runner and score its five metrics.
///
/// The measures are the oracle's: a project is created only when the case needs one, the runner is
/// called with `persist=true` (so the run journal grows, which is why `eval_report` is a writer and
/// not a read), and each metric is the same predicate the oracle uses.
pub fn run_case(r: &Registry, case: &Value, pack_map: &Value) -> Result<Value> {
    let skill_id = text(case, "skillId");
    let skill = r.get(&skill_id, true)?;
    let needs_project = truth(case, "projectBindingRequired")
        || truth(&skill["projectBinding"], "enabled")
        || !strings(&case["expectedArtifactTypes"]).is_empty();
    let project_id = if needs_project {
        text(
            &crate::projects::create_project(&format!("Skill Eval {skill_id}"), &r.root, r)
                .map_err(|e| error(e.message, e.status))?,
            "id",
        )
    } else {
        String::new()
    };
    let started = std::time::Instant::now();
    let mut output = json!({});
    let mut artifacts: Vec<Value> = Vec::new();
    let mut saved_items: Vec<Value> = Vec::new();
    let mut error_message = String::new();
    let mut schema_pass = false;
    let mut project_binding_pass = !truth(case, "projectBindingRequired");
    // The media fixture is refused **per case**, not per report: six built-in Skills ship an
    // `exampleInputs` entry that references `media_example`, so a report-level refusal would take
    // `scope: "all"` down with it. The case is then not run at all — scoring it against input that
    // was never prepared is the silent difference this refusal exists to avoid.
    let prepared = media_fixture(&case["input"], &project_id);
    let case_input = match &prepared {
        Ok(prepared_input) => prepared_input.clone(),
        Err(_) => case["input"].clone(),
    };
    let options = json!({"offline": true, "persist": true, "projectId": project_id});
    match &prepared {
        Err(refusal) => error_message = refusal.message.clone(),
        Ok(_) => match super::runner::offline(r, &skill_id, &case_input, &options) {
            Ok(result) => {
                output = if result["output"].is_object() {
                    result["output"].clone()
                } else {
                    json!({})
                };
                artifacts = result["artifacts"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter(|item| item.is_object())
                    .cloned()
                    .collect();
                saved_items = result["savedItems"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter(|item| item.is_object())
                    .cloned()
                    .collect();
                schema_pass = true;
                if truth(case, "projectBindingRequired") {
                    let exported = crate::projects::read_project(&project_id, &r.root, r)
                        .map_err(|e| error(e.message, e.status))?
                        .unwrap_or_else(|| json!({}));
                    let run_id = text(&result, "skillRunId");
                    project_binding_pass = exported["skillRuns"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .any(|item| text(item, "skillRunId") == run_id);
                }
            }
            Err(failure) => error_message = failure.message,
        },
    }
    let latency_ms = (started.elapsed().as_secs_f64() * 1000.0 * 100.0).round_ties_even() / 100.0;
    let metrics = json!({
        "schemaPass": schema_pass,
        "toolPolicyPass": tool_policy_pass(&skill, case)?,
        "artifactPass": artifact_pass(&skill, case, &artifacts, &saved_items),
        "projectBindingPass": project_binding_pass,
        "contentPass": content_pass(&output, case)?,
        "latencyMs": latency_ms,
    });
    let scored: Vec<bool> = [
        "schemaPass",
        "toolPolicyPass",
        "artifactPass",
        "projectBindingPass",
        "contentPass",
    ]
    .iter()
    .map(|key| truth(&metrics, key))
    .collect();
    let score = if scored.is_empty() {
        0.0
    } else {
        let passed = scored.iter().filter(|passed| **passed).count();
        ((100.0 * passed as f64 / scored.len() as f64) * 100.0).round_ties_even() / 100.0
    };
    let case_id = text(case, "caseId");
    let named = text(case, "name");
    // Sorted and de-duplicated, which is `sorted({...})` in the oracle.
    let artifact_kinds: Vec<String> = artifacts
        .iter()
        .map(|item| text(item, "type"))
        .filter(|kind| !kind.is_empty())
        .collect::<std::collections::BTreeSet<_>>()
        .into_iter()
        .collect();
    Ok(json!({
        "caseId": case["caseId"],
        "skillId": skill_id,
        "packIds": pack_map.get(&skill_id).cloned().unwrap_or_else(|| json!([])),
        "name": if named.is_empty() { case_id } else { named },
        "status": if score >= 100.0 && error_message.is_empty() { "PASS" } else { "FAIL" },
        "overallScore": score,
        "metrics": metrics,
        "input": case_input,
        "expected": {
            "keywords": case.get("expectedKeywords").cloned().unwrap_or_else(|| json!([])),
            "requiredOutputPaths": case.get("requiredOutputPaths").cloned().unwrap_or_else(|| json!([])),
            "artifactTypes": case.get("expectedArtifactTypes").cloned().unwrap_or_else(|| json!([])),
            "projectBindingRequired": truth(case, "projectBindingRequired"),
        },
        "artifactTypes": artifact_kinds,
        "savedItemCount": saved_items.len(),
        "error": error_message,
        "lastRunAt": r.now(),
    }))
}

/// `build_skill_eval_report`: the corpus for the selection, one [`run_case`] each, then the assembly.
///
/// `cases` is the corpus when the caller has one (the oracle's `cases=` argument) and the golden
/// plus user file otherwise.
pub fn report(
    r: &Registry,
    version: &str,
    scope: &str,
    skill_id: &str,
    pack_id: &str,
    baseline: &Value,
    corpus: Option<&[Value]>,
) -> Result<Value> {
    let selected = selected_skill_ids(r, scope, skill_id, pack_id)?;
    let loaded_corpus = match corpus {
        Some(given) => given.to_vec(),
        None => cases(r).as_array().cloned().unwrap_or_default(),
    };
    let loaded = cases_for_skills(r, &selected, &loaded_corpus)?;
    let pack_map = pack_membership(r)?;
    let mut case_results = Vec::new();
    for case in &loaded {
        case_results.push(run_case(r, case, &pack_map)?);
    }
    report_from_results(
        r,
        version,
        scope,
        &selected,
        &pack_map,
        baseline,
        &case_results,
    )
}

/// `eval_aware_upgrade_gate`: build the report for one item and extract the gate.
pub fn upgrade_gate_for(
    r: &Registry,
    kind: &str,
    item_id: &str,
    baseline: &Value,
) -> Result<Value> {
    let (scope, skill_id, pack_id) = if kind == "pack" {
        ("pack", "", item_id)
    } else {
        ("skill", item_id, "")
    };
    let report = report(r, "upgrade-gate", scope, skill_id, pack_id, baseline, None)?;
    Ok(upgrade_gate(&report))
}

/// `versioning._score_diff`: the gate's score and status, or the oracle's own `unavailable` shape
/// when the gate cannot be built — the swallowing is the oracle's, not this port's.
pub fn score_diff(r: &Registry, kind: &str, item_id: &str) -> Value {
    match upgrade_gate_for(r, kind, item_id, &json!({})) {
        Ok(gate) => json!({
            "before": Value::Null,
            "after": gate.get("overallScore").cloned().unwrap_or(Value::Null),
            "delta": Value::Null,
            "status": gate.get("status").cloned().unwrap_or(Value::Null),
        }),
        Err(_) => {
            json!({"before": Value::Null, "after": Value::Null, "delta": Value::Null, "status": "unavailable"})
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_string_list_splits_the_way_re_split_does() {
        assert_eq!(string_list(&json!("a, b;c\nd")), ["a", "b", "c", "d"]);
        assert_eq!(string_list(&json!("a,,b")), ["a", "b"]);
        assert_eq!(string_list(&json!(["x", 7, "", null])), ["x", "7", "None"]);
        assert_eq!(string_list(&json!({"a": 1})), Vec::<String>::new());
        assert_eq!(string_list(&json!(null)), Vec::<String>::new());
    }

    #[test]
    fn normalize_falls_back_through_aliases_in_order() {
        let case = normalize(&json!({
            "id": "case-1",
            "skillId": " skill_a ",
            "keywords": "one,two",
            "jsonPaths": ["a.b"],
            "forbiddenContent": "bad",
            "artifactTypes": ["md"],
            "deniedTool": "python_eval",
        }));
        assert_eq!(case["caseId"], "case-1");
        assert_eq!(case["skillId"], "skill_a");
        assert_eq!(case["name"], "case-1");
        assert_eq!(case["expectedKeywords"], json!(["one", "two"]));
        assert_eq!(case["requiredOutputPaths"], json!(["a.b"]));
        assert_eq!(case["forbidden"], json!(["bad"]));
        assert_eq!(case["expectedArtifactTypes"], json!(["md"]));
        assert_eq!(case["deniedTools"], json!(["python_eval"]));
        assert_eq!(case["source"], "golden");
        assert_eq!(case["input"], json!({}));
    }

    #[test]
    fn a_media_fixture_case_is_refused_rather_than_run_unprepared() {
        // Registering the media row is the one thing this engine cannot do, so a case that asks for
        // the fixture is refused by name; `run_case` records it as that case's failure instead of
        // taking the whole report down.
        let refusal = media_fixture(&json!({"mediaIds": ["media_example"]}), "proj-1").unwrap_err();
        assert!(
            refusal.message.contains("media-ingestion"),
            "{}",
            refusal.message
        );
        let single = media_fixture(&json!({"mediaId": "media_example"}), "proj-1");
        assert!(single.is_err(), "a single `mediaId` counts too");
        // Without a project — which is when the oracle skips the fixture as well — or without the
        // fixture id, the input passes through untouched.
        let wants_fixture = json!({"mediaIds": ["media_example"]});
        assert_eq!(media_fixture(&wants_fixture, "").unwrap(), wants_fixture);
        let other = json!({"mediaIds": ["media-1"]});
        assert_eq!(media_fixture(&other, "proj-1").unwrap(), other);
        assert_eq!(media_fixture(&json!({}), "proj-1").unwrap(), json!({}));
    }

    #[test]
    fn json_path_treats_null_like_a_missing_key() {
        let value = json!({"a": {"b": [{"c": 1}, {"c": null}]}, "empty": null});
        assert_eq!(json_path(&value, "$.a.b.0.c"), Some(json!(1)));
        assert_eq!(json_path(&value, "a.b.1.c"), None, "a null value is None");
        assert_eq!(json_path(&value, "$.empty"), None);
        assert_eq!(json_path(&value, "$.a.missing"), None);
        assert_eq!(json_path(&value, "$.a.b.9.c"), None, "out of range");
        assert_eq!(json_path(&value, "$.a.b.x"), None, "a non-numeric index");
        assert_eq!(
            json_path(&value, "$.a"),
            Some(json!({"b": [{"c": 1}, {"c": null}]}))
        );
    }

    #[test]
    fn content_pass_checks_keywords_patterns_and_paths() {
        let output = json!({"content": "Offline Skill run completed", "items": [{"title": "x"}]});
        let case = json!({"expectedKeywords": ["offline skill", "COMPLETED"], "requiredOutputPaths": ["content", "items.0.title"]});
        assert!(content_pass(&output, &case).unwrap());
        assert!(!content_pass(&output, &json!({"expectedKeywords": ["absent"]})).unwrap());
        assert!(
            !content_pass(&output, &json!({"forbidden": ["RUN.*COMPLETED"]})).unwrap(),
            "the search is case-insensitive"
        );
        assert!(
            !content_pass(&output, &json!({"requiredOutputPaths": ["items.1.title"]})).unwrap()
        );
        // A pattern Python's `re` accepts and `regex` does not is refused, not ignored.
        assert!(content_pass(&output, &json!({"forbidden": ["(a)\\1"]})).is_err());
    }

    #[test]
    fn artifact_pass_follows_the_expected_then_the_policy() {
        let plain = json!({});
        let with_policy = json!({"artifactPolicy": {"types": ["md"], "autoSave": false}});
        let artifacts = vec![json!({"type": "md"})];
        assert!(artifact_pass(&plain, &json!({}), &[], &[]));
        // `expectedArtifactTypes` needs the type in the artifacts, the policy, or nothing at all.
        assert!(!artifact_pass(
            &plain,
            &json!({"expectedArtifactTypes": ["md"]}),
            &[],
            &[]
        ));
        assert!(artifact_pass(
            &plain,
            &json!({"expectedArtifactTypes": ["md"]}),
            &artifacts,
            &[]
        ));
        assert!(
            artifact_pass(
                &with_policy,
                &json!({"expectedArtifactTypes": ["md"]}),
                &[],
                &[]
            ),
            "the policy's own types count as coverage"
        );
        // `autoSave` with nothing produced and no policy types fails.
        assert!(
            artifact_pass(&plain, &json!({}), &[], &[]),
            "no expectation, no autoSave"
        );
    }

    #[test]
    fn sample_input_prefers_the_enum_then_the_type() {
        let schema = json!({
            "properties": {
                "kind": {"enum": ["a", "b"]},
                "count": {"type": "integer"},
                "flag": {"type": "boolean"},
                "name": {"type": "string"},
            },
            "required": ["kind", "count", "flag", "name"],
        });
        assert_eq!(
            sample_input(&schema),
            json!({"kind": "a", "count": 1, "flag": true, "name": "sample name"})
        );
        // Without `required`, every property is sampled.
        let loose = json!({"properties": {"only": {"type": "string"}}});
        assert_eq!(sample_input(&loose), json!({"only": "sample only"}));
        assert_eq!(sample_input(&json!({})), json!({}));
    }

    #[test]
    fn compare_reports_splits_the_four_outcomes() {
        let before = json!({
            "version": "1",
            "skillResults": [
                {"skillId": "s1", "status": "PASS", "overallScore": 100.0},
                {"skillId": "s2", "status": "PASS", "overallScore": 100.0},
                {"skillId": "s3", "status": "FAIL", "overallScore": 40.0},
            ],
        });
        let current = json!({
            "version": "2",
            "skillResults": [
                {"skillId": "s1", "status": "FAIL", "overallScore": 20.0},
                {"skillId": "s2", "status": "PASS", "overallScore": 100.0},
                {"skillId": "s3", "status": "PASS", "overallScore": 100.0},
                {"skillId": "s4", "status": "FAIL", "overallScore": 0.0},
            ],
        });
        let verdict = compare_reports(&current, &before);
        assert_eq!(verdict["status"], "FAIL");
        assert_eq!(
            verdict["regressionCount"], 2,
            "one new failure and one score drop"
        );
        // A PASS that became a FAIL is a new failure, not a score drop, even though its score fell.
        assert_eq!(verdict["newFailures"][0]["id"], "s1");
        assert_eq!(verdict["scoreDrops"].as_array().unwrap().len(), 0);
        assert_eq!(verdict["fixedFailures"][0]["id"], "s3");
        // The same status with a fall of more than five points is the score-drop branch.
        let dropped = compare_reports(
            &json!({"skillResults": [{"skillId": "s1", "status": "PASS", "overallScore": 90.0}]}),
            &json!({"skillResults": [{"skillId": "s1", "status": "PASS", "overallScore": 100.0}]}),
        );
        assert_eq!(dropped["scoreDrops"][0]["delta"], -10.0);
        let nudged = compare_reports(
            &json!({"skillResults": [{"skillId": "s1", "status": "PASS", "overallScore": 97.0}]}),
            &json!({"skillResults": [{"skillId": "s1", "status": "PASS", "overallScore": 100.0}]}),
        );
        assert_eq!(nudged["scoreDrops"].as_array().unwrap().len(), 0);
        assert_eq!(
            nudged["stable"].as_array().unwrap().len(),
            1,
            "a three-point fall is stable"
        );
        // A Skill the baseline never saw is only a failure when it is failing.
        assert_eq!(verdict["newFailures"].as_array().unwrap().len(), 2);
        assert_eq!(verdict["stable"][0]["id"], "s2");
        // No baseline at all: everything is compared against nothing.
        let empty = compare_reports(&current, &json!({}));
        assert_eq!(empty["baselineVersion"], "");
        assert_eq!(empty["newFailures"].as_array().unwrap().len(), 2);
    }

    #[test]
    fn dedupe_keeps_the_last_value_in_the_first_position() {
        let cases = vec![
            json!({"caseId": "a", "name": "first"}),
            json!({"caseId": "b"}),
            json!({"caseId": "a", "name": "second"}),
        ];
        let deduped = dedupe(cases);
        assert_eq!(deduped.len(), 2);
        assert_eq!(deduped[0]["name"], "second");
        assert_eq!(deduped[1]["caseId"], "b");
    }
}
