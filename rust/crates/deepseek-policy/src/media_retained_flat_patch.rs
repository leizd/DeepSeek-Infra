//! Convert the retained value arena into an ordered typed PATCH response.
use super::{MediaRecord, Node, PublicRecordError, float};
use deepseek_protocol::generated::deepseek::media::v1::{
    MediaFlatPatchArray, MediaFlatPatchMember, MediaFlatPatchNode, MediaFlatPatchObject,
    MediaFlatPatchRecord, media_flat_patch_node::Kind,
};

impl MediaRecord {
    pub fn flat_patch_record(&self) -> Result<MediaFlatPatchRecord, PublicRecordError> {
        let document = &self.0;
        let mut pending = vec![(document.root, false)];
        let mut completed = Vec::new();
        let mut nodes = Vec::new();
        while let Some((id, expanded)) = pending.pop() {
            let node = &document.nodes[id];
            if !expanded {
                // Visit each occurrence in output order. Detached nodes
                // never enter the result, and shared subtrees are copied.
                match node {
                    Node::Array(items) if !items.is_empty() => {
                        pending.push((id, true));
                        pending.extend(items.iter().rev().map(|child| (*child, false)));
                        continue;
                    }
                    Node::Object(fields) if !fields.is_empty() => {
                        pending.push((id, true));
                        pending.extend(fields.iter().rev().map(|(_, child)| (*child, false)));
                        continue;
                    }
                    _ => {}
                }
            }
            let kind = match node {
                Node::Null => Kind::NullValue(true),
                Node::Bool(value) => Kind::BoolValue(*value),
                Node::Integer(value) => Kind::NumberValue(value.clone()),
                Node::Float(value) if value.is_finite() => Kind::NumberValue(float::text(*value)),
                Node::Float(_) => return Err(PublicRecordError::Unencodable),
                Node::Text(text) => {
                    Kind::TextValue(text.utf8().map_err(|_| PublicRecordError::Unencodable)?)
                }
                Node::Array(items) => Kind::ArrayValue(MediaFlatPatchArray {
                    children: completed.split_off(completed.len() - items.len()),
                }),
                Node::Object(fields) => {
                    let children = completed.split_off(completed.len() - fields.len());
                    let members = fields
                        .iter()
                        .zip(children)
                        .map(|((key, _), child)| {
                            Ok(MediaFlatPatchMember {
                                key: key.utf8().map_err(|_| PublicRecordError::Unencodable)?,
                                child,
                            })
                        })
                        .collect::<Result<_, PublicRecordError>>()?;
                    Kind::ObjectValue(MediaFlatPatchObject { members })
                }
            };
            nodes.push(MediaFlatPatchNode { kind: Some(kind) });
            completed.push(u32::try_from(nodes.len()).map_err(|_| PublicRecordError::TooLarge)?);
        }
        Ok(MediaFlatPatchRecord { nodes })
    }
}
