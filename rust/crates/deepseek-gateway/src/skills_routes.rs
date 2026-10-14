//! Authenticated Skill System API. All execution is native.
use axum::{
    Json, Router,
    body::Bytes,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::post,
};
use deepseek_policy::{
    app_error::codes,
    core_utils::web_truthy,
    skills::{
        self, analytics, catalog, eval, registry::Registry, runner, schema, security, versioning,
    },
};
use serde_json::{Value, json};

pub fn router() -> Router {
    Router::new()
        .route("/api/skills", post(api_skills))
        .route("/api/skills/:skill_id/run", post(api_skill_run))
        .with_state(Registry::from_env())
}
pub fn app_error(e: deepseek_policy::app_error::AppError) -> Response {
    (
        StatusCode::from_u16(e.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        Json(json!({"error":e.message,"code":e.code})),
    )
        .into_response()
}
pub fn boolean(payload: &Value, key: &str, default: bool) -> bool {
    payload
        .get(key)
        .map(|v| v.as_bool().unwrap_or_else(|| web_truthy(Some(v))))
        .unwrap_or(default)
}
pub fn id(payload: &Value, key: &str) -> skills::Result<String> {
    let value = skills::text(payload, key);
    let value = if value.is_empty() {
        skills::text(payload, "id")
    } else {
        value
    };
    if value.trim().is_empty() {
        Err(skills::error(format!("{key} is required"), 400))
    } else {
        Ok(value.trim().into())
    }
}
/// The catalog's item id: `itemId`, else `skillId`, else `packId`, else `id`.
///
/// `_item_id` is one `or` chain, not a per-key strip: a whitespace-only `itemId` is truthy in
/// Python, so it is *chosen* and then strips to empty, which raises instead of falling through to
/// `skillId`. Skipping a key here only happens when the value is falsy.
pub fn item_id(payload: &Value) -> skills::Result<String> {
    let chosen = ["itemId", "skillId", "packId", "id"]
        .iter()
        .map(|key| skills::text(payload, key))
        .find(|value| !value.is_empty())
        .unwrap_or_default();
    let value = chosen.trim().to_string();
    if value.is_empty() {
        Err(skills::error("itemId is required", 400))
    } else {
        Ok(value)
    }
}
/// `_bool(payload, "dryRun") or _bool(payload, "preview")` — the flag that decides whether
/// `catalog_install` writes a project binding at all.
pub fn catalog_dry_run(payload: &Value) -> bool {
    boolean(payload, "dryRun", false) || boolean(payload, "preview", false)
}
pub fn config(payload: &Value, key: &str) -> skills::Result<Value> {
    for field in [key, "config"] {
        if payload[field].is_object() {
            return Ok(payload[field].clone());
        }
    }
    let mut value = payload.clone();
    for key in ["action", "overwrite"] {
        value.as_object_mut().unwrap().remove(key);
    }
    if key == "pack" {
        value.as_object_mut().unwrap().remove("onConflict");
    }
    if value.as_object().is_some_and(|m| m.is_empty()) {
        Err(skills::error(
            if key == "pack" {
                "Skill Pack config is required"
            } else {
                "Skill config is required"
            },
            400,
        ))
    } else {
        Ok(value)
    }
}
async fn api_skills(State(registry): State<Registry>, body: Bytes) -> Response {
    match parse_payload(&body) {
        Ok(payload) => dispatch(registry, payload),
        Err(error) => app_error(error),
    }
}

/// `NATIVE_SKILLS_ACTION_NOT_READY` — the native edge cannot serve this action yet.
pub const ACTION_NOT_READY: &str = "NATIVE_SKILLS_ACTION_NOT_READY";

/// The actions `deepseek_infra/web/routes/skills.py` serves and this edge does not.
///
/// They are refused by name rather than by falling into the oracle's own "Unsupported Skill
/// action": the product *does* support them, and a 5.0 topology has no Python process to serve
/// them either, so 400 would blame the request for a migration that has not happened. The
/// sequence in `tasks/native-runtime/skills-migration.md` names the phases that close them —
/// `run`, `dry_run` and the run/catalog/versioning blocks are phases 3-5, and phase 6 registers
/// every action. An action that is neither here nor implemented still gets the oracle's 400, so
/// unknown input keeps parity with Python.
/// Offline runs and evaluation are implemented. The online branch of `run` still needs the
/// native provider exchange; registering its action or passing offline tests does not complete it.
pub const ACTION_NOT_MIGRATED: [&str; 1] = ["run"];

/// The refusal both the action dispatch and `/api/skills/{skill_id}/run` answer with.
pub fn not_ready_error(action: &str) -> deepseek_policy::app_error::AppError {
    deepseek_policy::app_error::AppError {
        status: 501,
        code: ACTION_NOT_READY,
        message: format!(
            "The native skills edge does not serve `{action}` yet; Python is still the only runtime that does"
        ),
    }
}

/// `/api/skills/{skill_id}/run` is the same runner as action `run`, with the path id winning.
async fn api_skill_run(
    State(registry): State<Registry>,
    Path(skill_id): Path<String>,
    body: Bytes,
) -> Response {
    let mut payload = match parse_payload(&body) {
        Ok(payload) => payload,
        Err(error) => return app_error(error),
    };
    payload["skillId"] = json!(skill_id);
    payload["action"] = json!("run");
    dispatch(registry, payload)
}

fn parse_payload(body: &Bytes) -> skills::Result<Value> {
    if body.is_empty() {
        return Err(skills::error("Request body is empty", 400));
    }
    if body.len() > 2_000_000 {
        return Err(deepseek_policy::app_error::AppError {
            message: "Request body is too large".into(),
            code: codes::UPLOAD_TOO_LARGE,
            status: 413,
        });
    }
    let payload: Value = serde_json::from_slice(body)
        .map_err(|error| skills::error(format!("Invalid JSON: {error}"), 400))?;
    if !payload.is_object() {
        return Err(skills::error("Request body must be a JSON object", 400));
    }
    Ok(payload)
}

fn dispatch(registry: Registry, payload: Value) -> Response {
    let result = (|| {
        let action = skills::text(&payload, "action");
        let action = if action.is_empty() { "list" } else { &action }
            .trim()
            .to_lowercase();
        let writes = matches!(
            action.as_str(),
            "create"
                | "import"
                | "update"
                | "enable"
                | "disable"
                | "delete"
                | "import_pack"
                | "delete_pack"
                | "trust_skill"
                | "untrust_skill"
                | "block_skill"
                | "catalog_refresh"
                | "delete_run"
                | "cleanup_runs"
                | "redact_run"
                | "rollback_skill"
                | "rollback_pack"
                | "create_eval_case"
                | "delete_eval_case"
                // These five run the eval corpus, which appends to the run journal.
                | "eval_report"
                | "eval_upgrade_gate"
                | "upgrade_pack"
                | "diff_versions"
                | "diff_pack_versions"
        ) || (action == "security_review"
            && !payload["skill"].is_object()
            && !payload["config"].is_object())
            || (action == "security_review_pack"
                && !payload["pack"].is_object()
                && !payload["config"].is_object());
        if (writes || action == "run") && !crate::may_write_native_store("skills_store") {
            return Err(deepseek_policy::app_error::AppError {
                message: "The skills store is still written by the Python runtime, so this gateway refuses to mutate it.".into(),
                code: "NATIVE_SKILLS_WRITE_NOT_OWNED",
                status: 409,
            });
        }
        // A catalog install binds the skill or pack to a project, and that binding lives in
        // `project.json` — a different store with a different cutover, so it carries its own
        // refusal. `dryRun` (or `preview`) returns before the oracle writes anything, so that
        // branch stays available while Python still owns the project store.
        let writes_project = action == "catalog_uninstall"
            || (action == "catalog_install" && !catalog_dry_run(&payload));
        if writes_project && !crate::may_write_native_store("project_metadata_store") {
            return Err(deepseek_policy::app_error::AppError {
                message: "project.json is still written by the Python runtime, so this gateway refuses to write it.".into(),
                code: crate::PROJECT_METADATA_WRITE_NOT_OWNED,
                status: 409,
            });
        }
        // A run or evaluation can append traces and artifacts before returning an error.
        // Check all owners before preparation so a partial handover cannot cause partial effects.
        let executes = matches!(
            action.as_str(),
            "run"
                | "eval_report"
                | "eval_upgrade_gate"
                | "upgrade_pack"
                | "diff_versions"
                | "diff_pack_versions"
        );
        if executes
            && [
                "observability_trace_store",
                "generated_files_store",
                "project_metadata_store",
            ]
            .iter()
            .any(|domain| !crate::may_write_native_store(domain))
        {
            return Err(deepseek_policy::app_error::AppError {
                message: "The skills store is still written by the Python runtime, so this gateway refuses to mutate it.".into(),
                code: "NATIVE_SKILLS_WRITE_NOT_OWNED",
                status: 409,
            });
        }
        execute(&registry, &payload, &action)
    })();
    match result {
        Ok(value) => Json(value).into_response(),
        Err(error) => app_error(error),
    }
}

fn execute(registry: &Registry, payload: &Value, action: &str) -> skills::Result<Value> {
    match action {
        "list" => Ok(json!({
            "ok": true,
            "skills": registry.list(boolean(payload, "includeDisabled", false), false)?,
        })),
        "builtin" => Ok(json!({
            "ok": true,
            "skills": registry.list(boolean(payload, "includeDisabled", true), true)?,
        })),
        "get" => Ok(json!({"ok": true, "skill": registry.get(&id(payload, "skillId")?, true)?})),
        "export" => Ok(json!({"ok": true, "skill": registry.export(&id(payload, "skillId")?)?})),
        "validate" => {
            Ok(json!({"ok": true, "skill": schema::validate_skill(&config(payload, "skill")?)?}))
        }
        "create" | "import" => Ok(json!({
            "ok": true,
            "skill": registry.create(&config(payload, "skill")?, boolean(payload, "overwrite", false))?,
        })),
        "update" => {
            let patch = ["patch", "skill", "config"]
                .iter()
                .find_map(|key| payload[*key].as_object().map(|_| payload[*key].clone()))
                .unwrap_or_else(|| {
                    let mut patch = payload.clone();
                    for key in ["action", "skillId", "id"] {
                        patch.as_object_mut().unwrap().remove(key);
                    }
                    patch
                });
            Ok(json!({"ok": true, "skill": registry.update(&id(payload, "skillId")?, &patch)?}))
        }
        "enable" | "disable" => Ok(json!({
            "ok": true,
            "skill": registry.set_disabled(&id(payload, "skillId")?, action == "disable")?,
        })),
        "delete" => registry.delete(&id(payload, "skillId")?),
        "run" => run_skill(registry, payload, &id(payload, "skillId")?),
        // The oracle's `dry_run` takes the Skill **configuration** from the request and never looks
        // an id up, so a `{"skillId": …}`-only payload is the oracle's own
        // `400 "Skill config missing required fields: …"` here too.
        "dry_run" => Ok(runner::dry_run(
            registry,
            &schema::validate_skill(&config(payload, "skill")?)?,
            &run_input(payload),
        )?),
        "list_runs" => Ok(json!({
            "ok": true,
            "skillRuns": analytics::list(registry, payload, run_limit(payload)),
        })),
        "get_run" => Ok(json!({
            "ok": true,
            "skillRun": analytics::get(registry, &run_record_id(payload)?)?,
        })),
        // The run journal. Three of these write it — it is the store the offline `run` already
        // appends to, so the route's `skills_store` gate covers them with the rest — and the other
        // two only read.
        "delete_run" => analytics::delete(registry, &run_record_id(payload)?),
        "cleanup_runs" => analytics::cleanup(registry, payload, limit_of(payload, "keepRecent", 0)),
        "redact_run" => analytics::redact(registry, &run_record_id(payload)?),
        "export_runs" => Ok(json!({
            "ok": true,
            "skillRuns": analytics::list(registry, payload, 0),
            "summary": analytics::summary(registry, payload, summary_days(payload)),
        })),
        "analytics_summary" => Ok(json!({
            "ok": true,
            "summary": analytics::summary(registry, payload, summary_days(payload)),
        })),
        // The security overview and the version family. `security_summary` and the five reads answer
        // whatever owns the stores; `rollback_skill` / `rollback_pack` write the item, its history
        // revision and possibly a project binding, so they carry the skills-store gate.
        "security_summary" => {
            let scope = skills::text(payload, "scope");
            security::summary(
                registry,
                if scope.is_empty() {
                    "all"
                } else {
                    scope.as_str()
                },
            )
        }
        "list_versions" => Ok(json!({
            "ok": true,
            "versions": versioning::list(registry, &id(payload, "skillId")?, false)?,
        })),
        "rollback_skill" => versioning::rollback(
            registry,
            &id(payload, "skillId")?,
            &required_version(payload)?,
            false,
            "",
            &skills::text(payload, "changeSummary"),
        ),
        "migration_plan" => Ok(json!({
            "ok": true,
            "migrationPlan": versioning::migration_plan(
                registry,
                &id(payload, "skillId")?,
                &bound_version(payload, "from"),
                &bound_version(payload, "to"),
            )?,
        })),
        "list_pack_versions" => Ok(json!({
            "ok": true,
            "versions": versioning::list(registry, &id(payload, "packId")?, true)?,
        })),
        "rollback_pack" => versioning::rollback(
            registry,
            &id(payload, "packId")?,
            &required_version(payload)?,
            true,
            &skills::text(payload, "projectId"),
            &skills::text(payload, "changeSummary"),
        ),
        // The eval **case store**. `list_eval_cases` reads the golden corpus and
        // `.skills/eval_cases.jsonl`; the other two write that file, so they carry the skills-store
        // gate. `eval_report` below also executes the corpus and writes its run results.
        "list_eval_cases" => Ok(json!({"ok": true, "cases": eval::cases(registry)})),
        "create_eval_case" => Ok(json!({
            "ok": true,
            "case": eval::save(registry, &eval_case(payload)?)?,
        })),
        "delete_eval_case" => eval::delete(registry, &id(payload, "caseId")?),
        // The report engine and the four actions that embed its verdict. All five **run** the
        // corpus — each case persists a run and may create its own eval project — so they are
        // writers against both stores, not reads.
        "eval_report" => {
            let given = skills::text(payload, "version");
            let version = if given.is_empty() {
                catalog::APP_VERSION.trim().to_string()
            } else {
                given
            };
            eval::report(
                registry,
                &version,
                &scope_or_all(payload),
                &skills::text(payload, "skillId"),
                &skills::text(payload, "packId"),
                &baseline(payload),
                None,
            )
            .map(|built| json!({"ok": true, "report": built}))
        }
        "eval_upgrade_gate" => {
            let kind = skills::text(payload, "kind");
            let kind = if kind.is_empty() {
                "skill".to_string()
            } else {
                kind
            };
            eval::upgrade_gate_for(registry, &kind, &gate_item_id(payload), &baseline(payload))
                .map(|gate| json!({"ok": true, "gate": gate}))
        }
        "upgrade_pack" => {
            // The oracle computes the gate from the pack's own id before it applies the upgrade, so
            // the gateway has to as well — `versioning::upgrade_pack` takes it as an argument.
            let pack_id = id(payload, "packId")?;
            let resolved = {
                let from_pack = skills::text(&registry.get_pack(&pack_id)?, "packId");
                if from_pack.is_empty() {
                    pack_id.clone()
                } else {
                    from_pack
                }
            };
            let gate = eval::upgrade_gate_for(registry, "pack", &resolved, &baseline(payload))?;
            versioning::upgrade_pack(
                registry,
                &pack_id,
                &optional_version(payload),
                &skills::text(payload, "projectId"),
                gate,
            )
        }
        "diff_versions" => {
            let skill_id = id(payload, "skillId")?;
            let eval_score = eval::score_diff(registry, "skill", &skill_id);
            Ok(json!({
                "ok": true,
                "diff": versioning::diff(
                    registry,
                    &skill_id,
                    &bound_version(payload, "from"),
                    &bound_version(payload, "to"),
                    false,
                    eval_score,
                )?,
            }))
        }
        "diff_pack_versions" => {
            let pack_id = id(payload, "packId")?;
            let eval_score = eval::score_diff(registry, "pack", &pack_id);
            Ok(json!({
                "ok": true,
                "diff": versioning::diff(
                    registry,
                    &pack_id,
                    &bound_version(payload, "from"),
                    &bound_version(payload, "to"),
                    true,
                    eval_score,
                )?,
            }))
        }
        // The catalog. Only `catalog_refresh` writes the skills store and only
        // `catalog_install` / `catalog_uninstall` write a project binding — the other five are
        // reads, and `catalog_install` with `dryRun`/`preview` returns before it writes anything.
        "catalog_list" => {
            let catalog_manifest = catalog::manifest(registry)?;
            Ok(json!({
                "ok": true,
                "catalog": catalog_manifest,
                "items": catalog_manifest["items"],
            }))
        }
        "catalog_get" => Ok(json!({
            "ok": true,
            "item": catalog::get(registry, &item_id(payload)?)?,
        })),
        "catalog_search" => catalog::search(
            registry,
            &skills::text(payload, "query"),
            &if payload["filters"].is_object() {
                payload["filters"].clone()
            } else {
                json!({})
            },
        ),
        "catalog_install" => catalog::install(
            registry,
            &item_id(payload)?,
            &skills::text(payload, "projectId"),
            boolean(payload, "securityApproved", false)
                || boolean(payload, "approveSecurityReview", false),
            catalog_dry_run(payload),
        ),
        "catalog_uninstall" => catalog::uninstall(
            registry,
            &item_id(payload)?,
            &skills::text(payload, "projectId"),
        ),
        "catalog_refresh" => catalog::refresh(registry),
        "catalog_export" => catalog::export(registry),
        "list_packs" => Ok(json!({
            "ok": true,
            "packs": registry.packs(boolean(payload, "includeBuiltin", true))?,
        })),
        "get_pack" => Ok(json!({"ok": true, "pack": registry.get_pack(&id(payload, "packId")?)?})),
        "export_pack" => {
            Ok(json!({"ok": true, "pack": registry.export_pack(&id(payload, "packId")?)?}))
        }
        "validate_pack" => {
            let pack = schema::validate_pack(&config(payload, "pack")?)?;
            Ok(
                json!({"ok": true, "toolPermissions": schema::tool_permissions(&pack), "pack": pack}),
            )
        }
        "import_pack" => registry.import_pack(
            &config(payload, "pack")?,
            boolean(payload, "overwrite", false),
            &skills::text(payload, "onConflict"),
        ),
        "delete_pack" => registry.delete_pack(&id(payload, "packId")?),
        "security_review" | "security_review_pack" => {
            let pack = action == "security_review_pack";
            let key = if pack { "pack" } else { "skill" };
            let supplied = payload[key].is_object() || payload["config"].is_object();
            let current = if supplied {
                config(payload, key)?
            } else if pack {
                let id = id(payload, "packId")?;
                let mut exported = registry.export_pack(&id)?;
                exported["builtin"] = registry.get_pack(&id)?["builtin"].clone();
                exported
            } else {
                registry.get(&id(payload, "skillId")?, true)?
            };
            Ok(
                json!({"ok": true, "review": security::review(registry, &current, pack, !supplied)?}),
            )
        }
        "trust_skill" | "untrust_skill" | "block_skill" => security::change_trust(
            registry,
            &id(payload, "skillId")?,
            action.split('_').next().unwrap(),
            &skills::text(payload, "reason"),
            false,
        ),
        other if ACTION_NOT_MIGRATED.contains(&other) => Err(not_ready_error(other)),
        _ => Err(skills::error("Unsupported Skill action", 400)),
    }
}

fn run_skill(registry: &Registry, payload: &Value, skill_id: &str) -> skills::Result<Value> {
    let input = run_input(payload);
    if boolean(payload, "offline", false) {
        let mut options = payload.clone();
        options["offline"] = json!(true);
        return runner::offline(registry, skill_id, &input, &options);
    }
    let key = {
        let supplied = skills::text(payload, "apiKey");
        if supplied.trim().is_empty() {
            std::env::var("DEEPSEEK_API_KEY").unwrap_or_default()
        } else {
            supplied
        }
    };
    if key.trim().is_empty() {
        let prepared = runner::prepare(registry, skill_id, &input, payload)?;
        return prepared.finish(
            registry,
            Err(deepseek_policy::app_error::AppError {
                message:
                    "Missing DeepSeek API Key. Set DEEPSEEK_API_KEY or enter a key in settings."
                        .into(),
                code: codes::MISSING_API_KEY,
                status: 400,
            }),
        );
    }
    Err(not_ready_error("run"))
}

fn run_input(payload: &Value) -> Value {
    for key in ["input", "inputData", "inputs"] {
        if payload[key].is_object() {
            return payload[key].clone();
        }
    }
    json!({})
}

/// `_limit(payload, key, default)`: `int(str(raw))`, the default when that fails, clamped to
/// 0..=1000.
///
/// `int("5.0")` and `int("True")` both raise in Python, so a JSON float or a boolean takes the
/// default rather than truncating.
fn limit_of(payload: &Value, key: &str, default: i64) -> usize {
    let raw = payload
        .get(key)
        .map(deepseek_policy::python_json::value_str)
        .unwrap_or_default();
    analytics::request_limit(&raw, default.clamp(0, 1000) as usize)
}
fn run_limit(payload: &Value) -> usize {
    limit_of(payload, "limit", 50)
}
/// `_analytics_summary`'s window: `_limit(payload, "days", 7)`. The `or 7` that makes a zero mean a
/// week lives in the policy function, where the oracle also puts it.
fn summary_days(payload: &Value) -> usize {
    limit_of(payload, "days", 7)
}

/// `_optional_version`: `version`, else `revisionId`, trimmed.
fn optional_version(payload: &Value) -> String {
    let value = skills::text(payload, "version");
    let value = if value.is_empty() {
        skills::text(payload, "revisionId")
    } else {
        value
    };
    value.trim().to_string()
}
/// `_version`: the optional one, or the oracle's refusal.
fn required_version(payload: &Value) -> skills::Result<String> {
    let value = optional_version(payload);
    if value.is_empty() {
        Err(skills::error("version is required", 400))
    } else {
        Ok(value)
    }
}
/// `_from_version` / `_to_version`: `from`, else `fromVersion`, else `current` — the default applies
/// after the strip, so a whitespace-only value also becomes `current`.
fn bound_version(payload: &Value, key: &str) -> String {
    let value = skills::text(payload, key);
    let value = if value.is_empty() {
        skills::text(payload, &format!("{key}Version"))
    } else {
        value
    };
    let value = value.trim().to_string();
    if value.is_empty() {
        "current".into()
    } else {
        value
    }
}
/// `_analytics_summary`'s and `eval_report`'s scope: the payload's, else `all`.
fn scope_or_all(payload: &Value) -> String {
    let scope = skills::text(payload, "scope");
    if scope.is_empty() {
        "all".to_string()
    } else {
        scope
    }
}
/// `payload.get("baseline") if isinstance(..., dict) else None` — every caller then does `or {}`,
/// so an absent or non-object baseline is the empty object.
fn baseline(payload: &Value) -> Value {
    if payload["baseline"].is_object() {
        payload["baseline"].clone()
    } else {
        json!({})
    }
}
/// `eval_upgrade_gate`'s item id: `itemId`, else `skillId`, else `packId`, else empty — an empty id
/// is allowed through, and the report then fails on the Skill it cannot find.
fn gate_item_id(payload: &Value) -> String {
    ["itemId", "skillId", "packId"]
        .iter()
        .map(|key| skills::text(payload, key))
        .find(|value| !value.is_empty())
        .unwrap_or_default()
}
/// `_eval_case`: the `case` object, else the payload without its `action`.
fn eval_case(payload: &Value) -> skills::Result<Value> {
    if payload["case"].is_object() {
        return Ok(payload["case"].clone());
    }
    let candidate: serde_json::Map<String, Value> = payload
        .as_object()
        .map(|fields| {
            fields
                .iter()
                .filter(|(key, _)| key.as_str() != "action")
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect()
        })
        .unwrap_or_default();
    if candidate.is_empty() {
        Err(skills::error("Skill eval case is required", 400))
    } else {
        Ok(Value::Object(candidate))
    }
}
fn run_record_id(payload: &Value) -> skills::Result<String> {
    let value = skills::text(payload, "skillRunId");
    let value = if value.is_empty() {
        skills::text(payload, "runId")
    } else {
        value
    };
    if value.trim().is_empty() {
        Err(skills::error("skillRunId is required", 400))
    } else {
        Ok(value.trim().into())
    }
}
