//! The server environment the native chat route's request assembly runs against.
//!
//! `build_deepseek_request` takes nine injected reads and settings; this module is the
//! one place that decides what they are in production, so the route does not have to.
//! Everything here is a *binding* — the logic it feeds is ported and probe-verified
//! elsewhere.
//!
//! # What is faithful, and what is not
//!
//! The five settings structs carry the oracle's own defaults and are used as such. The
//! ledger is a real `BudgetStore` over `<root>/.budget`, the file cache and index are the
//! real `FileStore`, and the clock is the OS zone through
//! [`crate::local_clock::system_local_now`].
//!
//! **Recorded gap:** the *env readers* for those settings are not ported. The oracle
//! reads them from `config.settings`, so a deployment that overrides e.g.
//! `CONTEXT_WINDOW_MESSAGES` or `LOCAL_RAG_BM25_K1` would get the oracle's default here
//! rather than its own value. Until those readers land, this assembly is exact for a
//! default-configured deployment and silently default-valued for an overridden one — which
//! is why it is recorded rather than glossed.
//!
//! # The file vector index: a mechanical guard, not a convention
//!
//! `FileContextDeps::search_file_chunks` returns a bare `Vec<i64>`, so it **cannot** signal
//! "no provider" itself, and returning an empty `Vec` would silently drop every indexed
//! chunk — the failure `file_store::vector_index_not_ready` exists to name. Rather than
//! trust a predicate about attachments to stay in step with
//! `expanded_message_content`'s own trigger, the injected search records that it was
//! reached, and [`NativeAssembly::with_env`] hands that flag back. A caller that sees
//! `true` refuses. The flag is set by the very call the oracle would have served, so it
//! cannot drift from the trigger the way a duplicated predicate would.

use std::cell::Cell;
use std::path::{Path, PathBuf};

use serde_json::Value;

use deepseek_policy::app_error::{AppError, codes};
use deepseek_policy::attachment_context::{
    FileContextDeps, LOCAL_RAG_EMBEDDING_DIMENSIONS, expanded_message_content, hash_text_embedding,
};
use deepseek_policy::budget_ledger::LedgerDeps;
use deepseek_policy::budget_manager::{BudgetSettings, today};
use deepseek_policy::budget_store::BudgetStore;
use deepseek_policy::context_engine::ContextEngineSettings;
use deepseek_policy::context_manager::ContextManagerSettings;
use deepseek_policy::context_taint::{ContextTaintSettings, file_context_guard_line};
use deepseek_policy::core_utils::utc_now_iso;
use deepseek_policy::dynamic_context::DynamicContextEnv;
use deepseek_policy::file_store::FileStore;
use deepseek_policy::model_router::ModelRouterSettings;

use crate::local_clock;
use crate::request_assembly::AssemblyEnv;

/// The workspace root, from `DEEPSEEK_INFRA_ROOT`.
///
/// Unlike [`crate::chat_tool_loop::ToolRoundExecutor::from_env`], which treats an unset
/// root as "no workspace" and lets the data branches report their disabled path, the
/// assembly **cannot** degrade that way: the memory store and the file cache both live
/// under the root, and reading them from somewhere else would be a silent divergence. So an
/// unset root is an error, and the message says which variable is missing.
pub const ROOT_ENV: &str = "DEEPSEEK_INFRA_ROOT";

/// The settings and store paths the assembly binds for a request.
#[derive(Debug)]
pub struct NativeAssembly {
    root: PathBuf,
    router: ModelRouterSettings,
    budget: BudgetSettings,
    taint: ContextTaintSettings,
    context_manager: ContextManagerSettings,
    engine: ContextEngineSettings,
    file_cache_dir: PathBuf,
    projects_dir: PathBuf,
    budget_dir: PathBuf,
    budget_db: PathBuf,
}

impl NativeAssembly {
    /// Bind the assembly to the server environment.
    pub fn from_env() -> Result<Self, AppError> {
        let root = std::env::var_os(ROOT_ENV)
            .map(PathBuf::from)
            .ok_or_else(|| AppError {
                message: format!(
                    "{ROOT_ENV} is not set, so the chat assembly has no workspace root to read \
                     its memory store, file cache or budget ledger from"
                ),
                code: codes::INTERNAL,
                status: 500,
            })?;
        Ok(Self::with_root(root))
    }

    /// Bind the assembly to an explicit root — what the tests use, and what a caller that
    /// already resolved the workspace passes.
    pub fn with_root(root: PathBuf) -> Self {
        Self {
            file_cache_dir: root.join(".file-cache"),
            projects_dir: root.join(".projects"),
            budget_dir: root.join(".budget"),
            budget_db: root.join(".budget").join("budget.db"),
            root,
            router: ModelRouterSettings::default(),
            budget: BudgetSettings::default(),
            taint: ContextTaintSettings::default(),
            context_manager: ContextManagerSettings::default(),
            engine: ContextEngineSettings::default(),
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Run `f` against an [`AssemblyEnv`] bound to this assembly.
    ///
    /// Returns `(f's result, file_vector_index_consulted)`. The second value is `true` only
    /// when the expander actually reached for the index, and a caller **must** refuse the
    /// turn in that case — see the module docs. It is returned rather than raised because
    /// the refusal belongs to the route, which decides what a request may do; this module
    /// only reports what the oracle would have needed.
    ///
    /// A closure rather than a returned struct: `AssemblyEnv` borrows a ledger and an
    /// expander that borrow *their* stores, so the whole graph has to live on one stack
    /// frame, and `with_env` is that frame.
    pub fn with_env<T>(&self, f: impl FnOnce(&AssemblyEnv<'_>) -> T) -> (T, bool) {
        let file_store = FileStore::new(&self.file_cache_dir, &self.projects_dir);
        let budget_store = BudgetStore::new(&self.budget_dir, &self.budget_db);
        let consulted = Cell::new(false);

        let load_cached_file = |file_id: &str, project_id: Option<&str>| {
            file_store.load_cached_file(file_id, project_id)
        };
        // Reaching this closure **is** the refusal condition; the empty vector is never
        // served, because the flag it sets is what the caller acts on.
        let search_file_chunks =
            |_file_id: &str, _project_id: &str, _query: &str, _limit: usize| {
                consulted.set(true);
                Vec::new()
            };
        let embed = |text: &str| hash_text_embedding(text, LOCAL_RAG_EMBEDDING_DIMENSIONS);
        let deps = FileContextDeps {
            load_cached_file: &load_cached_file,
            search_file_chunks: &search_file_chunks,
            embed: &embed,
            guard_line: file_context_guard_line(&self.taint),
        };
        let expander = |message: &Value| expanded_message_content(message, &deps);

        let today_string = today(now_epoch_seconds());
        let now_iso = utc_now_iso(now_epoch_seconds());
        let read_spend_row = |scope: &str, day: &str| budget_store.read_spend_row(scope, day);
        let write_spend_row = |row: &Value| budget_store.write_spend_row(row);
        let ledger = LedgerDeps {
            database_present: budget_store.database_present(),
            database_path: budget_store.database_path(),
            day: today_string,
            now_iso,
            read_spend_row: &read_spend_row,
            write_spend_row: &write_spend_row,
        };

        let dynamic = DynamicContextEnv::new(local_now());
        let api_key_fallback = deepseek_api_key_fallback();
        let env = AssemblyEnv {
            api_key_fallback: &api_key_fallback,
            expander: &expander,
            router: &self.router,
            budget: &self.budget,
            ledger: &ledger,
            taint: &self.taint,
            context_manager: &self.context_manager,
            engine: &self.engine,
            dynamic: &dynamic,
        };
        let outcome = f(&env);
        (outcome, consulted.get())
    }
}

/// The server-side upstream key, from `DEEPSEEK_API_KEY`.
///
/// The oracle's `preflight_deepseek_payload` falls back to `settings.deepseek_api_key`, so
/// this is the same source. It is returned as a `String` and borrowed by the caller in
/// [`NativeAssembly::with_env`] because `AssemblyEnv` takes a `&str`.
fn deepseek_api_key_fallback() -> String {
    std::env::var("DEEPSEEK_API_KEY").unwrap_or_default()
}

/// The machine's zone at the current instant, falling back to UTC.
///
/// The oracle cannot fail here — it asks the OS through `zoneinfo` and a failure is a
/// process-level error — so a failure to read the zone is degraded to UTC rather than
/// raised, which keeps the assembly's shape the same on every host. The zone name is left
/// empty in that case so the rendered context says "UTC" rather than inventing a label.
fn local_now() -> deepseek_policy::dynamic_context::LocalNow {
    match local_clock::system_local_now() {
        Ok(now) => now,
        Err(_) => deepseek_policy::dynamic_context::LocalNow {
            epoch_seconds: now_epoch_seconds(),
            offset_seconds: 0,
            timezone_name: "UTC".to_string(),
        },
    }
}

fn now_epoch_seconds() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs() as i64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    struct Scratch {
        root: PathBuf,
    }

    impl Scratch {
        fn new(tag: &str) -> Self {
            let root = std::env::temp_dir().join(format!(
                "native-assembly-{tag}-{}-{:?}",
                std::process::id(),
                std::thread::current().id()
            ));
            let _ = std::fs::remove_dir_all(&root);
            std::fs::create_dir_all(&root).unwrap();
            Self { root }
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    fn the_root_subdirectories_are_the_oracles_own() {
        let scratch = Scratch::new("paths");
        let assembly = NativeAssembly::with_root(scratch.root.clone());
        assert_eq!(assembly.root(), scratch.root.as_path());
        assert_eq!(assembly.file_cache_dir, scratch.root.join(".file-cache"));
        assert_eq!(assembly.projects_dir, scratch.root.join(".projects"));
        assert_eq!(
            assembly.budget_db,
            scratch.root.join(".budget").join("budget.db")
        );
        // The settings carry the oracle's defaults.
        assert_eq!(assembly.router.default_model, "deepseek-v4-pro");
        assert!(assembly.taint.enabled);
    }

    #[test]
    fn an_unset_root_is_an_error_that_names_the_variable() {
        let saved = std::env::var_os(ROOT_ENV);
        unsafe { std::env::remove_var(ROOT_ENV) };
        let error = NativeAssembly::from_env().expect_err("no root, no assembly");
        assert!(error.message.contains(ROOT_ENV), "{}", error.message);
        assert_eq!(error.code, codes::INTERNAL);
        if let Some(value) = saved {
            unsafe { std::env::set_var(ROOT_ENV, value) };
        }
    }

    #[test]
    fn a_message_without_attachments_never_touches_the_file_index() {
        let scratch = Scratch::new("no-attachments");
        let assembly = NativeAssembly::with_root(scratch.root.clone());
        let (expanded, consulted) =
            assembly.with_env(|env| (env.expander)(&json!({"role": "user", "content": " hi "})));
        assert_eq!(expanded, "hi");
        assert!(!consulted, "the index was consulted for a plain message");
    }

    #[test]
    fn an_empty_attachment_list_never_touches_the_file_index() {
        let scratch = Scratch::new("empty-attachments");
        let assembly = NativeAssembly::with_root(scratch.root.clone());
        let (_, consulted) = assembly.with_env(|env| {
            (env.expander)(&json!({"role": "user", "content": "hi", "attachments": []}))
        });
        assert!(!consulted);
    }

    #[test]
    fn a_file_attachment_records_the_index_consultation_the_caller_must_refuse_on() {
        let scratch = Scratch::new("file-attachment");
        let assembly = NativeAssembly::with_root(scratch.root.clone());

        // Reaching the vector search needs a cached file whose chunks exceed
        // `min(FILE_FULL_CONTEXT_LIMIT, char_budget)` — below that
        // `select_file_chunk_indices` returns every chunk and never asks the index. So the
        // fixture is deliberately large: 7 x 10 000 characters, over the 60 000 limit.
        // That the threshold is narrow is the point of the flag design: the route does not
        // have to model it, only report it.
        let file_id = "a".repeat(32);
        let chunk = "x".repeat(10_000);
        std::fs::create_dir_all(scratch.root.join(".file-cache")).unwrap();
        let document = json!({
            "id": file_id,
            "name": "报告.pdf",
            "kind": "text",
            "chunks": (0..7).map(|index| json!({"index": index, "text": chunk})).collect::<Vec<_>>(),
        });
        std::fs::write(
            scratch
                .root
                .join(".file-cache")
                .join(format!("{file_id}.json")),
            serde_json::to_string(&document).unwrap(),
        )
        .unwrap();

        let (_, consulted) = assembly.with_env(|env| {
            (env.expander)(&json!({
                "role": "user",
                "content": "看这个文件",
                "attachments": [{"fileId": file_id, "name": "报告.pdf"}],
            }))
        });
        assert!(
            consulted,
            "the file index was not consulted, so the guard cannot fire"
        );
    }

    #[test]
    fn a_file_attachment_too_small_to_need_the_index_does_not_trip_the_guard() {
        let scratch = Scratch::new("small-attachment");
        let assembly = NativeAssembly::with_root(scratch.root.clone());
        let file_id = "b".repeat(32);
        std::fs::create_dir_all(scratch.root.join(".file-cache")).unwrap();
        let document = json!({
            "id": file_id,
            "name": "小文件.txt",
            "kind": "text",
            "chunks": [{"index": 0, "text": "短内容"}],
        });
        std::fs::write(
            scratch
                .root
                .join(".file-cache")
                .join(format!("{file_id}.json")),
            serde_json::to_string(&document).unwrap(),
        )
        .unwrap();

        let (expanded, consulted) = assembly.with_env(|env| {
            (env.expander)(&json!({
                "role": "user",
                "content": "看这个文件",
                "attachments": [{"fileId": file_id, "name": "小文件.txt"}],
            }))
        });
        // The whole file fits, so the index is never asked and the guard stays down:
        // refusing here would refuse a request the oracle serves in full.
        assert!(!consulted, "the index was consulted for a file that fits");
        assert!(expanded.contains("短内容"), "{expanded}");
    }

    #[test]
    fn the_ledger_is_bound_to_the_workspace_budget_database() {
        let scratch = Scratch::new("ledger");
        let assembly = NativeAssembly::with_root(scratch.root.clone());
        let ((spend, error), consulted) = assembly.with_env(|env| {
            assert!(env.ledger.database_path.ends_with("budget.db"));
            deepseek_policy::budget_ledger::daily_spend("global", None, env.ledger)
        });
        assert!(!consulted);
        assert!(error.is_none());
        // No database on disk yet, so the oracle's empty view comes back.
        assert_eq!(spend["promptTokens"], json!(0));
        assert_eq!(spend["scope"], json!("global"));
    }
}
