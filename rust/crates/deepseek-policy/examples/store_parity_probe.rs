//! Store parity probe, Rust side: a real SQLite database and a real file cache, like the
//! Python side, compared byte for byte.
//!
//! `updated_at` is left out of the raw-row view: the oracle reads the wall clock there, so its
//! value cannot match and is not contract.
//!
//! Usage::
//!
//!     python tasks/native-runtime/store_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example store_parity_probe > ../rust.json

use std::path::PathBuf;

use rusqlite::Connection;
use serde_json::{Map, Value, json};

use deepseek_policy::app_error::AppError;
use deepseek_policy::budget_ledger::{LedgerDeps, daily_spend, record_spend};
use deepseek_policy::budget_manager::BudgetSettings;
use deepseek_policy::budget_store::{BudgetStore, connect_db, initialize_schema};
use deepseek_policy::file_store::FileStore;
use deepseek_policy::python_json::OrderedJson;

const DAY: &str = "2026-09-18";
const NOW_ISO: &str = "2026-09-18T00:00:00Z";

const RAW_COLUMNS: [&str; 8] = [
    "scope",
    "day",
    "prompt_tokens",
    "completion_tokens",
    "cost_usd",
    "model_calls",
    "search_calls",
    "tool_calls",
];

fn error_view(result: Result<Value, AppError>) -> Value {
    match result {
        Ok(value) => json!({"ok": value}),
        Err(error) => json!({"error": error.message, "code": error.code, "status": error.status}),
    }
}

/// The success case renders the document itself, as the oracle returns it; only the failures
/// get the code/status envelope.
fn ok_or_error(result: Result<Value, AppError>) -> Value {
    match result {
        Ok(value) => value,
        Err(error) => json!({"error": error.message, "code": error.code, "status": error.status}),
    }
}

fn bad_file_ids() -> Vec<String> {
    vec![
        "A".repeat(32),
        "a".repeat(31),
        "a".repeat(33),
        "g".repeat(32),
        String::new(),
        format!("{}B", "a".repeat(31)),
    ]
}

fn temp_dir(tag: &str) -> PathBuf {
    let base = std::env::temp_dir().join(format!("store-probe-{tag}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&base);
    std::fs::create_dir_all(&base).expect("a temp directory");
    base
}

fn raw_row(connection: &Connection, scope: &str, day: &str) -> Value {
    let mut statement = connection
        .prepare("SELECT * FROM budget_daily WHERE scope = ? AND day = ?")
        .expect("the select prepares");
    let mut rows = statement
        .query(rusqlite::params![scope, day])
        .expect("the select runs");
    let Some(row) = rows.next().expect("a row result") else {
        return Value::Null;
    };
    let mut out = Map::new();
    for name in RAW_COLUMNS {
        let index = row
            .as_ref()
            .column_names()
            .iter()
            .position(|column| *column == name)
            .expect("the column exists");
        let value = match row.get_ref(index).expect("the column reads") {
            rusqlite::types::ValueRef::Null => Value::Null,
            rusqlite::types::ValueRef::Integer(int) => Value::from(int),
            rusqlite::types::ValueRef::Real(float) => json!(float),
            rusqlite::types::ValueRef::Text(bytes) => {
                Value::String(String::from_utf8_lossy(bytes).into_owned())
            }
            rusqlite::types::ValueRef::Blob(bytes) => {
                Value::Array(bytes.iter().map(|byte| Value::from(*byte)).collect())
            }
        };
        out.insert(name.to_string(), value);
    }
    Value::Object(out)
}

fn main() {
    let mut out: Map<String, Value> = Map::new();
    let temp = temp_dir("all");

    // --- the budget database -----------------------------------------------------------
    let budget_dir = temp.join("budget");
    let budget_db = budget_dir.join("budget.db");
    let store = BudgetStore::new(&budget_dir, &budget_db);

    out.insert(
        "budget::schema".to_string(),
        json!(
            store
                .schema_sql()
                .expect("the schema reads")
                .unwrap_or_default()
        ),
    );
    out.insert(
        "budget::dir-created".to_string(),
        json!(budget_dir.is_dir()),
    );

    let read = |scope: &str, day: &str| store.read_spend_row(scope, day);
    let write = |row: &Value| store.write_spend_row(row);
    let settings = BudgetSettings::default();
    let writing = LedgerDeps {
        database_present: false,
        database_path: store.database_path(),
        day: DAY.to_string(),
        now_iso: NOW_ISO.to_string(),
        read_spend_row: &read,
        write_spend_row: &write,
    };
    assert!(
        record_spend(
            &json!("global"),
            &json!({"prompt_tokens": 10, "completion_tokens": 20, "cost_usd": 4.9e-05, "model_calls": 1}),
            &settings,
            &writing,
        )
        .is_none()
    );
    assert!(
        record_spend(
            &json!("global"),
            &json!({"prompt_tokens": 5, "cost_usd": 1e-06, "model_calls": 1, "tool_calls": 2}),
            &settings,
            &writing,
        )
        .is_none()
    );
    let reading = LedgerDeps {
        database_present: store.database_present(),
        database_path: store.database_path(),
        day: DAY.to_string(),
        now_iso: NOW_ISO.to_string(),
        read_spend_row: &read,
        write_spend_row: &write,
    };
    let (accumulated, _) = daily_spend("global", None, &reading);
    out.insert("budget::accumulated".to_string(), accumulated);
    let (other_scope, _) = daily_spend("project-x", None, &reading);
    out.insert("budget::other-scope".to_string(), other_scope);
    let (other_day, _) = daily_spend("global", Some("2026-01-01"), &reading);
    out.insert("budget::other-day".to_string(), other_day);

    let connection = connect_db(&budget_dir, &budget_db).expect("the database opens");
    initialize_schema(&connection).expect("the schema applies");
    out.insert(
        "budget::raw-row".to_string(),
        raw_row(&connection, "global", DAY),
    );
    drop(connection);

    assert!(
        record_spend(
            &json!("project-x"),
            &json!({"prompt_tokens": 1, "model_calls": 1}),
            &settings,
            &writing,
        )
        .is_none()
    );
    let (new_scope, _) = daily_spend("project-x", None, &reading);
    out.insert("budget::new-scope".to_string(), new_scope);

    // --- the file index ----------------------------------------------------------------
    let cache_dir = temp.join("cache");
    let projects_dir = temp.join("projects");
    std::fs::create_dir_all(&cache_dir).expect("the cache directory");
    let files = FileStore::new(&cache_dir, &projects_dir);

    let file_id = "a".repeat(32);
    let document = json!({"id": file_id, "name": "报告.pdf", "chunks": [{"text": "片段"}]});
    std::fs::write(
        cache_dir.join(format!("{file_id}.json")),
        serde_json::to_string(&document).expect("the document serialises"),
    )
    .expect("the document writes");
    out.insert(
        "file::ok".to_string(),
        ok_or_error(files.load_cached_file(&file_id, None)),
    );

    for (index, bad) in bad_file_ids().iter().enumerate() {
        out.insert(
            format!("file::bad-id-{index}"),
            error_view(files.load_cached_file(bad, None)),
        );
    }
    out.insert(
        "file::missing".to_string(),
        error_view(files.load_cached_file(&"b".repeat(32), None)),
    );

    std::fs::write(
        cache_dir.join(format!("{}.json", "c".repeat(32))),
        "not json",
    )
    .expect("the malformed document writes");
    out.insert(
        "file::not-json".to_string(),
        error_view(files.load_cached_file(&"c".repeat(32), None)),
    );
    std::fs::write(cache_dir.join(format!("{}.json", "d".repeat(32))), "[1, 2]")
        .expect("the array document writes");
    out.insert(
        "file::not-object".to_string(),
        error_view(files.load_cached_file(&"d".repeat(32), None)),
    );

    let project_files = projects_dir.join("proj1").join("files");
    std::fs::create_dir_all(&project_files).expect("the project directory");
    let mut projected = document.as_object().cloned().unwrap_or_default();
    projected.insert("projectId".to_string(), json!("proj1"));
    std::fs::write(
        project_files.join(format!("{file_id}.json")),
        serde_json::to_string(&Value::Object(projected)).expect("the document serialises"),
    )
    .expect("the project document writes");
    out.insert(
        "file::project-ok".to_string(),
        ok_or_error(files.load_cached_file(&file_id, Some("proj1"))),
    );
    out.insert(
        "file::project-short".to_string(),
        error_view(files.load_cached_file(&file_id, Some("ab"))),
    );
    out.insert(
        "file::project-empty-is-global".to_string(),
        ok_or_error(files.load_cached_file(&file_id, Some(""))),
    );

    let mut rewritten = document.as_object().cloned().unwrap_or_default();
    rewritten.insert("name".to_string(), json!("报告-v2.pdf"));
    std::fs::write(
        cache_dir.join(format!("{file_id}.json")),
        serde_json::to_string(&Value::Object(rewritten)).expect("the document serialises"),
    )
    .expect("the rewrite lands");
    out.insert(
        "file::after-rewrite".to_string(),
        ok_or_error(files.load_cached_file(&file_id, None)),
    );

    let rendered = OrderedJson::from_value_with_order(&Value::Object(out), &[]).render_indent_2();
    println!("{rendered}");
}
