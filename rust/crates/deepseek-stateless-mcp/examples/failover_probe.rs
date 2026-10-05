//! Offline fixture for a real native child process and Redis lease recovery.
use sha2::{Digest, Sha256};
use std::time::Duration;

fn main() {
    // Exercise the existing test-executor argument contract without an
    // interpreter. This fixture is included only in the qualification image.
    let expected = [
        "-m",
        "pytest",
        "rust/crates/deepseek-stateless-mcp/examples/failover_probe.rs",
        "--no-cov",
        "-q",
        "-p",
        "no:cacheprovider",
    ];
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    assert_eq!(arguments, expected);
    std::thread::sleep(Duration::from_secs(12));
    let digest = format!("{:x}", Sha256::digest(b"native task lease recovery probe"));
    assert_eq!(
        digest,
        "a2e09bf88bd1d291e9208a8a81180d48205ef223c62bfaaecb279861ab45056a"
    );
    println!("native failover probe completed; sha256={digest}");
}
