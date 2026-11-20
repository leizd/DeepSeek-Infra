//! Original media schema on values that have not crossed the response boundary.
use super::retained::{Document, MediaRecord, Node, Text, whitespace};
use crate::{app_error::AppError, media_schema, workspace_schema};
use std::collections::HashMap;

pub(super) fn normalize(input: &Document) -> Result<MediaRecord, AppError> {
    normalize_with_clock(input, media_schema::utc_now_z)
}

fn field_text(input: &Document, name: &str) -> Text {
    input
        .field(input.root, name)
        .filter(|id| input.truthy(*id))
        .map_or_else(Text::default, |id| input.text(id, usize::MAX))
}

// All recognizers below compare ASCII syntax. Non-ASCII, including unpaired
// units, cannot match a registered suffix/type/status; keep the actual text in
// the record independently rather than changing it into this recognition view.
fn recognition_text(text: &Text) -> String {
    text.scalars()
        .map(|value| char::from_u32(value).unwrap_or('\u{fffd}'))
        .collect()
}

fn mime_text(text: Text) -> Text {
    let first = text
        .0
        .iter()
        .position(|unit| *unit == 59)
        .unwrap_or(text.0.len());
    Text(text.0[..first].to_vec()).trimmed().lower()
}

fn title_text(text: Text) -> Text {
    let mut result = Text::default();
    let mut pending_space = false;
    let mut count = 0usize;
    for scalar in text.scalars() {
        if whitespace(scalar) {
            pending_space = !result.0.is_empty();
            continue;
        }
        if pending_space {
            if count == workspace_schema::MAX_TITLE_CHARS {
                break;
            }
            result.push(32);
            count += 1;
            pending_space = false;
        }
        if count == workspace_schema::MAX_TITLE_CHARS {
            break;
        }
        result.push(scalar);
        count += 1;
    }
    if result.0.is_empty() {
        Text::from("Untitled media")
    } else {
        result
    }
}

fn path_text(text: Text) -> Result<Text, AppError> {
    let mut text = text.trimmed();
    for unit in &mut text.0 {
        if *unit == 92 {
            *unit = 47;
        }
    }
    let units = &text.0;
    let drive = units.len() >= 3
        && units[0] <= 127
        && (units[0] as u8).is_ascii_alphabetic()
        && units[1..3] == [58, 47];
    if units.first() == Some(&47) || drive {
        return Err(AppError::invalid_payload(
            "Media path must be relative to the media library",
        ));
    }
    let mut result = Vec::new();
    for part in units.split(|unit| *unit == 47) {
        if part.is_empty() || part == [46] {
            continue;
        }
        if part == [46, 46] {
            return Err(AppError::invalid_payload(
                "Media path must not escape the media library",
            ));
        }
        if !result.is_empty() {
            result.push(47);
        }
        result.extend_from_slice(part);
    }
    Ok(Text(result))
}

pub(super) fn normalize_with_clock(
    input: &Document,
    mut clock: impl FnMut() -> String,
) -> Result<MediaRecord, AppError> {
    let raw_id = field_text(input, "mediaId")
        .trimmed()
        .utf8()
        .map_err(|_| AppError::invalid_payload("Invalid media id"))?;
    let media_id = workspace_schema::validate_workspace_id(&raw_id, "media id")?;
    let project = field_text(input, "projectId").trimmed();
    let project_id = if project.0.is_empty() {
        String::new()
    } else {
        let project = project
            .utf8()
            .map_err(|_| AppError::invalid_payload("Invalid project id"))?;
        workspace_schema::validate_project_id(&project)?
    };
    let mime = mime_text(field_text(input, "mimeType"));
    let title = field_text(input, "title");
    let declared = field_text(input, "type").trimmed().lower();
    let declared_view = recognition_text(&declared);
    let media_type = if media_schema::MEDIA_TYPES.contains(&declared_view.as_str()) {
        declared_view
    } else {
        let guessed =
            media_schema::media_type_from_mime(&recognition_text(&mime), &recognition_text(&title));
        if guessed.is_empty() {
            return Err(AppError::invalid_payload("Unsupported media type"));
        }
        guessed
    };
    // The original consumes this clock before evaluating the dictionary's path
    // validation; invalid rows still advance the observable normalization clock.
    let created = input
        .field(input.root, "createdAt")
        .filter(|id| input.truthy(*id))
        .map_or_else(
            || Text::from(clock().as_str()),
            |id| input.text(id, usize::MAX),
        );
    let updated = input
        .field(input.root, "updatedAt")
        .filter(|id| input.truthy(*id))
        .map_or_else(|| created.clone(), |id| input.text(id, usize::MAX));
    let title = title_text(title);
    let raw_path = field_text(input, "path");
    let mime_type = if mime.0.is_empty() {
        Text::from(media_schema::guess_mime_type(&recognition_text(&raw_path)).as_str())
    } else {
        mime
    };
    let path = path_text(raw_path)?;
    let mut output = Document::empty_builder();
    // Dictionary evaluation order is source before status, metadata last.
    let source = normalize_mapping(
        input,
        input.field(input.root, "source"),
        &mut output,
        Kind::Source,
    );
    let raw_status = input
        .field(input.root, "status")
        .filter(|id| input.truthy(*id))
        .map_or_else(|| Text::from("pending"), |id| input.text(id, usize::MAX));
    let status = recognition_text(&raw_status.trimmed().lower());
    if !media_schema::MEDIA_STATUSES.contains(&status.as_str()) {
        return Err(AppError::invalid_payload("Unsupported media status"));
    }
    let metadata = normalize_mapping(
        input,
        input.field(input.root, "metadata"),
        &mut output,
        Kind::Metadata,
    );
    let mut fields = Vec::new();
    for (key, value) in [
        ("mediaId", Text::from(media_id.as_str())),
        ("projectId", Text::from(project_id.as_str())),
        ("type", Text::from(media_type.as_str())),
        ("title", title),
        ("mimeType", mime_type),
        ("path", path),
        ("status", Text::from(status.as_str())),
        ("createdAt", created),
        ("updatedAt", updated),
    ] {
        let id = output.add(Node::Text(value));
        fields.push((Text::from(key), id));
    }
    // Preserve the original public dictionary insertion positions too.
    fields.insert(6, (Text::from("source"), source));
    fields.push((Text::from("metadata"), metadata));
    output.root = output.add(Node::Object(fields));
    Ok(MediaRecord(output))
}

#[derive(Clone, Copy)]
enum Kind {
    Metadata,
    Source,
}

impl Kind {
    fn text_limit(self) -> usize {
        match self {
            Self::Metadata => media_schema::MAX_SEGMENT_TEXT_CHARS,
            Self::Source => workspace_schema::MAX_SOURCE_REF_VALUE_CHARS,
        }
    }
    fn list_limit(self) -> usize {
        match self {
            Self::Metadata => 200,
            Self::Source => 20,
        }
    }
}

fn safe_key(text: &Text) -> Text {
    Text(
        text.0
            .iter()
            .copied()
            .filter(|unit| {
                *unit <= 127
                    && ((*unit as u8).is_ascii_alphanumeric() || matches!(*unit, 95 | 46 | 58 | 45))
            })
            .take(80)
            .collect(),
    )
}

fn compact(input: &Document, id: usize, output: &mut Document, limit: usize) -> usize {
    let node = match &input.nodes[id] {
        Node::Null => Node::Null,
        Node::Bool(value) => Node::Bool(*value),
        Node::Integer(value) => Node::Integer(value.clone()),
        Node::Float(value) => Node::Float(*value),
        _ => Node::Text(input.text(id, limit)),
    };
    output.add(node)
}

fn normalize_mapping(
    input: &Document,
    root: Option<usize>,
    output: &mut Document,
    kind: Kind,
) -> usize {
    let Some(root) = root.filter(|id| matches!(&input.nodes[*id], Node::Object(_))) else {
        return output.add(Node::Object(Vec::new()));
    };
    let mut pending = vec![(root, false)];
    let mut finished = HashMap::new();
    while let Some((id, finish)) = pending.pop() {
        let Node::Object(fields) = &input.nodes[id] else {
            unreachable!("mapping work item")
        };
        if !finish {
            pending.push((id, true));
            for (key, value) in fields.iter().rev() {
                if safe_key(key).0.is_empty() {
                    continue;
                }
                match &input.nodes[*value] {
                    Node::Object(_) => pending.push((*value, false)),
                    Node::Array(values) if matches!(kind, Kind::Metadata) => {
                        for child in values.iter().take(kind.list_limit()).rev() {
                            if matches!(&input.nodes[*child], Node::Object(_)) {
                                pending.push((*child, false));
                            }
                        }
                    }
                    _ => {}
                }
            }
            continue;
        }
        let mut normalized = Vec::<(Text, usize)>::new();
        let mut positions = HashMap::<Text, usize>::new();
        for (key, value) in fields {
            let key = safe_key(key);
            if key.0.is_empty() {
                continue;
            }
            let value = match &input.nodes[*value] {
                Node::Object(_) => {
                    let child = finished[value];
                    if matches!(&output.nodes[child], Node::Object(fields) if fields.is_empty()) {
                        continue;
                    }
                    child
                }
                Node::Array(values) => {
                    let mut cleaned = Vec::new();
                    for child in values.iter().take(kind.list_limit()) {
                        let child = match kind {
                            Kind::Source => {
                                if matches!(&input.nodes[*child], Node::Null) {
                                    continue;
                                }
                                output.add(Node::Text(input.text(*child, kind.text_limit())))
                            }
                            Kind::Metadata if matches!(&input.nodes[*child], Node::Object(_)) => {
                                finished[child]
                            }
                            Kind::Metadata => compact(input, *child, output, kind.text_limit()),
                        };
                        cleaned.push(child);
                    }
                    if cleaned.is_empty() && matches!(kind, Kind::Source) {
                        continue;
                    }
                    output.add(Node::Array(cleaned))
                }
                _ => compact(input, *value, output, kind.text_limit()),
            };
            if let Some(position) = positions.get(&key) {
                normalized[*position].1 = value;
            } else {
                positions.insert(key.clone(), normalized.len());
                normalized.push((key, value));
            }
        }
        let result = output.add(Node::Object(normalized));
        finished.insert(id, result);
    }
    finished[&root]
}
