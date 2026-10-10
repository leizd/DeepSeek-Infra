//! Offline probe: JSON cases in, real native skill-module results out.
use deepseek_policy::skills::{self, registry::Registry, schema, security, versioning};
use serde_json::{Value, json};
use std::io::{self, Read};
/// The string elements of a JSON array, in order.
fn ids_of(value: &Value) -> Vec<String> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .map(|item| item.as_str().unwrap_or("").to_string())
        .collect()
}

fn main() {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input).unwrap();
    let cases: Vec<Value> = serde_json::from_str(&input).unwrap();
    let mut output = Vec::new();
    for case in cases {
        let mut registry = Registry::new(case["root"].as_str().unwrap());
        registry.clock = Some(1767225600);
        let result = match case["op"].as_str().unwrap() {
            "validate" => schema::validate_skill(&case["value"]),
            "pack" => schema::validate_pack(&case["value"]),
            "tools" => schema::skill_allowed_tools(&case["value"]).map(|v| json!(v)),
            "instance" => Ok(json!(schema::validate_instance(
                &case["value"]["value"],
                &case["value"]["schema"],
                "input"
            ))),
            "normalize_run" => skills::analytics::normalize(&case["value"]),
            "review" => security::review(&registry, &case["value"], false, false),
            "review_pack" => security::review(&registry, &case["value"], true, false),
            "snapshot" => versioning::snapshot(
                &registry,
                &case["value"],
                false,
                "Created custom Skill",
                "create",
            ),
            "list" => registry.list(true, false).map(|v| json!(v)),
            "media_context" => skills::media::context(
                &registry,
                &case["value"]["input"],
                case["value"]["projectId"].as_str().unwrap_or(""),
            )
            .map(|v| json!(v)),
            // The offline run's user-visible payload. `persist: false` keeps the probe out of the
            // run journal it is also reading; the context is `""` for these inputs, which is why
            // they carry no `mediaIds` (the `media_context` cases measure that half).
            "offline_output" => {
                let options = json!({"offline": true, "persist": false});
                skills::runner::prepare(
                    &registry,
                    case["value"]["skillId"].as_str().unwrap_or(""),
                    &case["value"]["input"],
                    &options,
                )
                .map(|run| run.offline_output())
            }
            // The oracle's `dry_run` validates a Skill **configuration taken from the request**, so
            // the case carries the config; the gateway's own payload extraction is exercised by the
            // real-router test, not here.
            "dry_run" => schema::validate_skill(&case["value"]["skill"]).and_then(|skill| {
                skills::runner::dry_run(&registry, &skill, &case["value"]["input"])
            }),
            // The entry the route calls, so a refusal's **message** is compared and not only its
            // status. `offline` forces `offline: true` itself; `persist: false` keeps the refusal
            // from writing a failure record into the journal the `list_runs` cases read.
            "offline_refusal" => skills::runner::offline(
                &registry,
                case["value"]["skillId"].as_str().unwrap_or(""),
                &case["value"]["input"],
                &json!({"persist": false}),
            ),
            "list_runs" => Ok(json!(skills::analytics::list(
                &registry,
                &case["value"]["filter"],
                case["value"]["limit"].as_u64().unwrap_or(50) as usize,
            ))),
            "get_run" => skills::analytics::get(
                &registry,
                case["value"]["skillRunId"].as_str().unwrap_or(""),
            ),
            // The run-analytics family. `cleanup_runs` and `analytics_summary` take the request
            // object on this side because that is what their route helpers pass.
            "delete_run" => skills::analytics::delete(
                &registry,
                case["value"]["skillRunId"].as_str().unwrap_or(""),
            ),
            "redact_run" => skills::analytics::redact(
                &registry,
                case["value"]["skillRunId"].as_str().unwrap_or(""),
            ),
            "cleanup_runs" => skills::analytics::cleanup(
                &registry,
                &case["value"]["filter"],
                case["value"]["keepRecent"].as_u64().unwrap_or(0) as usize,
            ),
            "analytics_summary" => Ok(skills::analytics::summary(
                &registry,
                &case["value"]["payload"],
                case["value"]["days"].as_u64().unwrap_or(7) as usize,
            )),
            // The security overview and the version family.
            "security_summary" => {
                // The route resolves `scope or "all"` before the policy function sees it, so the
                // probe does the same on both sides.
                let raw = case["value"]["scope"].as_str().unwrap_or("");
                let scope = if raw.is_empty() { "all" } else { raw };
                skills::security::summary(&registry, scope)
            }
            "list_versions" => skills::versioning::list(
                &registry,
                case["value"]["itemId"].as_str().unwrap_or(""),
                false,
            ),
            "list_pack_versions" => skills::versioning::list(
                &registry,
                case["value"]["itemId"].as_str().unwrap_or(""),
                true,
            ),
            "migration_plan" => skills::versioning::migration_plan(
                &registry,
                case["value"]["itemId"].as_str().unwrap_or(""),
                case["value"]["from"].as_str().unwrap_or("current"),
                case["value"]["to"].as_str().unwrap_or("current"),
            ),
            "rollback_skill" => skills::versioning::rollback(
                &registry,
                case["value"]["itemId"].as_str().unwrap_or(""),
                case["value"]["version"].as_str().unwrap_or(""),
                false,
                "",
                case["value"]["changeSummary"].as_str().unwrap_or(""),
            ),
            "rollback_pack" => skills::versioning::rollback(
                &registry,
                case["value"]["itemId"].as_str().unwrap_or(""),
                case["value"]["version"].as_str().unwrap_or(""),
                true,
                "",
                case["value"]["changeSummary"].as_str().unwrap_or(""),
            ),
            // The eval case store. `normalize_eval_case` is the pure half; the other three read and
            // write the user case file.
            "normalize_eval_case" => Ok(skills::eval::normalize(&case["value"])),
            "list_eval_cases" => Ok(skills::eval::cases(&registry)),
            "create_eval_case" => skills::eval::save(&registry, &case["value"]["case"]),
            "delete_eval_case" => {
                skills::eval::delete(&registry, case["value"]["caseId"].as_str().unwrap_or(""))
            }
            // The pure half of the eval engine.
            "eval_json_path" => Ok(skills::eval::json_path(
                &case["value"]["value"],
                case["value"]["path"].as_str().unwrap_or(""),
            )
            .unwrap_or(Value::Null)),
            "eval_content_pass" => {
                skills::eval::content_pass(&case["value"]["output"], &case["value"]["case"])
                    .map(|passed| json!(passed))
            }
            "eval_artifact_pass" => Ok(json!(skills::eval::artifact_pass(
                &case["value"]["skill"],
                &case["value"]["case"],
                case["value"]["artifacts"]
                    .as_array()
                    .map(|v| v.as_slice())
                    .unwrap_or(&[]),
                case["value"]["savedItems"]
                    .as_array()
                    .map(|v| v.as_slice())
                    .unwrap_or(&[]),
            ))),
            "eval_sample_input" => Ok(skills::eval::sample_input(&case["value"]["schema"])),
            "eval_synthetic_case" => skills::eval::synthetic_case(
                &registry,
                case["value"]["skillId"].as_str().unwrap_or(""),
            ),
            // No `?` inside these arms: a `?` in a match-arm block returns from `main`, which is
            // why the fallible ones are written as `and_then` / `map` chains.
            "eval_selected_skill_ids" => skills::eval::selected_skill_ids(
                &registry,
                case["value"]["scope"].as_str().unwrap_or(""),
                case["value"]["skillId"].as_str().unwrap_or(""),
                case["value"]["packId"].as_str().unwrap_or(""),
            )
            .map(|ids| json!(ids)),
            "eval_pack_membership" => skills::eval::pack_membership(&registry),
            "eval_skill_results" => {
                let ids = ids_of(&case["value"]["skillIds"]);
                skills::eval::skill_results(
                    &registry,
                    case["value"]["caseResults"]
                        .as_array()
                        .map(|v| v.as_slice())
                        .unwrap_or(&[]),
                    &ids,
                    &case["value"]["packMap"],
                )
                .map(|results| json!(results))
            }
            "eval_pack_results" => {
                let selected = ids_of(&case["value"]["selected"]);
                skills::eval::pack_results(
                    &registry,
                    case["value"]["caseResults"]
                        .as_array()
                        .map(|v| v.as_slice())
                        .unwrap_or(&[]),
                    &case["value"]["packMap"],
                    &selected,
                )
                .map(|results| json!(results))
            }
            "eval_compare_reports" => Ok(skills::eval::compare_reports(
                &case["value"]["current"],
                &case["value"]["baseline"],
            )),
            "eval_evaluate_skill_tool" => skills::permissions::evaluate_skill_tool(
                &case["value"]["skill"],
                case["value"]["tool"].as_str().unwrap_or(""),
            )
            .map(|decision| decision.to_dict()),
            "eval_tool_policy_pass" => {
                skills::eval::tool_policy_pass(&case["value"]["skill"], &case["value"]["case"])
                    .map(|passed| json!(passed))
            }
            "eval_report" => skills::eval::report(
                &registry,
                case["value"]["version"].as_str().unwrap_or(""),
                case["value"]["scope"].as_str().unwrap_or(""),
                case["value"]["skillId"].as_str().unwrap_or(""),
                case["value"]["packId"].as_str().unwrap_or(""),
                &case["value"]["baseline"],
                None,
            ),
            "eval_upgrade_gate" => skills::eval::upgrade_gate_for(
                &registry,
                case["value"]["kind"].as_str().unwrap_or("skill"),
                case["value"]["itemId"].as_str().unwrap_or(""),
                &case["value"]["baseline"],
            ),
            "eval_score_diff" => Ok(skills::eval::score_diff(
                &registry,
                case["value"]["kind"].as_str().unwrap_or("skill"),
                case["value"]["itemId"].as_str().unwrap_or(""),
            )),
            "eval_report_from_results" => skills::eval::selected_skill_ids(
                &registry,
                case["value"]["scope"].as_str().unwrap_or("skill"),
                case["value"]["skillId"].as_str().unwrap_or(""),
                case["value"]["packId"].as_str().unwrap_or(""),
            )
            .and_then(|selected| {
                let pack_map = skills::eval::pack_membership(&registry)?;
                skills::eval::report_from_results(
                    &registry,
                    case["value"]["version"].as_str().unwrap_or(""),
                    case["value"]["scope"].as_str().unwrap_or("skill"),
                    &selected,
                    &pack_map,
                    &case["value"]["baseline"],
                    case["value"]["results"]
                        .as_array()
                        .map(|v| v.as_slice())
                        .unwrap_or(&[]),
                )
            }),
            // The catalog. `catalog_install` is driven both as the write it is and as its `dryRun`
            // early return, because those are different code paths.
            "catalog_manifest" => skills::catalog::manifest(&registry),
            "catalog_export" => skills::catalog::export(&registry),
            "catalog_refresh" => skills::catalog::refresh(&registry),
            "catalog_get" => {
                skills::catalog::get(&registry, case["value"]["itemId"].as_str().unwrap_or(""))
            }
            "catalog_search" => skills::catalog::search(
                &registry,
                case["value"]["query"].as_str().unwrap_or(""),
                &case["value"]["filters"],
            ),
            "catalog_preview" => {
                skills::catalog::get(&registry, case["value"]["itemId"].as_str().unwrap_or(""))
                    .and_then(|item| {
                        skills::catalog::preview(
                            &registry,
                            &item,
                            case["value"]["projectId"].as_str().unwrap_or(""),
                        )
                    })
            }
            "catalog_install" => skills::catalog::install(
                &registry,
                case["value"]["itemId"].as_str().unwrap_or(""),
                case["value"]["projectId"].as_str().unwrap_or(""),
                false,
                case["value"]["dryRun"].as_bool().unwrap_or(false),
            ),
            "catalog_uninstall" => skills::catalog::uninstall(
                &registry,
                case["value"]["itemId"].as_str().unwrap_or(""),
                case["value"]["projectId"].as_str().unwrap_or(""),
            ),
            _ => Err(skills::error("Unknown probe operation", 400)),
        };
        output.push(match result {
            Ok(v) => json!({"ok":v}),
            Err(e) => json!({"error":e.message}),
        });
    }
    println!("{}", json!(output));
}
