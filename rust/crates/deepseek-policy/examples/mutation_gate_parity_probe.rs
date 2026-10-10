//! Mutation-gate parity probe, Rust side.
//!
//! Replays the same scripted sequence as
//! `tasks/native-runtime/mutation_gate_parity_probe.py` through
//! `deepseek_policy::mutation_gate` and prints canonical JSON, so the two outputs
//! can be diffed byte-for-byte.
//!
//! Everything path-dependent is reported as a basename, because the two sides use
//! different scratch roots.
//!
//! Usage::
//!
//!     python tasks/native-runtime/mutation_gate_parity_probe.py > python.json
//!     cd rust && cargo run -p deepseek-policy --example mutation_gate_parity_probe > ../rust.json
//!     diff <(tr -d '\r' < python.json) <(tr -d '\r' < rust.json)

use std::fs;
use std::path::{Path, PathBuf};

use deepseek_policy::mutation_gate::{
    GateError, assert_mutation_allowed, bump_generation, clear_fence, exclusive_gate, fence_path,
    generation_path, lock_path, mutation_scope, read_fence, read_generation, write_fence,
};
use serde_json::{Map, Value, json};

/// A scratch root that cleans itself up.
struct Scratch {
    root: PathBuf,
}

impl Scratch {
    fn new(label: &str) -> Self {
        let root = std::env::temp_dir().join(format!("gate-parity-{label}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("create scratch root");
        Self { root }
    }

    fn path(&self) -> &Path {
        &self.root
    }

    /// Temp files left behind by a durable replace.
    fn leftovers(&self) -> Value {
        let mut names: Vec<String> = fs::read_dir(&self.root)
            .expect("read scratch root")
            .filter_map(Result::ok)
            .map(|entry| entry.file_name().to_string_lossy().to_string())
            .filter(|name| name.ends_with(".tmp"))
            .collect();
        names.sort();
        Value::Array(names.into_iter().map(Value::String).collect())
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}

/// Mirrors the Python probe's `outcome`: the error shape, or the success value.
///
/// `code`/`status` are `Option` on `GateError` because the oracle's `RuntimeError`
/// and `OSError` shapes carry neither — they serialise as `null`, which is exactly
/// what the Python probe's `getattr(exc, "code", None)` reports.
fn outcome<T: serde::Serialize>(result: Result<T, GateError>) -> Value {
    match result {
        Ok(value) => json!({"raised": Value::Null, "result": value}),
        Err(error) => json!({
            "raised": error.kind.name(),
            "message": error.message,
            "code": error.code,
            "status": error.status,
        }),
    }
}

/// `Ok(_)` renders as `null` in Python, so normalise it whatever the Ok type is.
fn outcome_unit<T>(result: Result<T, GateError>) -> Value {
    match result {
        Ok(_) => json!({"raised": Value::Null, "result": Value::Null}),
        Err(error) => outcome::<()>(Err(error)),
    }
}

fn main() {
    let scratch = Scratch::new("rust");
    let root = scratch.path();
    let mut out = Map::new();

    let generation = generation_path(root);
    let fence = fence_path(root);
    let lock = lock_path(root);

    let basename = |path: &Path| {
        Value::String(
            path.file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string(),
        )
    };
    // `json!` reads `[...]` as an array literal, so the predicate is evaluated
    // outside the macro.
    let all_under_root = [&lock, &fence, &generation]
        .iter()
        .all(|path| path.parent() == Some(root));
    out.insert(
        "paths".to_string(),
        json!({
            "lock": basename(&lock),
            "fence": basename(&fence),
            "generation": basename(&generation),
            "all_under_root": all_under_root,
        }),
    );

    // --- generation -----------------------------------------------------------
    out.insert(
        "generation::initial".to_string(),
        json!(read_generation(root)),
    );
    out.insert(
        "bump::first".to_string(),
        json!(bump_generation(root).unwrap()),
    );
    out.insert(
        "generation::file".to_string(),
        json!(fs::read_to_string(&generation).unwrap()),
    );
    out.insert(
        "bump::second".to_string(),
        json!(bump_generation(root).unwrap()),
    );
    out.insert(
        "generation::file2".to_string(),
        json!(fs::read_to_string(&generation).unwrap()),
    );

    fs::write(&generation, "not a number").unwrap();
    out.insert(
        "generation::unparseable".to_string(),
        json!(read_generation(root)),
    );
    fs::write(&generation, "-5").unwrap();
    out.insert(
        "generation::negative".to_string(),
        json!(read_generation(root)),
    );
    fs::write(&generation, "7").unwrap();
    out.insert(
        "generation::valid".to_string(),
        json!(read_generation(root)),
    );
    let _ = fs::remove_file(&generation);

    out.insert("temps::after-bumps".to_string(), scratch.leftovers());

    // --- the fence ------------------------------------------------------------
    out.insert(
        "fence::initial".to_string(),
        json!(read_fence(root).unwrap()),
    );
    out.insert(
        "assert::initial".to_string(),
        Value::String(if assert_mutation_allowed(None, root).is_ok() {
            "ok".to_string()
        } else {
            "raised".to_string()
        }),
    );

    let value = json!({"restoreId": "r1", "startedAt": "2026-09-15T00:00:00Z", "n": 1});
    write_fence(&value, root).unwrap();
    out.insert(
        "fence::file".to_string(),
        json!(fs::read_to_string(&fence).unwrap()),
    );
    out.insert("fence::read".to_string(), json!(read_fence(root).unwrap()));
    out.insert("temps::after-fence".to_string(), scratch.leftovers());

    out.insert(
        "assert::foreign".to_string(),
        outcome_unit(assert_mutation_allowed(None, root)),
    );
    out.insert(
        "assert::other-owner".to_string(),
        outcome_unit(assert_mutation_allowed(Some("other"), root)),
    );
    out.insert(
        "assert::owner".to_string(),
        outcome_unit(assert_mutation_allowed(Some("r1"), root)),
    );

    // A refused scope must not bump anything.
    let before = read_generation(root);
    out.insert(
        "scope::foreign".to_string(),
        outcome_unit(mutation_scope(None, root).map(|_scope| ())),
    );
    out.insert(
        "scope::foreign-bumped".to_string(),
        json!(read_generation(root) - before),
    );

    out.insert(
        "clear::foreign".to_string(),
        outcome(clear_fence("other", root)),
    );
    out.insert("clear::owner".to_string(), outcome(clear_fence("r1", root)));
    out.insert(
        "fence::after-clear".to_string(),
        json!(read_fence(root).unwrap()),
    );
    out.insert(
        "clear::absent".to_string(),
        outcome(clear_fence("r1", root)),
    );

    // --- the scope ------------------------------------------------------------
    let before = read_generation(root);
    {
        let _scope = mutation_scope(None, root).unwrap();
        out.insert(
            "scope::inside-bump".to_string(),
            json!(read_generation(root) - before),
        );
    }
    out.insert(
        "scope::after-bump".to_string(),
        json!(read_generation(root) - before),
    );

    // Nesting the same root is allowed.
    let before = read_generation(root);
    {
        let _outer = exclusive_gate(root).unwrap();
        let _inner = exclusive_gate(root).unwrap();
    }
    out.insert(
        "gate::nested-same-root".to_string(),
        Value::String("ok".to_string()),
    );
    out.insert(
        "gate::nested-generation".to_string(),
        json!(read_generation(root) - before),
    );

    // A different root inside an open gate is a programming error.
    let other = Scratch::new("rust-other");
    {
        let _outer = exclusive_gate(root).unwrap();
        out.insert(
            "gate::nested-other-root".to_string(),
            outcome_unit(exclusive_gate(other.path())),
        );
    }

    // --- malformed fences -----------------------------------------------------
    fs::write(&fence, "{not json").unwrap();
    out.insert("fence::unreadable".to_string(), outcome(read_fence(root)));
    fs::write(&fence, "42").unwrap();
    out.insert(
        "fence::scalar".to_string(),
        json!(read_fence(root).unwrap()),
    );
    let _ = fs::remove_file(&fence);

    out.insert(
        "lock::file".to_string(),
        json!(fs::read_to_string(&lock).unwrap()),
    );

    let mut encoded =
        serde_json::to_string_pretty(&Value::Object(out)).expect("serialize probe output");
    encoded.push('\n');
    print!("{encoded}");
}
