//! Projects-store parity probe, Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/projects_parity_probe.py`
//! through `deepseek_policy::projects` and prints canonical JSON.
//!
//! `FixedEntropy` mirrors the Python probe's patched `secrets`: a counter, consumed
//! in the same order. That matters here more than anywhere else in this migration,
//! because `read_project` **mints** ids for skill runs and saved items that have
//! none — so both sides must be pinned for a diff to mean anything.
//!
//! Usage::
//!
//!     python tasks/native-runtime/projects_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example projects_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use deepseek_policy::app_error::AppError;
use deepseek_policy::entropy::Entropy;
use deepseek_policy::projects::{
    list_projects, normalize_documents, normalize_project_artifacts, normalize_project_name,
    normalize_project_pack_versions, normalize_project_skills, normalize_saved_items,
    normalize_skill_run, projects_dir, read_project, require_project, safe_int_probe,
    unique_strings_probe, validate_project_id,
};
use deepseek_policy::python_json::OrderedJson;
use serde_json::{Map, Value, json};

struct Scratch {
    root: PathBuf,
}

impl Scratch {
    fn new(label: &str) -> Self {
        let root =
            std::env::temp_dir().join(format!("projects-parity-{label}-{}", std::process::id()));
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

/// Mirrors the probe's `_FixedSecrets`: a shared counter, consumed in order.
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

fn id_cases() -> Vec<String> {
    vec![
        "abcd".to_string(),
        "abc".to_string(),
        "a-b_c9".to_string(),
        "ABC_1234".to_string(),
        "ab cd".to_string(),
        "abcd!".to_string(),
        String::new(),
        "x".repeat(65),
        "  abcd  ".to_string(),
        "项目名称".to_string(),
        "a".repeat(64),
    ]
}

fn name_cases() -> Vec<Value> {
    vec![
        json!("My Project"),
        json!("  padded  "),
        json!("line\nbreak"),
        json!(""),
        Value::Null,
        json!(42),
        json!("x".repeat(100)),
    ]
}

fn doc_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("no-dict", json!(["x"])),
        ("missing-file-id", json!([{"projectId": "abcd"}])),
        (
            "bad-file-id",
            json!([{"fileId": "ABC", "projectId": "abcd"}]),
        ),
        (
            "uppercase-file-id",
            json!([{"fileId": "A".repeat(32), "projectId": "abcd"}]),
        ),
        (
            "empty-project-id",
            json!([{"fileId": "a".repeat(32), "projectId": ""}]),
        ),
        (
            "minimal",
            json!([{"fileId": "a".repeat(32), "projectId": "abcd"}]),
        ),
        (
            "full",
            json!([{"id": "d1", "fileId": "b".repeat(32), "projectId": "abcd", "name": "Doc",
                    "type": "text/plain", "size": 12, "kind": "pdf", "sourceAvailable": true,
                    "preview": "p".repeat(2000), "pageCount": 3, "charCount": 40, "chunkCount": 2,
                    "chunked": true, "createdAt": 99}]),
        ),
        ("not-a-list", json!({"a": 1})),
    ]
}

fn safe_int_cases() -> Vec<(&'static str, Value, i64)> {
    vec![
        ("none", Value::Null, 7),
        ("int", json!(5), 0),
        ("negative", json!(-3), 0),
        ("numeric-string", json!("12"), 0),
        ("float-string", json!("12.7"), 0),
        ("spaces", json!("  8  "), 0),
        ("garbage", json!("abc"), 4),
        ("bool-true", json!(true), 0),
        ("bool-false", json!(false), 0),
        ("underscore", json!("1_0"), 0),
        ("plus", json!("+9"), 0),
        ("empty-string", json!(""), 5),
        ("float", json!(5.5), 3),
    ]
}

fn skill_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("empty", Value::Null),
        ("empty-dict", json!({})),
        ("packs-only", json!({"enabledPacks": ["aaa", "bbb"]})),
        ("bad-pack", json!({"enabledPacks": ["ab", "aaa"]})),
        (
            "versions",
            json!({"enabledPackVersions": [{"packId": "aaa", "version": "1.0"}],
                   "enabledPacks": ["bbb"]}),
        ),
        (
            "pack-strings",
            json!({"enabledPackVersions": ["ccc", {"packId": "ddd"}]}),
        ),
        (
            "skills",
            json!({"enabledSkills": ["skill:a", "skill:a", "x"], "recentSkills": ["skill:b"]}),
        ),
        (
            "default-not-enabled",
            json!({"enabledSkills": ["skill:a"], "defaultSkill": "skill:z"}),
        ),
        (
            "default-enabled",
            json!({"enabledSkills": ["skill:a"], "defaultSkill": "skill:a"}),
        ),
    ]
}

fn run_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("minimal", json!({})),
        ("with-id", json!({"skillRunId": "r1"})),
        ("with-run-id", json!({"runId": "r2"})),
        (
            "full",
            json!({"skillRunId": "r3", "skillId": "skill:a", "skillVersion": "1.0",
                   "packId": {"packId": "pack:a"}, "status": "failed", "projectId": "abcd",
                   "input": {"k": "v"}, "inputSummary": "in", "outputSummary": "out",
                   "artifactIds": ["a1", "a1", "a2"], "savedItemIds": ["s1"],
                   "traceId": "t1", "startedAt": "2026-01-01", "completedAt": "2026-01-02",
                   "latencyMs": 100, "offline": true, "model": "m", "errorReason": "e",
                   "failureCategory": "f", "diagnosticSuggestion": "d",
                   "runSecurityLevel": "l", "securityReviewId": "s", "trustedAtRun": true,
                   "toolGrantHashAtRun": "h", "blockedReason": "b", "approvalRequired": true}),
        ),
        ("non-dict-input", json!({"input": ["x"]})),
        ("bad-skill-id", json!({"skillId": "ab"})),
    ]
}

fn unique_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("list", json!(["a", "a", "b", "  c  ", "", "a"])),
        ("string", json!("abc")),
        ("dict", json!({"k1": 1, "k2": 2})),
        ("scalar", json!(5)),
        ("none", Value::Null),
    ]
}

fn project_fixture() -> Value {
    json!({
        "name": "Fixture\nProject",
        "documents": [
            {"fileId": "a".repeat(32), "projectId": "abcd", "name": "Doc A", "chunkCount": 3},
            {"fileId": "BAD", "projectId": "abcd"}
        ],
        "skills": {"enabledPacks": ["pack:a"], "enabledSkills": ["skill:a"]},
        "skillRuns": [{"runId": "r1", "status": "completed"}],
        "savedItems": [{"title": "Note"}],
        "artifacts": [{"artifactId": "art1", "filename": "f.txt"}],
        "createdAt": 1,
        "updatedAt": 2
    })
}

/// The oracle raises a bare `TypeError` for a non-iterable, so there is no
/// `AppError` code to compare — only the message. This port's error type is
/// `AppError`, so comparing codes here would demand a value the oracle cannot
/// produce. See `docs/PROJECTS_STORE.md`.
fn outcome_no_code<T: serde::Serialize>(result: Result<T, AppError>) -> Value {
    match result {
        Ok(value) => json!({"ok": true, "result": value}),
        Err(error) => json!({"ok": false, "error": error.message}),
    }
}

fn outcome<T: serde::Serialize>(result: Result<T, AppError>) -> Value {
    match result {
        Ok(value) => json!({"ok": true, "result": value}),
        Err(error) => json!({"ok": false, "error": error.message, "code": error.code}),
    }
}

fn write_project(root: &Path, project_id: &str, raw: &str) {
    let directory = projects_dir(root).join(project_id);
    fs::create_dir_all(&directory).expect("create project dir");
    fs::write(directory.join("project.json"), raw).expect("write project.json");
}

fn main() {
    let scratch = Scratch::new("rust");
    let root = scratch.path();
    let entropy = FixedEntropy {
        ids: AtomicU64::new(0),
    };
    let mut out = Map::new();

    // --- ids ------------------------------------------------------------------
    for (index, value) in id_cases().iter().enumerate() {
        out.insert(format!("id::{index}"), outcome(validate_project_id(value)));
    }

    // --- names ----------------------------------------------------------------
    for (index, value) in name_cases().iter().enumerate() {
        out.insert(
            format!("name::{index}"),
            json!(normalize_project_name(Some(value))),
        );
    }

    // --- documents ------------------------------------------------------------
    for (label, value) in doc_cases() {
        out.insert(
            format!("documents::{label}"),
            json!(normalize_documents(Some(&value))),
        );
    }

    // --- safe int -------------------------------------------------------------
    for (label, value, default) in safe_int_cases() {
        out.insert(
            format!("safe-int::{label}"),
            json!(safe_int_probe(Some(&value), default)),
        );
    }

    // --- unique strings -------------------------------------------------------
    for (label, value) in unique_cases() {
        out.insert(
            format!("unique::{label}"),
            outcome_no_code(unique_strings_probe(Some(&value))),
        );
    }

    // --- skills ---------------------------------------------------------------
    for (label, value) in skill_cases() {
        out.insert(
            format!("skills::{label}"),
            outcome(normalize_project_skills(Some(&value))),
        );
    }

    // --- skill runs -----------------------------------------------------------
    for (label, value) in run_cases() {
        let object = value.as_object().cloned().unwrap_or_default();
        out.insert(
            format!("run::{label}"),
            outcome(normalize_skill_run(&object, &entropy)),
        );
    }

    // --- saved items and artifacts --------------------------------------------
    // Emitted raw, matching the probe.
    out.insert(
        "saved::minimal".to_string(),
        json!(normalize_saved_items(Some(&json!([{"title": "N"}])), &entropy).unwrap()),
    );
    out.insert(
        "saved::with-id".to_string(),
        json!(normalize_saved_items(Some(&json!([{"id": "s1", "title": "N"}])), &entropy).unwrap()),
    );
    out.insert(
        "saved::not-a-list".to_string(),
        json!(normalize_saved_items(Some(&json!("x")), &entropy).unwrap()),
    );
    out.insert(
        "artifacts::minimal".to_string(),
        json!(normalize_project_artifacts(Some(
            &json!([{"artifactId": "a"}])
        ))),
    );
    out.insert(
        "artifacts::not-a-list".to_string(),
        json!(normalize_project_artifacts(Some(&json!({})))),
    );

    // --- read_project ---------------------------------------------------------
    out.insert(
        "read::missing".to_string(),
        read_project("abcd", root, &entropy)
            .unwrap()
            .unwrap_or(Value::Null),
    );
    for (label, raw) in [
        ("not-json", "{not json"),
        ("scalar", "42"),
        ("list", "[]"),
        ("empty-dict", "{}"),
    ] {
        write_project(root, "abcd", raw);
        out.insert(
            format!("read::{label}"),
            outcome(read_project("abcd", root, &entropy)),
        );
    }

    // The fixture's bytes are compared, so they must match Python's
    // `json.dumps(..., indent=2)` including the fixture's own key order.
    const FIXTURE_KEYS: [&str; 8] = [
        "name",
        "documents",
        "skills",
        "skillRuns",
        "savedItems",
        "artifacts",
        "createdAt",
        "updatedAt",
    ];
    let fixture =
        OrderedJson::from_value_with_order(&project_fixture(), &FIXTURE_KEYS).render_indent_2();
    write_project(root, "abcd", &fixture);
    // Emitted raw, matching the probe.
    out.insert(
        "read::fixture".to_string(),
        read_project("abcd", root, &entropy)
            .unwrap()
            .unwrap_or(Value::Null),
    );
    out.insert(
        "require::hit".to_string(),
        require_project("abcd", root, &entropy).unwrap(),
    );
    out.insert(
        "require::miss".to_string(),
        outcome(require_project("zzzz", root, &entropy)),
    );

    // --- list_projects --------------------------------------------------------
    write_project(
        root,
        "efgh",
        &serde_json::to_string_pretty(&json!({"name": "Second", "updatedAt": 9})).unwrap(),
    );
    write_project(
        root,
        "ijkl",
        &serde_json::to_string_pretty(&json!({"name": "Third", "updatedAt": 5})).unwrap(),
    );
    fs::create_dir_all(projects_dir(root).join("not-a-project")).expect("dir");
    fs::write(projects_dir(root).join("a-file.txt"), "x").expect("file");

    let listed = list_projects(root, &entropy).expect("list_projects");
    let ids: Vec<Value> = listed
        .iter()
        .map(|item| item.get("id").cloned().unwrap_or(Value::Null))
        .collect();
    out.insert("list::ids".to_string(), json!(ids));
    out.insert("list::count".to_string(), json!(listed.len()));
    out.insert(
        "list::first".to_string(),
        listed.first().cloned().unwrap_or(Value::Null),
    );

    // `normalize_project_pack_versions` is exercised through the skills cases, but
    // one direct case pins its de-duplication.
    out.insert(
        "pack-versions::direct".to_string(),
        json!(normalize_project_pack_versions(Some(&json!([
            {"packId": "aaa", "version": "1"},
            {"packId": "aaa", "version": "2"},
            {"packId": "bbb"}
        ])))),
    );

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
