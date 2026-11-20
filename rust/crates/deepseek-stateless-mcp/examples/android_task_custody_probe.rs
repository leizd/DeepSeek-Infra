//! Instrumentation-APK-only Android custody fixture. Never a product test engine.
#[cfg(target_os = "android")]
mod android {
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
        let stat = std::fs::read_to_string("/proc/self/stat").unwrap();
        let fields: Vec<_> = stat
            .rsplit_once(") ")
            .unwrap()
            .1
            .split_whitespace()
            .collect();
        // SAFETY: getuid has no pointer arguments or process mutation.
        let uid = unsafe { libc::getuid() };
        publish(
            path,
            serde_json::json!({"pid": std::process::id(), "uid": uid,
            "startTicks": fields[19], "processGroup": fields[2], "session": fields[3]})
            .to_string()
            .as_bytes(),
        );
    }

    fn await_file(path: &Path, seconds: u64) {
        let deadline = Instant::now() + Duration::from_secs(seconds);
        while !path.is_file() {
            assert!(
                Instant::now() < deadline,
                "fixture release was not observed"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    // An owned negative fixture intentionally leaves descendants for the runner.
    #[allow(clippy::zombie_processes)]
    pub fn main() {
        let args: Vec<String> = std::env::args().skip(1).collect();
        let trace = std::env::var("MCP_ANDROID_TRACE_ROOT").unwrap();
        let trace = Path::new(&trace);
        if args.first().is_some_and(|value| value == "--driver") {
            assert_eq!(args.len(), 3);
            identity(&trace.join("driver.json"));
            let kind = &args[2];
            let cancelled = Arc::new(AtomicBool::new(false));
            if kind == "cancel" {
                let cancel = Arc::clone(&cancelled);
                let marker = trace.join("cancel-request");
                std::thread::spawn(move || {
                    await_file(&marker, 20);
                    cancel.store(true, Ordering::Relaxed);
                });
            }
            let keyword = format!("native-android-{kind}");
            let outcome = deepseek_stateless_mcp::runner::execute_configured_task(
                Path::new(&args[1]),
                "input.txt",
                Some(&keyword),
                None,
                if kind.starts_with("timeout") { 2 } else { 20 },
                256 * 1024,
                &cancelled,
            );
            publish(
                &trace.join("outcome.json"),
                serde_json::json!({
                    "stdout": outcome.stdout, "stderr": outcome.stderr,
                    "exitCode": outcome.exit_code, "error": outcome.error,
                    "cancelRequested": cancelled.load(Ordering::Relaxed)
                })
                .to_string()
                .as_bytes(),
            );
            return;
        }
        if args.first().is_some_and(|value| value == "--descendant") {
            assert_eq!(args.len(), 1);
            identity(&trace.join("descendant.json"));
            std::thread::sleep(Duration::from_secs(4));
            publish(
                &trace.join("late-effect.txt"),
                b"native Android descendant late effect\n",
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
        let kind = args[8].strip_prefix("native-android-").unwrap();
        if kind == "short-control" {
            println!("native Android short control completed");
            return;
        }
        identity(&trace.join("root.json"));
        let mut command = Command::new(std::env::current_exe().unwrap());
        command.arg("--descendant").stdin(Stdio::null());
        if kind != "timeout-regular" {
            use std::os::unix::process::CommandExt;
            // SAFETY: the pre-exec closure only invokes async-signal-safe setsid.
            unsafe {
                command.pre_exec(|| {
                    if libc::setsid() < 0 {
                        Err(std::io::Error::last_os_error())
                    } else {
                        Ok(())
                    }
                });
            }
        }
        let child = command.spawn().unwrap();
        await_file(&trace.join("descendant.json"), 5);
        println!("native Android root started; child={}", child.id());
        std::io::stdout().flush().unwrap();
        publish(
            &trace.join("root-ready"),
            b"root stdout flushed before observer release\n",
        );
        if kind == "parent-exit" {
            await_file(&trace.join("root-release"), 10);
            println!("native Android root completed");
            return;
        }
        std::thread::sleep(Duration::from_secs(30));
    }
}

#[cfg(target_os = "android")]
fn main() {
    if let Some(code) = deepseek_stateless_mcp::task_process::maybe_run_supervisor() {
        std::process::exit(code);
    }
    android::main();
}

#[cfg(not(target_os = "android"))]
fn main() {
    eprintln!("instrumentation-only Android custody fixture requires Android");
    std::process::exit(2);
}
