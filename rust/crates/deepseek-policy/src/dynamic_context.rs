//! Per-turn dynamic context assembly — the consumer of `searchContext`.
//!
//! Mirrors `build_dynamic_turn_context` and `append_context_to_latest_user` in
//! `gateway/deepseek_client.py`: the block that carries this turn's volatile state (the
//! clock, the compressed summary, memory, the search hint, the slides guidance, the
//! hardened search context) and the trailing system message that delivers it.
//!
//! **Why this is the slice that closes the search loop.** `harden_search_context` produces
//! a string that has nowhere to go until something reads `payload["searchContext"]` — this
//! is that reader. Order matters and is the whole point of the design: the search context
//! is appended *after* the stable prefixes so that flipping search on and off does not
//! invalidate the prompt cache behind it.
//!
//! **The clock is injected, not read.** The oracle calls `datetime.now().astimezone()` and
//! renders the machine's local zone; Rust's standard library has no local-timezone support
//! and this workspace has no time crate, so [`LocalNow`] carries the instant, the offset
//! and the zone name, and the caller resolves them. That follows the two precedents already
//! in this crate — `utc_now_iso(epoch_seconds)` ("the clock is a parameter so callers can
//! pin it") and the injected search transport. Resolving the OS zone is a wiring concern
//! that is **not** implemented here, and deliberately so: faking it would be worse than
//! leaving it visible.
//!
//! Two spellings of the same instant coexist in this module and must not be unified:
//! `format_current_time_context` renders UTC as `…Z`, while `core_utils::utc_now_iso`
//! renders `…+00:00`. The oracle replaces the suffix in exactly one of the two places.

use std::sync::OnceLock;

use regex::Regex;
use serde_json::{Value, json};

use crate::context_taint::PER_TURN_CONTEXT_MARKER;
use crate::core_utils::{isoformat_seconds, latest_user_query, python_truthy};
use crate::python_json::value_str;
use crate::search::search_tool_enabled;

// --- Constants ------------------------------------------------------------------------

pub const CURRENT_TIME_CONTEXT_HEADER: &str = "[Current time]";

/// `settings.context.summary_max_chars`, whose default is 12 000.
pub const CONTEXT_SUMMARY_MAX_CHARS: usize = 12_000;

/// The turn-level search hint. Note it is appended only when search is *enabled* this turn,
/// which is what keeps the search switch out of the cacheable prefix.
pub const WEB_SEARCH_SYSTEM_HINT: &str = concat!(
    "If web search is available, decide whether to call web_search before answering. ",
    "For current facts, prices, releases, documentation, citations, product comparisons, or uncertain external claims, search first. ",
    "If the available results are enough, do not keep searching; when a key fact is still missing, call web_search at most once more with a refined query. ",
    "Cite web search results with the exact [^Wn] markers provided by web_search or the per-turn search context. ",
    "Do not invent citation ids or use free-form labels like [Source] or [Reddit]. ",
    "Cite uploaded files with the existing [^Fn-m] markers."
);

pub const SLIDES_SKILL_NAME: &str = "slides";

/// Transcribed from `slides_skill.py`. The newlines are written as explicit `\n` escapes
/// through `concat!` rather than as a multi-line raw string: a raw string would take its
/// line endings from the source file, so a checkout with CRLF would silently change every
/// prompt byte this constant feeds.
pub const SLIDES_SKILL_REFERENCE: &str = concat!(
    "---\n",
    "name: slides\n",
    "description: Build polished, editable PowerPoint decks via the create_pptx tool. Use when creating, editing, or polishing presentations, slide decks, or visual summaries.\n",
    "---\n",
    "\n",
    "# Slides Skill\n",
    "\n",
    "Reference for serious, high-polish decks. \"Clean\" is not the bar — the target is\n",
    "an editable deck that reads like a strong editor, analyst, and designer built it\n",
    "together. Reject \"serviceable\": if a deck looks like a generic SaaS dashboard, a\n",
    "consulting card grid, or a template with the subject name swapped in, keep\n",
    "sharpening it.\n",
    "\n",
    "## North star: win the contact-sheet test\n",
    "\n",
    "Picture the whole deck shrunk to thumbnails on one contact sheet. It should show a\n",
    "coherent visual system, varied slide rhythms, and evidence-led storytelling. At\n",
    "readable size, every slide must carry a claim, one proof object, and no filler.\n",
    "\n",
    "## Every slide is a claim\n",
    "\n",
    "Before choosing a layout, write each slide as a claim:\n",
    "- claim title — a conclusion, not a topic label. It must fail the noun-swap test:\n",
    "  if another subject could be dropped in and the title still works, sharpen it.\n",
    "  - Weak: \"Revenue and margin trends\" -> Strong: \"Growth slowed, but the margin engine kept expanding.\"\n",
    "  - Weak: \"Expansion drivers\" -> Strong: \"Backlog is compounding faster than revenue.\"\n",
    "- one dominant proof object — the single most convincing structure for that claim\n",
    "  (a comparison, a process, a ranked set of cards, a thesis line). One per slide.\n",
    "- a short support note — concise, factual, specific. Numbers beat adjectives.\n",
    "\n",
    "## The renderer owns the visual system\n",
    "\n",
    "create_pptx already applies a coherent system: one deterministic accent theme, an\n",
    "accent eyebrow plus a near-black claim title on every slide, open hairline\n",
    "composition (no boxed cards), and an auto cover / agenda / page numbers. Do not ask\n",
    "for fonts, colors, charts, images, or logos — the tool renders none of those. Your\n",
    "job is the content and the structure, so spend the effort there. Convey brand\n",
    "through the wording and the claim itself, never a fabricated logo or mascot.\n",
    "\n",
    "## Contact-sheet rhythm\n",
    "\n",
    "Vary the layout so the deck looks authored, not generated:\n",
    "- across ~10 slides use several different layouts, not the same one repeated\n",
    "- never let 3 consecutive slides share the same layout\n",
    "- match the layout to each slide's job; don't default everything to plain bullets\n",
    "\n",
    "## Blocking anti-patterns (fix before delivering)\n",
    "\n",
    "- title states a topic instead of a conclusion\n",
    "- a proof too thin for the claim, or one slide trying to make several points at once\n",
    "- bullets exist only to fill space; equal-role items are uneven or padded out\n",
    "- every content slide uses the same layout, so the contact sheet reads as a template pack\n",
    "- a bullet has no \"lead: detail\" split, so it renders as one flat line with no hierarchy\n",
    "\n",
    "## Pre-flight quality bar\n",
    "\n",
    "Self-score the outline on story arc, specificity (noun-swap), rhythm, restraint,\n",
    "precision, and coherence. If any is weak, rebuild the weakest slides — sharpen\n",
    "titles, rebalance layouts, cut filler — before generating. Do not ship just\n",
    "because a file would export."
);

pub const SLIDES_RUNTIME_GUIDANCE: &str = concat!(
    "DeepSeek Infra runtime routing:\n",
    "- This app builds decks through ONE boundary: the `create_pptx` function tool. It renders a real, editable, downloadable 16:9 `.pptx` with python-pptx. There is no artifact-tool, imagegen, headless renderer, or shell/script step here — never claim those, and never answer a PPT request with only an outline, Marp, or Markdown slides.\n",
    "- Always call `create_pptx` for any request to create / edit / export / polish a PPT / slides / deck / presentation. Pass a `title`, an optional `subtitle`, and an ordered `slides` array of {title, bullets[], layout}.\n",
    "- Apply the slides-skill quality bar through the fields you control:\n",
    "  - title — write it as a claim (a conclusion), not a topic; run the noun-swap test.\n",
    "  - bullets — 3-6 tight, specific items; numbers over adjectives; no filler lines. Write each as \"lead: detail\" (split on a colon `：`/`:` or a dash `-`/`—`): in every layout the lead renders bold and the detail muted, so each point shows a point AND its proof. A bullet with no split renders as one flat line — always give it a proof.\n",
    "  - layout — pick the ONE structure that best proves the claim: `cards` for a set of key points, `process` or `timeline` for ordered steps, `comparison` for tradeoffs or A-vs-B, `quote` for a single thesis or section moment, `summary` for closing takeaways, `bullets` only when no structure fits. Use `auto` to let the tool infer from the title.\n",
    "- Compose for contact-sheet rhythm: vary layouts across the deck and avoid 3 same-layout slides in a row. The tool auto-adds a themed cover, an agenda (for 4+ slides), an accent eyebrow plus a bold claim title on each slide, page numbers, and a deterministic color theme — you do not set those, so spend your effort on claim titles, evidence, and layout variety.\n",
    "- Default to a 6-10 slide deck when length is unspecified. If the user supplies source material, convert it into editable slide content, not a prose summary.\n",
    "- After the tool returns, surface the result: state the title and slide count, walk the returned `outline` page by page (title + key points), and hand over the download as a Markdown link such as [下载 PPT](downloadUrl) (valid ~6 hours)."
);

// --- The injected environment ---------------------------------------------------------

/// The instant plus how the machine renders its local zone.
///
/// `timezone_name` is what Python's `tzname()` returns for the real zone (for example
/// `China Standard Time` or `UTC+08:00` for a bare offset). The oracle's fallback chain is
/// `tzname() or str(tzinfo) or "local"`; for every real zone `tzname()` is non-empty, so
/// the middle arm is unreachable in practice and an empty name here falls straight through
/// to `local`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LocalNow {
    pub epoch_seconds: i64,
    pub offset_seconds: i32,
    pub timezone_name: String,
}

/// What the oracle reads from module globals when it assembles the turn context: the clock
/// and the summary cap.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DynamicContextEnv {
    pub now: LocalNow,
    pub summary_max_chars: usize,
}

impl DynamicContextEnv {
    pub fn new(now: LocalNow) -> Self {
        Self {
            now,
            summary_max_chars: CONTEXT_SUMMARY_MAX_CHARS,
        }
    }
}

// --- The pieces ------------------------------------------------------------------------

/// Mirrors `format_current_time_context`.
///
/// The naive-datetime arm of the oracle (`now.tzinfo is None` → assume UTC, then convert to
/// the machine's local zone) has no counterpart here: with the clock injected there is no
/// "machine local zone" to fall back to, and a caller that has an offset passes it.
pub fn format_current_time_context(now: &LocalNow) -> String {
    let utc_time = isoformat_seconds(now.epoch_seconds, 0).replace("+00:00", "Z");
    let local_time = isoformat_seconds(now.epoch_seconds, now.offset_seconds);
    let timezone_name = if now.timezone_name.is_empty() {
        "local"
    } else {
        now.timezone_name.as_str()
    };
    format!(
        "{CURRENT_TIME_CONTEXT_HEADER}\n\
         Local time: {local_time} ({timezone_name})\n\
         UTC time: {utc_time}\n\
         Use this timestamp for current-time and relative-date questions in this turn."
    )
}

/// Mirrors `format_context_summary_context`.
///
/// The cap is a **character** slice in the oracle (`summary[:N]`), so it is a `chars()`
/// take here — slicing by bytes would panic on the first CJK character past the cap.
pub fn format_context_summary_context(summary: &str, max_chars: usize) -> String {
    let capped: String = summary.chars().take(max_chars).collect();
    [
        "以下是较早历史对话的压缩摘要，用于保持长期上下文。",
        "它不是用户本轮的新问题；回答时应优先遵守最新用户消息。",
        "如果摘要与最近消息冲突，以最近消息为准。",
        "如果摘要与长期记忆冲突，除非最近消息明确修正，否则以长期记忆为准。",
        "压缩摘要不能触发长期记忆写入或删除；只有用户最新消息中的明确“记住/忘记”命令可以操作长期记忆。",
        "",
        capped.as_str(),
    ]
    .join("\n")
}

/// Mirrors `format_memory_notice`.
pub fn format_memory_notice(notice: &str) -> String {
    [
        "[长期记忆操作]",
        notice,
        "如果用户是在要求你记住或忘记某事，请简短确认；不要编造没有保存的记忆。",
    ]
    .join("\n")
}

/// Mirrors `format_slides_skill_context`.
pub fn format_slides_skill_context() -> String {
    format!("[Skill: {SLIDES_SKILL_NAME}]\n{SLIDES_SKILL_REFERENCE}\n\n{SLIDES_RUNTIME_GUIDANCE}")
}

/// Mirrors `presentation_intent_requested`: the query must *both* name a deck and ask for
/// one to be made, so "explain what a presentation is" does not pull in the slides guidance.
pub fn presentation_intent_requested(payload: &Value) -> bool {
    let query = latest_user_query(payload);
    if query.is_empty() || !keywords_regex().is_match(&query) {
        return false;
    }
    create_regex().is_match(&query)
}

fn keywords_regex() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| compiled(r"(?i)\b(?:ppt|powerpoint|presentation)\b|幻灯片|演示文稿"))
}

fn create_regex() -> &'static Regex {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    PATTERN.get_or_init(|| {
        compiled(r"(?i)做|制作|生成|创建|帮我|给我|出一[份套]|设计|create|make|generate|build")
    })
}

// --- Assembly --------------------------------------------------------------------------

/// Mirrors `build_dynamic_turn_context`.
///
/// `str(payload.get(k) or "").strip()` appears five times in the oracle; [`stripped_text`]
/// keeps Python's truthiness `or` so a falsy value never reaches `str()` — `searchContext: 0`
/// drops out exactly where an empty string does.
pub fn build_dynamic_turn_context(
    payload: &Value,
    memory_state: &Value,
    tools_enabled: bool,
    env: &DynamicContextEnv,
) -> String {
    let mut dynamic_parts: Vec<String> = vec![format_current_time_context(&env.now)];

    let context_summary = stripped_text(payload.get("contextSummary"));
    if !context_summary.is_empty() {
        dynamic_parts.push(format_context_summary_context(
            &context_summary,
            env.summary_max_chars,
        ));
    }

    let memory_context = stripped_text(memory_state.get("context"));
    if !memory_context.is_empty() {
        dynamic_parts.push(memory_context);
    }

    let memory_notice = stripped_text(memory_state.get("notice"));
    if !memory_notice.is_empty() {
        dynamic_parts.push(format_memory_notice(&memory_notice));
    }

    // Search availability is a per-turn switch, not part of the stable prefix. It sits at
    // the tail so that turning search off and on again does not invalidate everything after
    // the system prompt.
    if tools_enabled && search_tool_enabled(payload) {
        dynamic_parts.push(WEB_SEARCH_SYSTEM_HINT.to_string());
    }

    if tools_enabled && presentation_intent_requested(payload) {
        dynamic_parts.push(format_slides_skill_context());
    }

    let search_context = stripped_text(payload.get("searchContext"));
    if !search_context.is_empty() {
        dynamic_parts.push(search_context);
    }

    let continuation_context = stripped_text(payload.get("continuationContext"));
    if !continuation_context.is_empty() {
        dynamic_parts.push(continuation_context);
    }

    // Unreachable today: the clock is always the first part. Kept because it is the
    // oracle's guard and the ordering of the two `join`s would otherwise be untestable.
    if dynamic_parts.is_empty() {
        return String::new();
    }

    let mut all: Vec<String> = Vec::with_capacity(dynamic_parts.len() + 1);
    all.push(PER_TURN_CONTEXT_MARKER.to_string());
    all.extend(dynamic_parts);
    all.join("\n\n")
}

/// Mirrors `append_context_to_latest_user`.
///
/// The trailing system message is what keeps the history byte-stable across turns: the
/// per-turn block rides at the end, so every earlier user/assistant message keeps hitting
/// the prompt cache.
///
/// **Recorded landmine for the request-assembly slice.** The oracle appends
/// `{"role": …, "content": …}` in that insertion order, and its body is serialized in
/// insertion order. `serde_json::Map` is a `BTreeMap` here, so `json!` emits `content`
/// before `role`; a body builder that serializes this message must not let `json!` decide,
/// or the injected message's bytes will differ from the oracle's.
pub fn append_context_to_latest_user(messages: &[Value], dynamic_context: &str) -> Vec<Value> {
    if dynamic_context.is_empty() {
        return messages.to_vec();
    }
    let mut result = messages.to_vec();
    result.push(json!({"role": "system", "content": dynamic_context}));
    result
}

// --- Helpers --------------------------------------------------------------------------

/// `str(value or "").strip()` — Python truthiness first, then `str()`, then strip.
fn stripped_text(value: Option<&Value>) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found).trim().to_string(),
        _ => String::new(),
    }
}

fn compiled(pattern: &str) -> Regex {
    Regex::new(pattern).expect("static pattern must compile")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::core_utils::utc_now_iso;

    /// 2025-09-17T08:04:28Z, rendered by a UTC+08:00 zone.
    const EPOCH: i64 = 1_758_096_268;
    const OFFSET: i32 = 28_800;

    fn anchor() -> LocalNow {
        LocalNow {
            epoch_seconds: EPOCH,
            offset_seconds: OFFSET,
            timezone_name: "China Standard Time".to_string(),
        }
    }

    fn env() -> DynamicContextEnv {
        DynamicContextEnv::new(anchor())
    }

    #[test]
    fn the_time_block_spells_utc_with_z_and_the_local_line_with_its_offset() {
        let text = format_current_time_context(&anchor());
        assert!(text.starts_with(CURRENT_TIME_CONTEXT_HEADER));
        assert!(text.contains("Local time: 2025-09-17T16:04:28+08:00 (China Standard Time)"));
        assert!(text.contains("UTC time: 2025-09-17T08:04:28Z"));
        // The same instant through the other helper keeps "+00:00": the two spellings
        // coexist on purpose and must not be unified.
        assert_eq!(utc_now_iso(EPOCH), "2025-09-17T08:04:28+00:00".to_string());
    }

    #[test]
    fn an_empty_zone_name_falls_back_to_local() {
        let now = LocalNow {
            timezone_name: String::new(),
            ..anchor()
        };
        assert!(format_current_time_context(&now).contains("(local)"));
    }

    #[test]
    fn the_search_switch_does_not_disturb_what_precedes_it() {
        // The reason the hint lives in the per-turn block at all: flipping search on and
        // off must leave everything before it byte-identical, or the prompt cache behind
        // it is invalidated for the whole turn.
        let on = build_dynamic_turn_context(
            &json!({"searchEnabled": true, "searchMode": "on"}),
            &json!({}),
            true,
            &env(),
        );
        let off = build_dynamic_turn_context(
            &json!({"searchEnabled": true, "searchMode": "off"}),
            &json!({}),
            true,
            &env(),
        );
        assert_eq!(on, format!("{off}\n\n{WEB_SEARCH_SYSTEM_HINT}"));
    }

    #[test]
    fn the_search_context_joins_after_the_search_hint() {
        let block = build_dynamic_turn_context(
            &json!({"searchEnabled": true, "searchMode": "on", "searchContext": "hits"}),
            &json!({}),
            true,
            &env(),
        );
        let hint = block.find(WEB_SEARCH_SYSTEM_HINT).expect("hint present");
        let context = block.find("hits").expect("context present");
        assert!(hint < context);
        assert!(block.starts_with(PER_TURN_CONTEXT_MARKER));
    }

    #[test]
    fn a_falsy_search_context_is_dropped_exactly_like_a_missing_one() {
        let bare = build_dynamic_turn_context(&json!({}), &json!({}), true, &env());
        for falsy in [json!(0), json!(""), json!(null), json!([]), json!({})] {
            let block = build_dynamic_turn_context(
                &json!({"searchContext": falsy}),
                &json!({}),
                true,
                &env(),
            );
            assert_eq!(block, bare);
        }
    }

    #[test]
    fn a_truthy_non_string_context_is_rendered_the_way_python_renders_it() {
        // `str([1, 2])` has the space after the comma; JSON would not.
        let block =
            build_dynamic_turn_context(&json!({"searchContext": [1, 2]}), &json!({}), true, &env());
        assert!(block.ends_with("[1, 2]"), "{block}");
    }

    #[test]
    fn surrounding_whitespace_is_stripped_from_every_spliced_value() {
        let block = build_dynamic_turn_context(
            &json!({"searchContext": "  hits  ", "continuationContext": "  more  "}),
            &json!({}),
            true,
            &env(),
        );
        assert!(block.ends_with("hits\n\nmore"), "{block}");
    }

    #[test]
    fn the_summary_cap_counts_characters_not_bytes() {
        let summary = "摘".repeat(30);
        let text = format_context_summary_context(&summary, 4);
        assert!(text.ends_with("摘摘摘摘"));
        assert!(!text.ends_with("摘摘摘摘摘"));
    }

    #[test]
    fn memory_pieces_keep_the_oracles_order_after_the_summary() {
        let block = build_dynamic_turn_context(
            &json!({"contextSummary": "s"}),
            &json!({"context": "mem", "notice": "saved"}),
            true,
            &env(),
        );
        let summary = block.find("较早历史对话").expect("summary");
        let memory = block.find("mem").expect("memory");
        let notice = block.find("[长期记忆操作]").expect("notice");
        assert!(summary < memory && memory < notice);
    }

    #[test]
    fn the_slides_block_needs_a_keyword_and_a_create_verb() {
        let deck = json!({"messages": [{"role": "user", "content": "帮我做一份 PPT"}]});
        let block = build_dynamic_turn_context(&deck, &json!({}), true, &env());
        assert!(block.contains(&format_slides_skill_context()));

        // Naming a presentation is not asking for one, so the guidance stays out.
        let ask = json!({"messages": [{"role": "user", "content": "什么是 presentation？"}]});
        let block = build_dynamic_turn_context(&ask, &json!({}), true, &env());
        assert!(!block.contains("[Skill: slides]"));
    }

    #[test]
    fn disabling_tools_gates_the_hint_and_the_slides_block_but_not_the_context() {
        let payload = json!({
            "searchEnabled": true,
            "searchContext": "ctx",
            "messages": [{"role": "user", "content": "做 PPT"}],
        });
        let block = build_dynamic_turn_context(&payload, &json!({}), false, &env());
        assert!(!block.contains(WEB_SEARCH_SYSTEM_HINT));
        assert!(!block.contains("[Skill: slides]"));
        assert!(block.ends_with("ctx"), "{block}");
    }

    #[test]
    fn appending_an_empty_context_leaves_the_history_untouched() {
        let messages = vec![json!({"role": "user", "content": "hi"})];
        assert_eq!(append_context_to_latest_user(&messages, ""), messages);
    }

    #[test]
    fn the_per_turn_block_arrives_as_a_trailing_system_message() {
        let messages = vec![json!({"role": "user", "content": "hi"})];
        let appended = append_context_to_latest_user(&messages, "ctx");
        assert_eq!(appended.len(), 2);
        assert_eq!(appended[1]["role"], json!("system"));
        assert_eq!(appended[1]["content"], json!("ctx"));
        assert_eq!(appended[0], messages[0]);
    }

    #[test]
    fn the_memory_notice_is_three_lines_in_the_oracles_order() {
        assert_eq!(
            format_memory_notice("已保存"),
            "[长期记忆操作]\n已保存\n如果用户是在要求你记住或忘记某事，请简短确认；不要编造没有保存的记忆。"
        );
    }
}
