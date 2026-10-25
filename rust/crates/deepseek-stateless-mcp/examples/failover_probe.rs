//! Offline fixture for a real native child process and Redis lease recovery.
use sha2::{Digest, Sha256};
use std::io::Write;
use std::path::Path;
use std::time::{Duration, Instant};

const STARTED: &str = "/tmp/deepseek-mcp-completion-started";
const RELEASED: &str = "/tmp/deepseek-mcp-completion-released";
const FINISHED: &str = "/tmp/deepseek-mcp-completion-finished";

fn marker(path: &str, value: &str) {
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .unwrap();
    file.write_all(value.as_bytes()).unwrap();
    file.sync_all().unwrap();
}

fn main() {
    // Exercise the existing test-executor argument contract without an
    // interpreter. This fixture is included only in the qualification image.
    let target = std::env::current_dir()
        .unwrap()
        .join("rust/crates/deepseek-stateless-mcp/examples/failover_probe.rs");
    let expected = [
        "-m",
        "pytest",
        target.to_str().unwrap(),
        "--no-cov",
        "-q",
        "-p",
        "no:cacheprovider",
    ];
    let arguments: Vec<String> = std::env::args().skip(1).collect();
    if arguments == ["--completion-status"] {
        let pid = std::fs::read_to_string(STARTED)
            .ok()
            .map(|value| value.trim().parse::<u32>().unwrap());
        println!(
            "{}",
            serde_json::json!({
                "started": pid.is_some(),
                "finished": Path::new(FINISHED).is_file(),
                "childAlive": pid.is_some_and(|pid| Path::new(&format!("/proc/{pid}")).exists()),
            })
        );
        return;
    }
    if arguments == ["--completion-release"] {
        assert!(Path::new(STARTED).is_file());
        marker(RELEASED, "release\n");
        return;
    }
    if arguments.len() == expected.len() + 2
        && arguments[expected.len()..] == ["-k", "native-workspace-boundary"]
    {
        assert_eq!(arguments[..2], expected[..2]);
        assert_eq!(arguments[3..expected.len()], expected[3..]);
        let target = &arguments[2];
        let (file_target, selector) = target
            .split_once("::")
            .map(|(path, selector)| (path, Some(selector)))
            .unwrap_or((target, None));
        let invocation = serde_json::json!({
            "pid": std::process::id(),
            "cwd": std::env::current_dir().unwrap(),
            "target": target,
        });
        let mut journal = std::fs::OpenOptions::new()
            .append(true)
            .open(std::env::var("MCP_BOUNDARY_INVOCATIONS").unwrap())
            .unwrap();
        writeln!(journal, "{invocation}").unwrap();
        journal.sync_all().unwrap();
        let contents = std::fs::read_to_string(file_target).unwrap();
        println!(
            "{}",
            serde_json::json!({"target": target, "selector": selector, "contents": contents})
        );
        return;
    }
    let completion_probe = arguments.len() == expected.len() + 2;
    if completion_probe {
        assert_eq!(arguments[..expected.len()], expected);
        assert_eq!(
            arguments[expected.len()..],
            ["-k", "native-completion-fence"]
        );
        marker(STARTED, &std::process::id().to_string());
        let deadline = Instant::now() + Duration::from_secs(30);
        while !Path::new(RELEASED).is_file() {
            assert!(
                Instant::now() < deadline,
                "completion latch was not released"
            );
            std::thread::sleep(Duration::from_millis(25));
        }
        println!("native completion fence probe completed");
        std::io::stdout().flush().unwrap();
        marker(FINISHED, "completed\n");
        return;
    }
    assert_eq!(arguments, expected);
    std::thread::sleep(Duration::from_secs(12));
    let digest = format!("{:x}", Sha256::digest(b"native task lease recovery probe"));
    assert_eq!(
        digest,
        "a2e09bf88bd1d291e9208a8a81180d48205ef223c62bfaaecb279861ab45056a"
    );
    println!("native failover probe completed; sha256={digest}");
}
