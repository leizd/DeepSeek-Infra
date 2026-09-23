//! Skill bindings and run/artifact projections on the existing native project store.
use super::{Result, registry::Registry, strings, text, truth};
use crate::{entropy::Entropy, projects};
use serde_json::{Value, json};

pub fn require(r: &Registry, id: &str) -> Result<Value> {
    projects::require_project(id, &r.root, r)
}
pub fn binding(r: &Registry, id: &str) -> Result<Value> {
    projects::normalize_project_skills(require(r, id)?.get("skills"))
}
pub fn set_binding(r: &Registry, id: &str, desired: &Value) -> Result<Value> {
    let _guard = crate::mutation_gate::mutation_scope(None, &r.root)
        .map_err(|e| super::error(e.to_string(), 409))?;
    let mut project = require(r, id)?;
    let previous = projects::normalize_project_skills(project.get("skills"))?;
    let mut value = desired.clone();
    value["recentSkills"] = previous["recentSkills"].clone();
    let default = text(&value, "defaultSkill");
    let mut skills = strings(&value["enabledSkills"]);
    if !default.is_empty() && !skills.contains(&default) {
        skills.insert(0, default);
    }
    value["enabledSkills"] = json!(skills);
    let normalized = projects::normalize_project_skills(Some(&value))?;
    project["skills"] = normalized.clone();
    project["updatedAt"] = r.now_millis().into();
    projects::write_project(&r.root, &project, r)?;
    Ok(normalized)
}
pub fn enable_pack(r: &Registry, project: &str, id: &str, version: &str) -> Result<Value> {
    let pack = r.get_pack(id)?;
    let resolved: Vec<_> = pack["skills"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|e| text(e, "skillId"))
        .filter(|id| r.get(id, true).is_ok())
        .collect();
    let mut value = binding(r, project)?;
    let mut skills = strings(&value["enabledSkills"]);
    for id in &resolved {
        if !skills.contains(id) {
            skills.push(id.clone());
        }
    }
    value["enabledSkills"] = json!(skills);
    if !truth(&value, "defaultSkill") {
        value["defaultSkill"] = resolved.first().cloned().unwrap_or_default().into();
    }
    let mut packs = strings(&value["enabledPacks"]);
    let id = text(&pack, "packId");
    if !packs.contains(&id) {
        packs.push(id.clone());
    }
    value["enabledPacks"] = json!(packs);
    let mut versions: Vec<_> = value["enabledPackVersions"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|v| text(v, "packId") != id)
        .cloned()
        .collect();
    versions.push(json!({"packId":id,"version":if version.is_empty() {text(&pack,"version")} else {version.into()},"installedAt":r.now()}));
    value["enabledPackVersions"] = versions.into();
    set_binding(r, project, &value)
}
pub fn append_run(r: &Registry, id: &str, run: &Value) -> Result<Value> {
    let _guard = crate::mutation_gate::mutation_scope(None, &r.root)
        .map_err(|e| super::error(e.to_string(), 409))?;
    let mut project = require(r, id)?;
    let run = projects::normalize_skill_run(run.as_object().unwrap(), r)?;
    let mut runs = projects::normalize_skill_runs(project.get("skillRuns"), r)?;
    runs.retain(|v| v["skillRunId"] != run["skillRunId"]);
    runs.insert(0, run.clone());
    runs.truncate(200);
    project["skillRuns"] = runs.into();
    let mut skills = projects::normalize_project_skills(project.get("skills"))?;
    let skill = text(&run, "skillId");
    if !skill.is_empty() {
        let mut enabled = strings(&skills["enabledSkills"]);
        if !enabled.contains(&skill) {
            enabled.insert(0, skill.clone());
        }
        skills["enabledSkills"] = enabled.into();
        let mut recent = strings(&skills["recentSkills"]);
        recent.retain(|s| s != &skill);
        recent.insert(0, skill.clone());
        recent.truncate(20);
        skills["recentSkills"] = recent.into();
        if !truth(&skills, "defaultSkill") {
            skills["defaultSkill"] = skill.into();
        }
    }
    project["skills"] = skills;
    project["updatedAt"] = r.now_millis().into();
    projects::write_project(&r.root, &project, r)?;
    Ok(run)
}
pub fn save_item(
    r: &Registry,
    id: &str,
    title: &str,
    content: &str,
    source: &Value,
) -> Result<Value> {
    let _guard = crate::mutation_gate::mutation_scope(None, &r.root)
        .map_err(|e| super::error(e.to_string(), 409))?;
    let mut project = require(r, id)?;
    let now = r.now_millis();
    let item = json!({"id":format!("saved-{}",r.new_id()?),"title":if title.is_empty() {"Skill output"} else {title}.chars().take(160).collect::<String>(),"kind":"skill_output",
        "content":content.chars().take(80_000).collect::<String>(),"source":source,"createdAt":now});
    let mut items = projects::normalize_saved_items(project.get("savedItems"), r)?;
    items.insert(0, item.clone());
    items.truncate(200);
    project["savedItems"] = items.into();
    project["updatedAt"] = now.into();
    projects::write_project(&r.root, &project, r)?;
    Ok(item)
}
pub fn link_artifact(r: &Registry, id: &str, artifact: &Value) -> Result<()> {
    let _guard = crate::mutation_gate::mutation_scope(None, &r.root)
        .map_err(|e| super::error(e.to_string(), 409))?;
    let mut project = require(r, id)?;
    let normalized = projects::normalize_project_artifacts(Some(&json!([artifact])));
    if let Some(value) = normalized.first() {
        let mut items = projects::normalize_project_artifacts(project.get("artifacts"));
        items.retain(|v| v["artifactId"] != value["artifactId"]);
        items.insert(0, value.clone());
        items.truncate(200);
        project["artifacts"] = items.into();
        project["updatedAt"] = r.now_millis().into();
        projects::write_project(&r.root, &project, r)?;
    }
    Ok(())
}
pub fn migration_targets(r: &Registry, id: &str) -> Result<Value> {
    let (mut bindings, mut runs, mut saved) = (0, 0, 0);
    for row in projects::list_projects(&r.root, r)? {
        let project = require(r, &text(&row, "id"))?;
        if strings(&project["skills"]["enabledSkills"]).contains(&id.to_string()) {
            bindings += 1;
        }
        runs += project["skillRuns"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|v| text(v, "skillId") == id)
            .count();
        saved += project["savedItems"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|v| text(&v["source"], "skillId") == id)
            .count();
    }
    Ok(json!({"projectBindings":bindings,"skillRuns":runs,"savedMetadata":saved}))
}
