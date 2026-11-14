//! Fuzz the frozen wire validators with arbitrary bytes.
//!
//! Receipt v4 and Commit v4 are frozen contracts (ADR-0048): a document written by
//! any past version must still validate, and a malformed one must be refused rather
//! than admitted. Both properties are security-relevant, and both are exactly what a
//! coverage-guided fuzzer finds regressions in.
//!
//! The invariants asserted here are the ones a unit test cannot cover, because they
//! must hold for *every* byte string, not for the samples someone thought to write:
//!
//! 1. no panic, on any input, including truncated and non-UTF-8 bytes;
//! 2. determinism - the same bytes give the same verdict, so a crash is replayable;
//! 3. a document that validates once still validates when re-read (validation is a
//!    pure function of the bytes, not of hidden state);
//! 4. swapping the two documents must not make an invalid pair valid, which would
//!    mean the check is not actually binding receipt to commit.
#![no_main]

use libfuzzer_sys::fuzz_target;

/// Split fuzz input into two documents on a separator byte, so one input can drive
/// cross-document checks. Falls back to feeding both halves the whole input when the
/// separator is absent, which keeps short inputs useful.
fn split(input: &[u8]) -> (&[u8], &[u8]) {
    match input.iter().position(|byte| *byte == 0) {
        Some(index) => (&input[..index], &input[index + 1..]),
        None => (input, input),
    }
}

fuzz_target!(|data: &[u8]| {
    let (receipt_bytes, commit_bytes) = split(data);

    // 1 + 2: same bytes, same verdict, no panic.
    let first = deepseek_storage::validate_committed_documents(receipt_bytes, commit_bytes);
    let second = deepseek_storage::validate_committed_documents(receipt_bytes, commit_bytes);
    assert_eq!(
        first.is_ok(),
        second.is_ok(),
        "validate_committed_documents is not deterministic for the same input"
    );

    // 3: an accepted pair is still accepted on a fresh call. Anything else means the
    // validator keeps state across calls, which would make it unusable in a retry.
    if first.is_ok() {
        assert!(
            deepseek_storage::validate_committed_documents(receipt_bytes, commit_bytes).is_ok(),
            "an accepted document pair was rejected when re-validated"
        );
    }

    // 4: the pairing has to matter. If swapping receipt and commit still validates,
    // the check is not binding one to the other.
    if first.is_ok() && receipt_bytes != commit_bytes {
        let swapped = deepseek_storage::validate_committed_documents(commit_bytes, receipt_bytes);
        assert!(
            swapped.is_err(),
            "swapping receipt and commit still validated, so the pairing is not checked"
        );
    }

    // The single-document digest path must also accept any byte string without
    // panicking; it is reachable from a header value.
    let _ = deepseek_storage::slot_digest(&String::from_utf8_lossy(receipt_bytes));
});
