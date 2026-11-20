//! Structured fuzz coverage for the frozen parsers.
//!
//! # Why this exists and why it is not cargo-fuzz
//!
//! The 5.0 `fuzz` gate had no target or harness anywhere in the tree. `cargo-fuzz`
//! with libFuzzer would be the obvious answer, but it needs a nightly toolchain and
//! a separate `cargo fuzz` invocation, which this workspace does not have pinned —
//! `rust/Cargo.toml` pins 1.85 and CI uses that toolchain deliberately.
//!
//! Rather than add a nightly-only job that nothing else depends on, this harness
//! drives the same parsers with a *deterministic structured corpus* inside a normal
//! `cargo test`. It reaches the properties that matter and it runs everywhere the
//! rest of the suite runs.
//!
//! # What it asserts
//!
//! For every generated input, each parser must satisfy all of:
//!
//! 1. **No panic.** Not `catch_unwind` around the call and call it fine — the
//!    process must not abort. A panic aborts the test, which is the signal.
//! 2. **Deterministic.** The same input twice returns the same verdict. A parser
//!    that depends on hash order or the clock would fail here.
//! 3. **Idempotent acceptance.** If a normalizing parser accepts its own output, it
//!    must accept it again — an accept-then-reject loop is a real bug class.
//! 4. **Bounded output.** Accepted text stays within the documented limits, so no
//!    input can produce unbounded growth.
//!
//! The corpus is generated from a fixed seed, so a failure is reproducible: the
//! test prints the exact input that broke an invariant.

use deepseek_policy::backup_mirror::parse_iso;
use deepseek_policy::media_schema::media_type_from_mime;
use deepseek_policy::projects::validate_project_id;
use deepseek_policy::reminders::parse_due_at;

use serde_json::Value;

/// A tiny deterministic PRNG. Chosen over `rand` so the corpus is identical on every
/// platform and every Rust version, and so a failure names an input you can paste
/// straight into a test.
struct Rng(u64);

impl Rng {
    fn new(seed: u64) -> Self {
        Self(seed.max(1))
    }

    fn next(&mut self) -> u64 {
        // xorshift64*
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn below(&mut self, bound: usize) -> usize {
        if bound == 0 {
            return 0;
        }
        (self.next() % bound as u64) as usize
    }
}

/// Byte alphabet aimed at the shapes these parsers branch on: separators, zone
/// markers, digits, control bytes, and multi-byte UTF-8 that must survive.
const ALPHABET: &[&str] = &[
    "0",
    "1",
    "2",
    "9",
    "-",
    "+",
    ":",
    ".",
    "T",
    "Z",
    "z",
    " ",
    "\t",
    "\n",
    "\u{0}",
    "\u{1f}",
    "\u{7f}",
    "é",
    "中",
    "😀",
    "\\",
    "{",
    "}",
    "[",
    "]",
    ",",
    "e",
    "E",
    "NaN",
    "null",
    "true",
    "false",
    "1970-01-01T00:00:00Z",
    "9999-12-31T23:59:59Z",
    // MIME-shaped pieces: without a slash the classification tests would only ever
    // see text that matches no prefix, so the precedence rules would go unexercised.
    "image/",
    "audio/",
    "video/",
    "text/html",
    "application/pdf",
    "application/xhtml+xml",
    ".pdf",
    ".mp3",
    ".mp4",
    ".html",
    "/",
];

fn random_text(rng: &mut Rng, max_parts: usize) -> String {
    let parts = rng.below(max_parts + 1);
    let mut out = String::new();
    for _ in 0..parts {
        out.push_str(ALPHABET[rng.below(ALPHABET.len())]);
    }
    out
}

fn assert_deterministic<T: PartialEq + std::fmt::Debug>(
    label: &str,
    input: &str,
    first: &T,
    second: &T,
) {
    assert_eq!(
        first, second,
        "{label} is not deterministic for input {input:?}"
    );
}

// ---------------------------------------------------------------------------
// parse_iso: ISO-8601 parsing for the mirror metadata
// ---------------------------------------------------------------------------

#[test]
fn fuzz_parse_iso_never_panics_and_is_deterministic() {
    let mut rng = Rng::new(0x5eed_0001);
    for _ in 0..4_000 {
        let input = random_text(&mut rng, 12);
        let first = parse_iso(&input, "fuzzed").is_ok();
        let second = parse_iso(&input, "fuzzed").is_ok();
        assert_deterministic("parse_iso", &input, &first, &second);
        // Acceptance implies the parser understood a non-empty trimmed value; an
        // all-whitespace input must never be accepted.
        if first {
            assert!(
                !input.trim().is_empty(),
                "parse_iso accepted an all-whitespace input {input:?}"
            );
        }
    }
}

#[test]
fn fuzz_parse_iso_accepts_its_own_canonical_form() {
    // Every accepted input must round-trip through the canonical RFC3339 rendering.
    // If a value parses and its re-rendering does not, the parser accepts a shape it
    // cannot reproduce - the accept-then-reject loop this harness exists to catch.
    // Start from inputs that are known to parse. Generating only random text would
    // make the round-trip assertion depend on chance - a corpus that happens to
    // produce no valid timestamp would pass this test vacuously.
    let mut seeds: Vec<String> = [
        "1970-01-01T00:00:00Z",
        "2026-11-12T20:40:30Z",
        "9999-12-31T23:59:59Z",
        "2024-02-29T12:00:00Z",
    ]
    .iter()
    .map(|value| (*value).to_string())
    .collect();
    let mut rng = Rng::new(0x5eed_0002);
    for _ in 0..2_000 {
        seeds.push(random_text(&mut rng, 10));
    }

    let mut accepted = 0;
    for input in &seeds {
        let Ok(parsed) = parse_iso(input, "fuzzed") else {
            continue;
        };
        accepted += 1;
        let canonical = parsed.to_rfc3339_opts(chrono::SecondsFormat::Secs, true);
        let reparsed = parse_iso(&canonical, "fuzzed");
        assert!(
            reparsed.is_ok(),
            "parse_iso accepted {input:?} but rejected its own canonical form {canonical:?}"
        );
    }
    // The seeded values guarantee this is not vacuous.
    assert!(
        accepted >= 4,
        "expected at least the four seeded timestamps to parse, accepted {accepted}"
    );
}

// ---------------------------------------------------------------------------
// validate_project_id: identifier acceptance with documented bounds
// ---------------------------------------------------------------------------

#[test]
fn fuzz_validate_project_id_respects_its_documented_bounds() {
    let mut rng = Rng::new(0x5eed_0003);
    for _ in 0..4_000 {
        let input = random_text(&mut rng, 14);
        let decided = validate_project_id(&input);
        let repeated = validate_project_id(&input);
        // Comparing the verdict alone is enough: determinism is about one input
        // yielding one answer, and the accepted value is asserted separately below.
        assert_deterministic(
            "validate_project_id",
            &input,
            &decided.is_ok(),
            &repeated.is_ok(),
        );
        if let (Err(first_error), Err(second_error)) = (&decided, &repeated) {
            assert_eq!(
                first_error.message, second_error.message,
                "validate_project_id reason drifted for input {input:?}"
            );
        }
        if let Ok(safe) = decided {
            // An accepted id is trimmed, within 4..=64 characters, and restricted to
            // the documented alphabet. Any accepted value violating these would mean
            // the guard does not actually guard.
            let trimmed = input.trim();
            assert_eq!(safe, trimmed, "accepted id was not the trimmed input");
            assert!(
                (4..=64).contains(&safe.chars().count()),
                "accepted id {safe:?} is outside the 4..=64 bound"
            );
            assert!(
                safe.chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-')),
                "accepted id {safe:?} contains a character outside the alphabet"
            );
        }
    }
}

// ---------------------------------------------------------------------------
// parse_due_at: reminder scheduling from an arbitrary JSON value
// ---------------------------------------------------------------------------

fn random_json(rng: &mut Rng, depth: usize) -> Value {
    match rng.below(8) {
        0 => Value::Null,
        1 => Value::Bool(rng.below(2) == 0),
        2 => Value::from(rng.below(1_000_000) as i64),
        3 => Value::from(random_text(rng, 8)),
        4 => Value::String(String::new()),
        5 => Value::Array(vec![random_text(rng, 3).into()]),
        6 if depth > 0 => {
            let mut map = serde_json::Map::new();
            map.insert("dueAt".to_string(), random_json(rng, depth - 1));
            Value::Object(map)
        }
        _ => Value::String(random_text(rng, 10)),
    }
}

#[test]
fn fuzz_parse_due_at_never_panics_over_arbitrary_json() {
    let mut rng = Rng::new(0x5eed_0004);
    for _ in 0..3_000 {
        let value = random_json(&mut rng, 2);
        let first = parse_due_at(Some(&value)).is_ok();
        let second = parse_due_at(Some(&value)).is_ok();
        assert_deterministic("parse_due_at", &value.to_string(), &first, &second);
        // A missing value is always refused; that is the documented contract and the
        // generator must not be able to talk the parser out of it.
        assert!(
            parse_due_at(None).is_err(),
            "parse_due_at accepted a missing value"
        );
        if first {
            // An accepted timestamp is non-empty after trimming.
            let rendered = value.as_str().unwrap_or_default();
            assert!(
                !rendered.trim().is_empty() || !value.is_string(),
                "parse_due_at accepted an empty string {value:?}"
            );
        }
    }
}

// ---------------------------------------------------------------------------
// media_type_from_mime: total function over arbitrary input
// ---------------------------------------------------------------------------

#[test]
fn fuzz_media_type_from_mime_is_total_and_deterministic() {
    let mut rng = Rng::new(0x5eed_0005);
    // The parser returns one of the six documented classes, or the empty string when
    // nothing matches. Anything outside that set would mean it invented a class the
    // rest of the system does not know.
    let allowed: std::collections::BTreeSet<&str> = deepseek_policy::media_schema::MEDIA_TYPES
        .iter()
        .copied()
        .chain(std::iter::once(""))
        .collect();
    for _ in 0..3_000 {
        let content_type = random_text(&mut rng, 6);
        let filename = random_text(&mut rng, 6);
        let first = media_type_from_mime(&content_type, &filename);
        let second = media_type_from_mime(&content_type, &filename);
        assert_deterministic("media_type_from_mime", &content_type, &first, &second);
        // A `image/*` content type is classified as an image regardless of filename;
        // that precedence is a documented rule the generator must not be able to
        // violate by supplying a misleading suffix.
        if content_type.starts_with("image/") {
            assert_eq!(
                first, "image",
                "image/* precedence lost for {content_type:?}"
            );
        }
        assert!(
            allowed.contains(first.as_str()),
            "media_type_from_mime returned an unknown class {first:?} for {content_type:?}/{filename:?}"
        );
    }
}
