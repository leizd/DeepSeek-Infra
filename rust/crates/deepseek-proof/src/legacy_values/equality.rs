//! Python JSON-container equality treats booleans as integers but never rounds an
//! integer to binary64 when comparing it with a float. Object insertion order is ignored.

use serde_json::Value;

enum Numeric {
    Integer(String),
    Float(f64),
}

fn numeric(value: &Value) -> Option<Numeric> {
    match value {
        Value::Bool(value) => Some(Numeric::Integer(u8::from(*value).to_string())),
        Value::Number(value) => {
            let raw = value.to_string();
            if raw.contains(['.', 'e', 'E']) {
                raw.parse().ok().map(Numeric::Float)
            } else {
                Some(Numeric::Integer(if raw == "-0" {
                    "0".to_string()
                } else {
                    raw
                }))
            }
        }
        _ => None,
    }
}

pub(crate) fn python_equal(left: &Value, right: &Value) -> bool {
    match (numeric(left), numeric(right)) {
        (Some(Numeric::Integer(left)), Some(Numeric::Integer(right))) => return left == right,
        (Some(Numeric::Float(left)), Some(Numeric::Float(right))) => return left == right,
        (Some(Numeric::Integer(integer)), Some(Numeric::Float(float)))
        | (Some(Numeric::Float(float)), Some(Numeric::Integer(integer))) => {
            // Fixed zero precision prints the exact integral binary64 value, at most
            // 309 decimal digits. Converting the integer to f64 would lose identity.
            return float.is_finite()
                && float.fract() == 0.0
                && integer
                    == if float == 0.0 {
                        "0".to_string()
                    } else {
                        format!("{float:.0}")
                    };
        }
        _ => {}
    }
    match (left, right) {
        (Value::Null, Value::Null) => true,
        (Value::String(left), Value::String(right)) => left == right,
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right)
                    .all(|(left, right)| python_equal(left, right))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left.iter().all(|(key, left)| {
                    right
                        .get(key)
                        .is_some_and(|right| python_equal(left, right))
                })
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn equality_does_not_round_an_integer_to_match_an_inexact_float() {
        for (left, right, equal) in [
            ("9007199254740993", "9007199254740992.0", false),
            ("100000000000000000000000", "1e23", false),
            ("99999999999999991611392", "1e23", true),
            ("-0.0", "false", true),
            ("1.0000000000000002", "1", false),
            ("1e999", "1e999", true),
        ] {
            assert_eq!(
                python_equal(
                    &serde_json::from_str(left).unwrap(),
                    &serde_json::from_str(right).unwrap()
                ),
                equal,
                "{left}/{right}"
            );
        }
    }
}
