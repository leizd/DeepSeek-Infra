//! The tool catalog, mirroring `available_tool_definitions` /
//! `agent_tool_definitions` / `tool_parameter_schemas` / `schema_for_tool` from
//! `infra/tool_runtime/tools.py`.
//!
//! The 28 local definitions are **not** transcribed by hand. They are the oracle's own
//! `json.dumps(available_tool_definitions(), ensure_ascii=False, indent=2)` bytes, kept
//! in `assets/tool_catalog_v1.json` and embedded verbatim. Hand-copying 40 KB of
//! descriptions and JSON Schema is where typos live, and a typo in a `parameters` block
//! would silently change what the model is allowed to send.
//!
//! The asset is regenerated from the oracle and compared byte-for-byte by
//! `tasks/native-runtime/tool_catalog_parity_probe.py`, which is the evidence that the
//! committed bytes still match.
//!
//! # Key order
//!
//! The asset preserves the oracle's rendering. Anything that *re-serializes* a
//! definition — rather than passing the stored text through — must go through
//! [`crate::python_json::OrderedJson`], because this crate's `serde_json` has no
//! `preserve_order` and would emit sorted keys where Python emits insertion order. That
//! matters if these definitions ever reach an upstream request body, since the body is
//! part of the prompt prefix.

use std::collections::BTreeMap;
use std::sync::OnceLock;

use serde_json::Value;

/// The oracle's exact `json.dumps(available_tool_definitions(), ensure_ascii=False, indent=2)`.
const CATALOG_JSON: &str = include_str!("../assets/tool_catalog_v1.json");

/// Mirrors `available_tool_definitions`: the **local-only** catalog.
///
/// The parsed form is built once. `serde_json` iterates object keys in sorted order, so
/// the *within-definition* key order differs from the oracle's; the array order — which
/// is the part callers depend on — is preserved.
pub fn available_tool_definitions() -> &'static [Value] {
    static PARSED: OnceLock<Vec<Value>> = OnceLock::new();
    PARSED
        .get_or_init(|| {
            serde_json::from_str::<Vec<Value>>(CATALOG_JSON)
                .expect("the embedded catalog asset must be valid JSON")
        })
        .as_slice()
}

/// The embedded asset's exact bytes, for the parity probe and for callers that must
/// forward the oracle's rendering rather than a re-serialization.
pub fn catalog_json() -> &'static str {
    CATALOG_JSON
}

/// Mirrors `tool_parameter_schemas`: local tool name -> its declared `parameters`.
///
/// The oracle caches this in a module global; the index is built once here. Entries are
/// skipped exactly as the oracle skips them — a definition without a `function` object,
/// without a name, or whose `parameters` is not an object contributes nothing.
pub fn tool_parameter_schemas() -> &'static BTreeMap<String, Value> {
    static INDEX: OnceLock<BTreeMap<String, Value>> = OnceLock::new();
    INDEX.get_or_init(|| {
        let mut index = BTreeMap::new();
        for definition in available_tool_definitions() {
            let Some(function) = definition.get("function").and_then(Value::as_object) else {
                continue;
            };
            let name = crate::python_json::value_str(function.get("name").unwrap_or(&Value::Null));
            let Some(parameters) = function.get("parameters") else {
                continue;
            };
            if name.is_empty() || !parameters.is_object() {
                continue;
            }
            index.insert(name, parameters.clone());
        }
        index
    })
}

/// The one thing `schema_for_tool` cannot answer locally: an external MCP profile's
/// input schema, which lives behind the MCP bridge.
///
/// Injected rather than looked up, because `infra.mcp.bridge` is a separate subsystem.
/// `None` is the honest default: the oracle's own `except Exception: return None` arm
/// produces the same answer when the bridge is unavailable, so this reproduces the
/// oracle's degradation path rather than inventing one.
pub type ExternalSchema = dyn Fn(&str) -> Option<Value>;

/// Mirrors `schema_for_tool`.
///
/// A `mcp__`-prefixed name goes to the bridge; everything else is the local index. Note
/// the oracle only accepts an external schema that is a **dict** — a list or a scalar
/// profile schema is `None`, not passed through.
pub fn schema_for_tool(name: &str, external: Option<&ExternalSchema>) -> Option<Value> {
    let tool_name = name.trim();
    if tool_name.starts_with("mcp__") {
        let schema = external?(tool_name)?;
        return if schema.is_object() {
            Some(schema)
        } else {
            None
        };
    }
    tool_parameter_schemas().get(tool_name).cloned()
}

/// Mirrors `agent_tool_definitions`: the local catalog, plus any external MCP profiles
/// appended with an `[External MCP: …]` marker in the description.
///
/// With no bridge the result is the local catalog unchanged — the oracle's own behaviour
/// when `external_mcp_registry` is empty or raises.
pub fn agent_tool_definitions(external_profiles: Option<&[Value]>) -> Vec<Value> {
    let mut tools: Vec<Value> = available_tool_definitions().to_vec();
    let Some(profiles) = external_profiles else {
        return tools;
    };
    for profile in profiles {
        let Some(object) = profile.as_object() else {
            continue;
        };
        let tool = crate::python_json::value_str(object.get("tool").unwrap_or(&Value::Null));
        let raw_schema = object.get("input_schema").cloned().unwrap_or(Value::Null);
        // `parameters = raw_schema if raw_schema.get("type") == "object" else
        // {"type": "object", "properties": raw_schema}` — the non-object case puts the
        // **whole** raw schema under `properties`, not its entries.
        let parameters = match &raw_schema {
            Value::Object(fields)
                if fields.get("type") == Some(&Value::String("object".to_string())) =>
            {
                raw_schema.clone()
            }
            other => serde_json::json!({"type": "object", "properties": other}),
        };
        let server = crate::python_json::value_str(object.get("server").unwrap_or(&Value::Null));
        // `schema_desc = str(raw_schema.get("description") or profile.tool)`.
        let description = crate::python_json::value_str(
            raw_schema
                .get("description")
                .unwrap_or(&Value::String(tool.clone())),
        );
        // The bridged name is the profile's own field, not something derived here.
        let name = crate::python_json::value_str(
            object
                .get("bridged_name")
                .unwrap_or(&Value::String(tool.clone())),
        );
        tools.push(serde_json::json!({
            "type": "function",
            "function": {
                "name": name,
                "strict": true,
                "description": format!("[External MCP: {server}] {description}"),
                "parameters": parameters,
            }
        }));
    }
    tools
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_asset_carries_the_local_catalog() {
        let tools = available_tool_definitions();
        assert_eq!(tools.len(), 28, "the local catalog has 28 tools");
        // Array order is the oracle's declaration order, and callers depend on it.
        assert_eq!(tools[0]["function"]["name"], "web_search");
        // Every entry is a function definition with a name and an object schema.
        for tool in tools {
            assert_eq!(tool["type"], "function", "{tool}");
            let function = tool["function"].as_object().expect("function object");
            assert!(
                function["name"]
                    .as_str()
                    .is_some_and(|name| !name.is_empty()),
                "{tool}"
            );
            assert!(function["parameters"].is_object(), "{tool}");
        }
    }

    #[test]
    fn the_schema_index_covers_every_definition() {
        let index = tool_parameter_schemas();
        assert_eq!(index.len(), 28);
        assert!(index.contains_key("create_reminder"));
        // A definition's schema is the same object its definition carries.
        let from_index = index.get("generate_chart").expect("generate_chart");
        let from_catalog = available_tool_definitions()
            .iter()
            .find(|tool| tool["function"]["name"] == "generate_chart")
            .expect("generate_chart");
        assert_eq!(from_index, &from_catalog["function"]["parameters"]);
    }

    #[test]
    fn schema_for_tool_reads_the_local_index_and_trims() {
        assert!(schema_for_tool("web_search", None).is_some());
        assert!(schema_for_tool("  web_search  ", None).is_some());
        assert!(schema_for_tool("no_such_tool", None).is_none());
        assert!(schema_for_tool("", None).is_none());
    }

    /// The `mcp__` arm goes to the injected bridge, and only accepts an object schema —
    /// a scalar or list profile schema is `None`, matching the oracle's `isinstance`
    /// check.
    #[test]
    fn schema_for_tool_routes_mcp_names_to_the_bridge() {
        let bridge = |name: &str| -> Option<Value> {
            match name {
                "mcp__ok" => Some(serde_json::json!({"type": "object"})),
                "mcp__scalar" => Some(serde_json::json!("nope")),
                _ => None,
            }
        };
        assert_eq!(
            schema_for_tool("mcp__ok", Some(&bridge)),
            Some(serde_json::json!({"type": "object"}))
        );
        assert!(schema_for_tool("mcp__scalar", Some(&bridge)).is_none());
        // An unknown external name is `None` even with a bridge present.
        assert!(schema_for_tool("mcp__missing", Some(&bridge)).is_none());
        // Without a bridge every `mcp__` name is `None`, never a panic.
        assert!(schema_for_tool("mcp__ok", None).is_none());
        // A local name never consults the bridge.
        assert!(schema_for_tool("web_search", Some(&bridge)).is_some());
    }

    #[test]
    fn agent_tool_definitions_is_the_local_catalog_without_a_bridge() {
        let agent = agent_tool_definitions(None);
        assert_eq!(agent.len(), 28);
        assert_eq!(agent, available_tool_definitions());
        // An empty profile list is the same as none.
        assert_eq!(agent_tool_definitions(Some(&[])).len(), 28);
    }

    #[test]
    fn an_external_profile_is_appended_and_marked() {
        let profiles = vec![serde_json::json!({
            "tool": "remote_search",
            "bridged_name": "mcp__remote_search",
            "server": "acme",
            "input_schema": {"type": "object", "description": "does a thing"},
        })];
        let agent = agent_tool_definitions(Some(&profiles));
        assert_eq!(agent.len(), 29);
        let appended = &agent[28];
        assert_eq!(appended["function"]["name"], "mcp__remote_search");
        assert_eq!(appended["function"]["strict"], true);
        assert_eq!(
            appended["function"]["description"],
            "[External MCP: acme] does a thing"
        );

        // A free-form schema is wrapped so `parameters` stays valid JSON Schema.
        let wrapped = agent_tool_definitions(Some(&[serde_json::json!({
            "tool": "free",
            "bridged_name": "mcp__free",
            "server": "s",
            "input_schema": {"q": {"type": "string"}},
        })]));
        assert_eq!(wrapped[28]["function"]["parameters"]["type"], "object");
        // The whole free-form schema becomes , as the oracle wraps it.
        assert_eq!(
            wrapped[28]["function"]["parameters"]["properties"]["q"]["type"],
            "string"
        );
    }
}
