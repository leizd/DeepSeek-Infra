use deepseek_policy::{app_error::AppError, entropy::Entropy, projects, workspace_projects};
use serde_json::{Value, json};

struct FixedEntropy(i64);
impl Entropy for FixedEntropy {
    fn now_millis(&self) -> i64 {
        self.0
    }
    fn new_id(&self) -> Result<String, AppError> {
        panic!("read fixture must not need an invented legacy id")
    }
}

fn outcome<T: serde::Serialize>(value: Result<T, AppError>) -> Value {
    match value {
        Ok(value) => json!({"ok": value}),
        Err(error) => {
            json!({"error": {"code": error.code, "message": error.message, "status": error.status}})
        }
    }
}

#[test]
fn workspace_project_reads_match_python_storage_oracle() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/workspace_projects_oracle.json")).unwrap();
    for case in cases {
        let root = tempfile::tempdir().unwrap();
        let mut before = Vec::new();
        for (relative, value) in case["files"].as_object().unwrap() {
            let path = root.path().join(relative);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            let bytes = value
                .as_str()
                .map(str::to_string)
                .unwrap_or_else(|| value.to_string());
            std::fs::write(&path, &bytes).unwrap();
            before.push((path, bytes));
        }
        let entropy = FixedEntropy(case["now_ms"].as_i64().unwrap());
        for (key, value) in [
            (
                "saved",
                outcome(workspace_projects::list_saved_items(
                    "proj-read",
                    root.path(),
                    "",
                    &[],
                )),
            ),
            (
                "saved_filtered",
                outcome(workspace_projects::list_saved_items(
                    "proj-read",
                    root.path(),
                    "chat_snippet",
                    &["a".into()],
                )),
            ),
            (
                "artifacts",
                outcome(workspace_projects::list_artifacts("proj-read", root.path())),
            ),
            (
                "legacy_list",
                outcome(projects::list_projects(root.path(), &entropy)),
            ),
            (
                "list",
                outcome(workspace_projects::list_projects(root.path(), &entropy)),
            ),
            (
                "get",
                outcome(workspace_projects::get_project(
                    "proj-read",
                    root.path(),
                    &entropy,
                )),
            ),
            (
                "conversations",
                outcome(workspace_projects::list_project_conversations(
                    "proj-read",
                    root.path(),
                    &entropy,
                )),
            ),
        ] {
            assert_eq!(
                value, case["expected"][key],
                "case {}, operation {key}",
                case["name"]
            );
        }
        for (path, bytes) in before {
            assert_eq!(std::fs::read_to_string(path).unwrap(), bytes);
        }
        assert!(!root.path().join(".workspace-generation").exists());
        assert!(!root.path().join(".workspace-mutation.lock").exists());
    }
}
