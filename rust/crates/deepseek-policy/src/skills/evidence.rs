use super::{
    Result,
    registry::{Registry, read_json},
    text, truth,
};
use crate::{entropy::Entropy, generated_files};
use serde_json::{Value, json};
pub fn register(r: &Registry, file: &Value, source: &Value, tool: &str) -> Result<Option<Value>> {
    let id = text(file, "fileId");
    let Some(path) = generated_files::resolve_generated_file(&r.root, &id) else {
        return Ok(None);
    };
    let artifact = json!({"artifactId":format!("art-{}",&id[..16]),"fileId":id,
        "filename":if truth(file,"filename") {text(file,"filename")} else {path.file_name().unwrap().to_string_lossy().into()},
        "downloadUrl":if truth(file,"downloadUrl") {text(file,"downloadUrl")} else {generated_files::download_url(&id)},
        "type":path.extension().unwrap_or_default().to_string_lossy().to_lowercase(),"tool":tool,"createdAt":r.now(),"source":source});
    let _guard = crate::mutation_gate::mutation_scope(None, &r.root)
        .map_err(|e| super::error(e.to_string(), 409))?;
    let index = generated_files::generated_dir(&r.root).join("artifacts.json");
    let mut artifacts: Vec<_> = read_json(&index)
        .as_array()
        .into_iter()
        .flatten()
        .filter(|v| v.is_object() && v["artifactId"] != artifact["artifactId"])
        .cloned()
        .collect();
    artifacts.push(artifact.clone());
    let start = artifacts.len().saturating_sub(1000);
    r.write_json(&index, &json!(&artifacts[start..]))?;
    Ok(Some(artifact))
}
pub fn markdown(r: &Registry, title: &str, content: &str, source: &Value) -> Result<Option<Value>> {
    if content.trim().is_empty() {
        return Ok(None);
    }
    let _guard = crate::mutation_gate::mutation_scope(None, &r.root)
        .map_err(|e| super::error(e.to_string(), 409))?;
    let file = generated_files::store_generated_file(
        if title.is_empty() {
            "skill-output"
        } else {
            title
        },
        "md",
        &r.root,
        r,
        r.now_millis() as f64 / 1000.0,
        |path| std::fs::write(path, format!("{}\n", content.trim())),
    )?;
    register(r, &file, source, "skill_markdown")
}
pub fn files(value: &Value, tool: &str, found: &mut Vec<Value>) {
    match value {
        Value::Object(fields) => {
            let next = if truth(value, "tool") {
                text(value, "tool")
            } else {
                tool.into()
            };
            let id = text(value, "fileId");
            if id.len() == 32
                && id
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                && (truth(value, "downloadUrl") || truth(value, "filename"))
            {
                let mut file = value.clone();
                if !next.is_empty() {
                    file["tool"] = next.clone().into();
                }
                found.push(file);
            }
            for v in fields.values() {
                files(v, &next, found);
            }
        }
        Value::Array(values) => {
            for v in values {
                files(v, tool, found);
            }
        }
        _ => {}
    }
}
