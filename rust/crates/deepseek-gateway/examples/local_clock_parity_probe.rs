//! OS-local clock/zone parity probe, Rust side.
//!
//! Replays the same measurement as `tasks/native-runtime/local_clock_parity_probe.py`: the
//! Python side resolves the machine's zone through CPython's own path, this side through
//! `deepseek_gateway::local_clock`, and both render the same pinned instant with the oracle's
//! `format_current_time_context`, so the outputs compare byte for byte.
//!
//! The epoch arrives on the command line because the two processes run seconds apart and each
//! one's own instant would otherwise differ. That pins the timestamp, not the zone: the offset
//! and the name printed here are this host's, read by [`system_zone`].
//!
//! The output is a `BTreeMap` rather than a `serde_json::Map` on purpose. `Map` is a `BTreeMap`
//! in a single-crate build and an `IndexMap` once the workspace's `preserve_order` feature is
//! unified in, so its order depends on how the probe was built; the Python side sorts, and this
//! side has to sort for the same reason — see `python_json::sorted_fields` for the same rule.
//!
//! Usage (the shell half lives in the Python probe's module doc)::
//!
//!     cargo run -p deepseek-gateway --example local_clock_parity_probe -- <epoch-seconds>

use std::collections::BTreeMap;

use deepseek_gateway::local_clock::system_zone;
use deepseek_policy::dynamic_context::format_current_time_context;
use serde_json::{Value, json};

fn main() {
    let epoch = match std::env::args().nth(1) {
        Some(raw) => raw.parse::<i64>().expect("the epoch must be whole seconds"),
        None => {
            eprintln!("usage: local_clock_parity_probe <epoch-seconds>");
            std::process::exit(2);
        }
    };

    let zone = system_zone().expect("the host reports a zone");
    let now = zone.local_now(epoch);

    let mut out: BTreeMap<String, Value> = BTreeMap::new();
    out.insert("epoch".to_string(), json!(epoch));
    out.insert("is_daylight".to_string(), json!(zone.is_daylight));
    out.insert("offset_seconds".to_string(), json!(now.offset_seconds));
    out.insert(
        "rendered".to_string(),
        json!(format_current_time_context(&now)),
    );
    out.insert("tzname".to_string(), json!(now.timezone_name));

    let mut encoded = serde_json::to_string_pretty(&out).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
