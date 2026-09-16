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
//! Key sorting is handled explicitly. Workspace feature unification can enable
//! `serde_json/preserve_order`, so relying on the map implementation would make
//! contract bytes depend on which crates are built together.

use serde_json::Value;

fn sorted_fields(fields: &serde_json::Map<String, Value>) -> Vec<(&String, &Value)> {
    let mut entries: Vec<_> = fields.iter().collect();
    // `by_key` rather than `by`: the local clippy (1.97) fires
    // `unnecessary_sort_by` on the closure form, and the two are identical here.
    entries.sort_unstable_by_key(|(key, _)| *key);
    entries
}

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
            let rendered: Vec<String> = sorted_fields(fields)
                .into_iter()
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
            let rendered: Vec<String> = sorted_fields(fields)
                .into_iter()
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

/// `json.dumps(value)` — default separators **and** `ensure_ascii=True`.
///
/// Python's `json.dumps` escapes every non-ASCII character by default, so
/// `{"query": "最新消息"}` is sent as `{"query": "\u6700\u65b0\u6d88\u606f"}`. That is a
/// byte-level property of any request body built this way, not a cosmetic one.
///
/// The escaping matches CPython exactly: BMP characters become a lowercase `\uXXXX`,
/// and an astral character becomes a lowercase surrogate **pair**
/// (`\ud83d\ude00` for U+1F600). Everything ASCII is passed through untouched, and no
/// escape is ever applied twice because JSON's own escape sequences are ASCII.
pub fn dumps_default_separators_ascii(value: &Value) -> String {
    escape_non_ascii(&dumps_default_separators(value))
}

/// `str`'s rendering with `ensure_ascii=True`, for one value.
pub fn escaped_string(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    escape_non_ascii_into(text, &mut out);
    out
}

/// Replace every non-ASCII `char` with CPython's `\uXXXX` (or surrogate pair) form.
pub fn escape_non_ascii(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    escape_non_ascii_into(text, &mut out);
    out
}

fn escape_non_ascii_into(text: &str, out: &mut String) {
    for character in text.chars() {
        let code = character as u32;
        if code < 0x80 {
            out.push(character);
        } else if code <= 0xFFFF {
            out.push_str(&format!("\\u{code:04x}"));
        } else {
            let adjusted = code - 0x1_0000;
            let high = 0xD800 + (adjusted >> 10);
            let low = 0xDC00 + (adjusted & 0x3FF);
            out.push_str(&format!("\\u{high:04x}\\u{low:04x}"));
        }
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

/// A JSON value that remembers its **key order**.
///
/// Python dicts iterate in insertion order, and the on-disk store files carry that
/// order. A store written from a plain `Value` can therefore differ from the oracle
/// byte-for-byte even though the JSON is equivalent. Since this repository's whole
/// subject is a backup system, file bytes are part of the contract, so callers
/// spell the order out.
#[derive(Debug, Clone, PartialEq)]
pub enum OrderedJson {
    Scalar(Value),
    Object(Vec<(String, OrderedJson)>),
    List(Vec<OrderedJson>),
}

/// A nested value becomes a real node so containers render with indentation, as
/// Python's `indent=2` does at every level.
///
/// Nested object **keys** come out sorted because Python's insertion order is not
/// recoverable from every `serde_json::Value` configuration. Callers that need a
/// nested order must flatten the value or take the order at the top level, where
/// [`OrderedJson::from_value_with_order`] accepts one.
fn nested(value: &Value) -> OrderedJson {
    OrderedJson::from_value_with_order(value, &[])
}

impl OrderedJson {
    /// Build from a `Value`, taking the given key order for the top-level object.
    ///
    /// Keys missing from `order` are appended in sorted order, so a schema drift
    /// is visible in the output rather than silently dropped.
    pub fn from_value_with_order(value: &Value, order: &[&str]) -> Self {
        match value {
            Value::Object(fields) => {
                let mut pairs: Vec<(String, OrderedJson)> = Vec::new();
                for key in order {
                    if let Some(item) = fields.get(*key) {
                        pairs.push(((*key).to_string(), nested(item)));
                    }
                }
                for (key, item) in sorted_fields(fields) {
                    if !order.contains(&key.as_str()) {
                        pairs.push((key.clone(), nested(item)));
                    }
                }
                OrderedJson::Object(pairs)
            }
            Value::Array(items) => OrderedJson::List(
                items
                    .iter()
                    .map(|item| OrderedJson::from_value_with_order(item, order))
                    .collect(),
            ),
            other => OrderedJson::Scalar(other.clone()),
        }
    }

    /// `json.dumps(value, ensure_ascii=False, indent=2)`.
    ///
    /// Python switches the separators to `(',', ': ')` whenever `indent` is set,
    /// puts each item on its own line, and renders an empty container inline.
    pub fn render_indent_2(&self) -> String {
        self.render(0)
    }

    fn render(&self, depth: usize) -> String {
        match self {
            OrderedJson::Scalar(value) => match value {
                Value::String(text) => Value::String(text.clone()).to_string(),
                other => other.to_string(),
            },
            OrderedJson::List(items) => {
                if items.is_empty() {
                    return "[]".to_string();
                }
                let inner = " ".repeat((depth + 1) * 2);
                let closing = " ".repeat(depth * 2);
                let rendered: Vec<String> = items
                    .iter()
                    .map(|item| format!("{inner}{}", item.render(depth + 1)))
                    .collect();
                format!("[\n{}\n{closing}]", rendered.join(",\n"))
            }
            OrderedJson::Object(pairs) => {
                if pairs.is_empty() {
                    return "{}".to_string();
                }
                let inner = " ".repeat((depth + 1) * 2);
                let closing = " ".repeat(depth * 2);
                let rendered: Vec<String> = pairs
                    .iter()
                    .map(|(key, item)| {
                        format!(
                            "{inner}{}: {}",
                            Value::String(key.clone()),
                            item.render(depth + 1)
                        )
                    })
                    .collect();
                format!("{{\n{}\n{closing}}}", rendered.join(",\n"))
            }
        }
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
    fn sorted_contracts_do_not_depend_on_serde_json_map_features() {
        let mut nested = serde_json::Map::new();
        nested.insert("z".to_string(), json!(1));
        nested.insert("y".to_string(), json!(2));
        let mut fields = serde_json::Map::new();
        fields.insert("b".to_string(), Value::Object(nested));
        fields.insert("a".to_string(), json!(0));
        let value = Value::Object(fields);

        assert_eq!(
            dumps_default_separators(&value),
            "{\"a\": 0, \"b\": {\"y\": 2, \"z\": 1}}"
        );
        assert_eq!(dumps_compact(&value), "{\"a\":0,\"b\":{\"y\":2,\"z\":1}}");
        assert_eq!(
            OrderedJson::from_value_with_order(&value, &["b"]),
            OrderedJson::Object(vec![
                (
                    "b".to_string(),
                    OrderedJson::Object(vec![
                        ("y".to_string(), OrderedJson::Scalar(json!(2))),
                        ("z".to_string(), OrderedJson::Scalar(json!(1))),
                    ]),
                ),
                ("a".to_string(), OrderedJson::Scalar(json!(0))),
            ])
        );
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
