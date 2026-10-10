//! The local-only skill catalog. Installation changes project bindings, never downloads code.
use super::{
    Result, error, project_integration as projects,
    registry::{Registry, read_json},
    schema, security, strings, text, truth,
};
use serde_json::{Value, json};
pub const APP_VERSION: &str = include_str!("../../../../../VERSION");
const CATEGORIES: &[(&str, &[&str])] = &[
    (
        "Study",
        &[
            "study", "tutor", "exam", "course", "learn", "paper", "reader", "document",
        ],
    ),
    ("Research", &["research", "brief", "source", "reading"]),
    (
        "Code",
        &["code", "review", "python", "engineering", "readme"],
    ),
    ("Office", &["ppt", "slide", "office", "document", "report"]),
    (
        "Writing",
        &["write", "writer", "paper", "summary", "markdown"],
    ),
    ("Data", &["data", "chart", "table", "analysis"]),
    ("Automation", &["automation", "workflow", "scheduler"]),
];
fn unique(items: Vec<String>) -> Vec<String> {
    let mut result = Vec::new();
    for item in items {
        if !result.contains(&item) {
            result.push(item);
        }
    }
    result
}
fn sorted(items: Vec<String>) -> Vec<String> {
    let mut items = unique(items);
    items.sort();
    items
}
fn category(item: &Value, tools: &[String]) -> String {
    let blob = format!(
        "{} {} {} {} {}",
        if truth(item, "skillId") {
            text(item, "skillId")
        } else {
            text(item, "packId")
        },
        text(item, "name"),
        text(item, "description"),
        item["skills"]
            .as_array()
            .into_iter()
            .flatten()
            .map(crate::python_json::value_str)
            .collect::<Vec<_>>()
            .join(" "),
        tools.join(" ")
    )
    .to_lowercase();
    CATEGORIES
        .iter()
        .find(|(_, words)| words.iter().any(|w| blob.contains(w)))
        .map(|v| v.0)
        .unwrap_or("General")
        .into()
}
fn use_cases(category: &str, name: &str) -> Value {
    json!(match category {
        "Study" => vec!["exam prep", "worked explanation", "revision notes"],
        "Research" => vec!["topic brief", "source synthesis", "markdown report"],
        "Code" => vec!["code review", "README support", "engineering notes"],
        "Office" => vec!["slides", "documents", "project export"],
        "Writing" => vec!["paper draft", "summary", "reference outline"],
        "Data" => vec!["analysis notes", "chart planning", "data summary"],
        "Automation" => vec!["repeatable workflow", "local task helper", "runtime prep"],
        _ => vec!["local workspace task", name],
    })
}
fn recommended(category: &str) -> Value {
    json!(match category {
        "Study" => vec!["exam-prep", "course-notes"],
        "Research" => vec!["research-library", "briefing"],
        "Code" => vec!["repo-review", "engineering"],
        "Office" => vec!["presentation", "reporting"],
        "Writing" => vec!["paper-draft", "article"],
        "Data" => vec!["analysis", "dashboard"],
        "Automation" => vec!["operations", "runtime"],
        _ => vec!["workspace"],
    })
}
fn tags(category: &str, tools: &[String], builtin: bool) -> Value {
    let mut tags = vec![
        category.to_lowercase(),
        "local".into(),
        if builtin { "builtin" } else { "custom" }.into(),
    ];
    for tool in tools {
        if ["web_search", "fetch_url", "compare_search_results"].contains(&tool.as_str()) {
            tags.push("network".into());
        }
        if ["create_document", "create_pptx", "create_mindmap"].contains(&tool.as_str()) {
            tags.push("artifact".into());
        }
        if ["search_files", "read_file_chunk", "list_project_files"].contains(&tool.as_str()) {
            tags.push("filesystem".into());
        }
    }
    json!(sorted(tags))
}
pub fn list(r: &Registry) -> Result<Vec<Value>> {
    let mut report = json!({});
    for name in [
        format!("skills-v{}.json", APP_VERSION.trim()),
        "skill-latest.json".into(),
        "latest.json".into(),
    ] {
        let value = read_json(&r.root.join("evals/reports").join(name));
        if value.is_object() {
            report = value;
            break;
        }
    }
    let project_rows = crate::projects::list_projects(&r.root, r).unwrap_or_default();
    let mut result = Vec::new();
    for (pack, items) in [(false, r.list(true, false)?), (true, r.packs(true)?)] {
        for item in items {
            let id = text(&item, if pack { "packId" } else { "skillId" });
            let builtin = truth(&item, "builtin");
            let mut expanded = if pack {
                r.export_pack(&id)?
            } else {
                item.clone()
            };
            if pack {
                for key in ["builtin", "createdAt", "updatedAt"] {
                    expanded[key] = item[key].clone();
                }
            }
            let review = security::review(r, &expanded, pack, false)?;
            let manifest = &review["manifest"];
            let included = if pack {
                expanded["skills"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|v| text(v, "skillId"))
                    .collect()
            } else {
                vec![id.clone()]
            };
            let tools = if pack {
                sorted(
                    expanded["skills"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .flat_map(|v| strings(&v["allowedTools"]))
                        .collect(),
                )
            } else {
                unique(strings(&item["allowedTools"]))
            };
            let artifact_types = if pack {
                sorted(
                    expanded["skills"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .flat_map(|v| strings(&v["artifactPolicy"]["types"]))
                        .collect(),
                )
            } else {
                unique(strings(&item["artifactPolicy"]["types"]))
            };
            let mut category_item = item.clone();
            if pack {
                category_item["skills"] = json!(included);
            }
            let category = category(&category_item, &tools);
            let name = if truth(&item, "name") {
                text(&item, "name")
            } else {
                id.clone()
            };
            let risk = review["riskScore"].as_u64().unwrap_or(0);
            let score = report[if pack { "packResults" } else { "skillResults" }]
                .as_array()
                .into_iter()
                .flatten()
                .find(|v| text(v, if pack { "packId" } else { "skillId" }) == id)
                .and_then(|v| v["overallScore"].as_f64())
                .unwrap_or(0.0);
            let installs = project_rows
                .iter()
                .map(|p| {
                    strings(
                        &p["skills"][if pack {
                            "enabledPacks"
                        } else {
                            "enabledSkills"
                        }],
                    )
                    .iter()
                    .filter(|v| *v == &id)
                    .count()
                })
                .sum::<usize>();
            let permissions = if pack {
                schema::tool_permissions(&expanded)
            } else {
                json!([{"skillId":id,"embedded":true,"allowedTools":review["allowedToolsRisk"].as_array().into_iter().flatten().map(|t|json!({"tool":text(t,"tool"),"risk":text(t,"risk"),"requiresApproval":truth(t,"requiresApproval")})).collect::<Vec<_>>()}])
            };
            let mut output = json!({"itemId":id,"kind":if pack {"pack"} else {"skill"},"skillId":if pack {""} else {&id},"packId":if pack {&id} else {""},"name":name,"description":text(&item,"description"),
            "category":category,"tags":tags(&category,&tools,builtin),"author":if pack && truth(&item,"author") {text(&item,"author")} else {if builtin {"builtin"} else {"local"}.into()},
            "version":text(&item,"version"),"trustLevel":text(&review,"reviewStatus"),"riskScore":risk,"evalScore":score,"installCount":installs,
            "lastUpdated":if truth(&item,"updatedAt") {text(&item,"updatedAt")} else {text(&item,"createdAt")},"includedSkills":included,"requiredTools":tools,"artifactTypes":artifact_types,
            "difficulty":if risk>=70 {"advanced"} else if risk>=25 {"intermediate"} else {"beginner"},"useCases":use_cases(&category,&name),"recommendedProjects":recommended(&category),
            "builtin":builtin,"disabled":!pack&&truth(&item,"disabled"),"source":if builtin {"builtin"} else {"custom"},"signed":truth(manifest,"signed"),"securityReview":review,"toolPermissionSummary":permissions});
            for key in ["contentHash", "schemaHash", "promptHash", "toolGrantHash"] {
                output[key] = text(manifest, key).into();
            }
            result.push(output);
        }
    }
    result.sort_by_key(|v| (text(v, "kind"), text(v, "category"), text(v, "name")));
    Ok(result)
}
pub fn summary(items: &[Value]) -> Value {
    let count = |key: &str, value: &str| items.iter().filter(|v| text(v, key) == value).count();
    let score = items
        .iter()
        .map(|v| v["evalScore"].as_f64().unwrap_or(0.0))
        .sum::<f64>()
        / items.len().max(1) as f64;
    json!({"itemCount":items.len(),"skillCount":count("kind","skill"),"packCount":count("kind","pack"),"trusted":count("trustLevel","trusted"),"needsReview":count("trustLevel","needs-review"),
        "highRisk":count("trustLevel","high-risk"),"blocked":count("trustLevel","blocked"),"averageEvalScore":(score*100.0).round_ties_even()/100.0,"localOnly":true})
}
pub fn manifest(r: &Registry) -> Result<Value> {
    let items = list(r)?;
    Ok(
        json!({"catalogVersion":"1.0.0","schemaVersion":"skill-catalog.v1","version":APP_VERSION.trim(),"generatedAt":r.now(),"source":"local","network":false,"summary":summary(&items),"items":items}),
    )
}
pub fn get(r: &Registry, id: &str) -> Result<Value> {
    if id.trim().is_empty() {
        return Err(error("itemId is required", 400));
    }
    list(r)?
        .into_iter()
        .find(|v| text(v, "itemId") == id.trim())
        .ok_or_else(|| error("Catalog item not found", 404))
}

/// `_filter_number`: the value stringified and parsed, and `None` when it is null, the empty
/// string, or does not parse.
///
/// The stringification is the oracle's, so a number, a numeric string and a `bool` all take the
/// same path — `False` becomes `"False"`, which does not parse, and is therefore not a filter.
fn filter_number(value: &Value) -> Option<f64> {
    if value.is_null() || value.as_str() == Some("") {
        return None;
    }
    crate::python_json::value_str(value).parse::<f64>().ok()
}

/// The key orders the oracle puts on the manifest's own objects, read off
/// `deepseek_infra/infra/skills/catalog.py`: `catalog_manifest`, `_catalog_summary`, `_skill_item`,
/// `_pack_item` and `_skill_review`/`_pack_review`. A skill item and a pack item carry the **same
/// 31 keys in the same order**, so one list serves both. These exist because
/// `python_json::OrderedJson` renders nested objects **sorted** unless the object's name is
/// registered, and `catalog_refresh` writes this manifest to disk with
/// `json.dumps(..., ensure_ascii=False, indent=2)`.
///
/// A skill review carries `skillId` where a pack review carries `packId`, and only a pack review
/// carries `skillReviews`; the merged order emits whichever is present, in the oracle's slot.
const MANIFEST_KEYS: &[&str] = &[
    "catalogVersion",
    "schemaVersion",
    "version",
    "generatedAt",
    "source",
    "network",
    "summary",
    "items",
];
const SUMMARY_KEYS: &[&str] = &[
    "itemCount",
    "skillCount",
    "packCount",
    "trusted",
    "needsReview",
    "highRisk",
    "blocked",
    "averageEvalScore",
    "localOnly",
];
const ITEM_KEYS: &[&str] = &[
    "itemId",
    "kind",
    "skillId",
    "packId",
    "name",
    "description",
    "category",
    "tags",
    "author",
    "version",
    "trustLevel",
    "riskScore",
    "evalScore",
    "installCount",
    "lastUpdated",
    "includedSkills",
    "requiredTools",
    "artifactTypes",
    "difficulty",
    "useCases",
    "recommendedProjects",
    "builtin",
    "disabled",
    "source",
    "signed",
    "contentHash",
    "schemaHash",
    "promptHash",
    "toolGrantHash",
    "securityReview",
    "toolPermissionSummary",
];
const REVIEW_KEYS: &[&str] = &[
    "schemaVersion",
    "reviewId",
    "kind",
    "skillId",
    "packId",
    "name",
    "version",
    "builtin",
    "trustLevel",
    "reviewStatus",
    "riskScore",
    "allowedToolsRisk",
    "requiresApprovalCount",
    "capabilities",
    "findings",
    "skillReviews",
    "manifest",
    "lastSecurityReviewAt",
    "signed",
];
/// The review's own `manifest` block has **two shapes at the same name and the same path**: a skill
/// review carries `skillId` and puts `packId` after `toolGrantHash`, a pack review carries `packId`
/// fourth and no `skillId`. No by-name table can separate them, which is why
/// `from_value_with_orders_and_shapes` selects on the object's own keys.
const REVIEW_MANIFEST_SKILL_KEYS: &[&str] = &[
    "schemaVersion",
    "kind",
    "skillId",
    "version",
    "contentHash",
    "schemaHash",
    "promptHash",
    "toolGrantHash",
    "packId",
    "reviewStatus",
    "signed",
];
const REVIEW_MANIFEST_PACK_KEYS: &[&str] = &[
    "schemaVersion",
    "kind",
    "packId",
    "version",
    "contentHash",
    "schemaHash",
    "promptHash",
    "toolGrantHash",
    "reviewStatus",
    "signed",
];
const TOOL_PERMISSION_KEYS: &[&str] = &["skillId", "embedded", "allowedTools"];
const ALLOWED_TOOL_KEYS: &[&str] = &["tool", "risk", "requiresApproval"];
const TOOL_RISK_KEYS: &[&str] = &[
    "tool",
    "risk",
    "riskScore",
    "network",
    "filesystem",
    "sensitive",
    "requiresApproval",
];
const SKILL_REVIEW_KEYS: &[&str] = &[
    "skillId",
    "reviewStatus",
    "riskScore",
    "findingCount",
    "toolGrantHash",
];
const FINDING_KEYS: &[&str] = &["type", "field", "severity", "message", "suggestion"];
const MANIFEST_ORDERS: &[(&str, &[&str])] = &[
    ("summary", SUMMARY_KEYS),
    ("items", ITEM_KEYS),
    ("securityReview", REVIEW_KEYS),
    ("manifest", REVIEW_MANIFEST_PACK_KEYS),
    ("toolPermissionSummary", TOOL_PERMISSION_KEYS),
    ("allowedTools", ALLOWED_TOOL_KEYS),
    ("allowedToolsRisk", TOOL_RISK_KEYS),
    ("skillReviews", SKILL_REVIEW_KEYS),
    ("findings", FINDING_KEYS),
];
/// `(name, key that decides the shape, order)`.
const MANIFEST_SHAPE_ORDERS: &[(&str, &str, &[&str])] = &[
    ("manifest", "skillId", REVIEW_MANIFEST_SKILL_KEYS),
    ("manifest", "packId", REVIEW_MANIFEST_PACK_KEYS),
];

/// `catalog_export`: the manifest, without touching the disk.
pub fn export(r: &Registry) -> Result<Value> {
    Ok(json!({"ok":true,"catalog":manifest(r)?}))
}

/// `catalog_refresh`: rebuild the manifest and cache it at `.skills/catalog/catalog.json`.
///
/// The oracle writes `json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"`, and this writes
/// the same bytes through [`MANIFEST_ORDERS`] rather than `serde_json`'s own printer — the latter
/// keeps insertion order only when the `preserve_order` feature is on, which a single-crate build
/// does not enable.
pub fn refresh(r: &Registry) -> Result<Value> {
    let _guard = r.mutation()?;
    let manifest = manifest(r)?;
    let path = r.data.join("catalog").join("catalog.json");
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| error(e.to_string(), 500))?;
    }
    let rendered = crate::python_json::OrderedJson::from_value_with_orders_and_shapes(
        &manifest,
        MANIFEST_KEYS,
        MANIFEST_ORDERS,
        MANIFEST_SHAPE_ORDERS,
    );
    std::fs::write(&path, format!("{}\n", rendered.render_indent_2()))
        .map_err(|e| error(e.to_string(), 500))?;
    Ok(json!({"ok":true,"path":path.to_string_lossy(),"manifest":manifest}))
}
pub fn search(r: &Registry, query: &str, filters: &Value) -> Result<Value> {
    let query_lower = query.trim().to_lowercase();
    let mut items = Vec::new();
    for item in list(r)? {
        let blob = format!(
            "{} {} {} {} {} {} {}",
            text(&item, "itemId"),
            text(&item, "name"),
            text(&item, "description"),
            text(&item, "category"),
            strings(&item["tags"]).join(" "),
            strings(&item["requiredTools"]).join(" "),
            strings(&item["includedSkills"]).join(" ")
        )
        .to_lowercase();
        if !query_lower.is_empty() && !blob.contains(&query_lower) {
            continue;
        }
        if ["kind", "trustLevel"]
            .iter()
            .any(|key| truth(filters, key) && text(&item, key) != text(filters, key))
        {
            continue;
        }
        if truth(filters, "category")
            && text(&item, "category").to_lowercase() != text(filters, "category").to_lowercase()
        {
            continue;
        }
        if filters["trusted"] == true && item["trustLevel"] != "trusted" {
            continue;
        }
        let tools = strings(&item["requiredTools"]);
        if filters["offline"] == true && tools.contains(&"web_search".into()) {
            continue;
        }
        // `_filter_number` first, then compare: a `0` (or `"0"`) is a **live** filter in the oracle,
        // so this cannot go through `text()`, whose falsy check swallows zero and silently drops
        // the filter. The oracle also trims `tool` *before* deciding whether it is set, so a
        // whitespace-only value is not a filter at all.
        if let Some(max) = filter_number(&filters["maxRiskScore"]) {
            if item["riskScore"].as_f64().unwrap_or(0.0).trunc() > max.trunc() {
                continue;
            }
        }
        if let Some(min) = filter_number(&filters["minEvalScore"]) {
            if item["evalScore"].as_f64().unwrap_or(0.0) < min {
                continue;
            }
        }
        let tool = text(filters, "tool").trim().to_string();
        if !tool.is_empty() && !tools.contains(&tool) {
            continue;
        }
        items.push(item);
    }
    Ok(json!({"ok":true,"query":query,"filters":filters,"summary":summary(&items),"items":items}))
}
pub fn preview(r: &Registry, item: &Value, project: &str) -> Result<Value> {
    let binding = projects::binding(r, project)?;
    let enabled = strings(&binding["enabledSkills"]);
    let packs = strings(&binding["enabledPacks"]);
    let included = strings(&item["includedSkills"]);
    let review = &item["securityReview"];
    let status = text(review, "reviewStatus");
    Ok(
        json!({"itemId":item["itemId"],"kind":item["kind"],"projectId":project,"includedSkills":included,
        "newSkills":included.iter().filter(|s|!enabled.contains(s)).collect::<Vec<_>>(),"alreadyEnabledSkills":included.iter().filter(|s|enabled.contains(s)).collect::<Vec<_>>(),
        "willEnablePack":item["kind"]=="pack"&&!packs.contains(&text(item,"packId")),"willModifyProjectBinding":true,"requiresSecurityApproval":(["high-risk","blocked"].contains(&status.as_str())),
        "blocked":status=="blocked","reviewStatus":status,"trustLevel":item["trustLevel"],"riskScore":item["riskScore"],"evalScore":item["evalScore"],"signed":truth(item,"signed"),
        "requiredTools":item["requiredTools"],"toolPermissionSummary":item["toolPermissionSummary"],"securityReview":review,
        "projectChanges":{"enabledPacksBefore":sorted(packs),"enabledSkillsBefore":sorted(enabled)}}),
    )
}
pub fn install(
    r: &Registry,
    id: &str,
    project: &str,
    approved: bool,
    dry_run: bool,
) -> Result<Value> {
    let _guard = if dry_run { None } else { Some(r.mutation()?) };
    let item = get(r, id)?;
    let project = project.trim();
    if project.is_empty() {
        return Err(error("projectId is required", 400));
    }
    projects::require(r, project)?;
    let preview = preview(r, &item, project)?;
    if dry_run {
        return Ok(json!({"ok":true,"dryRun":true,"item":item,"installPreview":preview}));
    }
    if truth(&preview, "blocked") {
        return Err(error(
            "Catalog item is blocked by local security review",
            403,
        ));
    }
    if truth(&preview, "requiresSecurityApproval") && !approved {
        return Err(error(
            "Catalog item requires securityApproved=true before install",
            403,
        ));
    }
    let binding = if item["kind"] == "pack" {
        projects::enable_pack(r, project, &text(&item, "packId"), &text(&item, "version"))?
    } else {
        let sid = text(&item, "skillId");
        r.get(&sid, true)?;
        let mut binding = projects::binding(r, project)?;
        let mut enabled = strings(&binding["enabledSkills"]);
        if !enabled.contains(&sid) {
            enabled.push(sid.clone());
        }
        binding["enabledSkills"] = enabled.into();
        if !truth(&binding, "defaultSkill") {
            binding["defaultSkill"] = sid.into();
        }
        projects::set_binding(r, project, &binding)?
    };
    Ok(
        json!({"ok":true,"dryRun":false,"item":item,"installPreview":preview,"projectId":project,"skills":binding}),
    )
}
pub fn uninstall(r: &Registry, id: &str, project: &str) -> Result<Value> {
    let _guard = r.mutation()?;
    let item = get(r, id)?;
    let project = project.trim();
    if project.is_empty() {
        return Err(error("projectId is required", 400));
    }
    projects::require(r, project)?;
    let mut binding = projects::binding(r, project)?;
    let mut enabled = strings(&binding["enabledSkills"]);
    if item["kind"] == "pack" {
        let id = text(&item, "packId");
        let removing = strings(&item["includedSkills"]);
        let packs: Vec<_> = strings(&binding["enabledPacks"])
            .into_iter()
            .filter(|p| p != &id)
            .collect();
        let protected: Vec<_> = packs
            .iter()
            .filter_map(|id| r.export_pack(id).ok())
            .flat_map(|p| {
                p["skills"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|s| text(s, "skillId"))
                    .collect::<Vec<_>>()
            })
            .collect();
        enabled.retain(|s| !removing.contains(s) || protected.contains(s));
        binding["enabledPacks"] = packs.into();
        binding["enabledPackVersions"] = json!(
            binding["enabledPackVersions"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|v| text(v, "packId") != id)
                .cloned()
                .collect::<Vec<_>>()
        );
    } else {
        enabled.retain(|s| s != &text(&item, "skillId"));
    }
    if !enabled.contains(&text(&binding, "defaultSkill")) {
        binding["defaultSkill"] = enabled.first().cloned().unwrap_or_default().into();
    }
    binding["enabledSkills"] = enabled.into();
    let binding = projects::set_binding(r, project, &binding)?;
    Ok(json!({"ok":true,"itemId":item["itemId"],"projectId":project,"skills":binding}))
}
