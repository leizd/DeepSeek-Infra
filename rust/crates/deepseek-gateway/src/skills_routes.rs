//! Authenticated Skill System API. All execution is native.
use axum::{
    Json, Router,
    body::Bytes,
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::post,
};
use deepseek_policy::{
    core_utils::web_truthy,
    skills::{self, registry::Registry, schema, security},
};
use serde_json::{Value, json};

pub fn router() -> Router {
    Router::new()
        .route("/api/skills", post(api_skills))
        .route("/api/skills/:skill_id/run", post(skill_run_not_ready))
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
    let result = (|| {
        if body.is_empty() {
            return Err(skills::error("Request body is empty", 400));
        }
        if body.len() > 2_000_000 {
            return Err(deepseek_policy::app_error::AppError {
                message: "Request body is too large".into(),
                code: "upload_too_large",
                status: 413,
            });
        }
        let payload: Value = serde_json::from_slice(&body)
            .map_err(|e| skills::error(format!("Invalid JSON: {e}"), 400))?;
        if !payload.is_object() {
            return Err(skills::error("Request body must be a JSON object", 400));
        }
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
        ) || (action == "security_review"
            && !payload["skill"].is_object()
            && !payload["config"].is_object())
            || (action == "security_review_pack"
                && !payload["pack"].is_object()
                && !payload["config"].is_object());
        if writes && !crate::may_write_native_store("skills_store") {
            return Err(deepseek_policy::app_error::AppError {message:"The skills store is still written by the Python runtime, so this gateway refuses to mutate it.".into(),code:"NATIVE_SKILLS_WRITE_NOT_OWNED",status:409});
        }
        match action.as_str() {
            "list" => Ok(
                json!({"ok":true,"skills":registry.list(boolean(&payload,"includeDisabled",false),false)?}),
            ),
            "builtin" => Ok(
                json!({"ok":true,"skills":registry.list(boolean(&payload,"includeDisabled",true),true)?}),
            ),
            "get" => Ok(json!({"ok":true,"skill":registry.get(&id(&payload,"skillId")?,true)?})),
            "export" => Ok(json!({"ok":true,"skill":registry.export(&id(&payload,"skillId")?)?})),
            "validate" => {
                Ok(json!({"ok":true,"skill":schema::validate_skill(&config(&payload,"skill")?)?}))
            }
            "create" | "import" => Ok(
                json!({"ok":true,"skill":registry.create(&config(&payload,"skill")?,boolean(&payload,"overwrite",false))?}),
            ),
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
                Ok(json!({"ok":true,"skill":registry.update(&id(&payload,"skillId")?,&patch)?}))
            }
            "enable" | "disable" => Ok(
                json!({"ok":true,"skill":registry.set_disabled(&id(&payload,"skillId")?,action=="disable")?}),
            ),
            "delete" => registry.delete(&id(&payload, "skillId")?),
            "list_packs" => Ok(
                json!({"ok":true,"packs":registry.packs(boolean(&payload,"includeBuiltin",true))?}),
            ),
            "get_pack" => Ok(json!({"ok":true,"pack":registry.get_pack(&id(&payload,"packId")?)?})),
            "export_pack" => {
                Ok(json!({"ok":true,"pack":registry.export_pack(&id(&payload,"packId")?)?}))
            }
            "validate_pack" => {
                let pack = schema::validate_pack(&config(&payload, "pack")?)?;
                Ok(json!({"ok":true,"toolPermissions":schema::tool_permissions(&pack),"pack":pack}))
            }
            "import_pack" => registry.import_pack(
                &config(&payload, "pack")?,
                boolean(&payload, "overwrite", false),
                &skills::text(&payload, "onConflict"),
            ),
            "delete_pack" => registry.delete_pack(&id(&payload, "packId")?),
            "security_review" | "security_review_pack" => {
                let pack = action == "security_review_pack";
                let key = if pack { "pack" } else { "skill" };
                let supplied = payload[key].is_object() || payload["config"].is_object();
                let current = if supplied {
                    config(&payload, key)?
                } else if pack {
                    let id = id(&payload, "packId")?;
                    let mut exported = registry.export_pack(&id)?;
                    exported["builtin"] = registry.get_pack(&id)?["builtin"].clone();
                    exported
                } else {
                    registry.get(&id(&payload, "skillId")?, true)?
                };
                Ok(json!({"ok":true,"review":security::review(&registry,&current,pack,!supplied)?}))
            }
            "trust_skill" | "untrust_skill" | "block_skill" => security::change_trust(
                &registry,
                &id(&payload, "skillId")?,
                action.split('_').next().unwrap(),
                &skills::text(&payload, "reason"),
                false,
            ),
            other if ACTION_NOT_MIGRATED.contains(&other) => Err(not_ready_error(other)),
            _ => Err(skills::error("Unsupported Skill action", 400)),
        }
    })();
    match result {
        Ok(value) => Json(value).into_response(),
        Err(e) => app_error(e),
    }
}

/// `NATIVE_SKILLS_ACTION_NOT_READY` — the native edge cannot serve this action yet.
pub const ACTION_NOT_READY: &str = "NATIVE_SKILLS_ACTION_NOT_READY";

/// The 30 actions `deepseek_infra/web/routes/skills.py` serves and this edge does not.
///
/// They are refused by name rather than by falling into the oracle's own "Unsupported Skill
/// action": the product *does* support them, and a 5.0 topology has no Python process to serve
/// them either, so 400 would blame the request for a migration that has not happened. The
/// sequence in `tasks/native-runtime/skills-migration.md` names the phases that close them —
/// `run`, `dry_run` and the run/catalog/versioning blocks are phases 3-5, and phase 6 registers
/// every action. An action that is neither here nor implemented still gets the oracle's 400, so
/// unknown input keeps parity with Python.
pub const ACTION_NOT_MIGRATED: [&str; 30] = [
    // Runners (phase 4).
    "run",
    "dry_run",
    // Evaluation cases and reports (phase 5).
    "eval_report",
    "list_eval_cases",
    "create_eval_case",
    "delete_eval_case",
    // Security summary (phase 2).
    "security_summary",
    // Catalog (phase 3).
    "catalog_list",
    "catalog_get",
    "catalog_search",
    "catalog_install",
    "catalog_uninstall",
    "catalog_refresh",
    "catalog_export",
    // Run analytics (phase 3).
    "list_runs",
    "get_run",
    "delete_run",
    "cleanup_runs",
    "redact_run",
    "export_runs",
    "analytics_summary",
    // Version diff, rollback and upgrade gates (phase 5).
    "list_versions",
    "diff_versions",
    "rollback_skill",
    "migration_plan",
    "list_pack_versions",
    "diff_pack_versions",
    "upgrade_pack",
    "rollback_pack",
    "eval_upgrade_gate",
];

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

fn action_not_ready(action: &str) -> Response {
    app_error(not_ready_error(action))
}

/// `/api/skills/{skill_id}/run` is registered so the path answers the same precise refusal
/// instead of falling through to the Go control proxy, which owns no part of this surface.
async fn skill_run_not_ready() -> Response {
    action_not_ready("run")
}
