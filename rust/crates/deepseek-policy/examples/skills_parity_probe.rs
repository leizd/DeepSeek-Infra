//! Offline probe: JSON cases in, real native skill-module results out.
use deepseek_policy::skills::{self, registry::Registry, schema, security, versioning};
use serde_json::{Value, json};
use std::io::{self, Read};
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
            _ => Err(skills::error("Unknown probe operation", 400)),
        };
        output.push(match result {
            Ok(v) => json!({"ok":v}),
            Err(e) => json!({"error":e.message}),
        });
    }
    println!("{}", json!(output));
}
