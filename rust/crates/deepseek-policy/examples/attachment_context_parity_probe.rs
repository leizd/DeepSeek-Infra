//! Attachment-expansion parity probe, Rust side.
//!
//! Replays the same corpus as `tasks/native-runtime/attachment_context_parity_probe.py`. The
//! three non-pure dependencies are stubbed here the same way the Python probe stubs them — a
//! fixed file index, a fixed vector-search result, and the hash embedding — so the two sides
//! compare behaviour rather than I/O.
//!
//! `guard-line::default` cross-checks the ported taint module: the Python side reads its own
//! module-level settings, this side reads `ContextTaintSettings::default()`, and the two
//! header rows have to agree for the attachment header to agree.
//!
//! `summary` renders the window through serde_json rather than `Debug`, because Python's
//! `repr` and Rust's `Debug` disagree on the quote character.
//!
//! Usage::
//!
//!     python tasks/native-runtime/attachment_context_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example attachment_context_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use serde_json::{Map, Value, json};

use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::attachment_context::{
    FILE_CONTEXT_CHAR_BUDGET, FileContextDeps, LOCAL_RAG_EMBEDDING_DIMENSIONS,
    build_attachment_context, cosine_similarity, expanded_message_content,
    format_cached_file_context, format_chunk_locator, hash_text_embedding, hybrid_chunk_score,
    is_broad_file_query, normalize_vector, select_file_chunk_indices,
};
use deepseek_policy::context_taint::{ContextTaintSettings, file_context_guard_line};
use deepseek_policy::core_utils::query_tokens;
use deepseek_policy::python_json::OrderedJson;

const GUARD_LINE: &str = "[防注入隔离] 探针锚点";

const BROAD_QUERIES: [&str; 8] = [
    "",
    "全文总结",
    "python 报错怎么修",
    "SUMMARIZE this",
    "附件",
    "随便聊聊",
    "Outline",
    "这份文档的知识点",
];

fn file_id() -> String {
    "f".repeat(32)
}

fn corpus_text() -> String {
    "x".repeat(5_600)
}

fn huge_text() -> String {
    "x".repeat(120_000)
}

fn embed_texts() -> Vec<String> {
    vec![
        String::new(),
        "a".to_string(),
        "abc".to_string(),
        "中文内容".to_string(),
        "Mixed Case Text".to_string(),
        "a".repeat(300),
        "关键词 关键词 关键词".to_string(),
        "Hello, World! 123".to_string(),
    ]
}

fn normalize_cases() -> Vec<Value> {
    vec![
        json!([]),
        json!([1.0, 2.0, 2.0]),
        json!([0.0, 0.0]),
        json!(["x", null, 3]),
        json!((0..70).collect::<Vec<i64>>()),
        json!([1e-9, -1e-9, 5]),
    ]
}

fn cosine_cases() -> Vec<(Value, Value)> {
    vec![
        (json!([]), json!([])),
        (json!([1.0]), json!([])),
        (json!([1.0, 0.0]), json!([1.0, 0.0])),
        (json!([1.0, 2.0]), json!([2.0, 1.0])),
        (json!([1.0, 1.0, 1.0]), json!([1.0, 1.0])),
        (json!(["x", 2.0]), json!([1.0, 1.0])),
        (json!([100.0]), json!([100.0])),
        (json!([-1.0]), json!([1.0])),
        (json!(["nan"]), json!([1.0])),
        (json!([null, 1.0]), json!([2.0, 1.0])),
    ]
}

fn small_chunks() -> Vec<Value> {
    vec![
        json!({"text": "第一段", "index": 0, "start": 0, "end": 3}),
        json!({"text": "second chunk", "index": 1, "start": 3, "end": 15}),
        json!({"text": "第三段内容", "index": 2, "start": 15, "end": 20}),
    ]
}

fn big_chunks() -> Vec<Value> {
    (0..12_i64)
        .map(|index| {
            let filler = if index % 3 == 0 {
                "关键词"
            } else {
                "填充"
            };
            json!({
                "text": format!("chunk {index} {filler}{}", "x".repeat(5_600)),
                "index": index,
                "start": index * 6_000,
                "end": (index + 1) * 6_000,
                "lineStart": index * 10 + 1,
                "lineEnd": (index + 1) * 10,
            })
        })
        .collect()
}

fn big_chunks_vectored() -> Vec<Value> {
    big_chunks()
        .into_iter()
        .enumerate()
        .map(|(index, chunk)| {
            if index % 4 != 0 {
                return chunk;
            }
            let mut object = chunk.as_object().cloned().unwrap_or_default();
            object.insert("vector".to_string(), json!(vec![0.5_f64; 64]));
            Value::Object(object)
        })
        .collect()
}

fn locator_cases() -> Vec<(Value, i64, usize, i64, i64)> {
    vec![
        (json!({"lineStart": 3, "lineEnd": 9}), 1, 4, 0, 100),
        (json!({"lineStart": 0, "lineEnd": 0}), 2, 4, 10, 20),
        (json!({"lineStart": 5, "lineEnd": 4}), 3, 4, 20, 30),
        (json!({}), 1, 1, 0, 5),
    ]
}

/// The file index the injected reader serves: one report with three chunks (one empty) and
/// one huge single-chunk text file.
fn cached_files() -> Map<String, Value> {
    let mut files = Map::new();
    files.insert(
        file_id(),
        json!({
            "id": file_id(),
            "name": "报告.pdf",
            "kind": "pdf",
            "charCount": 5_600 * 3,
            "projectId": "",
            "chunks": [
                {"text": corpus_text(), "index": 0, "start": 0, "end": 5_600,
                 "lineStart": 1, "lineEnd": 100},
                {"text": format!("第二块 {}", corpus_text()), "index": 1, "start": 5_600, "end": 11_200},
                {"text": "", "index": 2, "start": 11_200, "end": 11_200},
            ],
        }),
    );
    let huge = huge_text();
    files.insert(
        "g".repeat(32),
        json!({
            "id": "g".repeat(32),
            "name": "huge.txt",
            "kind": "text",
            "charCount": huge.chars().count(),
            "chunks": [{"text": huge, "index": 0, "start": 0, "end": huge_text().chars().count()}],
        }),
    );
    files
}

/// `file_id -> indices` for the injected vector search; anything else finds nothing.
fn indexed() -> Map<String, Value> {
    let mut index = Map::new();
    index.insert(file_id(), json!([5]));
    index
}

fn summary(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let head: String = chars.iter().take(60).collect();
    let tail: String = if chars.len() > 60 {
        chars[chars.len() - 60..].iter().collect()
    } else {
        text.to_string()
    };
    format!(
        "len={}|head={}|tail={}",
        chars.len(),
        Value::String(head),
        Value::String(tail)
    )
}

fn main() {
    let mut out: Map<String, Value> = Map::new();

    let files = cached_files();
    let index = indexed();
    let load = |file: &str, _project_id: Option<&str>| -> Result<Value, AppError> {
        if file == "0".repeat(32) {
            return Err(AppError {
                message: "Uploaded file index has expired or is missing".to_string(),
                code: codes::FILE_INDEX_EXPIRED,
                status: 410,
            });
        }
        files.get(file).cloned().ok_or(AppError {
            message: "Uploaded file index is invalid".to_string(),
            code: codes::INTERNAL,
            status: 500,
        })
    };
    let search = |file: &str, _project: &str, _query: &str, limit: usize| -> Vec<i64> {
        match index.get(file) {
            Some(Value::Array(items)) => {
                items.iter().take(limit).filter_map(Value::as_i64).collect()
            }
            _ => Vec::new(),
        }
    };
    let embed = |text: &str| hash_text_embedding(text, LOCAL_RAG_EMBEDDING_DIMENSIONS);
    let deps = FileContextDeps {
        load_cached_file: &load,
        search_file_chunks: &search,
        embed: &embed,
        guard_line: GUARD_LINE.to_string(),
    };
    let no_guard = FileContextDeps {
        load_cached_file: &load,
        search_file_chunks: &search,
        embed: &embed,
        guard_line: String::new(),
    };

    out.insert(
        "guard-line::default".to_string(),
        json!(file_context_guard_line(&ContextTaintSettings::default())),
    );
    out.insert("guard-line::patched".to_string(), json!(GUARD_LINE));

    for (index, text) in embed_texts().iter().enumerate() {
        out.insert(
            format!("embed::{index}"),
            json!(hash_text_embedding(text, LOCAL_RAG_EMBEDDING_DIMENSIONS)),
        );
    }
    for (index, vector) in normalize_cases().iter().enumerate() {
        let items = vector.as_array().cloned().unwrap_or_default();
        out.insert(
            format!("normalize::{index}"),
            json!(normalize_vector(&items, LOCAL_RAG_EMBEDDING_DIMENSIONS)),
        );
    }
    for (index, (left, right)) in cosine_cases().iter().enumerate() {
        let left = left.as_array().cloned().unwrap_or_default();
        let right = right.as_array().cloned().unwrap_or_default();
        out.insert(
            format!("cosine::{index}"),
            json!(cosine_similarity(&left, &right)),
        );
    }
    for (index, query) in BROAD_QUERIES.iter().enumerate() {
        out.insert(format!("broad::{index}"), json!(is_broad_file_query(query)));
    }

    let tokens = query_tokens("python 报错");
    for (index, chunk) in big_chunks_vectored().iter().take(4).enumerate() {
        let text = chunk
            .get("text")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string();
        out.insert(
            format!("score::{index}"),
            json!(hybrid_chunk_score(
                chunk,
                &text,
                &tokens,
                "python 报错",
                &embed
            )),
        );
    }

    let select_cases: Vec<(&str, Vec<Value>, &str, i64, String)> = vec![
        ("empty", Vec::new(), "python 报错", 115_000, String::new()),
        (
            "small",
            small_chunks(),
            "python 报错",
            115_000,
            String::new(),
        ),
        (
            "big-scored",
            big_chunks_vectored(),
            "python 报错 chunk 7",
            115_000,
            String::new(),
        ),
        (
            "big-broad",
            big_chunks_vectored(),
            "全文总结",
            115_000,
            String::new(),
        ),
        (
            "big-unmatched",
            big_chunks_vectored(),
            "zzz 不存在的词",
            115_000,
            String::new(),
        ),
        (
            "big-tight-budget",
            big_chunks_vectored(),
            "python 报错 chunk 7",
            9_000,
            String::new(),
        ),
        (
            "indexed",
            big_chunks_vectored(),
            "python 报错",
            115_000,
            file_id(),
        ),
        (
            "indexed-only",
            big_chunks_vectored(),
            "zzz 不存在的词",
            115_000,
            file_id(),
        ),
        (
            "indexed-tight",
            big_chunks_vectored(),
            "python 报错",
            9_000,
            file_id(),
        ),
    ];
    for (label, chunks, query, budget, file) in &select_cases {
        out.insert(
            format!("select::{label}"),
            json!(select_file_chunk_indices(
                chunks, query, *budget, file, "", &deps
            )),
        );
    }

    for (index, (chunk, chunk_index, total, start, end)) in locator_cases().iter().enumerate() {
        out.insert(
            format!("locator::{index}"),
            json!(format_chunk_locator(
                chunk,
                *chunk_index,
                *total,
                *start,
                *end
            )),
        );
    }

    let report = files.get(&file_id()).cloned().unwrap_or(Value::Null);
    out.insert(
        "format::empty".to_string(),
        json!(format_cached_file_context(
            1,
            &report,
            "报告讲了什么",
            FILE_CONTEXT_CHAR_BUDGET,
            &deps
        )),
    );
    out.insert(
        "format::no-chunks".to_string(),
        json!(format_cached_file_context(
            2,
            &json!({"name": "n"}),
            "q",
            FILE_CONTEXT_CHAR_BUDGET,
            &deps
        )),
    );
    out.insert(
        "format::tight".to_string(),
        json!(format_cached_file_context(
            1,
            &report,
            "报告讲了什么",
            2_000,
            &deps
        )),
    );

    let exhausted: Vec<Value> = (0..20)
        .map(|_| json!({"text": "x".repeat(20_000)}))
        .collect();
    let attachment_cases: Vec<(&str, Vec<Value>, &str)> = vec![
        ("none", Vec::new(), "q"),
        ("non-dict", vec![json!("x"), json!(5)], "q"),
        (
            "index",
            vec![json!({"fileId": file_id(), "name": "报告.pdf"})],
            "报告讲了什么",
        ),
        (
            "index-fails",
            vec![json!({"fileId": "0".repeat(32), "name": "gone"})],
            "q",
        ),
        (
            "index-unnamed",
            vec![json!({"fileId": "0".repeat(32)})],
            "q",
        ),
        (
            "legacy",
            vec![json!({"text": "旧版内容", "name": "n", "kind": "text"})],
            "q",
        ),
        ("legacy-long", vec![json!({"text": huge_text()})], "q"),
        ("budget-exhausted", exhausted, "q"),
        (
            "multi",
            vec![
                json!({"text": "一"}),
                json!({"text": "二"}),
                json!({"fileId": file_id()}),
            ],
            "q",
        ),
    ];
    for (label, attachments, query) in &attachment_cases {
        out.insert(
            format!("attachment::{label}"),
            json!(summary(&build_attachment_context(
                attachments,
                query,
                &deps
            ))),
        );
    }
    out.insert(
        "attachment::no-guard".to_string(),
        json!(summary(&build_attachment_context(
            &[json!({"text": "一"})],
            "q",
            &no_guard
        ))),
    );

    let expanded_cases: Vec<(&str, Value)> = vec![
        ("empty", json!({})),
        ("content-only", json!({"content": "  你好  "})),
        (
            "legacy-attachment",
            json!({"content": "", "attachments": [{"text": "旧版内容"}]}),
        ),
        (
            "non-dict-attachments",
            json!({"content": "问题", "attachments": ["x"]}),
        ),
        (
            "index",
            json!({"content": "报告讲了什么", "attachments": [{"fileId": file_id()}]}),
        ),
    ];
    for (label, message) in &expanded_cases {
        out.insert(
            format!("expanded::{label}"),
            json!(summary(&expanded_message_content(message, &deps))),
        );
    }

    let rendered = OrderedJson::from_value_with_order(&Value::Object(out), &[]).render_indent_2();
    println!("{rendered}");
}
