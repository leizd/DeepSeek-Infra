//! Text-upload parity probe. Compared with
//! `tasks/native-runtime/file_text_parity_probe.py`, which calls the unmodified
//! `extract_uploaded_file`. Chunk vectors are omitted: the oracle's embedding
//! pipeline may be the hash fallback or an ONNX model, and the public response
//! does not include them.

use std::collections::BTreeMap;

use deepseek_policy::file_upload::extract_uploaded_file;
use serde_json::{Value, json};

fn main() {
    let root = std::env::temp_dir().join(format!("file-text-probe-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(deepseek_policy::file_cache::file_cache_dir(&root)).expect("cache");
    let mut out = BTreeMap::new();
    for (label, filename, content_type, data) in cases() {
        let result = extract_uploaded_file(&root, filename, content_type, data, false, None);
        out.insert(label.to_string(), outcome(&root, result));
    }
    if let Ok(path) = std::env::var("FILE_TEXT_DOCX_DIR") {
        for (label, filename) in [
            ("docx", "sample.docx"),
            ("docx-empty", "empty.docx"),
            ("docx-bad", "bad.docx"),
            ("docx-xml", "broken.docx"),
            ("pptx", "sample.pptx"),
            ("pptx-empty", "empty.pptx"),
            ("pptx-bad", "bad.pptx"),
            ("pptx-xml", "broken.pptx"),
            ("xlsx", "sample.xlsx"),
            ("xlsx-empty", "empty.xlsx"),
            ("xlsx-bad", "bad.xlsx"),
            ("xlsx-xml", "broken.xlsx"),
            ("xlsx-nocontent", "nocontent.xlsx"),
            ("xlsx-shared", "shared.xlsx"),
            ("pdf", "sample.pdf"),
            ("pdf-blank", "blank.pdf"),
            ("pdf-bad", "bad.pdf"),
            ("pdf-header", "header.pdf"),
            ("pdf-drawn", "drawn.pdf"),
            ("epub", "sample.epub"),
            ("epub-empty", "empty.epub"),
            ("epub-bad", "bad.epub"),
            ("ocr-image", "ocr-hello.png"),
            ("ocr-pdf", "ocr-scan.pdf"),
        ] {
            let data =
                std::fs::read(std::path::Path::new(&path).join(filename)).expect("office fixture");
            let content_type = if filename.ends_with(".pptx") {
                "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            } else if filename.ends_with(".xlsx") {
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            } else if filename.ends_with(".pdf") {
                "application/pdf"
            } else if filename.ends_with(".epub") {
                "application/epub+zip"
            } else if filename.ends_with(".png") {
                "image/png"
            } else {
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            };
            let ocr = filename.starts_with("ocr-");
            let result = extract_uploaded_file(&root, filename, content_type, &data, ocr, None);
            out.insert(label.to_string(), outcome(&root, result));
        }
    }
    let _ = std::fs::remove_dir_all(&root);
    let mut encoded = serde_json::to_string_pretty(&out).expect("json");
    encoded.push('\n');
    print!("{encoded}");
}

fn cases() -> Vec<(&'static str, &'static str, &'static str, &'static [u8])> {
    vec![
        ("hello", "notes.txt", "text/plain", b"hello\nworld"),
        (
            "html",
            "page.html",
            "text/html",
            b"<p>Hello</p><script>secret()</script>",
        ),
        ("cjk", "页.txt", "text/plain", "第一页\n第二页".as_bytes()),
        ("empty", "empty.txt", "text/plain", b""),
        ("blank", "blank.txt", "text/plain", b" \n\t "),
        ("binary", "blob.bin", "application/octet-stream", b"a\0b"),
        ("image", "scan.png", "image/png", b"\x89PNG"),
    ]
}

fn outcome(
    root: &std::path::Path,
    result: Result<Value, deepseek_policy::app_error::AppError>,
) -> Value {
    match result {
        Ok(value) => {
            let file_id = value.get("fileId").and_then(Value::as_str).unwrap_or("");
            let chunks = read_chunks(root, file_id);
            json!({"ok": value, "chunks": chunks})
        }
        Err(error) => {
            json!({"error": {"message": error.message, "code": error.code, "status": error.status}})
        }
    }
}

fn read_chunks(root: &std::path::Path, file_id: &str) -> Value {
    let path = deepseek_policy::file_cache::file_cache_dir(root).join(format!("{file_id}.json"));
    let Ok(raw) = std::fs::read_to_string(path) else {
        return Value::Null;
    };
    let Ok(value) = serde_json::from_str::<Value>(&raw) else {
        return Value::Null;
    };
    let Some(chunks) = value.get("chunks").and_then(Value::as_array) else {
        return Value::Null;
    };
    Value::Array(
        chunks
            .iter()
            .map(|chunk| {
                json!({
                    "index": chunk.get("index").cloned().unwrap_or(Value::Null),
                    "start": chunk.get("start").cloned().unwrap_or(Value::Null),
                    "end": chunk.get("end").cloned().unwrap_or(Value::Null),
                    "lineStart": chunk.get("lineStart").cloned().unwrap_or(Value::Null),
                    "lineEnd": chunk.get("lineEnd").cloned().unwrap_or(Value::Null),
                    "text": chunk.get("text").cloned().unwrap_or(Value::Null),
                })
            })
            .collect(),
    )
}
