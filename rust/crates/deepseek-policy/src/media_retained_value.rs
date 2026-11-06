//! Values accepted by the original media reader, before response encoding.
//!
//! Neither arbitrary integers nor Python strings/nonfinite floats can be
//! faithfully materialized as an ordinary serde_json::Value in every feature
//! graph. Flat nodes retain those values through selection and normalization.
use super::document::deferred;
use crate::app_error::AppError;
use std::io::{self, Write};

#[path = "media_retained_flat_patch.rs"]
mod flat_patch;
#[path = "media_retained_float.rs"]
mod float;
#[path = "media_retained_patch.rs"]
mod patch;
#[path = "media_unicode15_repr.rs"]
mod unicode15;
pub use patch::{PublicRecordError, ordered_media_members, validate_patch_values};

#[derive(Clone, Debug, Default, Eq, Hash, PartialEq)]
pub(super) struct Text(pub(super) Vec<u16>);

impl From<&str> for Text {
    fn from(value: &str) -> Self {
        Self(value.encode_utf16().collect())
    }
}

impl Text {
    pub(super) fn scalars(&self) -> impl Iterator<Item = u32> + '_ {
        char::decode_utf16(self.0.iter().copied())
            .map(|item| item.map_or_else(|error| u32::from(error.unpaired_surrogate()), u32::from))
    }

    pub(super) fn push(&mut self, scalar: u32) {
        if let Some(character) = char::from_u32(scalar) {
            let mut units = [0; 2];
            self.0.extend_from_slice(character.encode_utf16(&mut units));
        } else {
            // Only escaped UTF-16 surrogates enter the non-scalar branch.
            self.0.push(scalar as u16);
        }
    }

    pub(super) fn utf8(&self) -> Result<String, ()> {
        String::from_utf16(&self.0).map_err(|_| ())
    }

    fn has_only_scalars(&self) -> bool {
        self.scalars()
            .all(|scalar| char::from_u32(scalar).is_some())
    }

    pub(super) fn equals(&self, text: &str) -> bool {
        self.0.iter().copied().eq(text.encode_utf16())
    }

    pub(super) fn take(&self, limit: usize) -> Self {
        if self.0.len() <= limit {
            return self.clone();
        }
        let mut result = Self::default();
        for scalar in self.scalars().take(limit) {
            result.push(scalar);
        }
        result
    }

    pub(super) fn trimmed(&self) -> Self {
        let scalars: Vec<_> = self.scalars().collect();
        let start = scalars
            .iter()
            .position(|value| !whitespace(*value))
            .unwrap_or(scalars.len());
        let end = scalars
            .iter()
            .rposition(|value| !whitespace(*value))
            .map_or(start, |at| at + 1);
        let mut result = Self::default();
        for scalar in &scalars[start..end] {
            result.push(*scalar);
        }
        result
    }

    pub(super) fn lower(&self) -> Self {
        use unicode15::CaseContext;
        if self.0.iter().all(|unit| *unit <= 127) {
            return Self(
                self.0
                    .iter()
                    .map(|unit| u16::from((*unit as u8).to_ascii_lowercase()))
                    .collect(),
            );
        }
        let scalars: Vec<_> = self.scalars().collect();
        let mut result = Self::default();
        let mut preceded_by_cased = false;
        for (index, scalar) in scalars.iter().copied().enumerate() {
            match scalar {
                0x130 => {
                    result.push(0x69);
                    result.push(0x307);
                }
                0x3a3 if preceded_by_cased => {
                    let followed_by_cased = scalars[index + 1..]
                        .iter()
                        .find_map(|value| match unicode15::case_context(*value) {
                            CaseContext::Boundary => Some(false),
                            CaseContext::Cased => Some(true),
                            CaseContext::Ignorable => None,
                        })
                        .unwrap_or(false);
                    result.push(if followed_by_cased { 0x3c3 } else { 0x3c2 });
                }
                value => result.push(unicode15::lower_simple(value)),
            }
            match unicode15::case_context(scalar) {
                CaseContext::Cased => preceded_by_cased = true,
                CaseContext::Boundary => preceded_by_cased = false,
                CaseContext::Ignorable => {}
            }
        }
        result
    }

    pub(super) fn write_json(
        &self,
        output: &mut (impl Write + ?Sized),
        strict: bool,
    ) -> io::Result<bool> {
        if let Ok(text) = self.utf8() {
            serde_json::to_writer(&mut *output, &text).map_err(io::Error::other)?;
            return Ok(true);
        }
        if strict {
            return Err(encoding_error());
        }
        output.write_all(b"\"")?;
        for scalar in self.scalars() {
            match scalar {
                34 => output.write_all(b"\\\"")?,
                92 => output.write_all(b"\\\\")?,
                8 => output.write_all(b"\\b")?,
                12 => output.write_all(b"\\f")?,
                10 => output.write_all(b"\\n")?,
                13 => output.write_all(b"\\r")?,
                9 => output.write_all(b"\\t")?,
                0..=31 => write!(output, "\\u{scalar:04x}")?,
                0xd800..=0xdfff => write!(output, "\\u{scalar:04x}")?,
                value => {
                    let character = char::from_u32(value).ok_or_else(encoding_error)?;
                    let mut bytes = [0; 4];
                    output.write_all(character.encode_utf8(&mut bytes).as_bytes())?;
                }
            }
        }
        output.write_all(b"\"")?;
        Ok(false)
    }
}

pub(super) fn whitespace(value: u32) -> bool {
    matches!(value, 0x1c..=0x1f) || char::from_u32(value).is_some_and(char::is_whitespace)
}

#[derive(Clone, Debug)]
pub(super) enum Node {
    Null,
    Bool(bool),
    Integer(String),
    Float(f64),
    Text(Text),
    Array(Vec<usize>),
    Object(Vec<(Text, usize)>),
}

#[derive(Clone, Debug)]
pub struct Document {
    pub(super) nodes: Vec<Node>,
    pub(super) root: usize,
}

fn encoding_error() -> io::Error {
    io::Error::new(
        io::ErrorKind::InvalidData,
        "media value is not encodable as a public JSON response",
    )
}

impl Document {
    pub(super) fn empty_builder() -> Self {
        Self {
            nodes: Vec::new(),
            root: 0,
        }
    }

    pub(super) fn decode(bytes: &[u8]) -> Result<Self, ()> {
        let (raw, root) = deferred::parse(bytes)?;
        let mut winners = vec![false; raw.len()];
        winners[root] = true;
        // The parser stores children before parents, so reverse order marks
        // precisely the reachable winning values without another stack.
        for (id, node) in raw.iter().enumerate().rev() {
            if !winners[id] {
                continue;
            }
            match node {
                deferred::Node::Leaf(_) => {}
                deferred::Node::Array(items) => {
                    for child in items {
                        winners[*child] = true;
                    }
                }
                deferred::Node::Object(fields) => {
                    for (_, child) in fields {
                        winners[*child] = true;
                    }
                }
            }
        }
        let mut nodes = Vec::with_capacity(raw.len());
        for (id, node) in raw.into_iter().enumerate() {
            if !winners[id] {
                // Keep stable indices without interpreting overwritten leaves.
                nodes.push(Node::Null);
                continue;
            }
            nodes.push(match node {
                deferred::Node::Leaf(span) => {
                    let token = &bytes[span];
                    match token {
                        b"null" => Node::Null,
                        b"true" => Node::Bool(true),
                        b"false" => Node::Bool(false),
                        b"NaN" => Node::Float(f64::NAN),
                        b"Infinity" => Node::Float(f64::INFINITY),
                        b"-Infinity" => Node::Float(f64::NEG_INFINITY),
                        [b'"', ..] => Node::Text(Text(deferred::text_units(token)?)),
                        _ => {
                            let text = std::str::from_utf8(token).map_err(|_| ())?;
                            if token.iter().any(|byte| matches!(byte, b'.' | b'e' | b'E')) {
                                Node::Float(float::parse(text)?)
                            } else {
                                Node::Integer(if text == "-0" {
                                    "0".to_string()
                                } else {
                                    text.to_string()
                                })
                            }
                        }
                    }
                }
                deferred::Node::Array(items) => Node::Array(items),
                deferred::Node::Object(fields) => Node::Object(
                    fields
                        .into_iter()
                        .map(|(key, value)| Ok((Text(deferred::text_units(&bytes[key])?), value)))
                        .collect::<Result<_, ()>>()?,
                ),
            });
        }
        Ok(Self { nodes, root })
    }

    pub(super) fn add(&mut self, node: Node) -> usize {
        let id = self.nodes.len();
        self.nodes.push(node);
        id
    }

    pub(super) fn field(&self, id: usize, name: &str) -> Option<usize> {
        let Node::Object(fields) = &self.nodes[id] else {
            return None;
        };
        fields
            .iter()
            .find(|(key, _)| key.equals(name))
            .map(|(_, value)| *value)
    }

    pub(super) fn truthy(&self, id: usize) -> bool {
        match &self.nodes[id] {
            Node::Null => false,
            Node::Bool(value) => *value,
            Node::Integer(value) => value != "0",
            Node::Float(value) => *value != 0.0,
            Node::Text(value) => !value.0.is_empty(),
            Node::Array(values) => !values.is_empty(),
            Node::Object(fields) => !fields.is_empty(),
        }
    }

    /// Python str(), with the caller's established business truncation only.
    /// Containers use repr(), which escapes unpaired units rather than losing
    /// them. Work and drop stacks stay on the heap at arbitrary JSON depth.
    pub(super) fn text(&self, id: usize, limit: usize) -> Text {
        if let Node::Text(text) = &self.nodes[id] {
            return text.take(limit);
        }
        enum Part<'a> {
            Value(usize),
            Text(&'a str),
            Quoted(&'a Text),
            Array(usize, usize),
            Object(usize, usize),
        }
        let mut result = Text::default();
        let mut count = 0usize;
        let mut pending = vec![Part::Value(id)];
        while count < limit {
            let Some(part) = pending.pop() else { break };
            let text = match part {
                Part::Text(text) => Text::from(text),
                Part::Quoted(text) => repr_text(text, limit - count),
                Part::Array(id, index) => {
                    let Node::Array(values) = &self.nodes[id] else {
                        unreachable!("array frame")
                    };
                    if let Some(value) = values.get(index) {
                        pending.push(Part::Array(id, index + 1));
                        pending.push(Part::Value(*value));
                        Text::from(if index == 0 { "" } else { ", " })
                    } else {
                        Text::from("]")
                    }
                }
                Part::Object(id, index) => {
                    let Node::Object(fields) = &self.nodes[id] else {
                        unreachable!("object frame")
                    };
                    if let Some((key, value)) = fields.get(index) {
                        pending.push(Part::Object(id, index + 1));
                        pending.push(Part::Value(*value));
                        pending.push(Part::Text(": "));
                        pending.push(Part::Quoted(key));
                        Text::from(if index == 0 { "" } else { ", " })
                    } else {
                        Text::from("}")
                    }
                }
                Part::Value(id) => match &self.nodes[id] {
                    Node::Null => Text::from("None"),
                    Node::Bool(true) => Text::from("True"),
                    Node::Bool(false) => Text::from("False"),
                    Node::Integer(text) => Text::from(&text[..text.len().min(limit - count)]),
                    Node::Float(value) => Text::from(float::text(*value).as_str()),
                    Node::Text(text) => repr_text(text, limit - count),
                    Node::Array(_) => {
                        pending.push(Part::Array(id, 0));
                        Text::from("[")
                    }
                    Node::Object(_) => {
                        pending.push(Part::Object(id, 0));
                        Text::from("{")
                    }
                },
            };
            for scalar in text.scalars().take(limit - count) {
                result.push(scalar);
                count += 1;
            }
        }
        result
    }

    pub fn normalize_media(&self) -> Result<MediaRecord, AppError> {
        super::retained_schema::normalize(self)
    }

    pub(super) fn write_json(
        &self,
        output: &mut (impl Write + ?Sized),
        strict: bool,
    ) -> io::Result<bool> {
        enum Part {
            Value(usize),
            Byte(u8),
            Key(usize, usize),
        }
        let mut pending = vec![Part::Value(self.root)];
        let mut durable_unicode = true;
        while let Some(part) = pending.pop() {
            match part {
                Part::Byte(byte) => output.write_all(&[byte])?,
                Part::Key(id, index) => {
                    let Node::Object(fields) = &self.nodes[id] else {
                        return Err(encoding_error());
                    };
                    durable_unicode &= fields[index].0.write_json(output, strict)?;
                }
                Part::Value(id) => match &self.nodes[id] {
                    Node::Null => output.write_all(b"null")?,
                    Node::Bool(value) => {
                        output.write_all(if *value { b"true" } else { b"false" })?
                    }
                    Node::Integer(value) => output.write_all(value.as_bytes())?,
                    Node::Float(value) if value.is_finite() => {
                        output.write_all(float::text(*value).as_bytes())?;
                    }
                    Node::Float(_) if strict => return Err(encoding_error()),
                    Node::Float(value) => output.write_all(if value.is_nan() {
                        b"NaN"
                    } else if value.is_sign_positive() {
                        b"Infinity"
                    } else {
                        b"-Infinity"
                    })?,
                    Node::Text(text) => durable_unicode &= text.write_json(output, strict)?,
                    Node::Array(values) => {
                        output.write_all(b"[")?;
                        pending.push(Part::Byte(b']'));
                        for (at, value) in values.iter().enumerate().rev() {
                            pending.push(Part::Value(*value));
                            if at != 0 {
                                pending.push(Part::Byte(b','));
                            }
                        }
                    }
                    Node::Object(fields) => {
                        output.write_all(b"{")?;
                        pending.push(Part::Byte(b'}'));
                        for (at, (_, value)) in fields.iter().enumerate().rev() {
                            pending.push(Part::Value(*value));
                            pending.push(Part::Byte(b':'));
                            pending.push(Part::Key(id, at));
                            if at != 0 {
                                pending.push(Part::Byte(b','));
                            }
                        }
                    }
                },
            }
        }
        Ok(durable_unicode)
    }
}

fn repr_text(text: &Text, limit: usize) -> Text {
    if limit == 0 {
        return Text::default();
    }
    let quote = if text.0.contains(&39) && !text.0.contains(&34) {
        34
    } else {
        39
    };
    let mut result = Text::default();
    result.push(quote);
    let mut remaining = limit - 1;
    for scalar in text.scalars() {
        if remaining == 0 {
            return result;
        }
        let escaped = match scalar {
            92 => Some("\\\\".to_string()),
            10 => Some("\\n".to_string()),
            13 => Some("\\r".to_string()),
            9 => Some("\\t".to_string()),
            value if value == quote => Some(format!("\\{}", char::from_u32(value).expect("quote"))),
            value if !matches!(value, 32..=126) && !unicode15::printable(value) => {
                Some(if value <= 0xff {
                    format!("\\x{value:02x}")
                } else if value <= 0xffff {
                    format!("\\u{value:04x}")
                } else {
                    format!("\\U{value:08x}")
                })
            }
            _ => None,
        };
        if let Some(escaped) = escaped {
            let count = remaining.min(escaped.len());
            result.0.extend(escaped[..count].encode_utf16());
            remaining -= count;
        } else {
            result.push(scalar);
            remaining -= 1;
        }
    }
    if remaining != 0 {
        result.push(quote);
    }
    result
}

/// Constructed only by the original schema normalizer or its digest-checked
/// private spool. Raw document bytes have no response or mutation interface.
#[derive(Clone, Debug)]
pub struct MediaRecord(pub(super) Document);

impl MediaRecord {
    pub fn normalize_again(&self) -> Result<Self, AppError> {
        self.0.normalize_media()
    }

    fn text_field(&self, field: &str) -> Option<&Text> {
        let id = self.0.field(self.0.root, field)?;
        if let Node::Text(text) = &self.0.nodes[id] {
            Some(text)
        } else {
            None
        }
    }

    pub fn field_equals(&self, field: &str, expected: &str) -> bool {
        self.text_field(field)
            .is_some_and(|text| text.equals(expected))
    }

    pub fn id(&self) -> String {
        self.text_field("mediaId")
            .expect("normalized mediaId")
            .utf8()
            .expect("validated mediaId")
    }

    /// UTF-8 orders scalar values lexicographically. Encode isolated units as
    /// three bytes in that same order in this SQLite BLOB only.
    pub fn sort_key(&self) -> Vec<u8> {
        let stamp = self.text_field("updatedAt").expect("normalized updatedAt");
        if let Ok(text) = stamp.utf8() {
            return text.into_bytes();
        }
        let mut key = Vec::with_capacity(stamp.0.len());
        for scalar in stamp.scalars() {
            if let Some(character) = char::from_u32(scalar) {
                let mut bytes = [0; 4];
                key.extend_from_slice(character.encode_utf8(&mut bytes).as_bytes());
            } else {
                key.extend_from_slice(&[
                    0xe0 | ((scalar >> 12) as u8),
                    0x80 | (((scalar >> 6) & 0x3f) as u8),
                    0x80 | ((scalar & 0x3f) as u8),
                ]);
            }
        }
        key
    }

    pub fn write_response(&self, output: &mut (impl Write + ?Sized)) -> io::Result<()> {
        self.0.write_json(output, true).map(|_| ())
    }

    /// True when the emitted strings can also be written by the durable store.
    pub(super) fn write_spool(&self, output: &mut (impl Write + ?Sized)) -> io::Result<bool> {
        self.0.write_json(output, false)
    }

    pub(super) fn from_spool(bytes: &[u8]) -> Result<Self, ()> {
        let record = Self(Document::decode(bytes)?);
        if !record
            .text_field("mediaId")
            .is_some_and(Text::has_only_scalars)
        {
            return Err(());
        }
        Ok(record)
    }

    /// Published images have Unicode scalar values throughout. Nonfinite
    /// numbers are valid in these images and do not affect this check.
    pub(super) fn has_durable_unicode(&self) -> bool {
        self.0.nodes.iter().all(|node| match node {
            Node::Text(text) => text.has_only_scalars(),
            Node::Object(fields) => fields.iter().all(|(key, _)| key.has_only_scalars()),
            _ => true,
        })
    }

    #[cfg(test)]
    pub fn test_value(&self) -> serde_json::Value {
        let mut bytes = Vec::new();
        self.write_response(&mut bytes)
            .expect("ordinary test response");
        serde_json::from_slice(&bytes).expect("ordinary test Value")
    }
}
