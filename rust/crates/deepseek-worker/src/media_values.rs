use deepseek_protocol::generated::deepseek::media::v1::{
    MediaArray, MediaMember, MediaObject, MediaValue, media_value::Kind,
};
use serde_json::{Map, Value};
use tonic::Status;

pub(super) fn invalid() -> Status {
    Status::failed_precondition("MEDIA_MUTATION_INVALID")
}
pub(super) fn from_json(value: &Value) -> Result<MediaValue, Status> {
    from_json_at(value, 0)
}

// Public records have already been normalized. Keep their existing v1 wire
// encoding so older controllers can verify outcomes from a newer worker.
pub(super) fn from_json_legacy(value: &Value) -> Result<MediaValue, Status> {
    let mut result = from_json(value)?;
    clear_source_ordinals(&mut result);
    Ok(result)
}

fn clear_source_ordinals(value: &mut MediaValue) {
    match value.kind.as_mut() {
        Some(Kind::ObjectValue(object)) => {
            for member in &mut object.members {
                member.source_ordinal = 0;
                if let Some(child) = member.value.as_mut() {
                    clear_source_ordinals(child);
                }
            }
        }
        Some(Kind::ArrayValue(array)) => {
            for child in &mut array.values {
                clear_source_ordinals(child);
            }
        }
        _ => {}
    }
}
fn from_json_at(value: &Value, depth: usize) -> Result<MediaValue, Status> {
    if depth > 128 {
        return Err(invalid());
    }
    let kind = match value {
        Value::Null => Kind::NullValue(true),
        Value::Bool(b) => Kind::BoolValue(*b),
        Value::String(s) => Kind::TextValue(s.clone()),
        Value::Number(n) => Kind::NumberValue(n.to_string()),
        Value::Array(a) => Kind::ArrayValue(MediaArray {
            values: a
                .iter()
                .map(|v| from_json_at(v, depth + 1))
                .collect::<Result<_, _>>()?,
        }),
        Value::Object(o) => {
            let mut members = o
                .iter()
                .enumerate()
                .map(|(position, (k, v))| {
                    Ok(MediaMember {
                        key: k.clone(),
                        value: Some(from_json_at(v, depth + 1)?),
                        source_ordinal: u32::try_from(position + 1).map_err(|_| invalid())?,
                    })
                })
                .collect::<Result<Vec<_>, Status>>()?;
            members.sort_by(|a, b| a.key.as_bytes().cmp(b.key.as_bytes()));
            Kind::ObjectValue(MediaObject { members })
        }
    };
    Ok(MediaValue { kind: Some(kind) })
}
pub(super) fn to_json(value: &MediaValue) -> Result<Value, Status> {
    to_json_at(value, 0)
}
fn to_json_at(value: &MediaValue, depth: usize) -> Result<Value, Status> {
    if depth > 128 {
        return Err(invalid());
    }
    match value.kind.as_ref().ok_or_else(invalid)? {
        Kind::NullValue(true) => Ok(Value::Null),
        Kind::NullValue(false) => Err(invalid()),
        Kind::BoolValue(b) => Ok(Value::Bool(*b)),
        Kind::TextValue(s) => Ok(Value::String(s.clone())),
        Kind::NumberValue(n) => {
            let v: Value = serde_json::from_str(n).map_err(|_| invalid())?;
            if !v.is_number() {
                return Err(invalid());
            }
            Ok(v)
        }
        Kind::ArrayValue(a) => Ok(Value::Array(
            a.values
                .iter()
                .map(|v| to_json_at(v, depth + 1))
                .collect::<Result<_, _>>()?,
        )),
        Kind::ObjectValue(o) => {
            let mut result = Map::new();
            let mut previous: Option<&str> = None;
            for m in &o.members {
                if previous.is_some_and(|p| p.as_bytes() >= m.key.as_bytes()) {
                    return Err(invalid());
                }
                previous = Some(&m.key);
            }
            let mut members = o.members.iter().collect::<Vec<_>>();
            if members.iter().any(|m| m.source_ordinal != 0) {
                members.sort_by_key(|m| m.source_ordinal);
                for (position, member) in members.iter().enumerate() {
                    if member.source_ordinal as usize != position + 1 {
                        return Err(invalid());
                    }
                }
            }
            for m in members {
                result.insert(
                    m.key.clone(),
                    to_json_at(m.value.as_ref().ok_or_else(invalid)?, depth + 1)?,
                );
            }
            Ok(Value::Object(result))
        }
    }
}

#[cfg(test)]
mod input_order_tests {
    use super::{Kind, MediaValue, from_json, from_json_legacy, to_json};
    use deepseek_policy::media_schema::normalize_media_record;
    use prost::Message as _;
    use serde_json::{Value, json};

    fn compare_original_metadata(raw: &str, expected: Value) {
        let input: Value = serde_json::from_str(raw).unwrap();
        let record = |metadata| {
            json!({"mediaId":"media_order", "type":"image", "title":"Ordered metadata",
                "createdAt":"2026-10-27T00:00:00Z", "metadata":metadata})
        };
        let original = normalize_media_record(&record(input.clone())).unwrap();
        assert_eq!(
            original["metadata"], expected,
            "the unencoded original normalization must match the independent Python oracle"
        );
        let transported = to_json(&from_json(&input).unwrap()).unwrap();
        let result = normalize_media_record(&record(transported)).unwrap();
        assert_eq!(
            result["metadata"], expected,
            "typed RPC transport must preserve the original input order before key normalization"
        );
    }

    #[test]
    fn media_typed_metadata_top_level_collisions_keep_input_order() {
        compare_original_metadata(r#"{"a?":"first","a!":"last"}"#, json!({"a":"last"}));
    }

    #[test]
    fn media_typed_metadata_nested_collisions_keep_input_order() {
        compare_original_metadata(
            r#"{"outer":{"a?":"first","a!":"last"}}"#,
            json!({"outer":{"a":"last"}}),
        );
    }

    #[test]
    fn media_typed_metadata_array_collisions_keep_input_order() {
        compare_original_metadata(
            r#"{"rows":[{"a?":"first","a!":"last"}]}"#,
            json!({"rows":[{"a":"last"}]}),
        );
    }

    #[test]
    fn media_typed_metadata_truncated_collisions_keep_input_order() {
        let prefix = "k".repeat(80);
        let input = format!(r#"{{"{prefix}z":"first","{prefix}a":"last"}}"#);
        let mut expected = serde_json::Map::new();
        expected.insert(prefix, Value::String("last".into()));
        compare_original_metadata(&input, Value::Object(expected));
    }

    #[test]
    fn media_typed_values_reject_ambiguous_source_ordinals() {
        let original = from_json(&json!({"a": "A", "b": "B"})).unwrap();
        for ordinals in [[0, 1], [1, 1], [1, 3], [1, u32::MAX]] {
            let mut value = original.clone();
            let Some(Kind::ObjectValue(object)) = value.kind.as_mut() else {
                panic!("object fixture");
            };
            for (member, ordinal) in object.members.iter_mut().zip(ordinals) {
                member.source_ordinal = ordinal;
            }
            assert!(
                to_json(&value).is_err(),
                "accepted ambiguous source ordinals"
            );
        }
    }

    #[test]
    fn media_typed_values_accept_exact_legacy_zero_ordinal_wire() {
        let wire = [
            0x2a, 0x14, 0x0a, 0x08, 0x0a, 0x01, 0x61, 0x12, 0x03, 0x1a, 0x01, 0x41, 0x0a, 0x08,
            0x0a, 0x01, 0x62, 0x12, 0x03, 0x1a, 0x01, 0x42,
        ];
        let value = MediaValue::decode(wire.as_slice()).unwrap();
        assert_eq!(value.encode_to_vec(), wire);
        assert_eq!(to_json(&value).unwrap(), json!({"a": "A", "b": "B"}));
        assert_eq!(
            from_json_legacy(&json!({"a": "A", "b": "B"}))
                .unwrap()
                .encode_to_vec(),
            wire
        );
    }
}
