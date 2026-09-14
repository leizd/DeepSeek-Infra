//! Parity probe: print how `request_preparation::prepare_request` handles the
//! normalization cases that distinguish it from the Python oracle.
//!
//! This exists because the Rust/Python differences in this area are not
//! documented anywhere authoritative - they were found by running both
//! implementations against the same inputs. Pair it with the Python side to
//! re-measure after either implementation changes:
//!
//! ```text
//! cargo run -p deepseek-gateway --example oracle_parity_probe
//! ```
//!
//! The companion Python probe extracts `normalize_chat_messages` (and its
//! helpers) straight from `deepseek_infra/infra/gateway/deepseek_client.py` and
//! `chat_payload.py` via `ast`, stubbing only `build_attachment_context`, so it
//! exercises the real oracle rather than a reimplementation. See
//! `tasks/native-runtime/worker-execution-plan.md` for the recorded output of
//! both sides and the resulting decision.
//!
//! This is a diagnostic, not a test: it asserts nothing, because the point is
//! to show the current behavior of both sides for a human to compare. The
//! guarding tests live in `request_preparation::tests`.

use serde_json::json;

fn main() {
    let cases: Vec<(&str, serde_json::Value)> = vec![
        (
            "A blank user then real",
            json!({"model":"deepseek-v4-pro","messages":[{"role":"user","content":"  "},{"role":"user","content":"real"}]}),
        ),
        (
            "B only blank user",
            json!({"model":"deepseek-v4-pro","messages":[{"role":"user","content":"   "}]}),
        ),
        (
            "C blank assistant then real",
            json!({"model":"deepseek-v4-pro","messages":[{"role":"assistant","content":""},{"role":"user","content":"real"}]}),
        ),
        (
            "D content null then real",
            json!({"model":"deepseek-v4-pro","messages":[{"role":"user","content":null},{"role":"user","content":"real"}]}),
        ),
        (
            "E system then real",
            json!({"model":"deepseek-v4-pro","messages":[{"role":"system","content":"sys"},{"role":"user","content":"real"}]}),
        ),
        (
            "F non-dict then real",
            json!({"model":"deepseek-v4-pro","messages":["nope",{"role":"user","content":"real"}]}),
        ),
        (
            "G tool missing id",
            json!({"model":"deepseek-v4-pro","messages":[{"role":"tool","content":"x"},{"role":"user","content":"real"}]}),
        ),
        (
            "H assistant tool_calls blank",
            json!({"model":"deepseek-v4-pro","messages":[{"role":"assistant","content":"","tool_calls":[{"id":"c1","type":"function","function":{"name":"f","arguments":"{}"}}]}]}),
        ),
        (
            "I user content list text",
            json!({"model":"deepseek-v4-pro","messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]}),
        ),
    ];
    for (name, body) in cases {
        match deepseek_gateway::request_preparation::prepare_request(&body) {
            Ok(prepared) => {
                let roles: Vec<&str> = prepared["messages"]
                    .as_array()
                    .map(|items| {
                        items
                            .iter()
                            .filter_map(|item| item["role"].as_str())
                            .collect()
                    })
                    .unwrap_or_default();
                println!("{:30} OK  out={:?}", name, roles);
            }
            Err(error) => {
                println!("{:30} ERR {} ({})", name, error.code, error.message)
            }
        }
    }
}
