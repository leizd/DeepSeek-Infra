//! Selectable PDF text, matching `extract_pdf_text` when pypdf is installed.
//!
//! `lopdf` owns xref, object streams and FlateDecode. The operator walk follows
//! pypdf's plain extractor far enough for literal `Tj` text, hex `TJ` text, and
//! a newline when the text baseline moves. A file that does not parse is
//! `Could not extract text from this PDF`. A parsed file with no text is an
//! empty string so the caller can apply the OCR refusal.

use lopdf::content::Content;
use lopdf::{Document, Object};
use serde_json::{Value, json};

use crate::app_error::{AppError, codes};
use crate::file_routes::normalize_extracted_text;

pub struct PdfDocument {
    pub text: String,
    pub page_count: i64,
    pub page_texts: Vec<Value>,
}

pub fn extract_pdf_document(data: &[u8]) -> Result<PdfDocument, AppError> {
    let document = Document::load_mem(data).map_err(|_| could_not_extract())?;
    let pages = document.get_pages();
    let mut blocks = Vec::new();
    let mut page_texts = Vec::new();
    for (number, id) in pages.iter() {
        let content = document
            .get_page_content(*id)
            .map_err(|_| could_not_extract())?;
        let text = normalize_extracted_text(&show_text(&content));
        if text.is_empty() {
            continue;
        }
        blocks.push(format!("[PDF page {number}]\n{text}"));
        page_texts.push(json!({"page": number, "text": text}));
    }
    Ok(PdfDocument {
        text: blocks.join("\n\n"),
        page_count: i64::from(u32::try_from(pages.len()).unwrap_or(u32::MAX)),
        page_texts,
    })
}

fn could_not_extract() -> AppError {
    AppError {
        message: "Could not extract text from this PDF".to_string(),
        code: codes::INVALID_PAYLOAD,
        status: 422,
    }
}

struct TextState {
    a: f64,
    b: f64,
    c: f64,
    d: f64,
    e: f64,
    f: f64,
    leading: f64,
    buffer: String,
    last_show_y: Option<f64>,
}

impl TextState {
    fn new() -> Self {
        Self {
            a: 1.0,
            b: 0.0,
            c: 0.0,
            d: 1.0,
            e: 0.0,
            f: 0.0,
            leading: 0.0,
            buffer: String::new(),
            last_show_y: None,
        }
    }

    fn begin_text(&mut self) {
        self.a = 1.0;
        self.b = 0.0;
        self.c = 0.0;
        self.d = 1.0;
        self.e = 0.0;
        self.f = 0.0;
    }

    fn translate(&mut self, tx: f64, ty: f64) {
        let next_e = tx * self.a + ty * self.c + self.e;
        let next_f = tx * self.b + ty * self.d + self.f;
        self.e = next_e;
        self.f = next_f;
    }

    fn set_matrix(&mut self, values: [f64; 6]) {
        self.a = values[0];
        self.b = values[1];
        self.c = values[2];
        self.d = values[3];
        self.e = values[4];
        self.f = values[5];
    }

    fn show(&mut self, text: &str) {
        if text.is_empty() {
            return;
        }
        if let Some(previous) = self.last_show_y {
            if (previous - self.f).abs() > 0.5
                && !self.buffer.is_empty()
                && !self.buffer.ends_with('\n')
            {
                self.buffer.push('\n');
            }
        }
        self.buffer.push_str(text);
        self.last_show_y = Some(self.f);
    }
}

fn show_text(content: &[u8]) -> String {
    let Ok(decoded) = Content::decode(content) else {
        return String::new();
    };
    let mut state = TextState::new();
    for operation in decoded.operations {
        match operation.operator.as_str() {
            "BT" => state.begin_text(),
            "Td" => {
                if let (Some(tx), Some(ty)) = (
                    operand_number(operation.operands.first()),
                    operand_number(operation.operands.get(1)),
                ) {
                    state.translate(tx, ty);
                }
            }
            "TD" => {
                if let Some(ty) = operand_number(operation.operands.get(1)) {
                    state.leading = -ty;
                }
                if let (Some(tx), Some(ty)) = (
                    operand_number(operation.operands.first()),
                    operand_number(operation.operands.get(1)),
                ) {
                    state.translate(tx, ty);
                }
            }
            "Tm" => {
                let mut values = [0.0; 6];
                let mut complete = true;
                for (index, value) in values.iter_mut().enumerate() {
                    match operand_number(operation.operands.get(index)) {
                        Some(number) => *value = number,
                        None => complete = false,
                    }
                }
                if complete {
                    state.set_matrix(values);
                }
            }
            "TL" => {
                if let Some(leading) = operand_number(operation.operands.first()) {
                    state.leading = leading;
                }
            }
            "T*" => state.translate(0.0, -state.leading),
            "Tj" => state.show(&operand_text(operation.operands.first())),
            "'" => {
                state.translate(0.0, -state.leading);
                state.show(&operand_text(operation.operands.first()));
            }
            "\"" => {
                state.translate(0.0, -state.leading);
                state.show(&operand_text(operation.operands.get(2)));
            }
            "TJ" => show_tj(&mut state, operation.operands.first()),
            _ => {}
        }
    }
    state.buffer
}

fn show_tj(state: &mut TextState, operand: Option<&Object>) {
    let Some(Object::Array(items)) = operand else {
        return;
    };
    for item in items {
        if let Some(text) = string_text(item) {
            state.show(&text);
        } else if let Some(adjustment) = operand_number(Some(item)) {
            // pypdf inserts a space when a TJ adjustment is at least ~95% of
            // half a space width. Small kerning numbers stay invisible.
            if adjustment.abs() >= 150.0 {
                state.show(" ");
            }
        }
    }
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
