//! Reminders parity probe, Rust side.
//!
//! Replays the same scripted sequence as
//! `tasks/native-runtime/reminders_parity_probe.py` through
//! `deepseek_policy::reminders` and prints canonical JSON, so the two outputs can
//! be diffed byte-for-byte.
//!
//! `FixedEntropy` mirrors the Python probe's patched `secrets`/`time`: a counter
//! for ids and a frozen clock, so `id` and `createdAt` are comparable.
//!
//! Usage::
//!
//!     python tasks/native-runtime/reminders_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example reminders_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use deepseek_policy::app_error::AppError;
use deepseek_policy::reminders::{
    Entropy, create_reminder, delete_reminder, due_reminders, list_reminders, load_reminders,
    reminders_dir, reminders_file,
};
use serde_json::{Map, Value, json};

struct Scratch {
    root: PathBuf,
}

impl Scratch {
    fn new(label: &str) -> Self {
        let root =
            std::env::temp_dir().join(format!("reminders-parity-{label}-{}", std::process::id()));
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

/// Mirrors the probe's `_FixedSecrets` / `_FixedTime`.
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

fn due_at_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("zulu", json!("2026-09-15T10:30:00Z")),
        ("offset", json!("2026-09-15T10:30:00+08:00")),
        ("naive", json!("2026-09-15T10:30:00")),
        ("no-seconds", json!("2026-09-15T10:30")),
        ("date-only", json!("2026-09-15")),
        ("microseconds", json!("2026-09-15T10:30:00.123456")),
        ("compact-offset", json!("2026-09-15T10:30:00+0000")),
        ("lowercase-z", json!("2026-09-15t10:30:00z")),
        ("already-utc", json!("2026-09-15T10:30:00+00:00")),
        ("negative-offset", json!("2026-09-15T10:30:00-05:00")),
        ("whitespace", json!("  2026-09-15T10:30:00Z  ")),
        ("empty", json!("")),
        ("none", Value::Null),
        ("garbage", json!("not a date")),
        ("compact-digits", json!("20260915")),
        ("week-format", json!("2026-W37-1")),
        ("lowercase-t", json!("2026-09-15t10:30:00")),
        ("uppercase-t-lowercase-z", json!("2026-09-15T10:30:00z")),
        ("space-separator", json!("2026-09-15 10:30:00")),
        ("hour-only", json!("2026-09-15T10")),
        ("fraction-1-digit", json!("2026-09-15T10:30:00.1")),
        ("compact-time", json!("20260915T103000")),
        ("invalid-hour", json!("2026-09-15T25:00:00")),
        ("invalid-date", json!("2026-02-30")),
        ("offset-no-colon", json!("2026-09-15T10:30:00+0800")),
        ("z-with-fraction", json!("2026-09-15T10:30:00.5Z")),
        ("offset-hours-only", json!("2026-09-15T10:30:00+08")),
        ("week-2026-w01", json!("2026-W01-1")),
        ("week-2026-w53", json!("2026-W53-1")),
        ("week-2025-w53", json!("2025-W53-1")),
        ("week-2020-w53", json!("2020-W53-1")),
        ("week-w00", json!("2026-W00-1")),
        ("week-w54", json!("2026-W54-1")),
        ("leap-day", json!("2024-02-29")),
    ]
}

fn create_cases() -> Vec<(&'static str, Value)> {
    vec![
        (
            "full",
            json!({"title": "Stand up", "content": "stretch", "dueAt": "2026-09-15T10:30:00Z"}),
        ),
        (
            "blank-title",
            json!({"title": "   ", "content": "c", "dueAt": "2026-09-15T10:30:00Z"}),
        ),
        (
            "missing-title",
            json!({"content": "c", "dueAt": "2026-09-15T10:30:00Z"}),
        ),
        (
            "snake-case-due",
            json!({"title": "t", "content": "c", "due_at": "2026-09-15T10:30:00Z"}),
        ),
        (
            "long-title",
            json!({"title": "x".repeat(200), "content": "c", "dueAt": "2026-09-15T10:30:00Z"}),
        ),
        (
            "long-content",
            json!({"title": "t", "content": "y".repeat(3000), "dueAt": "2026-09-15T10:30:00Z"}),
        ),
        ("no-due", json!({"title": "t", "content": "c"})),
        (
            "non-string",
            json!({"title": 42, "content": null, "dueAt": "2026-09-15T10:30:00Z"}),
        ),
    ]
}

fn status_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("active", json!("active")),
        ("notified", json!("notified")),
        ("all", json!("all")),
        ("bogus", json!("bogus")),
        ("mixed-case", json!("  ACTIVE  ")),
        ("empty", json!("")),
        ("none", Value::Null),
        ("non-string", json!(7)),
    ]
}

fn read_cases() -> Vec<(&'static str, &'static str)> {
    vec![
        ("not-json", "{not json"),
        ("scalar", "42"),
        ("object", "{}"),
        ("empty-list", "[]"),
        ("mixed-items", r#"[{"id": "a"}, "x", 7, null, {"id": "b"}]"#),
    ]
}

/// The probe's `outcome` shape for an `AppError`.
fn outcome<T: serde::Serialize>(result: Result<T, AppError>) -> Value {
    match result {
        Ok(value) => json!({"ok": true, "value": value}),
        Err(error) => json!({"ok": false, "error": error.message, "code": error.code}),
    }
}

/// The probe's shape for a created reminder.
fn create_outcome(result: Result<Value, AppError>) -> Value {
    match result {
        Ok(reminder) => json!({"ok": true, "reminder": reminder}),
        Err(error) => json!({"ok": false, "error": error.message, "code": error.code}),
    }
}

fn main() {
    let scratch = Scratch::new("rust");
    let root = scratch.path();
    let mut out = Map::new();
    // Interior mutability via `AtomicU64`, so no `mut` binding is needed.
    let entropy = FixedEntropy {
        ids: AtomicU64::new(0),
    };

    let store = reminders_file(root);

    // --- parse_due_at ---------------------------------------------------------
    for (label, value) in due_at_cases() {
        out.insert(
            format!("due::{label}"),
            outcome(deepseek_policy::reminders::parse_due_at(Some(&value))),
        );
    }

    // --- create ---------------------------------------------------------------
    for (label, payload) in create_cases() {
        let arguments = payload.as_object().cloned().unwrap_or_default();
        out.insert(
            format!("create::{label}"),
            create_outcome(create_reminder(&arguments, root, &entropy)),
        );
    }

    out.insert(
        "store::file".to_string(),
        json!(fs::read_to_string(&store).unwrap_or_default()),
    );
    out.insert("store::loaded".to_string(), json!(load_reminders(root)));
    out.insert(
        "store::count".to_string(),
        json!(load_reminders(root).len()),
    );
    let generation = root.join(".workspace-generation");
    out.insert(
        "store::generation".to_string(),
        match fs::read_to_string(&generation) {
            Ok(text) => json!(text),
            Err(_) => Value::Null,
        },
    );
    out.insert(
        "store::lock-exists".to_string(),
        json!(root.join(".workspace-mutation.lock").exists()),
    );
    let mut leftovers: Vec<String> = fs::read_dir(reminders_dir(root))
        .map(|entries| {
            entries
                .filter_map(Result::ok)
                .map(|entry| entry.file_name().to_string_lossy().to_string())
                .filter(|name| name.ends_with(".tmp"))
                .collect()
        })
        .unwrap_or_default();
    leftovers.sort();
    out.insert(
        "store::tmp-leftovers".to_string(),
        Value::Array(leftovers.into_iter().map(Value::String).collect()),
    );

    // --- list -----------------------------------------------------------------
    for (label, status) in status_cases() {
        let mut arguments = Map::new();
        arguments.insert("status".to_string(), status);
        let result = list_reminders(&arguments, root);
        let ids: Vec<Value> = result["reminders"]
            .as_array()
            .map(|items| items.iter().map(|item| item["id"].clone()).collect())
            .unwrap_or_default();
        out.insert(
            format!("list::{label}"),
            json!({
                "status": result["status"],
                "count": result["count"],
                "returned": result["reminders"].as_array().map(Vec::len).unwrap_or(0),
                "ids": ids,
            }),
        );
    }

    // --- filtered -------------------------------------------------------------
    let filtered_store = "[
  {
    \"id\": \"n1\",
    \"title\": \"a\",
    \"content\": \"\",
    \"dueAt\": \"2026-01-01T00:00:00+00:00\",
    \"createdAt\": 1,
    \"notified\": true,
    \"notifiedAt\": 2
  },
  {
    \"id\": \"a1\",
    \"title\": \"b\",
    \"content\": \"\",
    \"dueAt\": \"2027-01-01T00:00:00+00:00\",
    \"createdAt\": 1,
    \"notified\": false
  }
]";
    fs::create_dir_all(reminders_dir(root)).unwrap();
    fs::write(&store, filtered_store).unwrap();
    for (label, status) in status_cases() {
        let mut arguments = Map::new();
        arguments.insert("status".to_string(), status);
        let result = list_reminders(&arguments, root);
        let ids: Vec<Value> = result["reminders"]
            .as_array()
            .map(|items| items.iter().map(|item| item["id"].clone()).collect())
            .unwrap_or_default();
        out.insert(
            format!("filtered::{label}"),
            json!({
                "status": result["status"],
                "count": result["count"],
                "ids": ids,
            }),
        );
    }

    // --- tolerant reads -------------------------------------------------------
    for (label, raw) in read_cases() {
        fs::write(&store, raw).unwrap();
        out.insert(format!("read::{label}"), json!(load_reminders(root)));
    }
    let _ = fs::remove_file(&store);
    out.insert("read::missing".to_string(), json!(load_reminders(root)));

    // --- delete ---------------------------------------------------------------
    fs::create_dir_all(reminders_dir(root)).unwrap();
    fs::write(
        &store,
        "[\n  {\n    \"id\": \"keep\"\n  },\n  {\n    \"id\": \"drop\"\n  }\n]",
    )
    .unwrap();
    // The probe records these as bare integers, so no `outcome` wrapper here.
    out.insert(
        "delete::hit".to_string(),
        json!(delete_reminder("drop", root).unwrap()),
    );
    out.insert(
        "delete::miss".to_string(),
        json!(delete_reminder("nope", root).unwrap()),
    );
    out.insert(
        "delete::blank".to_string(),
        json!(delete_reminder("", root).unwrap()),
    );
    out.insert("delete::remaining".to_string(), json!(load_reminders(root)));

    // `due_reminders` is not part of the probe's compared corpus yet; calling it
    // here would mutate the store after the last observation, so it is left out.
    let _ = due_reminders;

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
