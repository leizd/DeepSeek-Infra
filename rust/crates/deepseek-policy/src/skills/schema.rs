//! Skill and pack schema normalization, matching `infra/skills/schema.py` and `pack.py`.
use super::{Result, error, text, truth};
use crate::core_utils::text_or_empty;
use crate::tool_policy::{all_tool_names, tool_metadata};
use serde_json::{Value, json};

pub const REQUIRED: &[&str] = &[
    "skillId",
    "name",
    "description",
    "version",
    "systemPrompt",
    "inputSchema",
    "outputSchema",
    "allowedTools",
    "memoryPolicy",
    "artifactPolicy",
    "projectBinding",
];
const TYPES: &[&str] = &[
    "object", "array", "string", "integer", "number", "boolean", "null",
];

pub fn normalize_id(value: &Value, key: &str) -> Result<String> {
    let id = text_or_empty(Some(value)).trim().to_string();
    if !(3..=80).contains(&id.len())
        || !id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"_:-".contains(&c))
    {
        return Err(error(
            format!("{key} must be 3-80 chars and contain only letters, numbers, _, :, or -"),
            400,
        ));
    }
    Ok(id)
}

fn required(config: &Value, fields: &[&str], label: &str) -> Result<()> {
    let Some(object) = config.as_object() else {
        return Err(error(format!("{label} must be an object"), 400));
    };
    let missing: Vec<_> = fields
        .iter()
        .copied()
        .filter(|key| !object.contains_key(*key))
        .collect();
    if !missing.is_empty() {
        return Err(error(
            format!("{label} missing required fields: {}", missing.join(", ")),
            400,
        ));
    }
    Ok(())
}

pub fn validate_skill(config: &Value) -> Result<Value> {
    required(config, REQUIRED, "Skill config")?;
    let mut data = config.clone();
    data["skillId"] = normalize_id(&data["skillId"], "skillId")?.into();
    for (key, limit) in [
        ("name", 120),
        ("description", 600),
        ("version", 40),
        ("systemPrompt", 20_000),
    ] {
        let value = text(&data, key).trim().to_string();
        if value.is_empty() {
            return Err(error(format!("{key} is required"), 400));
        }
        data[key] = value.chars().take(limit).collect::<String>().into();
    }
    for key in ["inputSchema", "outputSchema"] {
        data[key] = validate_json_schema(&data[key], key)?;
    }
    data["allowedTools"] = json!(allowed_tools(&data["allowedTools"])?);
    if !data["memoryPolicy"].is_object() {
        return Err(error("memoryPolicy must be an object", 400));
    }
    let scope = match text(&data["memoryPolicy"], "scope").as_str() {
        "" => "none".into(),
        value => value.trim().to_lowercase(),
    };
    if !["none", "global", "project"].contains(&scope.as_str()) {
        return Err(error(
            "memoryPolicy.scope must be one of none, global, project",
            400,
        ));
    }
    data["memoryPolicy"] = json!({"scope":scope,"read":truth(&data["memoryPolicy"],"read"),"write":truth(&data["memoryPolicy"],"write")});
    if !data["artifactPolicy"].is_object() {
        return Err(error("artifactPolicy must be an object", 400));
    }
    let Some(types) = data["artifactPolicy"]["types"].as_array() else {
        return Err(error("artifactPolicy.types must be a list", 400));
    };
    let mut normalized = Vec::new();
    for item in types {
        let kind = text_or_empty(Some(item))
            .trim()
            .to_lowercase()
            .trim_start_matches('.')
            .to_string();
        if kind.is_empty() {
            continue;
        }
        if !["docx", "pdf", "pptx", "md", "svg"].contains(&kind.as_str()) {
            return Err(error(
                format!("artifactPolicy.types contains unsupported type: {kind}"),
                400,
            ));
        }
        if !normalized.contains(&kind) {
            normalized.push(kind);
        }
    }
    data["artifactPolicy"] =
        json!({"autoSave":truth(&data["artifactPolicy"],"autoSave"),"types":normalized});
    if !data["projectBinding"].is_object() {
        return Err(error("projectBinding must be an object", 400));
    }
    data["projectBinding"] = json!({"enabled":truth(&data["projectBinding"],"enabled")});
    data["browserPolicy"] = if data["browserPolicy"].is_null() {
        json!({})
    } else {
        let policy = &data["browserPolicy"];
        if !policy.is_object() {
            return Err(error("browserPolicy must be an object", 400));
        }
        json!({"allowClick":truth(policy,"allowClick"),"allowType":truth(policy,"allowType"),
            "allowDownload":truth(policy,"allowDownload"),"requireConfirmation":policy.get("requireConfirmation").map(crate::core_utils::python_truthy).unwrap_or(true)})
    };
    if !data["exampleInputs"].is_array() {
        data["exampleInputs"] = json!([]);
    }
    data["disabled"] = truth(&data, "disabled").into();
    Ok(data)
}

pub fn allowed_tools(value: &Value) -> Result<Vec<String>> {
    let Some(items) = value.as_array() else {
        return Err(error("allowedTools must be a list", 400));
    };
    let known = all_tool_names();
    let mut tools = Vec::new();
    for item in items {
        let tool = text_or_empty(Some(item)).trim().to_string();
        if tool.is_empty() {
            continue;
        }
        if !known.contains(&tool.as_str()) && !tool.starts_with("mcp__") {
            return Err(error(
                format!("allowedTools contains unknown tool: {tool}"),
                400,
            ));
        }
        if !tools.contains(&tool) {
            tools.push(tool);
        }
    }
    Ok(tools)
}

pub fn skill_allowed_tools(skill: &Value) -> Result<Vec<String>> {
    let tools = allowed_tools(&skill["allowedTools"])?;
    let policy = &skill["browserPolicy"];
    if policy.as_object().is_none_or(|p| p.is_empty()) {
        return Ok(tools);
    }
    Ok(tools
        .into_iter()
        .filter(|tool| match tool.as_str() {
            "browser_click" => truth(policy, "allowClick"),
            "browser_type_text" | "browser_select" => truth(policy, "allowType"),
            "browser_download" => truth(policy, "allowDownload"),
            _ => true,
        })
        .collect())
}

pub fn validate_json_schema(schema: &Value, label: &str) -> Result<Value> {
    if schema.is_null() {
        return Ok(json!({}));
    }
    if !schema.is_object() {
        return Err(error(format!("{label} must be an object"), 400));
    }
    validate_schema_node(schema, label)?;
    Ok(schema.clone())
}

fn validate_schema_node(schema: &Value, label: &str) -> Result<()> {
    if let Some(kind) = schema.get("type").filter(|v| !v.is_null()) {
        let items = kind
            .as_array()
            .cloned()
            .unwrap_or_else(|| vec![kind.clone()]);
        let unknown: Vec<_> = items
            .iter()
            .filter(|v| !v.as_str().is_some_and(|s| TYPES.contains(&s)))
            .map(crate::python_json::value_str)
            .collect();
        if !unknown.is_empty() {
            return Err(error(
                format!(
                    "{label}.type contains unsupported values: {}",
                    unknown.join(", ")
                ),
                400,
            ));
        }
    }
    if let Some(properties) = schema.get("properties").filter(|v| !v.is_null()) {
        let Some(fields) = properties.as_object() else {
            return Err(error(format!("{label}.properties must be an object"), 400));
        };
        for (key, child) in fields {
            if !child.is_object() {
                return Err(error(
                    format!("{label}.properties must map strings to schema objects"),
                    400,
                ));
            }
            validate_schema_node(child, &format!("{label}.properties.{key}"))?;
        }
    }
    if let Some(required) = schema.get("required").filter(|v| !v.is_null()) {
        if !required
            .as_array()
            .is_some_and(|items| items.iter().all(Value::is_string))
        {
            return Err(error(
                format!("{label}.required must be a list of strings"),
                400,
            ));
        }
    }
    if let Some(items) = schema.get("items").filter(|v| !v.is_null()) {
        if !items.is_object() {
            return Err(error(format!("{label}.items must be an object"), 400));
        }
        validate_schema_node(items, &format!("{label}.items"))?;
    }
    Ok(())
}

pub fn is_reference(entry: &Value) -> bool {
    entry.is_object()
        && !REQUIRED
            .iter()
            .any(|key| *key != "skillId" && entry.get(*key).is_some())
}

/// The oracle's lightweight instance validator (not a general JSON Schema engine).
pub fn validate_instance(value: &Value, schema: &Value, label: &str) -> Vec<String> {
    let mut errors = Vec::new();
    if let Some(expected) = schema.get("type").filter(|v| !v.is_null()) {
        let kinds = expected
            .as_array()
            .cloned()
            .unwrap_or_else(|| vec![expected.clone()]);
        if !kinds
            .iter()
            .any(|kind| match crate::python_json::value_str(kind).as_str() {
                "object" => value.is_object(),
                "array" => value.is_array(),
                "string" => value.is_string(),
                "integer" => value.as_i64().is_some() || value.as_u64().is_some(),
                "number" => value.is_number(),
                "boolean" => value.is_boolean(),
                "null" => value.is_null(),
                _ => true,
            })
        {
            return vec![format!(
                "{label} must be {}",
                crate::python_json::value_str(expected)
            )];
        }
    }
    if let Some(items) = schema["enum"].as_array().filter(|v| !v.is_empty()) {
        if !items.iter().any(|v| python_equal(value, v)) {
            errors.push(format!(
                "{label} must be one of {}",
                crate::python_json::value_str(&schema["enum"])
            ));
        }
    }
    if let (Some(pattern), Some(text)) = (schema["pattern"].as_str(), value.as_str()) {
        if let Ok(re) = fancy_regex::RegexBuilder::new(pattern)
            .backtrack_limit(1_000_000)
            .build()
        {
            if !re.is_match(text).unwrap_or(false) {
                errors.push(format!("{label} does not match pattern"));
            }
        }
    }
    if let Some(fields) = value.as_object() {
        for key in schema["required"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            if !fields.contains_key(key) {
                errors.push(format!("{label}.{key} is required"));
            }
        }
        let props = schema["properties"].as_object();
        if schema["additionalProperties"] == false {
            for key in fields.keys() {
                if !props.is_some_and(|p| p.contains_key(key)) {
                    errors.push(format!("{label}.{key} is not allowed"));
                }
            }
        }
        if let Some(props) = props {
            for (key, child) in props {
                if let Some(v) = fields.get(key) {
                    if child.is_object() {
                        errors.extend(validate_instance(v, child, &format!("{label}.{key}")));
                    }
                }
            }
        }
    } else if let Some(items) = value.as_array() {
        if schema["items"].is_object() {
            for (i, v) in items.iter().enumerate() {
                errors.extend(validate_instance(
                    v,
                    &schema["items"],
                    &format!("{label}[{i}]"),
                ));
            }
        }
    }
    errors
}
fn python_equal(a: &Value, b: &Value) -> bool {
    let number = |v: &Value| {
        v.as_f64()
            .or_else(|| v.as_bool().map(|b| if b { 1.0 } else { 0.0 }))
    };
    if let (Some(a), Some(b)) = (number(a), number(b)) {
        return a == b;
    }
    match (a, b) {
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(a, b)| python_equal(a, b))
        }
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(k, v)| b.get(k).is_some_and(|b| python_equal(v, b)))
        }
        _ => a == b,
    }
}

pub fn validate_pack(config: &Value) -> Result<Value> {
    required(
        config,
        &["packId", "name", "description", "version", "skills"],
        "Skill Pack config",
    )?;
    let mut pack = config.clone();
    pack["packId"] = normalize_id(&pack["packId"], "packId")?.into();
    for (key, limit) in [("name", 160), ("description", 1200), ("version", 40)] {
        let value = text(&pack, key).trim().to_string();
        if value.is_empty() {
            return Err(error(format!("{key} is required"), 400));
        }
        pack[key] = value.chars().take(limit).collect::<String>().into();
    }
    let author = text(&pack, "author");
    pack["author"] = if author.is_empty() { "local" } else { &author }
        .trim()
        .chars()
        .take(120)
        .collect::<String>()
        .into();
    let Some(entries) = pack["skills"].as_array().filter(|v| !v.is_empty()) else {
        return Err(error("skills must be a non-empty list", 400));
    };
    let mut result = Vec::new();
    let mut seen = Vec::new();
    for (i, entry) in entries.iter().enumerate() {
        if !entry.is_string() && !entry.is_object() {
            return Err(error(
                format!("skills[{i}] must be a skillId string or a Skill config object"),
                400,
            ));
        }
        let id = if entry.is_string() {
            text_or_empty(Some(entry))
        } else {
            text(entry, "skillId")
        };
        if id.trim().is_empty() {
            return Err(error(format!("skills[{i}] skillId is required"), 400));
        }
        let item = if entry.is_string() || is_reference(entry) {
            normalize_id(&json!(id), "skillId").map(|id| json!({"skillId":id}))
        } else {
            validate_skill(entry)
        }
        .map_err(|e| error(format!("skills[{i}] {}", e.message), 400))?;
        let id = text(&item, "skillId");
        if seen.contains(&id) {
            return Err(error(format!("duplicate skillId in pack: {id}"), 400));
        }
        seen.push(id);
        result.push(item);
    }
    pack["skills"] = result.into();
    Ok(pack)
}

pub fn tool_risk_label(name: &str) -> &str {
    let Some(meta) = tool_metadata(name) else {
        return if name.starts_with("mcp__") {
            "mcp"
        } else {
            "unknown"
        };
    };
    if meta.requires_confirm {
        "requires approval"
    } else if meta.network {
        "network"
    } else if meta.filesystem {
        "filesystem"
    } else if meta.sensitive_sink {
        "sensitive"
    } else {
        meta.risk
    }
}

pub fn tool_permissions(pack: &Value) -> Value {
    json!(pack["skills"].as_array().into_iter().flatten().filter(|e| e.is_object()).map(|e| {
        let tools: Vec<_> = if is_reference(e) {vec![]} else {e["allowedTools"].as_array().into_iter().flatten().filter_map(|v| {
            let tool = text_or_empty(Some(v)).trim().to_string();
            if tool.is_empty() {None} else {Some(json!({"tool":tool,"risk":tool_risk_label(&tool),"requiresApproval":tool_risk_label(&tool)=="requires approval"}))}
        }).collect()};
        json!({"skillId":text(e,"skillId"),"embedded":!is_reference(e),"allowedTools":tools})
    }).collect::<Vec<_>>())
}
