//! create_document parity probe, Rust side.
//!
//! Usage::
//!
//!     python tasks/native-runtime/document_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example document_parity_probe > ../rust.json

use deepseek_policy::app_error::AppError;
use deepseek_policy::documents::document_plan;
use serde_json::{Map, Value, json};

fn error_view(error: &AppError) -> Value {
    json!({"error": error.message, "code": error.code, "status": error.status})
}

fn plan(fmt: &str, title: &str, sections: Value, subtitle: &str) -> Value {
    match document_plan(fmt, title, &sections, subtitle) {
        Ok(value) => value,
        Err(error) => error_view(&error),
    }
}

fn main() {
    let sample = json!([
        {
            "heading": "概述",
            "body": ["这是第一段正文，用于介绍背景。", "这是第二段，给出本文目标。"],
            "bullets": ["要点一", "要点二 <含特殊字符 & 符号>"],
            "table": {"headers": ["列 A", "列 B"], "rows": [["1", "2"], ["3", "4"]]}
        },
        {
            "heading": "结论",
            "body": ["总结性段落，给出下一步建议。"],
            "bullets": [],
            "table": {"headers": [], "rows": []}
        }
    ]);
    let mut out = Map::new();
    out.insert(
        "alias::word".to_string(),
        plan("word", "标题", sample.clone(), ""),
    );
    out.insert(
        "alias::pdf".to_string(),
        plan("PDF", "标题", sample.clone(), ""),
    );
    out.insert(
        "alias::txt".to_string(),
        plan("txt", "标题", sample.clone(), ""),
    );
    out.insert(
        "empty-title".to_string(),
        plan("docx", "  ", sample.clone(), ""),
    );
    out.insert(
        "empty-sections".to_string(),
        plan("docx", "标题", json!([]), ""),
    );
    out.insert(
        "sample".to_string(),
        plan("docx", "季度产品报告", sample, "2026 Q3"),
    );
    out.insert(
        "ragged".to_string(),
        plan(
            "docx",
            "表格测试",
            json!([{"heading": "数据", "body": [], "bullets": [], "table": {"headers": ["A", "B", "C"], "rows": [["1"], ["1", "2", "3", "4", "5"]]}}]),
            "",
        ),
    );
    out.insert(
        "skip-empty-section".to_string(),
        plan(
            "docx",
            "T",
            json!([{"heading": "", "body": [], "bullets": []}, {"heading": "Keep", "body": ["x"]}]),
            "",
        ),
    );
    out.insert(
        "default-heading".to_string(),
        plan("docx", "T", json!([{"body": ["only body"]}]), ""),
    );
    let mut encoded = serde_json::to_string_pretty(&Value::Object(out)).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}
