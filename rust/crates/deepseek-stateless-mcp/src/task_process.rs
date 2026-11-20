//! Task-local native process custody. The supervisor starts before Tokio and is
//! the sole reaper of its descendants; the HTTP service never reaps other tasks.
use std::io::{self, Read, Write};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

mod linux;

pub const SUPERVISOR_ARGUMENT: &str = "--native-task-supervisor-v1";
const MAGIC: &[u8; 5] = b"DMCP1";
const FRAME_LIMIT: usize = 8192;
const STDOUT: u8 = 1;
const STDERR: u8 = 2;
const ERROR: u8 = 3;
const EXIT: u8 = 4;

#[derive(Default, Debug)]
pub(crate) struct Capture {
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub exit_code: Option<i32>,
    pub error: Option<String>,
}

/// Called synchronously by the binary, before creating any runtime threads.
pub fn maybe_run_supervisor() -> Option<i32> {
    let mut args = std::env::args_os().skip(1);
    if args.next().as_deref() != Some(std::ffi::OsStr::new(SUPERVISOR_ARGUMENT)) {
        return None;
    }
    Some(linux::main(args.collect()))
}

pub(crate) fn execute(
    command: &Command,
    timeout_seconds: u64,
    max_output_bytes: usize,
    cancel: &AtomicBool,
) -> Capture {
    let result = execute_inner(command, timeout_seconds, max_output_bytes, cancel);
    result.unwrap_or_else(|error| Capture {
        error: Some(error.to_string()),
        ..Capture::default()
    })
}

fn execute_inner(
    command: &Command,
    timeout_seconds: u64,
    max_output_bytes: usize,
    cancel: &AtomicBool,
) -> io::Result<Capture> {
    if cancel.load(Ordering::Relaxed) {
        return Ok(Capture::default());
    }
    let mut supervisor = Command::new(std::env::current_exe()?);
    supervisor
        .arg(SUPERVISOR_ARGUMENT)
        .arg(command.get_program())
        .args(command.get_args())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    if let Some(root) = command.get_current_dir() {
        supervisor.current_dir(root);
    }
    for (name, value) in command.get_envs() {
        if let Some(value) = value {
            supervisor.env(name, value);
        } else {
            supervisor.env_remove(name);
        }
    }
    let mut child = supervisor.spawn()?;
    // Only the service owns the writer. Its actual death closes this pipe even
    // when a task has detached from the service's process group/session.
    let mut lifetime = child.stdin.take();
    let output = child
        .stdout
        .take()
        .ok_or_else(|| io::Error::other("task supervisor output missing"))?;
    let reader = match std::thread::Builder::new()
        .name("native-task-capture".into())
        .spawn(move || read_capture(output, max_output_bytes))
    {
        Ok(reader) => reader,
        Err(error) => {
            drop(lifetime.take());
            let _ = child.wait();
            return Err(error);
        }
    };
    let started = Instant::now();
    let limit = Duration::from_secs(timeout_seconds.max(1));
    let mut timed_out = false;
    let mut wait_error = None;
    let status = loop {
        if cancel.load(Ordering::Relaxed) || reader.is_finished() {
            drop(lifetime.take());
        } else if started.elapsed() >= limit {
            timed_out = true;
            drop(lifetime.take());
        }
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) => std::thread::sleep(Duration::from_millis(10)),
            Err(error) => {
                wait_error = Some(error.to_string());
                drop(lifetime.take());
                // Do not kill the sole reaper and abandon live descendants.
                break child.wait().ok();
            }
        }
    };
    drop(lifetime);
    let mut capture = reader
        .join()
        .map_err(|_| io::Error::other("task capture thread failed"))??;
    if let Some(error) = wait_error {
        capture.error = Some(error);
        capture.exit_code = None;
    } else if timed_out {
        capture.error = Some(format!("test run exceeded {timeout_seconds} seconds"));
    } else if !status.is_some_and(|status| status.success()) && capture.error.is_none() {
        capture.error = Some("native task supervisor terminated before confirming cleanup".into());
        capture.exit_code = None;
    }
    Ok(capture)
}

fn read_capture(mut input: impl Read, limit: usize) -> io::Result<Capture> {
    let mut magic = [0; MAGIC.len()];
    input.read_exact(&mut magic)?;
    if magic != *MAGIC {
        return Err(io::Error::other("invalid native task supervisor protocol"));
    }
    let mut result = Capture::default();
    let mut exited = false;
    loop {
        let mut tag = [0];
        let read = loop {
            match input.read(&mut tag) {
                Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                result => break result?,
            }
        };
        if read == 0 {
            break;
        }
        if exited {
            return Err(io::Error::other(
                "native task frame follows cleanup confirmation",
            ));
        }
        let mut length = [0; 4];
        input.read_exact(&mut length)?;
        let length = u32::from_be_bytes(length) as usize;
        if length > FRAME_LIMIT {
            return Err(io::Error::other("native task frame exceeds limit"));
        }
        let mut bytes = vec![0; length];
        input.read_exact(&mut bytes)?;
        match tag[0] {
            STDOUT | STDERR => {
                let output = if tag[0] == STDOUT {
                    &mut result.stdout
                } else {
                    &mut result.stderr
                };
                let keep = limit.saturating_sub(output.len()).min(bytes.len());
                output.extend_from_slice(&bytes[..keep]);
            }
            ERROR if result.error.is_none() => {
                result.error = Some(String::from_utf8(bytes).map_err(io::Error::other)?);
            }
            EXIT if bytes.len() == 5 && bytes[0] <= 1 => {
                result.exit_code =
                    (bytes[0] == 1).then(|| i32::from_be_bytes(bytes[1..].try_into().unwrap()));
                exited = true;
            }
            _ => return Err(io::Error::other("invalid native task supervisor frame")),
        }
    }
    if !exited {
        return Err(io::Error::other("native task cleanup was not confirmed"));
    }
    Ok(result)
}

fn write_frame(output: &mut impl Write, tag: u8, value: &[u8]) -> io::Result<()> {
    if value.len() > FRAME_LIMIT {
        return Err(io::Error::other("native task frame exceeds limit"));
    }
    output.write_all(&[tag])?;
    output.write_all(&(value.len() as u32).to_be_bytes())?;
    output.write_all(value)?;
    output.flush()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn end(output: &mut Vec<u8>, code: Option<i32>) {
        let mut value = vec![u8::from(code.is_some())];
        value.extend_from_slice(&code.unwrap_or(0).to_be_bytes());
        write_frame(output, EXIT, &value).unwrap();
    }

    #[test]
    fn custody_frames_preserve_binary_output_caps_and_exit_status() {
        let mut wire = MAGIC.to_vec();
        for _ in 0..20 {
            write_frame(&mut wire, STDOUT, &[0xff; 8192]).unwrap();
            write_frame(&mut wire, STDERR, b"stderr").unwrap();
        }
        end(&mut wire, Some(125));
        let mut input = Cursor::new(wire);
        let value = read_capture(&mut input, 17).unwrap();
        assert_eq!(value.stdout, [0xff; 17]);
        assert_eq!(value.stderr, b"stderrstderrstder");
        assert_eq!(value.exit_code, Some(125));
        assert!(value.error.is_none());
        assert_eq!(input.position(), input.get_ref().len() as u64);
    }

    #[test]
    fn zero_capture_still_drains_and_preserves_signal_or_spawn_failure() {
        for failure in [None, Some("controlled native spawn failure")] {
            let mut wire = MAGIC.to_vec();
            write_frame(&mut wire, STDOUT, b"unretained output").unwrap();
            if let Some(error) = failure {
                write_frame(&mut wire, ERROR, error.as_bytes()).unwrap();
            }
            end(&mut wire, None);
            let value = read_capture(Cursor::new(wire), 0).unwrap();
            assert!(value.stdout.is_empty() && value.stderr.is_empty());
            assert_eq!(value.exit_code, None);
            assert_eq!(value.error.as_deref(), failure);
        }
    }

    #[test]
    fn missing_truncated_oversized_or_duplicate_cleanup_is_rejected() {
        let mut valid = MAGIC.to_vec();
        end(&mut valid, Some(0));
        for end in 0..valid.len() {
            assert!(read_capture(Cursor::new(&valid[..end]), 20).is_err());
        }
        let mut oversized = MAGIC.to_vec();
        oversized.push(STDOUT);
        oversized.extend_from_slice(&8193u32.to_be_bytes());
        assert!(read_capture(Cursor::new(oversized), 20).is_err());
        end(&mut valid, Some(0));
        assert!(read_capture(Cursor::new(valid), 20).is_err());
    }

    #[test]
    fn unknown_or_malformed_frames_are_rejected() {
        for (tag, payload) in [
            (9, vec![]),
            (EXIT, vec![2, 0, 0, 0, 0]),
            (EXIT, vec![]),
            (ERROR, vec![0xff]),
        ] {
            let mut wire = MAGIC.to_vec();
            write_frame(&mut wire, tag, &payload).unwrap();
            end(&mut wire, None);
            assert!(read_capture(Cursor::new(wire), 20).is_err());
        }
    }
}
