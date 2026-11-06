//! Prepare a selected PATCH directly on the retained value model.
use super::{Document, MediaRecord, Node, Text, float};
use crate::app_error::AppError;
use deepseek_protocol::generated::deepseek::media::v1::{
    MediaArray, MediaFlatPatchRecord, MediaMember, MediaObject, MediaPatch, MediaValue,
    media_flat_patch_node::Kind as FlatKind, media_value::Kind,
};
use std::collections::{HashMap, HashSet};

const LEGACY_VALUE_DEPTH: usize = 128;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PublicRecordError {
    Unencodable,
    TooDeep,
    TooLarge,
}

fn invalid_patch() -> AppError {
    AppError::invalid_payload("Invalid media update")
}

pub fn validate_patch_values(patch: &MediaPatch) -> Result<(), AppError> {
    Document::empty_builder().patch_values(patch).map(|_| ())
}

/// Check canonical keys and restore the original dictionary insertion order.
pub fn ordered_media_members(members: &[MediaMember]) -> Result<Vec<&MediaMember>, AppError> {
    if members.windows(2).any(|pair| pair[0].key >= pair[1].key) {
        return Err(invalid_patch());
    }
    let mut ordered: Vec<_> = members.iter().collect();
    if ordered.iter().any(|member| member.source_ordinal != 0) {
        ordered.sort_by_key(|member| member.source_ordinal);
        if ordered
            .iter()
            .enumerate()
            .any(|(position, member)| member.source_ordinal as usize != position + 1)
        {
            return Err(invalid_patch());
        }
    }
    Ok(ordered)
}

fn merge_fields(fields: &mut Vec<(Text, usize)>, replacements: Vec<(Text, usize)>) {
    let mut positions: HashMap<_, _> = fields
        .iter()
        .enumerate()
        .map(|(position, (key, _))| (key.clone(), position))
        .collect();
    for (key, value) in replacements {
        let position = positions.entry(key.clone()).or_insert(fields.len());
        if *position == fields.len() {
            fields.push((key, value));
        } else {
            fields[*position].1 = value;
        }
    }
}

impl Document {
    fn patch_values(&mut self, patch: &MediaPatch) -> Result<Vec<(Text, usize)>, AppError> {
        let nested =
            patch.title.is_some() || patch.project_id.is_some() || patch.metadata.is_some();
        if let Some(record) = patch.flat_values.as_ref() {
            if nested {
                return Err(invalid_patch());
            }
            return self.add_flat_patch_values(record);
        }
        if !nested {
            return Err(invalid_patch());
        }
        [
            ("title", patch.title.as_ref()),
            ("projectId", patch.project_id.as_ref()),
            ("metadata", patch.metadata.as_ref()),
        ]
        .into_iter()
        .filter_map(|(name, value)| value.map(|value| (name, value)))
        .map(|(name, value)| Ok((Text::from(name), self.add_patch_value(value, 0)?)))
        .collect()
    }

    fn patch_number(value: &str, flat: bool) -> Result<Node, AppError> {
        let text = value.trim();
        if flat {
            if text != value {
                return Err(invalid_patch());
            }
            match text {
                "NaN" => return Ok(Node::Float(f64::NAN)),
                "Infinity" => return Ok(Node::Float(f64::INFINITY)),
                "-Infinity" => return Ok(Node::Float(f64::NEG_INFINITY)),
                _ => {}
            }
        }
        let digits = text.strip_prefix('-').unwrap_or(text);
        if !matches!(digits.as_bytes().first(), Some(b'0'..=b'9')) {
            return Err(invalid_patch());
        }
        let mut decoded = Self::decode(value.as_bytes()).map_err(|_| invalid_patch())?;
        match decoded.nodes.pop() {
            Some(node @ (Node::Integer(_) | Node::Float(_))) => Ok(node),
            _ => Err(invalid_patch()),
        }
    }

    fn add_flat_patch_values(
        &mut self,
        record: &MediaFlatPatchRecord,
    ) -> Result<Vec<(Text, usize)>, AppError> {
        if record.nodes.is_empty() {
            return Err(invalid_patch());
        }
        let start = self.nodes.len();
        let mut referenced = vec![false; record.nodes.len()];
        for (index, value) in record.nodes.iter().enumerate() {
            let mut reference = |child: u32| {
                let slot = (child as usize).checked_sub(1).ok_or_else(invalid_patch)?;
                if slot >= index || referenced[slot] {
                    return Err(invalid_patch());
                }
                referenced[slot] = true;
                Ok(start + slot)
            };
            let node = match value.kind.as_ref().ok_or_else(invalid_patch)? {
                FlatKind::NullValue(true) => Node::Null,
                FlatKind::NullValue(false) => return Err(invalid_patch()),
                FlatKind::BoolValue(value) => Node::Bool(*value),
                FlatKind::TextValue(value) => Node::Text(Text::from(value.as_str())),
                FlatKind::TextUtf16Le(value) => Node::Text(unpaired_patch_text(value)?),
                FlatKind::NumberValue(value) => Self::patch_number(value, true)?,
                FlatKind::ArrayValue(array) => Node::Array(
                    array
                        .children
                        .iter()
                        .copied()
                        .map(reference)
                        .collect::<Result<_, _>>()?,
                ),
                FlatKind::ObjectValue(object) => {
                    let mut keys = HashSet::new();
                    let mut fields = Vec::with_capacity(object.members.len());
                    for member in &object.members {
                        let key = if member.key_utf16_le.is_empty() {
                            Text::from(member.key.as_str())
                        } else if member.key.is_empty() {
                            unpaired_patch_text(&member.key_utf16_le)?
                        } else {
                            return Err(invalid_patch());
                        };
                        if !keys.insert(key.clone()) {
                            return Err(invalid_patch());
                        }
                        fields.push((key, reference(member.child)?));
                    }
                    Node::Object(fields)
                }
            };
            self.add(node);
        }
        if referenced[..referenced.len() - 1].iter().any(|&used| !used) {
            return Err(invalid_patch());
        }
        let Some(Node::Object(fields)) = self.nodes.last() else {
            return Err(invalid_patch());
        };
        if fields.is_empty()
            || fields.iter().any(|(key, _)| {
                !key.equals("title") && !key.equals("projectId") && !key.equals("metadata")
            })
        {
            return Err(invalid_patch());
        }
        Ok(fields.clone())
    }

    fn add_patch_value(&mut self, value: &MediaValue, depth: usize) -> Result<usize, AppError> {
        if depth > LEGACY_VALUE_DEPTH {
            return Err(invalid_patch());
        }
        let node = match value.kind.as_ref().ok_or_else(invalid_patch)? {
            Kind::NullValue(true) => Node::Null,
            Kind::NullValue(false) => return Err(invalid_patch()),
            Kind::BoolValue(value) => Node::Bool(*value),
            Kind::TextValue(value) => Node::Text(Text::from(value.as_str())),
            Kind::NumberValue(value) => Self::patch_number(value, false)?,
            Kind::ArrayValue(array) => Node::Array(
                array
                    .values
                    .iter()
                    .map(|child| self.add_patch_value(child, depth + 1))
                    .collect::<Result<_, _>>()?,
            ),
            Kind::ObjectValue(object) => Node::Object(
                ordered_media_members(&object.members)?
                    .into_iter()
                    .map(|member| {
                        let child = member.value.as_ref().ok_or_else(invalid_patch)?;
                        Ok((
                            Text::from(member.key.as_str()),
                            self.add_patch_value(child, depth + 1)?,
                        ))
                    })
                    .collect::<Result<_, AppError>>()?,
            ),
        };
        Ok(self.add(node))
    }

    fn public_value(&self, id: usize, depth: usize) -> Result<MediaValue, PublicRecordError> {
        if depth > LEGACY_VALUE_DEPTH {
            return Err(PublicRecordError::TooDeep);
        }
        let kind = match &self.nodes[id] {
            Node::Null => Kind::NullValue(true),
            Node::Bool(value) => Kind::BoolValue(*value),
            Node::Integer(value) => Kind::NumberValue(value.clone()),
            Node::Float(value) if value.is_finite() => Kind::NumberValue(float::text(*value)),
            Node::Float(_) => return Err(PublicRecordError::Unencodable),
            Node::Text(text) => {
                Kind::TextValue(text.utf8().map_err(|_| PublicRecordError::Unencodable)?)
            }
            Node::Array(items) => Kind::ArrayValue(MediaArray {
                values: items
                    .iter()
                    .map(|child| self.public_value(*child, depth + 1))
                    .collect::<Result<_, _>>()?,
            }),
            Node::Object(fields) => {
                let mut members = fields
                    .iter()
                    .map(|(key, child)| {
                        Ok(MediaMember {
                            key: key.utf8().map_err(|_| PublicRecordError::Unencodable)?,
                            value: Some(self.public_value(*child, depth + 1)?),
                            source_ordinal: 0,
                        })
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                members.sort_by(|left, right| left.key.cmp(&right.key));
                Kind::ObjectValue(MediaObject { members })
            }
        };
        Ok(MediaValue { kind: Some(kind) })
    }
}

fn unpaired_patch_text(bytes: &[u8]) -> Result<Text, AppError> {
    let pairs = bytes.chunks_exact(2);
    if !pairs.remainder().is_empty() {
        return Err(invalid_patch());
    }
    let units: Vec<_> = pairs
        .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
        .collect();
    if !char::decode_utf16(units.iter().copied()).any(|value| value.is_err()) {
        return Err(invalid_patch());
    }
    Ok(Text(units))
}

impl MediaRecord {
    pub fn apply_patch(&self, patch: &MediaPatch, updated_at: &str) -> Result<Self, AppError> {
        let mut merged = self.0.clone();
        let mut replacements = Vec::new();
        for (field, update) in merged.patch_values(patch)? {
            if !field.equals("metadata") {
                replacements.push((field, update));
                continue;
            }
            if let Node::Object(replacements_map) = &merged.nodes[update] {
                let metadata = merged
                    .field(merged.root, "metadata")
                    .ok_or_else(invalid_patch)?;
                let Node::Object(previous) = &merged.nodes[metadata] else {
                    return Err(invalid_patch());
                };
                let mut fields = previous.clone();
                merge_fields(&mut fields, replacements_map.clone());
                replacements.push((field, merged.add(Node::Object(fields))));
            }
        }
        replacements.push((
            Text::from("updatedAt"),
            merged.add(Node::Text(Text::from(updated_at))),
        ));
        let Node::Object(fields) = &mut merged.nodes[merged.root] else {
            return Err(invalid_patch());
        };
        merge_fields(fields, replacements);
        merged.normalize_media()
    }

    pub fn object_path(&self) -> Result<String, AppError> {
        self.text_field("path")
            .ok_or_else(invalid_patch)?
            .utf8()
            .map_err(|_| AppError::invalid_payload("Invalid media path"))
    }

    pub fn public_patch_object(&self) -> Result<MediaObject, PublicRecordError> {
        match self.0.public_value(self.0.root, 0)?.kind {
            Some(Kind::ObjectValue(object)) => Ok(object),
            _ => Err(PublicRecordError::Unencodable),
        }
    }
}
