//! Workspace Core schema parity probe, Rust side.
//!
//! Replays the same corpus as
//! `tasks/native-runtime/workspace_schema_parity_probe.py` through
//! `deepseek_policy::workspace_schema` and prints canonical JSON.
//!
//! Minted ids are reported by shape (prefix + `<hex16>`) on both sides, so two runs
//! compare equal.
//!
//! Usage::
//!
//!     $env:PYTHONIOENCODING = "utf-8"; $env:PYTHONUTF8 = "1"
//!     python tasks/native-runtime/workspace_schema_parity_probe.py > python.json
//!     cd rust; cargo run -p deepseek-policy --example workspace_schema_parity_probe > ../rust.json

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use deepseek_policy::entropy::SystemEntropy;
use deepseek_policy::workspace_schema as schema;
use serde_json::{Value, json};

fn title_cases() -> Vec<Value> {
    vec![
        json!("  a   b  "),
        json!(""),
        json!("   "),
        Value::Null,
        json!("a\r\nb"),
        json!("z".repeat(200)),
        json!("z".repeat(160)),
        json!(5),
        json!(true),
        json!(["x"]),
        json!({"a": 1}),
        json!("中文  标题"),
    ]
}

fn content_cases() -> Vec<Value> {
    vec![
        json!("  a\r\nb  "),
        json!("a\nb"),
        json!("a\rb"),
        json!(""),
        Value::Null,
        json!("z".repeat(200_001)),
        json!("z".repeat(200_000)),
        json!("  spaced  "),
        json!(5),
        json!(["x"]),
    ]
}

fn tags_cases() -> Vec<Value> {
    let many: Vec<String> = (0..40).map(|index| format!("t{index}")).collect();
    vec![
        json!(["Rust", "rust", " RUST ", "Go"]),
        json!(["", "  ", "ok"]),
        json!("not-a-list"),
        Value::Null,
        json!([1, 2, 3]),
        json!([null, "a"]),
        json!(["a".repeat(50)]),
        json!(["a".repeat(40)]),
        json!(many),
        json!(["中文", "中文"]),
        json!([]),
    ]
}

fn source_ref_cases() -> Vec<Value> {
    vec![
        json!({}),
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
        json!("not-a-dict"),
        Value::Null,
        json!([1, 2]),
    ]
}

fn saved_type_cases() -> Vec<Value> {
    vec![
        json!("chat_snippet"),
        json!(" Chat_Snippet "),
        json!("nonsense"),
        json!(""),
        Value::Null,
        json!(5),
    ]
}

fn saved_purpose_cases() -> Vec<Value> {
    vec![
        json!("reference"),
        json!("export_fragment"),
        json!(" MEMORY_CANDIDATE "),
        json!("nonsense"),
        json!(""),
        Value::Null,
    ]
}

fn artifact_type_cases() -> Vec<(Value, &'static str)> {
    vec![
        (json!("md"), ""),
        (json!(".svg"), ""),
        (json!("PPTX"), ""),
        (Value::Null, "a/b.md"),
        (Value::Null, "a/b.svg"),
        (Value::Null, "a/b.MD"),
        (Value::Null, "a/b"),
        (Value::Null, ""),
        (json!(""), "a/b.json"),
        (json!("nonsense"), "a.md"),
        (json!("markdown"), ""),
        (json!(5), ""),
    ]
}

fn export_format_cases() -> Vec<Value> {
    vec![
        Value::Null,
        json!(""),
        json!("md"),
        json!(".JSON"),
        json!("zip"),
        json!("nonsense"),
        json!(5),
    ]
}

fn timestamp_cases() -> Vec<Value> {
    vec![
        json!(0),
        json!(-1),
        json!(1_500),
        json!(1_789_815_737_000_i64),
        json!("1789815737000"),
        json!("abc"),
        json!(""),
        Value::Null,
        json!(true),
        json!(false),
        json!(1.9),
    ]
}

fn path_cases() -> Vec<&'static str> {
    vec![
        "a/b.md",
        "../etc/passwd",
        "a/../../etc",
        "",
        ".",
        "a\\b.md",
        "/etc/passwd",
        "a/./b",
        "a//b",
        "..",
    ]
}

fn safe_filename_cases() -> Vec<String> {
    vec![
        "my file!.txt".to_string(),
        "...".to_string(),
        String::new(),
        "中文文档".to_string(),
        "a".repeat(200),
        "a/b".to_string(),
        "  spaced  ".to_string(),
    ]
}

fn redact_cases() -> Vec<&'static str> {
    vec![
        "Authorization: Bearer abcdefghijklmnop",
        "key sk-abcdefghijklmno here",
        "see ?api_key=supersecret&x=1",
        "api_key=supersecret",
        "password=abc",
        "nothing sensitive here",
        "Bearer abcdefghijklmnop",
        "token=abcdefgh",
        "refresh_token=abcdefgh&y=2",
    ]
}

fn redact_value_cases() -> Vec<Value> {
    vec![
        json!({"apiKey": {"nested": "secret"}, "name": "ok"}),
        json!({"list": ["sk-abcdefghijklmno"]}),
        json!({"cookie": "x", "Authorization": "y"}),
        json!("sk-abcdefghijklmno"),
        json!([{"password": "p"}]),
        Value::Null,
        json!(5),
    ]
}

fn contains_secret_cases() -> Vec<&'static str> {
    vec![
        "sk-abcdefghijklmno",
        "Bearer abcdefghijklmnop",
        "password=supersecret",
        "?token=abcdefgh",
        "just a normal document",
        "",
    ]
}

fn id_cases() -> Vec<&'static str> {
    vec!["save", "!!!", "My-ID", "art", "", "_x_", "a b"]
}

fn is_hex16(text: &str) -> bool {
    text.len() == 16
        && text
            .chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase())
}

/// The oracle's `AppError` envelope, or the value on success.
fn outcome<T: serde::Serialize>(result: Result<T, deepseek_policy::app_error::AppError>) -> Value {
    match result {
        Ok(value) => json!({"value": value}),
        Err(error) => json!({"refused": true, "code": error.code}),
    }
}

fn temp_root() -> PathBuf {
    let root = std::env::temp_dir().join(format!("ws-schema-probe-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).expect("create probe root");
    root
}

fn main() {
    let mut out: BTreeMap<String, Value> = BTreeMap::new();

    for (index, value) in title_cases().iter().enumerate() {
        out.insert(
            format!("title::{index}"),
            json!(schema::normalize_title(Some(value), "Untitled")),
        );
    }

    for (index, value) in content_cases().iter().enumerate() {
        out.insert(
            format!("content::{index}"),
            json!(schema::normalize_content(Some(value))),
        );
        out.insert(
            format!("description::{index}"),
            json!(schema::normalize_description(Some(value))),
        );
    }

    for (index, value) in tags_cases().iter().enumerate() {
        out.insert(
            format!("tags::{index}"),
            json!(schema::normalize_tags(Some(value))),
        );
    }

    for (index, value) in source_ref_cases().iter().enumerate() {
        out.insert(
            format!("source_ref::{index}"),
            schema::normalize_source_ref(value),
        );
    }

    for (index, value) in saved_type_cases().iter().enumerate() {
        out.insert(
            format!("saved_type::{index}"),
            outcome(schema::normalize_saved_type(Some(value))),
        );
    }

    for (index, value) in saved_purpose_cases().iter().enumerate() {
        out.insert(
            format!("saved_purpose::{index}"),
            json!(schema::normalize_saved_purpose(Some(value))),
        );
    }

    for (index, (value, path)) in artifact_type_cases().iter().enumerate() {
        out.insert(
            format!("artifact_type::{index}"),
            outcome(schema::normalize_artifact_type(Some(value), path)),
        );
    }

    for (index, value) in export_format_cases().iter().enumerate() {
        out.insert(
            format!("export_format::{index}"),
            outcome(schema::normalize_export_format(Some(value))),
        );
    }

    for (index, value) in timestamp_cases().iter().enumerate() {
        out.insert(
            format!("timestamp::{index}"),
            json!(schema::timestamp_ms_to_iso(Some(value))),
        );
    }

    let root = temp_root();
    let generated = root.join(".generated");
    let projects = root.join(".projects");

    for (index, value) in path_cases().iter().enumerate() {
        let entry = match schema::runtime_relative_path(value, &generated, &projects, &root) {
            Ok(resolved) => json!({"value": resolved}),
            Err(error) => json!({
                "refused": true,
                "code": error.code,
                "message": error.message,
            }),
        };
        out.insert(format!("runtime_path::{index}"), entry);
    }

    for (index, value) in safe_filename_cases().iter().enumerate() {
        out.insert(
            format!("safe_filename::{index}"),
            json!(schema::safe_filename(value, "item")),
        );
    }

    for (index, value) in redact_cases().iter().enumerate() {
        out.insert(
            format!("redact::{index}"),
            json!(schema::redact_sensitive_text(value)),
        );
    }

    for (index, value) in redact_value_cases().iter().enumerate() {
        out.insert(
            format!("redact_value::{index}"),
            schema::redact_value(value, ""),
        );
    }

    for (index, value) in contains_secret_cases().iter().enumerate() {
        out.insert(
            format!("contains_secret::{index}"),
            json!(schema::contains_secret(value.as_bytes())),
        );
    }

    for (index, prefix) in id_cases().iter().enumerate() {
        let generated_id = schema::new_id(prefix, &SystemEntropy).expect("new_id");
        let (head, tail) = match generated_id.rsplit_once('_') {
            Some((head, tail)) => (head.to_string(), tail.to_string()),
            None => (String::new(), generated_id.clone()),
        };
        out.insert(
            format!("new_id::{index}"),
            json!({
                "prefix": head,
                "tail": if is_hex16(&tail) { "<hex16>".to_string() } else { tail },
            }),
        );
    }

    // --- the file helpers, against the real temp root ----------------------------
    let missing = root.join("nope.json");
    out.insert(
        "read_json::missing".to_string(),
        schema::read_json_file(&missing, json!({"items": []})),
    );

    let malformed = root.join("bad.json");
    std::fs::write(&malformed, "not json").unwrap();
    out.insert(
        "read_json::malformed".to_string(),
        schema::read_json_file(&malformed, json!({"items": []})),
    );

    let scalar = root.join("scalar.json");
    std::fs::write(&scalar, "[]").unwrap();
    out.insert(
        "read_json::scalar".to_string(),
        schema::read_json_file(&scalar, json!({"items": []})),
    );

    let good = root.join("good.json");
    std::fs::write(&good, r#"{"items":[1]}"#).unwrap();
    out.insert(
        "read_json::good".to_string(),
        schema::read_json_file(&good, json!({})),
    );

    let target = root.join("project.json");
    schema::write_json_atomic(&root, &target, &json!({"id": "p1", "nested": {"a": 1}}))
        .expect("write_json_atomic");
    out.insert(
        "write_json::text".to_string(),
        json!(std::fs::read_to_string(&target).unwrap()),
    );
    out.insert(
        "write_json::temp_left".to_string(),
        json!(root.join("project.json.tmp").exists()),
    );
    out.insert(
        "write_json::generation".to_string(),
        json!(
            std::fs::read_to_string(root.join(".workspace-generation"))
                .unwrap()
                .trim()
        ),
    );

    // `resolve_runtime_path` needs the configured roots; the probe root is masked to
    // `<root>` so both sides compare the same shape.
    let mask = |path: PathBuf| -> String {
        path.to_string_lossy()
            .replace('\\', "/")
            .replace(&root.to_string_lossy().replace('\\', "/") as &str, "<root>")
    };
    out.insert(
        "resolve::generated".to_string(),
        json!(mask(
            schema::resolve_runtime_path(".generated/a.svg", &generated, &projects, &root)
                .expect("resolve generated")
        )),
    );
    out.insert(
        "resolve::projects".to_string(),
        json!(mask(
            schema::resolve_runtime_path(".projects/p1/x.json", &generated, &projects, &root)
                .expect("resolve projects")
        )),
    );
    out.insert(
        "resolve::other".to_string(),
        json!(mask(
            schema::resolve_runtime_path("other/x", &generated, &projects, &root)
                .expect("resolve other")
        )),
    );

    let _ = Path::new("unused");

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out.into_iter().collect())).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}
