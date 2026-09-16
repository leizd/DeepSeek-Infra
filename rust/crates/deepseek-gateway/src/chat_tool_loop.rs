//! The non-streaming tool round loop — the core of `call_deepseek`'s tool loop.
//!
//! This is the wiring slice: it gives `deepseek_policy::tool_dispatch::dispatch`
//! its first production caller. One round here is exactly the oracle's loop
//! body, in the oracle's order:
//!
//! ```text
//! for tool_round in range(max_tool_rounds + 2):
//!     turn = upstream(body)                     // exchange_turn
//!     usage_totals = merge_usage_totals(...)     // chat_execution
//!     tool_calls = normalize(answer.tool_calls)  // the lenient round layer
//!     if not tool_calls: break                   // decide_round::Finish
//!     if tool_round >= max: force_final(body)    // decide_round::ForceFinalAnswer
//!     else: body = append_tool_exchange(body,    // decide_round::Continue
//!               answer, tool_calls, execute_tool_calls(tool_calls))
//! ```
//!
//! # What runs and what does not
//!
//! Eleven of the eighteen dispatch branches execute for real here — the four
//! request-independent ones plus the seven data branches against the injected
//! [`WorkspaceContext`]. The other seven are reported honestly to the model:
//! `execute_tool_calls` resolves an unported branch to the `Tool did not run`
//! envelope rather than a success, and the loop keeps going. That is a
//! degradation against the Python oracle for those seven branches (`browser_*`,
//! `python_eval`, `search_files`, `fetch_url`, `create_mindmap`, `create_pptx`,
//! `create_document`), stated plainly rather than hidden — this gateway is an
//! opt-in delegate, and the authority for those tools remains Python.
//!
//! # What is deliberately not ported from the surrounding oracle loop
//!
//! Each of these is a real gap with an owner, not a silent drop:
//! - the **web-search provider** — no callback is injected, so `web_search` /
//!   `compare_search_results` answer "not enabled for this request";
//! - **`mcp__*` bridging** — the external MCP registry is not ported, so a
//!   bridged name reports `Unsupported tool:`;
//! - the **artifact terminal check** (`terminal_artifact_result_from_messages`)
//!   and `ensure_pptx_response` — both exist for `create_pptx` /
//!   `create_document` / `create_mindmap`, none of which is ported;
//! - semantic cache, memory retrieval between rounds, scheduler leases,
//!   resiliency retries, traces/spans, the budget ledger, `system_note`
//!   emission, and the cancel event (a dropped client connection stops the
//!   loop the same way it stops any handler: the future stops being polled).
//!
//! # Divergences kept on purpose
//!
//! - the workspace root comes from `DEEPSEEK_INFRA_ROOT` (unset ⇒ no workspace,
//!   so data branches answer "not enabled for this request"); the oracle reads
//!   `config.ROOT`, a module global, so it always has one;
//! - the policy profile is the oracle's main-chat default (`capability: full`,
//!   schema/confirm knobs off, sanitization on, secrets = the process's
//!   `DEEPSEEK_API_KEY` + `AUTH_TOKEN`); payload-driven narrowing
//!   (`capability` / `allowedTools` / `approvedTools`) and the context-taint
//!   firewall are not ported, and the OpenAI facade carries no fields for them;
//! - concurrency is not reproduced: `execute_tool_calls` runs its parallel
//!   group sequentially, which yields the same index-ordered list.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use serde_json::Value;

use crate::chat_execution::{
    ChatCompletionResult, ChatExecutionError, UpstreamConfig, UpstreamTurn, exchange_turn,
    final_answer, merge_usage_totals,
};
use crate::tool_rounds::{self, RoundDecision};
use deepseek_policy::core_utils::SystemClock;
use deepseek_policy::entropy::SystemEntropy;
use deepseek_policy::file_cache::FileCache;
use deepseek_policy::tool_batch::{self, execute_tool_calls};
use deepseek_policy::tool_dispatch::{
    self, MAX_TOOL_CALLS_PER_RESPONSE, WorkspaceContext, dispatch,
};
use deepseek_policy::tool_policy::{ToolPolicy, ToolPolicyConfig};
use deepseek_policy::tool_search::ExecutorContext;

/// The workspace dependencies the data branches run against, bundled so one
/// request can hold them and the file cache persists across that request's
/// calls (and rounds).
///
/// The oracle's equivalents are module globals in `infra.data.*`; the root is
/// the one piece this process cannot derive, so it comes from the environment.
pub struct WorkspaceBundle {
    root: PathBuf,
    file_cache: FileCache,
    entropy: SystemEntropy,
    clock: SystemClock,
}

impl WorkspaceBundle {
    pub fn new(root: PathBuf) -> Self {
        Self {
            root,
            file_cache: FileCache::new(),
            entropy: SystemEntropy,
            clock: SystemClock,
        }
    }

    /// The borrow-view the data branches take. `vector_hits` is `None` — the
    /// `local_rag` index is not ported, which reproduces the oracle's own
    /// `except Exception` degradation path — and `on_memory_suggestion` is
    /// `None`, so a suggestion is still built and returned, just not notified:
    /// the OpenAI envelope has no `memorySuggestions` channel to carry it.
    fn view(&self) -> WorkspaceContext<'_> {
        WorkspaceContext {
            root: &self.root,
            entropy: &self.entropy,
            clock: &self.clock,
            file_cache: &self.file_cache,
            vector_hits: None,
            on_memory_suggestion: None,
            default_memory_scope: "global",
        }
    }
}

/// Per-request tool execution: what `dispatch` needs for this request's calls.
///
/// Constructed per request (the file cache is request-scoped by contract), from
/// the server environment. The policy is shared across the request's rounds and
/// calls — its counters accumulate exactly as the oracle's single `tool_policy`
/// object does across its loop.
pub struct ToolRoundExecutor {
    workspace: Option<Arc<WorkspaceBundle>>,
    policy: Option<Arc<Mutex<ToolPolicy>>>,
}

impl ToolRoundExecutor {
    /// Build one from injected parts. `None` workspace/policy are the honest
    /// states: data branches then report "not enabled for this request" and
    /// calls run ungated, both mirroring the oracle's disabled paths.
    pub fn new(workspace: Option<WorkspaceBundle>, policy: Option<ToolPolicy>) -> Self {
        Self {
            workspace: workspace.map(Arc::new),
            policy: policy.map(|policy| Arc::new(Mutex::new(policy))),
        }
    }

    /// From the server environment, mirroring the oracle's defaults.
    pub fn from_env() -> Self {
        let workspace = std::env::var_os("DEEPSEEK_INFRA_ROOT")
            .map(PathBuf::from)
            .map(WorkspaceBundle::new);
        Self::new(workspace, policy_from_env())
    }

    /// Execute the finalized calls of one round and assemble the
    /// `role: "tool"` messages, mirroring the keyword arguments threaded
    /// through the oracle's `execute_tool_calls`.
    ///
    /// Runs on the blocking pool: the data branches take OS file locks (the
    /// mutation gate retries once a second for up to ten) and read stores, none
    /// of which belongs on the async runtime.
    pub async fn run_round(&self, tool_calls: Vec<Value>) -> Vec<Value> {
        let workspace = self.workspace.clone();
        let policy = self.policy.clone();
        // The JoinError fallback needs the selected calls too, so the truncation
        // is computed once here and a copy is kept for it (it only runs when the
        // blocking task died, which is not the hot path).
        let selected: Vec<Value> = tool_calls
            .iter()
            .take(MAX_TOOL_CALLS_PER_RESPONSE)
            .cloned()
            .collect();
        let fallback = selected.clone();
        let joined = tokio::task::spawn_blocking(move || {
            let view = workspace.as_deref().map(|bundle| bundle.view());
            let context = ExecutorContext {
                web_search: None,
                workspace: view.as_ref(),
            };
            execute_tool_calls(&selected, &|| false, &|call| {
                let name = tool_dispatch::tool_call_name(call);
                let arguments = call
                    .get("function")
                    .and_then(|function| function.get("arguments"))
                    .cloned()
                    .unwrap_or(Value::Null);
                let mut policy_guard = policy.as_ref().map(|shared| {
                    shared
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner)
                });
                dispatch(
                    &name,
                    &arguments,
                    policy_guard.as_deref_mut(),
                    // `schema_for_tool` — the schema catalog — is not ported;
                    // with `enforce_schema` off (the oracle default) the
                    // absence changes no verdict.
                    None,
                    &context,
                )
            })
        })
        .await;
        match joined {
            Ok(results) => results,
            // The blocking task died (a panic inside a branch). Resolve every
            // selected slot through the batch layer's own "did not run"
            // envelope rather than inventing results — the same resolution its
            // None-slot path applies.
            Err(_) => fallback
                .iter()
                .map(|call| {
                    tool_batch::tool_result_message(call, &tool_batch::did_not_run_output(call))
                })
                .collect(),
        }
    }
}

/// The oracle's `build_tool_policy` for the main-chat shape this facade serves:
/// enabled by default, `capability: full`, the four strictness knobs at the
/// `ToolPolicySettings` defaults, and the process's own credentials as the
/// secret-exfiltration blocklist (`settings.deepseek_api_key`,
/// `settings.auth.token` there).
fn policy_from_env() -> Option<ToolPolicy> {
    // `_env_bool` semantics: unset/blank keeps the default, anything else is
    // truthy only in the oracle's four spellings.
    let enabled = match std::env::var("TOOL_POLICY_ENABLED") {
        Ok(raw) if !raw.trim().is_empty() => matches!(
            raw.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes" | "on"
        ),
        _ => true,
    };
    if !enabled {
        return None;
    }
    let config = ToolPolicyConfig {
        secrets: ["DEEPSEEK_API_KEY", "AUTH_TOKEN"]
            .into_iter()
            .filter_map(|name| {
                std::env::var(name)
                    .ok()
                    .map(|value| value.trim().to_string())
                    .filter(|value| !value.is_empty())
            })
            .collect(),
        ..ToolPolicyConfig::default()
    };
    Some(ToolPolicy::new(config))
}

/// Run the non-streaming chat tool loop and return the final answer.
///
/// `prepared` is the output of `request_preparation::prepare_chat_request`; the
/// loop's working body starts there and is rebuilt by
/// [`tool_rounds::append_tool_exchange`] / `force_final_answer_without_tools`.
pub async fn execute_chat_with_tool_rounds(
    config: &UpstreamConfig,
    prepared: &Value,
    executor: &ToolRoundExecutor,
) -> Result<ChatCompletionResult, ChatExecutionError> {
    let mut body = prepared.clone();
    let mut usage_totals = Value::Object(serde_json::Map::new());
    let mut last_turn: Option<UpstreamTurn> = None;
    for tool_round in 0..(tool_rounds::MAX_TOOL_ROUNDS + 2) {
        let turn = exchange_turn(config, &body).await?;
        usage_totals = merge_usage_totals(&usage_totals, &turn.usage);
        let calls = turn.tool_calls();
        let content = turn.content();
        let reasoning = turn.reasoning_content();
        let decision =
            tool_rounds::decide_round(calls.len(), tool_round, tool_rounds::MAX_TOOL_ROUNDS);
        last_turn = Some(turn);
        match decision {
            RoundDecision::Finish => break,
            RoundDecision::ForceFinalAnswer => {
                body = tool_rounds::force_final_answer_without_tools(&body);
            }
            RoundDecision::Continue => {
                let results = executor.run_round(calls.clone()).await;
                body = tool_rounds::append_tool_exchange(
                    &body, &content, &reasoning, &calls, &results,
                );
            }
        }
    }
    let turn = last_turn.ok_or(ChatExecutionError::NoAnswer)?;
    final_answer(&turn, &usage_totals)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn call(name: &str, arguments: &str) -> Value {
        json!({"id": "call-1", "type": "function", "function": {"name": name, "arguments": arguments}})
    }

    #[tokio::test]
    async fn a_data_branch_runs_against_the_injected_workspace() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().to_path_buf();
        let executor = ToolRoundExecutor::new(Some(WorkspaceBundle::new(root.clone())), None);
        let results = executor
            .run_round(vec![call(
                "create_reminder",
                r#"{"title":"buy milk","content":"two litres","dueAt":"2027-01-01T09:00:00Z"}"#,
            )])
            .await;
        assert_eq!(results.len(), 1);
        assert_eq!(results[0]["role"], "tool");
        assert_eq!(results[0]["tool_call_id"], "call-1");
        let content = results[0]["content"].as_str().unwrap();
        assert!(content.contains("\"ok\":true"), "content: {content}");
        assert!(content.contains("\"tool\":\"create_reminder\""));
        // The write went through the workspace: the store exists under the
        // injected root, not under some process-global default.
        assert!(root.join(".reminders").join("reminders.json").exists());
    }

    #[tokio::test]
    async fn a_data_branch_without_a_workspace_reports_not_enabled() {
        let executor = ToolRoundExecutor::new(None, None);
        let results = executor.run_round(vec![call("list_reminders", "{}")]).await;
        let content = results[0]["content"].as_str().unwrap();
        assert!(content.contains("\"ok\":false"), "content: {content}");
        assert!(
            content.contains("no workspace context"),
            "content: {content}"
        );
        // The name is still attached so the model can tell which tool failed.
        assert!(content.contains("\"tool\":\"list_reminders\""));
    }

    #[tokio::test]
    async fn the_gate_runs_before_the_branch_when_a_policy_is_attached() {
        // An unregistered name is denied by the policy before routing; without
        // one it would reach the "Unsupported tool:" envelope instead.
        let executor = ToolRoundExecutor::new(None, Some(ToolPolicy::permissive()));
        let results = executor
            .run_round(vec![call("mcp__external__nope", "{}")])
            .await;
        let content = results[0]["content"].as_str().unwrap();
        assert!(content.contains("\"ok\":false"), "content: {content}");
        assert!(
            content.contains("blocked by tool policy"),
            "content: {content}"
        );
    }

    #[test]
    fn the_policy_profile_is_the_main_chat_default() {
        // ToolPolicyConfig::default() carries the ToolPolicySettings defaults
        // (enabled, no schema enforcement, no forced confirmation,
        // sanitization on) — the profile build_tool_policy produces for a
        // main-chat request. The unit here guards against the bundle drifting
        // into a different shape by accident.
        let policy = ToolPolicy::new(ToolPolicyConfig::default());
        assert_eq!(policy.capability(), "full");
    }

    #[test]
    fn the_unported_resolution_matches_the_batch_layer() {
        // The JoinError fallback resolves to the same envelope the batch layer
        // uses for a slot that produced nothing.
        let call = call("fetch_url", "{}");
        let message =
            tool_batch::tool_result_message(&call, &tool_batch::did_not_run_output(&call));
        let content = message["content"].as_str().unwrap();
        assert!(content.contains("Tool did not run"));
    }
}
