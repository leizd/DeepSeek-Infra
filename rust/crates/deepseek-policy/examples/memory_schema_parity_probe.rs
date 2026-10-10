//! Memory v3 schema/policy parity probe, Rust side.
//!
//! Replays the same corpus as
//! `tasks/native-runtime/memory_schema_parity_probe.py` through
//! `deepseek_policy::memory_schema` and prints canonical JSON.
//!
//! The clock is pinned to the same instant the Python probe rebinds `utc_now_iso`
//! to, and minted ids are masked to `<id>` on both sides, so the two outputs are
//! byte-comparable.
//!
//! Usage::
//!
//!     python tasks/native-runtime/memory_schema_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example memory_schema_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use deepseek_policy::core_utils::{Clock, utc_now_iso};
use deepseek_policy::memory as legacy;
use deepseek_policy::memory_schema as schema;
use serde_json::{Map, Value, json};

/// The instant the Python probe pins `utc_now_iso` to
/// (`2026-09-19T11:02:17+00:00`).
const FIXED_EPOCH: i64 = 1_789_815_737;

struct FixedClock;

impl Clock for FixedClock {
    fn now_iso(&self) -> String {
        utc_now_iso(FIXED_EPOCH)
    }
}

fn scope_cases() -> Vec<&'static str> {
    vec![
        "global",
        "project:p1",
        "skill:s1",
        "automation:a1",
        "seek:x",
        "project",
        "PROJECT:p1",
        "project:",
        "project:bad id",
        // 81 and 80 `a`s: the storage normaliser's cap is 80.
        "project:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "project:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "unknown:x",
        "",
        "   ",
        "global:extra",
    ]
}

fn storage_scope_cases() -> Vec<(
    &'static str,
    &'static str,
    &'static str,
    &'static str,
    &'static str,
)> {
    vec![
        ("global-none", "global", "", "", ""),
        ("project-with-id", "project", "p1", "", ""),
        ("project-no-id", "project", "", "", ""),
        ("skill-with-id", "skill", "", "s1", ""),
        ("automation-with-id", "automation", "", "", "a1"),
        ("prefixed", "project:p1", "", "", ""),
        ("unknown-family", "nonsense", "p1", "", ""),
        ("blank", "", "p1", "", ""),
        ("bad-id", "project", "bad id", "", ""),
    ]
}

fn type_cases() -> Vec<&'static str> {
    vec![
        "preference",
        "fact",
        "project",
        "todo",
        "instruction",
        "summary",
        "artifact_ref",
        "PREFERENCE",
        "  fact  ",
        "",
        "nonsense",
    ]
}

fn confidence_cases() -> Vec<Value> {
    vec![
        Value::Null,
        json!(0.9),
        json!(1.0),
        json!(0.0),
        json!(2.0),
        json!(-1.0),
        json!(0.25),
        json!("0.5"),
        json!("1e-3"),
        json!("nonsense"),
        json!(""),
        json!(true),
        json!(false),
        json!("nan"),
        json!("inf"),
        json!("-inf"),
        json!(1_000_000_000_i64),
    ]
}

fn source_ref_cases() -> Vec<Value> {
    vec![
        json!({}),
        json!({"kind": "chat"}),
        json!({"bad key!": "x", "ok": 1}),
        json!({"nested": {"ok": 1, "drop me!": 2}}),
        json!({"emptyNested": {"!!!": 1}}),
        json!({"list": [1, null, "two"]}),
        json!({"emptyList": [null]}),
        json!({"nothing": null, "flag": true, "zero": 0}),
        json!({"!!!": 1}),
        json!({ "a".repeat(100): "x" }),
        json!({"deep": {"deeper": {"deepest": [1, 2, 3]}}}),
        json!({"many": (0..30).collect::<Vec<i64>>()}),
    ]
}

fn public_source_cases() -> Vec<Value> {
    vec![
        Value::Null,
        json!({"kind": "chat"}),
        json!({"kind": "nonsense"}),
        json!({"kind": ""}),
        json!({"type": "chat", "id": "m1"}),
        json!({"refId": "r1"}),
        json!({"savedItemId": "s1"}),
        json!({"messageId": "msg1"}),
        json!({"refId": "", "id": "fallback-used"}),
        json!("chat"),
        json!(5),
        json!(true),
        json!({}),
    ]
}

fn public_memory_cases() -> Vec<Value> {
    vec![
        json!({"id": "m1", "content": "hello"}),
        json!({"id": "m1", "content": "hello", "createdAt": "2020-01-01T00:00:00+00:00"}),
        json!({"id": "m1", "content": "hello", "updatedAt": "2021-01-01T00:00:00+00:00"}),
        json!({"memoryId": "mid1", "id": "m1", "content": "both"}),
        json!({"id": "m1", "content": "c", "scope": "project:p1", "type": "instruction"}),
        json!({"id": "m1", "content": "c", "scope": "seek:x", "category": "todo"}),
        json!({"id": "m1", "content": "c", "pinned": true, "confidence": 0.4}),
        json!({"id": "m1", "content": "c", "pinned": "yes"}),
        json!({"id": "m1", "content": "c", "expiresAt": "2030-01-01T00:00:00+00:00"}),
        json!({"id": "m1", "content": "c", "expiresAt": ""}),
        json!({"id": "m1", "content": "   collapsed   text  "}),
        json!({"id": "m1", "content": "c", "source": {"kind": "chat", "refId": "r"}}),
        json!({"id": "m1", "content": "c", "source": "manual"}),
        json!({"id": "m1", "content": "c", "source": 5}),
        json!({"id": "m1", "content": ""}),
        json!({"id": "", "content": "no id"}),
        json!({"content": "no id at all"}),
    ]
}

fn safe_cases() -> Vec<&'static str> {
    vec![
        "just a normal fact",
        "my api key is sk-abcdefghijklmnopqrstuvwxyz",
        "password: hunter2hunter2",
        "",
    ]
}

fn readable_scope_cases() -> Vec<(&'static str, &'static str, &'static str, &'static str)> {
    vec![
        ("none", "", "", ""),
        ("project", "p1", "", ""),
        ("skill", "", "s1", ""),
        ("automation", "", "", "a1"),
        ("all", "p1", "s1", "a1"),
    ]
}

fn skill_policy_cases() -> Vec<Value> {
    vec![
        json!({}),
        json!({"memoryPolicy": {}}),
        json!({"memoryPolicy": {"read": false}}),
        json!({"memoryPolicy": {"read": true}}),
        json!({"memoryPolicy": {"read": 0}}),
        json!({"memoryPolicy": {"read": "no"}}),
        json!({"memoryPolicy": {"read": true, "scope": "project"}}),
        json!({"memoryPolicy": {"read": true, "scope": "global"}}),
        json!({"memoryPolicy": "not-a-dict"}),
    ]
}

/// `mask_ids`: every minted id becomes `<id>` so two runs compare equal.
fn mask(value: &Value, known: &[String]) -> Value {
    match value {
        Value::String(text) => {
            let mut masked = text.clone();
            for id in known {
                if !id.is_empty() {
                    masked = masked.replace(id.as_str(), "<id>");
                }
            }
            Value::String(masked)
        }
        Value::Array(items) => Value::Array(items.iter().map(|item| mask(item, known)).collect()),
        Value::Object(fields) => Value::Object(
            fields
                .iter()
                .map(|(key, item)| (key.clone(), mask(item, known)))
                .collect(),
        ),
        other => other.clone(),
    }
}

fn id_shape(value: &Value) -> String {
    match value {
        Value::String(text)
            if text.len() == 16
                && text
                    .chars()
                    .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()) =>
        {
            "<hex16>".to_string()
        }
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

/// Python's `repr()` of a string, for the corpus labels.
///
/// Rust's `{:?}` always emits double quotes while Python prefers single quotes and
/// switches to double only when the text contains a `'` and no `"`. The corpus labels
/// are used verbatim as object keys, so the two must agree byte for byte.
fn python_repr(text: &str) -> String {
    let has_single = text.contains('\'');
    let has_double = text.contains('"');
    let quote = if has_single && !has_double { '"' } else { '\'' };
    let mut rendered = String::with_capacity(text.len() + 2);
    rendered.push(quote);
    for character in text.chars() {
        match character {
            '\\' => rendered.push_str("\\\\"),
            '\n' => rendered.push_str("\\n"),
            '\r' => rendered.push_str("\\r"),
            '\t' => rendered.push_str("\\t"),
            c if c == quote => {
                rendered.push('\\');
                rendered.push(c);
            }
            c => rendered.push(c),
        }
    }
    rendered.push(quote);
    rendered
}

/// The instant the Python probe pins, rendered the same way.
fn fixed_clock() -> FixedClock {
    FixedClock
}

fn temp_root() -> PathBuf {
    let root = std::env::temp_dir().join(format!("memory-schema-probe-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).expect("create probe root");
    root
}

fn main() {
    let mut out: BTreeMap<String, Value> = BTreeMap::new();

    for label in scope_cases() {
        out.insert(
            format!("public_scope::{}", python_repr(label)),
            json!(schema::public_scope(label)),
        );
    }

    for (label, scope, project_id, skill_id, automation_id) in storage_scope_cases() {
        out.insert(
            format!("storage_scope::{label}"),
            json!(schema::storage_scope(
                scope,
                project_id,
                skill_id,
                automation_id
            )),
        );
    }

    for label in type_cases() {
        out.insert(
            format!("public_type::{}", python_repr(label)),
            json!(schema::public_type(label)),
        );
        out.insert(
            format!("legacy_category::{}", python_repr(label)),
            json!(schema::legacy_category(label)),
        );
    }

    for (index, value) in confidence_cases().iter().enumerate() {
        let result = schema::public_confidence(Some(value));
        // JSON has no NaN/inf, so both sides report the same tags.
        let rendered = if result.is_nan() {
            json!("nan")
        } else if result.is_infinite() {
            if result > 0.0 {
                json!("inf")
            } else {
                json!("-inf")
            }
        } else {
            json!(result)
        };
        out.insert(format!("confidence::{index}"), rendered);
    }

    for (index, value) in source_ref_cases().iter().enumerate() {
        out.insert(
            format!("source_ref::{index}"),
            schema::normalize_source_ref(value),
        );
    }

    for (index, value) in public_source_cases().iter().enumerate() {
        let source = if value.is_null() { None } else { Some(value) };
        out.insert(
            format!("public_source::{index}"),
            schema::public_source(source, ""),
        );
        out.insert(
            format!("public_source_fallback::{index}"),
            schema::public_source(source, "fb"),
        );
    }

    for (index, value) in public_memory_cases().iter().enumerate() {
        out.insert(
            format!("public_memory::{index}"),
            schema::public_memory(value, &fixed_clock()),
        );
    }

    for label in safe_cases() {
        let entry = match schema::assert_memory_safe(label) {
            Ok(()) => json!({"refused": false}),
            Err(error) => json!({"refused": true, "code": error.code}),
        };
        out.insert(format!("safe::{}", python_repr(label)), entry);
    }

    for (label, project_id, skill_id, automation_id) in readable_scope_cases() {
        out.insert(
            format!("readable::{label}"),
            json!(schema::readable_scopes(project_id, skill_id, automation_id)),
        );
    }

    for (index, skill) in skill_policy_cases().iter().enumerate() {
        out.insert(
            format!("can_read::{index}"),
            json!(schema::skill_can_read_memory(skill, "p1")),
        );
        out.insert(
            format!("can_read_noproject::{index}"),
            json!(schema::skill_can_read_memory(skill, "")),
        );
    }

    // --- store operations, against a real temp root ------------------------------
    let root = temp_root();
    store_operations(&mut out, &root);

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out.into_iter().collect())).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}

fn store_operations(out: &mut BTreeMap<String, Value>, root: &Path) {
    let clock = fixed_clock();
    let created = schema::add_memory(
        "likes dark mode",
        "global",
        "preference",
        "",
        "",
        "",
        Some(&json!({"kind": "chat", "refId": "c1"})),
        0.7,
        "",
        true,
        root,
        &clock,
    )
    .expect("add_memory");
    let minted = vec![
        created
            .get("memoryId")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
    ];
    out.insert(
        "store::add::shape".to_string(),
        json!(id_shape(created.get("memoryId").unwrap_or(&Value::Null))),
    );
    out.insert("store::add::item".to_string(), mask(&created, &minted));

    let stored = legacy::load_memories(root);
    out.insert("store::stored_count".to_string(), json!(stored.len()));
    out.insert(
        "store::stored_row".to_string(),
        mask(stored.first().unwrap_or(&json!({})), &minted),
    );

    let _ = schema::add_memory(
        "a global fact",
        "global",
        "fact",
        "",
        "",
        "",
        None,
        0.9,
        "",
        false,
        root,
        &clock,
    );
    let _ = schema::add_memory(
        "a project fact",
        "project",
        "fact",
        "p1",
        "",
        "",
        None,
        0.9,
        "",
        false,
        root,
        &clock,
    );
    out.insert(
        "store::list_all".to_string(),
        mask(&json!(schema::list_memories("", "", root, &clock)), &minted),
    );
    out.insert(
        "store::list_project".to_string(),
        mask(
            &json!(schema::list_memories("project", "p1", root, &clock)),
            &minted,
        ),
    );
    out.insert(
        "store::list_global".to_string(),
        mask(
            &json!(schema::list_memories("global", "", root, &clock)),
            &minted,
        ),
    );

    let edited_id = minted[0].clone();
    let mut updates = Map::new();
    updates.insert("content".to_string(), json!("updated"));
    updates.insert("type".to_string(), json!("instruction"));
    updates.insert("pinned".to_string(), json!(true));
    let edited = schema::edit_memory(&edited_id, &updates, root, &clock).expect("edit_memory");
    out.insert("store::edit".to_string(), mask(&edited, &minted));

    match schema::edit_memory("missing-id", &Map::new(), root, &clock) {
        Ok(_) => out.insert(
            "store::edit_missing".to_string(),
            json!({"unexpected": true}),
        ),
        Err(error) => out.insert(
            "store::edit_missing".to_string(),
            json!({"code": error.code, "status": error.status}),
        ),
    };
    match schema::edit_memory("  ", &Map::new(), root, &clock) {
        Ok(_) => out.insert("store::edit_blank".to_string(), json!({"unexpected": true})),
        Err(error) => out.insert("store::edit_blank".to_string(), json!({"code": error.code})),
    };

    out.insert(
        "store::search".to_string(),
        mask(
            &json!(schema::search_memories(
                "updated",
                "",
                "",
                "",
                Some(10),
                root,
                &clock
            )),
            &minted,
        ),
    );
    out.insert(
        "store::search_zero".to_string(),
        mask(
            &json!(schema::search_memories(
                "updated",
                "",
                "",
                "",
                Some(0),
                root,
                &clock
            )),
            &minted,
        ),
    );
    out.insert(
        "store::search_no_limit".to_string(),
        json!(schema::search_memories("updated", "", "", "", None, root, &clock).len()),
    );
    out.insert(
        "store::context_none".to_string(),
        json!(schema::memory_context_for_skill(
            &json!({}),
            "updated",
            "",
            root,
            &clock
        )),
    );
    out.insert(
        "store::context_read".to_string(),
        mask(
            &json!(schema::memory_context_for_skill(
                &json!({"memoryPolicy": {"read": true}}),
                "updated",
                "",
                root,
                &clock
            )),
            &minted,
        ),
    );

    out.insert(
        "store::delete_by_id".to_string(),
        json!(schema::delete_memory(&edited_id, root, &clock).unwrap_or(-1)),
    );
    out.insert(
        "store::delete_again".to_string(),
        json!(schema::delete_memory(&edited_id, root, &clock).unwrap_or(-1)),
    );
    out.insert(
        "store::delete_blank".to_string(),
        json!(schema::delete_memory("", root, &clock).unwrap_or(-1)),
    );

    let before = legacy::load_memories(root).len();
    match schema::add_memory(
        "my api key is sk-abcdefghijklmnopqrstuvwxyz",
        "global",
        "fact",
        "",
        "",
        "",
        None,
        0.9,
        "",
        false,
        root,
        &clock,
    ) {
        Ok(_) => out.insert(
            "store::add_sensitive".to_string(),
            json!({"unexpected": true}),
        ),
        Err(error) => out.insert(
            "store::add_sensitive".to_string(),
            json!({
                "code": error.code,
                "unchanged": legacy::load_memories(root).len() == before,
            }),
        ),
    };
}
