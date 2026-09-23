use super::{Result, error, schema, text, truth};
use serde_json::{Value, json};
use std::path::{Path, PathBuf};

#[derive(Clone)]
pub struct Registry {
    pub root: PathBuf,
    pub data: PathBuf,
    pub builtin: PathBuf,
    pub packs: PathBuf,
    pub clock: Option<i64>,
}

pub fn read_json(path: &Path) -> Value {
    std::fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(Value::Null)
}
impl crate::entropy::Entropy for Registry {
    fn new_id(&self) -> Result<String> {
        crate::entropy::SystemEntropy.new_id()
    }
    fn now_millis(&self) -> i64 {
        self.clock
            .map(|s| s * 1000)
            .unwrap_or_else(|| crate::entropy::SystemEntropy.now_millis())
    }
}
impl Registry {
    pub fn mutation(&self) -> Result<crate::mutation_gate::MutationScope> {
        crate::mutation_gate::mutation_scope(None, self.data.parent().unwrap_or(&self.root))
            .map_err(|e| error(e.to_string(), 409))
    }
    pub fn create(&self, config: &Value, overwrite: bool) -> Result<Value> {
        let mut skill = schema::validate_skill(config)?;
        let id = text(&skill, "skillId");
        let _guard = self.mutation()?;
        if self
            .load(&self.builtin, true, false)?
            .iter()
            .any(|s| text(s, "skillId") == id)
        {
            return Err(error("Cannot overwrite a built-in Skill", 403));
        }
        let path = self.data.join("custom").join(format!("{id}.json"));
        if path.exists() && !overwrite {
            return Err(error("Skill already exists", 409));
        }
        let old = read_json(&path);
        let now = self.now();
        skill["createdAt"] = if truth(&old, "createdAt") {
            crate::python_json::value_str(&old["createdAt"])
        } else {
            now.clone()
        }
        .into();
        skill["updatedAt"] = now.into();
        skill["builtin"] = false.into();
        self.write_json(&path, &skill)?;
        super::versioning::snapshot(self, &skill, false, "Created custom Skill", "create")?;
        let review = super::security::review(self, &skill, false, true)?;
        skill["securityReview"] = review;
        Ok(skill)
    }
    pub fn update(&self, id: &str, patch: &Value) -> Result<Value> {
        let id = schema::normalize_id(&json!(id), "skillId")?;
        let _guard = self.mutation()?;
        let path = self.data.join("custom").join(format!("{id}.json"));
        if !path.exists() {
            if self
                .load(&self.builtin, true, false)?
                .iter()
                .any(|s| text(s, "skillId") == id)
            {
                return Err(error(
                    "Built-in Skills are read-only; export and import as a custom Skill to edit",
                    403,
                ));
            }
            return Err(error("Skill not found", 404));
        }
        let current = read_json(&path);
        let mut merged = current.clone();
        let Some(fields) = merged.as_object_mut() else {
            return Err(error("Skill file is corrupt", 400));
        };
        if let Some(patch) = patch.as_object() {
            for (key, value) in patch {
                if key != "changeSummary" {
                    fields.insert(key.clone(), value.clone());
                }
            }
        }
        if text(&merged, "skillId") != id {
            return Err(error("skillId cannot be changed", 400));
        }
        let mut skill = schema::validate_skill(&merged)?;
        skill["createdAt"] = if truth(&current, "createdAt") {
            crate::python_json::value_str(&current["createdAt"])
        } else {
            self.now()
        }
        .into();
        skill["updatedAt"] = self.now().into();
        skill["builtin"] = false.into();
        self.write_json(&path, &skill)?;
        let summary = text(patch, "changeSummary");
        super::versioning::snapshot(
            self,
            &skill,
            false,
            if summary.is_empty() {
                "Updated custom Skill"
            } else {
                &summary
            },
            "update",
        )?;
        skill["securityReview"] = super::security::review(self, &skill, false, true)?;
        Ok(skill)
    }
    pub fn set_disabled(&self, id: &str, disabled: bool) -> Result<Value> {
        let id = schema::normalize_id(&json!(id), "skillId")?;
        let _guard = self.mutation()?;
        self.get(&id, true)?;
        let mut ids = self.disabled();
        ids.retain(|s| s != &id);
        if disabled {
            ids.push(id.clone());
        }
        ids.sort();
        self.write_json(&self.data.join("disabled.json"), &json!(ids))?;
        self.get(&id, true)
    }
    pub fn delete(&self, id: &str) -> Result<Value> {
        let id = schema::normalize_id(&json!(id), "skillId")?;
        let _guard = self.mutation()?;
        let path = self.data.join("custom").join(format!("{id}.json"));
        if path.exists() {
            std::fs::remove_file(path)
                .map_err(|e| error(format!("Cannot delete Skill: {e}"), 500))?;
            let ids: Vec<_> = self.disabled().into_iter().filter(|s| s != &id).collect();
            self.write_json(&self.data.join("disabled.json"), &json!(ids))?;
            return Ok(json!({"ok":true,"deleted":id,"disabled":false}));
        }
        if self
            .load(&self.builtin, true, false)?
            .iter()
            .any(|s| text(s, "skillId") == id)
        {
            self.set_disabled(&id, true)?;
            return Ok(json!({"ok":true,"deleted":"","disabled":true,"skillId":id}));
        }
        Err(error("Skill not found", 404))
    }
    pub fn import_pack(&self, config: &Value, overwrite: bool, on_conflict: &str) -> Result<Value> {
        let pack = schema::validate_pack(config)?;
        let id = text(&pack, "packId");
        let mode = if on_conflict.is_empty() {
            "error"
        } else {
            on_conflict
        }
        .trim()
        .to_lowercase();
        if !["error", "overwrite", "skip"].contains(&mode.as_str()) {
            return Err(error(
                "onConflict must be one of error, overwrite, skip",
                400,
            ));
        }
        let _guard = self.mutation()?;
        let embedded: Vec<_> = pack["skills"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|s| !schema::is_reference(s))
            .collect();
        let conflicts: Vec<_> = embedded
            .iter()
            .filter(|s| self.get(&text(s, "skillId"), true).is_ok())
            .map(|s| text(s, "skillId"))
            .collect();
        if !conflicts.is_empty() && !overwrite && mode == "error" {
            return Err(error(
                format!(
                    "Skill Pack import would overwrite existing Skills: {}; set overwrite=true or onConflict=overwrite",
                    conflicts.join(", ")
                ),
                409,
            ));
        }
        let mut installed = Vec::new();
        let mut skipped = Vec::new();
        for skill in embedded {
            let sid = text(skill, "skillId");
            if self.get(&sid, true).is_ok() && !overwrite && mode == "skip" {
                skipped.push(sid);
                continue;
            }
            installed.push(text(
                &self.create(skill, overwrite || mode == "overwrite")?,
                "skillId",
            ));
        }
        let unresolved: Vec<_> = pack["skills"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|s| schema::is_reference(s) && self.get(&text(s, "skillId"), true).is_err())
            .map(|s| text(s, "skillId"))
            .collect();
        let mut manifest = pack.clone();
        manifest["skills"] = json!(
            pack["skills"]
                .as_array()
                .into_iter()
                .flatten()
                .map(|s| json!({"skillId":text(s,"skillId")}))
                .collect::<Vec<_>>()
        );
        manifest["createdAt"] = self.now().into();
        manifest["updatedAt"] = self.now().into();
        self.write_json(
            &self.data.join("packs").join(format!("{id}.json")),
            &manifest,
        )?;
        super::versioning::snapshot(self, &manifest, true, "Imported Skill Pack", "import")?;
        let review = super::security::review(self, &pack, true, true)?;
        let mut public = manifest;
        public["builtin"] = false.into();
        public["skills"]=json!(public["skills"].as_array().into_iter().flatten().map(|e|json!({"skillId":text(e,"skillId"),"name":text(e,"name"),"embedded":!schema::is_reference(e)})).collect::<Vec<_>>());
        Ok(
            json!({"ok":true,"packId":id,"name":text(&pack,"name"),"installedSkills":installed,"skippedSkills":skipped,"conflicts":conflicts,
            "unresolvedReferences":unresolved,"toolPermissions":schema::tool_permissions(&pack),"securityManifest":review["manifest"],"securityReview":review,"pack":public}),
        )
    }
    pub fn delete_pack(&self, id: &str) -> Result<Value> {
        let id = schema::normalize_id(&json!(id), "packId")?;
        let _guard = self.mutation()?;
        let path = self.data.join("packs").join(format!("{id}.json"));
        if path.exists() {
            std::fs::remove_file(path)
                .map_err(|e| error(format!("Cannot delete Skill Pack: {e}"), 500))?;
            return Ok(json!({"ok":true,"deleted":id}));
        }
        if self
            .load(&self.packs, true, true)?
            .iter()
            .any(|p| text(p, "packId") == id)
        {
            return Err(error(
                "Built-in Skill Packs are read-only; export and re-import as a custom Pack to edit",
                403,
            ));
        }
        Err(error("Skill Pack not found", 404))
    }
    pub fn new(root: impl Into<PathBuf>) -> Self {
        let root = root.into();
        Self {
            data: root.join(".skills"),
            builtin: root.join("skills/builtin"),
            packs: root.join("skills/packs"),
            root,
            clock: None,
        }
    }
    pub fn from_env() -> Self {
        let mut registry = Self::new(
            std::env::var_os("DEEPSEEK_INFRA_ROOT")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from(".")),
        );
        for (target, keys) in [
            (&mut registry.data, ["SKILLS_DIR", "DEEPSEEK_SKILLS_DIR"]),
            (
                &mut registry.builtin,
                ["BUILTIN_SKILLS_DIR", "DEEPSEEK_BUILTIN_SKILLS_DIR"],
            ),
            (
                &mut registry.packs,
                ["BUILTIN_PACKS_DIR", "DEEPSEEK_BUILTIN_PACKS_DIR"],
            ),
        ] {
            if let Some(path) = keys
                .iter()
                .filter_map(std::env::var_os)
                .find(|v| !v.is_empty())
            {
                *target = PathBuf::from(path);
            }
        }
        registry
    }
    pub fn now(&self) -> String {
        let seconds = self.clock.unwrap_or_else(|| {
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs() as i64
        });
        crate::core_utils::utc_now_iso(seconds)
    }
    pub fn write_json(&self, path: &Path, value: &Value) -> Result<()> {
        let _guard =
            crate::mutation_gate::mutation_scope(None, self.data.parent().unwrap_or(&self.root))
                .map_err(|e| error(e.to_string(), 409))?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| error(e.to_string(), 500))?;
        }
        let rendered = crate::python_json::OrderedJson::from_value_with_order(value, &[])
            .render_indent_2()
            + "\n";
        std::fs::write(path, rendered).map_err(|e| error(e.to_string(), 500))
    }
    pub fn append_json(&self, path: &Path, value: &Value) -> Result<()> {
        use std::io::Write;
        let _guard =
            crate::mutation_gate::mutation_scope(None, self.data.parent().unwrap_or(&self.root))
                .map_err(|e| error(e.to_string(), 409))?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| error(e.to_string(), 500))?;
        }
        let mut file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .map_err(|e| error(e.to_string(), 500))?;
        writeln!(
            file,
            "{}",
            crate::python_json::dumps_default_separators(value)
        )
        .map_err(|e| error(e.to_string(), 500))
    }
    pub fn packs(&self, include_builtin: bool) -> Result<Vec<Value>> {
        let mut packs = if include_builtin {
            self.load(&self.packs, true, true)?
        } else {
            vec![]
        };
        for pack in self.load(&self.data.join("packs"), false, true)? {
            if let Some(old) = packs.iter_mut().find(|p| p["packId"] == pack["packId"]) {
                *old = pack;
            } else {
                packs.push(pack);
            }
        }
        packs.sort_by_key(|p| (!truth(p, "builtin"), text(p, "name")));
        Ok(packs)
    }
    pub fn get_pack(&self, id: &str) -> Result<Value> {
        let id = schema::normalize_id(&json!(id), "packId")?;
        self.packs(true)?
            .into_iter()
            .find(|p| text(p, "packId") == id)
            .ok_or_else(|| error("Skill Pack not found", 404))
    }
    pub fn export_pack(&self, id: &str) -> Result<Value> {
        let mut pack = self.get_pack(id)?;
        let mut entries = Vec::new();
        for entry in pack["skills"].as_array().into_iter().flatten() {
            if !entry.is_object() {
                continue;
            }
            let mut skill = if schema::is_reference(entry) {
                self.get(&text(entry, "skillId"), true).map_err(|_| {
                    error(
                        format!(
                            "Skill Pack references unknown skillId: {}",
                            text(entry, "skillId")
                        ),
                        404,
                    )
                })?
            } else {
                entry.clone()
            };
            for key in ["builtin", "disabled", "createdAt", "updatedAt"] {
                skill.as_object_mut().unwrap().remove(key);
            }
            entries.push(skill);
        }
        for key in ["builtin", "createdAt", "updatedAt"] {
            pack.as_object_mut().unwrap().remove(key);
        }
        pack["skills"] = entries.into();
        Ok(pack)
    }
    pub fn load(&self, directory: &Path, builtin: bool, pack: bool) -> Result<Vec<Value>> {
        let mut paths: Vec<_> = std::fs::read_dir(directory)
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .filter(|p| p.is_file() && p.extension().is_some_and(|v| v == "json"))
            .collect();
        paths.sort();
        let mut result = Vec::new();
        for path in paths {
            let data = read_json(&path);
            let loaded = if !data.is_object() {
                Err(error(
                    format!(
                        "Invalid Skill{} file: {}",
                        if pack { " Pack" } else { "" },
                        path.display()
                    ),
                    400,
                ))
            } else if pack {
                schema::validate_pack(&data)
            } else {
                schema::validate_skill(&data)
            };
            match loaded {
                Ok(mut value) => {
                    value["builtin"] = builtin.into();
                    for key in ["createdAt", "updatedAt"] {
                        if truth(&data, key) {
                            value[key] = crate::python_json::value_str(&data[key]).into();
                        }
                    }
                    result.push(value);
                }
                Err(e) if builtin => return Err(e),
                Err(_) => (),
            }
        }
        Ok(result)
    }
    pub fn disabled(&self) -> Vec<String> {
        let value = read_json(&self.data.join("disabled.json"));
        let mut ids: Vec<_> = value
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|v| schema::normalize_id(v, "skillId").ok())
            .collect();
        ids.sort();
        ids.dedup();
        ids
    }
    pub fn list(&self, include_disabled: bool, builtin_only: bool) -> Result<Vec<Value>> {
        let disabled = self.disabled();
        let mut skills = self.load(&self.builtin, true, false)?;
        for skill in &mut skills {
            skill["disabled"] = disabled.contains(&text(skill, "skillId")).into();
        }
        if !builtin_only {
            for mut skill in self.load(&self.data.join("custom"), false, false)? {
                skill["disabled"] = (truth(&skill, "disabled")
                    || disabled.contains(&text(&skill, "skillId")))
                .into();
                if let Some(old) = skills.iter_mut().find(|s| s["skillId"] == skill["skillId"]) {
                    *old = skill;
                } else {
                    skills.push(skill);
                }
            }
        }
        skills.retain(|s| include_disabled || !truth(s, "disabled"));
        if builtin_only {
            skills.sort_by_key(|s| text(s, "skillId"));
        } else {
            skills.sort_by_key(|s| (!truth(s, "builtin"), text(s, "name")));
        }
        Ok(skills)
    }
    pub fn get(&self, id: &str, include_disabled: bool) -> Result<Value> {
        let id = schema::normalize_id(&json!(id), "skillId")?;
        let skill = self
            .list(true, false)?
            .into_iter()
            .find(|s| text(s, "skillId") == id)
            .ok_or_else(|| error("Skill not found", 404))?;
        if truth(&skill, "disabled") && !include_disabled {
            return Err(error("Skill is disabled", 403));
        }
        Ok(skill)
    }
    pub fn export(&self, id: &str) -> Result<Value> {
        let mut skill = self.get(id, true)?;
        for key in ["builtin", "createdAt", "updatedAt"] {
            skill.as_object_mut().unwrap().remove(key);
        }
        Ok(skill)
    }
}
