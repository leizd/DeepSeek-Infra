//! The synchronous parts of a Skill run; the gateway supplies the native async LLM exchange.
use super::{
    Result, analytics, error, evidence, project_integration as projects, registry::Registry,
    schema, security, strings, templates, text, trace::Trace, truth,
};
use crate::entropy::Entropy;
use serde_json::{Value, json};

pub struct Run {
    pub skill: Value,
    pub result: Value,
    pub options: Value,
    pub context: String,
    trace: Trace,
}
fn boolean(options: &Value, key: &str, default: bool) -> bool {
    options
        .get(key)
        .map(|v| {
            v.as_bool()
                .unwrap_or_else(|| crate::core_utils::web_truthy(Some(v)))
        })
        .unwrap_or(default)
}
fn fail_before(
    r: &Registry,
    skill: &Value,
    result: &Value,
    options: &Value,
    e: crate::app_error::AppError,
    category: &str,
) -> crate::app_error::AppError {
    if boolean(options, "persist", true) {
        if let Err(write) = analytics::record(
            r,
            skill,
            result,
            boolean(options, "offline", false),
            &text(options, "model"),
            Some((&e.message, category)),
        ) {
            return write;
        }
    }
    e
}
pub fn prepare(r: &Registry, id: &str, input: &Value, options: &Value) -> Result<Run> {
    let skill = r.get(id, false)?;
    let offline = boolean(options, "offline", false);
    let persist = boolean(options, "persist", true);
    let security = security::run_context(
        r,
        &skill,
        boolean(options, "securityApproved", false)
            || boolean(options, "approveSecurityReview", false),
        persist,
    )?;
    let mut result = json!({"ok":true,"skillRunId":format!("run-{}",r.new_id()?),"skillId":skill["skillId"],"skillVersion":skill["version"],"projectId":text(options,"projectId"),"status":"completed","input":input,"startedAt":r.now(),"security":security::run_metadata(&security)});
    if truth(&security, "blocked") {
        return Err(fail_before(
            r,
            &skill,
            &result,
            options,
            error(text(&security, "blockedReason"), 403),
            "security_review_blocked",
        ));
    }
    let violations = schema::validate_instance(input, &skill["inputSchema"], "input");
    if !input.is_object() || !violations.is_empty() {
        let message = if !input.is_object() {
            "Skill input must be an object".into()
        } else {
            format!(
                "Skill input failed schema validation: {}",
                violations.join("; ")
            )
        };
        return Err(fail_before(
            r,
            &skill,
            &result,
            options,
            error(message, 400),
            "schema_validation_failed",
        ));
    }
    let project_id = text(options, "projectId");
    let binding = truth(&skill["projectBinding"], "enabled");
    let project = if !project_id.is_empty() && binding {
        match projects::require(r, &project_id) {
            Ok(v) => v,
            Err(e) => {
                return Err(fail_before(
                    r,
                    &skill,
                    &result,
                    options,
                    e,
                    "project_binding_failed",
                ));
            }
        }
    } else {
        Value::Null
    };
    let context = [
        templates::project_context(&project),
        super::media::context(r, input, &project_id)?,
    ]
    .into_iter()
    .filter(|v| !v.is_empty())
    .collect::<Vec<_>>()
    .join("\n\n");
    let trace = Trace::start(r, &skill, &result, offline)?;
    result["traceId"] = trace.id.clone().into();
    if !binding {
        result["projectId"] = "".into();
    }
    Ok(Run {
        skill,
        result,
        options: options.clone(),
        context,
        trace,
    })
}
impl Run {
    pub fn offline_output(&self) -> Value {
        json!({"content":templates::offline(&self.skill,&self.result["input"],&self.context),"mode":"offline"})
    }
    pub fn request(&self) -> Result<Value> {
        let allowed = schema::skill_allowed_tools(&self.skill)?;
        let project = text(&self.options, "projectId");
        let mut payload = json!({"apiKey":text(&self.options,"apiKey"),"tavilyApiKey":text(&self.options,"tavilyApiKey"),"systemPrompt":templates::system(&self.skill,&self.context),
            "messages":[{"role":"user","content":templates::user(&self.result["input"]),"projectId":project}],"allowedTools":allowed,"searchEnabled":allowed.iter().any(|v|v=="web_search"||v=="compare_sources"),
            "memoryEnabled":truth(&self.skill["memoryPolicy"],"read"),"memoryScope":if self.skill["memoryPolicy"]["scope"]=="project"&&!project.is_empty() {format!("project:{project}")} else {"global".into()},"skillRun":{"skillId":self.skill["skillId"],"projectId":project}});
        if truth(&self.options, "model") {
            payload["model"] = self.options["model"].clone();
        }
        Ok(payload)
    }
    fn artifacts(&self, r: &Registry, output: &Value) -> Result<(Vec<Value>, Vec<Value>)> {
        let mut artifacts = Vec::new();
        let mut saved = Vec::new();
        if !boolean(&self.options, "persist", true)
            || !truth(&self.skill["artifactPolicy"], "autoSave")
        {
            return Ok((artifacts, saved));
        }
        let project = text(&self.result, "projectId");
        let content = text(output, "content");
        let title = if truth(output, "title") {
            text(output, "title")
        } else {
            text(&self.skill, "name")
        };
        let source = json!({"type":"skill_run","skillId":self.skill["skillId"],"skillRunId":self.result["skillRunId"],"projectId":project});
        if !project.is_empty() && !content.is_empty() {
            saved.push(projects::save_item(r, &project, &title, &content, &source)?);
        }
        if strings(&self.skill["artifactPolicy"]["types"]).contains(&"md".into())
            && !content.is_empty()
        {
            if let Some(artifact) = evidence::markdown(r, &title, &content, &source)? {
                artifacts.push(artifact);
            }
        }
        let mut files = Vec::new();
        evidence::files(output, "", &mut files);
        for file in files {
            if let Some(artifact) = evidence::register(r, &file, &source, &text(&file, "tool"))? {
                if !artifacts
                    .iter()
                    .any(|v| v["artifactId"] == artifact["artifactId"])
                {
                    artifacts.push(artifact);
                }
            }
        }
        if !project.is_empty() {
            for artifact in &artifacts {
                projects::link_artifact(r, &project, artifact)?;
            }
        }
        Ok((artifacts, saved))
    }
    pub fn finish(mut self, r: &Registry, output: Result<Value>) -> Result<Value> {
        let outcome = (|| {
            let output = output?;
            let violations =
                schema::validate_instance(&output, &self.skill["outputSchema"], "output");
            if !violations.is_empty() {
                return Err(error(
                    format!(
                        "Skill output failed schema validation: {}",
                        violations.join("; ")
                    ),
                    500,
                ));
            }
            let (artifacts, saved) = self.artifacts(r, &output)?;
            self.result["output"] = output;
            self.result["artifacts"] = artifacts.into();
            self.result["savedItems"] = saved.into();
            self.result["completedAt"] = r.now().into();
            self.result["policy"] =
                json!({"allowedTools":schema::skill_allowed_tools(&self.skill)?});
            if boolean(&self.options, "persist", true) {
                let record = analytics::record(
                    r,
                    &self.skill,
                    &self.result,
                    boolean(&self.options, "offline", false),
                    &text(&self.options, "model"),
                    None,
                )?;
                self.result["packId"] = record["packId"].clone();
                self.result["latencyMs"] = record["latencyMs"].clone();
                self.result["analytics"] = record.clone();
                if truth(&self.result, "projectId") {
                    projects::append_run(
                        r,
                        &text(&self.result, "projectId"),
                        &analytics::project_record(&record, &self.result["input"]),
                    )?;
                }
            }
            Ok(self.result.clone())
        })();
        if let Err(e) = &outcome {
            if boolean(&self.options, "persist", true) {
                let record = analytics::record(
                    r,
                    &self.skill,
                    &self.result,
                    boolean(&self.options, "offline", false),
                    &text(&self.options, "model"),
                    Some((&e.message, "")),
                )?;
                if truth(&self.result, "projectId") {
                    let _ = projects::append_run(
                        r,
                        &text(&self.result, "projectId"),
                        &analytics::project_record(&record, &self.result["input"]),
                    );
                }
            }
        }
        self.trace.finish(
            r,
            &self.result,
            outcome.as_ref().err().map_or("", |e| e.message.as_str()),
        )?;
        outcome
    }
}
pub fn offline(r: &Registry, id: &str, input: &Value, options: &Value) -> Result<Value> {
    let mut options = options.clone();
    options["offline"] = true.into();
    let run = prepare(r, id, input, &options)?;
    let output = run.offline_output();
    run.finish(r, Ok(output))
}
pub fn dry_run(r: &Registry, id: &str, input: &Value) -> Result<Value> {
    let skill = r.get(id, false)?;
    let violations = schema::validate_instance(input, &skill["inputSchema"], "input");
    if !violations.is_empty() {
        return Err(error(
            format!(
                "Skill input failed schema validation: {}",
                violations.join("; ")
            ),
            400,
        ));
    }
    let output = json!({"content":templates::offline(&skill,input,""),"mode":"offline"});
    let violations = schema::validate_instance(&output, &skill["outputSchema"], "output");
    if !violations.is_empty() {
        return Err(error(
            format!(
                "Skill output failed schema validation: {}",
                violations.join("; ")
            ),
            400,
        ));
    }
    Ok(
        json!({"ok":true,"skillRunId":"dry-run","skillId":skill["skillId"],"skillVersion":skill["version"],"projectId":"","status":"completed","input":input,"output":output,"artifacts":[],"savedItems":[],"traceId":"","startedAt":r.now(),"completedAt":r.now(),"policy":{"allowedTools":schema::skill_allowed_tools(&skill)?},"dryRun":true}),
    )
}
