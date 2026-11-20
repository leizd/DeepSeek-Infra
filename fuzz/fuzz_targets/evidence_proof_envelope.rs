//! Fuzz the evidence-proof envelope reader.
//!
//! `parse_evidence_proof_document` reads a document produced by another fleet and
//! decides whether a release gate may pass. The dangerous failure is not a crash but
//! an *acceptance*: a malformed or ambiguous document that parses successfully and
//! lets an unproven claim through.
//!
//! Asserted for every input:
//!
//! 1. no panic, including on invalid UTF-8 and deeply nested JSON;
//! 2. determinism - the same bytes give the same verdict;
//! 3. acceptance is stable: an accepted document is accepted again, so a retry cannot
//!    flip a gate;
//! 4. an accepted document always has the schema and a checks object, because those
//!    are the two things the parser claims to enforce before returning Ok.
//!
//! Point 4 is the one that would catch a future refactor that returns early before
//! the checks it is documented to perform.
#![no_main]

use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    let first = deepseek_proof::parse_evidence_proof_document(data, None);
    let second = deepseek_proof::parse_evidence_proof_document(data, None);
    assert_eq!(
        first.is_ok(),
        second.is_ok(),
        "parse_evidence_proof_document is not deterministic"
    );

    let Ok(envelope) = first else {
        return;
    };

    // 3: a second read must agree.
    assert!(
        deepseek_proof::parse_evidence_proof_document(data, None).is_ok(),
        "an accepted envelope was rejected when re-read"
    );

    // 4: the parser's documented preconditions must actually hold on what it returns.
    let value: serde_json::Value =
        serde_json::from_slice(data).expect("an accepted envelope must be valid JSON");
    let object = value
        .as_object()
        .expect("an accepted envelope must be a JSON object");
    assert!(
        object.get("schema").is_some_and(|schema| schema.is_string()),
        "accepted an envelope with no string schema"
    );
    assert!(
        object.get("checks").is_some_and(serde_json::Value::is_object),
        "accepted an envelope with no checks object"
    );

    // An expected-scenario read of the same bytes must also be deterministic, and
    // must not accept a document whose scenario differs from the one demanded.
    if let Some(scenario) = object.get("scenario").and_then(|value| value.as_str()) {
        assert!(
            deepseek_proof::parse_evidence_proof_document(data, Some(scenario)).is_ok(),
            "an envelope rejected its own scenario"
        );
        let mismatched = deepseek_proof::parse_evidence_proof_document(
            data,
            Some(if scenario == "x" { "y" } else { "x" }),
        );
        assert!(
            mismatched.is_err(),
            "an envelope accepted a scenario it does not carry"
        );
    }

    // Keep the parsed value live so the assertion above cannot be optimised into
    // "parse but never look".
    let _ = envelope;
});
