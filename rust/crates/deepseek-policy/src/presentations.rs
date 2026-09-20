//! `create_pptx` — a 16:9 PowerPoint deck from a structured outline.
//!
//! Mirrors `infra/tool_runtime/presentations.py` **`create_presentation`**.
//! The content model (normalize, layout picker, MD5 theme, outline, note) is
//! the oracle's. The `.pptx` bytes are not a python-pptx fingerprint: this
//! writer emits a valid OOXML zip with the same titles, bullets and layouts.

use std::path::Path;

use md5::{Digest, Md5};
use regex::Regex;
use serde_json::{Value, json};

use crate::app_error::{AppError, codes};
use crate::core_utils::{encode_lower_hex, python_truthy};
use crate::entropy::Entropy;
use crate::generated_files::{store_generated_file, zip_store};
use crate::python_json::value_str;

pub const MAX_SLIDES: usize = 60;
pub const MAX_BULLETS_PER_SLIDE: usize = 24;

const TITLE_INK: &str = "111827";
const DETAIL_INK: &str = "64748B";
const MUTED_INK: &str = "94A3B8";
const HAIRLINE: &str = "E2E8F0";

const DECK_THEMES: [[&str; 3]; 6] = [
    ["1E3A8A", "2563EB", "BFDBFE"],
    ["312E81", "6366F1", "C7D2FE"],
    ["0F766E", "0D9488", "99F6E4"],
    ["9A3412", "EA580C", "FED7AA"],
    ["0F172A", "0EA5E9", "BAE6FD"],
    ["581C87", "A855F7", "E9D5FF"],
];

#[derive(Debug, Clone)]
pub struct DeckTheme {
    pub cover_bg: String,
    pub accent: String,
    pub band: String,
}

#[derive(Debug, Clone)]
pub struct SlideModel {
    pub title: String,
    pub bullets: Vec<String>,
    pub layout: String,
}

fn python_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => String::new(),
    }
}

fn take_chars(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

/// Mirrors `_deck_theme_for`.
pub fn deck_theme_for(title: &str) -> DeckTheme {
    let digest = Md5::digest(title.as_bytes());
    let hex = encode_lower_hex(&digest);
    let index = (u128::from_str_radix(&hex, 16).unwrap_or(0) % DECK_THEMES.len() as u128) as usize;
    let [cover_bg, accent, band] = DECK_THEMES[index];
    DeckTheme {
        cover_bg: cover_bg.to_string(),
        accent: accent.to_string(),
        band: band.to_string(),
    }
}

/// Mirrors `_normalize_bullets`.
pub fn normalize_bullets(item: &Value) -> Vec<String> {
    let Some(fields) = item.as_object() else {
        return Vec::new();
    };
    if let Some(Value::Array(bullets)) = fields.get("bullets") {
        return bullets
            .iter()
            .map(value_str)
            .map(|text| text.trim().to_string())
            .filter(|text| !text.is_empty())
            .collect();
    }
    let content = python_or_empty(fields.get("content"));
    let content = content.trim();
    if content.is_empty() {
        return Vec::new();
    }
    content
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(str::to_string)
        .collect()
}

/// Mirrors `_split_lead_detail`.
pub fn split_lead_detail(value: &str) -> (String, String) {
    let text = value.trim();
    for delimiter in ["：", ":", " - ", " — ", "——"] {
        if let Some((left, right)) = text.split_once(delimiter) {
            if !left.trim().is_empty() && !right.trim().is_empty() {
                return (left.trim().to_string(), right.trim().to_string());
            }
        }
    }
    (text.to_string(), String::new())
}

/// Mirrors `_layout_for_slide`.
pub fn layout_for_slide(item: &Value, title: &str, bullets: &[String], index: usize) -> String {
    let requested = python_or_empty(item.get("layout"))
        .trim()
        .to_ascii_lowercase();
    if matches!(
        requested.as_str(),
        "section"
            | "agenda"
            | "cards"
            | "process"
            | "timeline"
            | "comparison"
            | "quote"
            | "summary"
            | "bullets"
    ) {
        return requested;
    }
    let title_text = title.to_lowercase();
    let joined = bullets.join(" ").to_lowercase();
    let mixed = format!("{title_text} {joined}");
    if search(
        &title_text,
        r"总结|结论|建议|下一步|行动|takeaway|summary|recommendation",
    ) {
        return "summary".to_string();
    }
    if search(
        &title_text,
        r"流程|步骤|路径|路线|工作流|计划|roadmap|workflow|process|timeline",
    ) {
        return "process".to_string();
    }
    if search(&mixed, r"对比|比较|差异|取舍|优劣|vs\.?|versus|compare") {
        return "comparison".to_string();
    }
    if bullets.len() <= 2
        && (index == 0
            || search(
                &title_text,
                r"背景|目标|问题|机会|挑战|核心观点|overview|context",
            ))
    {
        return "quote".to_string();
    }
    if (3..=6).contains(&bullets.len()) {
        return "cards".to_string();
    }
    "bullets".to_string()
}

fn search(haystack: &str, pattern: &str) -> bool {
    Regex::new(pattern)
        .expect("static regex")
        .is_match(haystack)
}

/// Content-model view for the parity probe (no file bytes).
pub fn presentation_plan(title: &str, slides: &Value, subtitle: &str) -> Result<Value, AppError> {
    let clean_title = title.trim();
    if clean_title.is_empty() {
        return Err(AppError {
            message: "演示文稿需要一个标题（title）。".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let Some(items) = slides.as_array().filter(|items| !items.is_empty()) else {
        return Err(AppError {
            message: "演示文稿至少需要一页内容（slides）。".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    };
    let mut normalized = Vec::new();
    for item in items.iter().take(MAX_SLIDES) {
        let Some(fields) = item.as_object() else {
            continue;
        };
        let fallback = format!("第 {} 页", normalized.len() + 1);
        let raw_title = python_or_empty(fields.get("title"));
        let slide_title = take_chars(
            if raw_title.trim().is_empty() {
                &fallback
            } else {
                raw_title.trim()
            },
            120,
        );
        let bullets: Vec<String> = normalize_bullets(item)
            .into_iter()
            .take(MAX_BULLETS_PER_SLIDE)
            .collect();
        let layout = layout_for_slide(item, &slide_title, &bullets, normalized.len());
        normalized.push(SlideModel {
            title: slide_title,
            bullets,
            layout,
        });
    }
    if normalized.is_empty() {
        return Err(AppError {
            message: "没有解析到有效的幻灯片内容。".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let theme = deck_theme_for(clean_title);
    let mut slide_count = 1i64; // cover
    if normalized.len() >= 4 {
        slide_count += 1; // agenda
    }
    slide_count += normalized.len() as i64;
    Ok(json!({
        "title": clean_title,
        "subtitle": subtitle.trim(),
        "theme": {
            "cover_bg": theme.cover_bg,
            "accent": theme.accent,
            "band": theme.band,
        },
        "slideCount": slide_count,
        "hasAgenda": normalized.len() >= 4,
        "outline": normalized.iter().enumerate().map(|(index, slide)| json!({
            "page": (index + 1) as i64,
            "title": slide.title,
            "bullets": slide.bullets,
            "layout": slide.layout,
        })).collect::<Vec<_>>(),
    }))
}

/// Mirrors `create_presentation`.
pub fn create_presentation(
    title: &str,
    slides: &Value,
    subtitle: &str,
    root: &Path,
    entropy: &dyn Entropy,
    now_epoch: f64,
) -> Result<Value, AppError> {
    let plan = presentation_plan(title, slides, subtitle)?;
    let clean_title = plan["title"].as_str().unwrap();
    let clean_subtitle = plan["subtitle"].as_str().unwrap();
    let theme = deck_theme_for(clean_title);
    let outline = plan["outline"].as_array().unwrap();
    let models: Vec<SlideModel> = outline
        .iter()
        .map(|item| SlideModel {
            title: item["title"].as_str().unwrap().to_string(),
            bullets: item["bullets"]
                .as_array()
                .unwrap()
                .iter()
                .map(value_str)
                .collect(),
            layout: item["layout"].as_str().unwrap().to_string(),
        })
        .collect();
    let bytes = render_pptx(clean_title, clean_subtitle, &models, &theme);
    let stored = store_generated_file(clean_title, "pptx", root, entropy, now_epoch, |path| {
        std::fs::write(path, &bytes)
    })?;
    Ok(json!({
        "fileId": stored["fileId"],
        "filename": stored["filename"],
        "slideCount": plan["slideCount"],
        "downloadUrl": stored["downloadUrl"],
        "title": clean_title,
        "outline": plan["outline"],
        "note": "PPT 已生成。请在最终回复里让用户看到制作过程：先一句话说明这份 PPT 的标题和共几页，再按 outline 字段【逐页展示】每一页的标题和要点（用清晰的分页小标题 + 项目符号列出，不要省略），最后用 Markdown 链接 [下载 PPT](downloadUrl) 给出下载（链接 6 小时内有效）。",
    }))
}

fn xml_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

fn emu(inches: f64) -> i64 {
    (inches * 914_400.0).round() as i64
}

fn fit_font_size(text: &str, base: i64, small: i64) -> i64 {
    let length = text.chars().count();
    if length > 90 {
        small
    } else if length > 58 {
        small.max(base - 3)
    } else {
        base
    }
}

struct SlideXml {
    bg: String,
    shapes: String,
    next_id: u32,
}

impl SlideXml {
    fn new(bg: &str) -> Self {
        Self {
            bg: bg.to_string(),
            shapes: String::new(),
            next_id: 2,
        }
    }

    fn alloc(&mut self) -> u32 {
        let id = self.next_id;
        self.next_id += 1;
        id
    }

    fn rect(&mut self, x: f64, y: f64, w: f64, h: f64, color: &str) {
        let id = self.alloc();
        self.shapes.push_str(&format!(
            "<p:sp><p:nvSpPr><p:cNvPr id=\"{id}\" name=\"rect{id}\"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>\
<p:spPr><a:xfrm><a:off x=\"{}\" y=\"{}\"/><a:ext cx=\"{}\" cy=\"{}\"/></a:xfrm>\
<a:prstGeom prst=\"rect\"><a:avLst/></a:prstGeom>\
<a:solidFill><a:srgbClr val=\"{color}\"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr></p:sp>",
            emu(x),
            emu(y),
            emu(w),
            emu(h)
        ));
    }

    fn oval(&mut self, x: f64, y: f64, w: f64, h: f64, color: &str) {
        let id = self.alloc();
        self.shapes.push_str(&format!(
            "<p:sp><p:nvSpPr><p:cNvPr id=\"{id}\" name=\"oval{id}\"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>\
<p:spPr><a:xfrm><a:off x=\"{}\" y=\"{}\"/><a:ext cx=\"{}\" cy=\"{}\"/></a:xfrm>\
<a:prstGeom prst=\"ellipse\"><a:avLst/></a:prstGeom>\
<a:solidFill><a:srgbClr val=\"{color}\"/></a:solidFill><a:ln><a:noFill/></a:ln></p:spPr></p:sp>",
            emu(x),
            emu(y),
            emu(w),
            emu(h)
        ));
    }

    fn textbox(
        &mut self,
        x: f64,
        y: f64,
        w: f64,
        h: f64,
        paragraphs: &[(String, i64, &str, bool, &str)],
    ) {
        let id = self.alloc();
        let mut body = String::new();
        for (index, (text, size, color, bold, align)) in paragraphs.iter().enumerate() {
            let b = if *bold { "1" } else { "0" };
            body.push_str(&format!(
                "<a:p><a:pPr algn=\"{align}\"{spc}/><a:r><a:rPr lang=\"zh-CN\" sz=\"{}\" b=\"{b}\" dirty=\"0\">\
<a:solidFill><a:srgbClr val=\"{color}\"/></a:solidFill>\
<a:latin typeface=\"微软雅黑\"/><a:ea typeface=\"微软雅黑\"/></a:rPr>\
<a:t>{}</a:t></a:r></a:p>",
                size * 100,
                xml_escape(text),
                spc = if index == 0 {
                    String::new()
                } else {
                    " <a:spcBef><a:spcPts val=\"300\"/></a:spcBef>".to_string()
                }
            ));
        }
        if body.is_empty() {
            body.push_str("<a:p><a:endParaRPr lang=\"zh-CN\"/></a:p>");
        }
        self.shapes.push_str(&format!(
            "<p:sp><p:nvSpPr><p:cNvPr id=\"{id}\" name=\"tx{id}\"/><p:cNvSpPr txBox=\"1\"/><p:nvPr/></p:nvSpPr>\
<p:spPr><a:xfrm><a:off x=\"{}\" y=\"{}\"/><a:ext cx=\"{}\" cy=\"{}\"/></a:xfrm>\
<a:prstGeom prst=\"rect\"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>\
<p:txBody><a:bodyPr wrap=\"square\" lIns=\"0\" tIns=\"0\" rIns=\"0\" bIns=\"0\"/><a:lstStyle/>{body}</p:txBody></p:sp>",
            emu(x),
            emu(y),
            emu(w),
            emu(h)
        ));
    }

    fn finish(self) -> String {
        format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\
<p:sld xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\" \
xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\" \
xmlns:p=\"http://schemas.openxmlformats.org/presentationml/2006/main\">\
<p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val=\"{}\"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>\
<p:spTree><p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>\
<p:grpSpPr><a:xfrm><a:off x=\"0\" y=\"0\"/><a:ext cx=\"12192000\" cy=\"6858000\"/>\
<a:chOff x=\"0\" y=\"0\"/><a:chExt cx=\"12192000\" cy=\"6858000\"/></a:xfrm></p:grpSpPr>\
{}</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>",
            self.bg, self.shapes
        )
    }
}

fn add_header(slide: &mut SlideXml, theme: &DeckTheme, title: &str) {
    slide.rect(0.82, 0.6, 0.62, 0.06, &theme.accent);
    let size = fit_font_size(title, 28, 22);
    slide.textbox(
        0.82,
        0.78,
        11.7,
        0.92,
        &[(take_chars(title, 120), size, TITLE_INK, true, "l")],
    );
}

fn add_footer(slide: &mut SlideXml, page_no: i64) {
    slide.textbox(
        11.72,
        6.92,
        0.95,
        0.28,
        &[(format!("{page_no:02}"), 9, MUTED_INK, true, "r")],
    );
}

fn add_card(
    slide: &mut SlideXml,
    theme: &DeckTheme,
    frame: (f64, f64, f64, f64),
    title: &str,
    body: &str,
    number: Option<i64>,
) {
    let (x, y, w, h) = frame;
    slide.rect(x, y, 0.34, 0.04, &theme.accent);
    let lead = if let Some(n) = number {
        format!("{n:02}  {}", take_chars(title, 160))
    } else {
        take_chars(title, 160)
    };
    let size = fit_font_size(&lead, 15, 12);
    let mut paras = vec![(lead, size, TITLE_INK, true, "l")];
    if !body.is_empty() {
        paras.push((
            take_chars(body, 220),
            fit_font_size(body, 11, 10),
            DETAIL_INK,
            false,
            "l",
        ));
    }
    slide.textbox(x, y + 0.16, w, h - 0.16, &paras);
}

fn cover_slide(title: &str, subtitle: &str, theme: &DeckTheme) -> String {
    let mut slide = SlideXml::new(&theme.cover_bg);
    slide.rect(0.0, 0.0, 0.3, 7.5, &theme.accent);
    slide.rect(1.2, 2.35, 0.7, 0.06, &theme.accent);
    let size = if title.chars().count() > 18 { 44 } else { 52 };
    let mut paras = vec![(take_chars(title, 120), size, "FFFFFF", true, "l")];
    if !subtitle.is_empty() {
        paras.push((
            take_chars(subtitle, 160),
            18,
            theme.band.as_str(),
            false,
            "l",
        ));
    }
    slide.textbox(1.2, 2.55, 10.9, 2.6, &paras);
    slide.finish()
}

fn bullets_slide(title: &str, bullets: &[String], theme: &DeckTheme, page_no: i64) -> String {
    let mut slide = SlideXml::new("FFFFFF");
    add_header(&mut slide, theme, title);
    let count = bullets.len().max(1);
    let lead_size: i64 = if count <= 4 {
        19
    } else if count <= 6 {
        17
    } else {
        15
    };
    let detail_size = (lead_size - 5).max(11);
    let top = 2.0;
    let avail = 4.9;
    let step = avail / count as f64;
    for (index, bullet) in bullets.iter().enumerate() {
        let y = top + index as f64 * step;
        let (lead, detail) = split_lead_detail(bullet);
        slide.rect(0.9, y + 0.07, 0.1, 0.1, &theme.accent);
        let mut paras = vec![(take_chars(&lead, 240), lead_size, TITLE_INK, true, "l")];
        if !detail.is_empty() {
            paras.push((
                take_chars(&detail, 300),
                detail_size,
                DETAIL_INK,
                false,
                "l",
            ));
        }
        slide.textbox(1.2, y, 11.2, step, &paras);
        if index + 1 < count {
            slide.rect(1.2, y + step - 0.12, 11.0, 0.01, HAIRLINE);
        }
    }
    add_footer(&mut slide, page_no);
    slide.finish()
}

fn agenda_slide(titles: &[String], theme: &DeckTheme, page_no: i64) -> String {
    let mut slide = SlideXml::new("FFFFFF");
    add_header(&mut slide, theme, "内容导航");
    let y0 = 1.95;
    for (index, item) in titles.iter().take(6).enumerate() {
        let y = y0 + index as f64 * 0.74;
        slide.textbox(
            0.9,
            y - 0.04,
            0.8,
            0.5,
            &[(
                format!("{:02}", index + 1),
                18,
                theme.accent.as_str(),
                true,
                "l",
            )],
        );
        slide.textbox(
            1.75,
            y,
            9.8,
            0.5,
            &[(take_chars(item, 96), 17, TITLE_INK, true, "l")],
        );
        slide.rect(1.75, y + 0.54, 9.6, 0.012, HAIRLINE);
    }
    add_footer(&mut slide, page_no);
    slide.finish()
}

fn cards_slide(title: &str, bullets: &[String], theme: &DeckTheme, page_no: i64) -> String {
    let mut slide = SlideXml::new("FFFFFF");
    add_header(&mut slide, theme, title);
    let positions = [
        (0.9, 1.95, 5.5, 1.25),
        (6.95, 1.95, 5.4, 1.25),
        (0.9, 3.35, 5.5, 1.25),
        (6.95, 3.35, 5.4, 1.25),
        (0.9, 4.75, 5.5, 1.25),
        (6.95, 4.75, 5.4, 1.25),
    ];
    for (index, bullet) in bullets.iter().take(6).enumerate() {
        let (lead, detail) = split_lead_detail(bullet);
        add_card(
            &mut slide,
            theme,
            positions[index],
            &lead,
            &detail,
            Some(index as i64 + 1),
        );
    }
    add_footer(&mut slide, page_no);
    slide.finish()
}

fn process_slide(title: &str, bullets: &[String], theme: &DeckTheme, page_no: i64) -> String {
    let mut slide = SlideXml::new("FFFFFF");
    add_header(&mut slide, theme, title);
    let steps: Vec<String> = if bullets.is_empty() {
        ["明确目标", "拆解任务", "执行推进", "验证结果"]
            .into_iter()
            .map(str::to_string)
            .collect()
    } else {
        bullets.iter().take(5).cloned().collect()
    };
    let width = 10.8 / steps.len().max(1) as f64;
    let y = 3.02;
    for (index, step) in steps.iter().enumerate() {
        let x = 0.98 + index as f64 * width;
        slide.oval(x + width / 2.0 - 0.34, y - 0.32, 0.68, 0.68, &theme.accent);
        slide.textbox(
            x + width / 2.0 - 0.34,
            y - 0.32,
            0.68,
            0.68,
            &[(format!("{}", index + 1), 13, "FFFFFF", true, "ctr")],
        );
        if index + 1 < steps.len() {
            slide.rect(
                x + width / 2.0 + 0.36,
                y,
                (width - 0.72).max(0.2),
                0.03,
                HAIRLINE,
            );
        }
        let (lead, detail) = split_lead_detail(step);
        let mut paras = vec![(
            take_chars(&lead, 80),
            fit_font_size(&lead, 13, 9),
            TITLE_INK,
            true,
            "ctr",
        )];
        if !detail.is_empty() {
            paras.push((take_chars(&detail, 120), 9, DETAIL_INK, false, "ctr"));
        }
        slide.textbox(x + 0.08, y + 0.62, width - 0.16, 1.25, &paras);
    }
    add_footer(&mut slide, page_no);
    slide.finish()
}

fn comparison_slide(title: &str, bullets: &[String], theme: &DeckTheme, page_no: i64) -> String {
    let mut slide = SlideXml::new("FFFFFF");
    add_header(&mut slide, theme, title);
    slide.rect(6.62, 1.95, 0.02, 4.45, HAIRLINE);
    let midpoint = ((bullets.len() + 1) / 2).max(1);
    let left = &bullets[..midpoint.min(bullets.len())];
    let right = if bullets.len() > midpoint {
        &bullets[midpoint..]
    } else {
        left
    };
    let columns = [left, right];
    let headers = ["方案 / 维度 A", "方案 / 维度 B"];
    let xs = [0.9, 6.95];
    for (col, items) in columns.iter().enumerate() {
        let x = xs[col];
        slide.textbox(
            x,
            1.95,
            5.2,
            0.4,
            &[(
                headers[col].to_string(),
                14,
                theme.accent.as_str(),
                true,
                "l",
            )],
        );
        slide.rect(x, 2.4, 0.32, 0.04, &theme.accent);
        let mut y = 2.7;
        for item in items.iter().take(5) {
            let (lead, detail) = split_lead_detail(item);
            slide.textbox(
                x,
                y,
                5.3,
                0.5,
                &[(take_chars(&lead, 100), 13, TITLE_INK, true, "l")],
            );
            if !detail.is_empty() {
                slide.textbox(
                    x,
                    y + 0.32,
                    5.3,
                    0.4,
                    &[(take_chars(&detail, 150), 10, DETAIL_INK, false, "l")],
                );
                y += 0.86;
            } else {
                y += 0.56;
            }
        }
    }
    add_footer(&mut slide, page_no);
    slide.finish()
}

fn quote_slide(title: &str, bullets: &[String], theme: &DeckTheme, page_no: i64) -> String {
    let mut slide = SlideXml::new(&theme.cover_bg);
    slide.rect(0.84, 1.5, 0.1, 4.2, &theme.accent);
    let quote = bullets
        .first()
        .cloned()
        .unwrap_or_else(|| title.to_string());
    let paras = vec![
        (
            take_chars(title, 120),
            fit_font_size(title, 34, 26),
            "FFFFFF",
            true,
            "l",
        ),
        (
            take_chars(&quote, 220),
            fit_font_size(&quote, 18, 13),
            theme.band.as_str(),
            false,
            "l",
        ),
    ];
    slide.textbox(1.25, 1.62, 10.8, 2.6, &paras);
    for (index, bullet) in bullets.iter().skip(1).take(3).enumerate() {
        let x = 1.26 + index as f64 * 3.7;
        let (lead, detail) = split_lead_detail(bullet);
        slide.rect(x, 5.18, 0.3, 0.04, &theme.accent);
        let mut paras = vec![(take_chars(&lead, 80), 13, "FFFFFF", true, "l")];
        if !detail.is_empty() {
            paras.push((
                take_chars(&detail, 120),
                10,
                theme.band.as_str(),
                false,
                "l",
            ));
        }
        slide.textbox(x, 5.32, 3.4, 1.05, &paras);
    }
    add_footer(&mut slide, page_no);
    slide.finish()
}

fn summary_slide(title: &str, bullets: &[String], theme: &DeckTheme, page_no: i64) -> String {
    let mut slide = SlideXml::new("FFFFFF");
    add_header(&mut slide, theme, title);
    slide.rect(0.9, 1.9, 11.5, 1.15, &theme.cover_bg);
    let headline = bullets
        .first()
        .cloned()
        .unwrap_or_else(|| "回顾重点，明确下一步行动。".to_string());
    slide.textbox(
        1.25,
        2.12,
        10.8,
        0.75,
        &[(
            take_chars(&headline, 160),
            fit_font_size(&headline, 19, 14),
            "FFFFFF",
            true,
            "l",
        )],
    );
    let rest: Vec<String> = if bullets.len() > 1 {
        bullets[1..].to_vec()
    } else {
        bullets.to_vec()
    };
    for (index, bullet) in rest.iter().take(4).enumerate() {
        let (lead, detail) = split_lead_detail(bullet);
        let x = 1.0 + (index % 2) as f64 * 5.75;
        let y = 3.6 + (index / 2) as f64 * 1.45;
        add_card(
            &mut slide,
            theme,
            (x, y, 5.3, 1.25),
            &lead,
            &detail,
            Some(index as i64 + 1),
        );
    }
    add_footer(&mut slide, page_no);
    slide.finish()
}

fn rich_slide(model: &SlideModel, theme: &DeckTheme, page_no: i64) -> String {
    match model.layout.as_str() {
        "agenda" => agenda_slide(&model.bullets, theme, page_no),
        "section" | "quote" => quote_slide(&model.title, &model.bullets, theme, page_no),
        "cards" => cards_slide(&model.title, &model.bullets, theme, page_no),
        "process" | "timeline" => process_slide(&model.title, &model.bullets, theme, page_no),
        "comparison" => comparison_slide(&model.title, &model.bullets, theme, page_no),
        "summary" => summary_slide(&model.title, &model.bullets, theme, page_no),
        _ => bullets_slide(&model.title, &model.bullets, theme, page_no),
    }
}

const THEME_XML: &str = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme"><a:themeElements><a:clrScheme name="Office"><a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1><a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="1F497D"/></a:dk2><a:lt2><a:srgbClr val="EEECE1"/></a:lt2><a:accent1><a:srgbClr val="4F81BD"/></a:accent1><a:accent2><a:srgbClr val="C0504D"/></a:accent2><a:accent3><a:srgbClr val="9BBB59"/></a:accent3><a:accent4><a:srgbClr val="8064A2"/></a:accent4><a:accent5><a:srgbClr val="4BACC6"/></a:accent5><a:accent6><a:srgbClr val="F79646"/></a:accent6><a:hlink><a:srgbClr val="0000FF"/></a:hlink><a:folHlink><a:srgbClr val="800080"/></a:folHlink></a:clrScheme><a:fontScheme name="Office"><a:majorFont><a:latin typeface="Calibri"/><a:ea typeface="微软雅黑"/><a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Calibri"/><a:ea typeface="微软雅黑"/><a:cs typeface=""/></a:minorFont></a:fontScheme><a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln><a:ln w="25400"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln><a:ln w="38100"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:prstDash val="solid"/></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>"#;

const MASTER_XML: &str = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"><p:cSld><p:bg><p:bgRef idx="1001"><a:schemeClr val="bg1"/></p:bgRef></p:bg><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="12192000" cy="6858000"/><a:chOff x="0" y="0"/><a:chExt cx="12192000" cy="6858000"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst></p:sldMaster>"#;

const LAYOUT_XML: &str = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1"><p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="12192000" cy="6858000"/><a:chOff x="0" y="0"/><a:chExt cx="12192000" cy="6858000"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"#;

fn render_pptx(title: &str, subtitle: &str, slides: &[SlideModel], theme: &DeckTheme) -> Vec<u8> {
    let mut xmls = vec![cover_slide(title, subtitle, theme)];
    if slides.len() >= 4 {
        let titles: Vec<String> = slides.iter().map(|slide| slide.title.clone()).collect();
        xmls.push(agenda_slide(&titles, theme, 2));
    }
    for model in slides {
        let page_no = xmls.len() as i64 + 1;
        xmls.push(rich_slide(model, theme, page_no));
    }

    let mut files: Vec<(String, Vec<u8>)> = Vec::new();
    let mut types = String::from(
        r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/><Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/><Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/><Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>"#,
    );
    let mut sld_ids = String::new();
    let mut pres_rels = String::from(
        r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>"#,
    );
    for (index, xml) in xmls.iter().enumerate() {
        let n = index + 1;
        types.push_str(&format!(
            "<Override PartName=\"/ppt/slides/slide{n}.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.presentationml.slide+xml\"/>"
        ));
        sld_ids.push_str(&format!(
            "<p:sldId id=\"{}\" r:id=\"rId{}\"/>",
            255 + n as u32,
            n + 1
        ));
        pres_rels.push_str(&format!(
            "<Relationship Id=\"rId{}\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide\" Target=\"slides/slide{n}.xml\"/>",
            n + 1
        ));
        files.push((format!("ppt/slides/slide{n}.xml"), xml.as_bytes().to_vec()));
        files.push((
            format!("ppt/slides/_rels/slide{n}.xml.rels"),
            br#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>"#.to_vec(),
        ));
    }
    types.push_str("</Types>");
    pres_rels.push_str("</Relationships>");
    let presentation = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><p:presentation xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\" xmlns:p=\"http://schemas.openxmlformats.org/presentationml/2006/main\"><p:sldMasterIdLst><p:sldMasterId id=\"2147483648\" r:id=\"rId1\"/></p:sldMasterIdLst><p:sldIdLst>{sld_ids}</p:sldIdLst><p:sldSz cx=\"12192000\" cy=\"6858000\"/><p:notesSz cx=\"6858000\" cy=\"9144000\"/></p:presentation>"
    );
    let mut named: Vec<(&str, Vec<u8>)> = vec![
        ("[Content_Types].xml", types.into_bytes()),
        (
            "_rels/.rels",
            br#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>"#.to_vec(),
        ),
        ("ppt/presentation.xml", presentation.into_bytes()),
        ("ppt/_rels/presentation.xml.rels", pres_rels.into_bytes()),
        ("ppt/theme/theme1.xml", THEME_XML.as_bytes().to_vec()),
        ("ppt/slideMasters/slideMaster1.xml", MASTER_XML.as_bytes().to_vec()),
        (
            "ppt/slideMasters/_rels/slideMaster1.xml.rels",
            br#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/></Relationships>"#.to_vec(),
        ),
        ("ppt/slideLayouts/slideLayout1.xml", LAYOUT_XML.as_bytes().to_vec()),
        (
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
            br#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>"#.to_vec(),
        ),
    ];
    // Lifetime: zip_store wants &str names that live long enough. Collect owned names.
    let extra: Vec<(String, Vec<u8>)> = files;
    let owned: Vec<(String, Vec<u8>)> = named
        .drain(..)
        .map(|(name, data)| (name.to_string(), data))
        .chain(extra)
        .collect();
    let refs: Vec<(&str, Vec<u8>)> = owned.iter().map(|(n, d)| (n.as_str(), d.clone())).collect();
    zip_store(&refs)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::entropy::SystemEntropy;
    use crate::generated_files::{self, resolve_generated_file};

    #[test]
    fn refusals_match_the_oracle() {
        let root = std::env::temp_dir().join(format!("pptx-empty-{}", std::process::id()));
        let err = create_presentation("", &json!([{"title": "x"}]), "", &root, &SystemEntropy, 0.0)
            .unwrap_err();
        assert_eq!(err.message, "演示文稿需要一个标题（title）。");
        let err =
            create_presentation("有标题", &json!([]), "", &root, &SystemEntropy, 0.0).unwrap_err();
        assert_eq!(err.message, "演示文稿至少需要一页内容（slides）。");
    }

    #[test]
    fn content_field_becomes_bullets_and_cover_counts() {
        let plan = presentation_plan(
            "标题",
            &json!([{"title": "页", "content": "第一行\n第二行"}]),
            "副标题",
        )
        .unwrap();
        assert_eq!(plan["slideCount"], 2);
        assert_eq!(plan["outline"][0]["bullets"], json!(["第一行", "第二行"]));
        assert_eq!(plan["hasAgenda"], false);
    }

    #[test]
    fn larger_deck_gets_agenda_and_rich_layouts() {
        let plan = presentation_plan(
            "Product Roadmap",
            &json!([
                {"title": "核心观点", "bullets": ["把复杂流程拆成三条主线"]},
                {"title": "关键能力", "bullets": ["洞察：统一指标", "执行：标准流程", "反馈：闭环复盘"]},
                {"title": "实施流程", "bullets": ["调研", "试点", "推广", "复盘"]},
                {"title": "方案对比", "bullets": ["自建：控制力强", "采购：上线快", "混合：风险均衡"]},
                {"title": "总结与下一步", "bullets": ["先跑 MVP", "两周后复盘", "明确负责人"]}
            ]),
            "",
        )
        .unwrap();
        assert_eq!(plan["slideCount"], 7);
        assert_eq!(plan["hasAgenda"], true);
        assert_eq!(plan["outline"][0]["layout"], "quote");
        assert_eq!(plan["outline"][1]["layout"], "cards");
        assert_eq!(plan["outline"][2]["layout"], "process");
        assert_eq!(plan["outline"][3]["layout"], "comparison");
        assert_eq!(plan["outline"][4]["layout"], "summary");
    }

    #[test]
    fn create_presentation_writes_a_pptx_zip() {
        let root = std::env::temp_dir().join(format!("pptx-ok-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let result = create_presentation(
            "测试标题",
            &json!([
                {"title": "第一页", "bullets": ["要点 A", "要点 B"]},
                {"title": "第二页", "bullets": ["要点 C"]}
            ]),
            "副标题",
            &root,
            &SystemEntropy,
            generated_files::system_now(),
        )
        .unwrap();
        assert_eq!(result["slideCount"], 3);
        assert!(result["filename"].as_str().unwrap().ends_with(".pptx"));
        let path = resolve_generated_file(&root, result["fileId"].as_str().unwrap()).unwrap();
        let bytes = std::fs::read(&path).unwrap();
        assert_eq!(&bytes[..4], b"PK\x03\x04");
        let as_text = String::from_utf8_lossy(&bytes);
        assert!(as_text.contains("ppt/slides/slide1.xml"));
        assert!(as_text.contains("测试标题"));
        let _ = std::fs::remove_dir_all(&root);
    }
}
