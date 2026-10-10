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
    apply_explicit_memory_command, build_memory_suggestion, clear_memories,
    delete_memories_by_query, delete_memory_by_id, detect_memory_conflicts, forget_memory,
    format_memory_context, is_sensitive_memory, load_memories, memory_conflict_key, memory_dir,
    memory_file, memory_fingerprint, memory_scope_candidates, memory_scope_from_payload,
    memory_scope_label, memory_tool_scopes, normalize_memory_category, normalize_memory_scope,
    normalize_memory_text, prepare_memory_state, recall_memory, save_memories, upsert_memory,
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

fn write_fixture(root: &Path, fixture: &Value) {
    // `store::file` observes the fixture's bytes, so they must match Python's
    // `json.dumps(entries, ensure_ascii=False, indent=2)` exactly — including the
    // fixture's key order, which differs from the store's canonical order. (The
    // implementation's own write is observed separately, via `store::migrated`.)
    let rendered = OrderedJson::from_value_with_order(fixture, &FIXTURE_KEYS).render_indent_2();
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
    write_fixture(root, &store_fixture());
    out.insert("store::loaded".to_string(), json!(load_memories(root)));
    out.insert(
        "store::file".to_string(),
        json!(fs::read_to_string(memory_file(root)).unwrap_or_default()),
    );

    let saved = json!([
        "not an object",
        {"content": "   "},
        {"content": "survivor", "confidence": 5, "category": "PREFERENCE", "scope": "bogus"},
        {"content": "bad confidence", "confidence": "nope"},
        {"content": 0},
        {"content": true},
        {"content": false}
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
    write_fixture(root, &store_fixture());
    for (label, payload, default_scope) in recall_cases() {
        let arguments = payload.as_object().cloned().unwrap_or_default();
        out.insert(
            format!("recall::{label}"),
            json!(recall_memory(&arguments, default_scope, root, None)),
        );
    }

    // --- forget ---------------------------------------------------------------
    for (label, payload) in forget_cases() {
        write_fixture(root, &store_fixture());
        let arguments = payload.as_object().cloned().unwrap_or_default();
        out.insert(
            format!("forget::{label}"),
            outcome(forget_memory(&arguments, "global", root, &clock())),
        );
    }
    write_fixture(root, &store_fixture());
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

    write_fixture(root, &store_fixture());
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
    write_fixture(root, &store_fixture());
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
    write_fixture(root, &store_fixture());
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

    // --- scope candidates / labels (the turn state's read half) ---------------
    let candidate_cases: Vec<Value> = vec![
        json!({}),
        json!({"messages": []}),
        json!({"memoryScope": "project:abc"}),
        json!({"memoryScope": "bogus"}),
        json!({"memoryScope": 0}),
        json!({"messages": [{"role": "user", "projectId": "p1"}]}),
        json!({"messages": [{"role": "user", "seekId": "s1"}]}),
        json!({"messages": [
            {"role": "assistant", "projectId": "p9"},
            {"role": "user", "content": "hi"},
        ]}),
        json!({"memoryScope": "seek:s2", "messages": [{"role": "user", "projectId": "p1"}]}),
    ];
    for (index, payload) in candidate_cases.iter().enumerate() {
        out.insert(
            format!("candidates::{index}"),
            json!(memory_scope_candidates(payload)),
        );
        out.insert(
            format!("scope-of::{index}"),
            json!(memory_scope_from_payload(payload)),
        );
    }
    for value in [
        "global",
        "project:abc",
        "seek:SEARCH-1",
        "skill:pack.v1",
        "bogus",
        "project:a:b:c",
        "",
    ] {
        out.insert(
            format!("label::{value:?}"),
            json!(memory_scope_label(value)),
        );
    }

    // --- format context ------------------------------------------------------
    // The budget corpus must be able to fail: `normalize_memory_text` caps a row
    // at 1200 chars, so reaching the 8000 budget takes six full rows (used = 7254)
    // plus a 737-char row that lands exactly on 8000 and is kept, with the row
    // after it crossing into the 省略 marker.
    let context_cases: Vec<(&str, Value)> = vec![
        ("empty", json!([])),
        ("fixture", store_fixture()),
        (
            "budget",
            json!([
                {"id": "c-1", "content": "戌".repeat(1200), "category": "fact", "scope": "global"},
                {"id": "c-2", "content": "戌".repeat(1200), "category": "fact", "scope": "global"},
                {"id": "c-3", "content": "戌".repeat(1200), "category": "fact", "scope": "global"},
                {"id": "c-4", "content": "戌".repeat(1200), "category": "fact", "scope": "global"},
                {"id": "c-5", "content": "戌".repeat(1200), "category": "fact", "scope": "global"},
                {"id": "c-6", "content": "戌".repeat(1200), "category": "fact", "scope": "global"},
                {"id": "c-7", "content": "辰".repeat(737), "category": "fact", "scope": "global"},
                {"id": "c-8", "content": "丁".repeat(100), "category": "fact", "scope": "global"},
            ]),
        ),
        (
            "boundary",
            json!([
                {"id": "c-first", "content": "戌".repeat(1200), "category": "fact", "scope": "global"},
                {"id": "c-second", "content": "y", "category": "fact", "scope": "global"},
            ]),
        ),
        (
            "falsy-rows",
            json!([
                {"id": "c-blank", "content": "   ", "category": "fact", "scope": "global"},
                {"id": "c-zero", "content": 0, "category": "fact", "scope": "global"},
                {"id": "c-false", "content": false, "category": "fact", "scope": "global"},
                {"id": "c-true", "content": true, "category": "fact", "scope": "global"},
                {"id": "c-real", "content": "真实记忆", "category": "fact", "scope": "global"},
            ]),
        ),
        (
            "scoped",
            json!([
                {"id": "c-scope", "content": "项目记忆", "category": "project", "scope": "project:abc"},
                {"id": "c-global", "content": "全局记忆", "category": "fact", "scope": "global"},
            ]),
        ),
        (
            "no-category",
            json!([{"id": "c-nocat", "content": "no category", "scope": "global"}]),
        ),
        (
            "category-number",
            json!([{"id": "c-numcat", "content": "x", "category": 7, "scope": "global"}]),
        ),
    ];
    for (label, rows) in context_cases {
        let rows = rows.as_array().cloned().unwrap_or_default();
        out.insert(
            format!("context::{label}"),
            json!(format_memory_context(&rows)),
        );
    }

    // --- upsert ----------------------------------------------------------------
    let update_id = memory_fingerprint("我用 React 做前端", "global");
    let mut update_fixture_value = store_fixture();
    for row in update_fixture_value.as_array_mut().expect("fixture rows") {
        if row.get("id").and_then(Value::as_str) == Some("m-pref") {
            row["id"] = Value::String(update_id.clone());
        }
    }
    let pinned_id = memory_fingerprint("固定内容", "global");
    let pinned_fixture = json!([
        {"id": pinned_id, "content": "固定内容", "category": "fact", "scope": "global",
         "pinned": true, "createdAt": "2026-01-01T00:00:00+00:00",
         "updatedAt": "2026-01-01T00:00:00+00:00"},
    ]);
    struct UpsertCase<'a> {
        label: &'a str,
        content: &'a str,
        category: Option<&'a str>,
        scope: &'a str,
        source: &'a str,
        pinned: bool,
        replace: Option<Vec<&'a str>>,
        fixture: Value,
    }
    let upsert_cases = vec![
        UpsertCase {
            label: "new",
            content: "新的一条：项目用 pnpm",
            category: None,
            scope: "global",
            source: "manual",
            pinned: false,
            replace: None,
            fixture: store_fixture(),
        },
        UpsertCase {
            label: "update",
            content: "我用 React 做前端",
            category: None,
            scope: "global",
            source: "manual",
            pinned: false,
            replace: None,
            fixture: update_fixture_value.clone(),
        },
        UpsertCase {
            label: "sensitive",
            content: "my password is hunter2",
            category: None,
            scope: "global",
            source: "manual",
            pinned: false,
            replace: None,
            fixture: store_fixture(),
        },
        UpsertCase {
            label: "empty",
            content: "   ",
            category: None,
            scope: "global",
            source: "manual",
            pinned: false,
            replace: None,
            fixture: store_fixture(),
        },
        UpsertCase {
            label: "replace",
            content: "全新的内容",
            category: None,
            scope: "global",
            source: "manual",
            pinned: false,
            replace: Some(vec!["m-pref"]),
            fixture: store_fixture(),
        },
        UpsertCase {
            label: "scoped",
            content: "项目事实",
            category: None,
            scope: "project:abc",
            source: "manual",
            pinned: false,
            replace: None,
            fixture: store_fixture(),
        },
        UpsertCase {
            label: "category-source",
            content: "whatever",
            category: Some("todo"),
            scope: "global",
            source: "agent",
            pinned: false,
            replace: None,
            fixture: store_fixture(),
        },
        UpsertCase {
            label: "pinned-merge",
            content: "固定内容",
            category: None,
            scope: "global",
            source: "manual",
            pinned: false,
            replace: None,
            fixture: pinned_fixture.clone(),
        },
    ];
    for case in &upsert_cases {
        write_fixture(root, &case.fixture);
        let category = case.category.map(|text| Value::String(text.to_string()));
        let replace_owned: Option<Vec<String>> = case
            .replace
            .as_ref()
            .map(|ids| ids.iter().map(|id| id.to_string()).collect());
        out.insert(
            format!("upsert::{}", case.label),
            outcome(upsert_memory(
                case.content,
                category.as_ref(),
                case.scope,
                case.source,
                case.pinned,
                replace_owned.as_deref(),
                root,
                &clock(),
            )),
        );
        out.insert(
            format!("upsert::{}::file", case.label),
            json!(fs::read_to_string(memory_file(root)).unwrap_or_default()),
        );
    }

    // --- clear / delete-by-id ---------------------------------------------------
    write_fixture(root, &store_fixture());
    out.insert(
        "clear::count".to_string(),
        outcome(clear_memories(root, &clock())),
    );
    out.insert(
        "clear::file".to_string(),
        json!(fs::read_to_string(memory_file(root)).unwrap_or_default()),
    );
    write_fixture(root, &store_fixture());
    out.insert(
        "by-id::hit".to_string(),
        outcome(delete_memory_by_id("m-pref", root, &clock())),
    );
    out.insert(
        "by-id::hit-file".to_string(),
        json!(fs::read_to_string(memory_file(root)).unwrap_or_default()),
    );
    out.insert(
        "by-id::miss".to_string(),
        outcome(delete_memory_by_id("absent-id", root, &clock())),
    );
    out.insert(
        "by-id::miss-file".to_string(),
        json!(fs::read_to_string(memory_file(root)).unwrap_or_default()),
    );

    // --- explicit commands --------------------------------------------------------
    struct CommandCase<'a> {
        label: &'a str,
        query: &'a str,
        scope: &'a str,
        scopes: Option<Vec<&'a str>>,
    }
    let command_cases = vec![
        CommandCase {
            label: "remember",
            query: "请帮我记住: 我的生日是 3 月 5 日",
            scope: "global",
            scopes: None,
        },
        CommandCase {
            label: "remember-en",
            query: "Don't forget: the alignment review",
            scope: "global",
            scopes: None,
        },
        CommandCase {
            label: "negated-forget",
            query: "不要忘记: 牙医预约",
            scope: "global",
            scopes: None,
        },
        CommandCase {
            label: "forget",
            query: "忘记: React",
            scope: "global",
            scopes: Some(vec!["global"]),
        },
        CommandCase {
            label: "delete-memory",
            query: "删除记忆: 生日",
            scope: "global",
            scopes: Some(vec!["global"]),
        },
        CommandCase {
            label: "forget-upper",
            query: "FORGET: react",
            scope: "global",
            scopes: Some(vec!["global"]),
        },
        CommandCase {
            label: "opt-out",
            query: "不要记住: 这是临时的",
            scope: "global",
            scopes: None,
        },
        CommandCase {
            label: "opt-out-en",
            query: "don't remember: this",
            scope: "global",
            scopes: None,
        },
        CommandCase {
            label: "bare-remember-gap",
            query: "记住: 我的生日",
            scope: "global",
            scopes: None,
        },
        CommandCase {
            label: "sensitive",
            query: "请帮我记住: my api key",
            scope: "global",
            scopes: None,
        },
        CommandCase {
            label: "multiline",
            query: "请帮我记住: 第一行\n第二行",
            scope: "global",
            scopes: None,
        },
        CommandCase {
            label: "kept-scoped",
            query: "不要忘记: 项目笔记",
            scope: "project:abc",
            scopes: None,
        },
        CommandCase {
            label: "forget-scoped",
            query: "忘记: Rust",
            scope: "global",
            scopes: Some(vec!["project:abc"]),
        },
        CommandCase {
            label: "plain",
            query: "今天天气怎么样",
            scope: "global",
            scopes: None,
        },
    ];
    for case in &command_cases {
        write_fixture(root, &store_fixture());
        let scopes_owned: Option<Vec<String>> = case
            .scopes
            .as_ref()
            .map(|scopes| scopes.iter().map(|scope| scope.to_string()).collect());
        out.insert(
            format!("command::{}", case.label),
            outcome(apply_explicit_memory_command(
                case.query,
                case.scope,
                scopes_owned.as_deref(),
                root,
                &clock(),
            )),
        );
        out.insert(
            format!("command::{}::file", case.label),
            json!(fs::read_to_string(memory_file(root)).unwrap_or_default()),
        );
    }

    // --- the turn state ------------------------------------------------------------
    let state_cases: Vec<(&str, Value)> = vec![
        (
            "disabled",
            json!({"memoryEnabled": false, "messages": [
                {"role": "user", "content": "请帮我记住: 这条不该保存"},
            ]}),
        ),
        (
            "falsy-enabled",
            json!({"memoryEnabled": 0, "messages": [
                {"role": "user", "content": "React 怎么用"},
            ]}),
        ),
        (
            "plain",
            json!({"messages": [{"role": "user", "content": "React 怎么用"}]}),
        ),
        (
            "remember",
            json!({"messages": [
                {"role": "user", "content": "请帮我记住: 我喜欢深色主题"},
            ]}),
        ),
        (
            "sensitive",
            json!({"messages": [
                {"role": "user", "content": "请帮我记住: my password is 123"},
            ]}),
        ),
        (
            "scoped",
            json!({"messages": [
                {"role": "user", "projectId": "abc", "content": "Rust"},
            ]}),
        ),
        ("no-messages", json!({})),
        ("explicit-scope", json!({"memoryScope": "seek:s1"})),
        (
            "broad",
            json!({"messages": [{"role": "user", "content": "你记得什么"}]}),
        ),
    ];
    for (label, payload) in state_cases {
        write_fixture(root, &store_fixture());
        out.insert(
            format!("state::{label}"),
            json!(prepare_memory_state(&payload, root, &clock(), None)),
        );
        out.insert(
            format!("state::{label}::file"),
            json!(fs::read_to_string(memory_file(root)).unwrap_or_default()),
        );
    }
    out.insert("state::generation".to_string(), generation(root));

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
