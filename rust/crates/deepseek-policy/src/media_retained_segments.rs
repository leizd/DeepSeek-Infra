//! Segment reads normalize one selected row at a time after complete JSON validation.
use super::{
    RawRecords, VisitError, document,
    retained::{Document, Node, Text},
    retained_schema::{field_text, path_text, source_ref},
};
use crate::{
    app_error::{AppError, codes},
    entropy::SystemEntropy,
    media_schema::MAX_SEGMENT_TEXT_CHARS,
    workspace_schema::{new_id, redact_sensitive_text, validate_workspace_id},
};
use std::io::{Read, Write};

const SEGMENT_TYPES: [&str; 6] = [
    "ocr_text",
    "caption",
    "transcript",
    "frame",
    "page_text",
    "webpage_text",
];

fn server_error() -> AppError {
    AppError {
        status: 500,
        code: codes::INTERNAL,
        message: "Server error".into(),
    }
}

/// Write a complete response into the caller's private spool. Syntax and
/// source I/O have the original empty fallback; invalid UTF-8 and private
/// storage or public encoding errors are surfaced before any HTTP delivery.
pub fn write_segment_response(
    source: Option<impl Read>,
    media_id: &str,
    output: &mut dyn Write,
) -> Result<(), AppError> {
    let Some(source) = source else {
        return write_empty_response(output);
    };
    let rows = RawRecords::empty().map_err(|_| server_error())?;
    let rows = match document::read_segments(source, rows) {
        Ok(rows) => rows,
        Err(document::Error::Syntax | document::Error::SourceIo) => {
            return write_empty_response(output);
        }
        Err(_) => return Err(server_error()),
    };
    output
        .write_all(b"{\"ok\":true,\"segments\":[")
        .map_err(|_| server_error())?;
    let mut first = true;
    rows.visit_indexed_retained(|ordinal, source| {
        if let Some(segment) = normalize(&source, media_id, ordinal)? {
            if !first {
                output.write_all(b",").map_err(|_| server_error())?;
            }
            first = false;
            segment
                .write_json(output, true)
                .map_err(|_| server_error())?;
        }
        Ok(())
    })
    .map_err(|error| match error {
        VisitError::Storage(_) => server_error(),
        VisitError::Visitor(error) => error,
    })?;
    output.write_all(b"]}").map_err(|_| server_error())
}

fn write_empty_response(output: &mut dyn Write) -> Result<(), AppError> {
    output
        .write_all(b"{\"ok\":true,\"segments\":[]}")
        .map_err(|_| server_error())
}

fn normalize(input: &Document, media_id: &str, ordinal: i64) -> Result<Option<Document>, AppError> {
    let kind = input
        .field(input.root, "type")
        .filter(|id| input.truthy(*id))
        .map_or_else(|| Text::from("page_text"), |id| input.text(id, usize::MAX));
    let Ok(kind) = kind.trimmed().lower().utf8() else {
        return Ok(None);
    };
    if !SEGMENT_TYPES.contains(&kind.as_str()) {
        return Ok(None);
    }
    let Ok(raw_id) = field_text(input, "segmentId").trimmed().utf8() else {
        return Ok(None);
    };
    let id = if raw_id.is_empty() {
        new_id("seg", &SystemEntropy).map_err(|_| server_error())?
    } else if let Ok(id) = validate_workspace_id(&raw_id, "segment id") {
        id
    } else {
        return Ok(None);
    };

    let text = redact_segment_text(&field_text(input, "text"));
    let confidence = confidence(input)?;
    let index = match non_null_field(input, "index") {
        Some(index) => integer(&input.nodes[index]).ok_or_else(server_error)?,
        None => ordinal.to_string(),
    };
    let mut output = Document::empty_builder();
    let mut fields = Vec::new();
    for (key, value) in [
        ("segmentId", Node::Text(Text::from(id.as_str()))),
        ("mediaId", Node::Text(Text::from(media_id))),
        ("type", Node::Text(Text::from(kind.as_str()))),
        ("text", Node::Text(text)),
        ("confidence", Node::Float(confidence)),
        ("index", Node::Integer(index)),
    ] {
        fields.push((Text::from(key), output.add(value)));
    }
    if let Some(page) = non_null_field(input, "page") {
        let number = if input.truthy(page) {
            integer(&input.nodes[page]).ok_or_else(server_error)?
        } else {
            "1".into()
        };
        let number = if number == "0" || number.starts_with('-') {
            "1".into()
        } else {
            number
        };
        fields.push((Text::from("page"), output.add(Node::Integer(number))));
    }
    if let Some((start, end)) = time_range(input)? {
        let start = output.add(Node::Float(start));
        let end = output.add(Node::Float(end));
        fields.push((
            Text::from("timeRange"),
            output.add(Node::Array(vec![start, end])),
        ));
    }
    let Ok(frame) = path_text(field_text(input, "framePath")) else {
        return Ok(None);
    };
    if !frame.0.is_empty() {
        fields.push((Text::from("framePath"), output.add(Node::Text(frame))));
    }
    if let Some(citation) = input
        .field(input.root, "citation")
        .filter(|id| matches!(&input.nodes[*id], Node::Object(_)))
    {
        fields.push((
            Text::from("citation"),
            source_ref(input, citation, &mut output),
        ));
    }
    output.root = output.add(Node::Object(fields));
    Ok(Some(output))
}

fn non_null_field(input: &Document, name: &str) -> Option<usize> {
    input
        .field(input.root, name)
        .filter(|id| !matches!(&input.nodes[*id], Node::Null))
}

fn redact_segment_text(text: &Text) -> Text {
    let limit = MAX_SEGMENT_TEXT_CHARS;
    if let Ok(text) = text.utf8() {
        let redacted: String = redact_sensitive_text(&text).chars().take(limit).collect();
        return Text::from(redacted.as_str());
    }
    // Both stand-ins have the same word and space classes as an unpaired
    // unit. Comparing two views avoids collisions with original characters.
    // If a unit survives redaction and truncation, defer the encoding failure
    // until the row is known to survive all remaining normalization.
    let view = |stand_in| {
        let text: String = text
            .scalars()
            .map(|scalar| char::from_u32(scalar).unwrap_or(stand_in))
            .collect();
        redact_sensitive_text(&text)
    };
    let first: String = view('\u{e000}').chars().take(limit).collect();
    if first.chars().eq(view('\u{fffd}').chars().take(limit)) {
        Text::from(first.as_str())
    } else {
        Text(vec![0xd800])
    }
}

// Starts of the decimal digit runs in the project's Unicode 15.0 oracle.
const DECIMAL_ZEROS: [u32; 68] = [
    0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
    0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
    0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0, 0xff10,
    0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0, 0x11650,
    0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x11f50, 0x16a60, 0x16ac0,
    0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0, 0x1e950,
    0x1fbf0,
];

fn numeric_text(value: &Text) -> Option<String> {
    let mut ascii = String::new();
    for character in value.utf8().ok()?.trim().chars() {
        if character.is_ascii() {
            ascii.push(character);
        } else {
            let scalar = u32::from(character);
            let position = DECIMAL_ZEROS.partition_point(|zero| *zero <= scalar);
            let digit = scalar - DECIMAL_ZEROS[position.checked_sub(1)?];
            ascii.push(char::from_digit(digit, 10)?);
        }
    }
    let raw = ascii.as_bytes();
    for (index, &byte) in raw.iter().enumerate() {
        if byte == b'_'
            && (index == 0
                || !raw[index - 1].is_ascii_digit()
                || raw.get(index + 1).is_none_or(|byte| !byte.is_ascii_digit()))
        {
            return None;
        }
    }
    ascii.retain(|character| character != '_');
    Some(ascii)
}

fn integer(node: &Node) -> Option<String> {
    match node {
        Node::Integer(value) => Some(value.clone()),
        Node::Bool(value) => Some(if *value { "1" } else { "0" }.into()),
        Node::Float(value) if value.is_finite() => {
            let value = value.trunc();
            Some(if value == 0.0 {
                "0".into()
            } else {
                format!("{value:.0}")
            })
        }
        Node::Text(value) => {
            let raw = numeric_text(value)?;
            let (sign, digits) = if let Some(digits) = raw.strip_prefix('-') {
                ("-", digits)
            } else {
                ("", raw.strip_prefix('+').unwrap_or(&raw))
            };
            if digits.is_empty() || !digits.bytes().all(|digit| digit.is_ascii_digit()) {
                return None;
            }
            let digits = digits.trim_start_matches('0');
            Some(if digits.is_empty() {
                "0".into()
            } else {
                format!("{sign}{digits}")
            })
        }
        _ => None,
    }
}

fn float(node: &Node) -> Result<Option<f64>, AppError> {
    let value = match node {
        Node::Float(value) => Some(*value),
        Node::Bool(value) => Some(if *value { 1.0 } else { 0.0 }),
        Node::Integer(value) => {
            let value = value.parse::<f64>().map_err(|_| server_error())?;
            if value.is_infinite() {
                return Err(server_error());
            }
            Some(value)
        }
        Node::Text(value) => numeric_text(value).and_then(|text| {
            let lower = text.to_ascii_lowercase();
            let unsigned = lower
                .strip_prefix('+')
                .or_else(|| lower.strip_prefix('-'))
                .unwrap_or(&lower);
            match unsigned {
                "nan" => Some(f64::NAN),
                "inf" | "infinity" => Some(if lower.starts_with('-') {
                    f64::NEG_INFINITY
                } else {
                    f64::INFINITY
                }),
                _ => text.parse().ok(),
            }
        }),
        _ => None,
    };
    Ok(value)
}

fn confidence(input: &Document) -> Result<f64, AppError> {
    let Some(id) = non_null_field(input, "confidence") else {
        return Ok(1.0);
    };
    let value = float(&input.nodes[id])?.unwrap_or(1.0);
    let value = if value < 1.0 { value } else { 1.0 };
    Ok(round(if value > 0.0 { value } else { 0.0 }, 4))
}

fn time_range(input: &Document) -> Result<Option<(f64, f64)>, AppError> {
    let Some(Node::Array(values)) = input
        .field(input.root, "timeRange")
        .map(|id| &input.nodes[id])
    else {
        return Ok(None);
    };
    let [start, end, ..] = values.as_slice() else {
        return Ok(None);
    };
    let Some(start) = float(&input.nodes[*start])? else {
        return Ok(None);
    };
    let start = if start > 0.0 { start } else { 0.0 };
    let Some(end) = float(&input.nodes[*end])? else {
        return Ok(None);
    };
    let end = if end > start { end } else { start };
    Ok(Some((round(start, 3), round(end, 3))))
}

fn round(value: f64, digits: usize) -> f64 {
    format!("{value:.digits$}").parse().unwrap_or(value)
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::{Engine as _, engine::general_purpose::STANDARD};
    use serde_json::Value;

    #[test]
    fn retained_segment_reader_matches_the_original_http_corpus() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../../tests/fixtures/native_retained_media_segments_v1.json"
        ))
        .unwrap();
        let cases = fixture["cases"].as_array().unwrap();
        assert_eq!(cases.len(), 24);
        assert_eq!(fixture["requests"], 48);
        assert_eq!(fixture["actualOriginalTCP"], true);
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let input = case["rawBase64"]
                .as_str()
                .map(|text| STANDARD.decode(text).unwrap());
            let mut output = Vec::new();
            let result = write_segment_response(
                input.as_deref(),
                case["mediaId"].as_str().unwrap(),
                &mut output,
            );
            if case["status"] == 200 {
                assert!(result.is_ok(), "{name}: {result:?}");
                assert_eq!(
                    String::from_utf8(output).unwrap(),
                    case["bodyJson"].as_str().unwrap(),
                    "{name}",
                );
            } else {
                let error = result.err().unwrap_or_else(|| panic!("{name}: succeeded"));
                assert_eq!(error.status, 500, "{name}");
                assert_eq!(error.code, codes::INTERNAL, "{name}");
            }
        }
    }
}
