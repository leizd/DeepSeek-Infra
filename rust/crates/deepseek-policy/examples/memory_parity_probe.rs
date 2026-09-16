//! Memory-store parity probe, Rust side.
//!
//! Replays the same sequence as `tasks/native-runtime/memory_parity_probe.py`
//! through `deepseek_policy::memory` and prints canonical JSON.
//!
//! Mirrors the probe's pins: a fixed clock (`FROZEN_EPOCH`), and no vector-hit
//! provider — which reproduces the oracle's own `except Exception` path, since the
//! RAG memory index is not ported. See `docs/MEMORY_STORE.md`.
//!
//! Usage::
//!
//!     python tasks/native-runtime/memory_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example memory_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use std::fs;
use std::path::{Path, PathBuf};

use deepseek_policy::app_error::AppError;
use deepseek_policy::core_utils::FixedClock;
use deepseek_policy::memory::{
    build_memory_suggestion, delete_memories_by_query, detect_memory_conflicts, forget_memory,
    is_sensitive_memory, load_memories, memory_conflict_key, memory_dir, memory_file,
    memory_fingerprint, memory_tool_scopes, normalize_memory_category, normalize_memory_scope,
    normalize_memory_text, recall_memory, save_memories,
};
use deepseek_policy::python_json::OrderedJson;
use serde_json::{Map, Value, json};

const FROZEN_EPOCH: i64 = 1_760_000_000;

struct Scratch {
    root: PathBuf,
}

impl Scratch {
    fn new(label: &str) -> Self {
        let root =
            std::env::temp_dir().join(format!("memory-parity-{label}-{}", std::process::id()));
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

fn clock() -> FixedClock {
    FixedClock {
        epoch_seconds: FROZEN_EPOCH,
    }
}

fn scope_cases() -> Vec<&'static str> {
    vec![
        "global",
        "  global  ",
        "project:abc",
        "seek:SEARCH-1",
        "skill:pack.v1",
        "automation:job_2",
        "project:",
        "project:a b",
        "unknown:x",
        "global:x",
        "PROJECT:abc",
        "",
        "   ",
    ]
}

fn text_cases() -> Vec<String> {
    vec![
        "  a   b\tc  ".to_string(),
        String::new(),
        "   ".to_string(),
        "x".repeat(2000),
    ]
}

fn category_cases() -> Vec<(&'static str, Option<&'static str>)> {
    vec![
        ("我喜欢简洁", None),
        ("项目 代码", None),
        ("待办 计划", None),
        ("the sky is blue", None),
        ("我喜欢简洁", Some("FACT")),
        ("x", Some("preference")),
        ("x", Some("  project  ")),
        ("x", Some("nope")),
    ]
}

fn conflict_cases() -> Vec<(&'static str, &'static str)> {
    vec![
        ("我用 React", "preference"),
        ("请一步一步来", "preference"),
        ("用中文回答", "preference"),
        ("叫我 leizd", "preference"),
        ("我喜欢深色主题", "preference"),
        ("我喜欢猫", "preference"),
        ("项目：DeepSeek-Infra", "project"),
        ("事实", "fact"),
    ]
}

fn sensitive_cases() -> Vec<&'static str> {
    vec![
        "my api key is x",
        "APIKEY",
        "the token",
        "secret",
        "password",
        "密码",
        "密钥",
        "银行卡",
        "身份证",
        "验证码",
        "授权码",
        "PASSWORD",
        "I like concise answers",
    ]
}

fn tool_scope_cases() -> Vec<(&'static str, &'static str)> {
    vec![
        ("", "global"),
        ("", "project:abc"),
        ("global", "project:abc"),
        ("seek:s1", "project:abc"),
        ("bogus", "project:abc"),
        ("", ""),
    ]
}

fn store_fixture() -> Value {
    json!([
        {"id": "m-pref", "content": "我用 React 做前端", "category": "preference", "scope": "global",
         "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-05T00:00:00+00:00"},
        {"id": "m-pinned", "content": "重要：我用 React", "category": "fact", "scope": "global",
         "pinned": true, "createdAt": "2026-01-01T00:00:00+00:00",
         "updatedAt": "2026-01-02T00:00:00+00:00"},
        {"id": "m-project", "content": "项目用 Rust 写", "category": "project", "scope": "project:abc",
         "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-03T00:00:00+00:00"},
        {"id": "m-unrelated", "content": "the sky is blue", "category": "fact", "scope": "global",
         "createdAt": "2026-01-01T00:00:00+00:00", "updatedAt": "2026-01-01T00:00:00+00:00"}
    ])
}

fn recall_cases() -> Vec<(&'static str, Value, &'static str)> {
    vec![
        ("match", json!({"query": "React"}), "global"),
        ("blank-query", json!({"query": "   "}), "global"),
        ("broad", json!({"query": "你记得什么"}), "global"),
        ("no-match", json!({"query": "zzz-absent"}), "global"),
        ("project-scope", json!({"query": "Rust"}), "project:abc"),
        (
            "explicit-global",
            json!({"query": "Rust", "scope": "global"}),
            "project:abc",
        ),
        (
            "explicit-scope",
            json!({"query": "Rust", "scope": "project:abc"}),
            "global",
        ),
    ]
}

fn forget_cases() -> Vec<(&'static str, Value)> {
    vec![
        ("blank", json!({"query": "   "})),
        ("absent", json!({"query": "zzz-absent"})),
    ]
}

fn suggest_cases() -> Vec<(&'static str, Value, &'static str)> {
    vec![
        (
            "plain",
            json!({"content": "  我喜欢简洁的回答  "}),
            "global",
        ),
        (
            "sensitive",
            json!({"content": "my password is hunter2"}),
            "global",
        ),
        ("empty", json!({"content": "   "}), "global"),
        (
            "project",
            json!({"content": "项目用 Rust 写", "scope": "project:abc"}),
            "global",
        ),
        (
            "bad-scope",
            json!({"content": "facts", "scope": "bogus"}),
            "global",
        ),
        (
            "category",
            json!({"content": "whatever", "category": "PREFERENCE"}),
            "global",
        ),
    ]
}

fn outcome<T: serde::Serialize>(result: Result<T, AppError>) -> Value {
    match result {
        Ok(value) => json!({"ok": true, "result": value}),
        Err(error) => json!({"ok": false, "error": error.message, "code": error.code}),
    }
}

fn write_raw(root: &Path, raw: &str) {
    fs::create_dir_all(memory_dir(root)).expect("create memory dir");
    fs::write(memory_file(root), raw).expect("write store");
}

/// The fixture's own key order, which is not the store's canonical order.
const FIXTURE_KEYS: [&str; 7] = [
    "id",
    "content",
    "category",
    "scope",
    "pinned",
    "createdAt",
    "updatedAt",
];

fn write_fixture(root: &Path) {
    // `store::file` observes the fixture's bytes, so they must match Python's
    // `json.dumps(entries, ensure_ascii=False, indent=2)` exactly — including the
    // fixture's key order, which differs from the store's canonical order. (The
    // implementation's own write is observed separately, via `store::migrated`.)
    let rendered =
        OrderedJson::from_value_with_order(&store_fixture(), &FIXTURE_KEYS).render_indent_2();
    write_raw(root, &rendered);
}

fn generation(root: &Path) -> Value {
    match fs::read_to_string(root.join(".workspace-generation")) {
        Ok(text) => json!(text),
        Err(_) => Value::Null,
    }
}

/// `normalize_memory_text` takes `Option<&Value>` in this port.
fn text(value: &str) -> Value {
    Value::String(normalize_memory_text(Some(&json!(value))))
}

fn optional(value: Option<&str>) -> Option<Value> {
    value.map(|text| Value::String(text.to_string()))
}

fn main() {
    let scratch = Scratch::new("rust");
    let root = scratch.path();
    let mut out = Map::new();

    // --- pure helpers ---------------------------------------------------------
    for (index, value) in text_cases().iter().enumerate() {
        out.insert(format!("text::{index}"), text(value));
    }
    for value in scope_cases() {
        out.insert(
            format!("scope::normalize::{:?}", value),
            json!(normalize_memory_scope(Some(&json!(value)))),
        );
    }
    for (label, content) in [
        ("plain", "Hello"),
        ("case", "hello"),
        ("spaces", "  a   b  "),
    ] {
        out.insert(
            format!("fingerprint::{label}"),
            json!(memory_fingerprint(content, "global")),
        );
    }
    out.insert(
        "fingerprint::scoped".to_string(),
        json!(memory_fingerprint("Hello", "project:abc")),
    );
    out.insert(
        "fingerprint::distinct".to_string(),
        json!(memory_fingerprint("x", "project:a")),
    );
    for content in sensitive_cases() {
        out.insert(
            format!("sensitive::{content:?}"),
            json!(is_sensitive_memory(content)),
        );
    }
    for (index, (content, category)) in category_cases().iter().enumerate() {
        let owned = optional(*category);
        out.insert(
            format!("category::{index}"),
            json!(normalize_memory_category(owned.as_ref(), content)),
        );
    }
    for (content, category) in conflict_cases() {
        out.insert(
            format!("conflict::{content:?}"),
            json!(memory_conflict_key(content, category)),
        );
    }

    // --- tool scopes ----------------------------------------------------------
    for (index, (scope, default)) in tool_scope_cases().iter().enumerate() {
        out.insert(
            format!("tool-scopes::{index}"),
            json!(memory_tool_scopes(scope, default)),
        );
    }

    // --- suggest --------------------------------------------------------------
    for (label, payload, default_scope) in suggest_cases() {
        let scope_argument = payload.get("scope").and_then(Value::as_str);
        let scope = match scope_argument {
            Some(text) if !text.is_empty() => text.to_string(),
            _ => default_scope.to_string(),
        };
        let scope = normalize_memory_scope(Some(&json!(scope)));
        let content = payload
            .get("content")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        let category = payload
            .get("category")
            .and_then(Value::as_str)
            .unwrap_or("");
        out.insert(
            format!("suggest::{label}"),
            outcome(build_memory_suggestion(
                &content,
                Some(&json!(category)),
                &scope,
                root,
            )),
        );
    }

    // --- the store ------------------------------------------------------------
    write_fixture(root);
    out.insert("store::loaded".to_string(), json!(load_memories(root)));
    out.insert(
        "store::file".to_string(),
        json!(fs::read_to_string(memory_file(root)).unwrap_or_default()),
    );

    let saved = json!([
        "not an object",
        {"content": "   "},
        {"content": "survivor", "confidence": 5, "category": "PREFERENCE", "scope": "bogus"},
        {"content": "bad confidence", "confidence": "nope"}
    ]);
    save_memories(root, saved.as_array().unwrap(), &clock()).expect("save");
    out.insert(
        "store::migrated".to_string(),
        json!(fs::read_to_string(memory_file(root)).unwrap_or_default()),
    );

    for (label, raw) in [
        ("not-json", "{not json"),
        ("scalar", "42"),
        ("object", "{}"),
        ("empty-list", "[]"),
        ("mixed", r#"[{"id": "a"}, "x", 7, null, {"id": "b"}]"#),
    ] {
        write_raw(root, raw);
        out.insert(format!("read::{label}"), json!(load_memories(root)));
    }

    // --- recall ---------------------------------------------------------------
    write_fixture(root);
    for (label, payload, default_scope) in recall_cases() {
        let arguments = payload.as_object().cloned().unwrap_or_default();
        out.insert(
            format!("recall::{label}"),
            json!(recall_memory(&arguments, default_scope, root, None)),
        );
    }

    // --- forget ---------------------------------------------------------------
    for (label, payload) in forget_cases() {
        write_fixture(root);
        let arguments = payload.as_object().cloned().unwrap_or_default();
        out.insert(
            format!("forget::{label}"),
            outcome(forget_memory(&arguments, "global", root, &clock())),
        );
    }
    write_fixture(root);
    let react = json!({"query": "react"});
    out.insert(
        "forget::react-global".to_string(),
        outcome(forget_memory(
            react.as_object().unwrap(),
            "global",
            root,
            &clock(),
        )),
    );
    out.insert("forget::after".to_string(), json!(load_memories(root)));

    write_fixture(root);
    let react_capitalized = json!({"query": "React"});
    out.insert(
        "forget::project-default".to_string(),
        outcome(forget_memory(
            react_capitalized.as_object().unwrap(),
            "project:abc",
            root,
            &clock(),
        )),
    );
    out.insert(
        "forget::project-after".to_string(),
        json!(
            load_memories(root)
                .iter()
                .map(|item| item["id"].clone())
                .collect::<Vec<Value>>()
        ),
    );

    // --- delete semantics -----------------------------------------------------
    write_fixture(root);
    let global = vec!["global".to_string()];
    out.insert(
        "delete::no-match".to_string(),
        outcome(delete_memories_by_query(
            "zzz-absent",
            Some(&global),
            root,
            &clock(),
        )),
    );
    out.insert("delete::no-write-generation".to_string(), generation(root));
    out.insert(
        "delete::match".to_string(),
        outcome(delete_memories_by_query(
            "react",
            Some(&global),
            root,
            &clock(),
        )),
    );
    out.insert("delete::generation".to_string(), generation(root));

    // --- conflicts ------------------------------------------------------------
    write_fixture(root);
    out.insert(
        "conflicts::same-domain".to_string(),
        json!(detect_memory_conflicts(
            root,
            "我用 Vue",
            Some(&json!("preference")),
            "global"
        )),
    );
    out.insert(
        "conflicts::identical".to_string(),
        json!(detect_memory_conflicts(
            root,
            "我用 React 做前端",
            Some(&json!("preference")),
            "global"
        )),
    );
    out.insert(
        "conflicts::no-key".to_string(),
        json!(detect_memory_conflicts(
            root,
            "the sky is blue",
            Some(&json!("fact")),
            "global"
        )),
    );
    out.insert(
        "lock::exists".to_string(),
        json!(memory_dir(root).join("memories.lock").exists()),
    );

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
