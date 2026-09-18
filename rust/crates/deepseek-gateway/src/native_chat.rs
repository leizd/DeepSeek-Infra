//! The composition the native `/v1/chat/completions` route runs before dispatch.
//!
//! Measured, not assumed. The oracle's route is
//! `routes/chat.py:65` → `openai_chat_completion` → `resolve_provider(model).chat(payload)`
//! → `call_deepseek` (`deepseek_client.py:1486`) → `prepare_deepseek_call` (`:651`), and
//! `prepare_deepseek_call` runs exactly four things, in this order:
//!
//! 1. `preflight_deepseek_payload(payload)` — validation **before** anything else, so a
//!    bad payload never reaches the memory store;
//! 2. `prepare_memory_state(payload)` — always, because the empty state is what carries
//!    `enabled`/`hitCount`/`context` into the assembly;
//! 3. `forced_search_mode(payload)` — and this is **structurally unreachable on this
//!    route**: `search_mode` is `payload.get("searchMode") or "auto"`, and
//!    `openai_to_internal_payload` never forwards `searchMode`. So the search prefetch
//!    branch is dead here, and this module guards nothing for it. Adding a refusal would
//!    introduce a refusal the oracle cannot perform — the mistake the `search_provider`
//!    docs already record for `search_budget`;
//! 4. `build_deepseek_request(payload, stream=…, memory_state=…, validated=…)`.
//!
//! `call_deepseek` passes `stream=False` at `:1513`, and the route only takes the
//! non-streaming branch when the payload's `stream` is falsy, so the two agree; `stream`
//! is a parameter here rather than a constant because the streaming route (`stream_deepseek`)
//! reaches the same assembly with `stream=True`.
//!
//! # Why two steps rather than one call
//!
//! Step 2 needs the memory state, and computing it needs the payload, so a single
//! function would have to take a callback and hide the ordering that step 1 exists to
//! pin. Splitting them keeps "validate before memory" visible in the call site instead
//! of implicit in a closure's position.

use serde_json::Value;

use deepseek_policy::app_error::AppError;
use deepseek_policy::request_messages::{ValidatedPayload, validate_deepseek_payload};

use crate::openai_facade::openai_to_internal_payload;
use crate::request_assembly::{AssemblyEnv, PreparedDeepSeekRequest, build_deepseek_request};

/// The facade translation plus `preflight_deepseek_payload` — steps 1 and 2 of the
/// oracle's `call_deepseek`, in that order.
#[derive(Debug, Clone, PartialEq)]
pub struct PreparedOpenAiChat {
    /// The internal payload the assembly consumes. The memory read needs it, which is
    /// why it is returned rather than kept private.
    pub payload: Value,
    pub validated: ValidatedPayload,
}

/// Steps 1-2: translate the OpenAI body, then validate it.
///
/// `local_base_url` is `request_base_url(request)`; `env.api_key_fallback` is the same
/// server-side key `build_deepseek_request` would fall back to, and using it here keeps
/// validation identical to the assembly's own default.
pub fn prepare_openai_chat(
    body: &Value,
    local_base_url: &str,
    env: &AssemblyEnv<'_>,
) -> Result<PreparedOpenAiChat, AppError> {
    let payload = openai_to_internal_payload(body, local_base_url, env.router)?;
    let validated = validate_deepseek_payload(&payload, env.api_key_fallback, env.router)?;
    Ok(PreparedOpenAiChat { payload, validated })
}

/// Steps 3-4: assemble the upstream request around the already-computed memory state.
///
/// `memory_state` is the caller's, from `prepare_memory_state`; it is passed through
/// `Some(..)` rather than re-derived here so `build_deepseek_request`'s
/// `memory_state or empty_memory_state(payload)` sees exactly what the oracle sees (a
/// falsy state still falls back to the payload-derived default, which is the oracle's
/// own behaviour and is preserved by passing it rather than filtering it).
pub fn assemble_openai_chat(
    prepared: PreparedOpenAiChat,
    stream: bool,
    memory_state: &Value,
    env: &AssemblyEnv<'_>,
) -> Result<PreparedDeepSeekRequest, AppError> {
    build_deepseek_request(
        &prepared.payload,
        stream,
        Some(memory_state),
        Some(&prepared.validated),
        env,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    use deepseek_policy::budget_ledger::LedgerDeps;
    use deepseek_policy::budget_manager::BudgetSettings;
    use deepseek_policy::context_engine::ContextEngineSettings;
    use deepseek_policy::context_manager::ContextManagerSettings;
    use deepseek_policy::context_taint::ContextTaintSettings;
    use deepseek_policy::dynamic_context::{DynamicContextEnv, LocalNow};
    use deepseek_policy::model_router::ModelRouterSettings;
    use serde_json::json;

    const EXPANDED: &str = "expanded";

    struct Fixture {
        router: ModelRouterSettings,
        budget: BudgetSettings,
        taint: ContextTaintSettings,
        context_manager: ContextManagerSettings,
        engine: ContextEngineSettings,
        dynamic: DynamicContextEnv,
        ledger: LedgerDeps<'static>,
        expander: Box<dyn Fn(&Value) -> String>,
    }

    fn fixture() -> Fixture {
        let read = |_scope: &str, _day: &str| -> Result<Option<Value>, String> { Ok(None) };
        let write = |_row: &Value| -> Result<(), String> { Ok(()) };
        Fixture {
            router: ModelRouterSettings::default(),
            budget: BudgetSettings::default(),
            taint: ContextTaintSettings::default(),
            context_manager: ContextManagerSettings::default(),
            engine: ContextEngineSettings::default(),
            dynamic: DynamicContextEnv::new(LocalNow {
                epoch_seconds: 1_760_000_000,
                offset_seconds: 0,
                timezone_name: "UTC".to_string(),
            }),
            ledger: LedgerDeps {
                database_present: false,
                database_path: "test-budget.db".to_string(),
                day: "2026-09-18".to_string(),
                now_iso: "2026-09-18T00:00:00Z".to_string(),
                read_spend_row: Box::leak(Box::new(read)),
                write_spend_row: Box::leak(Box::new(write)),
            },
            expander: Box::new(|_message: &Value| EXPANDED.to_string()),
        }
    }

    fn minimal() -> Value {
        json!({"model": "deepseek-v4-pro", "messages": [{"role": "user", "content": " hello "}]})
    }

    #[test]
    fn the_facade_runs_before_validation_so_a_non_object_body_is_the_facade_error() {
        let fixture = fixture();
        let env = fixture.env();
        let error = prepare_openai_chat(&json!([]), "http://127.0.0.1:8000", &env)
            .expect_err("a non-object body is refused");
        assert_eq!(error.message, "Request body must be a JSON object");
        assert_eq!(error.code, "invalid_payload");
    }

    #[test]
    fn the_translated_payload_is_what_validation_sees() {
        let fixture = fixture();
        let env = fixture.env();
        let prepared = prepare_openai_chat(&minimal(), "http://127.0.0.1:8000", &env)
            .expect("a minimal body is accepted");
        // `thinkingEnabled: false` comes from the facade, not from the client.
        assert_eq!(prepared.payload["thinkingEnabled"], json!(false));
        assert_eq!(prepared.validated.model, "deepseek-v4-pro");
    }

    #[test]
    fn the_assembled_body_carries_the_catalog_tools_the_client_never_sent() {
        let fixture = fixture();
        let env = fixture.env();
        let prepared = prepare_openai_chat(
            &json!({
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "client_tool"}}],
            }),
            "http://127.0.0.1:8000",
            &env,
        )
        .expect("the body is accepted");
        let assembled =
            assemble_openai_chat(prepared, false, &json!({"enabled": false}), &env).expect("built");
        let tools = assembled.body["tools"]
            .as_array()
            .expect("tools are always attached");
        let names: Vec<&str> = tools
            .iter()
            .filter_map(|tool| tool.get("function")?.get("name")?.as_str())
            .collect();
        // The client's tool is gone: the facade dropped it, and `tools_for_payload`
        // supplies the catalog instead.
        assert!(!names.contains(&"client_tool"), "{names:?}");
        assert!(names.contains(&"generate_chart"), "{names:?}");
        // And `web_search` is **not** in the list, because `tools_for_payload` adds it
        // only when `search_tool_enabled(payload)` sees `searchEnabled is True` — a field
        // the facade never forwards. Measured here rather than assumed: the OpenAI route
        // gets the catalog minus the search tool.
        assert!(!names.contains(&"web_search"), "{names:?}");
        assert!(assembled.body["messages"].is_array());
    }

    impl Fixture {
        fn env(&self) -> AssemblyEnv<'_> {
            AssemblyEnv {
                api_key_fallback: "test-key",
                expander: self.expander.as_ref(),
                router: &self.router,
                budget: &self.budget,
                ledger: &self.ledger,
                taint: &self.taint,
                context_manager: &self.context_manager,
                engine: &self.engine,
                dynamic: &self.dynamic,
            }
        }
    }
}
