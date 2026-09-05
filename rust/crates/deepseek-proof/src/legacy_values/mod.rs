//! The legacy validators call Python `str`, `repr`, and `int` on JSON values.
//! Keep those conversions here so malformed evidence follows the same decision path.

use serde_json::{Map, Number, Value};
use std::cmp::Ordering;
use std::fmt::Write;

mod equality;
mod timestamp;
mod unicode;
pub(crate) use equality::python_equal;
pub(crate) use timestamp::{ParsedTimestamp, parse_timestamp};
pub(crate) use unicode::casefold;
use unicode::decimal_digit;

pub(crate) fn require_fields(evidence: &Map<String, Value>, fields: &[&str]) -> Vec<String> {
    fields
        .iter()
        .filter(|field| missing(evidence.get(**field)))
        .map(|field| format!("missing-field:{field}"))
        .collect()
}

pub(crate) fn missing(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => true,
        Some(Value::String(value)) => value.is_empty(),
        _ => false,
    }
}

pub(crate) fn is_plain_sha256(value: Option<&Value>) -> bool {
    let value = value_or_empty_text(value);
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub(super) fn value_or_empty_text(value: Option<&Value>) -> String {
    value
        .filter(|value| python_truthy(value))
        .map_or_else(String::new, |value| python_text(Some(value)))
}

pub(super) fn python_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(value) => *value,
        Value::Number(value) => {
            let raw = value.to_string();
            if raw.contains(['.', 'e', 'E']) {
                raw.parse::<f64>().is_ok_and(|number| number != 0.0)
            } else {
                raw.bytes().any(|byte| (b'1'..=b'9').contains(&byte))
            }
        }
        Value::String(value) => !value.is_empty(),
        Value::Array(value) => !value.is_empty(),
        Value::Object(value) => !value.is_empty(),
    }
}

pub(super) fn python_text(value: Option<&Value>) -> String {
    if let Some(Value::String(value)) = value {
        return value.clone();
    }
    let mut result = String::new();
    append_repr(value.unwrap_or(&Value::Null), &mut result);
    result
}

fn append_repr(value: &Value, result: &mut String) {
    match value {
        Value::Null => result.push_str("None"),
        Value::Bool(true) => result.push_str("True"),
        Value::Bool(false) => result.push_str("False"),
        Value::Number(value) => result.push_str(&number_text(value)),
        Value::String(value) => append_string_repr(value, result),
        Value::Array(items) => {
            result.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    result.push_str(", ");
                }
                append_repr(item, result);
            }
            result.push(']');
        }
        Value::Object(fields) => {
            result.push('{');
            for (index, (key, value)) in fields.iter().enumerate() {
                if index > 0 {
                    result.push_str(", ");
                }
                append_string_repr(key, result);
                result.push_str(": ");
                append_repr(value, result);
            }
            result.push('}');
        }
    }
}

fn append_string_repr(value: &str, result: &mut String) {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    result.push(quote);
    for character in value.chars() {
        match character {
            '\\' => result.push_str("\\\\"),
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            character if character == quote => {
                result.push('\\');
                result.push(character);
            }
            '\'' | '"' => result.push(character),
            character if character.escape_debug().count() == 1 => result.push(character),
            character => {
                let codepoint = u32::from(character);
                if codepoint <= 0xff {
                    let _ = write!(result, "\\x{codepoint:02x}");
                } else if codepoint <= 0xffff {
                    let _ = write!(result, "\\u{codepoint:04x}");
                } else {
                    let _ = write!(result, "\\U{codepoint:08x}");
                }
            }
        }
    }
    result.push(quote);
}

fn number_text(number: &Number) -> String {
    let raw = number.to_string();
    if !raw.contains(['.', 'e', 'E']) {
        return if raw == "-0" { "0".to_string() } else { raw };
    }
    let Ok(value) = raw.parse::<f64>() else {
        return raw;
    };
    let representation = format!("{value:?}");
    if let Some((mantissa, exponent)) = representation.split_once('e') {
        if let Ok(exponent) = exponent.parse::<i32>() {
            return format!("{mantissa}e{exponent:+03}");
        }
    }
    representation
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(super) struct PythonInteger {
    negative: bool,
    magnitude: String,
}

pub(crate) fn python_integer(value: Option<&Value>) -> Option<PythonInteger> {
    PythonInteger::parse(&python_text(value))
}

impl PythonInteger {
    pub(super) fn from_usize(value: usize) -> Self {
        Self {
            negative: false,
            magnitude: value.to_string(),
        }
    }

    pub(super) fn is_negative(&self) -> bool {
        self.negative
    }

    pub(super) fn parse(value: &str) -> Option<Self> {
        // The supported Python reference interpreters use the default 4300-digit limit.
        // Normalize decimal digits directly; no bigint conversion or arithmetic is needed.
        const MAX_DIGITS: usize = 4300;
        let value = value.trim();
        let (negative, unsigned) = if let Some(unsigned) = value.strip_prefix('-') {
            (true, unsigned)
        } else if let Some(unsigned) = value.strip_prefix('+') {
            (false, unsigned)
        } else {
            (false, value)
        };
        if unsigned.is_empty() {
            return None;
        }
        let mut digits = String::with_capacity(unsigned.len().min(MAX_DIGITS));
        let mut characters = unsigned.chars().peekable();
        let mut previous_was_digit = false;
        while let Some(character) = characters.next() {
            if let Some(digit) = decimal_digit(character) {
                if digits.len() == MAX_DIGITS {
                    return None;
                }
                digits.push(digit);
                previous_was_digit = true;
            } else if character == '_'
                && previous_was_digit
                && characters
                    .peek()
                    .is_some_and(|next| decimal_digit(*next).is_some())
            {
                previous_was_digit = false;
            } else {
                return None;
            }
        }
        if !previous_was_digit {
            return None;
        }
        let normalized = digits.trim_start_matches('0');
        let magnitude = if normalized.is_empty() {
            "0"
        } else {
            normalized
        }
        .to_string();
        Some(Self {
            negative: negative && magnitude != "0",
            magnitude,
        })
    }

    pub(super) fn is_zero(&self) -> bool {
        self.magnitude == "0"
    }

    pub(super) fn is_one(&self) -> bool {
        !self.negative && self.magnitude == "1"
    }

    pub(super) fn canonical_text(&self) -> String {
        if self.negative {
            format!("-{}", self.magnitude)
        } else {
            self.magnitude.clone()
        }
    }

    pub(super) fn is_positive(&self) -> bool {
        !self.negative && !self.is_zero()
    }

    pub(super) fn compare(&self, other: &Self) -> Ordering {
        match (self.negative, other.negative) {
            (true, false) => Ordering::Less,
            (false, true) => Ordering::Greater,
            (negative, _) => {
                let magnitude_order = self
                    .magnitude
                    .len()
                    .cmp(&other.magnitude.len())
                    .then_with(|| self.magnitude.cmp(&other.magnitude));
                if negative {
                    magnitude_order.reverse()
                } else {
                    magnitude_order
                }
            }
        }
    }
}
