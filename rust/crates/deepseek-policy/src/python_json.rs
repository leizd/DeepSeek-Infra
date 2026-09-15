//! Python's JSON and float rendering, shared by the modules that must reproduce
//! them byte-for-byte.
//!
//! These exist because `serde_json` and Python's `json` disagree in ways that show
//! up in model-facing output:
//!
//! - Python's `json.dumps` defaults to `", "` / `": "` separators; `serde_json`'s
//!   compact form is `","` / `":"`. The oracle uses **both**, in different places.
//! - Python renders `float` as `1.0`, Rust's `Display` as `1`, and Python switches
//!   to a signed, zero-padded exponent outside `1e-4..1e16` where Rust never does.
//!
//! Key sorting is not handled here: this workspace compiles `serde_json` without
//! `preserve_order`, so maps already iterate in sorted key order, matching
//! `sort_keys=True`.

use serde_json::Value;

/// `json.dumps(value, ensure_ascii=False)` with Python's **default** separators.
///
/// Note `ensure_ascii=False` is Rust's default: non-ASCII text is emitted as raw
/// UTF-8 rather than `\uXXXX`.
pub fn dumps_default_separators(value: &Value) -> String {
    match value {
        Value::Array(items) => {
            let rendered: Vec<String> = items.iter().map(dumps_default_separators).collect();
            format!("[{}]", rendered.join(", "))
        }
        Value::Object(fields) => {
            let rendered: Vec<String> = fields
                .iter()
                .map(|(key, value)| {
                    format!(
                        "{}: {}",
                        Value::String(key.clone()),
                        dumps_default_separators(value)
                    )
                })
                .collect();
            format!("{{{}}}", rendered.join(", "))
        }
        Value::String(text) => Value::String(text.clone()).to_string(),
        other => other.to_string(),
    }
}

/// `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`.
pub fn dumps_compact(value: &Value) -> String {
    match value {
        Value::Array(items) => {
            let rendered: Vec<String> = items.iter().map(dumps_compact).collect();
            format!("[{}]", rendered.join(","))
        }
        Value::Object(fields) => {
            let rendered: Vec<String> = fields
                .iter()
                .map(|(key, value)| {
                    format!("{}:{}", Value::String(key.clone()), dumps_compact(value))
                })
                .collect();
            format!("{{{}}}", rendered.join(","))
        }
        Value::String(text) => Value::String(text.clone()).to_string(),
        other => other.to_string(),
    }
}

/// Python's `str(float)` — the shortest round-tripping form.
///
/// Integral values keep a decimal point (`1.0`, not `1`), and values outside
/// `1e-4..1e16` use a signed exponent padded to at least two digits (`1e+16`,
/// `1e-05`). `serde_json`'s `Number` renders `1.0` as `1.0` already, so prefer
/// `json_number_str` when the value came from JSON.
pub fn float_str(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_string();
    }
    if value.is_infinite() {
        return if value.is_sign_positive() {
            "inf"
        } else {
            "-inf"
        }
        .to_string();
    }
    let magnitude = value.abs();
    if value != 0.0 && !(1e-4..1e16).contains(&magnitude) {
        let raw = format!("{value:e}");
        let (mantissa, exponent) = raw.split_once('e').unwrap_or((raw.as_str(), "0"));
        let (sign, digits) = match exponent.strip_prefix('-') {
            Some(rest) => ('-', rest),
            None => ('+', exponent),
        };
        return format!("{mantissa}e{sign}{digits:0>2}");
    }
    let text = format!("{value}");
    if text.contains('.') {
        text
    } else {
        format!("{text}.0")
    }
}

/// `str(number)` for a `serde_json` number.
///
/// `serde_json` preserves the distinction between `1.0` and `1`, which is exactly
/// what Python's `json` round-trip does too, so an integral *float* keeps its
/// `.0` and an integer does not.
pub fn json_number_str(number: &serde_json::Number) -> String {
    match number.as_f64() {
        Some(value) if number.is_f64() => float_str(value),
        _ => number.to_string(),
    }
}

/// `str(value)` for the JSON scalars that reach a message.
pub fn value_str(value: &Value) -> String {
    match value {
        Value::Null => "None".to_string(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(number) => json_number_str(number),
        Value::String(text) => text.clone(),
        other => dumps_default_separators(other),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn default_separators_are_spaced() {
        assert_eq!(dumps_default_separators(&json!({"a": 1})), "{\"a\": 1}");
        assert_eq!(
            dumps_default_separators(&json!({"a": 1, "b": [1, 2]})),
            "{\"a\": 1, \"b\": [1, 2]}"
        );
        // Empty containers get no inner padding.
        assert_eq!(
            dumps_default_separators(&json!({"e": {}, "l": []})),
            "{\"e\": {}, \"l\": []}"
        );
        // Non-ASCII stays raw.
        assert_eq!(
            dumps_default_separators(&json!({"名": "值"})),
            "{\"名\": \"值\"}"
        );
    }

    #[test]
    fn compact_separators_are_tight() {
        assert_eq!(
            dumps_compact(&json!({"a": 1, "b": [1, 2]})),
            "{\"a\":1,\"b\":[1,2]}"
        );
        assert_eq!(dumps_compact(&json!({"名": "值"})), "{\"名\":\"值\"}");
    }

    #[test]
    fn float_str_matches_python() {
        assert_eq!(float_str(1.0), "1.0");
        assert_eq!(float_str(0.0), "0.0");
        assert_eq!(float_str(-0.0), "-0.0");
        assert_eq!(float_str(2.5), "2.5");
        assert_eq!(float_str(1e16), "1e+16");
        assert_eq!(float_str(1e-5), "1e-05");
        assert_eq!(float_str(f64::NAN), "nan");
        assert_eq!(float_str(f64::INFINITY), "inf");
    }

    #[test]
    fn json_numbers_keep_their_python_spelling() {
        // An integral float keeps `.0`; an integer does not.
        assert_eq!(value_str(&json!(1.0)), "1.0");
        assert_eq!(value_str(&json!(1)), "1");
        assert_eq!(value_str(&json!(true)), "True");
        assert_eq!(value_str(&json!(null)), "None");
        assert_eq!(value_str(&json!("x")), "x");
    }
}
