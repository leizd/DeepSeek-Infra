//! create_pptx parity probe, Rust side.

use deepseek_policy::app_error::AppError;
use deepseek_policy::presentations::presentation_plan;
use serde_json::{Map, Value, json};

fn error_view(error: &AppError) -> Value {
    json!({"error": error.message, "code": error.code, "status": error.status})
}

fn plan(title: &str, slides: Value, subtitle: &str) -> Value {
    match presentation_plan(title, &slides, subtitle) {
        Ok(value) => value,
        Err(error) => error_view(&error),
    }
}

fn main() {
    let roadmap = json!([
        {"title": "核心观点", "bullets": ["把复杂流程拆成三条主线"]},
        {"title": "关键能力", "bullets": ["洞察：统一指标", "执行：标准流程", "反馈：闭环复盘"]},
        {"title": "实施流程", "bullets": ["调研", "试点", "推广", "复盘"]},
        {"title": "方案对比", "bullets": ["自建：控制力强", "采购：上线快", "混合：风险均衡"]},
        {"title": "总结与下一步", "bullets": ["先跑 MVP", "两周后复盘", "明确负责人"]}
    ]);
    let mut out = Map::new();
    out.insert(
        "empty-title".to_string(),
        plan("", json!([{"title": "x", "bullets": ["a"]}]), ""),
    );
    out.insert("empty-slides".to_string(), plan("有标题", json!([]), ""));
    out.insert(
        "skip-non-dict".to_string(),
        plan(
            "T",
            json!([null, "bad", {"title": "页", "bullets": ["a"]}]),
            "",
        ),
    );
    out.insert(
        "content-fallback".to_string(),
        plan(
            "标题",
            json!([{"title": "页", "content": "第一行\n第二行"}]),
            "副标题",
        ),
    );
    out.insert(
        "default-title".to_string(),
        plan("T", json!([{"bullets": ["only"]}]), ""),
    );
    out.insert(
        "requested-layout".to_string(),
        plan(
            "T",
            json!([{"title": "X", "bullets": ["a", "b", "c"], "layout": "PROCESS"}]),
            "",
        ),
    );
    out.insert("roadmap".to_string(), plan("Product Roadmap", roadmap, ""));
    out.insert(
        "two-slides".to_string(),
        plan(
            "测试标题",
            json!([
                {"title": "第一页", "bullets": ["要点 A", "要点 B"]},
                {"title": "第二页", "bullets": ["要点 C"]}
            ]),
            "副标题",
        ),
    );
    let mut encoded = serde_json::to_string_pretty(&Value::Object(out)).expect("serialize");
    encoded.push('\n');
    print!("{encoded}");
}
