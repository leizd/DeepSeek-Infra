//! Local trust and manifest review, including grant expansion and tamper detection.
use super::{
    Result, error, hash,
    registry::{Registry, read_json},
    schema, strings, text, truth,
};
use crate::tool_policy::tool_metadata;
use base64::{
    Engine, alphabet,
    engine::general_purpose::{GeneralPurpose, GeneralPurposeConfig},
};
use serde_json::{Value, json};

fn finding(kind: &str, field: &str, severity: &str, message: &str, suggestion: &str) -> Value {
    json!({"type":kind,"field":field,"severity":severity,"message":message,"suggestion":suggestion})
}
fn sorted_strings(value: &Value) -> Vec<String> {
    let mut result = strings(value);
    result.sort();
    result.dedup();
    result
}
fn risk_score(tool: &str) -> u64 {
    let Some(m) = tool_metadata(tool) else {
        return if tool.starts_with("mcp__") { 18 } else { 25 };
    };
    let risk = ["low", "medium", "high", "critical"]
        .iter()
        .position(|v| *v == m.risk)
        .unwrap_or(0) as u64;
    (risk * 12
        + 18 * u64::from(m.network)
        + 10 * u64::from(m.filesystem)
        + 20 * u64::from(m.sensitive_sink)
        + 20 * u64::from(m.requires_confirm))
    .min(60)
}
pub fn tool_review(tools: &Value, baseline: &Value) -> Value {
    let tools = sorted_strings(tools);
    let baseline = sorted_strings(baseline);
    let mut findings = Vec::new();
    let mut details = Vec::new();
    let mut score = 0;
    let mut approvals = 0;
    let mut capabilities = Vec::new();
    for tool in &tools {
        let m = tool_metadata(tool);
        let risk = schema::tool_risk_label(tool);
        let s = risk_score(tool);
        score += s;
        if risk == "requires approval" {
            approvals += 1;
        }
        if let Some(m) = m {
            capabilities.push(m.capability);
        }
        details.push(json!({"tool":tool,"risk":risk,"riskScore":s,"network":m.map(|m|m.network).unwrap_or_else(||tool.starts_with("mcp__")),
            "filesystem":m.is_some_and(|m|m.filesystem),"sensitive":m.is_some_and(|m|m.sensitive_sink),"requiresApproval":m.is_some_and(|m|m.requires_confirm)}));
        if m.is_none() {
            findings.push(finding(
                "unknown_tool",
                &format!("allowedTools.{tool}"),
                "medium",
                &format!("Unknown tool grant: {tool}"),
                "Remove unknown tools or register metadata before trusting this Skill.",
            ));
        } else if m.is_some_and(|m| m.requires_confirm || ["high", "critical"].contains(&m.risk)) {
            findings.push(finding(
                "high_risk_tool",
                &format!("allowedTools.{tool}"),
                "high",
                &format!("Tool {tool} is {risk}."),
                "Review the tool grant and require approval before trusting the Skill.",
            ));
        }
    }
    let added: Vec<_> = tools
        .iter()
        .filter(|t| !baseline.contains(t))
        .cloned()
        .collect();
    let risky: Vec<_> = added
        .iter()
        .filter(|t| risk_score(t) >= 15)
        .cloned()
        .collect();
    if !baseline.is_empty() && !risky.is_empty() {
        findings.push(finding(
            "tool_grant_expanded",
            "allowedTools",
            "medium",
            &format!("allowedTools expanded: {}", risky.join(", ")),
            "Review newly granted network/filesystem/sensitive capabilities before upgrade.",
        ));
    }
    capabilities.sort();
    capabilities.dedup();
    json!({"tools":details,"riskScore":score.min(100),"requiresApprovalCount":approvals,"capabilities":capabilities,
        "toolGrantDiff":{"added":added,"removed":baseline.iter().filter(|t|!tools.contains(t)).collect::<Vec<_>>()},"findings":findings})
}
const PATTERNS: &[(&str, &str, &str, &str)] = &[
    (
        "prompt_injection",
        "high",
        r"ignore\s+(all\s+)?previous\s+instructions|disregard\s+(all\s+)?previous\s+instructions|system\s+override",
        "Remove instructions that try to override upstream system or developer guidance.",
    ),
    (
        "secret_exfiltration",
        "high",
        r"exfiltrate|send\s+secrets?|steal\s+secrets?|leak\s+secrets?|upload\s+.*(?:secret|token|api[_ -]?key)",
        "Remove instructions that request exposing credentials, tokens, or private files.",
    ),
    (
        "secret_file_access",
        "high",
        r"(?:read|cat|open)\s+(?:~[/\\]\.ssh|\.env|id_rsa|id_ed25519|credentials|secrets?)",
        "Do not instruct Skills to read credential stores or secret-bearing files.",
    ),
    (
        "network_exfiltration",
        "medium",
        r"curl\s+https?://|wget\s+https?://|post\s+to\s+https?://|send\s+to\s+https?://",
        "Keep network usage explicit and bounded to the Skill allowedTools policy.",
    ),
    (
        "hidden_tool_instruction",
        "medium",
        r"hidden\s+tool|covert\s+tool|do\s+not\s+tell\s+the\s+user|secretly\s+(?:call|use)",
        "Remove hidden tool-use instructions; tool grants must be visible in allowedTools.",
    ),
];
fn scan(fields: Vec<(String, String)>) -> Vec<Value> {
    let mut findings = Vec::new();
    let encoded = regex::Regex::new(r"\b[A-Za-z0-9+/]{32,}={0,2}\b").unwrap();
    let secret =
        regex::Regex::new(r"(?i)(secret|api[_ -]?key|token|password|ssh|\.env|exfiltrate)")
            .unwrap();
    let decoder = GeneralPurpose::new(
        &alphabet::STANDARD,
        GeneralPurposeConfig::new()
            .with_decode_padding_mode(base64::engine::DecodePaddingMode::Indifferent)
            .with_decode_allow_trailing_bits(true),
    );
    for (field, value) in fields {
        if value.is_empty() {
            continue;
        }
        for (kind, severity, pattern, suggestion) in PATTERNS {
            if regex::Regex::new(&format!("(?i){pattern}"))
                .unwrap()
                .is_match(&value)
            {
                findings.push(finding(
                    kind,
                    &field,
                    severity,
                    &format!("Suspicious instruction in {field}."),
                    suggestion,
                ));
            }
        }
        if encoded.find_iter(&value).any(|m| {
            decoder
                .decode(m.as_str().trim_end_matches('='))
                .ok()
                .is_some_and(|v| secret.is_match(&String::from_utf8_lossy(&v)))
        }) {
            findings.push(finding(
                "encoded_suspicious_text",
                &field,
                "medium",
                &format!("Base64-like encoded sensitive instruction in {field}."),
                "Remove encoded instructions from Skill metadata and prompts.",
            ));
        }
    }
    findings
}
fn skill_payload(skill: &Value) -> Value {
    json!(
        schema::REQUIRED
            .iter()
            .map(|key| ((*key).to_string(), skill[*key].clone()))
            .collect::<serde_json::Map<_, _>>()
    )
}
pub fn manifest(item: &Value, pack: bool) -> Value {
    let data = if pack {
        json!({"packId":item["packId"],"name":item["name"],"description":item["description"],"version":item["version"],"author":item["author"],
        "skills":item["skills"].as_array().into_iter().flatten().filter(|v|v.is_object()).map(|v|if schema::is_reference(v) {json!({"skillId":text(v,"skillId")})} else {skill_payload(v)}).collect::<Vec<_>>()})
    } else {
        skill_payload(item)
    };
    let mut result = json!({"schemaVersion":"skill-security-manifest.v1","kind":if pack {"pack"} else {"skill"},"version":text(&data,"version"),
        "contentHash":format!("sha256:{}",hash(&data)),"packId":"","reviewStatus":"","signed":false});
    if pack {
        result["packId"] = text(&data, "packId").into();
        result["schemaHash"] = format!(
            "sha256:{}",
            hash(&json!(
                data["skills"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|v| text(v, "skillId"))
                    .collect::<Vec<_>>()
            ))
        )
        .into();
        result["promptHash"] = format!(
            "sha256:{}",
            hash(&json!(
                data["skills"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|v| v["systemPrompt"].clone())
                    .collect::<Vec<_>>()
            ))
        )
        .into();
        let mut tools: Vec<_> = data["skills"]
            .as_array()
            .into_iter()
            .flatten()
            .flat_map(|v| strings(&v["allowedTools"]))
            .collect();
        tools.sort();
        tools.dedup();
        result["toolGrantHash"] = format!("sha256:{}", hash(&json!(tools))).into();
    } else {
        result["skillId"] = text(&data, "skillId").into();
        result["schemaHash"] = format!(
            "sha256:{}",
            hash(&json!({"inputSchema":data["inputSchema"],"outputSchema":data["outputSchema"]}))
        )
        .into();
        result["promptHash"] =
            format!("sha256:{}", hash(&json!(text(&data, "systemPrompt")))).into();
        let mut tools = strings(&data["allowedTools"]);
        tools.sort();
        result["toolGrantHash"] = format!("sha256:{}", hash(&json!(tools))).into();
    }
    result
}
pub fn trust_store(registry: &Registry) -> Value {
    let mut store = read_json(&registry.data.join("security/trust-store.json"));
    if !store.is_object() {
        store = json!({});
    }
    for (key, value) in [
        ("schemaVersion", json!("skill-trust-store.v1")),
        ("skills", json!({})),
        ("packs", json!({})),
    ] {
        store.as_object_mut().unwrap().entry(key).or_insert(value);
    }
    store
}
pub fn summary(r: &Registry, scope: &str) -> Result<Value> {
    let mut skills = Vec::new();
    let mut packs = Vec::new();
    if matches!(scope, "all" | "skills") {
        for skill in r.list(true, false)? {
            skills.push(review(r, &skill, false, false)?);
        }
    }
    if matches!(scope, "all" | "packs") {
        for pack in r.packs(true)? {
            packs.push(review(r, &pack, true, false)?);
        }
    }
    let all: Vec<_> = skills.iter().chain(&packs).collect();
    let count = |status: &str| all.iter().filter(|v| v["reviewStatus"] == status).count();
    let average = (all
        .iter()
        .map(|v| v["riskScore"].as_i64().unwrap_or(0))
        .sum::<i64>() as f64
        / all.len().max(1) as f64
        * 100.0)
        .round_ties_even()
        / 100.0;
    let high:Vec<_>=all.iter().filter(|v|matches!(text(v,"reviewStatus").as_str(),"high-risk"|"blocked")).map(|v|json!({"kind":v["kind"],"id":if truth(v,"skillId") {&v["skillId"]} else {&v["packId"]},"riskScore":v["riskScore"]})).collect();
    Ok(
        json!({"ok":true,"scope":scope,"generatedAt":r.now(),"summary":{"skillCount":skills.len(),"packCount":packs.len(),"trusted":count("trusted"),"needsReview":count("needs-review"),"highRisk":count("high-risk"),"blocked":count("blocked"),"averageRiskScore":average},"skills":skills,"packs":packs,"highRiskItems":high}),
    )
}
pub fn run_context(r: &Registry, skill: &Value, approved: bool, persist: bool) -> Result<Value> {
    let review = review(r, skill, false, persist)?;
    let required = review["reviewStatus"] == "high-risk" && review["trustLevel"] != "trusted";
    let reason = if review["reviewStatus"] == "blocked" {
        let stored = text(
            &trust_store(r)["skills"][text(skill, "skillId")],
            "blockedReason",
        );
        if stored.is_empty() {
            "Skill is blocked by local security review".into()
        } else {
            stored
        }
    } else if required && !approved {
        "High-risk Skill requires explicit securityApproved=true before running".into()
    } else {
        String::new()
    };
    Ok(
        json!({"review":review,"blocked":!reason.is_empty(),"blockedReason":reason,"approvalRequired":required,"approved":approved}),
    )
}
pub fn run_metadata(context: &Value) -> Value {
    let review = &context["review"];
    json!({"runSecurityLevel":if truth(review,"reviewStatus") {text(review,"reviewStatus")} else {"needs-review".into()},"securityReviewId":text(review,"reviewId"),"trustedAtRun":review["reviewStatus"]=="trusted","toolGrantHashAtRun":text(&review["manifest"],"toolGrantHash"),"blockedReason":text(context,"blockedReason"),"approvalRequired":truth(context,"approvalRequired")})
}
pub fn review(registry: &Registry, raw: &Value, pack: bool, persist: bool) -> Result<Value> {
    let mut current = if pack {
        raw.clone()
    } else {
        schema::validate_skill(raw).unwrap_or_else(|_| raw.clone())
    };
    for key in ["builtin", "disabled", "createdAt", "updatedAt"] {
        if let Some(v) = raw.get(key) {
            current[key] = v.clone();
        }
    }
    let id = if pack {
        text(&current, "packId")
    } else {
        schema::normalize_id(&current["skillId"], "skillId")?
    };
    let trust = trust_store(registry)[if pack { "packs" } else { "skills" }][&id].clone();
    let mut expanded = current.clone();
    if pack {
        expanded["skills"] = json!(
            current["skills"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|e| e.is_object())
                .map(|e| if schema::is_reference(e) {
                    registry
                        .get(&text(e, "skillId"), true)
                        .unwrap_or_else(|_| e.clone())
                } else {
                    e.clone()
                })
                .collect::<Vec<_>>()
        );
    }
    let manifest = manifest(&expanded, pack);
    let mut fields: Vec<_> = if pack {
        vec!["name", "description", "author"]
    } else {
        vec!["name", "description", "systemPrompt"]
    }
    .into_iter()
    .map(|k| (k.to_string(), text(&current, k)))
    .collect();
    if !pack {
        for name in ["inputSchema", "outputSchema"] {
            let s = &current[name];
            if truth(s, "description") {
                fields.push((format!("{name}.description"), text(s, "description")));
            }
            if let Some(props) = s["properties"].as_object() {
                for (key, child) in props {
                    if child.is_object() && truth(child, "description") {
                        fields.push((
                            format!("{name}.properties.{key}.description"),
                            text(child, "description"),
                        ));
                    }
                }
            }
        }
    }
    let mut findings = scan(fields);
    let mut children = Vec::new();
    let mut tools = Vec::new();
    if pack {
        for child in expanded["skills"].as_array().into_iter().flatten() {
            let value = if schema::is_reference(child) {
                registry
                    .get(&text(child, "skillId"), true)
                    .and_then(|v| review(registry, &v, false, false))
            } else {
                review(registry, child, false, false)
            };
            match value {
                Ok(r) => {
                    children.push(json!({"skillId":r["skillId"],"reviewStatus":r["reviewStatus"],"riskScore":r["riskScore"],"findingCount":r["findings"].as_array().map_or(0,Vec::len),"toolGrantHash":r["manifest"]["toolGrantHash"]}));
                    tools.extend(strings(&child["allowedTools"]));
                    for f in
                        r["findings"].as_array().into_iter().flatten().filter(|v| {
                            ["high", "critical"].contains(&text(v, "severity").as_str())
                        })
                    {
                        let mut f = f.clone();
                        f["field"] =
                            format!("skills.{}.{}", text(&r, "skillId"), text(&f, "field")).into();
                        findings.push(f);
                    }
                }
                Err(_) => findings.push(finding(
                    "unresolved_skill_reference",
                    "skills",
                    "high",
                    &format!("Pack references unknown Skill {}.", text(child, "skillId")),
                    "Install or remove the unresolved Skill reference.",
                )),
            }
        }
    } else {
        tools = strings(&current["allowedTools"]);
    }
    let tool_review = tool_review(&json!(tools), &trust["allowedTools"]);
    findings.extend(
        tool_review["findings"]
            .as_array()
            .into_iter()
            .flatten()
            .cloned(),
    );
    let tampered = text(&trust, "status") == "trusted"
        && truth(&trust, "contentHash")
        && trust["contentHash"] != manifest["contentHash"];
    if tampered {
        findings.push(finding(
            "tamper_detected",
            "contentHash",
            "high",
            "Trusted Skill content hash changed since trust was granted.",
            "Review the diff and trust the new content only after validation.",
        ));
    }
    let score = (tool_review["riskScore"].as_u64().unwrap_or(0)
        + children
            .iter()
            .map(|v| v["riskScore"].as_u64().unwrap_or(0))
            .sum::<u64>()
            / 4
        + findings
            .iter()
            .map(|f| match text(f, "severity").as_str() {
                "critical" => 60,
                "high" => 35,
                "medium" => 15,
                _ => 5,
            })
            .sum::<u64>())
    .min(100);
    let status = if text(&trust, "status") == "blocked" {
        "blocked"
    } else if !tampered && (text(&trust, "status") == "trusted" || truth(&current, "builtin")) {
        "trusted"
    } else if score >= 70
        || findings
            .iter()
            .any(|v| ["critical", "high"].contains(&text(v, "severity").as_str()))
    {
        "high-risk"
    } else if !findings.is_empty() || score >= 25 {
        "needs-review"
    } else {
        "local-custom"
    };
    let kind = if pack { "pack" } else { "skill" };
    let digest = text(&manifest, "contentHash");
    let digest = digest.split(':').nth(1).unwrap_or("");
    let mut result = json!({"schemaVersion":"skill-security-review.v1","reviewId":format!("sec-{kind}-{id}-{}",&digest[..digest.len().min(16)]).chars().take(120).collect::<String>(),
        "kind":kind,"name":if truth(&current,"name") {text(&current,"name")} else {id.clone()},"version":text(&current,"version"),"builtin":truth(&current,"builtin"),
        "trustLevel":status,"reviewStatus":status,"riskScore":score,"allowedToolsRisk":tool_review["tools"],"requiresApprovalCount":tool_review["requiresApprovalCount"],
        "capabilities":tool_review["capabilities"],"findings":findings,"manifest":manifest,"lastSecurityReviewAt":registry.now(),"signed":false});
    result[if pack { "packId" } else { "skillId" }] = id.into();
    if pack {
        result["skillReviews"] = children.into();
    }
    if persist {
        registry.append_json(&registry.data.join("security/reviews.jsonl"), &result)?;
    }
    Ok(result)
}
pub fn change_trust(
    registry: &Registry,
    id: &str,
    action: &str,
    reason: &str,
    pack: bool,
) -> Result<Value> {
    let key = if pack { "packId" } else { "skillId" };
    let id = schema::normalize_id(&json!(id), key)?;
    let bucket = if pack { "packs" } else { "skills" };
    let mut store = trust_store(registry);
    let mut result = if action == "untrust" {
        let removed = store[bucket]
            .as_object_mut()
            .and_then(|v| v.remove(&id))
            .is_some_and(|v| crate::core_utils::python_truthy(&v));
        json!({"ok":true,"removed":removed,"trustLevel":"needs-review"})
    } else {
        let current = if pack {
            registry.export_pack(&id)?
        } else {
            registry.get(&id, true)?
        };
        let r = review(registry, &current, pack, true)?;
        let m = &r["manifest"];
        let status = if action == "block" {
            "blocked"
        } else {
            "trusted"
        };
        let mut entry = json!({"status":status,"reviewId":r["reviewId"],"contentHash":m["contentHash"],"schemaHash":m["schemaHash"],"promptHash":m["promptHash"],
            "toolGrantHash":m["toolGrantHash"],"allowedTools":r["allowedToolsRisk"].as_array().into_iter().flatten().map(|v|v["tool"].clone()).collect::<Vec<_>>(),"updatedAt":registry.now()});
        let mut output = json!({"ok":true,"trustLevel":status,"review":r});
        if action == "block" {
            entry["blockedAt"] = registry.now().into();
            entry["blockedReason"] = if reason.is_empty() {
                "Blocked by local security review"
            } else {
                reason
            }
            .chars()
            .take(500)
            .collect::<String>()
            .into();
            output["blockedReason"] = entry["blockedReason"].clone();
        } else {
            output["securityManifest"] = m.clone();
        }
        if !store[bucket].is_object() {
            return Err(error("Invalid Skill trust store", 400));
        }
        store[bucket][&id] = entry;
        output
    };
    store["schemaVersion"] = "skill-trust-store.v1".into();
    store["updatedAt"] = registry.now().into();
    registry.write_json(&registry.data.join("security/trust-store.json"), &store)?;
    result[key] = id.into();
    Ok(result)
}
