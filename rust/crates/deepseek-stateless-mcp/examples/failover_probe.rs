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

#[cfg(target_os = "linux")]
fn process_identity() -> serde_json::Value {
    let stat = std::fs::read_to_string("/proc/self/stat").unwrap();
    let (_, fields) = stat.rsplit_once(") ").unwrap();
    let fields: Vec<&str> = fields.split_whitespace().collect();
    serde_json::json!({
        "pid": std::process::id(),
        "parent": fields[1].parse::<u32>().unwrap(),
        "group": fields[2].parse::<u32>().unwrap(),
        "session": fields[3].parse::<u32>().unwrap(),
        "startTicks": fields[19],
    })
}

// The negative lifecycle fixture deliberately leaves its descendant to the owner.
#[allow(clippy::zombie_processes)]
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
    #[cfg(target_os = "linux")]
    if arguments
        .first()
        .is_some_and(|arg| arg == "--lifecycle-descendant")
    {
        assert_eq!(arguments.len(), 3);
        if arguments[2] == "detached" {
            unsafe extern "C" {
                fn setsid() -> i32;
            }
            assert_ne!(unsafe { setsid() }, -1);
        }
        let trace = Path::new(&arguments[1]);
        marker(
            trace.join("descendant.json").to_str().unwrap(),
            &process_identity().to_string(),
        );
        std::thread::sleep(Duration::from_secs(4));
        marker(
            trace.join("late-effect.txt").to_str().unwrap(),
            "native descendant late effect\n",
        );
        std::thread::sleep(Duration::from_secs(2));
        return;
    }
    #[cfg(target_os = "linux")]
    if arguments.len() == expected.len() + 2
        && arguments[expected.len()] == "-k"
        && arguments[expected.len() + 1].starts_with("native-lifecycle-")
    {
        assert_eq!(arguments[..2], expected[..2]);
        assert_eq!(arguments[3..expected.len()], expected[3..]);
        let kind = arguments[expected.len() + 1]
            .strip_prefix("native-lifecycle-")
            .unwrap();
        let trace = std::env::var("MCP_LIFECYCLE_TRACE_ROOT").unwrap();
        let trace = Path::new(&trace);
        marker(
            trace.join("root.json").to_str().unwrap(),
            &process_identity().to_string(),
        );
        let child = std::process::Command::new(std::env::current_exe().unwrap())
            .arg("--lifecycle-descendant")
            .arg(trace)
            .arg(if kind == "timeout-regular" {
                "regular"
            } else {
                "detached"
            })
            .stdin(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        while !trace.join("descendant.json").is_file() {
            assert!(Instant::now() < deadline, "native descendant did not start");
            std::thread::sleep(Duration::from_millis(10));
        }
        println!("native lifecycle root started; child={}", child.id());
        std::io::stdout().flush().unwrap();
        if kind == "parent-exit" {
            println!("native lifecycle root completed");
            return;
        }
        std::thread::sleep(Duration::from_secs(30));
        return;
    }
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
