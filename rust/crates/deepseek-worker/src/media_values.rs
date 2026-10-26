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
                .map(|(k, v)| {
                    Ok(MediaMember {
                        key: k.clone(),
                        value: Some(from_json_at(v, depth + 1)?),
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
                result.insert(
                    m.key.clone(),
                    to_json_at(m.value.as_ref().ok_or_else(invalid)?, depth + 1)?,
                );
            }
            Ok(Value::Object(result))
        }
    }
}
