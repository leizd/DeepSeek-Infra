//! Projection for `infra/workspace/projects.py` and its child stores.
//!
//! Legacy list entries and Workspace 2.0 project details are different public
//! contracts. Children live in separate stores; legacy inline arrays are not
//! substitutes. Reads do not open a mutation scope. Saved-item and artifact
//! create, update, and delete write their project JSON store and then touch
//! `project.json`.

use std::cmp::Reverse;
use std::path::Path;

use serde_json::{Value, json};

use crate::app_error::{AppError, codes};
use crate::core_utils::{python_int_opt, python_truthy};
use crate::entropy::Entropy;
use crate::{memory, projects as legacy, workspace_schema as schema};

const MAX_SAVED_ITEMS: usize = 1_000;
const MAX_ARTIFACTS: usize = 500;

fn truthy(value: Option<&Value>) -> Option<&Value> {
    value.filter(|value| python_truthy(value))
}

fn text(value: Option<&Value>) -> String {
    schema::python_str(truthy(value))
}

fn integer(value: Option<&Value>, fallback: i64) -> Option<i64> {
    truthy(value).map_or(Some(fallback), |value| python_int_opt(Some(value)))
}

fn iso(value: i64) -> String {
    schema::timestamp_ms_to_iso(Some(&json!(value)))
}

fn source(value: Option<&Value>) -> Value {
    schema::normalize_source_ref(value.unwrap_or(&Value::Null))
}

fn records(value: Option<&Value>) -> impl Iterator<Item = &Value> {
    value
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter(|item| item.is_object())
}

fn coerce_timestamp(value: Option<&Value>, fallback: i64) -> i64 {
    integer(value, 0)
        .filter(|value| *value > 0)
        .unwrap_or(fallback)
}

pub fn normalize_conversation(value: &Value, entropy: &dyn Entropy) -> Value {
    let raw_id = text(truthy(value.get("conversationId")).or_else(|| value.get("id")));
    let mut id: String = raw_id.trim().chars().take(80).collect();
    if id.is_empty() {
        id = format!("conv-{}", entropy.now_millis());
    }
    let created = coerce_timestamp(
        truthy(value.get("createdAtMs")).or_else(|| value.get("createdAt")),
        entropy.now_millis(),
    );
    let updated = coerce_timestamp(
        truthy(value.get("updatedAtMs")).or_else(|| value.get("updatedAt")),
        created,
    );
    // Slice before discarding non-object messages, as the Python reader does.
    let messages: Vec<Value> = value
        .get("messages")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .take(400)
        .filter(|item| item.is_object())
        .map(|item| {
            json!({
                "id": text(item.get("id")).chars().take(80).collect::<String>(),
                "role": text(item.get("role")).chars().take(40).collect::<String>(),
                "content": schema::normalize_content(item.get("content")),
                "reasoning": schema::normalize_content(item.get("reasoning")),
                "sourceRef": source(item.get("sourceRef")),
                "createdAt": text(item.get("createdAt")).chars().take(80).collect::<String>(),
            })
        })
        .collect();
    json!({"id": id, "conversationId": id,
        "title": schema::normalize_title(value.get("title"), "Conversation"),
        "tags": schema::normalize_tags(value.get("tags")), "sourceRef": source(value.get("sourceRef")),
        "messageCount": messages.len(), "messages": messages,
        "createdAt": iso(created), "updatedAt": iso(updated), "createdAtMs": created, "updatedAtMs": updated})
}

pub fn normalize_conversations(value: Option<&Value>, entropy: &dyn Entropy) -> Vec<Value> {
    let mut conversations: Vec<Value> = records(value)
        .map(|item| normalize_conversation(item, entropy))
        .collect();
    conversations.sort_by_key(|item| Reverse(item["updatedAtMs"].as_i64().unwrap_or(0)));
    conversations.truncate(200);
    conversations
}

pub fn list_project_conversations(
    project_id: &str,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Vec<Value>, AppError> {
    let id = schema::validate_project_id(project_id)?;
    let project = legacy::require_project(&id, root, entropy)?;
    Ok(normalize_conversations(
        project.get("conversations"),
        entropy,
    ))
}

fn saved_items_path(root: &Path, project_id: &str) -> std::path::PathBuf {
    legacy::projects_dir(root)
        .join(project_id)
        .join("saved-items.json")
}

/// `saved_items._load_items`: file order, invalid rows raise, last 1000 kept.
fn load_saved_items(project_id: &str, root: &Path) -> Result<Vec<Value>, AppError> {
    let id = schema::validate_project_id(project_id)?;
    let data = schema::read_json_file(&saved_items_path(root, &id), json!({"items": []}));
    let mut items = Vec::new();
    for raw in records(data.get("items")) {
        let saved_id = text(truthy(raw.get("savedId")).or_else(|| raw.get("id")));
        if saved_id.is_empty() {
            continue;
        }
        let created = integer(raw.get("createdAtMs"), 0).unwrap_or(0);
        let project = truthy(raw.get("projectId"))
            .map(|value| text(Some(value)))
            .unwrap_or_else(|| id.clone());
        items.push(json!({
            "savedId": saved_id, "projectId": schema::validate_project_id(&project)?,
            "type": schema::normalize_saved_type(raw.get("type"))?,
            "title": schema::normalize_title(raw.get("title"), "Saved item"),
            "content": schema::normalize_content(raw.get("content")), "sourceRef": source(raw.get("sourceRef")),
            "tags": schema::normalize_tags(raw.get("tags")), "purpose": schema::normalize_saved_purpose(raw.get("purpose")),
            "createdAt": truthy(raw.get("createdAt")).map(|value| text(Some(value))).unwrap_or_else(|| iso(created)),
            "createdAtMs": created,
        }));
    }
    if items.len() > MAX_SAVED_ITEMS {
        items.drain(..items.len() - MAX_SAVED_ITEMS);
    }
    Ok(items)
}

fn write_saved_items(root: &Path, project_id: &str, items: &[Value]) -> Result<(), AppError> {
    let kept = if items.len() > MAX_SAVED_ITEMS {
        &items[items.len() - MAX_SAVED_ITEMS..]
    } else {
        items
    };
    schema::write_json_atomic(
        root,
        &saved_items_path(root, project_id),
        &json!({"items": kept}),
    )
}

fn touch_project(root: &Path, project_id: &str, entropy: &dyn Entropy) -> Result<(), AppError> {
    let mut project = legacy::require_project(project_id, root, entropy)?;
    if let Some(fields) = project.as_object_mut() {
        fields.insert("updatedAt".to_string(), Value::from(entropy.now_millis()));
    }
    legacy::write_project(root, &project, entropy)
}

pub struct SavedItemInput<'a> {
    pub item_type: &'a str,
    pub title: &'a str,
    pub content: &'a str,
    pub source_ref: &'a Value,
    pub tags: &'a Value,
    pub purpose: &'a str,
}

pub fn create_saved_item(
    project_id: &str,
    root: &Path,
    input: SavedItemInput<'_>,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let SavedItemInput {
        item_type,
        title,
        content,
        source_ref,
        tags,
        purpose,
    } = input;
    let id = schema::validate_project_id(project_id)?;
    legacy::require_project(&id, root, entropy)?;
    let mut items = load_saved_items(&id, root)?;
    if items.len() >= MAX_SAVED_ITEMS {
        return Err(AppError {
            message: "Too many saved items".into(),
            code: codes::UPLOAD_TOO_LARGE,
            status: 413,
        });
    }
    let created_at = entropy.now_millis();
    let item = json!({
        "savedId": schema::new_id("save", entropy)?,
        "projectId": id,
        "type": schema::normalize_saved_type(Some(&Value::String(item_type.to_string())))?,
        "title": schema::normalize_title(Some(&Value::String(title.to_string())), "Saved item"),
        "content": schema::normalize_content(Some(&Value::String(content.to_string()))),
        "sourceRef": schema::normalize_source_ref(source_ref),
        "tags": schema::normalize_tags(Some(tags)),
        "purpose": schema::normalize_saved_purpose(Some(&Value::String(purpose.to_string()))),
        "createdAt": schema::timestamp_ms_to_iso(Some(&Value::from(created_at))),
        "createdAtMs": created_at,
    });
    items.push(item.clone());
    write_saved_items(root, &id, &items)?;
    touch_project(root, &id, entropy)?;
    Ok(item)
}

pub fn update_saved_item(
    project_id: &str,
    saved_id: &str,
    updates: &Value,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let id = schema::validate_project_id(project_id)?;
    let saved = schema::validate_workspace_id(saved_id, "saved item id")?;
    let mut items = load_saved_items(&id, root)?;
    let Some(index) = items
        .iter()
        .position(|item| item.get("savedId").and_then(Value::as_str) == Some(saved.as_str()))
    else {
        return Err(AppError::not_found("Saved item not found"));
    };
    let mut updated = items[index].clone();
    if let (Some(fields), Some(object)) = (updated.as_object_mut(), updates.as_object()) {
        if object.contains_key("title") {
            let default = fields
                .get("title")
                .and_then(Value::as_str)
                .unwrap_or("Saved item")
                .to_string();
            fields.insert(
                "title".to_string(),
                Value::String(schema::normalize_title(object.get("title"), &default)),
            );
        }
        if object.contains_key("content") {
            fields.insert(
                "content".to_string(),
                Value::String(schema::normalize_content(object.get("content"))),
            );
        }
        if object.contains_key("tags") {
            fields.insert(
                "tags".to_string(),
                Value::Array(
                    schema::normalize_tags(object.get("tags"))
                        .into_iter()
                        .map(Value::String)
                        .collect(),
                ),
            );
        }
        if object.contains_key("purpose") {
            fields.insert(
                "purpose".to_string(),
                Value::String(schema::normalize_saved_purpose(object.get("purpose"))),
            );
        }
        if object.contains_key("sourceRef") {
            fields.insert("sourceRef".to_string(), source(object.get("sourceRef")));
        }
    }
    items[index] = updated.clone();
    write_saved_items(root, &id, &items)?;
    touch_project(root, &id, entropy)?;
    Ok(updated)
}

pub fn delete_saved_item(
    project_id: &str,
    saved_id: &str,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<i64, AppError> {
    let id = schema::validate_project_id(project_id)?;
    let saved = schema::validate_workspace_id(saved_id, "saved item id")?;
    let items = load_saved_items(&id, root)?;
    let kept: Vec<Value> = items
        .iter()
        .filter(|item| item.get("savedId").and_then(Value::as_str) != Some(saved.as_str()))
        .cloned()
        .collect();
    if kept.len() == items.len() {
        return Ok(0);
    }
    write_saved_items(root, &id, &kept)?;
    touch_project(root, &id, entropy)?;
    Ok(1)
}

pub fn list_saved_items(
    project_id: &str,
    root: &Path,
    item_type: &str,
    tags: &[String],
) -> Result<Vec<Value>, AppError> {
    let mut items = load_saved_items(project_id, root)?;
    if !item_type.is_empty() {
        let normalized = schema::normalize_saved_type(Some(&json!(item_type)))?;
        items.retain(|item| item["type"] == normalized);
    }
    let tag_filter: Vec<String> = schema::normalize_tags(Some(&json!(tags)))
        .into_iter()
        .map(|tag| tag.to_lowercase())
        .collect();
    items.retain(|item| {
        tag_filter.iter().all(|tag| {
            item["tags"].as_array().is_some_and(|tags| {
                tags.iter()
                    .any(|candidate| candidate.as_str().unwrap_or("").to_lowercase() == *tag)
            })
        })
    });
    items.sort_by_key(|item| Reverse(item["createdAtMs"].as_i64().unwrap_or(0)));
    Ok(items)
}

fn relative_path(value: &str, root: &Path) -> Result<String, AppError> {
    schema::runtime_relative_path(
        value,
        &root.join(".generated"),
        &legacy::projects_dir(root),
        root,
    )
}

fn normalize_versions(
    value: Option<&Value>,
    path: &str,
    version: i64,
    created: i64,
    root: &Path,
) -> Result<Vec<Value>, AppError> {
    if value
        .and_then(Value::as_array)
        .is_none_or(|items| items.is_empty())
    {
        return Ok(vec![
            json!({"version": version, "path": path, "createdAt": iso(created), "createdAtMs": created}),
        ]);
    }
    records(value).map(|raw| {
        let (version, stamp) = match (integer(raw.get("version"), 1), integer(raw.get("createdAtMs"), created)) {
            (Some(version), Some(stamp)) => (version, stamp),
            _ => (1, created),
        };
        let raw_path = truthy(raw.get("path")).map(|value| text(Some(value))).unwrap_or_else(|| path.to_string());
        Ok(json!({"version": version, "path": relative_path(&raw_path, root)?,
            "createdAt": truthy(raw.get("createdAt")).map(|value| text(Some(value))).unwrap_or_else(|| iso(stamp)), "createdAtMs": stamp}))
    }).collect()
}

fn artifacts_path(root: &Path, project_id: &str) -> std::path::PathBuf {
    legacy::projects_dir(root)
        .join(project_id)
        .join("artifacts.json")
}

fn artifact_download_url(artifact_id: &str, project_id: &str) -> String {
    format!("/api/workspace/artifacts/{artifact_id}/download?projectId={project_id}")
}

/// `artifacts._load_artifacts`: file order, invalid rows raise, last 500 kept.
fn load_artifacts(project_id: &str, root: &Path) -> Result<Vec<Value>, AppError> {
    let id = schema::validate_project_id(project_id)?;
    let data = schema::read_json_file(&artifacts_path(root, &id), json!({"artifacts": []}));
    let mut items = Vec::new();
    for raw in records(data.get("artifacts")) {
        let artifact_id = text(truthy(raw.get("artifactId")).or_else(|| raw.get("id")));
        if artifact_id.is_empty() {
            continue;
        }
        let numbers = integer(raw.get("createdAtMs"), 0).and_then(|created| {
            Some((
                created,
                integer(raw.get("updatedAtMs"), created)?,
                integer(raw.get("version"), 1)?,
            ))
        });
        let (created, updated, version) = numbers.unwrap_or((0, 0, 1));
        let path = relative_path(&text(raw.get("path")), root)?;
        let project = truthy(raw.get("projectId"))
            .map(|value| text(Some(value)))
            .unwrap_or_else(|| id.clone());
        let project = schema::validate_project_id(&project)?;
        items.push(json!({
            "artifactId": artifact_id, "projectId": project,
            "type": schema::normalize_artifact_type(raw.get("type"), &path)?,
            "title": schema::normalize_title(raw.get("title"), "Artifact"), "path": path,
            "source": source(raw.get("source")), "version": version,
            "versions": normalize_versions(raw.get("versions"), &path, version, created, root)?,
            "createdAt": truthy(raw.get("createdAt")).map(|value| text(Some(value))).unwrap_or_else(|| iso(created)),
            "updatedAt": truthy(raw.get("updatedAt")).map(|value| text(Some(value))).unwrap_or_else(|| iso(updated)),
            "createdAtMs": created, "updatedAtMs": updated,
            "downloadUrl": artifact_download_url(&artifact_id, &project),
        }));
    }
    if items.len() > MAX_ARTIFACTS {
        items.drain(..items.len() - MAX_ARTIFACTS);
    }
    Ok(items)
}

fn write_artifacts(root: &Path, project_id: &str, items: &[Value]) -> Result<(), AppError> {
    let kept = if items.len() > MAX_ARTIFACTS {
        &items[items.len() - MAX_ARTIFACTS..]
    } else {
        items
    };
    schema::write_json_atomic(
        root,
        &artifacts_path(root, project_id),
        &json!({"artifacts": kept}),
    )
}

pub fn register_artifact(
    project_id: &str,
    root: &Path,
    artifact_type: &str,
    title: &str,
    path: &str,
    source: &Value,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let id = schema::validate_project_id(project_id)?;
    legacy::require_project(&id, root, entropy)?;
    let mut artifacts = load_artifacts(&id, root)?;
    if artifacts.len() >= MAX_ARTIFACTS {
        return Err(AppError {
            message: "Too many artifacts".into(),
            code: codes::UPLOAD_TOO_LARGE,
            status: 413,
        });
    }
    let rel_path = relative_path(path, root)?;
    let created_at = entropy.now_millis();
    let artifact_id = schema::new_id("art", entropy)?;
    let artifact = json!({
        "artifactId": artifact_id,
        "projectId": id,
        "type": schema::normalize_artifact_type(Some(&Value::String(artifact_type.to_string())), &rel_path)?,
        "title": schema::normalize_title(Some(&Value::String(title.to_string())), "Artifact"),
        "path": rel_path.clone(),
        "source": schema::normalize_source_ref(source),
        "version": 1,
        "versions": [{
            "version": 1,
            "path": rel_path,
            "createdAt": schema::timestamp_ms_to_iso(Some(&Value::from(created_at))),
            "createdAtMs": created_at,
        }],
        "createdAt": schema::timestamp_ms_to_iso(Some(&Value::from(created_at))),
        "updatedAt": schema::timestamp_ms_to_iso(Some(&Value::from(created_at))),
        "createdAtMs": created_at,
        "updatedAtMs": created_at,
        "downloadUrl": artifact_download_url(&artifact_id, &id),
    });
    artifacts.push(artifact.clone());
    write_artifacts(root, &id, &artifacts)?;
    touch_project(root, &id, entropy)?;
    Ok(artifact)
}

pub fn update_artifact(
    project_id: &str,
    artifact_id: &str,
    updates: &Value,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let id = schema::validate_project_id(project_id)?;
    let artifact_id = schema::validate_workspace_id(artifact_id, "artifact id")?;
    let mut artifacts = load_artifacts(&id, root)?;
    let Some(index) = artifacts.iter().position(|item| {
        item.get("artifactId").and_then(Value::as_str) == Some(artifact_id.as_str())
    }) else {
        return Err(AppError::not_found("Artifact not found"));
    };
    let mut updated = artifacts[index].clone();
    if let (Some(fields), Some(object)) = (updated.as_object_mut(), updates.as_object()) {
        if object.contains_key("title") {
            let default = fields
                .get("title")
                .and_then(Value::as_str)
                .unwrap_or("Artifact")
                .to_string();
            fields.insert(
                "title".to_string(),
                Value::String(schema::normalize_title(object.get("title"), &default)),
            );
        }
        if object.contains_key("source") {
            fields.insert(
                "source".to_string(),
                schema::normalize_source_ref(object.get("source").unwrap_or(&Value::Null)),
            );
        }
    }
    let now = entropy.now_millis();
    if let Some(fields) = updated.as_object_mut() {
        fields.insert(
            "updatedAt".to_string(),
            Value::String(schema::timestamp_ms_to_iso(Some(&Value::from(now)))),
        );
        fields.insert("updatedAtMs".to_string(), Value::from(now));
        fields.insert(
            "downloadUrl".to_string(),
            Value::String(artifact_download_url(&artifact_id, &id)),
        );
    }
    artifacts[index] = updated.clone();
    write_artifacts(root, &id, &artifacts)?;
    touch_project(root, &id, entropy)?;
    Ok(updated)
}

pub fn add_artifact_version(
    project_id: &str,
    artifact_id: &str,
    path: &str,
    source: Option<&Value>,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let id = schema::validate_project_id(project_id)?;
    let artifact_id = schema::validate_workspace_id(artifact_id, "artifact id")?;
    let mut artifacts = load_artifacts(&id, root)?;
    let Some(index) = artifacts.iter().position(|item| {
        item.get("artifactId").and_then(Value::as_str) == Some(artifact_id.as_str())
    }) else {
        return Err(AppError::not_found("Artifact not found"));
    };
    let rel_path = relative_path(path, root)?;
    let now = entropy.now_millis();
    let version = artifacts[index]
        .get("version")
        .and_then(Value::as_i64)
        .unwrap_or(0)
        + 1;
    let chosen = match source {
        Some(value) if python_truthy(value) => value,
        _ => artifacts[index].get("source").unwrap_or(&Value::Null),
    };
    let normalized_source = schema::normalize_source_ref(chosen);
    let mut versions = artifacts[index]
        .get("versions")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    versions.push(json!({
        "version": version,
        "path": rel_path.clone(),
        "createdAt": schema::timestamp_ms_to_iso(Some(&Value::from(now))),
        "createdAtMs": now,
    }));
    let mut updated = artifacts[index].clone();
    if let Some(fields) = updated.as_object_mut() {
        fields.insert("path".to_string(), Value::String(rel_path));
        fields.insert("source".to_string(), normalized_source);
        fields.insert("version".to_string(), Value::from(version));
        fields.insert("versions".to_string(), Value::Array(versions));
        fields.insert(
            "updatedAt".to_string(),
            Value::String(schema::timestamp_ms_to_iso(Some(&Value::from(now)))),
        );
        fields.insert("updatedAtMs".to_string(), Value::from(now));
        fields.insert(
            "downloadUrl".to_string(),
            Value::String(artifact_download_url(&artifact_id, &id)),
        );
    }
    artifacts[index] = updated.clone();
    write_artifacts(root, &id, &artifacts)?;
    touch_project(root, &id, entropy)?;
    Ok(updated)
}

pub fn delete_artifact(
    project_id: &str,
    artifact_id: &str,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<i64, AppError> {
    let id = schema::validate_project_id(project_id)?;
    let artifact_id = schema::validate_workspace_id(artifact_id, "artifact id")?;
    let artifacts = load_artifacts(&id, root)?;
    let kept: Vec<Value> = artifacts
        .iter()
        .filter(|item| item.get("artifactId").and_then(Value::as_str) != Some(artifact_id.as_str()))
        .cloned()
        .collect();
    if kept.len() == artifacts.len() {
        return Ok(0);
    }
    write_artifacts(root, &id, &kept)?;
    touch_project(root, &id, entropy)?;
    Ok(1)
}

pub fn list_artifacts(project_id: &str, root: &Path) -> Result<Vec<Value>, AppError> {
    let mut items = load_artifacts(project_id, root)?;
    items.sort_by_key(|item| {
        Reverse(
            integer(
                truthy(item.get("updatedAtMs")).or_else(|| item.get("createdAtMs")),
                0,
            )
            .unwrap_or(0),
        )
    });
    Ok(items)
}

pub fn public_project(
    project: &Value,
    include_children: bool,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let id = text(project.get("id"));
    let documents = legacy::normalize_documents(project.get("documents"));
    let conversations = normalize_conversations(project.get("conversations"), entropy);
    // Only the project aggregate swallows child projection errors. Direct child
    // endpoints retain their AppError; do not turn an invalid type into a 200.
    let saved = list_saved_items(&id, root, "", &[]).unwrap_or_default();
    let artifacts = list_artifacts(&id, root).unwrap_or_default();
    let scope = format!("project:{id}");
    let memories: Vec<Value> = memory::load_memories(root)
        .into_iter()
        .filter(|item| text(item.get("scope")) == scope)
        .collect();
    let created = integer(project.get("createdAt"), 0)
        .ok_or_else(|| AppError::invalid_payload("Invalid project createdAt"))?;
    let updated = integer(project.get("updatedAt"), 0)
        .ok_or_else(|| AppError::invalid_payload("Invalid project updatedAt"))?;
    let mut result = json!({"id": id, "projectId": id, "name": legacy::normalize_project_name(project.get("name")),
        "description": schema::normalize_description(project.get("description")),
        "createdAt": iso(created), "updatedAt": iso(updated), "createdAtMs": created, "updatedAtMs": updated,
        "stats": {"files": documents.len(), "savedItems": saved.len(), "artifacts": artifacts.len(),
            "conversations": conversations.len(), "memories": memories.len()}});
    if include_children {
        result.as_object_mut().expect("project object").extend([
            ("files".into(), json!(documents)),
            ("documents".into(), json!(documents)),
            ("savedItems".into(), json!(saved)),
            ("artifacts".into(), json!(artifacts)),
            ("conversations".into(), json!(conversations)),
            ("memories".into(), json!(memories)),
        ]);
    }
    Ok(result)
}

pub fn get_project(
    project_id: &str,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let id = schema::validate_project_id(project_id)?;
    public_project(
        &legacy::require_project(&id, root, entropy)?,
        true,
        root,
        entropy,
    )
}

pub fn list_projects(root: &Path, entropy: &dyn Entropy) -> Result<Vec<Value>, AppError> {
    let mut result = Vec::new();
    for item in legacy::list_projects(root, entropy)? {
        if let Ok(project) = legacy::require_project(&text(item.get("id")), root, entropy) {
            result.push(public_project(&project, false, root, entropy)?);
        }
    }
    Ok(result)
}

const MAX_PROJECT_CONVERSATIONS: usize = 200;

/// Mirrors `workspace.projects.create_project`: the legacy record, then a
/// description written only when it normalises to something non-empty.
pub fn create_project(
    name: &str,
    description: &str,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let created = legacy::create_project(name, root, entropy)?;
    let id = text(created.get("id"));
    let normalized = schema::normalize_description(Some(&json!(description)));
    if !normalized.is_empty() {
        let mut stored = legacy::require_project(&id, root, entropy)?;
        {
            let object = stored
                .as_object_mut()
                .ok_or_else(|| AppError::invalid_payload("Project record is not an object"))?;
            object.insert("description".to_string(), json!(normalized));
            object.insert("updatedAt".to_string(), json!(entropy.now_millis()));
        }
        legacy::write_project(root, &stored, entropy)?;
    }
    get_project(&id, root, entropy)
}

/// Mirrors `workspace.projects.rename_project`. A blank name leaves the stored
/// name alone. `description == None` leaves the stored description alone.
pub fn rename_project(
    project_id: &str,
    name: &str,
    description: Option<&str>,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let id = schema::validate_project_id(project_id)?;
    let mut project = legacy::require_project(&id, root, entropy)?;
    {
        let object = project
            .as_object_mut()
            .ok_or_else(|| AppError::invalid_payload("Project record is not an object"))?;
        if !name.trim().is_empty() {
            object.insert(
                "name".to_string(),
                json!(legacy::normalize_project_name(Some(&json!(name)))),
            );
        }
        if let Some(description) = description {
            object.insert(
                "description".to_string(),
                json!(schema::normalize_description(Some(&json!(description)))),
            );
        }
        object.insert("updatedAt".to_string(), json!(entropy.now_millis()));
    }
    legacy::write_project(root, &project, entropy)?;
    public_project(&project, true, root, entropy)
}

/// Mirrors `workspace.projects.delete_project`.
pub fn delete_project(project_id: &str, root: &Path) -> Result<i64, AppError> {
    let id = schema::validate_project_id(project_id)?;
    legacy::delete_project(&id, root)
}

/// Mirrors `workspace.projects.upsert_project_conversation`.
pub fn upsert_project_conversation(
    project_id: &str,
    conversation: &Value,
    root: &Path,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let id = schema::validate_project_id(project_id)?;
    let mut project = legacy::require_project(&id, root, entropy)?;
    let normalized = normalize_conversation(conversation, entropy);
    let conversation_id = text(normalized.get("conversationId"));
    let mut conversations = normalize_conversations(project.get("conversations"), entropy);
    conversations.retain(|item| text(item.get("conversationId")) != conversation_id);
    conversations.push(normalized.clone());
    if conversations.len() > MAX_PROJECT_CONVERSATIONS {
        let drop_count = conversations.len() - MAX_PROJECT_CONVERSATIONS;
        conversations.drain(0..drop_count);
    }
    {
        let object = project
            .as_object_mut()
            .ok_or_else(|| AppError::invalid_payload("Project record is not an object"))?;
        object.insert("conversations".to_string(), json!(conversations));
        object.insert("updatedAt".to_string(), json!(entropy.now_millis()));
    }
    legacy::write_project(root, &project, entropy)?;
    Ok(normalized)
}
