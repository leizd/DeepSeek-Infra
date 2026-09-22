//! file-route helper parity probe, Rust side.
//!
//! Pins the helpers `web/routes/files.py` and the file-reader routes need:
//! `clean_filename`, `content_disposition_header`, `original_file_media_type`,
//! `file_reader_window` and `file_chunk`. The routes themselves are pinned by the
//! gateway's own tests; what this probe compares is the byte-level detail — the RFC 5987
//! header, the media-type ladder and the window arithmetic — where a port drifts
//! silently.
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
    clean_filename, content_disposition_header, file_chunk, file_reader_window,
    original_file_media_type,
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

/// A `Result` as a value, so an error's code and status are compared too.
fn outcome(result: Result<Value, deepseek_policy::app_error::AppError>) -> Value {
    match result {
        Ok(value) => json!({"ok": value}),
        Err(error) => {
            json!({"error": {"message": error.message, "code": error.code, "status": error.status}})
        }
    }
}
