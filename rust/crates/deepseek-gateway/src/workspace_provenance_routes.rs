//! Read-only `GET /api/workspace/projects/{project_id}/provenance` (`provenance.project_provenance`).

use std::collections::{HashMap, HashSet};

use axum::Json;
use axum::Router;
use axum::extract::Path as AxumPath;
use axum::http::Method;
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::{SystemClock, python_truthy};
use deepseek_policy::entropy::SystemEntropy;
use deepseek_policy::memory_schema;
use deepseek_policy::python_json::value_str;
use deepseek_policy::workspace_projects;
use deepseek_policy::workspace_schema::validate_project_id;
use serde_json::{Map, Value, json};

use crate::artifact_preview_routes::{
    error_response, load_artifacts, method_not_allowed, server_error,
};
use crate::automation_definition_routes::load_automations;
use crate::automation_run_routes::load_runs;
use crate::media_segment_routes::project_media_items;
use crate::workspace_export_routes::load_exports;

const COMPACT_KEYS: [&str; 10] = [
    "createdAt",
    "updatedAt",
    "status",
    "kind",
    "format",
    "type",
    "source",
    "sourceRef",
    "path",
    "downloadUrl",
];

const SOURCE_FIELD_MAP: [(&str, &str); 12] = [
    ("projectId", "project"),
    ("conversationId", "conversation"),
    ("messageId", "message"),
    ("savedId", "saved_item"),
    ("savedItemId", "saved_item"),
    ("artifactId", "artifact"),
    ("mediaId", "media"),
    ("automationId", "automation"),
    ("runId", "automation_run"),
    ("automationRunId", "automation_run"),
    ("skillRunId", "skill_run"),
    ("exportId", "export"),
];

pub fn router() -> Router {
    Router::new().route(
        "/api/workspace/projects/:project_id/provenance",
        any(dispatch),
    )
}

async fn dispatch(method: Method, AxumPath(project_id): AxumPath<String>) -> Response {
    if method != Method::GET && method != Method::HEAD {
        return method_not_allowed();
    }
    let result = tokio::task::spawn_blocking(move || project_provenance(&project_id)).await;
    match result {
        Ok(Ok(_)) if method == Method::HEAD => axum::http::StatusCode::OK.into_response(),
        Ok(Ok(body)) => Json(body).into_response(),
        Ok(Err(error)) if method == Method::HEAD => axum::http::StatusCode::from_u16(error.status)
            .unwrap_or(axum::http::StatusCode::INTERNAL_SERVER_ERROR)
            .into_response(),
        Ok(Err(error)) => error_response(error),
        Err(_) if method == Method::HEAD => {
            axum::http::StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
        Err(_) => error_response(server_error()),
    }
}

struct Graph {
    nodes: Vec<Value>,
    edges: Vec<Value>,
    node_ids: HashSet<String>,
    edge_ids: HashSet<(String, String, String)>,
}

impl Graph {
    fn new() -> Self {
        Self {
            nodes: Vec::new(),
            edges: Vec::new(),
            node_ids: HashSet::new(),
            edge_ids: HashSet::new(),
        }
    }

    fn node_id(kind: &str, object_id: &str) -> String {
        format!("{kind}:{object_id}")
    }

    fn add_node(&mut self, kind: &str, object_id: &str, title: &str, data: &Value) -> String {
        let id = Self::node_id(kind, object_id);
        if self.node_ids.insert(id.clone()) {
            self.nodes.push(json!({
                "id": id,
                "type": kind,
                "objectId": object_id,
                "title": title,
                "source": compact_source(data),
            }));
        }
        id
    }

    fn add_edge(&mut self, source: &str, target: &str, relation: &str) {
        if source.is_empty() || target.is_empty() || source == target {
            return;
        }
        let key = (source.to_string(), target.to_string(), relation.to_string());
        if self.edge_ids.insert(key) {
            self.edges.push(json!({
                "source": source,
                "target": target,
                "relation": relation,
            }));
        }
    }

    fn add_source_edge(&mut self, source_ref: Option<&Value>, target: &str, relation: &str) {
        let Some(ref_val) = source_ref.filter(|val| val.is_object()) else {
            return;
        };
        if ref_val.as_object().is_some_and(Map::is_empty) {
            return;
        }
        let source_nodes = self.source_nodes(ref_val);
        for src in source_nodes {
            self.add_edge(&src, target, relation);
        }
    }

    fn source_nodes(&mut self, source_ref: &Value) -> Vec<String> {
        let mut out = Vec::new();
        for (field, kind) in SOURCE_FIELD_MAP {
            let text = truthy_str(source_ref.get(field), "");
            let trimmed = text.trim();
            if !trimmed.is_empty() {
                let label = title_case_kind(kind);
                out.push(self.add_node(kind, trimmed, &label, source_ref));
            }
        }
        let ref_id = truthy_str(source_ref.get("refId"), "").trim().to_string();
        if !ref_id.is_empty() {
            let kind = truthy_str(source_ref.get("kind"), "source")
                .trim()
                .to_ascii_lowercase();
            let normalized_kind = if kind.is_empty() {
                "source".to_string()
            } else {
                kind
            };
            let label = title_case_kind(&normalized_kind);
            out.push(self.add_node(
                &format!("source_{normalized_kind}"),
                &ref_id,
                &label,
                source_ref,
            ));
        }
        out
    }

    fn type_counts(&self) -> Value {
        let mut counts: HashMap<String, usize> = HashMap::new();
        for node in &self.nodes {
            let node_type = node
                .get("type")
                .and_then(Value::as_str)
                .filter(|value| !value.is_empty())
                .unwrap_or("unknown");
            *counts.entry(node_type.to_string()).or_insert(0) += 1;
        }
        serde_json::to_value(counts).unwrap_or_else(|_| json!({}))
    }
}

fn title_case_kind(kind: &str) -> String {
    kind.split('_')
        .map(|word| {
            let mut chars = word.chars();
            match chars.next() {
                None => String::new(),
                Some(first) => {
                    let mut out = String::new();
                    out.extend(first.to_uppercase());
                    out.push_str(&chars.as_str().to_lowercase());
                    out
                }
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

fn truthy_str(value: Option<&Value>, default: &str) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => default.to_string(),
    }
}

fn item_title(item: &Value, default: &str) -> String {
    let title = truthy_str(item.get("title"), "");
    if !title.is_empty() {
        return title;
    }
    let name = truthy_str(item.get("name"), "");
    if !name.is_empty() {
        return name;
    }
    default.to_string()
}

fn compact_source(item: &Value) -> Value {
    let mut map = Map::new();
    if let Some(obj) = item.as_object() {
        for key in COMPACT_KEYS {
            if let Some(val) = obj.get(key) {
                map.insert(key.to_string(), val.clone());
            }
        }
    }
    Value::Object(map)
}

fn project_provenance(project_id: &str) -> Result<Value, AppError> {
    let safe_project_id = validate_project_id(project_id)?;
    let root = crate::data_routes::workspace_root();
    let project = workspace_projects::get_project(&safe_project_id, &root, &SystemEntropy)?;
    let mut graph = Graph::new();
    let project_node = graph.add_node(
        "project",
        &safe_project_id,
        &item_title(&project, "Project"),
        &project,
    );

    if let Some(conversations) = project.get("conversations").and_then(Value::as_array) {
        for conversation in conversations {
            if !conversation.is_object() {
                continue;
            }
            let cid = {
                let first = truthy_str(conversation.get("conversationId"), "");
                if first.is_empty() {
                    truthy_str(conversation.get("id"), "")
                } else {
                    first
                }
            };
            if cid.is_empty() {
                continue;
            }
            let cnode = graph.add_node(
                "conversation",
                &cid,
                &item_title(conversation, "Conversation"),
                conversation,
            );
            graph.add_edge(&project_node, &cnode, "contains");
            graph.add_source_edge(conversation.get("sourceRef"), &cnode, "sourced_from");
            if let Some(messages) = conversation.get("messages").and_then(Value::as_array) {
                for message in messages {
                    if !message.is_object() {
                        continue;
                    }
                    let mid = truthy_str(message.get("id"), "");
                    if mid.is_empty() {
                        continue;
                    }
                    let role = truthy_str(message.get("role"), "message");
                    let mnode = graph.add_node("message", &mid, &role, message);
                    graph.add_edge(&cnode, &mnode, "contains");
                    graph.add_source_edge(message.get("sourceRef"), &mnode, "sourced_from");
                }
            }
        }
    }

    for memory in memory_schema::list_memories("project", &safe_project_id, &root, &SystemClock) {
        let memory_id = {
            let first = truthy_str(memory.get("memoryId"), "");
            if first.is_empty() {
                truthy_str(memory.get("id"), "")
            } else {
                first
            }
        };
        if memory_id.is_empty() {
            continue;
        }
        let kind_title = truthy_str(memory.get("type"), "memory");
        let mnode = graph.add_node("memory", &memory_id, &kind_title, &memory);
        graph.add_edge(&project_node, &mnode, "contains");
        graph.add_source_edge(memory.get("source"), &mnode, "sourced_from");
    }

    for media in project_media_items(&safe_project_id)? {
        let media_id = truthy_str(media.get("mediaId"), "");
        if media_id.is_empty() {
            continue;
        }
        let mnode = graph.add_node("media", &media_id, &item_title(&media, "Media"), &media);
        graph.add_edge(&project_node, &mnode, "contains");
        graph.add_source_edge(media.get("source"), &mnode, "sourced_from");
    }

    for saved in
        workspace_projects::list_saved_items(&safe_project_id, &root, "", &[] as &[String])?
    {
        let saved_id = truthy_str(saved.get("savedId"), "");
        if saved_id.is_empty() {
            continue;
        }
        let snode = graph.add_node(
            "saved_item",
            &saved_id,
            &item_title(&saved, "Saved item"),
            &saved,
        );
        graph.add_edge(&project_node, &snode, "contains");
        graph.add_source_edge(saved.get("sourceRef"), &snode, "sourced_from");
    }

    let mut artifacts = load_artifacts(&root, &safe_project_id)?;
    artifacts.sort_by_key(|item| {
        std::cmp::Reverse(
            item.get("updatedAtMs")
                .and_then(Value::as_i64)
                .filter(|value| *value != 0)
                .or_else(|| item.get("createdAtMs").and_then(Value::as_i64))
                .unwrap_or(0),
        )
    });
    for artifact in artifacts {
        let artifact_id = truthy_str(artifact.get("artifactId"), "");
        if artifact_id.is_empty() {
            continue;
        }
        let anode = graph.add_node(
            "artifact",
            &artifact_id,
            &item_title(&artifact, "Artifact"),
            &artifact,
        );
        graph.add_edge(&project_node, &anode, "contains");
        graph.add_source_edge(artifact.get("source"), &anode, "sourced_from");
    }

    let mut automations: Vec<Value> = load_automations()?
        .into_iter()
        .filter(|item| item.get("projectId").and_then(Value::as_str) == Some(&safe_project_id))
        .collect();
    automations.sort_by_key(|item| {
        std::cmp::Reverse(
            item.get("updatedAtMs")
                .and_then(Value::as_i64)
                .filter(|value| *value != 0)
                .or_else(|| item.get("createdAtMs").and_then(Value::as_i64))
                .unwrap_or(0),
        )
    });
    for automation in automations {
        let aid = truthy_str(automation.get("automationId"), "");
        if aid.is_empty() {
            continue;
        }
        let anode = graph.add_node(
            "automation",
            &aid,
            &item_title(&automation, "Automation"),
            &automation,
        );
        graph.add_edge(&project_node, &anode, "contains");
    }

    let mut runs: Vec<Value> = load_runs()?
        .into_iter()
        .filter(|item| item.get("projectId").and_then(Value::as_str) == Some(&safe_project_id))
        .collect();
    runs.sort_by_key(|item| {
        std::cmp::Reverse(item.get("startedAtMs").and_then(Value::as_i64).unwrap_or(0))
    });
    for run in runs {
        let run_id = truthy_str(run.get("runId"), "");
        if run_id.is_empty() {
            continue;
        }
        let status = truthy_str(run.get("status"), "run");
        let rnode = graph.add_node("automation_run", &run_id, &status, &run);
        let aid = truthy_str(run.get("automationId"), "");
        let anode = if aid.is_empty() {
            project_node.clone()
        } else {
            Graph::node_id("automation", &aid)
        };
        graph.add_edge(&anode, &rnode, "ran");
        if let Some(outputs) = run.get("outputs").and_then(Value::as_object) {
            for (field, obj_type) in [
                ("artifactIds", "artifact"),
                ("savedItemIds", "saved_item"),
                ("mediaIds", "media"),
                ("exportIds", "export"),
            ] {
                if let Some(ids) = outputs.get(field).and_then(Value::as_array) {
                    let label = title_case_kind(obj_type);
                    for oid in ids {
                        let id_text = value_str(oid);
                        let target =
                            graph.add_node(obj_type, &id_text, &label, &json!({"id": id_text}));
                        graph.add_edge(&rnode, &target, "produced");
                    }
                }
            }
        }
    }

    let mut exports = load_exports(&root, &safe_project_id)?;
    exports.sort_by(|left, right| {
        let l = truthy_str(left.get("createdAt"), "");
        let r = truthy_str(right.get("createdAt"), "");
        r.cmp(&l)
    });
    for export in exports {
        let export_id = truthy_str(export.get("exportId"), "");
        if export_id.is_empty() {
            continue;
        }
        let filename = truthy_str(export.get("filename"), "Export");
        let enode = graph.add_node("export", &export_id, &filename, &export);
        graph.add_edge(&project_node, &enode, "contains");
        if let Some(includes) = export.get("includes").and_then(Value::as_object) {
            for (field, obj_type) in [
                ("conversationIds", "conversation"),
                ("savedItemIds", "saved_item"),
                ("artifactIds", "artifact"),
                ("mediaIds", "media"),
            ] {
                if let Some(ids) = includes.get(field).and_then(Value::as_array) {
                    let label = title_case_kind(obj_type);
                    for oid in ids {
                        let id_text = value_str(oid);
                        let target =
                            graph.add_node(obj_type, &id_text, &label, &json!({"id": id_text}));
                        graph.add_edge(&enode, &target, "includes");
                    }
                }
            }
        }
    }

    let node_count = graph.nodes.len();
    let edge_count = graph.edges.len();
    let types = graph.type_counts();
    Ok(json!({
        "ok": true,
        "projectId": safe_project_id,
        "nodes": graph.nodes,
        "edges": graph.edges,
        "summary": {
            "nodes": node_count,
            "edges": edge_count,
            "types": types,
        },
    }))
}
