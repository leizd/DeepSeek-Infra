//! Tool dispatch — the executor seam, mirroring `execute_tool_call` in
//! `deepseek_infra/infra/tool_runtime/tools.py`.
//!
//! # What this is
//!
//! The oracle funnels every model tool call through one function that (1) gates
//! it, (2) routes it to one of 17 local branches or the `browser_*` family, and
//! (3) wraps the result in a fixed envelope. This module ports the *seam*: the
//! envelope contract, the argument/limit/name normalization the branches rely on,
//! the branch inventory, and the ordering — gate before branch.
//!
//! # What this is not
//!
//! **Only four of the eighteen branches are implemented** — `generate_chart`,
//! `data_transform`, `web_search` and `compare_search_results`, the four with no
//! external package behind them. The rest are marked [`Branch::is_ported`] `false`
//! and can never produce output. That is deliberate: [`DispatchOutcome::Unported`]
//! has no envelope at all, so a branch that has not been ported cannot be mistaken
//! for one that ran. Nothing in this module is wired to a route.
//!
//! The remaining branches are not blocked on this module's shape. They are
//! blocked on packages: `search` / `rag`, `data` (projects, reminders, memory),
//! `media` (presentations, mindmaps, documents), `browser`, and — for
//! `python_eval` — a real sandbox, since the oracle shells out to a Python
//! interpreter and the migrated runtime must not.
//!
//! # Two orderings that are load-bearing
//!
//! 1. **The gate runs before the branch**, and a denial short-circuits with
//!    `ToolPolicy::denial_output` — the branch never runs.
//! 2. **The unknown-tool fallback is only reachable without a policy.** With a
//!    policy attached, an unregistered name is denied as `unknown_tool` first, so
//!    the `Unsupported tool:` envelope is a no-policy path only. Reaching it with
//!    a policy would mean the gate had been skipped.

use serde_json::{Map, Value, json};

use crate::tool_policy::{ToolPolicy, ToolPolicyDecision};

/// `ErrorCode.INVALID_PAYLOAD`.
pub const INVALID_PAYLOAD: &str = "invalid_payload";
/// `ErrorCode.INTERNAL`.
pub const INTERNAL: &str = "internal";

/// `MAX_TOOL_CALLS_PER_RESPONSE` — how many calls of one model response are run.
///
/// Defined in `tools.py` alongside the dispatcher, not in the policy module,
/// because it bounds *execution* rather than gating.
pub const MAX_TOOL_CALLS_PER_RESPONSE: usize = 6;

/// Tools that must not run in the parallel batch, mirroring `SERIAL_TOOL_NAMES`.
///
/// They either mutate shared state (`create_reminder`, `forget_memory`,
/// `suggest_memory`) or depend on a single-flight upstream (`web_search`,
/// `compare_search_results`).
pub const SERIAL_TOOL_NAMES: [&str; 5] = [
    "create_reminder",
    "forget_memory",
    "suggest_memory",
    "web_search",
    "compare_search_results",
];

// --- normalization helpers -------------------------------------------------------

/// Mirrors `tool_call_name`: `function.name`, else a top-level `name`, trimmed.
pub fn tool_call_name(tool_call: &Value) -> String {
    let from_function = tool_call
        .get("function")
        .and_then(Value::as_object)
        .and_then(|function| function.get("name"))
        .and_then(Value::as_str);
    let from_top = tool_call.get("name").and_then(Value::as_str);
    let chosen = from_function
        .filter(|name| !name.is_empty())
        .or_else(|| from_top.filter(|name| !name.is_empty()))
        .unwrap_or("");
    chosen.trim().to_string()
}

/// Mirrors `parse_tool_arguments`.
///
/// A dict passes through, a JSON object string is parsed, and everything else —
/// including malformed JSON, a non-object, or a blank string — becomes empty.
///
/// Divergence worth noting: Python's `json.loads` accepts the non-standard
/// `NaN` / `Infinity` literals by default; `serde_json` rejects them, so such a
/// string is parsed as empty here rather than as a value.
pub fn parse_tool_arguments(value: Option<&Value>) -> Map<String, Value> {
    match value {
        Some(Value::Object(fields)) => fields.clone(),
        Some(Value::String(text)) => {
            if text.trim().is_empty() {
                return Map::new();
            }
            match serde_json::from_str::<Value>(text) {
                Ok(Value::Object(fields)) => fields,
                _ => Map::new(),
            }
        }
        _ => Map::new(),
    }
}

/// Mirrors `safe_limit`: `max(1, min(int(value), maximum))`, or `default` when the
/// value is not integer-like.
///
/// Python's `int()` truncates toward zero for a float, but for a **string** it
/// requires an integer literal — `int("7.9")` raises `ValueError` and falls back,
/// where `int(7.9)` truncates to `7`. The two paths are therefore handled
/// separately. `None`, containers and objects raise `TypeError` and also fall
/// back.
pub fn safe_limit(value: Option<&Value>, default: i64, maximum: i64) -> i64 {
    let parsed = match value {
        Some(Value::Number(number)) => number.as_f64(),
        Some(Value::String(text)) => text.trim().parse::<i64>().ok().map(|value| value as f64),
        Some(Value::Bool(flag)) => Some(if *flag { 1.0 } else { 0.0 }),
        _ => None,
    };
    let Some(parsed) = parsed else {
        return default;
    };
    if !parsed.is_finite() {
        return default;
    }
    // `int()` truncates toward zero.
    let truncated = parsed.trunc();
    if truncated > i64::MAX as f64 || truncated < i64::MIN as f64 {
        return maximum.max(1);
    }
    // `max(1, min(value, maximum))`. `clamp` needs a non-inverted range, so the
    // upper bound is lifted to 1 — which is also what Python produces when
    // `maximum < 1`, since `min(v, 0)` can never exceed the floor of 1.
    truncated.clamp(1.0, (maximum as f64).max(1.0)) as i64
}

/// Mirrors `is_parallel_safe_tool`.
pub fn is_parallel_safe_tool(tool_call: &Value) -> bool {
    let name = tool_call_name(tool_call);
    !name.is_empty() && !SERIAL_TOOL_NAMES.contains(&name.as_str())
}

/// Python's `str(float)` — the shortest round-tripping form, with a decimal point
/// for integral values and a signed, zero-padded exponent outside `1e-4..1e16`.
///
/// Rust's `Display` for `f64` prints `1` where Python prints `1.0`, and never
/// switches to exponent form, so the values interpolated into chart markdown
/// would otherwise differ.
pub fn python_float_str(value: f64) -> String {
    crate::python_json::float_str(value)
}

/// Python's `s[:n]` — a truncation by **code point**, not by byte.
fn python_truncate(text: &str, limit: usize) -> String {
    text.chars().take(limit).collect()
}

/// Python's `str(value or fallback)` for the narrow case of a text field.
///
/// Public because the branch modules stringify arguments the same way.
pub fn python_str_or(value: Option<&Value>, fallback: &str) -> String {
    match value {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        Some(Value::Number(number)) => number.to_string(),
        Some(Value::Bool(flag)) => {
            if *flag {
                "True".to_string()
            } else {
                fallback.to_string()
            }
        }
        _ => fallback.to_string(),
    }
}

// --- the branch inventory --------------------------------------------------------

/// One dispatch branch of `execute_tool_call`.
///
/// The inventory is exhaustive on purpose: a tool that has no branch must be a
/// visible gap rather than a silent fall-through, and the migration matrix reads
/// its ported/unported state from here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Branch {
    BrowserFamily,
    PythonEval,
    SearchFiles,
    FetchUrl,
    WebSearch,
    CompareSearchResults,
    SuggestMemory,
    CreateReminder,
    ListReminders,
    RecallMemory,
    ForgetMemory,
    ListProjectFiles,
    ReadFileChunk,
    DataTransform,
    GenerateChart,
    CreateMindmap,
    CreatePptx,
    CreateDocument,
}

impl Branch {
    /// The oracle's branch label, for reports and the matrix.
    pub fn name(self) -> &'static str {
        match self {
            Branch::BrowserFamily => "browser_*",
            Branch::PythonEval => "python_eval",
            Branch::SearchFiles => "search_files",
            Branch::FetchUrl => "fetch_url",
            Branch::WebSearch => "web_search",
            Branch::CompareSearchResults => "compare_search_results",
            Branch::SuggestMemory => "suggest_memory",
            Branch::CreateReminder => "create_reminder",
            Branch::ListReminders => "list_reminders",
            Branch::RecallMemory => "recall_memory",
            Branch::ForgetMemory => "forget_memory",
            Branch::ListProjectFiles => "list_project_files",
            Branch::ReadFileChunk => "read_file_chunk",
            Branch::DataTransform => "data_transform",
            Branch::GenerateChart => "generate_chart",
            Branch::CreateMindmap => "create_mindmap",
            Branch::CreatePptx => "create_pptx",
            Branch::CreateDocument => "create_document",
        }
    }

    /// Whether the branch actually runs. Only `generate_chart` is pure enough to
    /// port without the packages the others depend on.
    pub fn is_ported(self) -> bool {
        matches!(
            self,
            Branch::GenerateChart
                | Branch::DataTransform
                | Branch::WebSearch
                | Branch::CompareSearchResults
        )
    }

    /// Why a branch is not ported, or `None` when it is.
    pub fn blocker(self) -> Option<&'static str> {
        match self {
            Branch::GenerateChart => None,
            Branch::WebSearch | Branch::CompareSearchResults => None,
            Branch::BrowserFamily => Some("infra.browser.actions"),
            Branch::PythonEval => {
                Some("needs a real sandbox; the oracle shells out to a Python interpreter")
            }
            Branch::SearchFiles => Some("infra.rag / search"),
            Branch::FetchUrl => Some("http client + DNS-time SSRF guard"),
            Branch::SuggestMemory | Branch::RecallMemory | Branch::ForgetMemory => {
                Some("infra.data.memory")
            }
            Branch::CreateReminder | Branch::ListReminders => Some("infra.data.reminders"),
            Branch::ListProjectFiles | Branch::ReadFileChunk => Some("infra.data.projects"),
            Branch::DataTransform => None,
            Branch::CreateMindmap => Some("infra.tool_runtime.mindmaps"),
            Branch::CreatePptx => Some("infra.tool_runtime.presentations"),
            Branch::CreateDocument => Some("infra.tool_runtime.documents"),
        }
    }
}

/// Resolve a tool name to its branch, mirroring the oracle's `if/elif` chain
/// (the `browser_` prefix is tested first).
pub fn branch_for(name: &str) -> Option<Branch> {
    let name = name.trim();
    if name.starts_with("browser_") {
        return Some(Branch::BrowserFamily);
    }
    Some(match name {
        "python_eval" => Branch::PythonEval,
        "search_files" => Branch::SearchFiles,
        "fetch_url" => Branch::FetchUrl,
        "web_search" => Branch::WebSearch,
        "compare_search_results" => Branch::CompareSearchResults,
        "suggest_memory" => Branch::SuggestMemory,
        "create_reminder" => Branch::CreateReminder,
        "list_reminders" => Branch::ListReminders,
        "recall_memory" => Branch::RecallMemory,
        "forget_memory" => Branch::ForgetMemory,
        "list_project_files" => Branch::ListProjectFiles,
        "read_file_chunk" => Branch::ReadFileChunk,
        "data_transform" => Branch::DataTransform,
        "generate_chart" => Branch::GenerateChart,
        "create_mindmap" => Branch::CreateMindmap,
        "create_pptx" => Branch::CreatePptx,
        "create_document" => Branch::CreateDocument,
        _ => return None,
    })
}

/// Every branch, in the oracle's dispatch order.
pub const BRANCHES: [Branch; 18] = [
    Branch::BrowserFamily,
    Branch::PythonEval,
    Branch::SearchFiles,
    Branch::FetchUrl,
    Branch::WebSearch,
    Branch::CompareSearchResults,
    Branch::SuggestMemory,
    Branch::CreateReminder,
    Branch::ListReminders,
    Branch::RecallMemory,
    Branch::ForgetMemory,
    Branch::ListProjectFiles,
    Branch::ReadFileChunk,
    Branch::DataTransform,
    Branch::GenerateChart,
    Branch::CreateMindmap,
    Branch::CreatePptx,
    Branch::CreateDocument,
];

// --- error envelope --------------------------------------------------------------

/// A tool failure, rendered exactly as the oracle's `except` arms do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolFailure {
    /// `name or "unknown"` — a blank name becomes `"unknown"`.
    pub tool: String,
    pub error: String,
    pub code: String,
}

impl ToolFailure {
    /// The `AppError` arm.
    pub fn app(name: &str, error: impl Into<String>) -> Self {
        Self {
            tool: display_tool(name),
            error: error.into(),
            code: INVALID_PAYLOAD.to_string(),
        }
    }

    /// The catch-all arm.
    pub fn internal(name: &str, error: impl Into<String>) -> Self {
        Self {
            tool: display_tool(name),
            error: error.into(),
            code: INTERNAL.to_string(),
        }
    }

    /// `{"ok": false, "tool": …, "error": …, "code": …}`
    pub fn to_output(&self) -> Value {
        json!({
            "ok": false,
            "tool": self.tool,
            "error": self.error,
            "code": self.code,
        })
    }
}

fn display_tool(name: &str) -> String {
    if name.is_empty() {
        "unknown".to_string()
    } else {
        name.to_string()
    }
}

// --- the ported branch: generate_chart -------------------------------------------

/// Mirrors `generate_chart`.
///
/// Pure: it normalizes the chart type, keeps at most 12 usable points (dropping
/// non-objects, missing values and unparseable numbers), rejects an empty result,
/// and renders the markdown table the model reads back.
pub fn generate_chart(arguments: &Map<String, Value>) -> Result<Value, ToolFailure> {
    let normalized_type = match arguments.get("type").and_then(Value::as_str) {
        Some("bar") => "bar",
        Some("line") => "line",
        Some("pie") => "pie",
        _ => "bar",
    };
    let title = python_str_or(arguments.get("title"), "Chart");
    let data = arguments.get("data");

    let items = match data {
        Some(Value::Array(items)) if !items.is_empty() => items,
        _ => {
            return Err(ToolFailure::app(
                "generate_chart",
                "Chart data must be a non-empty list",
            ));
        }
    };

    let mut points: Vec<Value> = Vec::new();
    for item in items.iter().take(12) {
        let Some(object) = item.as_object() else {
            continue;
        };
        let label = python_truncate(python_str_or(object.get("label"), "").trim(), 80);
        let Some(raw_value) = object.get("value") else {
            continue;
        };
        if raw_value.is_null() {
            continue;
        }
        let Some(value) = python_float(raw_value) else {
            continue;
        };
        if !label.is_empty() {
            points.push(json!({"label": label, "value": value}));
        }
    }

    if points.is_empty() {
        return Err(ToolFailure::app(
            "generate_chart",
            "Chart data has no valid points",
        ));
    }

    Ok(json!({
        "type": normalized_type,
        "title": python_truncate(&title, 120),
        "data": points,
        "markdownTable": chart_markdown_table(&points),
    }))
}

/// Python's `float(value)` for the shapes a JSON argument can hold.
///
/// `None` (already filtered), a container, or unparseable text raise, which the
/// caller treats as "skip this point".
fn python_float(value: &Value) -> Option<f64> {
    match value {
        Value::Number(number) => number.as_f64(),
        Value::Bool(flag) => Some(if *flag { 1.0 } else { 0.0 }),
        Value::String(text) => {
            let trimmed = text.trim();
            if trimmed.is_empty() {
                return None;
            }
            // Python also accepts "inf"/"nan" spellings here.
            match trimmed.to_ascii_lowercase().as_str() {
                "inf" | "+inf" | "infinity" | "+infinity" => Some(f64::INFINITY),
                "-inf" | "-infinity" => Some(f64::NEG_INFINITY),
                "nan" | "+nan" | "-nan" => Some(f64::NAN),
                _ => trimmed.parse::<f64>().ok(),
            }
        }
        _ => None,
    }
}

/// Mirrors `chart_markdown_table`. Public because the table is part of the model
/// -facing result, so it is worth probing directly.
pub fn chart_markdown_table(points: &[Value]) -> String {
    let mut lines = vec!["| label | value |".to_string(), "|---|---:|".to_string()];
    for point in points {
        let label = point
            .get("label")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .replace('|', "\\|");
        let value = point
            .get("value")
            .and_then(Value::as_f64)
            .map(python_float_str)
            .unwrap_or_default();
        lines.push(format!("| {label} | {value} |"));
    }
    lines.join("\n")
}

// --- dispatch --------------------------------------------------------------------

/// What the dispatcher decided, kept separate from the wire envelope so that an
/// unported branch has *no* envelope to serve by accident.
#[derive(Debug, Clone, PartialEq)]
pub enum DispatchOutcome {
    /// The branch ran; the payload is the sanitized success envelope.
    Executed(Value),
    /// The gate denied it; the payload is `ToolPolicy::denial_output`.
    Denied(Value),
    /// No branch for this name — the oracle's `Unsupported tool:` `AppError`.
    Unsupported(Value),
    /// A branch exists but is not implemented. **No envelope: nothing may serve
    /// this as a tool result.**
    Unported { tool: String, branch: Branch },
}

impl DispatchOutcome {
    /// The wire envelope, or `None` when the branch never ran.
    ///
    /// Returning `None` for [`DispatchOutcome::Unported`] is the guard rail: a
    /// caller that forwards `to_output()` cannot accidentally report success, or
    /// even a clean error, for a tool that was never implemented.
    pub fn to_output(&self) -> Option<&Value> {
        match self {
            DispatchOutcome::Executed(value)
            | DispatchOutcome::Denied(value)
            | DispatchOutcome::Unsupported(value) => Some(value),
            DispatchOutcome::Unported { .. } => None,
        }
    }
}

/// Run one tool call, mirroring `execute_tool_call`.
///
/// Order: parse arguments, gate, route, envelope + sanitization.
///
/// **Parsing happens before the gate**, and that ordering is a security
/// property: the model sends `arguments` as a JSON *string*, and the guards
/// inspect fields inside it. Gating the raw string would leave every argument
/// guard looking at an empty object — the SSRF and path checks would silently
/// pass. (`execute_tool_call` parses first for the same reason.)
pub fn dispatch(
    name: &str,
    arguments: &Value,
    mut policy: Option<&mut ToolPolicy>,
    schema: Option<&Value>,
    context: &crate::tool_search::ExecutorContext<'_>,
) -> DispatchOutcome {
    let tool = name.trim().to_string();
    let fields = Value::Object(parse_tool_arguments(Some(arguments)));

    // 1. The gate decides before any branch is considered. `as_mut` reborrows so
    //    the policy can be used again for sanitization below.
    if let Some(gate) = policy.as_mut() {
        let decision: ToolPolicyDecision = gate.evaluate(&tool, Some(&fields), schema);
        if !decision.allowed() {
            return DispatchOutcome::Denied(ToolPolicy::denial_output(&decision));
        }
    }

    // 2. Route. With no policy attached, an unknown name reaches the oracle's
    //    `Unsupported tool:` error rather than a catalog denial.
    let Some(branch) = branch_for(&tool) else {
        return DispatchOutcome::Unsupported(
            ToolFailure::app(&tool, format!("Unsupported tool: {tool}")).to_output(),
        );
    };

    if !branch.is_ported() {
        return DispatchOutcome::Unported { tool, branch };
    }

    let object = fields.as_object().cloned().unwrap_or_default();
    // The oracle stringifies each argument with `str(value or default)` before
    // handing it to the branch.
    let result = match branch {
        Branch::GenerateChart => generate_chart(&object),
        Branch::DataTransform => crate::tool_transform::data_transform(
            &python_str_or(object.get("operation"), ""),
            &python_str_or(object.get("input"), ""),
            &python_str_or(object.get("pattern"), ""),
            &python_str_or(object.get("path"), ""),
            &python_str_or(object.get("delimiter"), ","),
        ),
        Branch::WebSearch => crate::tool_search::web_search(&object, context),
        Branch::CompareSearchResults => {
            crate::tool_search::compare_search_results_branch(&object, context)
        }
        // Unreachable: `is_ported` was checked above.
        other => {
            return DispatchOutcome::Unported {
                tool,
                branch: other,
            };
        }
    };
    match result {
        Ok(result) => {
            let mut output = json!({"ok": true, "tool": tool, "result": result});
            if let Some(gate) = policy.as_mut() {
                output = gate.sanitize_result(&tool, output);
            }
            DispatchOutcome::Executed(output)
        }
        Err(failure) => DispatchOutcome::Unsupported(failure.to_output()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tool_policy::{ToolPolicy, ToolPolicyConfig};
    use crate::tool_search::ExecutorContext;

    /// [`dispatch`] with no per-request dependencies — what every test here wants.
    fn run(
        name: &str,
        arguments: &Value,
        policy: Option<&mut ToolPolicy>,
        schema: Option<&Value>,
    ) -> DispatchOutcome {
        dispatch(name, arguments, policy, schema, &ExecutorContext::default())
    }

    fn args(value: Value) -> Value {
        value
    }

    fn fields(value: Value) -> Map<String, Value> {
        parse_tool_arguments(Some(&value))
    }

    // --- normalization -------------------------------------------------------

    #[test]
    fn tool_call_name_prefers_the_function_name() {
        assert_eq!(
            tool_call_name(&json!({"function": {"name": " search_files "}})),
            "search_files"
        );
        // A blank function name falls through to the top-level one.
        assert_eq!(
            tool_call_name(&json!({"function": {"name": ""}, "name": "x"})),
            "x"
        );
        assert_eq!(tool_call_name(&json!({"name": "y"})), "y");
        assert_eq!(tool_call_name(&json!({})), "");
    }

    #[test]
    fn parse_tool_arguments_accepts_objects_and_object_strings_only() {
        assert_eq!(fields(json!({"a": 1}))["a"], 1);
        assert_eq!(fields(json!("{\"a\": 1}"))["a"], 1);
        // Everything else is empty, not an error.
        assert!(fields(json!("not json")).is_empty());
        assert!(fields(json!("[1, 2]")).is_empty());
        assert!(fields(json!("   ")).is_empty());
        assert!(fields(json!(null)).is_empty());
        assert!(fields(json!(42)).is_empty());
        assert!(parse_tool_arguments(None).is_empty());
    }

    #[test]
    fn safe_limit_clamps_and_falls_back() {
        assert_eq!(safe_limit(Some(&json!(3)), 5, 10), 3);
        assert_eq!(safe_limit(Some(&json!(0)), 5, 10), 1);
        assert_eq!(safe_limit(Some(&json!(-9)), 5, 10), 1);
        assert_eq!(safe_limit(Some(&json!(99)), 5, 10), 10);
        // `int()` truncates toward zero, but a numeric *string* must be an
        // integer literal.
        assert_eq!(safe_limit(Some(&json!(3.9)), 5, 10), 3);
        assert_eq!(safe_limit(Some(&json!("7")), 5, 10), 7);
        assert_eq!(safe_limit(Some(&json!("7.9")), 5, 10), 5);
        // Unparseable, missing, and non-scalar values fall back.
        assert_eq!(safe_limit(Some(&json!("abc")), 5, 10), 5);
        assert_eq!(safe_limit(None, 5, 10), 5);
        assert_eq!(safe_limit(Some(&json!([1])), 5, 10), 5);
        assert_eq!(safe_limit(Some(&json!(null)), 5, 10), 5);
    }

    #[test]
    fn parallel_safety_is_the_complement_of_the_serial_set() {
        for name in SERIAL_TOOL_NAMES {
            assert!(
                !is_parallel_safe_tool(&json!({"function": {"name": name}})),
                "{name} must be serial"
            );
        }
        assert!(is_parallel_safe_tool(
            &json!({"function": {"name": "generate_chart"}})
        ));
        // A nameless call is not parallel-safe.
        assert!(!is_parallel_safe_tool(&json!({})));
    }

    #[test]
    fn python_float_str_matches_pythons_rendering() {
        assert_eq!(python_float_str(1.0), "1.0");
        assert_eq!(python_float_str(2.5), "2.5");
        assert_eq!(python_float_str(0.0), "0.0");
        assert_eq!(python_float_str(-0.0), "-0.0");
        assert_eq!(python_float_str(-3.25), "-3.25");
        assert_eq!(python_float_str(100.0), "100.0");
        // Python switches to a signed, zero-padded exponent outside 1e-4..1e16.
        assert_eq!(python_float_str(1e16), "1e+16");
        assert_eq!(python_float_str(1e-5), "1e-05");
        assert_eq!(python_float_str(f64::INFINITY), "inf");
        assert_eq!(python_float_str(f64::NEG_INFINITY), "-inf");
        assert_eq!(python_float_str(f64::NAN), "nan");
    }

    // --- branch inventory ----------------------------------------------------

    #[test]
    fn the_branch_inventory_is_complete_and_ordered() {
        assert_eq!(BRANCHES.len(), 18);
        assert_eq!(BRANCHES[0], Branch::BrowserFamily);
        assert_eq!(BRANCHES[17], Branch::CreateDocument);
        // Every branch resolves from its own name (the browser family is a
        // prefix, so it is checked separately).
        for branch in BRANCHES {
            if branch == Branch::BrowserFamily {
                assert_eq!(branch_for("browser_click"), Some(branch));
                continue;
            }
            assert_eq!(branch_for(branch.name()), Some(branch), "{branch:?}");
        }
        assert_eq!(branch_for("not_a_tool"), None);
    }

    #[test]
    fn ported_branches_are_exactly_the_pure_ones_and_the_rest_name_a_blocker() {
        let ported: Vec<&str> = BRANCHES
            .iter()
            .filter(|branch| branch.is_ported())
            .map(|branch| branch.name())
            .collect();
        // The four branches with no external package behind them.
        assert_eq!(
            ported,
            vec![
                "web_search",
                "compare_search_results",
                "data_transform",
                "generate_chart"
            ]
        );
        for branch in BRANCHES {
            assert_eq!(branch.blocker().is_none(), branch.is_ported(), "{branch:?}");
        }
    }

    // --- generate_chart ------------------------------------------------------

    #[test]
    fn generate_chart_normalizes_points_and_renders_markdown() {
        let result = generate_chart(&fields(json!({
            "type": "line",
            "title": "Revenue",
            "data": [
                {"label": "Q1", "value": 1},
                {"label": "Q2", "value": 2.5},
            ],
        })))
        .expect("valid chart");
        assert_eq!(result["type"], "line");
        assert_eq!(result["title"], "Revenue");
        assert_eq!(result["data"][0], json!({"label": "Q1", "value": 1.0}));
        assert_eq!(
            result["markdownTable"],
            "| label | value |\n|---|---:|\n| Q1 | 1.0 |\n| Q2 | 2.5 |"
        );
    }

    #[test]
    fn generate_chart_defaults_the_type_and_title() {
        let result =
            generate_chart(&fields(json!({"data": [{"label": "a", "value": 1}]}))).unwrap();
        assert_eq!(result["type"], "bar");
        assert_eq!(result["title"], "Chart");
        // An unknown type is not an error — it becomes "bar".
        let unknown = generate_chart(&fields(
            json!({"type": "radar", "data": [{"label": "a", "value": 1}]}),
        ))
        .unwrap();
        assert_eq!(unknown["type"], "bar");
    }

    /// `data[:12]` is applied **before** the validity filter, so the cap counts
    /// *raw* items, not usable points. With 26 items only the first 12 are even
    /// examined, and 7 of those survive.
    ///
    /// My first version of this test asserted 12 — it read "at most 12 points"
    /// into a cap on input items. The oracle slices first.
    #[test]
    fn generate_chart_slices_to_twelve_before_filtering() {
        let mut data = vec![
            json!("not an object"),
            json!({"value": 1}),
            json!({"label": "no-value"}),
            json!({"label": "null-value", "value": null}),
            json!({"label": "bad-number", "value": "abc"}),
            json!({"label": "ok", "value": "3"}),
        ];
        for index in 0..20 {
            data.push(json!({"label": format!("p{index}"), "value": index}));
        }
        let result = generate_chart(&fields(json!({"data": data}))).unwrap();
        let points = result["data"].as_array().unwrap();
        // The 6 leading junk items plus p0..p5 are the first 12; of those, "ok"
        // and p0..p5 are usable.
        assert_eq!(points.len(), 7);
        assert_eq!(points[0], json!({"label": "ok", "value": 3.0}));
        assert_eq!(points[1], json!({"label": "p0", "value": 0.0}));
        assert_eq!(points[6], json!({"label": "p5", "value": 5.0}));
    }

    #[test]
    fn generate_chart_escapes_pipes_in_labels() {
        let result =
            generate_chart(&fields(json!({"data": [{"label": "a|b", "value": 1}]}))).unwrap();
        assert!(result["markdownTable"].as_str().unwrap().contains("a\\|b"));
    }

    #[test]
    fn generate_chart_rejects_empty_and_useless_data() {
        let no_list = generate_chart(&fields(json!({"data": "nope"}))).unwrap_err();
        assert_eq!(no_list.error, "Chart data must be a non-empty list");
        assert_eq!(no_list.code, INVALID_PAYLOAD);

        let empty = generate_chart(&fields(json!({"data": []}))).unwrap_err();
        assert_eq!(empty.error, "Chart data must be a non-empty list");

        let useless = generate_chart(&fields(json!({"data": [{"value": 1}]}))).unwrap_err();
        assert_eq!(useless.error, "Chart data has no valid points");
        assert_eq!(useless.tool, "generate_chart");
    }

    // --- dispatch ------------------------------------------------------------

    #[test]
    fn without_a_policy_an_unknown_tool_reaches_the_unsupported_envelope() {
        let outcome = run("not_a_tool", &args(json!({})), None, None);
        let DispatchOutcome::Unsupported(output) = outcome else {
            panic!("expected Unsupported, got {outcome:?}");
        };
        assert_eq!(output["ok"], false);
        assert_eq!(output["code"], INVALID_PAYLOAD);
        assert_eq!(output["error"], "Unsupported tool: not_a_tool");
        assert_eq!(output["tool"], "not_a_tool");
    }

    /// With a policy attached the gate wins first, so the fallback above is
    /// unreachable — which is exactly how the oracle behaves.
    #[test]
    fn with_a_policy_the_gate_denies_an_unknown_tool_first() {
        let mut policy = ToolPolicy::new(ToolPolicyConfig {
            audit: false,
            ..ToolPolicyConfig::default()
        });
        let outcome = run("not_a_tool", &args(json!({})), Some(&mut policy), None);
        let DispatchOutcome::Denied(output) = outcome else {
            panic!("expected Denied, got {outcome:?}");
        };
        assert_eq!(output["code"], "forbidden");
        assert_eq!(output["policy"]["reasons"], json!(["unknown_tool"]));
    }

    #[test]
    fn a_ported_branch_runs_and_returns_the_success_envelope() {
        let outcome = run(
            "generate_chart",
            &args(json!({"data": [{"label": "a", "value": 1}]})),
            None,
            None,
        );
        let DispatchOutcome::Executed(output) = outcome else {
            panic!("expected Executed, got {outcome:?}");
        };
        assert_eq!(output["ok"], true);
        assert_eq!(output["tool"], "generate_chart");
        assert_eq!(output["result"]["type"], "bar");
    }

    #[test]
    fn a_branch_failure_uses_the_error_envelope() {
        let outcome = run("generate_chart", &args(json!({"data": []})), None, None);
        let DispatchOutcome::Unsupported(output) = outcome else {
            panic!("expected Unsupported, got {outcome:?}");
        };
        assert_eq!(output["ok"], false);
        assert_eq!(output["error"], "Chart data must be a non-empty list");
        assert_eq!(output["code"], INVALID_PAYLOAD);
    }

    /// The guard rail: an unported branch has *no* envelope, so a caller cannot
    /// report success (or even a tidy error) for a tool that never ran.
    #[test]
    fn an_unported_branch_has_no_envelope_at_all() {
        for name in ["fetch_url", "search_files", "browser_click", "create_pptx"] {
            let outcome = run(name, &args(json!({})), None, None);
            let DispatchOutcome::Unported { tool, branch } = &outcome else {
                panic!("expected Unported for {name}, got {outcome:?}");
            };
            assert_eq!(tool, name);
            assert!(!branch.is_ported());
            assert!(
                outcome.to_output().is_none(),
                "{name} must not yield an envelope"
            );
        }
    }

    #[test]
    fn a_denial_short_circuits_before_the_branch_runs() {
        // `fetch_url` to a metadata address: denied by the SSRF guard, so the
        // unported branch is never even reached.
        let mut policy = ToolPolicy::new(ToolPolicyConfig {
            audit: false,
            ..ToolPolicyConfig::default()
        });
        let outcome = run(
            "fetch_url",
            &args(json!({"url": "http://169.254.169.254/"})),
            Some(&mut policy),
            None,
        );
        let DispatchOutcome::Denied(output) = outcome else {
            panic!("expected Denied, got {outcome:?}");
        };
        assert_eq!(output["code"], "forbidden");
        assert_eq!(output["risk"], "critical");
    }

    /// The model sends `arguments` as a JSON *string*. Gating the raw string
    /// would leave every argument guard inspecting an empty object, so the SSRF
    /// check would silently pass — this test fails if parsing is moved after the
    /// gate.
    #[test]
    fn string_encoded_arguments_are_parsed_before_the_guard_runs() {
        let mut policy = ToolPolicy::new(ToolPolicyConfig {
            audit: false,
            ..ToolPolicyConfig::default()
        });
        let outcome = run(
            "fetch_url",
            &args(json!("{\"url\": \"http://169.254.169.254/\"}")),
            Some(&mut policy),
            None,
        );
        let DispatchOutcome::Denied(output) = outcome else {
            panic!("a string-encoded SSRF target must still be denied, got {outcome:?}");
        };
        assert_eq!(output["risk"], "critical");
        assert_eq!(
            output["policy"]["reasons"],
            json!(["ssrf_blocked:private or local ip is not allowed: 169.254.169.254"])
        );
    }

    #[test]
    fn string_encoded_arguments_reach_a_ported_branch() {
        // The same parsing rule feeds the chart branch.
        let outcome = run(
            "generate_chart",
            &args(json!("{\"data\": [{\"label\": \"a\", \"value\": 1}]}")),
            None,
            None,
        );
        let DispatchOutcome::Executed(output) = outcome else {
            panic!("expected Executed, got {outcome:?}");
        };
        assert_eq!(
            output["result"]["data"][0],
            json!({"label": "a", "value": 1.0})
        );
    }
}
