//! Read projection for `infra/workspace/projects.py` and its child stores.
//!
//! Legacy list entries and Workspace 2.0 project details are different public
//! contracts. Children live in separate stores; legacy inline arrays are not
//! substitutes. No read here opens a mutation scope or writes normalization back.

use std::cmp::Reverse;
use std::path::Path;

use serde_json::{Value, json};

use crate::app_error::AppError;
use crate::core_utils::{python_int_opt, python_truthy};
use crate::entropy::Entropy;
use crate::{memory, projects as legacy, workspace_schema as schema};

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

pub fn list_saved_items(
    project_id: &str,
    root: &Path,
    item_type: &str,
    tags: &[String],
) -> Result<Vec<Value>, AppError> {
    let id = schema::validate_project_id(project_id)?;
    let data = schema::read_json_file(
        &legacy::projects_dir(root)
            .join(&id)
            .join("saved-items.json"),
        json!({"items": []}),
    );
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
    // Retention precedes filtering and sorting, including when records are unsorted.
    if items.len() > 1000 {
        items.drain(..items.len() - 1000);
    }
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

pub fn list_artifacts(project_id: &str, root: &Path) -> Result<Vec<Value>, AppError> {
    let id = schema::validate_project_id(project_id)?;
    let data = schema::read_json_file(
        &legacy::projects_dir(root).join(&id).join("artifacts.json"),
        json!({"artifacts": []}),
    );
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
            "downloadUrl": format!("/api/workspace/artifacts/{artifact_id}/download?projectId={project}"),
        }));
    }
    if items.len() > 500 {
        items.drain(..items.len() - 500);
    }
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
