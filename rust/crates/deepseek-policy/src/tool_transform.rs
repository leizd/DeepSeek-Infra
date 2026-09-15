//! The `data_transform` branch, mirroring the corresponding helpers in
//! `deepseek_infra/infra/tool_runtime/tools.py`.
//!
//! Pure: no I/O, no packages. This is the second of the 18 dispatch branches to
//! be ported, after `generate_chart`.
//!
//! One detail worth knowing before reading the code: Python's path splitter uses
//! a lookahead (`re.split(r"\.(?![^\[]*\])", …)`), which the `regex` crate does
//! not support. It is replaced by a plain split on `.`, which is equivalent for
//! every path this function can *accept* — a well-formed part is
//! `key[index]` with a digits-only index, so no dot can appear inside brackets.
//! Paths where the two disagree are exactly the paths that fail the per-part
//! `fullmatch` and raise "Unsupported JSON path" either way.

use regex::Regex;
use serde_json::{Map, Value, json};

use crate::python_json::dumps_default_separators;
use crate::tool_dispatch::ToolFailure;

/// `ErrorCode.NOT_FOUND`.
pub const NOT_FOUND: &str = "not_found";

/// Mirrors `data_transform`.
pub fn data_transform(
    operation: &str,
    input_text: &str,
    pattern: &str,
    path: &str,
    delimiter: &str,
) -> Result<Value, ToolFailure> {
    let op = operation.trim();
    // The oracle truncates the input to 50k code points before dispatching.
    let text: String = input_text.chars().take(50_000).collect();
    match op {
        "extract_regex" => transform_extract_regex(&text, pattern),
        "json_path" => transform_json_path(&text, path),
        "csv_summary" => transform_csv_summary(&text, delimiter),
        "number_summary" => Ok(transform_number_summary(&text)),
        _ => Err(ToolFailure::app(
            "data_transform",
            "Unsupported data_transform operation",
        )),
    }
}

/// Mirrors `transform_extract_regex`. Caps at 100 matches.
fn transform_extract_regex(text: &str, pattern: &str) -> Result<Value, ToolFailure> {
    if pattern.is_empty() {
        return Err(ToolFailure::app(
            "data_transform",
            "Regex pattern is required",
        ));
    }
    let Ok(compiled) = Regex::new(pattern) else {
        // The oracle embeds the underlying `re.error` text; the exact wording is
        // engine-specific, so this port embeds the `regex` crate's.
        return Err(ToolFailure::app(
            "data_transform",
            format!("Invalid regex: {pattern}"),
        ));
    };
    let mut matches: Vec<Value> = Vec::new();
    for found in compiled.captures_iter(text) {
        let whole = found.get(0).expect("group 0 always participates");
        let groups: Vec<Value> = (1..found.len())
            .map(|index| match found.get(index) {
                Some(group) => Value::String(group.as_str().to_string()),
                None => Value::Null,
            })
            .collect();
        matches.push(json!({
            "match": whole.as_str(),
            "groups": groups,
            "start": text[..whole.start()].chars().count(),
            "end": text[..whole.end()].chars().count(),
        }));
        if matches.len() >= 100 {
            break;
        }
    }
    Ok(json!({
        "operation": "extract_regex",
        "count": matches.len(),
        "matches": matches,
    }))
}

/// Mirrors `transform_json_path`.
fn transform_json_path(text: &str, path: &str) -> Result<Value, ToolFailure> {
    let value = match serde_json::from_str::<Value>(text) {
        Ok(value) => value,
        // The oracle embeds its engine's own diagnostic after the prefix; this
        // port embeds serde_json's. The prefix is ours and identical.
        Err(error) => {
            return Err(ToolFailure::app(
                "data_transform",
                format!("Invalid JSON: {error}"),
            ));
        }
    };
    let selected = read_simple_json_path(&value, path)?;
    Ok(json!({
        "operation": "json_path",
        "path": path,
        "value": compact_json_value(&selected),
    }))
}

/// Mirrors `read_simple_json_path`.
fn read_simple_json_path(value: &Value, path: &str) -> Result<Value, ToolFailure> {
    let expression = if path.trim().is_empty() {
        "$"
    } else {
        path.trim()
    };
    if expression == "$" {
        return Ok(value.clone());
    }
    let expression = expression
        .strip_prefix("$.")
        .or_else(|| expression.strip_prefix('$'))
        .unwrap_or(expression);

    let part_regex = Regex::new(r"^([A-Za-z0-9_-]+)(?:\[(\d+)\])?$")
        .expect("static path-part pattern must compile");
    let mut current = value.clone();
    for part in expression.split('.').filter(|part| !part.is_empty()) {
        let Some(captures) = part_regex.captures(part) else {
            return Err(ToolFailure::app("data_transform", "Unsupported JSON path"));
        };
        let key = captures.get(1).expect("group 1 is required").as_str();
        let index_text = captures.get(2).map(|group| group.as_str());

        let Some(object) = current.as_object() else {
            return Err(ToolFailure {
                tool: "data_transform".to_string(),
                error: "JSON path not found".to_string(),
                code: NOT_FOUND.to_string(),
            });
        };
        let Some(next) = object.get(key) else {
            return Err(ToolFailure {
                tool: "data_transform".to_string(),
                error: "JSON path not found".to_string(),
                code: NOT_FOUND.to_string(),
            });
        };
        current = next.clone();

        if let Some(index_text) = index_text {
            let index: usize = index_text.parse().unwrap_or(usize::MAX);
            let Some(items) = current.as_array() else {
                return Err(ToolFailure {
                    tool: "data_transform".to_string(),
                    error: "JSON path not found".to_string(),
                    code: NOT_FOUND.to_string(),
                });
            };
            if index >= items.len() {
                return Err(ToolFailure {
                    tool: "data_transform".to_string(),
                    error: "JSON path not found".to_string(),
                    code: NOT_FOUND.to_string(),
                });
            }
            current = items[index].clone();
        }
    }
    Ok(current)
}

/// Mirrors `compact_json_value`: return the value unchanged unless its default
/// (`", "` / `": "`) encoding exceeds 4000 characters, in which case return the
/// truncated string.
///
/// The length is measured in **code points**, as Python's `len(str)` is.
fn compact_json_value(value: &Value) -> Value {
    let encoded = dumps_default_separators(value);
    if encoded.chars().count() <= 4000 {
        value.clone()
    } else {
        Value::String(encoded.chars().take(4000).collect())
    }
}

/// Mirrors `transform_number_summary`.
fn transform_number_summary(text: &str) -> Value {
    let number_regex =
        Regex::new(r"[-+]?(?:\d+\.\d+|\d+|\.\d+)").expect("static number pattern must compile");
    let values: Vec<f64> = number_regex
        .find_iter(text)
        .filter_map(|found| found.as_str().parse::<f64>().ok())
        .collect();
    let mut payload = match number_summary_payload("numbers", &values).as_object() {
        Some(fields) => fields.clone(),
        None => Map::new(),
    };
    payload.insert(
        "operation".to_string(),
        Value::String("number_summary".to_string()),
    );
    Value::Object(payload)
}

/// Mirrors `number_summary_payload`.
fn number_summary_payload(label: &str, values: &[f64]) -> Value {
    if values.is_empty() {
        return json!({"label": label, "count": 0});
    }
    let count = values.len();
    let mut sorted = values.to_vec();
    let min = values.iter().copied().fold(f64::INFINITY, f64::min);
    let max = values.iter().copied().fold(f64::NEG_INFINITY, f64::max);
    let sum: f64 = values.iter().sum();
    let mean = sum / count as f64;
    // `statistics.median`: the middle value, or the mean of the two middles.
    // It sorts with `<`, so a partial comparison is enough here.
    sorted.sort_by(|left, right| left.partial_cmp(right).unwrap_or(std::cmp::Ordering::Equal));
    let median = if count % 2 == 1 {
        sorted[count / 2]
    } else {
        (sorted[count / 2 - 1] + sorted[count / 2]) / 2.0
    };
    json!({
        "label": label,
        "count": count,
        "min": min,
        "max": max,
        "sum": sum,
        "mean": mean,
        "median": median,
    })
}

/// Mirrors `transform_csv_summary`.
fn transform_csv_summary(text: &str, delimiter: &str) -> Result<Value, ToolFailure> {
    let delimiter = (delimiter.chars().next()).unwrap_or(',');
    let mut rows = csv_read(text, delimiter);
    rows.truncate(501);
    if rows.is_empty() {
        return Ok(json!({
            "operation": "csv_summary",
            "rows": 0,
            "columns": [],
            "numericColumns": [],
        }));
    }
    let headers: Vec<String> = rows[0]
        .iter()
        .enumerate()
        .map(|(index, cell)| {
            let text = if cell.is_empty() {
                format!("col{}", index + 1)
            } else {
                cell.clone()
            };
            text.chars().take(80).collect()
        })
        .collect();

    let mut numeric_columns: Vec<Value> = Vec::new();
    for (column_index, header) in headers.iter().enumerate() {
        let mut values: Vec<f64> = Vec::new();
        for row in rows.iter().skip(1) {
            let Some(cell) = row.get(column_index) else {
                continue;
            };
            // Python strips thousands separators before parsing.
            let candidate = cell.replace(',', "");
            let candidate = candidate.trim();
            if let Ok(value) = candidate.parse::<f64>() {
                values.push(value);
            }
        }
        if !values.is_empty() {
            numeric_columns.push(number_summary_payload(header, &values));
        }
    }

    Ok(json!({
        "operation": "csv_summary",
        "rows": rows.len().saturating_sub(1),
        "columns": headers,
        "numericColumns": numeric_columns,
    }))
}

/// Read CSV text the way Python's `csv.reader` does with its default dialect:
/// `quotechar='"'`, doubled quotes escape a literal quote, and a quoted field may
/// contain the delimiter or a newline.
///
/// Blank trailing lines do not produce a row, matching `csv.reader` over a
/// `StringIO`.
fn csv_read(text: &str, delimiter: char) -> Vec<Vec<String>> {
    let mut rows: Vec<Vec<String>> = Vec::new();
    let mut row: Vec<String> = Vec::new();
    let mut field = String::new();
    let mut in_quotes = false;
    let mut field_started = false;
    let mut chars = text.chars().peekable();

    while let Some(character) = chars.next() {
        if in_quotes {
            if character == '"' {
                if chars.peek() == Some(&'"') {
                    chars.next();
                    field.push('"');
                } else {
                    in_quotes = false;
                }
            } else {
                field.push(character);
            }
            continue;
        }
        if character == '"' && !field_started {
            in_quotes = true;
            field_started = true;
            continue;
        }
        if character == delimiter {
            row.push(std::mem::take(&mut field));
            field_started = false;
            continue;
        }
        if character == '\n' || character == '\r' {
            if character == '\r' && chars.peek() == Some(&'\n') {
                chars.next();
            }
            row.push(std::mem::take(&mut field));
            rows.push(std::mem::take(&mut row));
            field_started = false;
            continue;
        }
        field.push(character);
        field_started = true;
    }

    // A trailing `\n` must not synthesise an extra row, but a final unterminated
    // line must still be emitted.
    if !field.is_empty() || !row.is_empty() {
        row.push(field);
        rows.push(row);
    }
    rows
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tool_dispatch::INVALID_PAYLOAD;

    #[test]
    fn extract_regex_reports_matches_and_groups() {
        let result = transform_extract_regex("a1 b2 c3", r"([a-z])(\d)").unwrap();
        assert_eq!(result["operation"], "extract_regex");
        assert_eq!(result["count"], 3);
        assert_eq!(result["matches"][0]["match"], "a1");
        assert_eq!(result["matches"][0]["groups"], json!(["a", "1"]));
        assert_eq!(result["matches"][0]["start"], 0);
        assert_eq!(result["matches"][0]["end"], 2);
        assert_eq!(result["matches"][2]["start"], 6);
    }

    #[test]
    fn extract_regex_caps_at_one_hundred_matches() {
        let text = "x".repeat(250);
        let result = transform_extract_regex(&text, "x").unwrap();
        assert_eq!(result["count"], 100);
        assert_eq!(result["matches"].as_array().unwrap().len(), 100);
    }

    #[test]
    fn extract_regex_rejects_missing_and_invalid_patterns() {
        let missing = transform_extract_regex("x", "").unwrap_err();
        assert_eq!(missing.error, "Regex pattern is required");
        assert_eq!(missing.code, INVALID_PAYLOAD);
        assert!(transform_extract_regex("x", "(").is_err());
    }

    #[test]
    fn json_path_reads_simple_paths() {
        let text = r#"{"a": {"b": [10, 20]}}"#;
        let whole = transform_json_path(text, "$").unwrap();
        assert_eq!(whole["value"]["a"]["b"], json!([10, 20]));

        let nested = transform_json_path(text, "$.a.b[1]").unwrap();
        assert_eq!(nested["value"], 20);

        // A path without the `$` prefix works too.
        assert_eq!(transform_json_path(text, "a.b[0]").unwrap()["value"], 10);
        // An empty path is `$`.
        assert_eq!(
            transform_json_path(text, "").unwrap()["value"]["a"]["b"][0],
            10
        );
    }

    #[test]
    fn json_path_reports_not_found_and_unsupported() {
        let text = r#"{"a": 1}"#;
        let missing = transform_json_path(text, "$.nope").unwrap_err();
        assert_eq!(missing.error, "JSON path not found");
        assert_eq!(missing.code, NOT_FOUND);

        let unsupported = transform_json_path(text, "$.a[*]").unwrap_err();
        assert_eq!(unsupported.error, "Unsupported JSON path");
        assert_eq!(unsupported.code, INVALID_PAYLOAD);

        // Out-of-range indexing is "not found".
        let out_of_range = transform_json_path(r#"{"a": [1]}"#, "$.a[5]").unwrap_err();
        assert_eq!(out_of_range.code, NOT_FOUND);

        assert!(transform_json_path("not json", "$").is_err());
    }

    #[test]
    fn json_path_compacts_only_oversized_values() {
        let small = json!({"a": 1});
        assert_eq!(compact_json_value(&small), small);

        // Over 4000 characters of default-separated JSON becomes a truncated
        // string rather than the value.
        let big = json!({"a": "x".repeat(5000)});
        let compact = compact_json_value(&big);
        let text = compact.as_str().expect("oversized values become a string");
        assert_eq!(text.chars().count(), 4000);
        assert!(text.starts_with("{\"a\": \"xxx"));
    }

    #[test]
    fn number_summary_handles_the_simple_statistics() {
        let summary = transform_number_summary("1 2 3 4");
        assert_eq!(summary["operation"], "number_summary");
        assert_eq!(summary["count"], 4);
        assert_eq!(summary["min"], 1.0);
        assert_eq!(summary["max"], 4.0);
        assert_eq!(summary["sum"], 10.0);
        assert_eq!(summary["mean"], 2.5);
        assert_eq!(summary["median"], 2.5);
    }

    #[test]
    fn number_summary_matches_python_extraction() {
        // Signs, decimals, and a bare leading dot.
        let summary = transform_number_summary("-1.5 and +2 and .5");
        assert_eq!(summary["count"], 3);
        assert_eq!(summary["sum"], 1.0);

        // No numbers at all still reports a labelled zero count.
        let empty = transform_number_summary("no digits here");
        assert_eq!(empty["count"], 0);
        assert_eq!(empty["label"], "numbers");
        assert!(empty.get("min").is_none());
    }

    #[test]
    fn csv_read_handles_quotes_and_newlines() {
        assert_eq!(
            csv_read("a,b\n1,2\n", ','),
            vec![vec!["a", "b"], vec!["1", "2"]]
        );
        // A quoted field may contain the delimiter.
        assert_eq!(
            csv_read("a,b\n\"x,y\",2\n", ','),
            vec![vec!["a", "b"], vec!["x,y", "2"]]
        );
        // Doubled quotes escape a literal quote.
        assert_eq!(
            csv_read("a\n\"say \"\"hi\"\"\"\n", ','),
            vec![vec!["a"], vec!["say \"hi\""]]
        );
        // A quoted field may span a newline.
        assert_eq!(
            csv_read("a\n\"one\ntwo\"\n", ','),
            vec![vec!["a"], vec!["one\ntwo"]]
        );
        // A trailing newline adds no row; an unterminated final line does.
        assert_eq!(csv_read("a\n", ','), vec![vec!["a"]]);
        assert_eq!(csv_read("a", ','), vec![vec!["a"]]);
        assert_eq!(csv_read("", ','), Vec::<Vec<String>>::new());
    }

    #[test]
    fn csv_summary_names_columns_and_summarizes_numbers() {
        let result = transform_csv_summary("name,value\nx,1\ny,2.5\n", ",").unwrap();
        assert_eq!(result["operation"], "csv_summary");
        assert_eq!(result["rows"], 2);
        assert_eq!(result["columns"], json!(["name", "value"]));
        // Only the numeric column is summarized.
        assert_eq!(result["numericColumns"].as_array().unwrap().len(), 1);
        assert_eq!(result["numericColumns"][0]["label"], "value");
        assert_eq!(result["numericColumns"][0]["count"], 2);
        assert_eq!(result["numericColumns"][0]["sum"], 3.5);
    }

    #[test]
    fn csv_summary_defaults_blank_headers_and_reads_thousands_separators() {
        let result = transform_csv_summary(",v\n,\"1,000\"\n", ",").unwrap();
        assert_eq!(result["columns"], json!(["col1", "v"]));
        assert_eq!(result["numericColumns"][0]["sum"], 1000.0);
    }

    #[test]
    fn csv_summary_handles_an_empty_document_and_a_single_row() {
        let empty = transform_csv_summary("", ",").unwrap();
        assert_eq!(empty["rows"], 0);
        assert_eq!(empty["columns"], json!([]));

        let header_only = transform_csv_summary("a,b\n", ",").unwrap();
        assert_eq!(header_only["rows"], 0);
        assert_eq!(header_only["columns"], json!(["a", "b"]));
        assert_eq!(header_only["numericColumns"], json!([]));
    }

    #[test]
    fn data_transform_routes_and_rejects_unknown_operations() {
        assert_eq!(
            data_transform("number_summary", "1 2", "", "", ",").unwrap()["count"],
            2
        );
        let unknown = data_transform("nope", "x", "", "", ",").unwrap_err();
        assert_eq!(unknown.error, "Unsupported data_transform operation");
        assert_eq!(unknown.tool, "data_transform");
    }

    #[test]
    fn data_transform_truncates_input_before_dispatch() {
        // 50k cap: a match past the cap must not be reported.
        let text = format!("{}z", "a".repeat(50_000));
        let result = data_transform("extract_regex", &text, "z", "", ",").unwrap();
        assert_eq!(result["count"], 0);
    }
}
