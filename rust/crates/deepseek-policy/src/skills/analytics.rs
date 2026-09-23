//! Skill run journal and summaries; no host-global paths.
use super::{Result, error, registry::Registry, strings, text, truth};
use serde_json::{Value, json};

pub const FAILURE_SUGGESTIONS: &[(&str, &str)] = &[
    (
        "schema_validation_failed",
        "Review the Skill input/output schema and make sure required fields are present.",
    ),
    (
        "tool_policy_denied",
        "Review allowedTools and the Tool Policy audit before granting additional capability.",
    ),
    (
        "artifact_policy_failed",
        "Check artifactPolicy types and whether the run produced persistable content.",
    ),
    (
        "project_binding_failed",
        "Verify the project exists and projectBinding.enabled is true for this Skill.",
    ),
    (
        "llm_api_error",
        "Retry offline or check API key, model route, and upstream diagnostics.",
    ),
    (
        "timeout",
        "Reduce input size or retry with a simpler Skill run.",
    ),
    ("user_cancelled", "Run was cancelled before completion."),
    (
        "security_review_blocked",
        "Review Skill trust status, suspicious prompt findings, and allowedTools risk before approving the run.",
    ),
    (
        "unknown_error",
        "Inspect the linked trace and run metadata.",
    ),
];
pub fn safe_int(value: &Value, default: i64) -> i64 {
    crate::python_json::value_str(value)
        .trim()
        .parse::<i64>()
        .unwrap_or(default)
        .max(0)
}
fn unique(value: &Value) -> Vec<String> {
    let mut out = Vec::new();
    for s in strings(value) {
        if !out.contains(&s) {
            out.push(s);
        }
    }
    out
}
pub fn normalize(record: &Value) -> Result<Value> {
    let id = if truth(record, "skillRunId") {
        text(record, "skillRunId")
    } else {
        text(record, "runId")
    }
    .trim()
    .to_string();
    if id.is_empty() {
        return Err(error("skillRunId is required", 400));
    }
    let mut out = json!({"schemaVersion":"skill-run.v1","skillRunId":id.chars().take(80).collect::<String>()});
    for (key, limit) in [
        ("skillId", 80),
        ("skillVersion", 40),
        ("packId", 80),
        ("projectId", 80),
        ("model", 120),
        ("inputSummary", 600),
        ("outputSummary", 600),
        ("errorReason", 1200),
        ("failureCategory", 80),
        ("diagnosticSuggestion", 240),
        ("traceId", 80),
        ("runSecurityLevel", 40),
        ("securityReviewId", 120),
        ("toolGrantHashAtRun", 100),
        ("blockedReason", 500),
    ] {
        out[key] = text(record, key)
            .chars()
            .take(limit)
            .collect::<String>()
            .into();
    }
    out["status"] = if truth(record, "status") {
        text(record, "status")
    } else {
        "completed".into()
    }
    .chars()
    .take(40)
    .collect::<String>()
    .into();
    for key in ["startedAt", "completedAt"] {
        out[key] = text(record, key).into();
    }
    for key in ["offline", "redacted", "trustedAtRun", "approvalRequired"] {
        out[key] = truth(record, key).into();
    }
    for key in ["artifactIds", "savedItemIds"] {
        out[key] = json!(
            unique(&record[key])
                .into_iter()
                .take(40)
                .collect::<Vec<_>>()
        );
    }
    for (key, default) in [
        ("latencyMs", 0),
        (
            "artifactCount",
            out["artifactIds"].as_array().unwrap().len() as i64,
        ),
        (
            "savedItemCount",
            out["savedItemIds"].as_array().unwrap().len() as i64,
        ),
    ] {
        out[key] = safe_int(&record[key], default).into();
    }
    let mut links = json!({});
    let trace = text(&out, "traceId");
    let project = text(&out, "projectId");
    if !trace.is_empty() {
        links["trace"] = format!("/api/traces/{trace}").into();
    }
    if !project.is_empty() {
        links["projectRuns"] = format!("/api/workspace/projects/{project}/skill-runs").into();
        links["projectAnalytics"] =
            format!("/api/workspace/projects/{project}/skill-analytics").into();
        links["savedItems"] = if truth(&out, "savedItemIds") {
            format!("/api/workspace/projects/{project}/saved-items")
        } else {
            String::new()
        }
        .into();
        links["artifacts"] = if truth(&out, "artifactIds") {
            format!("/api/workspace/projects/{project}/artifacts")
        } else {
            String::new()
        }
        .into();
    }
    out["links"] = links;
    Ok(out)
}
pub fn read(registry: &Registry) -> Vec<Value> {
    std::fs::read_to_string(registry.data.join("runs/runs.jsonl"))
        .unwrap_or_default()
        .lines()
        .filter_map(|s| serde_json::from_str::<Value>(s).ok())
        .filter(|v| v.is_object())
        .filter_map(|v| normalize(&v).ok())
        .collect()
}
pub fn matches(run: &Value, filter: &Value) -> bool {
    ["status", "skillId", "packId", "projectId"]
        .iter()
        .all(|k| !truth(filter, k) || run[*k] == filter[*k])
}
pub fn list(registry: &Registry, filter: &Value, limit: usize) -> Vec<Value> {
    read(registry)
        .into_iter()
        .filter(|r| matches(r, filter))
        .take(if limit == 0 {
            usize::MAX
        } else {
            limit.min(500)
        })
        .collect()
}
pub fn get(registry: &Registry, id: &str) -> Result<Value> {
    read(registry)
        .into_iter()
        .find(|r| text(r, "skillRunId") == id.trim())
        .ok_or_else(|| error("Skill run not found", 404))
}
pub fn write(registry: &Registry, runs: &[Value]) -> Result<()> {
    let _guard = registry.mutation()?;
    let path = registry.data.join("runs/runs.jsonl");
    std::fs::create_dir_all(path.parent().unwrap()).map_err(|e| error(e.to_string(), 500))?;
    let mut text = String::new();
    for run in runs {
        text.push_str(&crate::python_json::dumps_default_separators(&normalize(
            run,
        )?));
        text.push('\n');
    }
    std::fs::write(path, text).map_err(|e| error(e.to_string(), 500))
}
pub fn append(registry: &Registry, record: &Value) -> Result<Value> {
    let _guard = registry.mutation()?;
    let normalized = normalize(record)?;
    let mut runs = read(registry);
    runs.retain(|v| v["skillRunId"] != normalized["skillRunId"]);
    runs.insert(0, normalized.clone());
    runs.truncate(500);
    write(registry, &runs)?;
    Ok(normalized)
}
pub fn delete(registry: &Registry, id: &str) -> Result<Value> {
    let _guard = registry.mutation()?;
    let runs = read(registry);
    let kept: Vec<_> = runs
        .iter()
        .filter(|r| text(r, "skillRunId") != id.trim())
        .cloned()
        .collect();
    write(registry, &kept)?;
    Ok(json!({"ok":true,"deleted":runs.len()-kept.len(),"skillRunId":id.trim()}))
}
pub fn cleanup(registry: &Registry, filter: &Value, keep: usize) -> Result<Value> {
    let _guard = registry.mutation()?;
    let runs = read(registry);
    let mut matched = 0;
    let kept: Vec<_> = runs
        .iter()
        .filter(|r| {
            if !matches(r, filter) {
                true
            } else {
                matched += 1;
                matched <= keep
            }
        })
        .cloned()
        .collect();
    write(registry, &kept)?;
    Ok(
        json!({"ok":true,"deleted":runs.len()-kept.len(),"remaining":kept.len(),"scope":{
        "status":text(filter,"status"),"skillId":text(filter,"skillId"),"packId":text(filter,"packId"),"projectId":text(filter,"projectId")}}),
    )
}
pub fn redact(registry: &Registry, id: &str) -> Result<Value> {
    let _guard = registry.mutation()?;
    let mut runs = read(registry);
    let run = runs
        .iter_mut()
        .find(|r| text(r, "skillRunId") == id.trim())
        .ok_or_else(|| error("Skill run not found", 404))?;
    for key in ["inputSummary", "outputSummary", "errorReason"] {
        run[key] = "[redacted]".into();
    }
    run["redacted"] = true.into();
    let result = run.clone();
    write(registry, &runs)?;
    Ok(json!({"ok":true,"run":result}))
}
pub fn summarize(value: &Value) -> String {
    let text = match value {
        Value::String(s) => s.clone(),
        Value::Array(v) => format!("{} items", v.len()),
        Value::Object(v) => {
            if v.is_empty() {
                "{}".into()
            } else {
                v.iter()
                    .take(8)
                    .map(|(k, v)| {
                        let v = match v {
                            Value::String(s) => s.chars().take(80).collect(),
                            Value::Array(v) => format!("{} items", v.len()),
                            Value::Object(v) => format!("{} fields", v.len()),
                            _ => crate::python_json::value_str(v),
                        };
                        format!("{k}={v}")
                    })
                    .collect::<Vec<_>>()
                    .join(", ")
            }
        }
        _ => crate::core_utils::text_or_empty(Some(value)),
    };
    text.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(600)
        .collect()
}
pub fn classify(message: &str) -> &'static str {
    let message = message.to_lowercase();
    for (category, words) in [
        (
            "schema_validation_failed",
            &["schema", "validation", "required"][..],
        ),
        (
            "tool_policy_denied",
            &[
                "tool policy",
                "permission",
                "denied",
                "forbidden",
                "not allowed",
            ],
        ),
        ("artifact_policy_failed", &["artifact"]),
        ("project_binding_failed", &["project"]),
        ("timeout", &["timeout", "timed out"]),
        ("user_cancelled", &["cancel"]),
        ("llm_api_error", &["api", "llm", "deepseek", "upstream"]),
    ] {
        if words.iter().any(|w| message.contains(w)) {
            return category;
        }
    }
    "unknown_error"
}
pub fn latency(start: &str, end: &str) -> i64 {
    match (
        chrono::DateTime::parse_from_rfc3339(start),
        chrono::DateTime::parse_from_rfc3339(end),
    ) {
        (Ok(a), Ok(b)) => (b - a).num_milliseconds().max(0),
        _ => 0,
    }
}
pub fn project_record(record: &Value, input: &Value) -> Value {
    let mut value = record.clone();
    for key in ["schemaVersion", "links", "redacted"] {
        value.as_object_mut().unwrap().remove(key);
    }
    value["input"] = if input.is_object() {
        input.clone()
    } else {
        json!({})
    };
    value
}
pub fn record(
    r: &Registry,
    skill: &Value,
    result: &Value,
    offline: bool,
    model: &str,
    failure: Option<(&str, &str)>,
) -> Result<Value> {
    let mut record = result.clone();
    let mut pack = String::new();
    for item in r.packs(true)? {
        if item["skills"]
            .as_array()
            .into_iter()
            .flatten()
            .any(|v| v["skillId"] == skill["skillId"])
        {
            pack = text(&item, "packId");
            break;
        }
    }
    record["packId"] = pack.into();
    record["skillVersion"] = skill["version"].clone();
    record["skillId"] = skill["skillId"].clone();
    record["offline"] = offline.into();
    record["model"] = if model.is_empty() {
        text(&result["output"], "model")
    } else {
        model.into()
    }
    .into();
    record["inputSummary"] = summarize(&result["input"]).into();
    if !truth(&record, "completedAt") {
        record["completedAt"] = r.now().into();
    }
    record["latencyMs"] =
        latency(&text(&record, "startedAt"), &text(&record, "completedAt")).into();
    for (items, ids, count, primary, fallback) in [
        (
            "artifacts",
            "artifactIds",
            "artifactCount",
            "artifactId",
            "id",
        ),
        (
            "savedItems",
            "savedItemIds",
            "savedItemCount",
            "id",
            "savedItemId",
        ),
    ] {
        let objects: Vec<_> = result[items]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|v| v.is_object())
            .collect();
        record[ids] = json!(
            objects
                .iter()
                .map(|v| if truth(v, primary) {
                    text(v, primary)
                } else {
                    text(v, fallback)
                })
                .collect::<Vec<_>>()
        );
        record[count] = objects.len().into();
    }
    record["outputSummary"] = summarize(if truth(&result["output"], "content") {
        &result["output"]["content"]
    } else {
        &result["output"]
    })
    .into();
    if let Some(meta) = result["security"].as_object() {
        for (key, v) in meta {
            record[key] = v.clone();
        }
    }
    if let Some((message, category)) = failure {
        let category = if category.is_empty() {
            classify(message)
        } else {
            category
        };
        record["status"] = "failed".into();
        record["outputSummary"] = "".into();
        record["errorReason"] = message.into();
        record["failureCategory"] = category.into();
        record["diagnosticSuggestion"] = FAILURE_SUGGESTIONS
            .iter()
            .find(|v| v.0 == category)
            .unwrap_or(FAILURE_SUGGESTIONS.last().unwrap())
            .1
            .into();
    }
    append(r, &record)
}
pub fn top_counts(values: impl IntoIterator<Item = String>) -> Value {
    let mut counts: Vec<(String, u64)> = Vec::new();
    for key in values {
        if key.is_empty() {
            continue;
        }
        if let Some(v) = counts.iter_mut().find(|v| v.0 == key) {
            v.1 += 1;
        } else {
            counts.push((key, 1));
        }
    }
    counts.sort_by_key(|v| std::cmp::Reverse(v.1));
    json!(
        counts
            .into_iter()
            .take(5)
            .map(|(id, count)| json!({"id":id,"count":count}))
            .collect::<Vec<_>>()
    )
}
pub fn summary(registry: &Registry, payload: &Value, days: usize) -> Value {
    let scope = if truth(payload, "scope") {
        text(payload, "scope")
    } else {
        "all".into()
    };
    let mut filter = json!({});
    let field = match scope.as_str() {
        "skill" => "skillId",
        "pack" => "packId",
        "project" => "projectId",
        _ => "",
    };
    if !field.is_empty() {
        filter[field] = text(payload, field).into();
    }
    let runs = list(registry, &filter, 0);
    let completed: Vec<_> = runs.iter().filter(|r| r["status"] == "completed").collect();
    let failed: Vec<_> = runs.iter().filter(|r| r["status"] == "failed").collect();
    let mut latencies: Vec<_> = completed
        .iter()
        .map(|r| safe_int(&r["latencyMs"], 0))
        .collect();
    latencies.sort();
    let percentile = |p: f64| -> i64 {
        if latencies.is_empty() {
            0
        } else {
            latencies[(((latencies.len() - 1) as f64 * p / 100.0).round_ties_even() as usize)
                .min(latencies.len() - 1)]
        }
    };
    let round = |value: f64, scale: f64| (value * scale).round_ties_even() / scale;
    let now = chrono::DateTime::parse_from_rfc3339(&registry.now())
        .unwrap()
        .date_naive();
    let mut trend = Vec::new();
    for offset in (0..days.clamp(1, 30)).rev() {
        let date = (now - chrono::Duration::days(offset as i64)).to_string();
        let matching: Vec<_> = runs
            .iter()
            .filter(|r| {
                let time = if truth(r, "completedAt") {
                    text(r, "completedAt")
                } else {
                    text(r, "startedAt")
                };
                chrono::DateTime::parse_from_rfc3339(&time)
                    .ok()
                    .map(|d| d.date_naive().to_string())
                    .as_deref()
                    == Some(&date)
            })
            .collect();
        trend.push(json!({"date":date,"runs":matching.len(),"failed":matching.iter().filter(|r|r["status"]=="failed").count()}));
    }
    json!({"scope":scope,"skillId":text(payload,"skillId"),"packId":text(payload,"packId"),"projectId":text(payload,"projectId"),
        "totalRuns":runs.len(),"successRuns":completed.len(),"failedRuns":failed.len(),
        "successRate":if runs.is_empty() {0.0} else {round(completed.len() as f64/runs.len() as f64,10000.0)},"failureRate":if runs.is_empty() {0.0} else {round(failed.len() as f64/runs.len() as f64,10000.0)},
        "averageLatencyMs":if latencies.is_empty() {0.0} else {round(latencies.iter().sum::<i64>() as f64/latencies.len() as f64,100.0)},"p50LatencyMs":percentile(50.0),"p90LatencyMs":percentile(90.0),
        "artifactCount":runs.iter().map(|r|safe_int(&r["artifactCount"],0)).sum::<i64>(),"savedItemCount":runs.iter().map(|r|safe_int(&r["savedItemCount"],0)).sum::<i64>(),
        "projectBindingRuns":runs.iter().filter(|r|truth(r,"projectId")).count(),"topSkills":top_counts(runs.iter().map(|r|text(r,"skillId"))),"topPacks":top_counts(runs.iter().map(|r|text(r,"packId"))),
        "failureCategories":top_counts(failed.iter().map(|r|text(r,"failureCategory"))),"securityLevels":top_counts(runs.iter().map(|r|text(r,"runSecurityLevel"))),
        "recentTrend":trend,"recentRuns":runs.iter().take(10).collect::<Vec<_>>(),"generatedAt":registry.now()})
}
