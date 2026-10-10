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

use serde_json::{Value, json};

use crate::python_json::OrderedJson;

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

/// The catalog with the oracle's **key order** preserved, parsed from the same asset
/// through [`crate::python_json::loads`].
///
/// [`available_tool_definitions`] cannot carry this order: it goes through
/// `serde_json::Value`, whose `Map` sorts. The lost order is load-bearing once a
/// definition travels into an upstream request body — `parameters.properties` names
/// each tool's properties in **declaration order** (not alphabetical: `sessionId`
/// before `projectId`, `query` before `intent`), and every property object orders
/// `type` before `description`. That order is per-tool and per-property, so no table
/// keyed by *name* can express it; the trees themselves have to carry it, which is why
/// they live here and the assembly borrows them when it renders a body.
///
/// The two parses are positionally aligned — both read `CATALOG_JSON`, so index *i* is
/// the same definition in both — which is what lets [`ordered_tool_definition`] check
/// identity against the `Value` view.
pub fn ordered_tool_definitions() -> &'static [OrderedJson] {
    static PARSED: OnceLock<Vec<OrderedJson>> = OnceLock::new();
    PARSED.get_or_init(|| match crate::python_json::loads(CATALOG_JSON) {
        Ok(OrderedJson::List(items)) => items,
        Ok(other) => panic!("the embedded catalog asset must be a JSON array, found {other:?}"),
        Err(error) => panic!("the embedded catalog asset must parse: {error}"),
    })
}

/// Tool name -> position in the catalog, for the ordered lookup.
///
/// The local catalog has unique names (asserted by a test); a duplicate would keep the
/// first, and `ordered_tool_definition`'s equality guard would then simply refuse the
/// second — visible, not silently mis-ordered.
fn ordered_index() -> &'static BTreeMap<String, usize> {
    static INDEX: OnceLock<BTreeMap<String, usize>> = OnceLock::new();
    INDEX.get_or_init(|| {
        let mut index = BTreeMap::new();
        for (position, definition) in available_tool_definitions().iter().enumerate() {
            let Some(function) = definition.get("function").and_then(Value::as_object) else {
                continue;
            };
            let name = crate::python_json::value_str(function.get("name").unwrap_or(&Value::Null));
            if name.is_empty() {
                continue;
            }
            index.entry(name).or_insert(position);
        }
        index
    })
}

/// The ordered tree for one definition as it travels into a request body, or `None`
/// when this catalog does not own that definition **byte for byte**.
///
/// The equality check is deliberate. The orders are the *catalog's*; a definition that
/// has drifted from the catalog (edited by some other path) must not silently borrow
/// them and render as if it were the catalog's own. `None` sends it back through the
/// generic by-name renderer, where the difference stays visible in the output instead
/// of being papered over.
pub fn ordered_tool_definition(definition: &Value) -> Option<OrderedJson> {
    let function = definition.get("function")?.as_object()?;
    let name = crate::python_json::value_str(function.get("name").unwrap_or(&Value::Null));
    let position = *ordered_index().get(&name)?;
    if available_tool_definitions().get(position)? != definition {
        return None;
    }
    ordered_tool_definitions().get(position).cloned()
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

const MUTATING_MCP_TOOLS: &[&str] = &[
    "create_pptx",
    "create_document",
    "create_mindmap",
    "create_reminder",
    "suggest_memory",
    "forget_memory",
];

/// MCP `tools/list` catalog, mirroring `infra.mcp.registry.mcp_tools` for the
/// local runtime (external `mcp__*` profiles are not appended here).
pub fn mcp_tools(capability: &str) -> Vec<Value> {
    let capability = {
        let trimmed = capability.trim();
        if trimmed.is_empty() { "full" } else { trimmed }
    };
    let allowed: std::collections::HashSet<&str> = if capability == "full" {
        crate::tool_policy::all_tool_names().into_iter().collect()
    } else {
        crate::tool_policy::capability_tools(capability)
            .into_iter()
            .collect()
    };
    let mut tools = Vec::new();
    for definition in available_tool_definitions() {
        let Some(function) = definition.get("function").and_then(Value::as_object) else {
            continue;
        };
        let Some(name) = function
            .get("name")
            .and_then(Value::as_str)
            .filter(|n| !n.is_empty())
        else {
            continue;
        };
        if !allowed.contains(name) {
            continue;
        }
        let parameters = function
            .get("parameters")
            .cloned()
            .unwrap_or(json!({"type": "object"}));
        let mut tool = json!({
            "name": name,
            "description": function.get("description").and_then(Value::as_str).unwrap_or(""),
            "inputSchema": if parameters.is_object() { parameters } else { json!({"type": "object"}) },
        });
        if let Some(meta) = crate::tool_policy::tool_metadata(name) {
            tool.as_object_mut().unwrap().insert(
                "annotations".to_string(),
                json!({
                    "title": name,
                    "readOnlyHint": !MUTATING_MCP_TOOLS.contains(&name),
                    "destructiveHint": meta.requires_confirm,
                    "openWorldHint": meta.network,
                }),
            );
        }
        tools.push(tool);
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
        let mcp = mcp_tools("full");
        assert!(mcp.iter().any(|tool| tool["name"] == "python_eval"));
        let pptx = mcp
            .iter()
            .find(|tool| tool["name"] == "create_pptx")
            .unwrap();
        assert_eq!(pptx["annotations"]["readOnlyHint"], false);
        assert_eq!(mcp_tools("researcher").len(), 3);
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

    /// The strongest available check of the ordered parse: the whole committed asset,
    /// re-rendered from the parsed trees, must reproduce its own bytes. The asset *is*
    /// `json.dumps(tools, ensure_ascii=False, indent=2)`, so this compares the parse and
    /// the indent renderer against 28 real schemas at once — escapes, CJK text and
    /// nesting included.
    #[test]
    fn the_asset_round_trips_through_the_ordered_parser() {
        let ordered = ordered_tool_definitions();
        assert_eq!(ordered.len(), 28);
        let rerendered = OrderedJson::List(ordered.to_vec()).render_indent_2();
        assert_eq!(rerendered, catalog_json());
    }

    /// Declaration order is not alphabetical, and the ordered tree keeps it.
    #[test]
    fn ordered_definitions_carry_declaration_order() {
        let web_search = available_tool_definitions()
            .iter()
            .find(|tool| tool["function"]["name"] == "web_search")
            .expect("web_search");
        let ordered = ordered_tool_definition(web_search).expect("the catalog owns it");
        let rendered = ordered.render_default_separators();
        // `query` is declared before `intent`, and the property object puts `type` first.
        assert!(
            rendered.contains(r#""properties": {"query": {"type": "string""#),
            "{rendered}"
        );

        // The generic renderer cannot know either, which is the gap this closes.
        let generic = OrderedJson::from_value_with_orders(web_search, &[], &[]);
        let generic_rendered = generic.render_default_separators();
        assert!(!generic_rendered.contains(r#""properties": {"query""#));
        assert_ne!(rendered, generic_rendered);
    }

    /// Every catalog definition is served its own tree, and the names the index relies
    /// on are unique.
    #[test]
    fn every_definition_is_served_its_own_order() {
        let mut names = std::collections::BTreeSet::new();
        for (position, tool) in available_tool_definitions().iter().enumerate() {
            let name = tool["function"]["name"].as_str().expect("name").to_string();
            assert!(names.insert(name.clone()), "duplicate tool name {name}");
            let ordered =
                ordered_tool_definition(tool).unwrap_or_else(|| panic!("{name} is not served"));
            assert_eq!(ordered, ordered_tool_definitions()[position]);
        }
    }

    /// A definition the catalog does not own — drifted, unknown, or malformed — is
    /// refused rather than re-rendered with the catalog's orders.
    #[test]
    fn a_definition_the_catalog_does_not_own_is_never_reordered() {
        let mut drifted = available_tool_definitions()[0].clone();
        drifted["function"]["description"] = serde_json::json!("edited elsewhere");
        assert!(ordered_tool_definition(&drifted).is_none());

        assert!(ordered_tool_definition(&serde_json::json!({"type": "function"})).is_none());
        assert!(
            ordered_tool_definition(&serde_json::json!({
                "type": "function",
                "function": {"name": "mcp__remote", "parameters": {"type": "object"}},
            }))
            .is_none()
        );
    }
}
