//! file-route helper parity probe, Rust side.
//!
//! Pins the helpers `web/routes/files.py` and the file-reader routes need:
//! `clean_filename`, `content_disposition_header`, `original_file_media_type`,
//! `file_reader_window`, `file_chunk` and `file_page_text`. The routes themselves are
//! pinned by the gateway's own tests; what this probe compares is the value-level
//! detail — the RFC 5987 header, the media-type ladder, the window arithmetic and one
//! page of extracted text — where a port drifts silently.
//!
//! Run against `tasks/native-runtime/file_routes_parity_probe.py`; the two outputs must
//! be byte-identical.
//!
//! ```text
//! python tasks/native-runtime/file_routes_parity_probe.py > python.json
//! cd rust && cargo run -p deepseek-policy --example file_routes_parity_probe > ../rust.json
//! ```
//!
//! Keys come from a `BTreeMap` so the order is sorted whatever `serde_json` was built
//! with; the Python side uses `sort_keys=True` for the same bytes.

use std::collections::BTreeMap;

use deepseek_policy::file_cache::FileCache;
use deepseek_policy::file_routes::{
    clean_filename, content_disposition_header, file_chunk, file_page_search, file_page_text,
    file_reader_window, original_file_media_type,
};
use serde_json::{Value, json};

/// The filenames the two name helpers are compared over.
const FILENAME_CASES: [&str; 26] = [
    "",
    "   ",
    "report.pdf",
    "  report.pdf  ",
    "\"report.pdf\"",
    "\"\"",
    "///",
    "\\\\",
    "a/b.txt",
    "a\\b.txt",
    r"C:\Users\me\report.pdf",
    "/tmp/a/b/report.pdf",
    "a\"b.txt",
    "my report.pdf",
    "报告.pdf",
    "报告",
    "日本語のファイル.txt",
    "éclair.txt",
    "naïve — dash.txt",
    "emoji 🎉.png",
    "with%percent.txt",
    "with+plus.txt",
    "with#hash.txt",
    "with?query=1.txt",
    "with&and.txt",
    ".hidden",
];

/// The `cached` shapes `original_file_media_type` is compared over.
fn media_type_cases() -> Vec<(&'static str, Value)> {
    let ooxml = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
    vec![
        ("empty", json!({})),
        ("pdf_kind", json!({"kind": "pdf"})),
        ("pdf_type", json!({"type": "application/pdf"})),
        (
            "pdf_kind_wins",
            json!({"kind": "pdf", "type": "text/plain"}),
        ),
        ("image_png", json!({"kind": "image", "type": "image/png"})),
        ("image_jpeg", json!({"kind": "image", "type": "image/jpeg"})),
        (
            "image_svg",
            json!({"kind": "image", "type": "image/svg+xml"}),
        ),
        ("svg_without_kind", json!({"type": "image/svg+xml"})),
        ("text_markdown", json!({"type": "text/markdown"})),
        ("text_plain", json!({"type": "text/plain"})),
        (
            "text_with_charset",
            json!({"type": "text/plain; charset=utf-16"}),
        ),
        ("text_html", json!({"type": "text/html"})),
        ("kind_csv", json!({"kind": "csv"})),
        ("kind_md", json!({"kind": "md"})),
        ("kind_TXT_uppercase", json!({"kind": "TXT"})),
        ("kind_unknown", json!({"kind": "zip"})),
        ("ooxml_passthrough", json!({"type": ooxml})),
        ("ooxml_with_kind", json!({"kind": "docx", "type": ooxml})),
        (
            "image_kind_bad_type",
            json!({"kind": "image", "type": "text/plain"}),
        ),
        ("null_kind", json!({"kind": null, "type": "text/csv"})),
        ("number_type", json!({"type": 5})),
    ]
}

fn main() {
    let mut out: BTreeMap<String, Value> = BTreeMap::new();

    let cleaned: BTreeMap<String, Value> = FILENAME_CASES
        .iter()
        .map(|case| ((*case).to_string(), json!(clean_filename(case))))
        .collect();
    out.insert("clean_filename".to_string(), json!(cleaned));

    let inline: BTreeMap<String, Value> = FILENAME_CASES
        .iter()
        .map(|case| {
            (
                (*case).to_string(),
                json!(content_disposition_header("inline", case)),
            )
        })
        .collect();
    out.insert("disposition_inline".to_string(), json!(inline));
    let attachment: BTreeMap<String, Value> = FILENAME_CASES
        .iter()
        .map(|case| {
            (
                (*case).to_string(),
                json!(content_disposition_header("attachment", case)),
            )
        })
        .collect();
    out.insert("disposition_attachment".to_string(), json!(attachment));

    let media: BTreeMap<String, Value> = media_type_cases()
        .into_iter()
        .map(|(label, cached)| (label.to_string(), json!(original_file_media_type(&cached))))
        .collect();
    out.insert("media_types".to_string(), json!(media));

    // A long name, built rather than literal, so the cap is exercised at 179/180/181.
    let long: BTreeMap<String, Value> = [179usize, 180, 181, 400]
        .into_iter()
        .map(|length| {
            let name = "a".repeat(length);
            (
                length.to_string(),
                json!({
                    "cleaned": clean_filename(&name),
                    "cleaned_len": clean_filename(&name).chars().count(),
                    "disposition": content_disposition_header("inline", &name),
                }),
            )
        })
        .collect();
    out.insert("long_names".to_string(), json!(long));

    // --- the reader window -----------------------------------------------------------------
    //
    // One cached index per case, written to a temporary root; the Python side writes the
    // same indexes under the same ids and points its own cache directory at them.
    let root = std::env::temp_dir().join(format!("file-routes-probe-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    let directory = deepseek_policy::file_cache::file_cache_dir(&root);
    std::fs::create_dir_all(&directory).expect("cache dir");
    let cache = FileCache::new();

    let mut windows: BTreeMap<String, Value> = BTreeMap::new();
    let mut chunks: BTreeMap<String, Value> = BTreeMap::new();
    for (label, index) in reader_indexes() {
        let file_id = format!("{:032x}", label.len());
        std::fs::write(directory.join(format!("{file_id}.json")), index.to_string())
            .expect("write index");
        for (start, count) in READER_WINDOWS {
            let label_of =
                |value: &Option<i64>| value.map_or("none".to_string(), |number| number.to_string());
            let key = format!("{label}|{}|{}", label_of(start), label_of(count));
            let value = match (*start, *count) {
                (None, None) => file_reader_window(&root, &file_id, None, None, None, &cache),
                (Some(start), None) => {
                    file_reader_window(&root, &file_id, None, Some(&json!(start)), None, &cache)
                }
                (None, Some(count)) => {
                    file_reader_window(&root, &file_id, None, None, Some(&json!(count)), &cache)
                }
                (Some(start), Some(count)) => file_reader_window(
                    &root,
                    &file_id,
                    None,
                    Some(&json!(start)),
                    Some(&json!(count)),
                    &cache,
                ),
            };
            windows.insert(key, outcome(value));
        }
        for index in [None, Some(0i64), Some(1), Some(2), Some(99), Some(-3)] {
            let key = format!(
                "{label}|{}",
                index.map_or("none".to_string(), |value| value.to_string())
            );
            let value = file_chunk(
                &root,
                &file_id,
                None,
                index.map(|value| json!(value)).as_ref(),
                &cache,
            );
            chunks.insert(key, outcome(value));
        }
    }
    out.insert("reader_windows".to_string(), json!(windows));
    out.insert("reader_chunks".to_string(), json!(chunks));

    let mut pages: BTreeMap<String, Value> = BTreeMap::new();
    for (label, file_id, index) in page_indexes() {
        std::fs::write(directory.join(format!("{file_id}.json")), index.to_string())
            .expect("write page index");
        for (page_label, page) in page_cases() {
            let key = format!("{label}|{page_label}");
            let value = file_page_text(&root, &file_id, None, page.as_ref(), &cache);
            pages.insert(key, outcome(value));
        }
    }
    pages.insert(
        "missing|one".to_string(),
        outcome(file_page_text(
            &root,
            &"9".repeat(32),
            None,
            Some(&json!(1)),
            &cache,
        )),
    );
    pages.insert(
        "bad-id|one".to_string(),
        outcome(file_page_text(
            &root,
            "../escape",
            None,
            Some(&json!(1)),
            &cache,
        )),
    );
    out.insert("page_texts".to_string(), json!(pages));

    let mut searches: BTreeMap<String, Value> = BTreeMap::new();
    for (label, file_id, index) in search_indexes() {
        std::fs::write(directory.join(format!("{file_id}.json")), index.to_string())
            .expect("write search index");
        for (query_label, query) in search_queries() {
            let key = format!("{label}|{query_label}");
            let query_value = json!(query);
            let value = file_page_search(&root, &file_id, None, Some(&query_value), &cache);
            searches.insert(key, outcome(value));
        }
    }
    out.insert("page_search".to_string(), json!(searches));
    let _ = std::fs::remove_dir_all(&root);

    let mut encoded = serde_json::to_string_pretty(&out).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}

/// `(chunkStart, chunkCount)` pairs, including the defaults and the falsy spellings.
const READER_WINDOWS: &[(Option<i64>, Option<i64>)] = &[
    (None, None),
    (Some(1), Some(2)),
    (Some(2), Some(2)),
    (Some(0), Some(3)),
    (Some(99), Some(2)),
    (Some(1), Some(99)),
    (Some(0), Some(0)),
    (Some(-5), Some(-1)),
];

/// The cached indexes the reader is compared over.
fn reader_indexes() -> Vec<(&'static str, Value)> {
    let chunk = |index: usize| {
        json!({
            "index": index,
            "start": index * 10,
            "end": index * 10 + 9,
            "lineStart": index + 1,
            "lineEnd": index + 1,
            "text": format!("chunk {index}"),
        })
    };
    vec![
        ("empty", json!({"name": "empty.txt", "chunks": []})),
        (
            "five",
            json!({"name": "a.txt", "kind": "txt", "size": 42, "chunks":
                (0..5).map(chunk).collect::<Vec<Value>>()}),
        ),
        (
            "twenty",
            json!({"name": "b.txt", "kind": "txt", "charCount": 90, "chunkCount": 20,
                "chunks": (0..20).map(chunk).collect::<Vec<Value>>()}),
        ),
        (
            "malformed",
            json!({"name": "c.txt", "chunks": [
                {"index": 0, "text": "first"},
                "not an object",
                {"text": "third", "index": "2"},
            ]}),
        ),
        (
            "string_index",
            json!({"name": "d.txt", "chunks": [{"index": "7", "text": "seven"}]}),
        ),
        ("no_name", json!({"chunks": [{"index": 0, "text": "x"}]})),
        (
            "source_available",
            json!({"name": "e.txt", "kind": "pdf", "type": "application/pdf",
                "sourceAvailable": true, "pageCount": 3, "chunks": [{"index": 0, "text": "x"}]}),
        ),
    ]
}

fn search_queries() -> Vec<(&'static str, String)> {
    vec![
        ("page", "page".to_string()),
        ("PAGE", "PAGE".to_string()),
        ("padded", "  page  ".to_string()),
        ("blank", "   ".to_string()),
        ("missing-word", "nope".to_string()),
        ("eszett", "strasse".to_string()),
        ("long", "q".repeat(201)),
    ]
}

fn search_indexes() -> Vec<(&'static str, String, Value)> {
    vec![
        (
            "pages",
            "4".repeat(32),
            json!({
                "name": "a.pdf",
                "kind": "pdf",
                "pageCount": 1,
                "pageTexts": [
                    {"page": 1, "text": "See Straße and Page"},
                    {"page": 5, "text": "page five"},
                ],
                "chunks": [],
            }),
        ),
        (
            "chunks",
            "5".repeat(32),
            json!({
                "name": "c.txt",
                "pageCount": 2,
                "chunks": [{"text": "alpha BETA"}, {"text": "gamma"}],
            }),
        ),
    ]
}

/// `(label, page)` pairs. `None` is the missing page, which takes the default.
fn page_cases() -> Vec<(&'static str, Option<Value>)> {
    vec![
        ("none", None),
        ("one", Some(json!(1))),
        ("two", Some(json!(2))),
        ("three", Some(json!(3))),
        ("five", Some(json!(5))),
        ("ninety-nine", Some(json!(99))),
        ("zero", Some(json!(0))),
        ("negative", Some(json!(-4))),
        ("empty", Some(json!(""))),
        ("bad", Some(json!("x"))),
        ("float-string", Some(json!("1.5"))),
        ("padded", Some(json!(" 4 "))),
        ("underscore", Some(json!("1_0"))),
        ("bool-true", Some(json!(true))),
        ("bool-false", Some(json!(false))),
        ("float", Some(json!(2.5))),
    ]
}

/// The cached indexes `file_page_text` is compared over. Ids match the Python probe.
fn page_indexes() -> Vec<(&'static str, String, Value)> {
    let long_text = "a".repeat(40_001);
    vec![
        (
            "raised",
            "a".repeat(32),
            json!({
                "name": "a.pdf",
                "kind": "pdf",
                "type": "application/pdf",
                "size": 120,
                "charCount": 18,
                "pageCount": 2,
                "sourceAvailable": true,
                "pageTexts": [
                    {"page": 1, "text": "page one"},
                    {"page": 5, "text": "page five"},
                ],
                "chunks": [{"index": 0, "text": "chunk text"}],
            }),
        ),
        (
            "chunks",
            "b".repeat(32),
            json!({
                "name": "split.txt",
                "pageCount": 4,
                "chunks": [{"text": "aaaa"}, {"text": "bbbb"}, {"text": "cccc"}],
            }),
        ),
        (
            "empty",
            "c".repeat(32),
            json!({"name": "empty.txt", "chunks": []}),
        ),
        (
            "malformed",
            "d".repeat(32),
            json!({
                "name": "m.pdf",
                "kind": "pdf",
                "pageCount": 1,
                "pageTexts": [
                    {"page": 1, "text": "one"},
                    {"page": 0, "text": "zero"},
                    {"page": 2.5, "text": "two-and-a-half"},
                    {"page": true, "text": "from-bool"},
                    {"page": "4", "text": "four"},
                    {"page": "x", "text": "bad"},
                    {"page": "1.5", "text": "float-string"},
                    {"page": 3, "text": "   \r\n  "},
                    "not an object",
                    {"page": 6, "text": 5},
                    {"text": "no page"},
                ],
                "chunks": [{"text": "fallback"}],
            }),
        ),
        (
            "crlf",
            "e".repeat(32),
            json!({
                "name": "lines.txt",
                "pageCount": 1,
                "pageTexts": [{"page": 1, "text": "  a  \r\nb\u{0}c  \n"}],
                "chunks": [],
            }),
        ),
        (
            "cjk",
            "1".repeat(32),
            json!({
                "name": "页.pdf",
                "kind": "pdf",
                "pageCount": 1,
                "pageTexts": [{"page": 1, "text": "第一页"}],
                "chunks": [],
            }),
        ),
        (
            "capped",
            "2".repeat(32),
            json!({
                "name": "long.txt",
                "pageCount": 1,
                "pageTexts": [{"page": 1, "text": long_text}],
                "chunks": [],
            }),
        ),
        (
            "noname",
            "3".repeat(32),
            json!({"pageCount": 1, "chunks": [{"text": "only"}]}),
        ),
    ]
}

/// A `Result` as a value, so an error's code and status are compared too.
fn outcome(result: Result<Value, deepseek_policy::app_error::AppError>) -> Value {
    match result {
        Ok(value) => json!({"ok": value}),
        Err(error) => {
            json!({"error": {"message": error.message, "code": error.code, "status": error.status}})
        }
    }
}
