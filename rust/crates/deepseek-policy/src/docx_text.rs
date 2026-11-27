//! Office XML text extraction for DOCX, PPTX and XLSX.
//!
//! The oracle opens the ZIP, checks entry sizes and the compression ratio, then
//! reads the package parts. Element text is the text before the first child,
//! which is what `xml.etree` stores in `node.text`. XLSX follows the installed
//! openpyxl path (`read_only`, `data_only`): a package openpyxl cannot load is
//! `Invalid xlsx file`, and cell text is the same `str(cell.value)` the oracle
//! joins into `Sheet:` / `行` lines.

use std::collections::BTreeMap;
use std::io::{Cursor, Read};

use chrono::{Duration, NaiveDate, NaiveDateTime, NaiveTime, Timelike};
use zip::ZipArchive;

use crate::app_error::{AppError, codes};

const MAX_ZIP_ENTRY_BYTES: u64 = 20_000_000;
const MAX_ZIP_TOTAL_BYTES: u64 = 120_000_000;
const MAX_ZIP_COMPRESSION_RATIO: f64 = 100.0;
struct Node {
    local: String,
    text: String,
    attrs: Vec<(String, String)>,
    children: Vec<Node>,
}

pub fn extract_docx_text(data: &[u8]) -> Result<String, AppError> {
    let invalid = invalid_office("docx");
    let mut archive = ZipArchive::new(Cursor::new(data)).map_err(|_| invalid.clone())?;
    let entries = zip_entries(&mut archive, &invalid)?;
    let mut names = vec!["word/document.xml".to_string()];
    let mut extras: Vec<String> = entries
        .iter()
        .map(|(name, _, _)| name.clone())
        .filter(|name| is_header_or_footer(name))
        .collect();
    extras.sort();
    names.extend(extras);
    let mut blocks = Vec::new();
    for name in names {
        if !entries.iter().any(|(entry, _, _)| entry == &name) {
            continue;
        }
        let xml = read_entry(&mut archive, &name, &invalid)?;
        let root = parse_office_xml(&xml, &invalid_office_xml("docx"))?;
        blocks.push(extract_word_xml_text(&root));
    }
    Ok(blocks
        .into_iter()
        .filter(|block| !block.trim().is_empty())
        .collect::<Vec<_>>()
        .join("\n\n"))
}

/// `extract_pptx_text`. Slide order is the numeric `slideN.xml` key, while the
/// visible page number is the position in that list, including slides that have
/// no text and are therefore omitted from the body.
pub fn extract_pptx_text(data: &[u8]) -> Result<String, AppError> {
    let invalid = invalid_office("pptx");
    let mut archive = ZipArchive::new(Cursor::new(data)).map_err(|_| invalid.clone())?;
    let entries = zip_entries(&mut archive, &invalid)?;
    let mut slide_names: Vec<String> = entries
        .iter()
        .map(|(name, _, _)| name.clone())
        .filter(|name| is_slide(name))
        .collect();
    slide_names.sort_by_key(|name| slide_number(name));
    let invalid_xml = invalid_office_xml("pptx");
    let mut slides = Vec::new();
    for (index, name) in slide_names.iter().enumerate() {
        let xml = read_entry(&mut archive, name, &invalid)?;
        let root = parse_office_xml(&xml, &invalid_xml)?;
        let text = presentation_text(&root);
        if !text.is_empty() {
            slides.push(format!("[PPTX 第 {} 页]\n{text}", index + 1));
        }
    }
    Ok(slides.join("\n\n"))
}

/// `extract_epub_text`. Chapter order is lexicographic. `nav.xhtml` and
/// `toc.xhtml` are omitted. Each remaining HTML part is the same extractor the
/// `.html` upload uses.
pub fn extract_epub_text(data: &[u8]) -> Result<String, AppError> {
    let invalid = invalid_office("epub");
    let mut archive = ZipArchive::new(Cursor::new(data)).map_err(|_| invalid.clone())?;
    let entries = zip_entries(&mut archive, &invalid)?;
    let mut names: Vec<String> = entries
        .iter()
        .map(|(name, _, _)| name.clone())
        .filter(|name| is_epub_chapter(name))
        .collect();
    names.sort();
    let mut sections = Vec::new();
    for name in &names {
        let html = read_entry(&mut archive, name, &invalid)?;
        let text = crate::fetch_url::extract_html_text(&html);
        if !text.is_empty() {
            sections.push(format!("[EPUB: {name}]\n{text}"));
        }
    }
    Ok(sections.join("\n\n"))
}

fn is_epub_chapter(name: &str) -> bool {
    let lower = name.to_lowercase();
    let html = lower.ends_with(".xhtml") || lower.ends_with(".html") || lower.ends_with(".htm");
    html && !lower.ends_with("nav.xhtml") && !lower.ends_with("toc.xhtml")
}

fn invalid_office(kind: &str) -> AppError {
    AppError {
        message: format!("Invalid {kind} file"),
        code: codes::INVALID_PAYLOAD,
        status: 422,
    }
}

fn invalid_office_xml(kind: &str) -> AppError {
    AppError {
        message: format!("Invalid {kind} XML"),
        code: codes::INVALID_PAYLOAD,
        status: 422,
    }
}

fn is_slide(name: &str) -> bool {
    let Some(rest) = name.strip_prefix("ppt/slides/slide") else {
        return false;
    };
    let digits = rest
        .bytes()
        .take_while(|byte| byte.is_ascii_digit())
        .count();
    digits > 0 && rest[digits..].starts_with(".xml")
}

/// `slide(\d+)\.xml$`.
fn slide_number(name: &str) -> u64 {
    let Some(start) = name.rfind("slide") else {
        return 0;
    };
    let rest = &name[start + "slide".len()..];
    let digits: String = rest
        .chars()
        .take_while(|character| character.is_ascii_digit())
        .collect();
    if digits.is_empty() || &rest[digits.len()..] != ".xml" {
        return 0;
    }
    digits.parse().unwrap_or(0)
}

fn presentation_text(root: &Node) -> String {
    descendants(root)
        .into_iter()
        .filter(|node| node.local == "t" && !node.text.is_empty())
        .map(|node| node.text.trim().to_string())
        .filter(|part| !part.is_empty())
        .collect::<Vec<_>>()
        .join("\n")
}

fn too_large(message: String) -> AppError {
    AppError {
        message,
        code: codes::UPLOAD_TOO_LARGE,
        status: 413,
    }
}

fn is_header_or_footer(name: &str) -> bool {
    let Some(rest) = name.strip_prefix("word/") else {
        return false;
    };
    let Some(rest) = rest
        .strip_prefix("header")
        .or_else(|| rest.strip_prefix("footer"))
    else {
        return false;
    };
    let Some(number) = rest.strip_suffix(".xml") else {
        return false;
    };
    !number.is_empty() && number.bytes().all(|byte| byte.is_ascii_digit())
}

fn zip_entries(
    archive: &mut ZipArchive<Cursor<&[u8]>>,
    invalid_file: &AppError,
) -> Result<Vec<(String, u64, u64)>, AppError> {
    let mut entries = Vec::new();
    let mut total = 0u64;
    let mut compressed_total = 0u64;
    for index in 0..archive.len() {
        let file = archive.by_index(index).map_err(|_| invalid_file.clone())?;
        let size = file.size();
        let compressed = file.compressed_size();
        if size > MAX_ZIP_ENTRY_BYTES {
            return Err(too_large(format!(
                "File entry is too large: {}",
                file.name()
            )));
        }
        total = total.saturating_add(size);
        compressed_total = compressed_total.saturating_add(compressed);
        entries.push((file.name().to_string(), size, compressed));
    }
    if total > MAX_ZIP_TOTAL_BYTES {
        return Err(too_large(
            "Compressed document is too large after extraction".to_string(),
        ));
    }
    if total > 0 && compressed_total == 0 {
        return Err(too_large(
            "Compressed document has an unsafe compression ratio".to_string(),
        ));
    }
    if compressed_total > 0
        && (total as f64) / (compressed_total as f64) > MAX_ZIP_COMPRESSION_RATIO
    {
        return Err(too_large(
            "Compressed document has an unsafe compression ratio".to_string(),
        ));
    }
    Ok(entries)
}

fn read_entry(
    archive: &mut ZipArchive<Cursor<&[u8]>>,
    name: &str,
    invalid_file: &AppError,
) -> Result<Vec<u8>, AppError> {
    let mut file = archive.by_name(name).map_err(|_| AppError {
        message: format!("Missing file entry: {name}"),
        code: codes::INVALID_PAYLOAD,
        status: 422,
    })?;
    if file.size() > MAX_ZIP_ENTRY_BYTES {
        return Err(too_large(format!("File entry is too large: {name}")));
    }
    let mut buffer = Vec::new();
    file.read_to_end(&mut buffer)
        .map_err(|_| invalid_file.clone())?;
    Ok(buffer)
}

fn event_attributes(
    event: &quick_xml::events::BytesStart<'_>,
) -> Result<Vec<(String, String)>, ()> {
    let mut attrs = Vec::new();
    for attr in event.attributes() {
        let attr = attr.map_err(|_| ())?;
        let key = local_name(attr.key.as_ref());
        let value = attr.unescape_value().map_err(|_| ())?.into_owned();
        attrs.push((key, value));
    }
    Ok(attrs)
}

fn parse_office_xml(xml: &[u8], invalid_xml: &AppError) -> Result<Node, AppError> {
    let mut reader = quick_xml::Reader::from_reader(xml);
    reader.trim_text(false);
    let mut stack = vec![Node {
        local: String::new(),
        text: String::new(),
        attrs: Vec::new(),
        children: Vec::new(),
    }];
    let mut buffer = Vec::new();
    loop {
        match reader.read_event_into(&mut buffer) {
            Ok(quick_xml::events::Event::Start(event)) => {
                let attrs = event_attributes(&event).map_err(|_| invalid_xml.clone())?;
                stack.push(Node {
                    local: local_name(event.name().as_ref()),
                    text: String::new(),
                    attrs,
                    children: Vec::new(),
                });
            }
            Ok(quick_xml::events::Event::Empty(event)) => {
                let attrs = event_attributes(&event).map_err(|_| invalid_xml.clone())?;
                stack.last_mut().expect("xml stack").children.push(Node {
                    local: local_name(event.name().as_ref()),
                    text: String::new(),
                    attrs,
                    children: Vec::new(),
                });
            }
            Ok(quick_xml::events::Event::Text(event)) => {
                let text = event
                    .unescape()
                    .map_err(|_| invalid_xml.clone())?
                    .into_owned();
                let node = stack.last_mut().expect("xml stack");
                if node.children.is_empty() {
                    node.text.push_str(&text);
                }
            }
            Ok(quick_xml::events::Event::CData(event)) => {
                let node = stack.last_mut().expect("xml stack");
                if node.children.is_empty() {
                    node.text.push_str(&String::from_utf8_lossy(event.as_ref()));
                }
            }
            Ok(quick_xml::events::Event::End(_)) => {
                let finished = stack.pop().ok_or_else(|| invalid_xml.clone())?;
                if stack.is_empty() {
                    return Err(invalid_xml.clone());
                }
                stack.last_mut().expect("xml stack").children.push(finished);
            }
            Ok(quick_xml::events::Event::Eof) => {
                // The synthetic root is the only frame a finished document leaves.
                // An unclosed start tag is `ParseError` in ElementTree.
                if stack.len() != 1 {
                    return Err(invalid_xml.clone());
                }
                break;
            }
            Err(_) => return Err(invalid_xml.clone()),
            _ => {}
        }
        buffer.clear();
    }
    let mut root = stack.pop().ok_or_else(|| invalid_xml.clone())?;
    if stack.is_empty() && root.children.len() == 1 {
        root = root.children.pop().expect("one document root");
    }
    Ok(root)
}

fn local_name(qname: &[u8]) -> String {
    let name = String::from_utf8_lossy(qname);
    let after_namespace = name.rsplit_once('}').map(|(_, rest)| rest).unwrap_or(&name);
    after_namespace
        .rsplit_once(':')
        .map(|(_, rest)| rest)
        .unwrap_or(after_namespace)
        .to_string()
}

fn extract_word_xml_text(root: &Node) -> String {
    let nodes = find_direct_child(root, "body")
        .map(|body| body.children.as_slice())
        .unwrap_or(root.children.as_slice());
    let mut lines = Vec::new();
    for node in nodes {
        if node.local == "p" {
            let line = paragraph_text(node);
            if !line.is_empty() {
                lines.push(line);
            }
        } else if node.local == "tbl" {
            let table = table_text(node);
            if !table.is_empty() {
                lines.push(table);
            }
        }
    }
    lines.join("\n")
}

fn paragraph_text(paragraph: &Node) -> String {
    let mut parts = Vec::new();
    for node in descendants(paragraph) {
        if node.local == "t" && !node.text.is_empty() {
            parts.push(node.text.as_str());
        } else if node.local == "tab" {
            parts.push("\t");
        } else if node.local == "br" || node.local == "cr" {
            parts.push("\n");
        }
    }
    parts.concat().trim().to_string()
}

fn table_text(table: &Node) -> String {
    let mut rows = Vec::new();
    for row in descendants(table)
        .into_iter()
        .filter(|node| node.local == "tr")
    {
        let mut cells = Vec::new();
        for cell in descendants(row)
            .into_iter()
            .filter(|node| node.local == "tc")
        {
            let paragraphs: Vec<String> = descendants(cell)
                .into_iter()
                .filter(|node| node.local == "p")
                .map(paragraph_text)
                .filter(|part| !part.is_empty())
                .collect();
            cells.push(paragraphs.join(" / "));
        }
        if cells.iter().any(|cell| !cell.trim().is_empty()) {
            rows.push(cells.join("\t").trim_end().to_string());
        }
    }
    rows.join("\n")
}

fn find_direct_child<'a>(node: &'a Node, local: &str) -> Option<&'a Node> {
    node.children.iter().find(|child| child.local == local)
}

/// `Element.iter`, including the node itself, in document order.
fn descendants(node: &Node) -> Vec<&Node> {
    let mut out = Vec::new();
    fn walk<'a>(node: &'a Node, out: &mut Vec<&'a Node>) {
        out.push(node);
        for child in &node.children {
            walk(child, out);
        }
    }
    walk(node, &mut out);
    out
}

const XLSX_CONTENT: &str =
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml";
const XLSM_CONTENT: &str = "application/vnd.ms-excel.sheet.macroEnabled.main+xml";
const XLTX_CONTENT: &str =
    "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml";
const XLTM_CONTENT: &str = "application/vnd.ms-excel.template.macroEnabled.main+xml";
const SHARED_STRINGS_CONTENT: &str =
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml";
const WORKBOOK_CONTENT_TYPES: [&str; 4] = [XLTM_CONTENT, XLTX_CONTENT, XLSM_CONTENT, XLSX_CONTENT];

#[derive(Clone, Copy)]
struct SheetBounds {
    min_col: u32,
    min_row: u32,
    max_col: u32,
    max_row: u32,
}

struct WorkbookRel {
    id: String,
    kind: String,
    target: String,
    external: bool,
}

enum ExcelNumber {
    Int(String),
    Float(f64),
}

/// `extract_xlsx_text` when openpyxl is installed.
///
/// `requirements.txt` ships openpyxl, so the ZIP/XML fallback in the oracle is
/// not the behavior a production process produces. Load failures are
/// `Invalid xlsx file`. A sheet contributes text only when a cell value survives
/// `str(value).strip()`.
pub fn extract_xlsx_text(data: &[u8]) -> Result<String, AppError> {
    let invalid = invalid_office("xlsx");
    let mut archive = ZipArchive::new(Cursor::new(data)).map_err(|_| invalid.clone())?;
    let entries = zip_entries(&mut archive, &invalid)?;
    let names: Vec<String> = entries.into_iter().map(|(name, _, _)| name).collect();
    let content = read_required(&mut archive, "[Content_Types].xml", &invalid)?;
    let content_root = parse_office_xml(&content, &invalid)?;
    let workbook_name = workbook_part(&content_root).ok_or_else(|| invalid.clone())?;
    if !names.iter().any(|name| name == &workbook_name) {
        return Err(invalid);
    }
    let workbook_xml = read_required(&mut archive, &workbook_name, &invalid)?;
    let workbook = parse_office_xml(&workbook_xml, &invalid)?;
    let mac_1904 = excel_flag(attr(find_first(&workbook, "workbookPr"), "date1904"));
    let rels_name = rels_path(&workbook_name);
    if !names.iter().any(|name| name == &rels_name) {
        return Err(invalid);
    }
    let rels_xml = read_required(&mut archive, &rels_name, &invalid)?;
    let rels = workbook_rels(&rels_xml, &rels_name, &invalid)?;
    let shared = match shared_strings_part(&content_root) {
        Some(part) if names.iter().any(|name| name == &part) => {
            let xml = read_required(&mut archive, &part, &invalid)?;
            let root = parse_office_xml(&xml, &invalid)?;
            shared_string_table(&root)
        }
        Some(_) => return Err(invalid),
        None => Vec::new(),
    };
    let (date_styles, delta_styles) = if names.iter().any(|name| name == "xl/styles.xml") {
        let xml = read_required(&mut archive, "xl/styles.xml", &invalid)?;
        let root = parse_office_xml(&xml, &invalid)?;
        date_style_flags(&root)
    } else {
        (Vec::new(), Vec::new())
    };
    let mut sheets = Vec::new();
    let sheet_nodes = find_first(&workbook, "sheets")
        .map(|node| node.children.as_slice())
        .unwrap_or(&[]);
    for sheet in sheet_nodes.iter().filter(|node| node.local == "sheet") {
        let rel_id = attr(Some(sheet), "id").unwrap_or("");
        if rel_id.is_empty() {
            continue;
        }
        let Some(rel) = rels.iter().find(|rel| rel.id == rel_id) else {
            return Err(invalid);
        };
        if rel.external
            || rel.kind.contains("chartsheet")
            || !names.iter().any(|name| name == &rel.target)
        {
            continue;
        }
        let target = rel.target.clone();
        let title = attr(Some(sheet), "name").unwrap_or("").to_string();
        let xml = read_required(&mut archive, &target, &invalid)?;
        let root = parse_office_xml(&xml, &invalid)?;
        match sheet_text(
            &root,
            &title,
            &shared,
            &date_styles,
            &delta_styles,
            mac_1904,
        ) {
            Ok(Some(text)) => sheets.push(text),
            Ok(None) => {}
            Err(()) => return Err(invalid),
        }
    }
    Ok(sheets.join("\n\n"))
}

fn read_required(
    archive: &mut ZipArchive<Cursor<&[u8]>>,
    name: &str,
    invalid: &AppError,
) -> Result<Vec<u8>, AppError> {
    match read_entry(archive, name, invalid) {
        Ok(bytes) => Ok(bytes),
        Err(error) if error.status == 413 => Err(error),
        Err(_) => Err(invalid.clone()),
    }
}

fn attr<'a>(node: Option<&'a Node>, name: &str) -> Option<&'a str> {
    let node = node?;
    node.attrs
        .iter()
        .find(|(key, _)| key == name)
        .map(|(_, value)| value.as_str())
}

fn find_first<'a>(node: &'a Node, local: &str) -> Option<&'a Node> {
    descendants(node)
        .into_iter()
        .find(|item| item.local == local)
}

fn workbook_part(content_types: &Node) -> Option<String> {
    let overrides = content_overrides(content_types);
    for kind in WORKBOOK_CONTENT_TYPES {
        if let Some(part) = overrides
            .iter()
            .find(|(_, content)| *content == kind)
            .map(|(part, _)| part_path(part))
        {
            return Some(part);
        }
    }
    let defaults = content_defaults(content_types);
    for kind in WORKBOOK_CONTENT_TYPES {
        if defaults.iter().any(|content| content == kind) {
            return Some("xl/workbook.xml".to_string());
        }
    }
    None
}

fn shared_strings_part(content_types: &Node) -> Option<String> {
    content_overrides(content_types)
        .into_iter()
        .find(|(_, content)| content == SHARED_STRINGS_CONTENT)
        .map(|(part, _)| part_path(&part))
}

fn content_overrides(root: &Node) -> Vec<(String, String)> {
    descendants(root)
        .into_iter()
        .filter(|node| node.local == "Override")
        .filter_map(|node| {
            Some((
                attr(Some(node), "PartName")?.to_string(),
                attr(Some(node), "ContentType")?.to_string(),
            ))
        })
        .collect()
}

fn content_defaults(root: &Node) -> Vec<String> {
    descendants(root)
        .into_iter()
        .filter(|node| node.local == "Default")
        .filter_map(|node| attr(Some(node), "ContentType").map(str::to_string))
        .collect()
}

fn part_path(part_name: &str) -> String {
    part_name.strip_prefix('/').unwrap_or(part_name).to_string()
}

fn rels_path(part: &str) -> String {
    match part.rfind('/') {
        Some(index) => format!("{}/_rels/{}.rels", &part[..index], &part[index + 1..]),
        None => format!("_rels/{part}.rels"),
    }
}

fn rels_parent(rels_file: &str) -> String {
    let folder = match rels_file.rfind('/') {
        Some(index) => &rels_file[..index],
        None => "",
    };
    match folder.rfind('/') {
        Some(index) => folder[..index].to_string(),
        None => String::new(),
    }
}

fn zip_normpath(path: &str) -> String {
    let mut stack = Vec::new();
    for part in path.split('/') {
        if part.is_empty() || part == "." {
            continue;
        }
        if part == ".." {
            stack.pop();
        } else {
            stack.push(part);
        }
    }
    stack.join("/")
}

fn workbook_rels(
    xml: &[u8],
    rels_file: &str,
    invalid: &AppError,
) -> Result<Vec<WorkbookRel>, AppError> {
    let root = parse_office_xml(xml, invalid)?;
    let parent = rels_parent(rels_file);
    let mut rels = Vec::new();
    for node in root
        .children
        .iter()
        .filter(|node| node.local == "Relationship")
    {
        let target = attr(Some(node), "Target").unwrap_or("");
        let external = attr(Some(node), "TargetMode").unwrap_or("") == "External";
        let resolved = if external {
            target.to_string()
        } else if let Some(rest) = target.strip_prefix('/') {
            rest.to_string()
        } else if parent.is_empty() {
            zip_normpath(target)
        } else {
            zip_normpath(&format!("{parent}/{target}"))
        };
        rels.push(WorkbookRel {
            id: attr(Some(node), "Id").unwrap_or("").to_string(),
            kind: attr(Some(node), "Type").unwrap_or("").to_string(),
            target: resolved,
            external,
        });
    }
    Ok(rels)
}

fn shared_string_table(root: &Node) -> Vec<String> {
    root.children
        .iter()
        .filter(|node| node.local == "si")
        .map(|node| office_text_content(node).replace("x005F_", ""))
        .collect()
}

/// `Text.content`: the plain `t` plus each direct rich-text `r/t`. Phonetic
/// `rPh` runs are not part of the value openpyxl stringifies.
fn office_text_content(node: &Node) -> String {
    let mut snippets = Vec::new();
    if let Some(plain) = node.children.iter().find(|child| child.local == "t") {
        snippets.push(plain.text.as_str());
    }
    for child in node.children.iter().filter(|child| child.local == "r") {
        if let Some(text) = child.children.iter().find(|node| node.local == "t") {
            snippets.push(text.text.as_str());
        }
    }
    snippets.concat()
}

fn date_style_flags(styles: &Node) -> (Vec<bool>, Vec<bool>) {
    let mut custom = Vec::new();
    if let Some(formats) = find_first(styles, "numFmts") {
        for format in formats
            .children
            .iter()
            .filter(|node| node.local == "numFmt")
        {
            let Some(id) =
                attr(Some(format), "numFmtId").and_then(|value| value.parse::<u32>().ok())
            else {
                continue;
            };
            custom.push((
                id,
                attr(Some(format), "formatCode").unwrap_or("").to_string(),
            ));
        }
    }
    let Some(cell_xfs) = find_first(styles, "cellXfs") else {
        return (Vec::new(), Vec::new());
    };
    let mut dates = Vec::new();
    let mut deltas = Vec::new();
    for xf in cell_xfs.children.iter().filter(|node| node.local == "xf") {
        let id = attr(Some(xf), "numFmtId")
            .and_then(|value| value.parse::<u32>().ok())
            .unwrap_or(0);
        let format = custom
            .iter()
            .find(|(fmt_id, _)| *fmt_id == id)
            .map(|(_, code)| code.as_str())
            .unwrap_or_else(|| builtin_format(id).unwrap_or(""));
        dates.push(!format.is_empty() && is_date_format(format));
        deltas.push(!format.is_empty() && is_timedelta_format(format));
    }
    (dates, deltas)
}

fn builtin_format(id: u32) -> Option<&'static str> {
    Some(match id {
        0 => "General",
        1 => "0",
        2 => "0.00",
        3 => "#,##0",
        4 => "#,##0.00",
        5 => "\"$\"#,##0_);(\"$\"#,##0)",
        6 => "\"$\"#,##0_);[Red](\"$\"#,##0)",
        7 => "\"$\"#,##0.00_);(\"$\"#,##0.00)",
        8 => "\"$\"#,##0.00_);[Red](\"$\"#,##0.00)",
        9 => "0%",
        10 => "0.00%",
        11 => "0.00E+00",
        12 => "# ?/?",
        13 => "# ??/??",
        14 => "mm-dd-yy",
        15 => "d-mmm-yy",
        16 => "d-mmm",
        17 => "mmm-yy",
        18 => "h:mm AM/PM",
        19 => "h:mm:ss AM/PM",
        20 => "h:mm",
        21 => "h:mm:ss",
        22 => "m/d/yy h:mm",
        37 => "#,##0_);(#,##0)",
        38 => "#,##0_);[Red](#,##0)",
        39 => "#,##0.00_);(#,##0.00)",
        40 => "#,##0.00_);[Red](#,##0.00)",
        41 => "_(* #,##0_);_(* \\(#,##0\\);_(* \"-\"_);_(@_)",
        42 => "_(\"$\"* #,##0_);_(\"$\"* \\(#,##0\\);_(\"$\"* \"-\"_);_(@_)",
        43 => "_(* #,##0.00_);_(* \\(#,##0.00\\);_(* \"-\"??_);_(@_)",
        44 => "_(\"$\"* #,##0.00_)_(\"$\"* \\(#,##0.00\\)_(\"$\"* \"-\"??_)_(@_)",
        45 => "mm:ss",
        46 => "[h]:mm:ss",
        47 => "mmss.0",
        48 => "##0.0E+0",
        49 => "@",
        _ => return None,
    })
}

fn is_date_format(format: &str) -> bool {
    let section = format.split(';').next().unwrap_or("");
    let stripped = strip_format_literals(section);
    let bytes = stripped.as_bytes();
    for (index, byte) in bytes.iter().copied().enumerate() {
        if !matches!(
            byte,
            b'd' | b'm' | b'h' | b'y' | b's' | b'D' | b'M' | b'H' | b'Y' | b'S'
        ) {
            continue;
        }
        let escaped = index > 0 && matches!(bytes[index - 1], b'_' | b'\\');
        if !escaped {
            return true;
        }
    }
    false
}

fn strip_format_literals(format: &str) -> String {
    let chars: Vec<char> = format.chars().collect();
    let mut out = String::new();
    let mut index = 0;
    while index < chars.len() {
        if chars[index] == '"' {
            index += 1;
            while index < chars.len() && chars[index] != '"' {
                index += 1;
            }
            if index < chars.len() {
                index += 1;
            }
            continue;
        }
        if chars[index] == '[' {
            if let Some(end) = chars[index + 1..].iter().position(|ch| *ch == ']') {
                let end = index + 1 + end;
                let inner: String = chars[index + 1..end].iter().collect();
                if matches!(inner.as_str(), "h" | "hh" | "m" | "mm" | "s" | "ss") {
                    out.push('[');
                    out.push_str(&inner);
                    out.push(']');
                }
                index = end + 1;
                continue;
            }
        }
        out.push(chars[index]);
        index += 1;
    }
    out
}

fn is_timedelta_format(format: &str) -> bool {
    let section = format.split(';').next().unwrap_or("").to_ascii_lowercase();
    ["[h]", "[hh]", "[m]", "[mm]", "[s]", "[ss]"]
        .iter()
        .any(|token| section.contains(token))
}

fn excel_flag(value: Option<&str>) -> bool {
    !matches!(value, None | Some("" | "false" | "f" | "0"))
}

fn sheet_text(
    sheet: &Node,
    title: &str,
    shared: &[String],
    date_styles: &[bool],
    delta_styles: &[bool],
    mac_1904: bool,
) -> Result<Option<String>, ()> {
    let bounds = sheet_bounds(sheet)?;
    let rows = find_first(sheet, "sheetData")
        .map(|node| node.children.as_slice())
        .unwrap_or(&[]);
    let mut lines = vec![format!("Sheet: {title}")];
    let mut row_counter = 0u32;
    for row in rows.iter().filter(|node| node.local == "row") {
        row_counter = match attr(Some(row), "r").filter(|value| !value.is_empty()) {
            Some(raw) => parse_row_number(raw)?,
            None => row_counter.saturating_add(1),
        };
        let mut by_column: BTreeMap<u32, (u32, String)> = BTreeMap::new();
        let mut col_counter = 0u32;
        for cell in row.children.iter().filter(|node| node.local == "c") {
            let (cell_row, cell_col) = match attr(Some(cell), "r").filter(|value| !value.is_empty())
            {
                Some(reference) => {
                    let (parsed_row, parsed_col) = coordinate_to_tuple(reference)?;
                    col_counter = parsed_col;
                    (parsed_row, parsed_col)
                }
                None => {
                    col_counter = col_counter.saturating_add(1);
                    (row_counter, col_counter)
                }
            };
            if let Some(bounds) = bounds {
                if cell_row < bounds.min_row
                    || cell_row > bounds.max_row
                    || cell_col < bounds.min_col
                    || cell_col > bounds.max_col
                {
                    continue;
                }
            }
            match cell_text(cell, shared, date_styles, delta_styles, mac_1904)? {
                Some(text) if !text.trim().is_empty() => {
                    by_column.insert(cell_col, (cell_row, text));
                }
                _ => {
                    by_column.remove(&cell_col);
                }
            }
        }
        let mut rendered = Vec::new();
        let mut row_number = None;
        for (column, (cell_row, text)) in by_column {
            if row_number.is_none() {
                row_number = Some(cell_row);
            }
            rendered.push(format!("{}{}={text}", column_letter(column), cell_row));
        }
        if rendered.is_empty() {
            continue;
        }
        let prefix = match row_number {
            Some(number) => format!("行 {number}"),
            None => "行".to_string(),
        };
        lines.push(format!("{prefix}\t{}", rendered.join("\t")));
    }
    if lines.len() > 1 {
        Ok(Some(lines.join("\n")))
    } else {
        Ok(None)
    }
}

fn sheet_bounds(sheet: &Node) -> Result<Option<SheetBounds>, ()> {
    let Some(reference) = attr(find_first(sheet, "dimension"), "ref") else {
        return Ok(None);
    };
    if reference.is_empty() {
        return Ok(None);
    }
    let (start, end) = reference.split_once(':').unwrap_or((reference, reference));
    let (min_row, min_col) = coordinate_to_tuple(start)?;
    let (max_row, max_col) = coordinate_to_tuple(end)?;
    Ok(Some(SheetBounds {
        min_col,
        min_row,
        max_col,
        max_row,
    }))
}

fn coordinate_to_tuple(reference: &str) -> Result<(u32, u32), ()> {
    let mut chars = reference.chars().peekable();
    if chars.peek() == Some(&'$') {
        chars.next();
    }
    let mut letters = String::new();
    while chars.peek().is_some_and(|ch| ch.is_ascii_alphabetic()) {
        letters.push(chars.next().unwrap_or_default());
    }
    if letters.is_empty() {
        return Err(());
    }
    if chars.peek() == Some(&'$') {
        chars.next();
    }
    let digits: String = chars.collect();
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(());
    }
    let mut column = 0u32;
    for character in letters.chars() {
        let value = u32::from(character.to_ascii_uppercase()) - u32::from(b'A') + 1;
        column = column
            .checked_mul(26)
            .ok_or(())?
            .checked_add(value)
            .ok_or(())?;
    }
    if column == 0 || column > 16_384 {
        return Err(());
    }
    let row: u32 = digits.parse().map_err(|_| ())?;
    if row == 0 {
        return Err(());
    }
    Ok((row, column))
}

fn column_letter(mut column: u32) -> String {
    let mut letters = Vec::new();
    while column > 0 {
        column -= 1;
        letters.push(b'A' + (column % 26) as u8);
        column /= 26;
    }
    letters.reverse();
    String::from_utf8(letters).unwrap_or_default()
}

fn parse_row_number(raw: &str) -> Result<u32, ()> {
    if let Ok(value) = raw.parse::<u32>() {
        return if value == 0 { Err(()) } else { Ok(value) };
    }
    let value: f64 = raw.parse().map_err(|_| ())?;
    if value.is_finite() && value > 0.0 && value.fract() == 0.0 && value <= u32::MAX as f64 {
        Ok(value as u32)
    } else {
        Err(())
    }
}

fn cell_text(
    cell: &Node,
    shared: &[String],
    date_styles: &[bool],
    delta_styles: &[bool],
    mac_1904: bool,
) -> Result<Option<String>, ()> {
    let data_type = match attr(Some(cell), "t") {
        Some(value) if !value.is_empty() => value,
        _ => "n",
    };
    let style_id = match attr(Some(cell), "s") {
        Some(value) if !value.is_empty() => value.parse::<usize>().map_err(|_| ())?,
        _ => 0,
    };
    let stored = if data_type == "inlineStr" {
        None
    } else {
        find_direct_child(cell, "v")
            .map(|node| node.text.clone())
            .filter(|text| !text.is_empty())
    };
    if let Some(raw) = stored {
        let text = match data_type {
            "n" => {
                let number = cast_number(&raw)?;
                if date_styles.get(style_id).copied().unwrap_or(false) {
                    from_excel(
                        number_as_f64(&number)?,
                        mac_1904,
                        delta_styles.get(style_id).copied().unwrap_or(false),
                    )
                    .unwrap_or_else(|_| "#VALUE!".to_string())
                } else {
                    number_text(&number)
                }
            }
            "s" => shared.get(python_index(&raw)?).cloned().ok_or(())?,
            "b" => python_bool_text(&raw)?,
            "d" => iso_text(&raw)?,
            _ => raw,
        };
        return Ok(Some(text));
    }
    if data_type == "inlineStr" {
        if let Some(inline) = find_direct_child(cell, "is") {
            return Ok(Some(office_text_content(inline)));
        }
    }
    Ok(None)
}

fn cast_number(raw: &str) -> Result<ExcelNumber, ()> {
    if raw.contains('.') || raw.contains('e') || raw.contains('E') {
        let value: f64 = raw.parse().map_err(|_| ())?;
        if !value.is_finite() {
            return Err(());
        }
        Ok(ExcelNumber::Float(value))
    } else {
        Ok(ExcelNumber::Int(python_int_string(raw)?))
    }
}

fn number_text(number: &ExcelNumber) -> String {
    match number {
        ExcelNumber::Int(text) => text.clone(),
        ExcelNumber::Float(value) => python_float_str(*value),
    }
}

fn number_as_f64(number: &ExcelNumber) -> Result<f64, ()> {
    match number {
        ExcelNumber::Float(value) => Ok(*value),
        ExcelNumber::Int(text) => text.parse().map_err(|_| ()),
    }
}

fn python_int_string(raw: &str) -> Result<String, ()> {
    let (sign, digits) = if let Some(rest) = raw.strip_prefix('+') {
        ("", rest)
    } else if let Some(rest) = raw.strip_prefix('-') {
        ("-", rest)
    } else {
        ("", raw)
    };
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(());
    }
    let trimmed = digits.trim_start_matches('0');
    if trimmed.is_empty() {
        return Ok("0".to_string());
    }
    if sign == "-" {
        Ok(format!("-{trimmed}"))
    } else {
        Ok(trimmed.to_string())
    }
}

fn python_index(raw: &str) -> Result<usize, ()> {
    let text = python_int_string(raw)?;
    if text.starts_with('-') {
        return Err(());
    }
    text.parse().map_err(|_| ())
}

fn python_bool_text(raw: &str) -> Result<String, ()> {
    let text = python_int_string(raw)?;
    Ok(if text == "0" || text == "-0" {
        "False"
    } else {
        "True"
    }
    .to_string())
}

fn python_float_str(value: f64) -> String {
    let text = format!("{value}");
    if let Some(exponent) = text.find(['e', 'E']) {
        let mantissa = &text[..exponent];
        let exponent_text = &text[exponent + 1..];
        let signed = if exponent_text.starts_with('+') || exponent_text.starts_with('-') {
            exponent_text.to_string()
        } else {
            format!("+{exponent_text}")
        };
        let mantissa = if mantissa.contains('.') {
            mantissa.to_string()
        } else {
            format!("{mantissa}.0")
        };
        return format!("{mantissa}e{signed}");
    }
    if text.contains('.') {
        text
    } else {
        format!("{text}.0")
    }
}

fn from_excel(value: f64, mac_1904: bool, timedelta: bool) -> Result<String, ()> {
    if !value.is_finite() {
        return Err(());
    }
    if timedelta {
        return format_excel_timedelta(value);
    }
    let (mut day, fraction) = python_divmod_one(value);
    let milliseconds = python_round_i64(fraction * 86_400.0 * 1_000.0).ok_or(())?;
    let extra_days = milliseconds.div_euclid(86_400_000);
    let within_day = milliseconds.rem_euclid(86_400_000);
    if (0.0..1.0).contains(&value) && extra_days == 0 {
        return Ok(format_time_ms(within_day));
    }
    if !mac_1904 && value > 0.0 && value < 60.0 {
        day += 1.0;
    }
    let whole_days = (day as i64).checked_add(extra_days).ok_or(())?;
    let epoch = if mac_1904 {
        NaiveDate::from_ymd_opt(1904, 1, 1)
    } else {
        NaiveDate::from_ymd_opt(1899, 12, 30)
    }
    .ok_or(())?
    .and_hms_opt(0, 0, 0)
    .ok_or(())?;
    let stamp = epoch
        .checked_add_signed(Duration::days(whole_days))
        .ok_or(())?
        .checked_add_signed(Duration::milliseconds(within_day))
        .ok_or(())?;
    Ok(format_naive_datetime(stamp))
}

fn python_divmod_one(value: f64) -> (f64, f64) {
    let quotient = value.floor();
    (quotient, value - quotient)
}

fn python_round_i64(value: f64) -> Option<i64> {
    if !value.is_finite() {
        return None;
    }
    let floor = value.floor();
    let fraction = value - floor;
    let rounds_away = fraction > 0.5 || (fraction == 0.5 && (floor as i64).rem_euclid(2) != 0);
    let chosen = if rounds_away { floor + 1.0 } else { floor };
    if !(i64::MIN as f64..=i64::MAX as f64).contains(&chosen) {
        return None;
    }
    Some(chosen as i64)
}

fn format_naive_datetime(stamp: NaiveDateTime) -> String {
    let base = stamp.format("%Y-%m-%d %H:%M:%S").to_string();
    let micros = stamp.nanosecond() / 1_000;
    if micros == 0 {
        base
    } else {
        format!("{base}.{micros:06}")
    }
}

fn format_time_ms(milliseconds: i64) -> String {
    let milliseconds = milliseconds.rem_euclid(86_400_000);
    let hours = (milliseconds / 3_600_000) as u32;
    let minutes = ((milliseconds % 3_600_000) / 60_000) as u32;
    let seconds = ((milliseconds % 60_000) / 1_000) as u32;
    let micros = ((milliseconds % 1_000) * 1_000) as u32;
    let Some(time) = NaiveTime::from_hms_micro_opt(hours, minutes, seconds, micros) else {
        return "00:00:00".to_string();
    };
    let base = time.format("%H:%M:%S").to_string();
    if micros == 0 {
        base
    } else {
        format!("{base}.{micros:06}")
    }
}

fn format_excel_timedelta(days: f64) -> Result<String, ()> {
    let microseconds = python_round_i64(days * 86_400.0 * 1_000_000.0).ok_or(())?;
    let negative = microseconds < 0;
    let mut remaining = microseconds.unsigned_abs();
    let micros = (remaining % 1_000_000) as u32;
    remaining /= 1_000_000;
    let seconds = (remaining % 60) as u32;
    remaining /= 60;
    let minutes = (remaining % 60) as u32;
    remaining /= 60;
    let hours = (remaining % 24) as u32;
    let day_count = remaining / 24;
    let mut text = format!("{hours}:{minutes:02}:{seconds:02}");
    if day_count > 0 {
        let label = if day_count == 1 { "day" } else { "days" };
        text = format!("{day_count} {label}, {text}");
    }
    if micros > 0 {
        text = format!("{text}.{micros:06}");
    }
    if negative {
        text = format!("-{text}");
    }
    Ok(text)
}

fn iso_text(raw: &str) -> Result<String, ()> {
    if raw.is_empty() {
        return Err(());
    }
    let raw = raw.trim_end_matches('Z');
    let (date, time) = if let Some((date, time)) = raw.split_once('T') {
        (
            Some(date).filter(|value| !value.is_empty()),
            Some(time).filter(|value| !value.is_empty()),
        )
    } else if raw.contains(':') {
        (None, Some(raw))
    } else {
        (Some(raw), None)
    };
    match (date, time) {
        (Some(date), None) => {
            let (year, month, day) = parse_ymd(date)?;
            Ok(format!("{year:04}-{month:02}-{day:02}"))
        }
        (None, Some(time)) => {
            let (hour, minute, second, micros) = parse_hms(time)?;
            Ok(format_clock(hour, minute, second, micros))
        }
        (Some(date), Some(time)) => {
            let (year, month, day) = parse_ymd(date)?;
            let (hour, minute, second, micros) = parse_hms(time)?;
            let base = format!(
                "{year:04}-{month:02}-{day:02} {}",
                format_clock(hour, minute, second, 0)
            );
            if micros == 0 {
                Ok(base)
            } else {
                Ok(format!("{base}.{micros:06}"))
            }
        }
        (None, None) => Err(()),
    }
}

fn parse_ymd(value: &str) -> Result<(i32, u32, u32), ()> {
    let mut parts = value.split('-');
    let year = parts.next().ok_or(())?.parse().map_err(|_| ())?;
    let month = parts.next().ok_or(())?.parse().map_err(|_| ())?;
    let day = parts.next().ok_or(())?.parse().map_err(|_| ())?;
    if parts.next().is_some() || !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return Err(());
    }
    Ok((year, month, day))
}

fn parse_hms(value: &str) -> Result<(u32, u32, u32, u32), ()> {
    let mut parts = value.split(':');
    let hour: u32 = parts.next().ok_or(())?.parse().map_err(|_| ())?;
    let minute: u32 = parts.next().ok_or(())?.parse().map_err(|_| ())?;
    let second_text = parts.next().unwrap_or("0");
    if parts.next().is_some() || hour > 23 || minute > 59 {
        return Err(());
    }
    let (second_text, fraction) = second_text.split_once('.').unwrap_or((second_text, ""));
    let second: u32 = second_text.parse().map_err(|_| ())?;
    if second > 60 {
        return Err(());
    }
    let micros = if fraction.is_empty() {
        0
    } else {
        let padded = format!("{fraction:0<6}");
        padded
            .chars()
            .take(6)
            .collect::<String>()
            .parse()
            .map_err(|_| ())?
    };
    Ok((hour, minute, second, micros))
}

fn format_clock(hour: u32, minute: u32, second: u32, micros: u32) -> String {
    let base = format!("{hour:02}:{minute:02}:{second:02}");
    if micros == 0 {
        base
    } else {
        format!("{base}.{micros:06}")
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write;

    use zip::write::FileOptions;
    use zip::{CompressionMethod, ZipWriter};

    use super::{
        builtin_format, extract_docx_text, extract_epub_text, extract_pptx_text, extract_xlsx_text,
        format_excel_timedelta, from_excel, is_date_format, parse_hms, python_float_str,
        strip_format_literals,
    };

    const W_NS: &str = "http://schemas.openxmlformats.org/wordprocessingml/2006/main";

    fn office_zip(entries: &[(&str, &[u8])]) -> Vec<u8> {
        let mut writer = ZipWriter::new(std::io::Cursor::new(Vec::new()));
        let opts = FileOptions::default().compression_method(CompressionMethod::Stored);
        for (name, content) in entries {
            writer.start_file(*name, opts).unwrap();
            writer.write_all(content).unwrap();
        }
        writer.finish().unwrap().into_inner()
    }

    fn document_xml(body: &str) -> Vec<u8> {
        format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\
             <w:document xmlns:w=\"{W_NS}\"><w:body>{body}</w:body></w:document>"
        )
        .into_bytes()
    }

    /// Measured against `deepseek_infra.infra.rag.files.extract_docx_text`, which
    /// returns `'Hello world\nSecond\ttabbed\nA1\tB1\nA2\tB2'` for this input.
    #[test]
    fn docx_paragraphs_tables_and_tabs_match_the_oracle() {
        let body = concat!(
            "<w:p><w:r><w:t>Hello world</w:t></w:r></w:p>",
            "<w:p><w:r><w:t>Second</w:t></w:r><w:r><w:tab/><w:t>tabbed</w:t></w:r></w:p>",
            "<w:tbl>",
            "<w:tr><w:tc><w:p><w:r><w:t>A1</w:t></w:r></w:p></w:tc>",
            "<w:tc><w:p><w:r><w:t>B1</w:t></w:r></w:p></w:tc></w:tr>",
            "<w:tr><w:tc><w:p><w:r><w:t>A2</w:t></w:r></w:p></w:tc>",
            "<w:tc><w:p><w:r><w:t>B2</w:t></w:r></w:p></w:tc></w:tr>",
            "</w:tbl>",
            "<w:p/>",
        );
        let data = office_zip(&[("word/document.xml", &document_xml(body))]);
        assert_eq!(
            extract_docx_text(&data).unwrap(),
            "Hello world\nSecond\ttabbed\nA1\tB1\nA2\tB2"
        );
    }

    /// A line break inside a run is a newline, and surrounding whitespace is
    /// trimmed per paragraph, which is what `paragraph_text` does.
    #[test]
    fn docx_line_breaks_and_trimming() {
        let body = "<w:p><w:r><w:t>  padded  </w:t></w:r></w:p>\
                    <w:p><w:r><w:t>one</w:t><w:br/><w:t>two</w:t></w:r></w:p>";
        let data = office_zip(&[("word/document.xml", &document_xml(body))]);
        assert_eq!(extract_docx_text(&data).unwrap(), "padded\none\ntwo");
    }

    /// Headers and footers are appended after the body in sorted name order, so
    /// `word/footer1.xml` precedes `word/header1.xml`. The oracle returns
    /// `'body\n\nfooter\n\nheader'` for this input.
    #[test]
    fn docx_headers_and_footers_follow_the_body() {
        let body = "<w:p><w:r><w:t>body</w:t></w:r></w:p>";
        let header = document_xml("<w:p><w:r><w:t>header</w:t></w:r></w:p>");
        let footer = document_xml("<w:p><w:r><w:t>footer</w:t></w:r></w:p>");
        let data = office_zip(&[
            ("word/document.xml", &document_xml(body)),
            ("word/header1.xml", &header),
            ("word/footer1.xml", &footer),
        ]);
        assert_eq!(
            extract_docx_text(&data).unwrap(),
            "body\n\nfooter\n\nheader"
        );
    }

    /// A ZIP without `word/document.xml` extracts nothing rather than failing:
    /// the loop skips names that are not present.
    #[test]
    fn docx_without_document_xml_yields_empty_text() {
        let data = office_zip(&[("unrelated.txt", b"nothing")]);
        assert_eq!(extract_docx_text(&data).unwrap(), "");
    }

    /// Bytes that are not a ZIP at all are `Invalid docx file`.
    #[test]
    fn docx_rejects_a_non_zip_payload() {
        let error = extract_docx_text(b"not a zip").unwrap_err();
        assert_eq!(error.message, "Invalid docx file");
        assert_eq!(error.status, 422);
    }

    /// Malformed XML inside a valid ZIP is `Invalid docx XML`, which the oracle
    /// also raises for this input; the non-ZIP case above is the `... file` one.
    #[test]
    fn docx_rejects_malformed_xml() {
        let data = office_zip(&[("word/document.xml", b"<w:document><unclosed>")]);
        let error = extract_docx_text(&data).unwrap_err();
        assert_eq!(error.message, "Invalid docx XML");
        assert_eq!(error.status, 422);
    }

    fn slide_xml(text: &str) -> Vec<u8> {
        format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?><p:sld xmlns:p=\"http://schemas.openxmlformats.org/presentationml/2006/main\" \
             xmlns:a=\"http://schemas.openxmlformats.org/drawingml/2006/main\">\
             <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )
        .into_bytes()
    }

    /// Slides are ordered by their numeric suffix, not by ZIP entry order, and
    /// each block carries its visible page number. The oracle returns
    /// `'[PPTX 第 1 页]\none\n\n[PPTX 第 2 页]\ntwo\n\n[PPTX 第 3 页]\nten'`.
    #[test]
    fn pptx_slides_are_numbered_in_numeric_order() {
        let data = office_zip(&[
            ("ppt/slides/slide10.xml", &slide_xml("ten")),
            ("ppt/slides/slide2.xml", &slide_xml("two")),
            ("ppt/slides/slide1.xml", &slide_xml("one")),
        ]);
        assert_eq!(
            extract_pptx_text(&data).unwrap(),
            "[PPTX 第 1 页]\none\n\n[PPTX 第 2 页]\ntwo\n\n[PPTX 第 3 页]\nten"
        );
    }

    /// A package with no slides is not an error; it simply has no text.
    #[test]
    fn pptx_without_slides_yields_empty_text() {
        let data = office_zip(&[("unrelated.txt", b"nothing")]);
        assert_eq!(extract_pptx_text(&data).unwrap(), "");
    }

    #[test]
    fn pptx_rejects_a_non_zip_payload() {
        let error = extract_pptx_text(b"not a zip").unwrap_err();
        assert_eq!(error.message, "Invalid pptx file");
    }

    /// `from_excel` converts a serial date the way openpyxl does: the 1899-12-30
    /// epoch, a value below 1 is a bare time, and serials between 1 and 60 are
    /// shifted by one day to absorb Excel's 1900 leap-year bug.
    #[test]
    fn from_excel_matches_the_workbook_semantics() {
        assert_eq!(from_excel(0.0, false, false).unwrap(), "00:00:00");
        assert_eq!(from_excel(0.5, false, false).unwrap(), "12:00:00");
        // Below 60 the day is shifted, so 1 lands on 1900-01-01, not 12-31.
        assert_eq!(
            from_excel(1.0, false, false).unwrap(),
            "1900-01-01 00:00:00"
        );
        assert_eq!(
            from_excel(1.5, false, false).unwrap(),
            "1900-01-01 12:00:00"
        );
        assert_eq!(
            from_excel(59.0, false, false).unwrap(),
            "1900-02-28 00:00:00"
        );
        // 60 and 61 are not shifted, so both map onto 02-28 and 03-01.
        assert_eq!(
            from_excel(60.0, false, false).unwrap(),
            "1900-02-28 00:00:00"
        );
        assert_eq!(
            from_excel(61.0, false, false).unwrap(),
            "1900-03-01 00:00:00"
        );
        assert_eq!(
            from_excel(45000.0, false, false).unwrap(),
            "2023-03-15 00:00:00"
        );
        assert_eq!(
            from_excel(45000.25, false, false).unwrap(),
            "2023-03-15 06:00:00"
        );
        // The 1904 epoch has no leap-year bug to absorb.
        assert_eq!(from_excel(1.0, true, false).unwrap(), "1904-01-02 00:00:00");
        // A non-finite serial cannot be a date.
        assert!(from_excel(f64::NAN, false, false).is_err());
        assert!(from_excel(f64::INFINITY, false, false).is_err());
    }

    /// `format_excel_timedelta` renders `[h]:mm:ss` with an optional day prefix
    /// and a six-digit fraction, matching `str(datetime.timedelta(...))` shape.
    #[test]
    fn excel_timedelta_matches_the_delta_shape() {
        assert_eq!(format_excel_timedelta(0.5).unwrap(), "12:00:00");
        assert_eq!(format_excel_timedelta(1.0).unwrap(), "1 day, 0:00:00");
        assert_eq!(format_excel_timedelta(1.5).unwrap(), "1 day, 12:00:00");
        assert_eq!(format_excel_timedelta(2.0).unwrap(), "2 days, 0:00:00");
        // A negative delta keeps the sign in front.
        assert_eq!(format_excel_timedelta(-0.5).unwrap(), "-12:00:00");
        // A sub-second delta carries the fraction.
        assert_eq!(
            format_excel_timedelta(0.5 / 86_400.0).unwrap(),
            "0:00:00.500000"
        );
    }

    /// `parse_hms` accepts `H:M`, `H:M:S` and a fractional second, refuses a
    /// fourth field, an out-of-range component and non-digits.
    #[test]
    fn parse_hms_accepts_and_refuses() {
        assert_eq!(parse_hms("12:34").unwrap(), (12, 34, 0, 0));
        assert_eq!(parse_hms("12:34:56").unwrap(), (12, 34, 56, 0));
        assert_eq!(parse_hms("12:34:56.5").unwrap(), (12, 34, 56, 500_000));
        assert_eq!(parse_hms("00:00:00.000001").unwrap(), (0, 0, 0, 1));
        // 24 o'clock and 60 minutes are out of range; 60 seconds is allowed.
        assert!(parse_hms("24:00").is_err());
        assert!(parse_hms("12:60").is_err());
        assert_eq!(parse_hms("12:34:60").unwrap(), (12, 34, 60, 0));
        assert!(parse_hms("12:34:56:78").is_err());
        assert!(parse_hms("not-a-time").is_err());
        assert!(parse_hms("").is_err());
    }

    /// `python_float_str` always shows a decimal part and normalises the
    /// exponent sign, matching `str(float)`.
    #[test]
    fn python_float_str_matches_str_float() {
        assert_eq!(python_float_str(1.0), "1.0");
        assert_eq!(python_float_str(1.5), "1.5");
        assert_eq!(python_float_str(123456789.0), "123456789.0");
        // An exponent keeps its sign and gains one when `format!` omits it.
        assert_eq!(python_float_str(1e16), "10000000000000000.0");
        assert_eq!(python_float_str(1e-5), "0.00001");
    }

    /// `is_date_format` looks only at the first section, ignores text inside
    /// quotes, and honours `\` and `_` escapes.
    #[test]
    fn is_date_format_detects_date_tokens() {
        assert!(is_date_format("mm-dd-yy"));
        assert!(is_date_format("h:mm:ss"));
        assert!(is_date_format("yyyy"));
        // A numeric format has no date token.
        assert!(!is_date_format("0.00"));
        assert!(!is_date_format("General"));
        assert!(!is_date_format("#,##0"));
        // Tokens inside a quoted literal do not count.
        assert!(!is_date_format("\"day\"0"));
        // Only the first section before `;` is inspected.
        assert!(!is_date_format("0.00;mmm"));
        // An escaped token is skipped, so this is not a date format.
        assert!(!is_date_format("\\d"));
    }

    /// `strip_format_literals` drops quoted text and bracket groups, except the
    /// `[h]`/`[m]`/`[s]` elapsed-time groups which are kept.
    #[test]
    fn strip_format_literals_keeps_only_elapsed_groups() {
        assert_eq!(strip_format_literals("\"text\"0.00"), "0.00");
        assert_eq!(strip_format_literals("[Red]0.00"), "0.00");
        assert_eq!(strip_format_literals("[h]:mm:ss"), "[h]:mm:ss");
        assert_eq!(strip_format_literals("[mm]:ss"), "[mm]:ss");
        // An unterminated quote is dropped to the end.
        assert_eq!(strip_format_literals("0.00\"open"), "0.00");
        assert_eq!(strip_format_literals(""), "");
    }

    /// `builtin_format` maps the fixed ids and refuses the rest.
    #[test]
    fn builtin_format_covers_the_fixed_ids() {
        assert_eq!(builtin_format(0), Some("General"));
        assert_eq!(builtin_format(1), Some("0"));
        assert_eq!(builtin_format(2), Some("0.00"));
        assert_eq!(builtin_format(9), Some("0%"));
        assert_eq!(builtin_format(14), Some("mm-dd-yy"));
        assert_eq!(builtin_format(22), Some("m/d/yy h:mm"));
        assert_eq!(builtin_format(49), Some("@"));
        // Ids outside the table are custom or unknown.
        assert_eq!(builtin_format(200), None);
        assert_eq!(builtin_format(999), None);
    }

    const XLSX_CT: &str = concat!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">",
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>",
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>",
        "<Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/>",
        "<Override PartName=\"/xl/worksheets/sheet1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>",
        "<Override PartName=\"/xl/sharedStrings.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml\"/>",
        "</Types>"
    );

    const XLSX_WB: &str = concat!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
        "<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" ",
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">",
        "<sheets><sheet name=\"Data\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>"
    );

    const XLSX_RELS: &str = concat!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">",
        "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet1.xml\"/>",
        "<Relationship Id=\"rId2\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings\" Target=\"sharedStrings.xml\"/>",
        "</Relationships>"
    );

    const XLSX_SS: &str = concat!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
        "<sst xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" count=\"1\" uniqueCount=\"1\">",
        "<si><t>Hello</t></si></sst>"
    );

    const XLSX_SHEET: &str = concat!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
        "<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheetData>",
        "<row r=\"1\"><c r=\"A1\" t=\"s\"><v>0</v></c><c r=\"B1\"><v>42</v></c></row>",
        "<row r=\"2\"><c r=\"A2\" t=\"str\"><v>text</v></c></row>",
        "</sheetData></worksheet>"
    );

    fn xlsx_parts(content: &str, workbook: &str, rels: &str, shared: &str, sheet: &str) -> Vec<u8> {
        office_zip(&[
            ("[Content_Types].xml", content.as_bytes()),
            ("xl/workbook.xml", workbook.as_bytes()),
            ("xl/_rels/workbook.xml.rels", rels.as_bytes()),
            ("xl/sharedStrings.xml", shared.as_bytes()),
            ("xl/worksheets/sheet1.xml", sheet.as_bytes()),
        ])
    }

    /// Measured against `deepseek_infra.infra.rag.files.extract_xlsx_text`, which
    /// returns `'Sheet: Data\n行 1\tA1=Hello\tB1=42\n行 2\tA2=text'`.
    #[test]
    fn xlsx_sheet_rows_match_the_oracle() {
        let data = xlsx_parts(XLSX_CT, XLSX_WB, XLSX_RELS, XLSX_SS, XLSX_SHEET);
        assert_eq!(
            extract_xlsx_text(&data).unwrap(),
            "Sheet: Data\n行 1\tA1=Hello\tB1=42\n行 2\tA2=text"
        );
    }

    /// A sheet whose rows are all empty contributes nothing.
    #[test]
    fn xlsx_without_cell_data_yields_empty_text() {
        let empty_sheet = concat!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>",
            "<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheetData/></worksheet>"
        );
        let data = xlsx_parts(XLSX_CT, XLSX_WB, XLSX_RELS, XLSX_SS, empty_sheet);
        assert_eq!(extract_xlsx_text(&data).unwrap(), "");
    }

    /// Every required part is checked: a package missing any of them is an
    /// invalid workbook rather than an empty one.
    #[test]
    fn xlsx_refuses_incomplete_packages() {
        // Not a ZIP at all.
        assert_eq!(
            extract_xlsx_text(b"not a zip").unwrap_err().message,
            "Invalid xlsx file"
        );
        // Missing `[Content_Types].xml`.
        let no_types = office_zip(&[("xl/workbook.xml", XLSX_WB.as_bytes())]);
        assert_eq!(
            extract_xlsx_text(&no_types).unwrap_err().message,
            "Invalid xlsx file"
        );
        // Content types without a workbook override.
        let no_workbook_part = office_zip(&[
            ("[Content_Types].xml", br#"<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>"# as &[u8]),
            ("xl/workbook.xml", XLSX_WB.as_bytes()),
        ]);
        assert_eq!(
            extract_xlsx_text(&no_workbook_part).unwrap_err().message,
            "Invalid xlsx file"
        );
        // The declared workbook is not present in the archive.
        let missing_workbook = office_zip(&[("[Content_Types].xml", XLSX_CT.as_bytes())]);
        assert_eq!(
            extract_xlsx_text(&missing_workbook).unwrap_err().message,
            "Invalid xlsx file"
        );
    }

    fn container_opf() -> Vec<u8> {
        b"<?xml version=\"1.0\"?><package/>".to_vec()
    }

    #[test]
    fn epub_rejects_a_non_zip_payload() {
        let error = extract_epub_text(b"not a zip").unwrap_err();
        assert_eq!(error.message, "Invalid epub file");
    }

    #[test]
    fn epub_without_chapters_yields_empty_text() {
        let data = office_zip(&[("META-INF/container.xml", &container_opf())]);
        assert_eq!(extract_epub_text(&data).unwrap(), "");
    }

    /// Chapters are ordered by name and each is prefixed with its entry path;
    /// `nav.xhtml` and `toc.xhtml` are not chapters. The oracle returns
    /// `'[EPUB: OEBPS/ch1.xhtml]\nChapter one\n\n[EPUB: OEBPS/ch2.xhtml]\nChapter two'`.
    #[test]
    fn epub_chapters_are_ordered_and_navigation_is_skipped() {
        let data = office_zip(&[
            (
                "OEBPS/ch2.xhtml",
                b"<html><body><p>Chapter two</p></body></html>",
            ),
            (
                "OEBPS/ch1.xhtml",
                b"<html><body><p>Chapter one</p></body></html>",
            ),
            (
                "OEBPS/nav.xhtml",
                b"<html><body><p>Navigation</p></body></html>",
            ),
            ("META-INF/container.xml", &container_opf()),
        ]);
        assert_eq!(
            extract_epub_text(&data).unwrap(),
            "[EPUB: OEBPS/ch1.xhtml]\nChapter one\n\n[EPUB: OEBPS/ch2.xhtml]\nChapter two"
        );
    }

    /// `toc.xhtml` is excluded the same way `nav.xhtml` is.
    #[test]
    fn epub_skips_toc_entries() {
        let data = office_zip(&[
            (
                "OEBPS/toc.xhtml",
                b"<html><body><p>Contents</p></body></html>",
            ),
            (
                "OEBPS/ch1.htm",
                b"<html><body><p>Only chapter</p></body></html>",
            ),
        ]);
        assert_eq!(
            extract_epub_text(&data).unwrap(),
            "[EPUB: OEBPS/ch1.htm]\nOnly chapter"
        );
    }

    /// A chapter whose markup carries no text is omitted rather than emitted
    /// with an empty body.
    #[test]
    fn epub_omits_chapters_without_text() {
        let data = office_zip(&[
            ("OEBPS/empty.xhtml", b"<html><body></body></html>"),
            ("OEBPS/ch1.xhtml", b"<html><body><p>Text</p></body></html>"),
        ]);
        assert_eq!(
            extract_epub_text(&data).unwrap(),
            "[EPUB: OEBPS/ch1.xhtml]\nText"
        );
    }

    /// A nested table inside a DOCX cell joins its paragraphs with ` / `.
    #[test]
    fn docx_multi_paragraph_cells_join_with_a_slash() {
        let body = "<w:tbl><w:tr><w:tc>\
                    <w:p><w:r><w:t>first</w:t></w:r></w:p>\
                    <w:p><w:r><w:t>second</w:t></w:r></w:p>\
                    </w:tc><w:tc><w:p><w:r><w:t>right</w:t></w:r></w:p></w:tc></w:tr></w:tbl>";
        let data = office_zip(&[("word/document.xml", &document_xml(body))]);
        assert_eq!(extract_docx_text(&data).unwrap(), "first / second\tright");
    }

    /// An empty table row is dropped instead of leaving a blank line.
    #[test]
    fn docx_drops_rows_without_text() {
        let body = "<w:tbl><w:tr><w:tc><w:p/></w:tc></w:tr>\
                    <w:tr><w:tc><w:p><w:r><w:t>kept</w:t></w:r></w:p></w:tc></w:tr></w:tbl>";
        let data = office_zip(&[("word/document.xml", &document_xml(body))]);
        assert_eq!(extract_docx_text(&data).unwrap(), "kept");
    }
}
