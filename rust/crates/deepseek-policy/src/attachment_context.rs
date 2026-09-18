//! Attachment expansion — the pure half of `rag/files.py` (lines 69-283) with
//! `gateway/chat_payload.expanded_message_content` on top.
//!
//! Three things the oracle reaches for are not pure, and all three arrive as parameters:
//! `load_cached_file` (the file index), `local_rag.search_file_chunks` (the vector index), and
//! the embedding pipeline (`local_rag.embed_text` — the local hash embedding unless an ONNX
//! model is configured). `context_taint_file_guard_line` is already ported, so the caller hands
//! in its output rather than this module re-deriving it; an empty string is the firewall-off
//! case, and it drops a row from the header.
//!
//! # Lengths are code points
//!
//! Every budget and every truncation here counts **code points**, matching `len(str)` in
//! Python. Bytes would cut a Chinese attachment at a different place, and the section
//! boundaries — which are part of the prompt the model sees — would differ.
//!
//! # Two asymmetries, measured and recorded
//!
//! `hash_text_embedding` lowercases with Python's `str.lower`, which is a *simple* case
//! mapping; Rust's `to_lowercase` is the full one. They agree on ASCII and on text without
//! case (`中文内容`), which is what the corpus covers, and they can differ on characters like
//! `İ`. And the oracle's `int(...)` calls inside the formatters **raise** on an unusable value
//! (a `charCount` of `"abc"` would be a 500); this reads zero instead. Both are unreachable
//! from the wired path — the file index is written by `extract_uploaded_file`, which fixes the
//! field types — and both are called out rather than smoothed over.

use std::collections::BTreeSet;
use std::sync::OnceLock;

use blake2::Blake2bVar;
use blake2::digest::{Update, VariableOutput};
use regex::Regex;
use serde_json::Value;

use crate::app_error::AppError;
use crate::core_utils::{
    python_float_opt, python_int_opt, python_truthy, query_tokens, round_six, score_chunk,
    text_or_empty,
};
use crate::python_json::value_str;

/// Mirrors `FILE_FULL_CONTEXT_LIMIT`.
pub const FILE_FULL_CONTEXT_LIMIT: i64 = 60_000;
/// Mirrors `FILE_CONTEXT_CHAR_BUDGET`.
pub const FILE_CONTEXT_CHAR_BUDGET: i64 = 115_000;
/// Mirrors `FILE_CONTEXT_MAX_CHUNKS`.
pub const FILE_CONTEXT_MAX_CHUNKS: usize = 18;
/// Mirrors `LOCAL_RAG_EMBEDDING_DIMENSIONS`.
pub const LOCAL_RAG_EMBEDDING_DIMENSIONS: usize = 64;
/// The floor a single attachment's share of the budget cannot fall below.
const PER_FILE_BUDGET_FLOOR: i64 = 8_000;

/// `load_cached_file(file_id, project_id)` — the file index.
type LoadCachedFile<'a> = &'a dyn Fn(&str, Option<&str>) -> Result<Value, AppError>;
/// `local_rag.search_file_chunks(file_id, project_id, query, limit)` — the vector index.
type SearchFileChunks<'a> = &'a dyn Fn(&str, &str, &str, usize) -> Vec<i64>;
/// `local_rag.embed_text` — the embedding pipeline.
type EmbedText<'a> = &'a dyn Fn(&str) -> Vec<f64>;

/// The reads and the embedding pipeline the pure half needs, injected.
///
/// `load_cached_file` returns the cached index document; a missing or expired index is an
/// `Err`, which becomes the visible `[文件索引读取失败：...]` row rather than a silent omission.
pub struct FileContextDeps<'a> {
    pub load_cached_file: LoadCachedFile<'a>,
    pub search_file_chunks: SearchFileChunks<'a>,
    pub embed: EmbedText<'a>,
    pub guard_line: String,
}

/// Python's `len(str)` — code points, not bytes.
fn char_len(text: &str) -> usize {
    text.chars().count()
}

/// Python's `s[:n]`.
fn char_take(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

/// `str(value or fallback)` for an attachment's display name.
fn name_or(value: Option<&Value>, fallback: impl FnOnce() -> String) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => fallback(),
    }
}

fn blake2b_4(data: &[u8]) -> [u8; 4] {
    let mut hasher = Blake2bVar::new(4).expect("4 is a valid blake2b digest length");
    hasher.update(data);
    let mut out = [0u8; 4];
    hasher
        .finalize_variable(&mut out)
        .expect("the buffer matches the requested length");
    out
}

/// Mirrors `hash_text_embedding`: a signed bag-of-tokens hash, L2-normalised.
pub fn hash_text_embedding(text: &str, dimensions: usize) -> Vec<f64> {
    let size = dimensions.max(1);
    let mut vector = vec![0.0f64; size];
    let value = text.to_lowercase();
    for feature in query_tokens(&value) {
        let number = u32::from_be_bytes(blake2b_4(feature.as_bytes())) as u64;
        let index = (number % size as u64) as usize;
        vector[index] += if number & 1 == 1 { -1.0 } else { 1.0 };
    }
    normalise_cleaned(vector)
}

/// Mirrors `normalize_vector`.
pub fn normalize_vector(vector: &[Value], dimensions: usize) -> Vec<f64> {
    let size = dimensions.max(1);
    let mut cleaned: Vec<f64> = vector
        .iter()
        .take(size)
        .map(|item| python_float_opt(item).unwrap_or(0.0))
        .collect();
    cleaned.resize(size, 0.0);
    normalise_cleaned(cleaned)
}

/// The shared tail of both entry points: L2-normalise, or return the vector as-is when the
/// norm is not positive.
fn normalise_cleaned(cleaned: Vec<f64>) -> Vec<f64> {
    let norm = cleaned.iter().map(|item| item * item).sum::<f64>().sqrt();
    if norm <= 0.0 {
        return cleaned;
    }
    cleaned
        .into_iter()
        .map(|item| round_six(item / norm))
        .collect()
}

/// Mirrors `cosine_similarity`: `zip` over the shorter side, unusable values skipped, and the
/// result clamped into `0.0..=1.0`.
///
/// Both sides are JSON values because the oracle takes any `Sequence` — a chunk carries its
/// vector as parsed JSON, the embedding pipeline hands back floats, and either one may hold a
/// value `float()` rejects. The clamp is written as comparisons rather than `f64::clamp`,
/// because Python's `min`/`max` keep the bound when a comparison is false — so a `NaN` total
/// comes out as **1.0**, where `clamp` would hand back `NaN` and no JSON writer would take it.
pub fn cosine_similarity(left: &[Value], right: &[Value]) -> f64 {
    if left.is_empty() || right.is_empty() {
        return 0.0;
    }
    let mut total = 0.0;
    for (left_value, right_value) in left.iter().zip(right.iter()) {
        let (Some(left_float), Some(right_float)) =
            (python_float_opt(left_value), python_float_opt(right_value))
        else {
            continue;
        };
        total += left_float * right_float;
    }
    let capped = if total < 1.0 { total } else { 1.0 };
    if capped > 0.0 { capped } else { 0.0 }
}

/// A `Vec<f64>` as JSON numbers, so the cosine helper sees the shape Python sees. A value
/// JSON cannot carry (a `NaN` or an infinity) becomes `null`, which the loop skips.
fn floats_to_values(values: Vec<f64>) -> Vec<Value> {
    values
        .into_iter()
        .map(|value| serde_json::Number::from_f64(value).map_or(Value::Null, Value::Number))
        .collect()
}

/// Mirrors `local_text_vector` — the embedding pipeline, injected.
fn local_text_vector(text: &str, embed: &dyn Fn(&str) -> Vec<f64>) -> Vec<f64> {
    embed(text)
}

/// Mirrors `is_broad_file_query`.
pub fn is_broad_file_query(query: &str) -> bool {
    broad_query_pattern().is_match(query)
}

fn broad_query_pattern() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        Regex::new(
            r"(?i)(全文|全部|所有|整体|总结|概括|梳理|整理|分类|目录|大纲|这份|这个文件|附件|文档|试卷|题集|知识点|提取|summary|summarize|outline)",
        )
        .expect("the broad-query pattern compiles")
    })
}

/// Mirrors `hybrid_chunk_score`: keyword hits weigh 10, cosine 100, and the sum truncates.
pub fn hybrid_chunk_score(
    chunk: &Value,
    text: &str,
    tokens: &[String],
    query: &str,
    embed: &dyn Fn(&str) -> Vec<f64>,
) -> i64 {
    let keyword_score = score_chunk(text, tokens);
    let query_vector = floats_to_values(local_text_vector(query, embed));
    let chunk_vector = match chunk.get("vector") {
        Some(Value::Array(items)) => items.clone(),
        _ => floats_to_values(local_text_vector(text, embed)),
    };
    let vector_score = cosine_similarity(&query_vector, &chunk_vector);
    (keyword_score as f64 * 10.0 + vector_score * 100.0) as i64
}

/// Mirrors `format_chunk_locator`.
pub fn format_chunk_locator(
    chunk: &Value,
    chunk_index: i64,
    total_chunks: usize,
    start: i64,
    end: i64,
) -> String {
    let mut parts = vec![
        format!("片段 {chunk_index}/{total_chunks}"),
        format!("字符 {start}-{end}"),
    ];
    let line_start = python_int_opt(chunk.get("lineStart")).unwrap_or(0);
    let line_end = python_int_opt(chunk.get("lineEnd")).unwrap_or(0);
    if line_start > 0 && line_end >= line_start {
        parts.push(format!("行 {line_start}-{line_end}"));
    }
    parts.join("；")
}

/// The running state of `select_file_chunk_indices`'s inner `add`, which is a closure over
/// three locals in the oracle.
struct Chooser<'a> {
    chunks: &'a [Value],
    char_budget: i64,
    chosen: Vec<usize>,
    chosen_set: BTreeSet<usize>,
    used: i64,
}

impl Chooser<'_> {
    /// Mirrors `add`: `false` means "stop asking". A first pick is always admitted — the
    /// budget only binds once something is chosen — and an out-of-range or non-object index
    /// reports success without choosing anything.
    fn add(&mut self, index: i64) -> bool {
        if index < 0 || index as usize >= self.chunks.len() {
            return true;
        }
        let index = index as usize;
        if self.chosen_set.contains(&index) {
            return true;
        }
        let chunk = &self.chunks[index];
        if !chunk.is_object() {
            return true;
        }
        let text_len = char_len(&text_or_empty(chunk.get("text"))) as i64;
        if !self.chosen.is_empty()
            && (self.used + text_len > self.char_budget
                || self.chosen.len() >= FILE_CONTEXT_MAX_CHUNKS)
        {
            return false;
        }
        self.chosen.push(index);
        self.chosen_set.insert(index);
        self.used += text_len;
        true
    }

    fn full(&self) -> bool {
        self.chosen.len() >= FILE_CONTEXT_MAX_CHUNKS || self.used >= self.char_budget
    }
}

/// Mirrors `select_file_chunk_indices`.
///
/// The paths, in order: everything when the file is small enough; a spread from the top for a
/// broad query; the vector index's hits plus their neighbours; the scored hits plus theirs;
/// and, only if nothing at all was chosen, an even spread as a last resort.
pub fn select_file_chunk_indices(
    chunks: &[Value],
    query: &str,
    char_budget: i64,
    file_id: &str,
    project_id: &str,
    deps: &FileContextDeps,
) -> Vec<usize> {
    if chunks.is_empty() {
        return Vec::new();
    }

    let total_chars: i64 = chunks
        .iter()
        .filter(|chunk| chunk.is_object())
        .map(|chunk| char_len(&text_or_empty(chunk.get("text"))) as i64)
        .sum();
    if total_chars <= FILE_FULL_CONTEXT_LIMIT.min(char_budget) {
        return (0..chunks.len()).collect();
    }

    let tokens = query_tokens(query);
    let broad = is_broad_file_query(query);
    let mut scored: Vec<(i64, usize)> = Vec::new();
    for (index, chunk) in chunks.iter().enumerate() {
        if !chunk.is_object() {
            continue;
        }
        let text = text_or_empty(chunk.get("text"));
        let score = hybrid_chunk_score(chunk, &text, &tokens, query, deps.embed);
        if score > 0 {
            scored.push((score, index));
        }
    }
    scored.sort_unstable_by_key(|&(score, index)| (-score, index));

    let mut chooser = Chooser {
        chunks,
        char_budget,
        chosen: Vec::new(),
        chosen_set: BTreeSet::new(),
        used: 0,
    };

    if broad {
        chooser.add(0);
        let step = (chunks.len() / 6).max(1);
        let mut index = step;
        while index < chunks.len() {
            if !chooser.add(index as i64) {
                break;
            }
            index += step;
        }
    }

    if !file_id.is_empty() {
        for index in (deps.search_file_chunks)(file_id, project_id, query, FILE_CONTEXT_MAX_CHUNKS)
        {
            if chooser.full() {
                break;
            }
            chooser.add(index);
            chooser.add(index - 1);
            chooser.add(index + 1);
        }
    }

    for &(_, index) in &scored {
        if chooser.full() {
            break;
        }
        let index = index as i64;
        chooser.add(index);
        chooser.add(index - 1);
        chooser.add(index + 1);
    }

    if chooser.chosen.is_empty() {
        let step = (chunks.len() / FILE_CONTEXT_MAX_CHUNKS.min(chunks.len())).max(1);
        let mut index = 0usize;
        while index < chunks.len() {
            if !chooser.add(index as i64) {
                break;
            }
            index += step;
        }
    }

    let mut result = chooser.chosen;
    result.sort_unstable();
    result
}

/// Mirrors `format_cached_file_context`.
pub fn format_cached_file_context(
    index: usize,
    cached: &Value,
    query: &str,
    char_budget: i64,
    deps: &FileContextDeps,
) -> String {
    let name = name_or(cached.get("name"), || format!("附件 {index}"));
    let kind = name_or(cached.get("kind"), || "text".to_string());
    let char_count = python_int_opt(cached.get("charCount")).unwrap_or(0);
    let chunks: Vec<Value> = match cached.get("chunks") {
        Some(Value::Array(items)) => items.clone(),
        _ => Vec::new(),
    };

    let selected = select_file_chunk_indices(
        &chunks,
        query,
        char_budget,
        &text_or_empty(cached.get("id")),
        &text_or_empty(cached.get("projectId")),
        deps,
    );
    let selected_chunks: Vec<&Value> = selected
        .iter()
        .filter_map(|&position| chunks.get(position))
        .collect();

    let mut lines = vec![
        format!("--- 文件 {index}: {name} ({kind}) ---"),
        format!(
            "全文字符数：{char_count}；分块数：{}；本轮选取片段数：{}。",
            chunks.len(),
            selected_chunks.len()
        ),
    ];
    if selected_chunks.is_empty() {
        lines.push("[未找到可用文本片段]".to_string());
        return lines.join("\n");
    }

    let mut used: i64 = 0;
    for chunk in selected_chunks {
        let text = text_or_empty(chunk.get("text")).trim().to_string();
        if text.is_empty() {
            continue;
        }
        let remaining = char_budget - used;
        if remaining <= 0 {
            break;
        }
        let text = if char_len(&text) as i64 > remaining {
            char_take(&text, remaining as usize).trim_end().to_string()
        } else {
            text
        };
        used += char_len(&text) as i64;
        let chunk_index = python_int_opt(chunk.get("index")).unwrap_or(0) + 1;
        let start = python_int_opt(chunk.get("start")).unwrap_or(0);
        // `int(chunk.get("end") or start + len(text))`: a missing **or zero** end falls back
        // to the text's own length, so `0` is not treated as a position.
        let end = match chunk.get("end") {
            Some(found) if python_truthy(found) => python_int_opt(Some(found)).unwrap_or(0),
            _ => start + char_len(&text) as i64,
        };
        lines.push(format!(
            "\n[{}；引用ID F{index}-{chunk_index}]",
            format_chunk_locator(chunk, chunk_index, chunks.len(), start, end)
        ));
        lines.push(text);
    }

    lines.join("\n")
}

/// Mirrors `build_attachment_context`.
///
/// The budget is spent in attachment order and each attachment's share is
/// `min(remaining, max(8_000, remaining / left))` — which shrinks *multiplicatively*, so the
/// "not sent" row only appears once the floor is what binds. A failed index read becomes a
/// visible row rather than a dropped attachment, and the section's **code-point** length is
/// what the budget is charged.
pub fn build_attachment_context(
    attachments: &[Value],
    query: &str,
    deps: &FileContextDeps,
) -> String {
    let mut sections: Vec<String> = Vec::new();
    let mut remaining_budget = FILE_CONTEXT_CHAR_BUDGET;
    let valid: Vec<&Value> = attachments.iter().filter(|item| item.is_object()).collect();

    for (offset, attachment) in valid.iter().enumerate() {
        let position = offset + 1;
        if remaining_budget <= 0 {
            sections.push("[其余附件因上下文预算不足，本轮未发送。]".to_string());
            break;
        }

        let file_id = text_or_empty(attachment.get("fileId")).trim().to_string();
        let attachments_left = (valid.len() - position + 1).max(1) as i64;
        let per_file_budget =
            remaining_budget.min(PER_FILE_BUDGET_FLOOR.max(remaining_budget / attachments_left));

        if !file_id.is_empty() {
            let project_id = text_or_empty(attachment.get("projectId"))
                .trim()
                .to_string();
            let project_id = if project_id.is_empty() {
                None
            } else {
                Some(project_id.as_str())
            };
            match (deps.load_cached_file)(&file_id, project_id) {
                Ok(cached) => {
                    let section =
                        format_cached_file_context(position, &cached, query, per_file_budget, deps);
                    remaining_budget -= char_len(&section) as i64;
                    sections.push(section);
                }
                Err(error) => {
                    let name = name_or(attachment.get("name"), || file_id.clone());
                    sections.push(format!(
                        "--- 文件 {position}: {name} ---\n[文件索引读取失败：{}]",
                        error.message
                    ));
                }
            }
            continue;
        }

        let legacy_text = text_or_empty(attachment.get("text")).trim().to_string();
        if !legacy_text.is_empty() {
            let name = name_or(attachment.get("name"), || format!("附件 {position}"));
            let kind = name_or(attachment.get("kind"), || "text".to_string());
            let text = char_take(&legacy_text, per_file_budget.max(0) as usize);
            let suffix = if char_len(&legacy_text) > char_len(&text) {
                "\n[旧版附件内容较长，本轮只发送前半部分。建议重新上传以启用分块索引。]"
            } else {
                ""
            };
            let section = format!("--- 文件 {position}: {name} ({kind}) ---\n{text}{suffix}");
            remaining_budget -= char_len(&section) as i64;
            sections.push(section);
        }
    }

    if sections.is_empty() {
        return String::new();
    }

    let mut parts: Vec<String> = vec!["[用户上传文件上下文]".to_string()];
    if !deps.guard_line.is_empty() {
        parts.push(deps.guard_line.clone());
    }
    parts.push(
        "说明：文件全文已在本地后端分块索引中保存；本轮会按用户问题选取相关片段送入模型。回答时优先依据这些片段，若片段不足以支持结论，请明确指出需要更具体的问题或更多上下文。引用文件片段时请使用形如 [^F1-2] 的引用标记。".to_string(),
    );
    parts.extend(sections);
    parts.join("\n\n")
}

/// Mirrors `expanded_message_content`: the user's own text, then the attachment context.
pub fn expanded_message_content(message: &Value, deps: &FileContextDeps) -> String {
    let content = text_or_empty(message.get("content")).trim().to_string();
    let Some(Value::Array(attachments)) = message.get("attachments") else {
        return content;
    };
    if attachments.is_empty() {
        return content;
    }
    let attachment_context = build_attachment_context(attachments, &content, deps);
    if attachment_context.is_empty() {
        return content;
    }
    let lead = if content.is_empty() {
        "请根据附件内容回答。"
    } else {
        content.as_str()
    };
    format!("{lead}\n\n{attachment_context}").trim().to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    const GUARD: &str = "[防注入隔离] 测试锚点";

    fn no_search(_file: &str, _project: &str, _query: &str, _limit: usize) -> Vec<i64> {
        Vec::new()
    }

    fn no_index(file: &str, _project: Option<&str>) -> Result<Value, AppError> {
        Err(AppError {
            message: format!("no index for {file}"),
            code: crate::app_error::codes::FILE_INDEX_EXPIRED,
            status: 410,
        })
    }

    fn hits_chunk_five(_file: &str, _project: &str, _query: &str, limit: usize) -> Vec<i64> {
        vec![5].into_iter().take(limit).collect()
    }

    fn hash_embed(text: &str) -> Vec<f64> {
        hash_text_embedding(text, LOCAL_RAG_EMBEDDING_DIMENSIONS)
    }

    fn deps_with<'a>(
        load: LoadCachedFile<'a>,
        search: SearchFileChunks<'a>,
        guard: &str,
    ) -> FileContextDeps<'a> {
        FileContextDeps {
            load_cached_file: load,
            search_file_chunks: search,
            embed: &hash_embed,
            guard_line: guard.to_string(),
        }
    }

    fn nonzero(vector: &[f64]) -> Vec<(usize, f64)> {
        vector
            .iter()
            .enumerate()
            .filter(|(_, value)| **value != 0.0)
            .map(|(index, value)| (index, *value))
            .collect()
    }

    #[test]
    fn hash_embedding_matches_the_oracle() {
        // Anchors taken from the Python probe's own output. Both the unsigned 4-byte blake2b
        // digest — its length *is* part of the hash — and the sign bit and the `round(x, 6)`
        // normalisation have to agree, or the selector scores a different chunk order.
        assert_eq!(
            nonzero(&hash_text_embedding(
                "Mixed Case Text",
                LOCAL_RAG_EMBEDDING_DIMENSIONS
            )),
            vec![(11, -0.894427), (50, 0.447214)]
        );
        assert_eq!(
            nonzero(&hash_text_embedding(
                "关键词 关键词 关键词",
                LOCAL_RAG_EMBEDDING_DIMENSIONS
            )),
            vec![(1, -0.57735), (18, 0.57735), (21, -0.57735)]
        );
        assert_eq!(
            hash_text_embedding("Mixed Case Text", LOCAL_RAG_EMBEDDING_DIMENSIONS).len(),
            LOCAL_RAG_EMBEDDING_DIMENSIONS
        );
        // A single character does not survive `query_tokens`, so the vector stays zero rather
        // than hashing something.
        assert!(
            hash_text_embedding("a", LOCAL_RAG_EMBEDDING_DIMENSIONS)
                .iter()
                .all(|value| *value == 0.0)
        );
    }

    #[test]
    fn cosine_skips_unusable_values_and_keeps_the_bound_for_nan() {
        assert_eq!(cosine_similarity(&[], &[json!(1.0)]), 0.0);
        assert_eq!(cosine_similarity(&[json!(100.0)], &[json!(100.0)]), 1.0);
        assert_eq!(cosine_similarity(&[json!(-1.0)], &[json!(1.0)]), 0.0);
        // Python's `min`/`max` keep the bound when a comparison is false, so a NaN product
        // lands on 1.0 — `f64::clamp` would hand back NaN, which no JSON writer will take.
        assert_eq!(cosine_similarity(&[json!("nan")], &[json!(1.0)]), 1.0);
        // A pair `float()` rejects is skipped, not treated as zero.
        assert_eq!(
            cosine_similarity(&[json!(null), json!(1.0)], &[json!(2.0), json!(1.0)]),
            1.0
        );
    }

    #[test]
    fn the_budget_binds_only_after_something_is_chosen() {
        let chunks = vec![json!({"text": "x".repeat(5_000)})];
        let deps = deps_with(&no_index, &no_search, "");
        // Nothing scores and there is no index, so the last-resort spread picks the first
        // chunk even though the budget is already zero.
        assert_eq!(
            select_file_chunk_indices(&chunks, "无匹配", 0, "", "", &deps),
            vec![0]
        );
    }

    #[test]
    fn the_vector_index_contributes_its_hit_and_the_neighbours() {
        let chunks: Vec<Value> = (0..12)
            .map(|index| json!({"text": "z".repeat(6_000), "index": index}))
            .collect();
        let deps = deps_with(&no_index, &hits_chunk_five, "");
        let chosen = select_file_chunk_indices(
            &chunks,
            "无匹配",
            115_000,
            "f".repeat(32).as_str(),
            "",
            &deps,
        );
        assert!(chosen.contains(&4) && chosen.contains(&5) && chosen.contains(&6));
        assert!(chosen.len() <= FILE_CONTEXT_MAX_CHUNKS);
        assert_eq!(chosen, {
            let mut sorted = chosen.clone();
            sorted.sort_unstable();
            sorted
        });
    }

    #[test]
    fn a_failed_index_read_is_a_visible_row() {
        let deps = deps_with(&no_index, &no_search, "");
        let context = build_attachment_context(
            &[json!({"fileId": "a".repeat(32), "name": "gone"})],
            "q",
            &deps,
        );
        assert!(context.contains("--- 文件 1: gone ---"), "{context}");
        assert!(
            context.contains("[文件索引读取失败：no index for "),
            "{context}"
        );
    }

    #[test]
    fn the_legacy_suffix_appears_only_when_the_text_is_cut() {
        let deps = deps_with(&no_index, &no_search, "");
        let short = build_attachment_context(&[json!({"text": "短"})], "q", &deps);
        assert!(!short.contains("旧版附件内容较长"), "{short}");
        let cut = build_attachment_context(&[json!({"text": "x".repeat(120_000)})], "q", &deps);
        assert!(cut.contains("旧版附件内容较长"), "{}", &cut[..200]);
    }

    #[test]
    fn the_budget_runs_out_once_the_floor_is_what_binds() {
        let deps = deps_with(&no_index, &no_search, "");
        // Each attachment's share is `min(remaining, max(8_000, remaining / left))`, which
        // shrinks multiplicatively — so the tail of a long list never quite reaches zero and
        // the "not sent" row needs enough rows for the 8_000 floor to be the binding term.
        let many: Vec<Value> = (0..20)
            .map(|_| json!({"text": "x".repeat(20_000)}))
            .collect();
        let context = build_attachment_context(&many, "q", &deps);
        assert!(
            context.contains("[其余附件因上下文预算不足，本轮未发送。]"),
            "len={}",
            context.chars().count()
        );
    }

    #[test]
    fn an_empty_attachment_list_yields_no_context_at_all() {
        let deps = deps_with(&no_index, &no_search, "");
        assert_eq!(build_attachment_context(&[], "q", &deps), "");
        // Non-objects are filtered before anything is charged to the budget.
        assert_eq!(
            build_attachment_context(&[json!("x"), json!(5)], "q", &deps),
            ""
        );
    }

    #[test]
    fn the_header_loses_a_row_without_the_guard_line() {
        let with = build_attachment_context(
            &[json!({"text": "一"})],
            "q",
            &deps_with(&no_index, &no_search, GUARD),
        );
        let without = build_attachment_context(
            &[json!({"text": "一"})],
            "q",
            &deps_with(&no_index, &no_search, ""),
        );
        assert!(with.starts_with(&format!("[用户上传文件上下文]\n\n{GUARD}\n\n说明：")));
        assert!(without.starts_with("[用户上传文件上下文]\n\n说明："));
    }

    #[test]
    fn expanded_content_falls_back_to_the_placeholder_only_when_it_has_to() {
        let deps = deps_with(&no_index, &no_search, "");
        assert_eq!(
            expanded_message_content(&json!({"content": "  你好  "}), &deps),
            "你好"
        );
        // Nothing to expand: the user's own text survives unchanged.
        assert_eq!(
            expanded_message_content(&json!({"content": "问题", "attachments": ["x"]}), &deps),
            "问题"
        );
        let expanded = expanded_message_content(
            &json!({"content": "", "attachments": [{"text": "旧版"}]}),
            &deps,
        );
        assert!(expanded.starts_with("请根据附件内容回答。\n\n[用户上传文件上下文]"));
    }

    #[test]
    fn a_zero_end_is_a_missing_position_rather_than_position_zero() {
        // `int(chunk.get("end") or start + len(text))`: zero is falsy in Python, so the
        // locator falls back to the text's own length.
        let deps = deps_with(&no_index, &no_search, "");
        let cached = json!({
            "name": "n",
            "chunks": [{"text": "abcdef", "index": 0, "start": 10, "end": 0}],
        });
        let rendered = format_cached_file_context(1, &cached, "q", 1_000, &deps);
        assert!(rendered.contains("字符 10-16"), "{rendered}");
    }

    #[test]
    fn broad_queries_and_locators_keep_their_spellings() {
        assert!(!is_broad_file_query(""));
        assert!(is_broad_file_query("全文总结"));
        assert!(is_broad_file_query("SUMMARIZE this"));
        assert!(!is_broad_file_query("随便聊聊"));
        assert_eq!(
            format_chunk_locator(&json!({"lineStart": 3, "lineEnd": 9}), 1, 4, 0, 100),
            "片段 1/4；字符 0-100；行 3-9"
        );
        // A line range that ends before it starts is dropped, and so is a zero start.
        assert_eq!(
            format_chunk_locator(&json!({"lineStart": 5, "lineEnd": 4}), 3, 4, 20, 30),
            "片段 3/4；字符 20-30"
        );
    }
}
