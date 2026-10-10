//! Prompt templates shared by dry runs and the native runner.
use super::{text, truth};
use serde_json::Value;
fn repr(value: &Value, key: &str) -> String {
    crate::python_json::value_str(&value[key])
}
pub fn project_context(project: &Value) -> String {
    if !project.is_object() {
        return String::new();
    }
    let mut lines = vec![
        "[Project context]".into(),
        format!("projectId: {}", repr(project, "id")),
        format!("name: {}", repr(project, "name")),
    ];
    for (key, title, limit) in [
        ("documents", "documents:", 12),
        ("savedItems", "saved items:", 8),
        ("skillRuns", "recent Skill runs:", 8),
    ] {
        if let Some(items) = project[key].as_array().filter(|v| !v.is_empty()) {
            lines.push(title.into());
            for item in items.iter().take(limit).filter(|v| v.is_object()) {
                lines.push(match key {
                    "documents" => format!(
                        "- {} ({}, fileId={})",
                        repr(item, "name"),
                        repr(item, "kind"),
                        repr(item, "fileId")
                    ),
                    "savedItems" => format!("- {} ({})", repr(item, "title"), repr(item, "kind")),
                    _ => format!(
                        "- {} -> {} at {}",
                        repr(item, "skillId"),
                        repr(item, "status"),
                        repr(
                            item,
                            if truth(item, "completedAt") {
                                "completedAt"
                            } else {
                                "startedAt"
                            }
                        )
                    ),
                });
            }
        }
    }
    lines.join("\n")
}
pub fn system(skill: &Value, context: &str) -> String {
    [text(skill,"systemPrompt").trim(),"[Skill contract]","You are running inside DeepSeek Infra Skill System.","Use only the tools granted to this Skill. Tool calls still pass through Tool Policy.","Honor the input schema, output schema, memory policy, artifact policy, and project binding.",context].into_iter().filter(|s|!s.is_empty()).collect::<Vec<_>>().join("\n\n")
}
pub fn user(input: &Value) -> String {
    format!(
        "Run this Skill with the following JSON input:\n\n{}",
        serde_json::to_string_pretty(input).unwrap()
    )
}
pub fn offline(skill: &Value, input: &Value, context: &str) -> String {
    let title = ["title", "topic", "question"]
        .iter()
        .find(|k| truth(input, k))
        .map(|k| text(input, k))
        .unwrap_or_else(|| {
            if truth(skill, "name") {
                text(skill, "name")
            } else {
                "Skill output".into()
            }
        });
    let purpose = ["purpose", "task", "prompt"]
        .iter()
        .find(|k| truth(input, k))
        .map(|k| text(input, k))
        .unwrap_or_default();
    let mut lines = vec![
        format!("# {title}"),
        String::new(),
        format!(
            "Skill: {} ({})",
            repr(skill, "name"),
            repr(skill, "skillId")
        ),
    ];
    if !purpose.is_empty() {
        lines.extend([String::new(), "## Request".into(), purpose]);
    }
    lines.extend([String::new(),"## Result".into(),"Offline Skill run completed. The registry, schema, permissions, project binding, and artifact policy were exercised.".into()]);
    if !context.is_empty() {
        lines.extend([
            String::new(),
            "## Project Context".into(),
            context.chars().take(2000).collect(),
        ]);
    }
    lines.join("\n").trim().into()
}
