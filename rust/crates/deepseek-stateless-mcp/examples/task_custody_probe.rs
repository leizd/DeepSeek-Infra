//! Offline Windows process-custody fixture; never shipped in the runtime image.
#[cfg(windows)]
mod windows {
    use std::io::Write;
    use std::path::Path;
    use std::process::{Command, Stdio};
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::time::{Duration, Instant};

    fn publish(path: &Path, value: &[u8]) {
        let mut file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)
            .unwrap();
        file.write_all(value).unwrap();
        file.sync_all().unwrap();
    }

    fn identity(path: &Path) {
        publish(
            path,
            serde_json::json!({"pid": std::process::id()})
                .to_string()
                .as_bytes(),
        );
    }

    // The negative fixture intentionally leaves a real descendant to the owner.
    #[allow(clippy::zombie_processes)]
    pub fn main() {
        let args: Vec<String> = std::env::args().skip(1).collect();
        if args.first().is_some_and(|value| value == "--driver") {
            assert_eq!(args.len(), 4);
            let workspace = Path::new(&args[1]);
            let kind = &args[2];
            let cancelled = Arc::new(AtomicBool::new(false));
            if kind == "cancel" {
                let cancel = Arc::clone(&cancelled);
                let marker =
                    std::path::PathBuf::from(std::env::var("MCP_WINDOWS_TRACE_ROOT").unwrap())
                        .join("cancel-request");
                std::thread::spawn(move || {
                    let deadline = Instant::now() + Duration::from_secs(20);
                    while !marker.is_file() {
                        assert!(Instant::now() < deadline, "cancellation was not released");
                        std::thread::sleep(Duration::from_millis(10));
                    }
                    cancel.store(true, Ordering::Relaxed);
                });
            }
            let keyword = format!("native-windows-{kind}");
            let outcome = deepseek_stateless_mcp::runner::execute_configured_task(
                workspace,
                "input.txt",
                Some(&keyword),
                None,
                if kind.starts_with("timeout") { 2 } else { 20 },
                256 * 1024,
                &cancelled,
            );
            let value = serde_json::json!({
                "stdout": outcome.stdout, "stderr": outcome.stderr,
                "exitCode": outcome.exit_code, "error": outcome.error,
                "cancelRequested": cancelled.load(Ordering::Relaxed),
            });
            publish(Path::new(&args[3]), value.to_string().as_bytes());
            return;
        }
        if args.first().is_some_and(|value| value == "--descendant") {
            assert_eq!(args.len(), 2);
            let trace = Path::new(&args[1]);
            identity(&trace.join("descendant.json"));
            std::thread::sleep(Duration::from_secs(4));
            publish(
                &trace.join("late-effect.txt"),
                b"native Windows descendant late effect\n",
            );
            std::thread::sleep(Duration::from_secs(2));
            return;
        }
        assert_eq!(args.len(), 9);
        assert_eq!(args[..2], ["-m", "pytest"]);
        assert_eq!(
            args[3..8],
            ["--no-cov", "-q", "-p", "no:cacheprovider", "-k"]
        );
        let kind = args[8].strip_prefix("native-windows-").unwrap();
        if kind == "short-control" {
            println!("native Windows short control completed");
            return;
        }
        let trace = std::env::var("MCP_WINDOWS_TRACE_ROOT").unwrap();
        let trace = Path::new(&trace);
        identity(&trace.join("root.json"));
        let mut command = Command::new(std::env::current_exe().unwrap());
        command.arg("--descendant").arg(trace).stdin(Stdio::null());
        if kind != "timeout-regular" {
            use std::os::windows::process::CommandExt;
            // Microsoft CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS. Neither
            // provides an escape from a correctly configured kernel Job Object.
            command.creation_flags(0x0000_0200 | 0x0000_0008);
        }
        let child = command.spawn().unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        while !trace.join("descendant.json").is_file() {
            assert!(
                Instant::now() < deadline,
                "Windows descendant did not publish its PID"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        println!("native Windows root started; child={}", child.id());
        std::io::stdout().flush().unwrap();
        if kind == "parent-exit" {
            let deadline = Instant::now() + Duration::from_secs(10);
            while !trace.join("root-release").is_file() {
                assert!(
                    Instant::now() < deadline,
                    "Windows root exit was not released"
                );
                std::thread::sleep(Duration::from_millis(10));
            }
            println!("native Windows root completed");
            return;
        }
        std::thread::sleep(Duration::from_secs(30));
    }
}

#[cfg(windows)]
fn main() {
    windows::main();
}

#[cfg(not(windows))]
fn main() {
    eprintln!("offline Windows custody fixture requires Windows");
    std::process::exit(2);
}
