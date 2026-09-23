//! Skill run spans in the existing observability SQLite schema.
use super::{Result, error, registry::Registry, text};
use crate::entropy::Entropy;
use rusqlite::{Connection, params};
use serde_json::{Value, json};

pub struct Trace {
    pub id: String,
    span: String,
    started: i64,
    input: Value,
    skill: String,
}
fn db(r: &Registry) -> Result<Connection> {
    let dir = r.root.join(".traces");
    std::fs::create_dir_all(&dir).map_err(|e| error(e.to_string(), 500))?;
    let conn = Connection::open(dir.join("traces.sqlite3")).map_err(sql_error)?;
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(sql_error)?;
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS trace_runs (trace_id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,status TEXT NOT NULL,started_at TEXT NOT NULL,started_epoch REAL NOT NULL,completed_at TEXT NOT NULL,completed_epoch REAL,duration_ms INTEGER NOT NULL,metadata TEXT NOT NULL,error TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trace_spans (span_id TEXT PRIMARY KEY,trace_id TEXT NOT NULL,parent_span_id TEXT NOT NULL,name TEXT NOT NULL,kind TEXT NOT NULL,status TEXT NOT NULL,started_at TEXT NOT NULL,started_epoch REAL NOT NULL,completed_at TEXT NOT NULL,completed_epoch REAL NOT NULL,duration_ms INTEGER NOT NULL,input_json TEXT NOT NULL,output_json TEXT NOT NULL,usage_json TEXT NOT NULL,diagnostics_json TEXT NOT NULL,cache_hit_rate REAL NOT NULL,total_tokens INTEGER NOT NULL,error TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_trace_spans_trace ON trace_spans(trace_id,started_epoch);
CREATE INDEX IF NOT EXISTS idx_trace_runs_started ON trace_runs(started_epoch);").map_err(sql_error)?;
    Ok(conn)
}
fn sql_error(e: rusqlite::Error) -> crate::app_error::AppError {
    error(format!("Skill trace storage: {e}"), 500)
}
fn clip(value: &str, limit: usize) -> String {
    let size = value.chars().count();
    if size <= limit {
        value.into()
    } else {
        format!(
            "{}...[truncated {} chars]",
            value.chars().take(limit).collect::<String>(),
            size - limit
        )
    }
}
fn sanitize(value: &Value, limit: usize) -> Value {
    match value {
        Value::Object(map) => Value::Object(
            map.iter()
                .map(|(k, v)| {
                    (
                        k.clone(),
                        if [
                            "apikey",
                            "api_key",
                            "authorization",
                            "tavilyapikey",
                            "token",
                            "auth_token",
                        ]
                        .contains(&k.to_lowercase().as_str())
                        {
                            json!("[redacted]")
                        } else {
                            sanitize(v, limit)
                        },
                    )
                })
                .collect(),
        ),
        Value::Array(items) => json!(
            items
                .iter()
                .take(100)
                .map(|v| sanitize(v, limit))
                .collect::<Vec<_>>()
        ),
        Value::String(s) => clip(s, limit).into(),
        _ => value.clone(),
    }
}
fn encoded(value: &Value, limit: usize) -> String {
    crate::python_json::dumps_default_separators(&sanitize(value, limit))
}
impl Trace {
    pub fn start(r: &Registry, skill: &Value, run: &Value, offline: bool) -> Result<Self> {
        let mut trace = Self {
            id: String::new(),
            span: String::new(),
            started: r.now_millis(),
            input: json!({"skillId":skill["skillId"],"input":run["input"],"projectId":run["projectId"],"offline":offline}),
            skill: text(skill, "skillId"),
        };
        if std::env::var("TRACE_ENABLED")
            .is_ok_and(|v| ["0", "false", "no", "off"].contains(&v.to_lowercase().as_str()))
        {
            return Ok(trace);
        }
        let _guard = crate::mutation_gate::mutation_scope(None, &r.root)
            .map_err(|e| error(e.to_string(), 409))?;
        trace.id = r.new_file_id()?;
        trace.span = r.new_file_id()?;
        let meta = json!({"skillId":skill["skillId"],"skillRunId":run["skillRunId"],"skillVersion":skill["version"],"projectId":run["projectId"],"offline":offline});
        db(r)?
            .execute(
                "INSERT INTO trace_runs VALUES (?1,'skill',?2,'running',?3,?4,'',NULL,0,?5,'')",
                params![
                    trace.id,
                    clip(&text(skill, "name"), 240),
                    r.now().replace("+00:00", "Z"),
                    trace.started as f64 / 1000.0,
                    encoded(&meta, 4000)
                ],
            )
            .map_err(sql_error)?;
        Ok(trace)
    }
    pub fn finish(&self, r: &Registry, result: &Value, err: &str) -> Result<()> {
        if self.id.is_empty() {
            return Ok(());
        }
        let _guard = crate::mutation_gate::mutation_scope(None, &r.root)
            .map_err(|e| error(e.to_string(), 409))?;
        let mut conn = db(r)?;
        let tx = conn.transaction().map_err(sql_error)?;
        let end = r.now_millis();
        let duration = (end - self.started).max(0);
        let now = r.now().replace("+00:00", "Z");
        let start = chrono::DateTime::from_timestamp_millis(self.started)
            .unwrap()
            .to_rfc3339()
            .replace("+00:00", "Z");
        let output = if err.is_empty() {
            json!({"artifactCount":result["artifacts"].as_array().map_or(0,Vec::len),"savedItemCount":result["savedItems"].as_array().map_or(0,Vec::len)})
        } else {
            Value::Null
        };
        tx.execute("INSERT INTO trace_spans VALUES (?1,?2,'',?3,'skill_run',?4,?5,?6,?7,?8,?9,?10,?11,'{}','{}',0,0,?12)",params![self.span,self.id,format!("skill.run:{}",self.skill),if err.is_empty() {"ok"} else {"error"},start,self.started as f64/1000.0,now,end as f64/1000.0,duration,encoded(&self.input,4000),encoded(&output,8000),clip(err,8000)]).map_err(sql_error)?;
        tx.execute("UPDATE trace_runs SET status=?1,completed_at=?2,completed_epoch=?3,duration_ms=?4,error=?5 WHERE trace_id=?6",params![if err.is_empty() {"completed"} else {"error"},now,end as f64/1000.0,duration,clip(err,8000),self.id]).map_err(sql_error)?;
        tx.commit().map_err(sql_error)
    }
}
