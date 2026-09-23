//! Durable revision snapshots. Revision ids and hashes retain the Python canonical format.
use super::{
    Result, error, hash,
    registry::{Registry, read_json},
    schema, strings, text, truth,
};
use serde_json::{Value, json};

pub fn snapshot(
    registry: &Registry,
    item: &Value,
    pack: bool,
    summary: &str,
    event: &str,
) -> Result<Value> {
    let mut config = item.clone();
    for key in if pack {
        &["builtin"][..]
    } else {
        &["builtin", "disabled"][..]
    } {
        config.as_object_mut().unwrap().remove(*key);
    }
    let config = if pack {
        schema::validate_pack(&config)?
    } else {
        schema::validate_skill(&config)?
    };
    let metadata = metadata(registry, &config, pack, summary, event);
    let id = text(&config, if pack { "packId" } else { "skillId" });
    let directory = if pack {
        registry.data.join("history/packs")
    } else {
        registry.data.join("history")
    };
    let path = directory.join(id).join(format!(
        "{}--{}.json",
        safe_version(&text(&metadata, "version")),
        text(&metadata, "revisionId")
    ));
    let mut payload = json!({"schemaVersion":if pack {"skill-pack-revision.v1"} else {"skill-revision.v1"},"metadata":metadata});
    payload[if pack { "pack" } else { "skill" }] = config;
    registry.write_json(&path, &payload)?;
    let mut result = metadata;
    result["path"] = path
        .strip_prefix(&registry.data)
        .unwrap()
        .to_string_lossy()
        .into_owned()
        .into();
    Ok(result)
}
pub fn metadata(
    registry: &Registry,
    config: &Value,
    pack: bool,
    summary: &str,
    event: &str,
) -> Value {
    let now = registry.now();
    let compact = now
        .chars()
        .filter(char::is_ascii_digit)
        .take(14)
        .collect::<String>();
    let digest = hash(&json!({"event":event,"config":config}));
    let mut meta = json!({"version":text(config,"version"),"revisionId":format!("rev_{compact}_{}",&digest[..10]),"createdAt":now,
        "changeSummary":if summary.is_empty() {event} else {summary}.chars().take(400).collect::<String>(),"event":event});
    if pack {
        meta["packId"] = config["packId"].clone();
        meta["packHash"] = hash(config).into();
        meta["skillIdsHash"] = hash(&json!(
            config["skills"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|v| v.is_object())
                .map(|v| text(v, "skillId"))
                .collect::<Vec<_>>()
        ))
        .into();
        let mut tools: Vec<_> = config["skills"]
            .as_array()
            .into_iter()
            .flatten()
            .flat_map(|v| strings(&v["allowedTools"]))
            .collect();
        tools.sort();
        tools.dedup();
        meta["toolGrantHash"] = hash(&json!(tools)).into();
    } else {
        meta["skillId"] = config["skillId"].clone();
        meta["schemaHash"] = hash(
            &json!({"inputSchema":config["inputSchema"],"outputSchema":config["outputSchema"]}),
        )
        .into();
        meta["promptHash"] = hash(&config["systemPrompt"]).into();
        meta["toolGrantHash"] = hash(&config["allowedTools"]).into();
    }
    meta
}
fn safe_version(version: &str) -> String {
    let version = if version.is_empty() { "0.0.0" } else { version }.trim();
    let re = regex::Regex::new(r"[^A-Za-z0-9_.:-]+").unwrap();
    if (1..=80).contains(&version.len()) && !re.is_match(version) {
        version.into()
    } else {
        let result = re
            .replace_all(version, "_")
            .chars()
            .take(80)
            .collect::<String>();
        if result.is_empty() {
            "0.0.0".into()
        } else {
            result
        }
    }
}

pub fn clean(item: &Value, pack: bool) -> Result<Value> {
    let mut config = item.clone();
    for key in if pack {
        &["builtin"][..]
    } else {
        &["builtin", "disabled"][..]
    } {
        config.as_object_mut().unwrap().remove(*key);
    }
    if pack {
        schema::validate_pack(&config)
    } else {
        schema::validate_skill(&config)
    }
}
fn schema_name(pack: bool) -> &'static str {
    if pack {
        "skill-pack-revision.v1"
    } else {
        "skill-revision.v1"
    }
}
fn snapshots(r: &Registry, id: &str, pack: bool) -> Vec<Value> {
    let directory = if pack {
        r.data.join("history/packs")
    } else {
        r.data.join("history")
    }
    .join(id);
    let mut paths: Vec<_> = std::fs::read_dir(directory)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .filter(|p| p.extension().is_some_and(|v| v == "json"))
        .collect();
    paths.sort();
    let mut result = Vec::new();
    for path in paths {
        let mut v = read_json(&path);
        if v["schemaVersion"] == schema_name(pack)
            && v["metadata"].is_object()
            && v[if pack { "pack" } else { "skill" }].is_object()
        {
            v["path"] = path.to_string_lossy().into_owned().into();
            result.push(v);
        }
    }
    result.sort_by_key(|v| text(&v["metadata"], "createdAt"));
    result
}
fn public(revision: &Value) -> Value {
    let mut metadata = if revision["metadata"].is_object()
        && !revision["metadata"].as_object().unwrap().is_empty()
    {
        revision["metadata"].clone()
    } else {
        revision.clone()
    };
    if let Some(v) = revision.get("path") {
        metadata["path"] = crate::python_json::value_str(v).into();
    }
    if revision.get("current").is_some() {
        metadata["current"] = truth(revision, "current").into();
    }
    metadata
}
pub fn list(r: &Registry, id: &str, pack: bool) -> Result<Value> {
    let id = schema::normalize_id(&json!(id), if pack { "packId" } else { "skillId" })?;
    let mut versions: Vec<_> = snapshots(r, &id, pack).iter().map(public).collect();
    if let Ok(current) = if pack {
        r.get_pack(&id)
    } else {
        r.get(&id, true)
    } {
        let mut meta = metadata(
            r,
            &clean(&current, pack)?,
            pack,
            "Current registry state",
            "current",
        );
        meta["current"] = true.into();
        versions.push(meta);
    }
    let mut result: Vec<Value> = Vec::new();
    for item in versions {
        if let Some(v) = result
            .iter_mut()
            .find(|v| v["revisionId"] == item["revisionId"] && v["event"] == item["event"])
        {
            *v = item;
        } else {
            result.push(item);
        }
    }
    result.sort_by_key(|v| text(v, "createdAt"));
    Ok(json!(result))
}
pub fn resolve(r: &Registry, id: &str, version: &str, pack: bool) -> Result<Value> {
    let id = schema::normalize_id(&json!(id), if pack { "packId" } else { "skillId" })?;
    let wanted = version.trim();
    if let Some(v) = snapshots(r, &id, pack).into_iter().rev().find(|v| {
        text(&v["metadata"], "version") == wanted || text(&v["metadata"], "revisionId") == wanted
    }) {
        return Ok(v);
    }
    if ["", "current", "latest"].contains(&wanted) {
        let current = clean(
            &if pack {
                r.get_pack(&id)?
            } else {
                r.get(&id, true)?
            },
            pack,
        )?;
        let mut result = json!({"schemaVersion":schema_name(pack),"metadata":metadata(r,&current,pack,"Current registry state","current")});
        result[if pack { "pack" } else { "skill" }] = current;
        return Ok(result);
    }
    Err(error(
        if pack {
            "Skill Pack version not found"
        } else {
            "Skill version not found"
        },
        404,
    ))
}
pub fn list_diff(before: &[String], after: &[String]) -> Value {
    let a: std::collections::BTreeSet<_> = before.iter().cloned().collect();
    let b: std::collections::BTreeSet<_> = after.iter().cloned().collect();
    json!({"added":b.difference(&a).collect::<Vec<_>>(),"removed":a.difference(&b).collect::<Vec<_>>(),"unchanged":a.intersection(&b).collect::<Vec<_>>()})
}
fn pack_tools(item: &Value) -> Vec<String> {
    let mut tools: Vec<_> = item["skills"]
        .as_array()
        .into_iter()
        .flatten()
        .flat_map(|v| strings(&v["allowedTools"]))
        .collect();
    tools.sort();
    tools.dedup();
    tools
}
pub fn diff(
    r: &Registry,
    id: &str,
    from: &str,
    to: &str,
    pack: bool,
    eval_score: Value,
) -> Result<Value> {
    let before = resolve(r, id, from, pack)?;
    let after = resolve(r, id, to, pack)?;
    let key = if pack { "pack" } else { "skill" };
    let a = &before[key];
    let b = &after[key];
    let fields = if pack {
        &["name", "description", "version", "author", "skills"][..]
    } else {
        &[
            "systemPrompt",
            "inputSchema",
            "outputSchema",
            "allowedTools",
            "memoryPolicy",
            "artifactPolicy",
            "projectBinding",
        ][..]
    };
    let fields:Vec<_>=fields.iter().map(|f|json!({"field":f,"changed":a[*f]!=b[*f],"before":a[*f],"after":b[*f],"beforeHash":hash(&a[*f]),"afterHash":hash(&b[*f])})).collect();
    let mut result = json!({"ok":true,"kind":key,"from":public(&before),"to":public(&after),"changed":fields.iter().any(|f|truth(f,"changed")),"fields":fields,"evalScoreDiff":eval_score,
        "toolGrantDiff":list_diff(&if pack {pack_tools(a)} else {strings(&a["allowedTools"])},&if pack {pack_tools(b)} else {strings(&b["allowedTools"])})});
    result[if pack { "packId" } else { "skillId" }] =
        b[if pack { "packId" } else { "skillId" }].clone();
    if pack {
        result["skillDiff"] = list_diff(
            &a["skills"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|v| text(v, "skillId"))
                .collect::<Vec<_>>(),
            &b["skills"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|v| text(v, "skillId"))
                .collect::<Vec<_>>(),
        );
    }
    Ok(result)
}
pub fn migration_plan(r: &Registry, id: &str, from: &str, to: &str) -> Result<Value> {
    use std::collections::BTreeSet;
    let before = resolve(r, id, from, false)?;
    let after = resolve(r, id, to, false)?;
    let a = &before["skill"]["inputSchema"];
    let b = &after["skill"]["inputSchema"];
    let keys = |v: &Value| -> BTreeSet<String> {
        v["properties"]
            .as_object()
            .into_iter()
            .flat_map(|m| m.keys().cloned())
            .collect()
    };
    let ak = keys(a);
    let bk = keys(b);
    let ar: BTreeSet<_> = strings(&a["required"]).into_iter().collect();
    let br: BTreeSet<_> = strings(&b["required"]).into_iter().collect();
    let removed: Vec<_> = ak.difference(&bk).cloned().collect();
    let added: Vec<_> = bk.difference(&ak).cloned().collect();
    let mut renamed_a = BTreeSet::new();
    let mut renamed_b = BTreeSet::new();
    let mut changes = Vec::new();
    for old in &removed {
        if let Some(new) = added.iter().find(|new| {
            !renamed_b.contains(*new)
                && a["properties"][old]["type"] == b["properties"][*new]["type"]
        }) {
            renamed_a.insert(old.clone());
            renamed_b.insert(new.clone());
            changes.push(json!({"type":"inputFieldRenamed","from":old,"to":new,"safe":true}));
        }
    }
    for field in &removed {
        if !renamed_a.contains(field) {
            changes
                .push(json!({"type":"inputFieldRemoved","field":field,"safe":!ar.contains(field)}));
        }
    }
    for field in &added {
        if !renamed_b.contains(field) {
            let default = &b["properties"][field]["default"];
            let required = br.contains(field);
            let mut change = json!({"type":"inputFieldAdded","field":field,"required":required,"safe":!required||!default.is_null()});
            if !default.is_null() {
                change["default"] = default.clone();
            }
            changes.push(change);
        }
    }
    for field in br
        .difference(&ar)
        .filter(|f| !renamed_b.contains(*f) && !added.contains(*f))
    {
        let default = &b["properties"][field]["default"];
        let mut change =
            json!({"type":"requiredFieldAdded","field":field,"safe":!default.is_null()});
        if !default.is_null() {
            change["default"] = default.clone();
        }
        changes.push(change);
    }
    let targets = super::project_integration::migration_targets(r, id)?;
    let safe = changes.iter().all(|v| truth(v, "safe"));
    Ok(
        json!({"ok":true,"skillId":id,"fromVersion":before["metadata"]["version"],"toVersion":after["metadata"]["version"],"safe":safe,"changes":changes,"migrationTargets":targets,
        "summary":format!("{} schema changes, {}; targets: {} project bindings, {} skill runs, {} saved metadata records.",changes.len(),if safe {"safe"} else {"requires review"},targets["projectBindings"],targets["skillRuns"],targets["savedMetadata"])}),
    )
}
pub fn rollback(
    r: &Registry,
    id: &str,
    version: &str,
    pack: bool,
    project: &str,
    summary: &str,
) -> Result<Value> {
    let _guard = r.mutation()?;
    if !pack
        && r.load(&r.builtin, true, false)?
            .iter()
            .any(|s| text(s, "skillId") == id)
    {
        return Err(error(
            "Built-in Skills cannot be rolled back; clone them as custom Skills first",
            403,
        ));
    }
    let target = resolve(r, id, version, pack)?;
    let current = if pack {
        r.get_pack(id)?
    } else {
        r.get(id, true)?
    };
    if pack && truth(&current, "builtin") {
        return Err(error(
            "Built-in Skill Packs are read-only; export and import as a custom Pack to edit",
            403,
        ));
    }
    snapshot(
        r,
        &current,
        pack,
        &format!(
            "{}Rollback checkpoint before {version}",
            if pack { "Pack " } else { "" }
        ),
        "rollback_checkpoint",
    )?;
    let mut restored = clean(&target[if pack { "pack" } else { "skill" }], pack)?;
    restored["updatedAt"] = r.now().into();
    r.write_json(
        &r.data
            .join(if pack { "packs" } else { "custom" })
            .join(format!("{id}.json")),
        &restored,
    )?;
    let revision = snapshot(
        r,
        &restored,
        pack,
        &if summary.is_empty() {
            format!(
                "Rolled back {}to {version}",
                if pack { "Pack " } else { "" }
            )
        } else {
            summary.into()
        },
        "rollback",
    )?;
    let mut public = restored.clone();
    public["builtin"] = false.into();
    if pack {
        public["skills"]=json!(restored["skills"].as_array().into_iter().flatten().map(|v|json!({"skillId":text(v,"skillId"),"name":text(v,"name"),"embedded":!schema::is_reference(v)})).collect::<Vec<_>>());
    } else {
        public["disabled"] = truth(&restored, "disabled").into();
    }
    let mut result = json!({"ok":true,"rolledBackTo":target["metadata"],"revision":revision});
    result[if pack { "pack" } else { "skill" }] = public;
    if pack {
        result["projectBinding"] = if project.is_empty() {
            json!({})
        } else {
            super::project_integration::enable_pack(r, project, id, &text(&restored, "version"))?
        };
    }
    Ok(result)
}
pub fn upgrade_pack(
    r: &Registry,
    id: &str,
    version: &str,
    project: &str,
    gate: Value,
) -> Result<Value> {
    let pack = r.get_pack(id)?;
    let version = if version.is_empty() || ["current", "latest"].contains(&version) {
        text(&pack, "version")
    } else {
        version.into()
    };
    let mut applied = pack.clone();
    if !version.is_empty() && version != text(&pack, "version") {
        let target = resolve(r, id, &version, true)?;
        if truth(&pack, "builtin") {
            return Err(error(
                "Built-in Skill Packs are read-only; export and import as a custom Pack to upgrade locally",
                403,
            ));
        }
        applied = clean(&target["pack"], true)?;
        applied["updatedAt"] = r.now().into();
        r.write_json(&r.data.join("packs").join(format!("{id}.json")), &applied)?;
        snapshot(
            r,
            &applied,
            true,
            &format!("Upgraded Pack to {version}"),
            "upgrade",
        )?;
    }
    let binding = if project.is_empty() {
        json!({})
    } else {
        super::project_integration::enable_pack(r, project, id, &text(&applied, "version"))?
    };
    Ok(
        json!({"ok":true,"pack":r.get_pack(id)?,"targetVersion":version,"evalAwareUpgradeGate":gate,"projectBinding":binding}),
    )
}
