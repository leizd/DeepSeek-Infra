//! `create_document` — Word (.docx) and PDF from a structured outline.
//!
//! Mirrors `infra/tool_runtime/documents.py`. The **content model** (format
//! aliases, section/table normalization, MD5 theme, outline, note) is the
//! oracle's. The Office/PDF **bytes** are not python-docx / reportlab
//! fingerprints: those libraries are not a frozen protocol. This writer
//! produces a valid OOXML zip and a valid PDF that carry the same title,
//! sections, bullets and tables, including CJK.
//!
//! PDF CJK uses `/STSong-Light` + `/UniGB-UCS2-H`, the same CID approach
//! reportlab's `UnicodeCIDFont("STSong-Light")` uses, so viewers with Adobe
//! CJK CMaps (PDFium, Acrobat, poppler) render Chinese without embedding a
//! multi-megabyte TTF.

use std::io::Write;
use std::path::Path;

use md5::{Digest, Md5};
use serde_json::{Value, json};

use crate::app_error::{AppError, codes};
use crate::core_utils::{encode_lower_hex, python_truthy};
use crate::entropy::Entropy;
use crate::generated_files::{store_generated_file, zip_store};
use crate::python_json::value_str;

pub const MAX_SECTIONS: usize = 40;
pub const MAX_PARAGRAPHS_PER_SECTION: usize = 40;
pub const MAX_BULLETS_PER_SECTION: usize = 40;
pub const MAX_TABLE_ROWS: usize = 60;
pub const MAX_TABLE_COLS: usize = 8;
pub const MAX_PARAGRAPH_CHARS: usize = 4000;

const INK: &str = "1F2933";
const MUTED: &str = "6B7280";
const BAND: &str = "F1F5F9";

const DOC_THEMES: [[&str; 4]; 6] = [
    ["1D4ED8", "1E3A8A", "DBEAFE", "93C5FD"],
    ["4F46E5", "312E81", "E0E7FF", "A5B4FC"],
    ["0D9488", "115E59", "CCFBF1", "5EEAD4"],
    ["EA580C", "9A3412", "FFEDD5", "FDBA74"],
    ["0284C7", "0C4A6E", "E0F2FE", "7DD3FC"],
    ["7C3AED", "581C87", "F3E8FF", "C4B5FD"],
];

#[derive(Debug, Clone)]
pub struct Theme {
    pub primary: String,
    pub dark: String,
    pub light: String,
    pub rule: String,
}

#[derive(Debug, Clone)]
pub struct TableModel {
    pub headers: Vec<String>,
    pub rows: Vec<Vec<String>>,
}

#[derive(Debug, Clone)]
pub struct Section {
    pub heading: String,
    pub body: Vec<String>,
    pub bullets: Vec<String>,
    pub table: Option<TableModel>,
}

/// Mirrors `_clean_text`.
pub fn clean_text(value: &str, limit: usize) -> String {
    regex::Regex::new(r"\s+")
        .expect("static regex")
        .replace_all(value, " ")
        .trim()
        .chars()
        .take(limit)
        .collect()
}

fn python_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => String::new(),
    }
}

/// Mirrors `_normalize_format`.
pub fn normalize_format(fmt: &str) -> Result<&'static str, AppError> {
    let value = fmt.trim().to_ascii_lowercase();
    match value.as_str() {
        "word" | "doc" | "docx" => Ok("docx"),
        "pdf" => Ok("pdf"),
        _ => Err(AppError {
            message: "format 必须是 docx（Word）或 pdf。".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        }),
    }
}

/// Mirrors `_theme_for`.
pub fn theme_for(title: &str) -> Theme {
    let digest = Md5::digest(title.as_bytes());
    let hex = encode_lower_hex(&digest);
    let index = (u128::from_str_radix(&hex, 16).unwrap_or(0) % DOC_THEMES.len() as u128) as usize;
    let [primary, dark, light, rule] = DOC_THEMES[index];
    Theme {
        primary: primary.to_string(),
        dark: dark.to_string(),
        light: light.to_string(),
        rule: rule.to_string(),
    }
}

fn normalize_str_list(value: Option<&Value>, limit: usize) -> Vec<String> {
    let Some(Value::Array(items)) = value else {
        return Vec::new();
    };
    let mut out = Vec::new();
    for item in items {
        let text = clean_text(&python_or_empty(Some(item)), MAX_PARAGRAPH_CHARS);
        if !text.is_empty() {
            out.push(text);
        }
        if out.len() >= limit {
            break;
        }
    }
    out
}

fn normalize_table(value: Option<&Value>) -> Option<TableModel> {
    let Value::Object(fields) = value? else {
        return None;
    };
    let headers = match fields.get("headers") {
        Some(Value::Array(items)) => items
            .iter()
            .map(|cell| clean_text(&python_or_empty(Some(cell)), 160))
            .take(MAX_TABLE_COLS)
            .collect::<Vec<_>>(),
        _ => Vec::new(),
    };
    let mut rows = Vec::new();
    if let Some(Value::Array(raw_rows)) = fields.get("rows") {
        for raw_row in raw_rows.iter().take(MAX_TABLE_ROWS) {
            let Value::Array(cells) = raw_row else {
                continue;
            };
            let cells: Vec<String> = cells
                .iter()
                .map(|cell| clean_text(&python_or_empty(Some(cell)), 300))
                .take(MAX_TABLE_COLS)
                .collect();
            if cells.iter().any(|cell| !cell.is_empty()) {
                rows.push(cells);
            }
        }
    }
    let width = headers
        .len()
        .max(rows.iter().map(Vec::len).max().unwrap_or(0));
    if width == 0 {
        return None;
    }
    let headers = if headers.is_empty() {
        Vec::new()
    } else {
        let mut padded = headers;
        padded.resize(width, String::new());
        padded
    };
    let rows = rows
        .into_iter()
        .map(|mut row| {
            row.resize(width, String::new());
            row
        })
        .collect();
    Some(TableModel { headers, rows })
}

/// Mirrors `_normalize_sections`.
pub fn normalize_sections(sections: &Value) -> Result<Vec<Section>, AppError> {
    let Value::Array(items) = sections else {
        return Err(AppError {
            message: "文档至少需要一个章节（sections）。".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    };
    if items.is_empty() {
        return Err(AppError {
            message: "文档至少需要一个章节（sections）。".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let mut normalized = Vec::new();
    for item in items.iter().take(MAX_SECTIONS) {
        let Some(fields) = item.as_object() else {
            continue;
        };
        let heading = clean_text(&python_or_empty(fields.get("heading")), 200);
        let body = normalize_str_list(fields.get("body"), MAX_PARAGRAPHS_PER_SECTION);
        let bullets = normalize_str_list(fields.get("bullets"), MAX_BULLETS_PER_SECTION);
        let table = normalize_table(fields.get("table"));
        if heading.is_empty() && body.is_empty() && bullets.is_empty() && table.is_none() {
            continue;
        }
        normalized.push(Section {
            heading: if heading.is_empty() {
                "正文".to_string()
            } else {
                heading
            },
            body,
            bullets,
            table,
        });
    }
    if normalized.is_empty() {
        return Err(AppError {
            message: "没有解析到有效的文档内容。".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    Ok(normalized)
}

fn document_result(stored: &Value, fmt: &str, title: &str, sections: &[Section]) -> Value {
    let outline: Vec<Value> = sections
        .iter()
        .enumerate()
        .map(|(index, section)| {
            json!({
                "index": (index + 1) as i64,
                "heading": section.heading,
                "paragraphs": section.body.len() as i64,
                "bullets": section.bullets.len() as i64,
                "hasTable": section.table.is_some(),
            })
        })
        .collect();
    let label = if fmt == "docx" {
        "Word 文档"
    } else {
        "PDF 文档"
    };
    json!({
        "fileId": stored["fileId"],
        "filename": stored["filename"],
        "format": fmt,
        "sectionCount": sections.len() as i64,
        "downloadUrl": stored["downloadUrl"],
        "title": title,
        "outline": outline,
        "note": format!(
            "{label}已生成。请在最终回复里：先一句话说明文档标题、格式（{label}）和包含的主要章节，再用 Markdown 链接 [下载文档](downloadUrl) 把下载交给用户（链接 6 小时内有效）。不要把整篇正文重新粘回聊天，简述结构即可。"
        ),
    })
}

/// Public normalize+theme view for the parity probe (no file bytes).
pub fn document_plan(
    fmt: &str,
    title: &str,
    sections: &Value,
    subtitle: &str,
) -> Result<Value, AppError> {
    let normalized_fmt = normalize_format(fmt)?;
    let clean_title = title.trim();
    if clean_title.is_empty() {
        return Err(AppError {
            message: "文档需要一个标题（title）。".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let normalized_sections = normalize_sections(sections)?;
    let clean_subtitle = clean_text(subtitle, 300);
    let theme = theme_for(clean_title);
    Ok(json!({
        "format": normalized_fmt,
        "title": clean_title,
        "subtitle": clean_subtitle,
        "theme": {
            "primary": theme.primary,
            "dark": theme.dark,
            "light": theme.light,
            "rule": theme.rule,
        },
        "sections": normalized_sections.iter().map(|section| {
            json!({
                "heading": section.heading,
                "body": section.body,
                "bullets": section.bullets,
                "table": section.table.as_ref().map(|table| json!({
                    "headers": table.headers,
                    "rows": table.rows,
                })),
            })
        }).collect::<Vec<_>>(),
    }))
}

/// Mirrors `create_document`.
pub fn create_document(
    fmt: &str,
    title: &str,
    sections: &Value,
    subtitle: &str,
    root: &Path,
    entropy: &dyn Entropy,
    now_epoch: f64,
) -> Result<Value, AppError> {
    let plan = document_plan(fmt, title, sections, subtitle)?;
    let normalized_fmt = plan["format"].as_str().unwrap();
    let clean_title = plan["title"].as_str().unwrap();
    let clean_subtitle = plan["subtitle"].as_str().unwrap();
    let normalized_sections = normalize_sections(sections)?;
    let theme = theme_for(clean_title);
    let bytes = if normalized_fmt == "docx" {
        render_docx(clean_title, clean_subtitle, &normalized_sections, &theme)
    } else {
        render_pdf(clean_title, clean_subtitle, &normalized_sections, &theme)
    };
    let stored = store_generated_file(
        clean_title,
        normalized_fmt,
        root,
        entropy,
        now_epoch,
        |path| std::fs::write(path, &bytes),
    )?;
    Ok(document_result(
        &stored,
        normalized_fmt,
        clean_title,
        &normalized_sections,
    ))
}

fn xml_escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

fn w_t(text: &str, size: &str, color: &str, bold: bool) -> String {
    let b = if bold { "<w:b/>" } else { "" };
    format!(
        "<w:r><w:rPr>{b}<w:rFonts w:ascii=\"微软雅黑\" w:eastAsia=\"微软雅黑\" w:hAnsi=\"微软雅黑\"/><w:sz w:val=\"{size}\"/><w:szCs w:val=\"{size}\"/><w:color w:val=\"{color}\"/></w:rPr><w:t xml:space=\"preserve\">{}</w:t></w:r>",
        xml_escape(text)
    )
}

fn w_p(inner: &str, extra_pr: &str) -> String {
    format!("<w:p><w:pPr>{extra_pr}</w:pPr>{inner}</w:p>")
}

fn render_docx(title: &str, subtitle: &str, sections: &[Section], theme: &Theme) -> Vec<u8> {
    let mut body = String::new();
    body.push_str(&w_p(
        &w_t(title, "48", &theme.dark, true),
        "<w:spacing w:after=\"40\"/>",
    ));
    if !subtitle.is_empty() {
        body.push_str(&w_p(
            &w_t(subtitle, "25", MUTED, false),
            "<w:spacing w:after=\"80\"/>",
        ));
    }
    body.push_str(&w_p(
        "",
        &format!(
            "<w:pBdr><w:bottom w:val=\"single\" w:sz=\"18\" w:space=\"2\" w:color=\"{}\"/></w:pBdr><w:spacing w:after=\"200\"/>",
            theme.primary
        ),
    ));
    for (index, section) in sections.iter().enumerate() {
        let heading = format!("{}. {}", index + 1, section.heading);
        body.push_str(&w_p(
            &w_t(&heading, "30", &theme.dark, true),
            &format!(
                "<w:pBdr><w:bottom w:val=\"single\" w:sz=\"8\" w:space=\"2\" w:color=\"{}\"/></w:pBdr><w:spacing w:before=\"240\" w:after=\"80\"/>",
                theme.rule
            ),
        ));
        for paragraph in &section.body {
            body.push_str(&w_p(
                &w_t(paragraph, "21", INK, false),
                "<w:jc w:val=\"both\"/><w:spacing w:after=\"120\" w:line=\"312\" w:lineRule=\"auto\"/>",
            ));
        }
        for bullet in &section.bullets {
            body.push_str(&w_p(
                &w_t(bullet, "21", INK, false),
                "<w:numPr><w:ilvl w:val=\"0\"/><w:numId w:val=\"1\"/></w:numPr><w:spacing w:after=\"60\"/>",
            ));
        }
        if let Some(table) = &section.table {
            body.push_str(&w_table(table, theme));
            body.push_str(&w_p("", "<w:spacing w:after=\"80\"/>"));
        }
    }
    body.push_str(
        "<w:sectPr><w:footerReference w:type=\"default\" r:id=\"rId1\"/><w:pgMar w:top=\"1250\" w:right=\"1361\" w:bottom=\"1250\" w:left=\"1361\"/></w:sectPr>",
    );
    let document = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\" xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\"><w:body>{body}</w:body></w:document>"
    );
    let footer = format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?><w:ftr xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:p><w:pPr><w:jc w:val=\"center\"/></w:pPr>{}{}{}{}{}</w:p></w:ftr>",
        w_t("第 ", "18", MUTED, false),
        "<w:r><w:rPr><w:sz w:val=\"18\"/><w:color w:val=\"6B7280\"/></w:rPr><w:fldChar w:fldCharType=\"begin\"/></w:r>",
        "<w:r><w:rPr><w:sz w:val=\"18\"/><w:color w:val=\"6B7280\"/></w:rPr><w:instrText xml:space=\"preserve\">PAGE</w:instrText></w:r>",
        "<w:r><w:rPr><w:sz w:val=\"18\"/><w:color w:val=\"6B7280\"/></w:rPr><w:fldChar w:fldCharType=\"end\"/></w:r>",
        w_t(" 页", "18", MUTED, false),
    );
    let numbering = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:abstractNum w:abstractNumId="0"><w:nsid w:val="12345678"/><w:multiLevelType w:val="hybridMultilevel"/><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>"#;
    let content_types = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/><Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/></Types>"#;
    let rels = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>"#;
    let document_rels = r#"<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/></Relationships>"#;
    zip_store(&[
        ("[Content_Types].xml", content_types.as_bytes().to_vec()),
        ("_rels/.rels", rels.as_bytes().to_vec()),
        ("word/document.xml", document.into_bytes()),
        (
            "word/_rels/document.xml.rels",
            document_rels.as_bytes().to_vec(),
        ),
        ("word/footer1.xml", footer.into_bytes()),
        ("word/numbering.xml", numbering.as_bytes().to_vec()),
    ])
}

fn w_table(table: &TableModel, theme: &Theme) -> String {
    let col_count = if table.headers.is_empty() {
        table.rows.first().map(Vec::len).unwrap_or(0)
    } else {
        table.headers.len()
    };
    if col_count == 0 {
        return String::new();
    }
    let mut grid = String::new();
    for _ in 0..col_count {
        grid.push_str("<w:gridCol w:w=\"2000\"/>");
    }
    let mut rows_xml = String::new();
    if !table.headers.is_empty() {
        rows_xml.push_str(&w_table_row(&table.headers, true, false, theme));
    }
    for (index, row) in table.rows.iter().enumerate() {
        rows_xml.push_str(&w_table_row(row, false, index % 2 == 1, theme));
    }
    format!(
        "<w:tbl><w:tblPr><w:tblW w:w=\"5000\" w:type=\"pct\"/><w:jc w:val=\"center\"/><w:tblBorders><w:top w:val=\"single\" w:sz=\"4\" w:color=\"{rule}\"/><w:left w:val=\"single\" w:sz=\"4\" w:color=\"{rule}\"/><w:bottom w:val=\"single\" w:sz=\"4\" w:color=\"{rule}\"/><w:right w:val=\"single\" w:sz=\"4\" w:color=\"{rule}\"/><w:insideH w:val=\"single\" w:sz=\"4\" w:color=\"{rule}\"/><w:insideV w:val=\"single\" w:sz=\"4\" w:color=\"{rule}\"/></w:tblBorders></w:tblPr><w:tblGrid>{grid}</w:tblGrid>{rows_xml}</w:tbl>",
        rule = theme.rule
    )
}

fn w_table_row(cells: &[String], header: bool, band: bool, theme: &Theme) -> String {
    let mut xml = String::from("<w:tr>");
    for cell in cells {
        let fill = if header {
            theme.primary.as_str()
        } else if band {
            BAND
        } else {
            "FFFFFF"
        };
        let color = if header { "FFFFFF" } else { INK };
        xml.push_str(&format!(
            "<w:tc><w:tcPr><w:shd w:val=\"clear\" w:color=\"auto\" w:fill=\"{fill}\"/></w:tcPr>{}</w:tc>",
            w_p(&w_t(cell, "20", color, header), "")
        ));
    }
    xml.push_str("</w:tr>");
    xml
}

fn pdf_escape_literal(text: &str) -> String {
    let mut hex = String::new();
    for ch in text.chars() {
        let code = ch as u32;
        if code <= 0xFFFF {
            hex.push_str(&format!("{code:04X}"));
        } else {
            let adjusted = code - 0x1_0000;
            let high = 0xD800 + (adjusted >> 10);
            let low = 0xDC00 + (adjusted & 0x3FF);
            hex.push_str(&format!("{high:04X}{low:04X}"));
        }
    }
    hex
}

fn is_cjk(ch: char) -> bool {
    (ch as u32) > 0x2E7F
}

fn wrap_pdf(text: &str, max_width: f64, font_size: f64) -> Vec<String> {
    let mut lines = Vec::new();
    let mut current = String::new();
    let mut width = 0.0;
    for ch in text.chars() {
        let add = font_size * if is_cjk(ch) { 1.0 } else { 0.5 };
        if !current.is_empty() && width + add > max_width {
            lines.push(std::mem::take(&mut current));
            width = 0.0;
        }
        current.push(ch);
        width += add;
    }
    if !current.is_empty() || lines.is_empty() {
        lines.push(current);
    }
    lines
}

fn render_pdf(title: &str, subtitle: &str, sections: &[Section], theme: &Theme) -> Vec<u8> {
    const PAGE_W: f64 = 595.0;
    const PAGE_H: f64 = 842.0;
    const MARGIN: f64 = 56.7;
    let usable = PAGE_W - 2.0 * MARGIN;
    let mut pages: Vec<String> = Vec::new();
    let mut ops = String::new();
    let mut y = PAGE_H - MARGIN - 12.0;

    let new_page = |ops: &mut String, y: &mut f64, pages: &mut Vec<String>| {
        pages.push(std::mem::take(ops));
        *y = PAGE_H - MARGIN - 28.0;
    };

    let draw = |ops: &mut String,
                y: &mut f64,
                pages: &mut Vec<String>,
                text: &str,
                size: f64,
                r: f64,
                g: f64,
                b: f64| {
        let lines = wrap_pdf(text, usable, size);
        for line in lines {
            if *y < MARGIN + 40.0 {
                new_page(ops, y, pages);
            }
            ops.push_str(&format!(
                "BT /F1 {size:.1} Tf {r:.3} {g:.3} {b:.3} rg {} {} Td <{}> Tj ET\n",
                MARGIN,
                *y,
                pdf_escape_literal(&line)
            ));
            *y -= size + 6.0;
        }
    };

    let hex_rgb = |color: &str| -> (f64, f64, f64) {
        let n = u32::from_str_radix(color, 16).unwrap_or(0);
        (
            ((n >> 16) & 0xFF) as f64 / 255.0,
            ((n >> 8) & 0xFF) as f64 / 255.0,
            (n & 0xFF) as f64 / 255.0,
        )
    };
    let (dr, dg, db) = hex_rgb(&theme.dark);
    let (pr, pg, pb) = hex_rgb(&theme.primary);
    let (rr, rg, rb) = hex_rgb(&theme.rule);
    let (ir, ig, ib) = hex_rgb(INK);
    let (mr, mg, mb) = hex_rgb(MUTED);

    draw(&mut ops, &mut y, &mut pages, title, 24.0, dr, dg, db);
    if !subtitle.is_empty() {
        draw(&mut ops, &mut y, &mut pages, subtitle, 12.5, mr, mg, mb);
    }
    ops.push_str(&format!(
        "{pr:.3} {pg:.3} {pb:.3} RG 2 w {MARGIN} {} m {} {} l S\n",
        y,
        PAGE_W - MARGIN,
        y
    ));
    y -= 16.0;
    for (index, section) in sections.iter().enumerate() {
        draw(
            &mut ops,
            &mut y,
            &mut pages,
            &format!("{}. {}", index + 1, section.heading),
            15.0,
            dr,
            dg,
            db,
        );
        ops.push_str(&format!(
            "{rr:.3} {rg:.3} {rb:.3} RG 0.6 w {MARGIN} {} m {} {} l S\n",
            y,
            PAGE_W - MARGIN,
            y
        ));
        y -= 10.0;
        for paragraph in &section.body {
            draw(&mut ops, &mut y, &mut pages, paragraph, 10.5, ir, ig, ib);
        }
        for bullet in &section.bullets {
            draw(
                &mut ops,
                &mut y,
                &mut pages,
                &format!("• {bullet}"),
                10.5,
                ir,
                ig,
                ib,
            );
        }
        if let Some(table) = &section.table {
            let mut rows = Vec::new();
            if !table.headers.is_empty() {
                rows.push(table.headers.join(" | "));
            }
            for row in &table.rows {
                rows.push(row.join(" | "));
            }
            for row in rows {
                draw(&mut ops, &mut y, &mut pages, &row, 9.5, ir, ig, ib);
            }
        }
        y -= 8.0;
    }
    pages.push(ops);

    let mut decorated = Vec::new();
    for (index, content) in pages.iter().enumerate() {
        let mut page = content.clone();
        page.push_str(&format!(
            "{rr:.3} {rg:.3} {rb:.3} RG 0.5 w {MARGIN} 42.5 m {} 42.5 l S\n",
            PAGE_W - MARGIN
        ));
        page.push_str(&format!(
            "BT /F1 8.5 Tf {mr:.3} {mg:.3} {mb:.3} rg {} 30 Td <{}> Tj ET\n",
            PAGE_W / 2.0 - 20.0,
            pdf_escape_literal(&format!("第 {} 页", index + 1))
        ));
        if index > 0 {
            page.push_str(&format!(
                "BT /F1 8.5 Tf {mr:.3} {mg:.3} {mb:.3} rg {MARGIN} {} Td <{}> Tj ET\n",
                PAGE_H - 40.0,
                pdf_escape_literal(&title.chars().take(60).collect::<String>())
            ));
        }
        decorated.push(page);
    }

    build_pdf(&decorated)
}

fn build_pdf(pages: &[String]) -> Vec<u8> {
    let mut objects: Vec<Vec<u8>> = Vec::new();
    objects.push(b"<< /Type /Catalog /Pages 2 0 R >>".to_vec());
    let kids: String = (0..pages.len())
        .map(|index| format!("{} 0 R", 3 + index * 2))
        .collect::<Vec<_>>()
        .join(" ");
    objects.push(format!("<< /Type /Pages /Kids [{kids}] /Count {} >>", pages.len()).into_bytes());
    let font_obj = 3 + pages.len() * 2;
    let cid_obj = font_obj + 1;
    let desc_obj = font_obj + 2;
    for (index, content) in pages.iter().enumerate() {
        let content_id = 4 + index * 2;
        let page_id_index = objects.len();
        objects.push(
            format!(
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {content_id} 0 R /Resources << /Font << /F1 {font_obj} 0 R >> >> >>"
            )
            .into_bytes(),
        );
        let _ = page_id_index;
        let stream = content.as_bytes();
        let mut obj = format!("<< /Length {} >>\nstream\n", stream.len()).into_bytes();
        obj.extend_from_slice(stream);
        obj.extend_from_slice(b"\nendstream");
        objects.push(obj);
    }
    objects.push(
        format!(
            "<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /UniGB-UCS2-H /DescendantFonts [{cid_obj} 0 R] >>"
        )
        .into_bytes(),
    );
    objects.push(
        format!(
            "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light /CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> /FontDescriptor {desc_obj} 0 R >>"
        )
        .into_bytes(),
    );
    objects.push(
        b"<< /Type /FontDescriptor /FontName /STSong-Light /Flags 6 /FontBBox [-1000 -200 1000 900] /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 800 /StemV 80 >>".to_vec(),
    );

    let mut out = b"%PDF-1.4\n".to_vec();
    let mut offsets = vec![0u32; objects.len() + 1];
    for (index, object) in objects.iter().enumerate() {
        offsets[index + 1] = out.len() as u32;
        writeln!(&mut out, "{} 0 obj", index + 1).unwrap();
        out.extend_from_slice(object);
        out.extend_from_slice(b"\nendobj\n");
    }
    let xref = out.len();
    write!(&mut out, "xref\n0 {}\n", objects.len() + 1).unwrap();
    out.extend_from_slice(b"0000000000 65535 f \n");
    for offset in offsets.iter().skip(1) {
        writeln!(&mut out, "{offset:010} 00000 n ").unwrap();
    }
    write!(
        &mut out,
        "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
        objects.len() + 1
    )
    .unwrap();
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::entropy::SystemEntropy;
    use crate::generated_files::{self, resolve_generated_file};

    fn sample_sections() -> Value {
        json!([
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
        ])
    }

    #[test]
    fn format_aliases_and_refusals() {
        assert_eq!(normalize_format("word").unwrap(), "docx");
        assert_eq!(normalize_format("PDF").unwrap(), "pdf");
        assert_eq!(
            normalize_format("txt").unwrap_err().message,
            "format 必须是 docx（Word）或 pdf。"
        );
        let root = std::env::temp_dir().join(format!("doc-empty-{}", std::process::id()));
        let err = create_document(
            "docx",
            "",
            &sample_sections(),
            "",
            &root,
            &SystemEntropy,
            0.0,
        )
        .unwrap_err();
        assert_eq!(err.message, "文档需要一个标题（title）。");
        let err = create_document("docx", "标题", &json!([]), "", &root, &SystemEntropy, 0.0)
            .unwrap_err();
        assert_eq!(err.message, "文档至少需要一个章节（sections）。");
    }

    #[test]
    fn create_word_document_is_a_zip_with_document_xml() {
        let root = std::env::temp_dir().join(format!("doc-docx-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let result = create_document(
            "docx",
            "季度产品报告",
            &sample_sections(),
            "2026 Q3",
            &root,
            &SystemEntropy,
            generated_files::system_now(),
        )
        .unwrap();
        assert_eq!(result["format"], "docx");
        assert_eq!(result["sectionCount"], 2);
        assert!(result["filename"].as_str().unwrap().ends_with(".docx"));
        let path = resolve_generated_file(&root, result["fileId"].as_str().unwrap()).unwrap();
        let bytes = std::fs::read(&path).unwrap();
        assert_eq!(&bytes[..4], b"PK\x03\x04");
        let xml = String::from_utf8_lossy(&bytes);
        assert!(xml.contains("word/document.xml"));
        assert!(xml.contains("季度产品报告"));
        assert!(xml.contains("概述"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn create_pdf_document_starts_with_pdf_magic_and_keeps_cjk() {
        let root = std::env::temp_dir().join(format!("doc-pdf-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let result = create_document(
            "pdf",
            "季度产品报告",
            &sample_sections(),
            "",
            &root,
            &SystemEntropy,
            generated_files::system_now(),
        )
        .unwrap();
        assert_eq!(result["format"], "pdf");
        let path = resolve_generated_file(&root, result["fileId"].as_str().unwrap()).unwrap();
        let bytes = std::fs::read(&path).unwrap();
        assert_eq!(&bytes[..5], b"%PDF-");
        let hex = pdf_escape_literal("季度产品报告");
        let as_text = String::from_utf8_lossy(&bytes);
        assert!(as_text.contains(&hex), "PDF missing UCS-2 title");
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn ragged_tables_are_padded_not_dropped() {
        let sections = json!([{
            "heading": "数据",
            "body": [],
            "bullets": [],
            "table": {"headers": ["A", "B", "C"], "rows": [["1"], ["1", "2", "3", "4", "5"]]}
        }]);
        let plan = document_plan("docx", "表格测试", &sections, "").unwrap();
        assert_eq!(
            plan["sections"][0]["table"]["headers"],
            json!(["A", "B", "C", "", ""])
        );
        assert_eq!(
            plan["sections"][0]["table"]["rows"][0],
            json!(["1", "", "", "", ""])
        );
        assert_eq!(
            plan["sections"][0]["table"]["rows"][1],
            json!(["1", "2", "3", "4", "5"])
        );
    }
}
