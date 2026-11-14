//! Fuzz the restore path sanitiser.
//!
//! `sanitize_relative_path` is the only thing standing between a manifest written by
//! a compromised or buggy peer and a write outside the restore root. A path-traversal
//! bug here is a remote-write primitive, so the property that matters is not "the
//! cases we thought of are refused" but "no accepted path can escape".
//!
//! Asserted for every input:
//!
//! 1. no panic, including on non-UTF-8 bytes;
//! 2. determinism;
//! 3. every accepted path is *relative* - no root component, no prefix;
//! 4. every accepted path round-trips: joining it to a root and re-relativising that
//!    root yields the same path, so the joiner cannot reinterpret it;
//! 5. an accepted path has no parent-directory component after normalisation.
//!
//! Points 3-5 are the actual security property. A parser can be crash-free and still
//! hand back `../../etc/passwd`.
#![no_main]

use std::path::Component;
use std::path::Path;

use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    // Paths arrive as text, but a lossy conversion is the honest model of what a
    // caller does with arbitrary bytes, and it still exercises the components.
    let candidate = String::from_utf8_lossy(data);

    let first = deepseek_storage::sanitize_relative_path(&candidate);
    let second = deepseek_storage::sanitize_relative_path(&candidate);
    assert_eq!(
        first.is_ok(),
        second.is_ok(),
        "sanitize_relative_path is not deterministic"
    );

    let Ok(path) = first else {
        return;
    };

    // 3: only normal components may appear. RootDir, Prefix and ParentDir are all
    // escapes, and CurDir means the sanitiser did not normalise what it accepted.
    for component in path.components() {
        assert!(
            matches!(component, Component::Normal(_)),
            "accepted a path with a non-normal component {component:?} from {candidate:?}"
        );
    }
    assert!(
        path.is_relative(),
        "accepted an absolute path {candidate:?}"
    );

    // 4: joining to a root and re-relativising must give the same path, so no join
    // can reinterpret the accepted value.
    let root = Path::new("/restore-root");
    let joined = root.join(&path);
    let relative = joined
        .strip_prefix(root)
        .expect("a normal relative path must stay under the root");
    assert_eq!(
        relative, path,
        "accepted path {candidate:?} did not round-trip through a root join"
    );

    // 5: the raw text cannot contain a parent escape even if it normalised away.
    assert!(
        !candidate.contains(".."),
        "accepted {candidate:?} which contains a parent-directory escape"
    );
});
