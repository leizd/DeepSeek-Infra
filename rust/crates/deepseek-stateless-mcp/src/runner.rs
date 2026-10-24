use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use crate::model::TaskOutcome;

pub fn execute_configured_task(
    workspace: &Path,
    target: &str,
    keyword: Option<&str>,
    markers: Option<&str>,
    timeout_seconds: u64,
    max_output_bytes: usize,
    cancel: &AtomicBool,
) -> TaskOutcome {
    let Ok(program) = std::env::var("MCP_TASK_PROGRAM") else {
        return TaskOutcome {
            stdout: String::new(),
            stderr: String::new(),
            exit_code: None,
            error: Some("task program is not configured".to_string()),
        };
    };
    if program.trim().is_empty() {
        return TaskOutcome {
            stdout: String::new(),
            stderr: String::new(),
            exit_code: None,
            error: Some("task program is not configured".to_string()),
        };
    }
    let mut command = Command::new(program);
    command
        .arg("-m")
        .arg("pytest")
        .arg(target)
        .arg("--no-cov")
        .arg("-q")
        .arg("-p")
        .arg("no:cacheprovider");
    if let Some(keyword) = keyword {
        command.arg("-k").arg(keyword);
    }
    if let Some(markers) = markers {
        command.arg("-m").arg(markers);
    }
    command
        .current_dir(workspace)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    command.env("PYTHONHASHSEED", "0");
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            return TaskOutcome {
                stdout: String::new(),
                stderr: String::new(),
                exit_code: None,
                error: Some(error.to_string()),
            };
        }
    };
    let started = Instant::now();
    let limit = Duration::from_secs(timeout_seconds.max(1));
    let mut timed_out = false;
    let mut wait_error = None;
    let mut stdout_pipe = child.stdout.take();
    let mut stderr_pipe = child.stderr.take();
    let stdout_handle = std::thread::spawn(move || {
        stdout_pipe
            .as_mut()
            .and_then(|pipe| read_limited(pipe, max_output_bytes))
            .unwrap_or_default()
    });
    let stderr_handle = std::thread::spawn(move || {
        stderr_pipe
            .as_mut()
            .and_then(|pipe| read_limited(pipe, max_output_bytes))
            .unwrap_or_default()
    });
    let status = loop {
        if cancel.load(Ordering::Relaxed) {
            let _ = child.kill();
            break child.wait().ok();
        }
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if started.elapsed() >= limit => {
                timed_out = true;
                let _ = child.kill();
                break child.wait().ok();
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(30)),
            Err(error) => {
                let _ = child.kill();
                wait_error = Some(error.to_string());
                break child.wait().ok();
            }
        }
    };
    let mut stdout = stdout_handle.join().unwrap_or_default();
    let mut stderr = stderr_handle.join().unwrap_or_default();
    if let Some(error) = wait_error {
        return TaskOutcome {
            stdout,
            stderr,
            exit_code: None,
            error: Some(error),
        };
    }
    if stdout.len() > max_output_bytes {
        stdout.truncate(max_output_bytes);
    }
    if stderr.len() > max_output_bytes {
        stderr.truncate(max_output_bytes);
    }
    TaskOutcome {
        stdout,
        stderr,
        exit_code: status.and_then(|status| status.code()),
        error: if timed_out {
            Some(format!("test run exceeded {timeout_seconds} seconds"))
        } else {
            None
        },
    }
}

fn read_limited(pipe: &mut impl std::io::Read, limit: usize) -> Option<String> {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 8192];
    while buf.len() < limit {
        let read = pipe.read(&mut chunk).ok()?;
        if read == 0 {
            break;
        }
        let room = limit - buf.len();
        buf.extend_from_slice(&chunk[..read.min(room)]);
    }
    Some(String::from_utf8_lossy(&buf).into_owned())
}

#[cfg(test)]
mod tests {
    use super::read_limited;
    use std::io::Cursor;

    #[test]
    fn task_output_keeps_draining_after_the_storage_limit() {
        let input = vec![b'x'; 128 * 1024];
        let mut pipe = Cursor::new(input.clone());
        let captured = read_limited(&mut pipe, 17).unwrap();
        assert_eq!(captured, "x".repeat(17));
        assert_eq!(pipe.position(), input.len() as u64);
    }

    #[test]
    fn zero_output_limit_still_drains_the_child_pipe() {
        let input = vec![b'x'; 128 * 1024];
        let mut pipe = Cursor::new(input.clone());
        assert_eq!(read_limited(&mut pipe, 0).unwrap(), "");
        assert_eq!(pipe.position(), input.len() as u64);
    }

    #[test]
    fn task_output_storage_limit_is_also_a_utf8_byte_limit() {
        for input in ["界🤖".as_bytes(), &[0xff, 0xff, 0xff]] {
            for limit in 0..=8 {
                let captured = read_limited(&mut Cursor::new(input), limit).unwrap();
                assert!(captured.len() <= limit, "limit={limit}, value={captured:?}");
                assert!(std::str::from_utf8(captured.as_bytes()).is_ok());
            }
        }
    }

    #[test]
    fn task_output_preserves_empty_and_complete_streams() {
        for input in ["", "complete", "界🤖"] {
            let captured = read_limited(&mut Cursor::new(input.as_bytes()), 64).unwrap();
            assert_eq!(captured, input);
        }
    }

    #[test]
    fn task_output_read_failure_is_not_a_successful_capture() {
        struct FailedPipe;
        impl std::io::Read for FailedPipe {
            fn read(&mut self, _: &mut [u8]) -> std::io::Result<usize> {
                Err(std::io::Error::other("controlled pipe failure"))
            }
        }
        assert!(read_limited(&mut FailedPipe, 64).is_none());
    }
}
