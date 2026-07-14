//! Side-effect-free preparation of text already parsed by Python.

use blake2::Blake2bVar;
use blake2::digest::{Update, VariableOutput};
use serde_json::{Map, Value, json};
use std::collections::BTreeMap;

pub const MAX_DOCUMENT_CHARACTERS: usize = 8_000_000;
pub const MAX_REQUEST_BYTES: usize = 40_000_000;
pub const MAX_NESTING: usize = 24;
pub const MAX_DOCUMENT_ID_CHARACTERS: usize = 256;
pub const MAX_CHUNK_CHARACTERS: usize = 1_000_000;

const TOP_LEVEL_KEYS: &[&str] = &["documentId", "text", "metadata", "chunking"];
const CHUNKING_KEYS: &[&str] = &["chunkChars", "chunkOverlap"];
const METADATA_KEYS: &[&str] = &["displayName", "sourceType", "kind"];
const SENSITIVE_KEY_PARTS: &[&str] = &[
    "absolutepath",
    "temporarypath",
    "uploadpath",
    "cachepath",
    "authorization",
    "apikey",
    "token",
    "rawfilebytes",
    "databaselocation",
    "workspacesecret",
    "filesystempath",
];

fn error(code: &'static str, message: &'static str) -> Value {
    json!({"ok": false, "code": code, "message": message})
}

fn normalized_key(value: &str) -> String {
    value
        .chars()
        .flat_map(char::to_lowercase)
        .filter(|character| character.is_alphanumeric())
        .collect()
}

fn sensitive_key(value: &str) -> bool {
    let normalized = normalized_key(value);
    normalized.ends_with("path")
        || SENSITIVE_KEY_PARTS
            .iter()
            .any(|part| normalized.contains(part))
}

fn max_depth(value: &Value) -> usize {
    let mut maximum = 1;
    let mut stack = vec![(value, 1usize)];
    while let Some((current, depth)) = stack.pop() {
        maximum = maximum.max(depth);
        if depth > MAX_NESTING {
            return depth;
        }
        match current {
            Value::Array(items) => {
                for item in items {
                    if item.is_array() || item.is_object() {
                        stack.push((item, depth + 1));
                    }
                }
            }
            Value::Object(items) => {
                for item in items.values() {
                    if item.is_array() || item.is_object() {
                        stack.push((item, depth + 1));
                    }
                }
            }
            _ => {}
        }
    }
    maximum
}

pub fn normalize_document_text(value: &str) -> String {
    let line_endings = value
        .replace("\r\n", "\n")
        .replace('\r', "\n")
        .replace('\0', "");
    line_endings
        .split('\n')
        .map(str::trim_end)
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_string()
}

fn blake2b_96(bytes: &[u8]) -> String {
    let mut hasher = Blake2bVar::new(12).expect("BLAKE2b-96 output size is valid");
    hasher.update(bytes);
    let mut output = [0u8; 12];
    hasher
        .finalize_variable(&mut output)
        .expect("fixed output buffer has the configured size");
    output.iter().map(|byte| format!("{byte:02x}")).collect()
}

pub fn chunk_content_hash(text: &str) -> String {
    blake2b_96(text.trim().as_bytes())
}

pub fn document_content_hash(chunk_hashes: &BTreeMap<usize, String>) -> String {
    let mut bytes = Vec::new();
    for (index, content_hash) in chunk_hashes {
        bytes.extend_from_slice(format!("{index}:{content_hash}\0").as_bytes());
    }
    blake2b_96(&bytes)
}

fn normalize_metadata(value: Option<&Value>) -> Result<Map<String, Value>, Value> {
    let Some(value) = value else {
        return Ok(Map::new());
    };
    let Some(metadata) = value.as_object() else {
        return Err(error("invalid_metadata", "metadata must be an object"));
    };
    if metadata.keys().any(|key| sensitive_key(key)) {
        return Err(error(
            "invalid_metadata",
            "metadata contains a path or credential field",
        ));
    }
    let mut normalized = Map::new();
    for key in METADATA_KEYS {
        let Some(item) = metadata.get(*key) else {
            continue;
        };
        if item.is_null() || item.is_string() || item.is_boolean() || item.is_number() {
            normalized.insert((*key).to_string(), item.clone());
        } else {
            return Err(error(
                "invalid_metadata",
                "allowlisted metadata values must be JSON scalars",
            ));
        }
    }
    Ok(normalized)
}

fn bounded_integer(value: Option<&Value>) -> Option<i64> {
    value.and_then(Value::as_i64)
}

fn chunk_text(
    text: &str,
    document_id: &str,
    chunk_chars: usize,
    chunk_overlap: usize,
) -> Vec<Value> {
    let characters: Vec<char> = text.chars().collect();
    let text_length = characters.len();
    let mut chunks = Vec::new();
    let mut start = 0usize;
    while start < text_length {
        let mut end = (start + chunk_chars).min(text_length);
        if end < text_length {
            let boundary = (start..end).rev().find(|index| characters[*index] == '\n');
            if let Some(boundary) = boundary
                && boundary > start + chunk_chars / 2
            {
                end = boundary;
            }
        }
        let raw: String = characters[start..end].iter().collect();
        let body = raw.trim().to_string();
        if !body.is_empty() {
            let index = chunks.len();
            let content_hash = chunk_content_hash(&body);
            let line_start = characters[..start]
                .iter()
                .filter(|character| **character == '\n')
                .count()
                + 1;
            let line_end = characters[..end]
                .iter()
                .filter(|character| **character == '\n')
                .count()
                + 1;
            chunks.push(json!({
                "index": index,
                "chunkId": format!("{document_id}:{index}:{content_hash}"),
                "text": body,
                "start": start,
                "end": end,
                "lineStart": line_start,
                "lineEnd": line_end,
                "contentHash": content_hash,
            }));
        }
        if end >= text_length {
            break;
        }
        start = end.saturating_sub(chunk_overlap).max(start + 1);
    }
    chunks
}

pub fn prepare_document_value(value: &Value, payload_size: usize) -> Value {
    if payload_size > MAX_REQUEST_BYTES {
        return error(
            "request_too_large",
            "document preparation request is too large",
        );
    }
    if max_depth(value) > MAX_NESTING {
        return error(
            "nesting_limit_exceeded",
            "document preparation request is too deeply nested",
        );
    }
    let Some(request) = value.as_object() else {
        return error("invalid_request", "request must be a JSON object");
    };
    if request.keys().any(|key| sensitive_key(key)) {
        return error(
            "invalid_request",
            "request contains a path or credential field",
        );
    }
    if request
        .keys()
        .any(|key| !TOP_LEVEL_KEYS.contains(&key.as_str()))
    {
        return error("invalid_request", "request contains unsupported fields");
    }

    let Some(document_id) = request.get("documentId").and_then(Value::as_str) else {
        return error(
            "invalid_document_id",
            "documentId must be a normalized non-empty string",
        );
    };
    if document_id.is_empty()
        || document_id.trim() != document_id
        || document_id.chars().count() > MAX_DOCUMENT_ID_CHARACTERS
        || document_id.chars().any(|character| character < ' ')
    {
        return error(
            "invalid_document_id",
            "documentId must be a normalized non-empty string",
        );
    }
    let Some(raw_text) = request.get("text").and_then(Value::as_str) else {
        return error("invalid_text", "text must be a string");
    };
    let normalized_text = normalize_document_text(raw_text);
    if normalized_text.is_empty() {
        return error("invalid_text", "text must contain readable content");
    }
    if normalized_text.chars().count() > MAX_DOCUMENT_CHARACTERS {
        return error(
            "document_too_large",
            "normalized document exceeds the character limit",
        );
    }
    let metadata = match normalize_metadata(request.get("metadata")) {
        Ok(metadata) => metadata,
        Err(error) => return error,
    };
    let Some(chunking) = request.get("chunking").and_then(Value::as_object) else {
        return error("invalid_request", "chunking must be an object");
    };
    if chunking.keys().any(|key| sensitive_key(key))
        || chunking
            .keys()
            .any(|key| !CHUNKING_KEYS.contains(&key.as_str()))
    {
        return error("invalid_request", "chunking contains unsupported fields");
    }
    let Some(chunk_chars) = bounded_integer(chunking.get("chunkChars")) else {
        return error(
            "invalid_chunk_size",
            "chunkChars must be a positive bounded integer",
        );
    };
    if chunk_chars <= 0 || chunk_chars as usize > MAX_CHUNK_CHARACTERS {
        return error(
            "invalid_chunk_size",
            "chunkChars must be a positive bounded integer",
        );
    }
    let Some(chunk_overlap) = bounded_integer(chunking.get("chunkOverlap")) else {
        return error(
            "invalid_chunk_overlap",
            "chunkOverlap must be a non-negative integer",
        );
    };
    if chunk_overlap < 0 {
        return error(
            "invalid_chunk_overlap",
            "chunkOverlap must be a non-negative integer",
        );
    }
    if chunk_overlap >= chunk_chars {
        return error(
            "chunk_overlap_too_large",
            "chunkOverlap must be smaller than chunkChars",
        );
    }

    let chunks = chunk_text(
        &normalized_text,
        document_id,
        chunk_chars as usize,
        chunk_overlap as usize,
    );
    let chunk_hashes = chunks
        .iter()
        .filter_map(|chunk| {
            Some((
                chunk.get("index")?.as_u64()? as usize,
                chunk.get("contentHash")?.as_str()?.to_string(),
            ))
        })
        .collect::<BTreeMap<_, _>>();
    json!({
        "ok": true,
        "document": {
            "documentId": document_id,
            "contentHash": document_content_hash(&chunk_hashes),
            "characterCount": normalized_text.chars().count(),
            "chunkCount": chunks.len(),
            "metadata": metadata,
        },
        "chunks": chunks,
        "chunking": {"chunkChars": chunk_chars, "chunkOverlap": chunk_overlap},
        "diagnostics": {"normalized": normalized_text != raw_text},
    })
}

pub fn prepare_document_bytes(body: &[u8]) -> Value {
    if body.len() > MAX_REQUEST_BYTES {
        return error(
            "request_too_large",
            "document preparation request is too large",
        );
    }
    let value: Value = match serde_json::from_slice(body) {
        Ok(value) => value,
        Err(_) => return error("invalid_request", "request must contain valid JSON"),
    };
    prepare_document_value(&value, body.len())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(text: &str, chunk_chars: i64, chunk_overlap: i64) -> Value {
        json!({
            "documentId":"doc-1",
            "text":text,
            "metadata":{"displayName":"notes.txt","sourceType":"text/plain"},
            "chunking":{"chunkChars":chunk_chars,"chunkOverlap":chunk_overlap}
        })
    }

    #[test]
    fn prepares_minimal_document() {
        let result = prepare_document_value(&request("hello", 6000, 400), 100);
        assert_eq!(result["ok"], true);
        assert_eq!(result["chunks"][0]["text"], "hello");
    }

    #[test]
    fn normalizes_line_endings() {
        let result = prepare_document_value(&request(" a  \r\n b\r\n", 20, 0), 100);
        assert_eq!(result["chunks"][0]["text"], "a\n b");
        assert_eq!(result["document"]["characterCount"], 4);
    }

    #[test]
    fn chunks_exact_boundary() {
        let result = prepare_document_value(&request("abcdefgh", 4, 0), 100);
        assert_eq!(result["document"]["chunkCount"], 2);
        assert_eq!(result["chunks"][1]["start"], 4);
    }

    #[test]
    fn chunks_with_overlap() {
        let result = prepare_document_value(&request("abcdefghij", 6, 2), 100);
        assert_eq!(result["chunks"][0]["text"], "abcdef");
        assert_eq!(result["chunks"][1]["text"], "efghij");
    }

    #[test]
    fn prevents_overlap_infinite_loop() {
        let result = prepare_document_value(&request("abcdefgh", 4, 4), 100);
        assert_eq!(result["code"], "chunk_overlap_too_large");

        let boundary = prepare_document_value(&request("aaaaaa\nbbbbbbbbbbbb", 10, 9), 100);
        assert_eq!(boundary["ok"], true);
        assert!(boundary["chunks"].as_array().unwrap().len() <= 19);
    }

    #[test]
    fn preserves_paragraph_boundaries() {
        let result = prepare_document_value(&request("aaaa\n\nbbbb\ncccc", 12, 0), 100);
        assert_eq!(result["chunks"][0]["text"], "aaaa\n\nbbbb");
    }

    #[test]
    fn preserves_cjk() {
        let result = prepare_document_value(&request("中文分块测试", 3, 1), 100);
        assert_eq!(result["chunks"][0]["text"], "中文分");
        assert_eq!(result["chunks"][1]["start"], 2);
    }

    #[test]
    fn preserves_emoji() {
        let result = prepare_document_value(&request("A🚀B🙂C", 3, 1), 100);
        assert_eq!(result["chunks"][0]["text"], "A🚀B");
    }

    #[test]
    fn uses_character_offsets_not_byte_offsets() {
        let result = prepare_document_value(&request("中🚀ab", 2, 0), 100);
        assert_eq!(result["chunks"][1]["start"], 2);
        assert_eq!(result["chunks"][1]["text"], "ab");
    }

    #[test]
    fn handles_combining_characters() {
        let result = prepare_document_value(&request("e\u{301}x", 2, 0), 100);
        assert_eq!(result["chunks"][0]["end"], 2);
        assert_eq!(result["chunks"][0]["text"], "e\u{301}");
    }

    #[test]
    fn generates_stable_document_hash() {
        let left = prepare_document_value(&request("stable text", 6, 1), 100);
        let right = prepare_document_value(&request("stable text", 6, 1), 100);
        assert_eq!(
            left["document"]["contentHash"],
            right["document"]["contentHash"]
        );
    }

    #[test]
    fn generates_unique_chunk_ids() {
        let result = prepare_document_value(&request("abcdefghijkl", 4, 1), 100);
        let ids = result["chunks"]
            .as_array()
            .unwrap()
            .iter()
            .map(|chunk| chunk["chunkId"].as_str().unwrap())
            .collect::<std::collections::HashSet<_>>();
        assert_eq!(ids.len(), result["chunks"].as_array().unwrap().len());
    }

    #[test]
    fn rejects_invalid_chunk_size() {
        assert_eq!(
            prepare_document_value(&request("x", 0, 0), 100)["code"],
            "invalid_chunk_size"
        );
    }

    #[test]
    fn rejects_invalid_overlap() {
        assert_eq!(
            prepare_document_value(&request("x", 4, -1), 100)["code"],
            "invalid_chunk_overlap"
        );
    }

    #[test]
    fn rejects_oversized_document() {
        let value = request("small", 4, 0);
        assert_eq!(
            prepare_document_value(&value, MAX_REQUEST_BYTES + 1)["code"],
            "request_too_large"
        );
        assert_eq!(MAX_DOCUMENT_CHARACTERS, 8_000_000);
    }

    #[test]
    fn rejects_excessive_nesting() {
        let mut nested = json!({});
        for _ in 0..=MAX_NESTING {
            nested = json!({"nested": nested});
        }
        let mut value = request("x", 4, 0);
        value["metadata"] = nested;
        assert_eq!(
            prepare_document_value(&value, 100)["code"],
            "nesting_limit_exceeded"
        );
    }

    #[test]
    fn rejects_path_like_sensitive_fields() {
        let mut value = request("x", 4, 0);
        value["metadata"]["absolutePath"] = json!("C:/secret.txt");
        assert_eq!(
            prepare_document_value(&value, 100)["code"],
            "invalid_metadata"
        );
    }

    #[test]
    fn response_roundtrips_json() {
        let result = prepare_document_value(&request("中文🚀", 2, 0), 100);
        let encoded = serde_json::to_vec(&result).unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&encoded).unwrap(), result);
    }

    #[test]
    fn never_reads_files() {
        let mut value = request("already parsed", 20, 0);
        value["uploadPath"] = json!("/tmp/upload");
        assert_eq!(
            prepare_document_value(&value, 100)["code"],
            "invalid_request"
        );
    }

    #[test]
    fn never_writes_index() {
        let value = request("pure transformation", 20, 0);
        let first = prepare_document_value(&value, 100);
        let second = prepare_document_value(&value, 100);
        assert_eq!(first, second);
    }

    #[test]
    fn never_receives_credentials() {
        let mut value = request("x", 4, 0);
        value["metadata"]["authorization"] = json!("Bearer secret");
        assert_eq!(
            prepare_document_value(&value, 100)["code"],
            "invalid_metadata"
        );
    }
}
