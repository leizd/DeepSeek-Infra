//! The budget ledger's database — the store behind [`crate::budget_ledger::LedgerDeps`].
//!
//! Mirrors `connect_db`, `initialize_schema` and the two statements `record_spend` and
//! `daily_spend` hand to SQLite. Three properties are contract, not incidental:
//!
//! - **the table and its columns**, because the database file outlives the process and
//!   `budget_status` publishes its path;
//! - **the upsert accumulates** (`ON CONFLICT ... DO UPDATE SET x = x + excluded.x`), so two
//!   calls for the same `(scope, day)` add up rather than replace;
//! - **each operation opens its own connection**, as the oracle's `connect_db()` does, with
//!   the same `WAL` / `synchronous` pragmas.
//!
//! A failure is a `String` rather than an [`AppError`](crate::app_error::AppError) because
//! the caller stores it in the ledger's `lastError` — the oracle's database errors are never
//! raised, only remembered. **The text differs from CPython's**: `sqlite3.OperationalError`
//! and `rusqlite::Error` do not phrase things the same way, and only `lastError` would ever
//! render it.

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use rusqlite::types::ValueRef;
use rusqlite::{Connection, Row, params};
use serde_json::{Map, Value, json};

use crate::budget_manager::SPEND_TABLE;

/// The DDL, byte-for-byte as the oracle's f-string produces it — indentation and the
/// surrounding newlines included, because SQLite stores the statement text verbatim and
/// `sqlite_master.sql` is inspectable. Written with `concat!` and explicit `\n` rather than a
/// multi-line literal: a checkout with `autocrlf` would rewrite the line endings inside a
/// literal and silently change the schema bytes.
const SCHEMA: &str = concat!(
    "\n        CREATE TABLE IF NOT EXISTS budget_daily (\n",
    "            scope TEXT NOT NULL,\n",
    "            day TEXT NOT NULL,\n",
    "            prompt_tokens INTEGER NOT NULL DEFAULT 0,\n",
    "            completion_tokens INTEGER NOT NULL DEFAULT 0,\n",
    "            cost_usd REAL NOT NULL DEFAULT 0,\n",
    "            model_calls INTEGER NOT NULL DEFAULT 0,\n",
    "            search_calls INTEGER NOT NULL DEFAULT 0,\n",
    "            tool_calls INTEGER NOT NULL DEFAULT 0,\n",
    "            updated_at TEXT NOT NULL,\n",
    "            PRIMARY KEY (scope, day)\n",
    "        )\n",
    "        ",
);

const UPSERT: &str = "INSERT INTO budget_daily
        (scope, day, prompt_tokens, completion_tokens, cost_usd, model_calls, search_calls, tool_calls, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(scope, day) DO UPDATE SET
        prompt_tokens = prompt_tokens + excluded.prompt_tokens,
        completion_tokens = completion_tokens + excluded.completion_tokens,
        cost_usd = cost_usd + excluded.cost_usd,
        model_calls = model_calls + excluded.model_calls,
        search_calls = search_calls + excluded.search_calls,
        tool_calls = tool_calls + excluded.tool_calls,
        updated_at = excluded.updated_at";

/// `SELECT *` for one day and scope, as the oracle runs it.
const SELECT_ROW: &str = "SELECT * FROM budget_daily WHERE scope = ? AND day = ?";

/// Mirrors `connect_db`: the directory appears first, then the connection, then the two
/// pragmas.
pub fn connect_db(budget_dir: &Path, budget_db: &Path) -> Result<Connection, String> {
    std::fs::create_dir_all(budget_dir).map_err(|error| error.to_string())?;
    let connection = Connection::open(budget_db).map_err(|error| error.to_string())?;
    connection
        .execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
        .map_err(|error| error.to_string())?;
    Ok(connection)
}

/// Mirrors `initialize_schema`.
pub fn initialize_schema(connection: &Connection) -> Result<(), String> {
    connection
        .execute_batch(SCHEMA)
        .map_err(|error| error.to_string())
}

/// One row as a JSON object keyed by column name, so the pure layer reads it the way
/// `sqlite3.Row` is read.
fn row_to_value(row: &Row<'_>) -> Result<Value, String> {
    let names = row.as_ref().column_names();
    let mut object = Map::new();
    for (index, name) in names.iter().enumerate() {
        let value = row.get_ref(index).map_err(|error| error.to_string())?;
        let rendered = match value {
            ValueRef::Null => Value::Null,
            ValueRef::Integer(int) => Value::from(int),
            ValueRef::Real(float) => json!(float),
            ValueRef::Text(bytes) => Value::String(String::from_utf8_lossy(bytes).into_owned()),
            ValueRef::Blob(bytes) => {
                Value::Array(bytes.iter().map(|byte| Value::from(*byte)).collect())
            }
        };
        object.insert(name.to_string(), rendered);
    }
    Ok(Value::Object(object))
}

fn column_i64(row: &Value, name: &str) -> i64 {
    row.get(name).and_then(Value::as_i64).unwrap_or(0)
}

fn column_f64(row: &Value, name: &str) -> f64 {
    row.get(name).and_then(Value::as_f64).unwrap_or(0.0)
}

fn column_text(row: &Value, name: &str) -> String {
    row.get(name)
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_string()
}

/// A budget ledger bound to one database file.
///
/// The lock mirrors the oracle's module-level `_db_lock`: the statements are serialised
/// within the process, and each call still opens its own connection.
pub struct BudgetStore {
    budget_dir: PathBuf,
    budget_db: PathBuf,
    lock: Mutex<()>,
}

impl BudgetStore {
    pub fn new(budget_dir: impl Into<PathBuf>, budget_db: impl Into<PathBuf>) -> Self {
        Self {
            budget_dir: budget_dir.into(),
            budget_db: budget_db.into(),
            lock: Mutex::new(()),
        }
    }

    /// `str(BUDGET_DB)`.
    pub fn database_path(&self) -> String {
        self.budget_db.to_string_lossy().into_owned()
    }

    /// `BUDGET_DB.exists()`, which is what `daily_spend` checks before reading.
    pub fn database_present(&self) -> bool {
        self.budget_db.exists()
    }

    /// The statement name the ledger's DDL uses, exposed for the schema check.
    pub fn table(&self) -> &'static str {
        SPEND_TABLE
    }

    /// `SELECT sql FROM sqlite_master` for the ledger table — the schema the file carries.
    pub fn schema_sql(&self) -> Result<Option<String>, String> {
        let _guard = self
            .lock
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let connection = connect_db(&self.budget_dir, &self.budget_db)?;
        initialize_schema(&connection)?;
        let mut statement = connection
            .prepare("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?")
            .map_err(|error| error.to_string())?;
        let mut rows = statement
            .query(params![SPEND_TABLE])
            .map_err(|error| error.to_string())?;
        match rows.next().map_err(|error| error.to_string())? {
            Some(row) => Ok(Some(
                row.get::<_, String>(0).map_err(|error| error.to_string())?,
            )),
            None => Ok(None),
        }
    }

    /// Mirrors the `SELECT` in `daily_spend`.
    pub fn read_spend_row(&self, scope: &str, day: &str) -> Result<Option<Value>, String> {
        let _guard = self
            .lock
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let connection = connect_db(&self.budget_dir, &self.budget_db)?;
        initialize_schema(&connection)?;
        let mut statement = connection
            .prepare(SELECT_ROW)
            .map_err(|error| error.to_string())?;
        let mut rows = statement
            .query(params![scope, day])
            .map_err(|error| error.to_string())?;
        match rows.next().map_err(|error| error.to_string())? {
            Some(row) => Ok(Some(row_to_value(row)?)),
            None => Ok(None),
        }
    }

    /// Mirrors the upsert in `record_spend`.
    pub fn write_spend_row(&self, row: &Value) -> Result<(), String> {
        let _guard = self
            .lock
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let connection = connect_db(&self.budget_dir, &self.budget_db)?;
        initialize_schema(&connection)?;
        connection
            .execute(
                UPSERT,
                params![
                    column_text(row, "scope"),
                    column_text(row, "day"),
                    column_i64(row, "prompt_tokens"),
                    column_i64(row, "completion_tokens"),
                    column_f64(row, "cost_usd"),
                    column_i64(row, "model_calls"),
                    column_i64(row, "search_calls"),
                    column_i64(row, "tool_calls"),
                    column_text(row, "updated_at"),
                ],
            )
            .map_err(|error| error.to_string())?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::budget_ledger::{LedgerDeps, daily_spend, record_spend};
    use crate::budget_manager::BudgetSettings;

    /// The text SQLite keeps for the DDL, which is what the parity probe compares.
    const NORMALISED_SCHEMA: &str = concat!(
        "CREATE TABLE budget_daily (\n",
        "            scope TEXT NOT NULL,\n",
        "            day TEXT NOT NULL,\n",
        "            prompt_tokens INTEGER NOT NULL DEFAULT 0,\n",
        "            completion_tokens INTEGER NOT NULL DEFAULT 0,\n",
        "            cost_usd REAL NOT NULL DEFAULT 0,\n",
        "            model_calls INTEGER NOT NULL DEFAULT 0,\n",
        "            search_calls INTEGER NOT NULL DEFAULT 0,\n",
        "            tool_calls INTEGER NOT NULL DEFAULT 0,\n",
        "            updated_at TEXT NOT NULL,\n",
        "            PRIMARY KEY (scope, day)\n",
        "        )",
    );

    struct Scratch {
        root: PathBuf,
    }

    impl Scratch {
        fn new(tag: &str) -> Self {
            let root = std::env::temp_dir().join(format!(
                "budget-store-test-{tag}-{}-{:?}",
                std::process::id(),
                std::thread::current().id()
            ));
            let _ = std::fs::remove_dir_all(&root);
            std::fs::create_dir_all(&root).expect("a scratch directory");
            Self { root }
        }

        fn store(&self) -> BudgetStore {
            BudgetStore::new(self.root.join("budget"), self.root.join("budget/budget.db"))
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn the_stored_ddl_is_the_oracles_text() {
        let scratch = Scratch::new("schema");
        let store = scratch.store();
        assert_eq!(
            store.schema_sql().expect("the schema reads").as_deref(),
            Some(NORMALISED_SCHEMA)
        );
        assert_eq!(store.table(), "budget_daily");
    }

    #[test]
    fn two_writes_for_one_day_accumulate_and_other_keys_do_not() {
        let scratch = Scratch::new("accumulate");
        let store = scratch.store();
        let read = |scope: &str, day: &str| store.read_spend_row(scope, day);
        let write = |row: &Value| store.write_spend_row(row);
        let settings = BudgetSettings::default();
        // Writing does not consult `database_present` -- `record_spend` only checks the
        // tracking switch -- while the read does, and by then the file exists.
        let writing = LedgerDeps {
            database_present: false,
            database_path: store.database_path(),
            day: "2026-09-18".to_string(),
            now_iso: "2026-09-18T00:00:00Z".to_string(),
            read_spend_row: &read,
            write_spend_row: &write,
        };
        for (tokens, cost) in [(10_i64, 4.9e-05_f64), (5, 1e-06)] {
            assert!(
                record_spend(
                    &json!("global"),
                    &json!({"prompt_tokens": tokens, "cost_usd": cost, "model_calls": 1}),
                    &settings,
                    &writing,
                )
                .is_none()
            );
        }
        let reading = LedgerDeps {
            database_present: store.database_present(),
            ..writing
        };
        let (spend, error) = daily_spend("global", None, &reading);
        assert!(error.is_none());
        assert_eq!(spend["promptTokens"], 15);
        assert_eq!(spend["modelCalls"], 2);

        // A key that was never written reads as the empty view rather than an error.
        let (empty, error) = daily_spend("global", Some("2026-01-01"), &reading);
        assert!(error.is_none());
        assert_eq!(empty["totalTokens"], 0);
    }
}
