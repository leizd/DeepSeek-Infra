//! File-cache and projects-branch parity probe, Rust side (slice E2).
//!
//! Replays the same corpus as `tasks/native-runtime/file_cache_parity_probe.py`
//! through `deepseek_policy::file_cache` and `deepseek_policy::projects`.
//!
//! Usage::
//!
//!     python tasks/native-runtime/file_cache_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example file_cache_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use deepseek_policy::app_error::AppError;
use deepseek_policy::entropy::Entropy;
use deepseek_policy::file_cache::{
    FileCache, file_cache_dir, load_cached_file, project_file_cache_dir,
};
use deepseek_policy::projects::{list_project_files, projects_dir, read_file_chunk};
use serde_json::{Map, Value, json};

const GOOD_ID: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

struct Scratch {
    root: PathBuf,
}

impl Scratch {
    fn new(label: &str) -> Self {
        let root =
            std::env::temp_dir().join(format!("file-cache-parity-{label}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("create scratch root");
        Self { root }
    }
    fn path(&self) -> &Path {
        &self.root
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

struct FixedEntropy {
    ids: AtomicU64,
}

impl Entropy for FixedEntropy {
    fn new_id(&self) -> Result<String, AppError> {
        let value = self.ids.fetch_add(1, Ordering::Relaxed) + 1;
        Ok(format!("{value:016x}"))
    }
    fn now_millis(&self) -> i64 {
        1_760_000_000_000
    }
}

/// The index the Python probe serves. `chunks[3]` is deliberately not an object.
fn index_body() -> Value {
    json!({
        "id": "file-under-test",
        "name": "Report.pdf",
        "kind": "pdf",
        "projectId": "abcd",
        "chunks": [
            {"lineStart": 1, "lineEnd": 10, "text": "first chunk"},
            {"lineStart": 11, "lineEnd": 20, "text": "x".repeat(7000)},
            {"lineStart": 21, "lineEnd": 30, "text": 42},
            "not-a-chunk"
        ]
    })
}

fn chunk_cases() -> Vec<(&'static str, Value, &'static str)> {
    vec![
        ("first-default", json!({}), "normal"),
        ("index-one", json!({"chunkIndex": 1}), "normal"),
        ("index-two", json!({"chunkIndex": 2}), "normal"),
        ("index-last", json!({"chunkIndex": 4}), "normal"),
        ("out-of-range", json!({"chunkIndex": 5}), "normal"),
        ("large-index", json!({"chunkIndex": 99}), "normal"),
        ("zero-index", json!({"chunkIndex": 0}), "normal"),
        ("negative-index", json!({"chunkIndex": -1}), "normal"),
        ("non-dict-chunk", json!({"chunkIndex": 4}), "normal"),
        ("missing-chunks", json!({}), "no-chunks"),
        ("chunks-not-list", json!({}), "chunks-scalar"),
        (
            "project-scoped",
            json!({"chunkIndex": 1, "projectId": "abcd"}),
            "project",
        ),
        (
            "bad-project-id",
            json!({"chunkIndex": 1, "projectId": "ab"}),
            "normal",
        ),
    ]
}

fn outcome<T: serde::Serialize>(result: Result<T, AppError>) -> Value {
    match result {
        Ok(value) => json!({"ok": true, "result": value}),
        Err(error) => json!({
            "ok": false,
            "error": error.message,
            "code": error.code,
            "status": error.status,
        }),
    }
}

fn write_index(directory: &Path, body: &Value) {
    fs::create_dir_all(directory).expect("create cache dir");
    let text = match body {
        Value::String(raw) => raw.clone(),
        other => serde_json::to_string_pretty(other).expect("serialize index"),
    };
    fs::write(directory.join(format!("{GOOD_ID}.json")), text).expect("write index");
}

fn write_project(root: &Path, project_id: &str, body: &Value) {
    let target = projects_dir(root).join(project_id);
    fs::create_dir_all(&target).expect("create project dir");
    fs::write(
        target.join("project.json"),
        serde_json::to_string_pretty(body).expect("serialize project"),
    )
    .expect("write project.json");
}

fn main() {
    let scratch = Scratch::new("rust");
    let root = scratch.path();
    let entropy = FixedEntropy {
        ids: AtomicU64::new(0),
    };
    let cache = FileCache::new();
    let mut out = Map::new();

    let global_dir = file_cache_dir(root);
    let project_files = project_file_cache_dir(root, "abcd").expect("project files dir");

    // --- load_cached_file error shapes ----------------------------------------
    for (label, file_id) in [
        ("bad-short-id", "abc"),
        ("bad-uppercase-id", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        ("bad-nonhex-id", "gggggggggggggggggggggggggggggggg"),
        ("empty-id", ""),
    ] {
        out.insert(
            format!("load::{label}"),
            outcome(load_cached_file(root, file_id, None, &cache)),
        );
    }
    out.insert(
        "load::missing".to_string(),
        outcome(load_cached_file(root, GOOD_ID, None, &cache)),
    );

    write_index(&global_dir, &json!("{not json"));
    out.insert(
        "load::malformed".to_string(),
        outcome(load_cached_file(root, GOOD_ID, None, &cache)),
    );
    write_index(&global_dir, &json!("42"));
    out.insert(
        "load::scalar".to_string(),
        outcome(load_cached_file(root, GOOD_ID, None, &cache)),
    );

    // --- the project-scoped path ----------------------------------------------
    write_index(&project_files, &index_body());
    out.insert(
        "load::global-cannot-see-project".to_string(),
        outcome(load_cached_file(root, GOOD_ID, None, &cache)),
    );
    out.insert(
        "load::scoped".to_string(),
        outcome(load_cached_file(root, GOOD_ID, Some("abcd"), &cache)),
    );
    out.insert(
        "load::bad-project-id".to_string(),
        outcome(load_cached_file(root, GOOD_ID, Some("ab"), &cache)),
    );
    // A whitespace-only id is truthy, so it reaches the shape check and fails.
    out.insert(
        "load::blank-project-id-is-global".to_string(),
        outcome(load_cached_file(root, GOOD_ID, Some("   "), &cache)),
    );
    out.insert(
        "cache-dir::project".to_string(),
        outcome(project_file_cache_dir(root, "abcd").map(|path| {
            path.strip_prefix(root)
                // Python's `str(Path)` uses the native separator, so it is not
                // normalised away here.
                .map(|relative| relative.to_string_lossy().to_string())
                .unwrap_or_default()
        })),
    );

    // --- read_file_chunk_tool -------------------------------------------------
    for (label, arguments, variant) in chunk_cases() {
        let scoped = variant == "project";
        let body = match variant {
            "no-chunks" => json!({"id": "f", "name": "n", "kind": "text"}),
            "chunks-scalar" => json!({"id": "f", "name": "n", "kind": "text", "chunks": "no"}),
            _ => index_body(),
        };
        if scoped {
            write_index(&project_files, &body);
            // The global copy must not exist, so only the project path can serve.
            let _ = fs::remove_file(global_dir.join(format!("{GOOD_ID}.json")));
        } else {
            write_index(&global_dir, &body);
            let _ = fs::remove_file(project_files.join(format!("{GOOD_ID}.json")));
        }
        // The probe's dispatch falls back to GOOD_ID when `fileId` is absent.
        let mut served = arguments.as_object().cloned().unwrap_or_default();
        if !served.contains_key("fileId") {
            served.insert("fileId".to_string(), json!(GOOD_ID));
        }
        out.insert(
            format!("chunk::{label}"),
            outcome(read_file_chunk(&served, root, &cache)),
        );
    }

    // --- list_project_files_tool ----------------------------------------------
    let mut documents: Vec<Value> = vec![
        json!({"fileId": "b".repeat(32), "projectId": "abcd", "name": "A", "kind": "pdf",
               "pageCount": 2, "charCount": 30, "chunkCount": 4, "preview": "p".repeat(700)}),
        json!({"fileId": "c".repeat(32), "projectId": "abcd", "name": "B"}),
        json!({"fileId": "bad", "projectId": "abcd"}),
        json!("not-a-dict"),
    ];
    for index in 0..200 {
        documents.push(json!({
            "fileId": format!("{index:032x}"),
            "projectId": "abcd",
            "name": format!("D{index}")
        }));
    }
    write_project(
        root,
        "abcd",
        &json!({"name": "Alpha", "documents": documents, "updatedAt": 5}),
    );
    write_project(
        root,
        "efgh",
        &json!({"name": "Beta", "documents": [], "updatedAt": 9}),
    );

    let named = |project_id: &str| {
        let mut arguments = Map::new();
        arguments.insert("projectId".to_string(), json!(project_id));
        outcome(list_project_files(&arguments, root, &entropy))
    };
    out.insert("list::named".to_string(), named("abcd"));
    out.insert("list::missing".to_string(), named("zzzz"));
    out.insert("list::invalid-id".to_string(), named("ab"));

    let listed = outcome(list_project_files(&Map::new(), root, &entropy));
    out.insert(
        "list::all".to_string(),
        match &listed {
            Value::Object(fields) if fields.get("ok") == Some(&Value::Bool(true)) => {
                let payload = &fields["result"];
                let projects = payload["projects"].as_array().cloned().unwrap_or_default();
                let ids: Vec<Value> = projects
                    .iter()
                    .map(|project| project["id"].clone())
                    .collect();
                let per_project: Vec<Value> = projects
                    .iter()
                    .map(|project| json!(project["files"].as_array().map(Vec::len).unwrap_or(0)))
                    .collect();
                let first_file = projects
                    .iter()
                    .find_map(|project| {
                        project["files"]
                            .as_array()
                            .and_then(|files| files.first().cloned())
                    })
                    .unwrap_or(Value::Null);
                json!({
                    "ok": true,
                    "ids": ids,
                    "count": payload["count"],
                    "per-project": per_project,
                    "first-file": first_file,
                })
            }
            _ => listed,
        },
    );

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
