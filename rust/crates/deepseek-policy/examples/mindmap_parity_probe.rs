//! create_mindmap parity probe, Rust side.
//!
//! Usage::
//!
//!     python tasks/native-runtime/mindmap_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example mindmap_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use std::fs;
use std::path::{Path, PathBuf};

use deepseek_policy::app_error::AppError;
use deepseek_policy::entropy::Entropy;
use deepseek_policy::generated_files;
use deepseek_policy::mindmaps::create_mindmap;
use serde_json::{Map, Value, json};

struct FixedEntropy {
    id: String,
}

impl Entropy for FixedEntropy {
    fn new_id(&self) -> Result<String, AppError> {
        Ok(self.id.chars().take(16).collect())
    }
    fn new_file_id(&self) -> Result<String, AppError> {
        Ok(self.id.clone())
    }
    fn now_millis(&self) -> i64 {
        0
    }
}

fn error_view(error: &AppError) -> Value {
    json!({"error": error.message, "code": error.code, "status": error.status})
}

fn make(title: &str, nodes: &Value, subtitle: &str, root: &Path, file_id: &str) -> Value {
    let entropy = FixedEntropy {
        id: file_id.to_string(),
    };
    match create_mindmap(title, nodes, subtitle, root, &entropy, 1_700_000_000.0) {
        Ok(mut result) => {
            let id = result["fileId"].as_str().unwrap().to_string();
            let path = generated_files::resolve_generated_file(root, &id).unwrap();
            let svg = fs::read_to_string(path).unwrap();
            result
                .as_object_mut()
                .unwrap()
                .insert("svg".to_string(), json!(svg));
            result
        }
        Err(error) => error_view(&error),
    }
}

fn main() {
    let mut out = Map::new();
    let scratch: PathBuf =
        std::env::temp_dir().join(format!("mindmap-parity-{}", std::process::id()));
    let _ = fs::remove_dir_all(&scratch);
    fs::create_dir_all(&scratch).unwrap();

    let sample = json!([
        {
            "label": "Market analysis",
            "children": [
                {"label": "User profile", "children": []},
                {"label": "Competition", "children": [{"label": "Pricing", "children": []}]}
            ]
        },
        {
            "label": "Product strategy",
            "children": [
                {"label": "Core features", "children": []},
                {"label": "Launch rhythm", "children": []}
            ]
        }
    ]);

    out.insert(
        "empty-title".to_string(),
        make("", &sample, "", &scratch, &"a".repeat(32)),
    );
    out.insert(
        "empty-nodes".to_string(),
        make("Empty", &json!([]), "", &scratch, &"a".repeat(32)),
    );

    let grown = make("Growth plan", &sample, "2026", &scratch, &"b".repeat(32));
    out.insert("growth::format".to_string(), grown["format"].clone());
    out.insert("growth::nodeCount".to_string(), grown["nodeCount"].clone());
    out.insert("growth::title".to_string(), grown["title"].clone());
    out.insert("growth::outline".to_string(), grown["outline"].clone());
    out.insert("growth::filename".to_string(), grown["filename"].clone());
    out.insert(
        "growth::downloadUrl".to_string(),
        grown["downloadUrl"].clone(),
    );
    out.insert("growth::svg".to_string(), grown["svg"].clone());

    let escaped = make(
        "<Title>",
        &json!([{"label": "A & B", "children": [{"title": "Child"}]}]),
        "sub",
        &scratch,
        &"c".repeat(32),
    );
    out.insert("escape::svg".to_string(), escaped["svg"].clone());

    let aliases = make(
        "Aliases",
        &json!([null, "", "one", {"title": "two"}, {"name": "three"}]),
        "",
        &scratch,
        &"d".repeat(32),
    );
    out.insert("aliases::outline".to_string(), aliases["outline"].clone());
    out.insert(
        "aliases::nodeCount".to_string(),
        aliases["nodeCount"].clone(),
    );

    let _ = fs::remove_dir_all(&scratch);
    let mut encoded = serde_json::to_string_pretty(&Value::Object(out)).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}
