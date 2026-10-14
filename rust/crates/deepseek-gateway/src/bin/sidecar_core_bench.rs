use std::fmt::Write as _;
use std::hint::black_box;
use std::io::{self, Read};
use std::path::Path;
use std::time::Instant;

use blake2::{Blake2s256, Digest};
use deepseek_policy::capability::{Capability, RiskLevel, is_capability_allowed};
use deepseek_policy::path_guard::{PathPolicy, validate_workspace_path};
use deepseek_policy::url_guard::{UrlPolicy, validate_url_access};
use serde::Deserialize;
use serde_json::{Value, json};

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Command {
    component: String,
    payload: Value,
    warmups: usize,
    iterations: usize,
}

fn parse_enum<T: serde::de::DeserializeOwned>(value: Option<&Value>) -> Result<T, String> {
    value
        .cloned()
        .ok_or_else(|| "missing benchmark policy field".to_string())
        .and_then(|value| {
            serde_json::from_value(value).map_err(|_| "invalid benchmark policy field".to_string())
        })
}

fn run_component(component: &str, payload: &Value) -> Result<Value, String> {
    match component {
        "gateway_prepare" => Ok(
            match deepseek_gateway::request_preparation::prepare_request(payload) {
                Ok(request) => json!({
                    "ok": true,
                    "request": request,
                    "diagnostics": {"runtime": "rust", "normalized": true}
                }),
                Err(error) => error.response(),
            },
        ),
        "mcp_prepare" => {
            let payload_size = serde_json::to_vec(payload)
                .map_err(|error| error.to_string())?
                .len();
            Ok(deepseek_mcp::prepare_protocol_value(payload, payload_size))
        }
        "policy_url" => {
            let url = payload
                .get("url")
                .and_then(Value::as_str)
                .ok_or_else(|| "missing benchmark URL".to_string())?;
            let capability = payload
                .get("capability")
                .map(|value| parse_enum(Some(value)))
                .transpose()?
                .unwrap_or(Capability::NetworkFetch);
            let risk_level = payload
                .get("risk_level")
                .map(|value| parse_enum(Some(value)))
                .transpose()?
                .unwrap_or(RiskLevel::High);
            serde_json::to_value(
                validate_url_access(url, &UrlPolicy::default())
                    .with_context(capability, risk_level),
            )
            .map_err(|error| error.to_string())
        }
        "policy_path" => {
            let root = payload
                .get("root")
                .and_then(Value::as_str)
                .ok_or_else(|| "missing benchmark root".to_string())?;
            let requested = payload
                .get("requested")
                .and_then(Value::as_str)
                .ok_or_else(|| "missing benchmark path".to_string())?;
            let capability = payload
                .get("capability")
                .map(|value| parse_enum(Some(value)))
                .transpose()?
                .unwrap_or(Capability::ReadFile);
            let risk_level = payload
                .get("risk_level")
                .map(|value| parse_enum(Some(value)))
                .transpose()?
                .unwrap_or(RiskLevel::High);
            serde_json::to_value(
                validate_workspace_path(Path::new(root), Path::new(requested), &PathPolicy)
                    .with_context(capability, risk_level),
            )
            .map_err(|error| error.to_string())
        }
        "policy_capability" => {
            let requested: Capability = parse_enum(payload.get("requested"))?;
            let max_risk: RiskLevel = parse_enum(payload.get("max_risk"))?;
            let granted = payload
                .get("granted")
                .and_then(Value::as_array)
                .ok_or_else(|| "missing benchmark grants".to_string())?
                .iter()
                .map(|value| parse_enum(Some(value)))
                .collect::<Result<Vec<Capability>, String>>()?;
            serde_json::to_value(is_capability_allowed(requested, &granted, max_risk))
                .map_err(|error| error.to_string())
        }
        "rag_vector_rank" => {
            let query: Vec<f64> = serde_json::from_value(
                payload
                    .get("query")
                    .cloned()
                    .ok_or_else(|| "missing benchmark query".to_string())?,
            )
            .map_err(|error| error.to_string())?;
            let candidates: Vec<Vec<f64>> = serde_json::from_value(
                payload
                    .get("candidates")
                    .cloned()
                    .ok_or_else(|| "missing benchmark candidates".to_string())?,
            )
            .map_err(|error| error.to_string())?;
            let best = deepseek_rag::vector::best_match(&query, &candidates);
            Ok(json!({
                "index": best.map(|(index, _)| index),
                "similarity": best.map_or(0.0, |(_, similarity)| similarity)
            }))
        }
        "rag_document_prepare" => {
            let payload_size = serde_json::to_vec(payload)
                .map_err(|error| error.to_string())?
                .len();
            Ok(deepseek_rag::document_preparation::prepare_document_value(
                payload,
                payload_size,
            ))
        }
        _ => Err("unsupported benchmark component".to_string()),
    }
}

fn semantic_value(component: &str, value: &Value) -> Value {
    if component == "gateway_prepare" {
        if value.get("ok").and_then(Value::as_bool) == Some(true) {
            return json!({"ok": true, "request": value.get("request")});
        }
        return json!({"ok": false, "code": value.get("code")});
    }
    if component.starts_with("policy_") {
        return json!({
            "allowed": value.get("allowed"),
            "code": value.get("code"),
            "capability": value.get("capability"),
            "risk_level": value.get("risk_level")
        });
    }
    value.clone()
}

fn output_hash(component: &str, value: &Value) -> String {
    // The workspace preserves map insertion order. Python's benchmark hashes
    // sorted JSON, so normalize every object before comparing semantic hashes.
    let mut semantic = semantic_value(component, value);
    semantic.sort_all_objects();
    let encoded = serde_json::to_vec(&semantic).unwrap_or_default();
    let digest = Blake2s256::digest(encoded);
    let mut rendered = String::with_capacity(digest.len() * 2);
    for byte in digest {
        write!(&mut rendered, "{byte:02x}").expect("writing to a String cannot fail");
    }
    rendered
}

fn main() {
    let mut input = String::new();
    if let Err(error) = io::stdin().read_to_string(&mut input) {
        eprintln!("failed to read benchmark command: {error}");
        std::process::exit(2);
    }
    let command: Command = match serde_json::from_str(&input) {
        Ok(command) => command,
        Err(error) => {
            eprintln!("invalid benchmark command: {error}");
            std::process::exit(2);
        }
    };
    if command.iterations == 0 || command.iterations > 1000 || command.warmups > 100 {
        eprintln!("benchmark warmup or iteration count is outside the bounded contract");
        std::process::exit(2);
    }

    for _ in 0..command.warmups {
        match run_component(&command.component, &command.payload) {
            Ok(value) => {
                black_box(value);
            }
            Err(error) => {
                eprintln!("pure Rust warmup failed: {error}");
                std::process::exit(1);
            }
        }
    }

    let mut samples_us = Vec::with_capacity(command.iterations);
    let mut errors = 0;
    let mut output_bytes = 0;
    let mut semantic_hash = String::new();
    for _ in 0..command.iterations {
        let started = Instant::now();
        match run_component(&command.component, &command.payload) {
            Ok(value) => {
                output_bytes = serde_json::to_vec(&value).map_or(0, |encoded| encoded.len());
                semantic_hash = output_hash(&command.component, &value);
                black_box(value);
            }
            Err(_) => errors += 1,
        }
        samples_us.push(started.elapsed().as_secs_f64() * 1_000_000.0);
    }
    let profile = if cfg!(debug_assertions) {
        "debug"
    } else {
        "release"
    };
    println!(
        "{}",
        serde_json::to_string(&json!({
            "profile": profile,
            "component": command.component,
            "warmups": command.warmups,
            "iterations": command.iterations,
            "samplesUs": samples_us,
            "errors": errors,
            "outputBytes": output_bytes,
            "semanticHash": semantic_hash
        }))
        .expect("benchmark result must serialize")
    );
}

#[cfg(test)]
mod semantic_hash_tests {
    use super::*;

    #[test]
    fn object_order_matches_the_python_semantic_digest() {
        let first: Value = serde_json::from_str(
            r#"{"ok":true,"request":{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hello"}]}}"#,
        )
        .unwrap();
        let reordered: Value = serde_json::from_str(
            r#"{"request":{"messages":[{"content":"hello","role":"user"}],"model":"deepseek-v4-pro"},"ok":true}"#,
        )
        .unwrap();
        let digest = output_hash("gateway_prepare", &first);
        // Public Python sorted-JSON/BLAKE2s fixture digest; not a credential.
        assert_eq!(
            digest,
            "910163f57b02a655f1a5efa8d2d5209a46f94b48f3dc62306b403a64bef5c6f9" // pragma: allowlist secret
        );
        assert_eq!(digest, output_hash("gateway_prepare", &reordered));
    }

    #[test]
    fn request_values_and_array_order_remain_distinct() {
        let request = json!({"ok": true, "request": {"messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "reply"}
        ]}});
        let digest = output_hash("gateway_prepare", &request);
        let mut changed = request.clone();
        changed["request"]["messages"][0]["content"] = json!("changed");
        assert_ne!(digest, output_hash("gateway_prepare", &changed));
        let mut reversed = request;
        reversed["request"]["messages"]
            .as_array_mut()
            .unwrap()
            .reverse();
        assert_ne!(digest, output_hash("gateway_prepare", &reversed));
    }
}
