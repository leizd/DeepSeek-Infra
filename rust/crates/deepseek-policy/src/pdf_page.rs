//! PDF page preview: a PNG of one page, and the word boxes laid over it.
//!
//! The layout follows PyMuPDF's `get_text("words")` for an unembedded Helvetica
//! page: Adobe widths, MuPDF's 1.075 / -0.299 em box, and a new block when the
//! gap under the previous line is more than 0.15 em. The PNG is rendered by
//! `pdftoppm`. Its pixels are the page; they are not MuPDF's pixmap bytes.

use std::path::{Path, PathBuf};
use std::process::Command;

use lopdf::{Document, Object, ObjectId};
use serde_json::{Value, json};

use crate::app_error::{AppError, codes};
use crate::file_cache::FileCache;
use crate::file_routes::{
    FILE_PAGE_TEXT_CHARS, cached_file_source, clean_filename, content_disposition_header,
    reader_file_payload, reader_positive_int,
};
use crate::memory_index::python_round;

const FILE_PAGE_IMAGE_DEFAULT_SCALE: f64 = 1.6;
const FILE_PAGE_IMAGE_MIN_SCALE: f64 = 0.3;
const FILE_PAGE_IMAGE_MAX_SCALE: f64 = 3.0;
const FILE_PAGE_LAYOUT_MAX_WORDS: usize = 6_000;
const HELVETICA_ASCENT: f64 = 1.075;
const HELVETICA_DESCENT: f64 = -0.299;

/// Advances for bytes 32..=126, in thousandths of an em. Measured from MuPDF's
/// Helvetica, which matches the Adobe AFM for the letters and digits.
const HELVETICA_WIDTHS: [u16; 95] = [
    278, 278, 355, 556, 556, 889, 667, 222, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556,
    556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667,
    611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667,
    667, 611, 278, 278, 278, 469, 556, 222, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500,
    222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
];

struct TextState {
    a: f64,
    b: f64,
    c: f64,
    d: f64,
    e: f64,
    f: f64,
    line_a: f64,
    line_b: f64,
    line_c: f64,
    line_d: f64,
    line_e: f64,
    line_f: f64,
    size: f64,
    horizontal: f64,
    leading: f64,
    font: String,
}

struct Word {
    x0: f64,
    x1: f64,
    y: f64,
    size: f64,
    text: String,
}

struct Line {
    y: f64,
    size: f64,
    words: Vec<Word>,
}

pub struct PagePng {
    pub bytes: Vec<u8>,
    pub page: i64,
    pub page_count: i64,
}

pub struct PageImage {
    pub cached: Value,
    pub png: Vec<u8>,
    pub page: i64,
    pub page_count: i64,
    pub disposition: String,
}

/// `file_page_image`. A legal PDF writes `{id}.page-{n}-{scale}.png` beside the
/// source. A refusal returns before that write.
pub fn file_page_image(
    root: &Path,
    file_id: &str,
    project_id: Option<&str>,
    page: Option<&Value>,
    scale: Option<&Value>,
    cache: &FileCache,
) -> Result<PageImage, AppError> {
    let (cached, source_path) = cached_file_source(root, file_id, project_id, cache)?;
    if !is_pdf(&cached) {
        return Err(AppError {
            message: "Page image preview is only available for PDF files".to_string(),
            code: codes::UNSUPPORTED_FILE,
            status: 415,
        });
    }
    let requested_page = reader_positive_int(page, "Invalid page", 1)?;
    let requested_scale =
        reader_scale_float(scale, "Invalid scale", FILE_PAGE_IMAGE_DEFAULT_SCALE)?;
    let cached_page_count = int_or_zero(cached.get("pageCount"));
    let mut page_count = cached_page_count.max(1);
    let mut rendered_page = requested_page as i64;
    if cached_page_count > 0 {
        rendered_page = rendered_page.min(page_count);
    }
    let scale_key = python_round(requested_scale * 100.0) as i64;
    let mut cache_path =
        source_path.with_file_name(format!("{file_id}.page-{rendered_page}-{scale_key}.png"));
    if let Some(bytes) = fresh_cache(&source_path, &cache_path) {
        return Ok(PageImage {
            disposition: png_disposition(&cached, rendered_page),
            cached,
            png: bytes,
            page: rendered_page,
            page_count,
        });
    }
    let data =
        std::fs::read(&source_path).map_err(|_| render_unavailable("could not read the PDF"))?;
    let rendered = render_pdf_page_png(&data, rendered_page, requested_scale)?;
    page_count = page_count.max(rendered.page_count).max(rendered.page);
    if rendered.page != rendered_page {
        rendered_page = rendered.page;
        cache_path =
            source_path.with_file_name(format!("{file_id}.page-{rendered_page}-{scale_key}.png"));
    }
    let _ = std::fs::write(&cache_path, &rendered.bytes);
    Ok(PageImage {
        disposition: png_disposition(&cached, rendered_page),
        cached,
        png: rendered.bytes,
        page: rendered_page,
        page_count,
    })
}

/// `file_page_layout`. This only reads the source. Success and refusal leave
/// the cache directory unchanged.
pub fn file_page_layout(
    root: &Path,
    file_id: &str,
    project_id: Option<&str>,
    page: Option<&Value>,
    cache: &FileCache,
) -> Result<Value, AppError> {
    let (cached, source_path) = cached_file_source(root, file_id, project_id, cache)?;
    if !is_pdf(&cached) {
        return Err(AppError {
            message: "Page text layout is only available for PDF files".to_string(),
            code: codes::UNSUPPORTED_FILE,
            status: 415,
        });
    }
    let mut requested_page = reader_positive_int(page, "Invalid page", 1)? as i64;
    let cached_page_count = int_or_zero(cached.get("pageCount"));
    if cached_page_count > 0 {
        requested_page = requested_page.min(cached_page_count.max(1));
    }
    let data =
        std::fs::read(&source_path).map_err(|_| layout_unavailable("could not read the PDF"))?;
    let mut layout = render_pdf_page_layout(&data, requested_page)?;
    let page_count = cached_page_count
        .max(int_or_zero(layout.get("pageCount")))
        .max(int_or_zero(layout.get("index")))
        .max(1);
    if let Some(object) = layout.as_object_mut() {
        object.insert("pageCount".to_string(), json!(page_count));
        if let Some(Value::Array(words)) = object.get_mut("words") {
            words.truncate(FILE_PAGE_LAYOUT_MAX_WORDS);
        }
    }
    let chunks = cached
        .get("chunks")
        .and_then(Value::as_array)
        .map(Vec::len)
        .unwrap_or(0);
    Ok(json!({
        "ok": true,
        "file": reader_file_payload(&cached, file_id, project_id, chunks),
        "page": layout,
    }))
}

pub fn render_pdf_page_layout(data: &[u8], page: i64) -> Result<Value, AppError> {
    let document =
        Document::load_mem(data).map_err(|error| layout_unavailable(&error.to_string()))?;
    let pages = document.get_pages();
    if pages.is_empty() {
        return Err(AppError {
            message: "PDF has no pages".to_string(),
            code: codes::UNSUPPORTED_FILE,
            status: 415,
        });
    }
    let page_count = pages.len() as i64;
    let rendered = page.clamp(1, page_count);
    let Some(page_id) = pages.get(&(rendered as u32)) else {
        return Err(layout_unavailable("page is missing"));
    };
    let (llx, lly, urx, ury) = media_box(&document, *page_id);
    let width = (urx - llx).max(1.0);
    let height = (ury - lly).max(1.0);
    let content = document
        .get_page_content(*page_id)
        .map_err(|error| layout_unavailable(&error.to_string()))?;
    let fonts = page_fonts(&document, *page_id);
    let words = layout_words(&content, &fonts);
    let placed = place_words(words, llx, ury, width, height);
    let text = placed
        .lines
        .iter()
        .filter(|line| !line.is_empty())
        .cloned()
        .collect::<Vec<_>>()
        .join("\n");
    let text: String = text.chars().take(FILE_PAGE_TEXT_CHARS).collect();
    Ok(json!({
        "index": rendered,
        "pageCount": page_count,
        "width": round_places(width, 2),
        "height": round_places(height, 2),
        "text": text.trim(),
        "words": placed.words,
        "hasText": !placed.words.is_empty(),
    }))
}

pub fn render_pdf_page_png(data: &[u8], page: i64, scale: f64) -> Result<PagePng, AppError> {
    let document =
        Document::load_mem(data).map_err(|error| render_unavailable(&error.to_string()))?;
    let page_count = document.get_pages().len() as i64;
    if page_count <= 0 {
        return Err(AppError {
            message: "PDF has no pages".to_string(),
            code: codes::UNSUPPORTED_FILE,
            status: 415,
        });
    }
    let rendered = page.clamp(1, page_count);
    let Some(pdftoppm) = which("pdftoppm") else {
        return Err(render_unavailable("pdftoppm was not found"));
    };
    let directory = std::env::temp_dir().join(format!(
        "deepseek-page-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|elapsed| elapsed.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&directory).map_err(|error| render_unavailable(&error.to_string()))?;
    let input = directory.join("input.pdf");
    let prefix = directory.join("page");
    let write_result = std::fs::write(&input, data);
    let dpi = (scale * 72.0).round().clamp(36.0, 432.0) as i64;
    let rendered_ok = write_result.and_then(|_| {
        Command::new(&pdftoppm)
            .args([
                "-f",
                &rendered.to_string(),
                "-l",
                &rendered.to_string(),
                "-r",
                &dpi.to_string(),
                "-png",
                &input.display().to_string(),
                &prefix.display().to_string(),
            ])
            .output()
            .map_err(std::io::Error::other)
    });
    let png = std::fs::read_dir(&directory).ok().and_then(|entries| {
        entries
            .flatten()
            .map(|entry| entry.path())
            .find(|path| path.extension().and_then(|ext| ext.to_str()) == Some("png"))
            .and_then(|path| std::fs::read(path).ok())
    });
    let _ = std::fs::remove_dir_all(&directory);
    match (rendered_ok, png) {
        (Ok(output), Some(bytes)) if output.status.success() && bytes.starts_with(b"\x89PNG") => {
            Ok(PagePng {
                bytes,
                page: rendered,
                page_count,
            })
        }
        _ => Err(render_unavailable("pdftoppm did not produce a PNG")),
    }
}

struct Placed {
    words: Vec<Value>,
    lines: Vec<String>,
}

fn place_words(mut words: Vec<Word>, llx: f64, ury: f64, width: f64, height: f64) -> Placed {
    words.sort_by(|left, right| {
        right
            .y
            .total_cmp(&left.y)
            .then_with(|| left.x0.total_cmp(&right.x0))
    });
    let mut lines: Vec<Line> = Vec::new();
    for word in words {
        let same = lines
            .last()
            .is_some_and(|line| (line.y - word.y).abs() <= 0.5);
        if same {
            lines.last_mut().expect("line").words.push(word);
        } else {
            lines.push(Line {
                y: word.y,
                size: word.size,
                words: vec![word],
            });
        }
    }
    for line in &mut lines {
        line.words
            .sort_by(|left, right| left.x0.total_cmp(&right.x0));
    }
    let mut block = 0i64;
    let mut line_no = 0i64;
    let mut placed_words = Vec::new();
    let mut line_texts = Vec::new();
    for (index, line) in lines.iter().enumerate() {
        if index > 0 {
            let previous = &lines[index - 1];
            let previous_bottom = (ury - previous.y) + (-HELVETICA_DESCENT) * previous.size;
            let this_top = (ury - line.y) - HELVETICA_ASCENT * line.size;
            if this_top - previous_bottom > 0.15 * previous.size {
                block += 1;
                line_no = 0;
            } else {
                line_no += 1;
            }
        }
        let mut parts = Vec::new();
        for (word_no, word) in line.words.iter().enumerate() {
            let y_top = (ury - word.y) - HELVETICA_ASCENT * word.size;
            let y_bottom = (ury - word.y) - HELVETICA_DESCENT * word.size;
            let left = ((word.x0 - llx) / width) * 100.0;
            let top = (y_top / height) * 100.0;
            let right = ((word.x1 - llx) / width) * 100.0;
            let bottom = (y_bottom / height) * 100.0;
            let box_width = (right.max(left) - left).max(0.01);
            let box_height = (bottom.max(top) - top).max(0.01);
            placed_words.push(json!({
                "index": placed_words.len(),
                "text": word.text,
                "left": round_places(left.clamp(0.0, 100.0), 4),
                "top": round_places(top.clamp(0.0, 100.0), 4),
                "width": round_places(box_width.min(100.0), 4),
                "height": round_places(box_height.min(100.0), 4),
                "block": block,
                "line": line_no,
                "word": word_no,
            }));
            parts.push(word.text.clone());
            if placed_words.len() >= FILE_PAGE_LAYOUT_MAX_WORDS {
                break;
            }
        }
        line_texts.push(parts.join(" "));
        if placed_words.len() >= FILE_PAGE_LAYOUT_MAX_WORDS {
            break;
        }
    }
    Placed {
        words: placed_words,
        lines: line_texts,
    }
}

fn layout_words(content: &[u8], fonts: &std::collections::BTreeMap<String, String>) -> Vec<Word> {
    let Ok(decoded) = lopdf::content::Content::decode(content) else {
        return Vec::new();
    };
    let mut state = TextState {
        a: 1.0,
        b: 0.0,
        c: 0.0,
        d: 1.0,
        e: 0.0,
        f: 0.0,
        line_a: 1.0,
        line_b: 0.0,
        line_c: 0.0,
        line_d: 1.0,
        line_e: 0.0,
        line_f: 0.0,
        size: 1.0,
        horizontal: 100.0,
        leading: 0.0,
        font: String::new(),
    };
    let mut open: Option<Word> = None;
    let mut words = Vec::new();
    let finish = |open: &mut Option<Word>, words: &mut Vec<Word>| {
        if let Some(word) = open.take() {
            if !word.text.is_empty() {
                words.push(word);
            }
        }
    };
    for operation in decoded.operations {
        match operation.operator.as_str() {
            "BT" => {
                finish(&mut open, &mut words);
                state.a = 1.0;
                state.b = 0.0;
                state.c = 0.0;
                state.d = 1.0;
                state.e = 0.0;
                state.f = 0.0;
                state.line_a = 1.0;
                state.line_b = 0.0;
                state.line_c = 0.0;
                state.line_d = 1.0;
                state.line_e = 0.0;
                state.line_f = 0.0;
            }
            "Tf" => {
                if let Some(name) = operation.operands.first().and_then(name_str) {
                    state.font = fonts.get(&name).cloned().unwrap_or(name);
                }
                if let Some(size) = operand_number(operation.operands.get(1)) {
                    state.size = size;
                }
            }
            "Tz" => {
                if let Some(scale) = operand_number(operation.operands.first()) {
                    state.horizontal = scale;
                }
            }
            "Td" | "TD" => {
                finish(&mut open, &mut words);
                if operation.operator == "TD" {
                    if let Some(ty) = operand_number(operation.operands.get(1)) {
                        state.leading = -ty;
                    }
                }
                if let (Some(tx), Some(ty)) = (
                    operand_number(operation.operands.first()),
                    operand_number(operation.operands.get(1)),
                ) {
                    move_line(&mut state, tx, ty);
                }
            }
            "Tm" => {
                finish(&mut open, &mut words);
                let mut values = [0.0; 6];
                if operation.operands.len() >= 6
                    && (0..6).all(|index| operand_number(operation.operands.get(index)).is_some())
                {
                    for (index, value) in values.iter_mut().enumerate() {
                        *value = operand_number(operation.operands.get(index)).unwrap_or(0.0);
                    }
                    state.a = values[0];
                    state.b = values[1];
                    state.c = values[2];
                    state.d = values[3];
                    state.e = values[4];
                    state.f = values[5];
                    state.line_a = values[0];
                    state.line_b = values[1];
                    state.line_c = values[2];
                    state.line_d = values[3];
                    state.line_e = values[4];
                    state.line_f = values[5];
                }
            }
            "TL" => {
                if let Some(leading) = operand_number(operation.operands.first()) {
                    state.leading = leading;
                }
            }
            "T*" => {
                finish(&mut open, &mut words);
                let leading = state.leading;
                move_line(&mut state, 0.0, -leading);
            }
            "Tj" => show_text(
                &mut state,
                &operand_text(operation.operands.first()),
                &mut open,
                &mut words,
                &finish,
            ),
            "'" => {
                let leading = state.leading;
                move_line(&mut state, 0.0, -leading);
                show_text(
                    &mut state,
                    &operand_text(operation.operands.first()),
                    &mut open,
                    &mut words,
                    &finish,
                );
            }
            "\"" => {
                let leading = state.leading;
                move_line(&mut state, 0.0, -leading);
                show_text(
                    &mut state,
                    &operand_text(operation.operands.get(2)),
                    &mut open,
                    &mut words,
                    &finish,
                );
            }
            "TJ" => show_tj(
                &mut state,
                operation.operands.first(),
                &mut open,
                &mut words,
                &finish,
            ),
            _ => {}
        }
    }
    finish(&mut open, &mut words);
    words
}

fn show_tj(
    state: &mut TextState,
    operand: Option<&Object>,
    open: &mut Option<Word>,
    words: &mut Vec<Word>,
    finish: &impl Fn(&mut Option<Word>, &mut Vec<Word>),
) {
    let Some(Object::Array(items)) = operand else {
        return;
    };
    for item in items {
        if let Some(text) = string_text(item) {
            show_text(state, &text, open, words, finish);
        } else if let Some(adjustment) = operand_number(Some(item)) {
            if adjustment.abs() >= 150.0 {
                finish(open, words);
            }
            let shift = -adjustment / 1000.0 * state.size * state.horizontal / 100.0;
            advance(state, shift);
        }
    }
}

fn show_text(
    state: &mut TextState,
    text: &str,
    open: &mut Option<Word>,
    words: &mut Vec<Word>,
    finish: &impl Fn(&mut Option<Word>, &mut Vec<Word>),
) {
    for ch in text.chars() {
        let width = glyph_width(ch, &state.font, state.size) * state.horizontal / 100.0;
        if ch.is_whitespace() {
            finish(open, words);
            advance(state, width);
            continue;
        }
        if open.is_none() {
            *open = Some(Word {
                x0: state.e,
                x1: state.e,
                y: state.f,
                size: state.size,
                text: String::new(),
            });
        }
        if let Some(word) = open.as_mut() {
            word.text.push(ch);
            word.x1 = state.e + width * state.a;
        }
        advance(state, width);
    }
}

fn advance(state: &mut TextState, tx: f64) {
    state.e += tx * state.a;
    state.f += tx * state.b;
}

/// `Td` / `T*` move the text line matrix, then copy it onto the text matrix.
/// Glyph advances update only the text matrix, so the next line starts at the
/// line origin rather than at the end of the previous string.
fn move_line(state: &mut TextState, tx: f64, ty: f64) {
    let next_e = tx * state.line_a + ty * state.line_c + state.line_e;
    let next_f = tx * state.line_b + ty * state.line_d + state.line_f;
    state.line_e = next_e;
    state.line_f = next_f;
    state.a = state.line_a;
    state.b = state.line_b;
    state.c = state.line_c;
    state.d = state.line_d;
    state.e = next_e;
    state.f = next_f;
}

fn glyph_width(ch: char, font: &str, size: f64) -> f64 {
    let units = if font.contains("Courier") {
        600
    } else {
        let code = ch as u32;
        if (32..127).contains(&code) {
            HELVETICA_WIDTHS[(code - 32) as usize]
        } else {
            556
        }
    };
    units as f64 / 1000.0 * size
}

fn page_fonts(
    document: &Document,
    page_id: ObjectId,
) -> std::collections::BTreeMap<String, String> {
    let mut fonts = std::collections::BTreeMap::new();
    let Some(resources) = inherited_dict(document, page_id, b"Resources") else {
        return fonts;
    };
    let Ok(Object::Dictionary(font_dict)) = resources.get(b"Font") else {
        return fonts;
    };
    for (name, value) in font_dict.iter() {
        let key = String::from_utf8_lossy(name).into_owned();
        let referenced = match value {
            Object::Reference(id) => document.get_object(*id).ok(),
            other => Some(other),
        };
        let base = referenced
            .and_then(|object| object.as_dict().ok())
            .and_then(|dict| dict.get(b"BaseFont").ok())
            .and_then(name_str)
            .unwrap_or_else(|| key.clone());
        fonts.insert(key, base);
    }
    fonts
}

fn media_box(document: &Document, page_id: ObjectId) -> (f64, f64, f64, f64) {
    let Some(array) = inherited_array(document, page_id, b"MediaBox") else {
        return (0.0, 0.0, 612.0, 792.0);
    };
    if array.len() != 4 {
        return (0.0, 0.0, 612.0, 792.0);
    }
    let numbers: Vec<f64> = array
        .iter()
        .filter_map(|item| operand_number(Some(item)))
        .collect();
    if numbers.len() == 4 {
        (numbers[0], numbers[1], numbers[2], numbers[3])
    } else {
        (0.0, 0.0, 612.0, 792.0)
    }
}

fn inherited_dict<'a>(
    document: &'a Document,
    mut current: ObjectId,
    key: &[u8],
) -> Option<&'a lopdf::Dictionary> {
    for _ in 0..8 {
        let dict = dictionary(document, current)?;
        if let Ok(Object::Dictionary(found)) = dict.get(key) {
            return Some(found);
        }
        current = match dict.get(b"Parent").ok()? {
            Object::Reference(id) => *id,
            _ => return None,
        };
    }
    None
}

fn inherited_array<'a>(
    document: &'a Document,
    mut current: ObjectId,
    key: &[u8],
) -> Option<&'a Vec<Object>> {
    for _ in 0..8 {
        let dict = dictionary(document, current)?;
        if let Ok(Object::Array(found)) = dict.get(key) {
            return Some(found);
        }
        current = match dict.get(b"Parent").ok()? {
            Object::Reference(id) => *id,
            _ => return None,
        };
    }
    None
}

fn dictionary(document: &Document, id: ObjectId) -> Option<&lopdf::Dictionary> {
    match document.get_object(id).ok()? {
        Object::Dictionary(dict) => Some(dict),
        _ => None,
    }
}

fn fresh_cache(source: &Path, cache: &Path) -> Option<Vec<u8>> {
    let source_time = std::fs::metadata(source).ok()?.modified().ok()?;
    let cache_time = std::fs::metadata(cache).ok()?.modified().ok()?;
    if cache_time >= source_time {
        std::fs::read(cache).ok()
    } else {
        None
    }
}

fn png_disposition(cached: &Value, page: i64) -> String {
    let name = clean_filename(&text_or(cached.get("name"), "document"));
    let stem = Path::new(&name)
        .file_stem()
        .and_then(|stem| stem.to_str())
        .filter(|stem| !stem.is_empty())
        .unwrap_or("document");
    content_disposition_header("inline", &format!("{stem}-page-{page}.png"))
}

fn is_pdf(cached: &Value) -> bool {
    let kind = text_or(cached.get("kind"), "").to_ascii_lowercase();
    let media = text_or(cached.get("type"), "")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    kind == "pdf" || media == "application/pdf"
}

fn reader_scale_float(value: Option<&Value>, message: &str, default: f64) -> Result<f64, AppError> {
    let Some(value) = value else {
        return Ok(default);
    };
    if value.is_null() {
        return Ok(default);
    }
    if let Value::String(text) = value {
        if text.is_empty() {
            return Ok(default);
        }
    }
    let number = match value {
        Value::Number(number) => number.as_f64(),
        Value::Bool(flag) => Some(if *flag { 1.0 } else { 0.0 }),
        Value::String(text) => parse_python_float(text),
        _ => None,
    };
    let Some(number) = number else {
        return Err(AppError {
            message: message.to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    };
    if number <= 0.0 {
        return Err(AppError {
            message: message.to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    Ok(number.clamp(FILE_PAGE_IMAGE_MIN_SCALE, FILE_PAGE_IMAGE_MAX_SCALE))
}

fn parse_python_float(text: &str) -> Option<f64> {
    let trimmed = text.trim().replace('_', "");
    if trimmed.is_empty() {
        return None;
    }
    trimmed
        .parse::<f64>()
        .ok()
        .filter(|number| number.is_finite())
}

fn int_or_zero(value: Option<&Value>) -> i64 {
    match value {
        Some(Value::Number(number)) => number.as_i64().unwrap_or(0),
        Some(Value::Bool(flag)) => i64::from(*flag),
        Some(Value::String(text)) => text.trim().parse::<i64>().unwrap_or(0),
        _ => 0,
    }
}

fn text_or(value: Option<&Value>, fallback: &str) -> String {
    let text = crate::core_utils::text_or_empty(value);
    if text.is_empty() {
        fallback.to_string()
    } else {
        text
    }
}

fn round_places(value: f64, places: i32) -> f64 {
    let factor = 10f64.powi(places);
    python_round(value * factor) / factor
}

fn layout_unavailable(detail: &str) -> AppError {
    AppError {
        message: format!("PDF page text layout is unavailable: {detail}"),
        code: codes::UNSUPPORTED_FILE,
        status: 415,
    }
}

fn render_unavailable(detail: &str) -> AppError {
    AppError {
        message: format!("PDF page rendering is unavailable: {detail}"),
        code: codes::UNSUPPORTED_FILE,
        status: 415,
    }
}

fn which(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for entry in std::env::split_paths(&path) {
        let candidate = entry.join(name);
        if candidate.is_file() {
            return Some(candidate);
        }
        let with_exe = entry.join(format!("{name}.exe"));
        if with_exe.is_file() {
            return Some(with_exe);
        }
    }
    None
}

fn operand_number(operand: Option<&Object>) -> Option<f64> {
    match operand? {
        Object::Integer(value) => Some(*value as f64),
        Object::Real(value) => Some(f64::from(*value)),
        _ => None,
    }
}

fn operand_text(operand: Option<&Object>) -> String {
    operand.and_then(string_text).unwrap_or_default()
}

fn string_text(object: &Object) -> Option<String> {
    match object {
        Object::String(bytes, _) => Some(decode_pdf_bytes(bytes)),
        _ => None,
    }
}

fn name_str(object: &Object) -> Option<String> {
    match object {
        Object::Name(bytes) => Some(String::from_utf8_lossy(bytes).into_owned()),
        _ => None,
    }
}

fn decode_pdf_bytes(bytes: &[u8]) -> String {
    if bytes.starts_with(&[0xFE, 0xFF]) {
        let units: Vec<u16> = bytes[2..]
            .chunks(2)
            .filter(|pair| pair.len() == 2)
            .map(|pair| u16::from_be_bytes([pair[0], pair[1]]))
            .collect();
        return String::from_utf16_lossy(&units);
    }
    bytes.iter().map(|byte| char::from(*byte)).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn simple_pdf() -> Vec<u8> {
        let stream = b"BT\n/F1 12 Tf\n72 720 Td\n(Hello pdf) Tj\n0 -16 Td\n(World) Tj\nET\n";
        let mut objects = vec![
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_vec(),
            Vec::new(),
            b"<< /Type /Catalog /Pages 2 0 R >>".to_vec(),
        ];
        let mut content = format!("<< /Length {} >>\nstream\n", stream.len()).into_bytes();
        content.extend_from_slice(stream);
        content.extend_from_slice(b"endstream");
        objects.push(content);
        objects.push(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 1 0 R >> >> >>".to_vec(),
        );
        objects[1] = b"<< /Type /Pages /Kids [5 0 R] /Count 1 >>".to_vec();
        let mut parts = vec![b"%PDF-1.4\n".to_vec()];
        let mut offsets = vec![0usize];
        for (index, object) in objects.iter().enumerate() {
            offsets.push(parts.iter().map(Vec::len).sum());
            let mut encoded = format!("{} 0 obj\n", index + 1).into_bytes();
            encoded.extend_from_slice(object);
            encoded.extend_from_slice(b"\nendobj\n");
            parts.push(encoded);
        }
        let xref = parts.iter().map(Vec::len).sum::<usize>();
        let mut trailer = b"xref\n0 6\n0000000000 65535 f \n".to_vec();
        for offset in offsets.iter().skip(1) {
            trailer.extend(format!("{offset:010} 00000 n \n").into_bytes());
        }
        trailer.extend(
            format!("trailer\n<< /Size 6 /Root 3 0 R >>\nstartxref\n{xref}\n%%EOF\n").into_bytes(),
        );
        parts.push(trailer);
        parts.into_iter().flatten().collect()
    }

    #[test]
    fn helvetica_words_match_the_mupdf_boxes() {
        let layout = render_pdf_page_layout(&simple_pdf(), 5).expect("layout");
        assert_eq!(layout["index"], 1);
        assert_eq!(layout["pageCount"], 1);
        assert_eq!(layout["width"], 612.0);
        assert_eq!(layout["height"], 792.0);
        assert_eq!(layout["text"], "Hello pdf\nWorld");
        assert_eq!(layout["hasText"], true);
        let words = layout["words"].as_array().expect("words");
        assert_eq!(words.len(), 3);
        assert_eq!(words[0]["text"], "Hello");
        assert_eq!(words[0]["block"], 0);
        assert_eq!(words[0]["line"], 0);
        assert_eq!(words[0]["word"], 0);
        assert_eq!(words[1]["text"], "pdf");
        assert_eq!(words[1]["word"], 1);
        assert_eq!(words[2]["text"], "World");
        assert_eq!(words[2]["block"], 0);
        assert_eq!(words[2]["line"], 1);
        assert!((words[2]["left"].as_f64().unwrap() - 11.7647).abs() < 0.0001);
        assert!((words[0]["left"].as_f64().unwrap() - 11.7647).abs() < 0.0001);
        assert!((words[0]["top"].as_f64().unwrap() - 7.4621).abs() < 0.0001);
        assert!((words[0]["width"].as_f64().unwrap() - 4.4667).abs() < 0.0001);
        assert!((words[0]["height"].as_f64().unwrap() - 2.0818).abs() < 0.0001);
    }
}
