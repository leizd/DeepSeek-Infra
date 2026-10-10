//! Python JSON coercion helpers shared by transfer phase machines.
//! These functions do not contact a provider or move payload bytes.

#![allow(dead_code)]

use serde_json::{Map, Value};

pub(crate) fn set_phase(
    mut job: Map<String, Value>,
    phase: Value,
    extra: Map<String, Value>,
    now: &str,
) -> Map<String, Value> {
    job.insert("phase".to_string(), phase);
    job.insert("updatedAt".to_string(), Value::String(now.to_string()));
    for (key, value) in extra {
        job.insert(key, value);
    }
    job
}

pub(crate) fn extra_map(case: &Value) -> Map<String, Value> {
    case.get("extra")
        .and_then(Value::as_object)
        .cloned()
        .unwrap_or_default()
}

pub(crate) fn extra_from(
    pairs: impl IntoIterator<Item = (&'static str, Value)>,
) -> Map<String, Value> {
    pairs
        .into_iter()
        .map(|(key, value)| (key.to_string(), value))
        .collect()
}

pub(crate) fn object_field(case: &Value, key: &str) -> Map<String, Value> {
    object_opt(case.get(key)).unwrap_or_default()
}

pub(crate) fn object_opt(value: Option<&Value>) -> Option<Map<String, Value>> {
    match value {
        Some(Value::Object(map)) => Some(map.clone()),
        _ => None,
    }
}

pub(crate) fn array_field(case: &Value, key: &str) -> Vec<Value> {
    case.get(key)
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default()
}

pub(crate) fn phase_text(job: &Map<String, Value>) -> String {
    python_str_or_empty(job.get("phase"))
}

pub(crate) fn python_str_or_empty(value: Option<&Value>) -> String {
    match value {
        None => String::new(),
        Some(value) if !python_truthy(value) => String::new(),
        Some(Value::String(value)) => value.clone(),
        Some(value) => python_text(Some(value)),
    }
}

pub(crate) fn python_text(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::Bool(true)) => "True".to_string(),
        Some(Value::Bool(false)) => "False".to_string(),
        Some(Value::Number(number)) => number.to_string(),
        Some(Value::String(value)) => value.clone(),
        Some(Value::Array(items)) => python_repr_array(items),
        Some(Value::Object(fields)) => python_repr_object(fields),
    }
}

fn python_repr_array(items: &[Value]) -> String {
    let mut result = String::from("[");
    for (index, item) in items.iter().enumerate() {
        if index > 0 {
            result.push_str(", ");
        }
        result.push_str(&python_text(Some(item)));
    }
    result.push(']');
    result
}

fn python_repr_object(fields: &Map<String, Value>) -> String {
    let mut result = String::from("{");
    for (index, (key, value)) in fields.iter().enumerate() {
        if index > 0 {
            result.push_str(", ");
        }
        result.push_str(&format!("'{key}': "));
        result.push_str(&python_text(Some(value)));
    }
    result.push('}');
    result
}

pub(crate) fn python_float(value: &Value) -> Result<f64, &'static str> {
    match value {
        Value::Bool(true) => Ok(1.0),
        Value::Bool(false) => Ok(0.0),
        Value::Number(number) => number.as_f64().ok_or("OverflowError"),
        Value::String(text) => {
            let text = text.trim();
            if text.is_empty() {
                return Err("ValueError");
            }
            text.parse::<f64>().map_err(|_| "ValueError")
        }
        Value::Null | Value::Array(_) | Value::Object(_) => Err("TypeError"),
    }
}

pub(crate) fn python_float_or(value: Option<&Value>, fallback: f64) -> Result<f64, &'static str> {
    let Some(value) = value else {
        return Ok(fallback);
    };
    if !python_truthy(value) {
        return Ok(fallback);
    }
    python_float(value)
}

pub(crate) fn python_int_or(value: Option<&Value>, fallback: i64) -> Result<i64, &'static str> {
    let Some(value) = value else {
        return Ok(fallback);
    };
    if !python_truthy(value) {
        return Ok(fallback);
    }
    python_int(value)
}

pub(crate) fn python_int(value: &Value) -> Result<i64, &'static str> {
    match value {
        Value::Bool(true) => Ok(1),
        Value::Bool(false) => Ok(0),
        Value::Number(number) => {
            if let Some(value) = number.as_i64() {
                return Ok(value);
            }
            if let Some(value) = number.as_u64() {
                return i64::try_from(value).map_err(|_| "OverflowError");
            }
            if let Some(value) = number.as_f64() {
                if !value.is_finite() {
                    return Err("OverflowError");
                }
                return Ok(value as i64);
            }
            Err("ValueError")
        }
        Value::String(text) => python_int_str(text),
        Value::Null | Value::Array(_) | Value::Object(_) => Err("TypeError"),
    }
}

fn python_int_str(text: &str) -> Result<i64, &'static str> {
    let text = text.trim();
    let (negative, unsigned) = if let Some(rest) = text.strip_prefix('-') {
        (true, rest)
    } else if let Some(rest) = text.strip_prefix('+') {
        (false, rest)
    } else {
        (false, text)
    };
    if unsigned.is_empty() {
        return Err("ValueError");
    }
    let mut digits = String::new();
    let mut characters = unsigned.chars().peekable();
    let mut previous_was_digit = false;
    while let Some(character) = characters.next() {
        if let Some(digit) = decimal_digit(character) {
            digits.push(char::from(b'0' + digit as u8));
            previous_was_digit = true;
        } else if character == '_'
            && previous_was_digit
            && characters.peek().copied().and_then(decimal_digit).is_some()
        {
            previous_was_digit = false;
        } else {
            return Err("ValueError");
        }
    }
    if !previous_was_digit {
        return Err("ValueError");
    }
    let magnitude = digits.parse::<i64>().map_err(|_| "ValueError")?;
    if negative {
        magnitude.checked_neg().ok_or("ValueError")
    } else {
        Ok(magnitude)
    }
}

fn decimal_digit(character: char) -> Option<u32> {
    const ZERO_POINTS: &[u32] = &[
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
        0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0,
        0x11650, 0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x11f50, 0x16a60,
        0x16ac0, 0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0,
        0x1e950, 0x1fbf0,
    ];
    let codepoint = u32::from(character);
    let index = ZERO_POINTS
        .partition_point(|zero| *zero <= codepoint)
        .checked_sub(1)?;
    let digit = codepoint - ZERO_POINTS[index];
    (digit < 10).then_some(digit)
}

pub(crate) fn python_truthy(value: &Value) -> bool {
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

pub(crate) fn parse_iso(value: Option<&Value>) -> Option<i64> {
    let Value::String(text) = value? else {
        return None;
    };
    if !python_truthy(&Value::String(text.clone())) {
        return None;
    }
    parse_iso_text(text)
}

fn parse_iso_text(text: &str) -> Option<i64> {
    let text = text.replace('Z', "+00:00");
    if text.len() != 25 {
        return None;
    }
    let bytes = text.as_bytes();
    if bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
        || bytes[19] != b'+'
        || bytes[22] != b':'
        || &text[20..] != "00:00"
    {
        return None;
    }
    let year: i32 = text[0..4].parse().ok()?;
    let month: u32 = text[5..7].parse().ok()?;
    let day: u32 = text[8..10].parse().ok()?;
    let hour: u32 = text[11..13].parse().ok()?;
    let minute: u32 = text[14..16].parse().ok()?;
    let second: u32 = text[17..19].parse().ok()?;
    if !(1..=12).contains(&month) || hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    if day == 0 || day > days_in_month(year, month) {
        return None;
    }
    Some(
        days_from_civil(year, month, day) * 86400
            + i64::from(hour) * 3600
            + i64::from(minute) * 60
            + i64::from(second),
    )
}

pub(crate) fn format_iso(timestamp: i64) -> String {
    let day = timestamp.div_euclid(86400);
    let mut seconds = timestamp.rem_euclid(86400);
    let (year, month, day) = civil_from_days(day);
    let hour = seconds / 3600;
    seconds %= 3600;
    let minute = seconds / 60;
    let second = seconds % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z")
}

fn days_in_month(year: i32, month: u32) -> u32 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 if is_leap(year) => 29,
        2 => 28,
        _ => 0,
    }
}

fn is_leap(year: i32) -> bool {
    year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)
}

fn days_from_civil(year: i32, month: u32, day: u32) -> i64 {
    let mut year = i64::from(year);
    let month = month as i64;
    if month <= 2 {
        year -= 1;
    }
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let yoe = year - era * 400;
    let mp = if month > 2 { month - 3 } else { month + 9 };
    let doy = (153 * mp + 2) / 5 + i64::from(day) - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

fn civil_from_days(days: i64) -> (i32, u32, u32) {
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    (y as i32, m as u32, d as u32)
}
