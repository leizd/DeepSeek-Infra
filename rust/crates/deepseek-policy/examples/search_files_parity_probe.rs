//! search_files parity probe, Rust side.
//!
//! Replays the same corpus as
//! `tasks/native-runtime/search_files_parity_probe.py`.
//!
//! Usage::
//!
//!     python tasks/native-runtime/search_files_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example search_files_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use std::fs;
use std::path::{Path, PathBuf};

use deepseek_policy::app_error::AppError;
use deepseek_policy::search_files::{compact_snippet, search_files};
use serde_json::{Map, Value, json};

fn error_view(error: &AppError) -> Value {
    json!({"error": error.message, "code": error.code, "status": error.status})
}

fn write_cache(root: &Path, relative: &str, body: &str) {
    let path = root.join(relative);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, body).unwrap();
}

fn search_result(query: &str, limit: usize, root: &Path) -> Value {
    match search_files(query, limit, root) {
        Ok(value) => value,
        Err(error) => error_view(&error),
    }
}

fn main() {
    let mut out = Map::new();
    let exact_limit = "a".repeat(700);
    let long_a = "a".repeat(2000);
    let window_needle = "prefix ".repeat(40) + "NEEDLE" + &" tail".repeat(40);
    let cjk = "甲".repeat(80) + "关键词" + &"乙".repeat(80);
    let snippet_cases = [
        ("short", "  hello   world  ", "hello", 700usize),
        ("exact-limit", exact_limit.as_str(), "a", 700),
        ("long-a", long_a.as_str(), "a", 700),
        ("window-needle", window_needle.as_str(), "needle", 40),
        ("cjk", cjk.as_str(), "关键词", 20),
        ("empty-text", "", "q", 700),
    ];
    for (label, text, query, limit) in snippet_cases {
        out.insert(
            format!("snippet::{label}"),
            json!(compact_snippet(text, query, limit)),
        );
    }

    let scratch: PathBuf =
        std::env::temp_dir().join(format!("search-files-parity-{}", std::process::id()));
    let _ = fs::remove_dir_all(&scratch);
    fs::create_dir_all(&scratch).unwrap();

    out.insert("empty".to_string(), search_result("", 5, &scratch));
    out.insert("blank".to_string(), search_result("   ", 5, &scratch));

    write_cache(
        &scratch,
        ".file-cache/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
        r#"{"id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"react-notes.txt","kind":"text","chunks":[{"index":0,"text":"useMemo 可以缓存计算结果","lineStart":1,"lineEnd":1}]}"#,
    );
    write_cache(
        &scratch,
        ".projects/proj_1/files/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.json",
        r#"{"id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","name":"project-guide.txt","kind":"text","chunks":[{"index":0,"text":"项目空间里的 useMemo 笔记","lineStart":2,"lineEnd":3}]}"#,
    );
    write_cache(&scratch, ".file-cache/not-json.json", "not json");
    write_cache(&scratch, ".file-cache/array.json", "[]");
    write_cache(
        &scratch,
        ".projects/proj/files/cccccccccccccccccccccccccccccccc.json",
        r#"{"id":"file","name":"notes","kind":"text","chunks":[null,{"index":0,"text":"needle text","lineStart":1,"lineEnd":2}]}"#,
    );

    out.insert(
        "two-indexes".to_string(),
        search_result("useMemo", 10, &scratch),
    );
    out.insert("needle".to_string(), search_result("needle", 2, &scratch));
    out.insert(
        "no-hit".to_string(),
        search_result("zzzz-not-present", 5, &scratch),
    );

    let _ = fs::remove_dir_all(&scratch);
    let mut encoded = serde_json::to_string_pretty(&Value::Object(out)).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}
