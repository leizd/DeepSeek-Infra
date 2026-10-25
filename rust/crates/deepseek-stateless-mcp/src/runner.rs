use std::path::Path;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use crate::model::{StoreError, TaskOutcome, TaskRecord};

/// Retry only an explicit refusal that guarantees settlement was not applied.
/// The caller keeps its lease heartbeat alive until this operation returns.
pub async fn complete_when_unfenced<F>(
    mut complete: F,
    cancel: &AtomicBool,
    poll: Duration,
    retry_limit: Duration,
) -> Result<Option<TaskRecord>, StoreError>
where
    F: FnMut() -> Result<Option<TaskRecord>, StoreError>,
{
    let started = Instant::now();
    loop {
        if cancel.load(Ordering::Relaxed) {
            return Ok(None);
        }
        match complete() {
            Err(StoreError::RestoreFenced) if started.elapsed() < retry_limit => {
                tokio::time::sleep(poll).await;
            }
            result => return result,
        }
    }
}

pub fn execute_configured_task(
    workspace: &Path,
    target: &str,
    keyword: Option<&str>,
    markers: Option<&str>,
    timeout_seconds: u64,
    max_output_bytes: usize,
    cancel: &AtomicBool,
) -> TaskOutcome {
    let target = match crate::workspace::resolve_task_target(workspace, target) {
        Ok(target) => target,
        Err(error) => {
            return TaskOutcome {
                stdout: String::new(),
                stderr: String::new(),
                exit_code: None,
                error: Some(error),
            };
        }
    };
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
    #[cfg(any(target_os = "linux", target_os = "android"))]
    {
        let capture =
            crate::task_process::execute(&command, timeout_seconds, max_output_bytes, cancel);
        TaskOutcome {
            stdout: read_limited(&mut std::io::Cursor::new(capture.stdout), max_output_bytes)
                .unwrap_or_default(),
            stderr: read_limited(&mut std::io::Cursor::new(capture.stderr), max_output_bytes)
                .unwrap_or_default(),
            exit_code: capture.exit_code,
            error: capture.error,
        }
    }
    #[cfg(not(any(target_os = "linux", target_os = "android")))]
    {
        execute_direct(command, timeout_seconds, max_output_bytes, cancel)
    }
}

#[cfg(not(any(target_os = "linux", target_os = "android")))]
fn execute_direct(
    mut command: Command,
    timeout_seconds: u64,
    max_output_bytes: usize,
    cancel: &AtomicBool,
) -> TaskOutcome {
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
    let stdout = stdout_handle.join().unwrap_or_default();
    let stderr = stderr_handle.join().unwrap_or_default();
    if let Some(error) = wait_error {
        return TaskOutcome {
            stdout,
            stderr,
            exit_code: None,
            error: Some(error),
        };
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

#[cfg(test)]
mod completion_tests {
    use super::complete_when_unfenced;
    use crate::model::StoreError;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::time::Duration;

    #[tokio::test]
    async fn completion_waits_only_for_explicit_restore_refusals() {
        let cancelled = AtomicBool::new(false);
        let mut calls = 0;
        let result = complete_when_unfenced(
            || {
                calls += 1;
                if calls <= 2 {
                    Err(StoreError::RestoreFenced)
                } else {
                    Ok(None)
                }
            },
            &cancelled,
            Duration::from_millis(1),
            Duration::from_secs(30),
        )
        .await;
        assert!(matches!(result, Ok(None)));
        assert_eq!(calls, 3);
    }

    #[tokio::test]
    async fn unknown_or_other_completion_errors_are_never_retried() {
        for error in [
            StoreError::Message("completion response was lost".to_string()),
            StoreError::BackupFenced,
            StoreError::RestoreConflict,
            StoreError::RestoreState("recovery required".to_string()),
        ] {
            let cancelled = AtomicBool::new(false);
            let mut calls = 0;
            let result = complete_when_unfenced(
                || {
                    calls += 1;
                    Err(error.clone())
                },
                &cancelled,
                Duration::from_millis(1),
                Duration::from_secs(30),
            )
            .await;
            assert_eq!(result, Err(error));
            assert_eq!(calls, 1);
        }
    }

    #[tokio::test]
    async fn cancellation_before_settlement_does_not_attempt_a_write() {
        let cancelled = AtomicBool::new(true);
        let mut calls = 0;
        let result = complete_when_unfenced(
            || {
                calls += 1;
                Ok(None)
            },
            &cancelled,
            Duration::from_millis(1),
            Duration::from_secs(30),
        )
        .await;
        assert!(matches!(result, Ok(None)));
        assert_eq!(calls, 0);
    }

    #[tokio::test]
    async fn lease_loss_while_restore_fenced_stops_settlement() {
        let cancelled = AtomicBool::new(false);
        let mut calls = 0;
        let result = complete_when_unfenced(
            || {
                calls += 1;
                cancelled.store(true, Ordering::Relaxed);
                Err(StoreError::RestoreFenced)
            },
            &cancelled,
            Duration::from_millis(1),
            Duration::from_secs(30),
        )
        .await;
        assert!(matches!(result, Ok(None)));
        assert_eq!(calls, 1);
    }

    #[tokio::test]
    async fn restore_refusal_is_retained_when_the_retry_budget_expires() {
        let cancelled = AtomicBool::new(false);
        let mut calls = 0;
        let result = complete_when_unfenced(
            || {
                calls += 1;
                Err(StoreError::RestoreFenced)
            },
            &cancelled,
            Duration::from_millis(1),
            Duration::ZERO,
        )
        .await;
        assert_eq!(result, Err(StoreError::RestoreFenced));
        assert_eq!(calls, 1);
    }
}

fn read_limited(pipe: &mut impl std::io::Read, limit: usize) -> Option<String> {
    let mut buf = Vec::new();
    let mut chunk = [0u8; 8192];
    loop {
        let read = pipe.read(&mut chunk).ok()?;
        if read == 0 {
            break;
        }
        // Keep draining after the storage limit so a healthy child cannot be
        // failed by a closed stdout/stderr pipe or blocked by a full one.
        let room = limit.saturating_sub(buf.len());
        buf.extend_from_slice(&chunk[..read.min(room)]);
    }
    let mut text = String::from_utf8_lossy(&buf).into_owned();
    // Replacing an incomplete or invalid sequence can expand it. Apply the
    // byte limit to valid UTF-8 as well, without splitting a character.
    let mut end = text.len().min(limit);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    text.truncate(end);
    Some(text)
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

    #[cfg(unix)]
    #[test]
    fn a_native_child_can_finish_after_exceeding_the_output_storage_limit() {
        use std::process::{Command, Stdio};

        // The cloud runner's native coreutils producer writes far beyond pipe
        // capacity. Closing its reader at the storage limit causes SIGPIPE.
        let mut child = Command::new("head")
            .args(["-c", "1048576", "/dev/zero"])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        let captured = read_limited(&mut child.stdout.take().unwrap(), 17).unwrap();
        assert_eq!(captured.as_bytes(), &[0; 17]);
        assert!(child.wait().unwrap().success());
    }
}
