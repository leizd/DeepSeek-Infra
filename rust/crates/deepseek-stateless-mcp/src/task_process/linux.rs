use super::{ERROR, EXIT, FRAME_LIMIT, MAGIC, STDERR, STDOUT, write_frame};
use std::ffi::OsString;
use std::io::{self, Read, Write};
use std::os::unix::fs::FileTypeExt;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

type Output = Arc<Mutex<io::Stdout>>;

pub(super) fn main(arguments: Vec<OsString>) -> i32 {
    let output = Arc::new(Mutex::new(io::stdout()));
    if output.lock().unwrap().write_all(MAGIC).is_err() {
        return 125;
    }
    let result = supervise(arguments, &output);
    let mut code = None;
    if let Ok(value) = &result {
        code = *value;
    } else if let Err(error) = &result {
        let text = error.to_string();
        let mut end = text.len().min(FRAME_LIMIT);
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        if emit(&output, ERROR, &text.as_bytes()[..end]).is_err() {
            return 125;
        }
    }
    // This is emitted only after kernel ECHILD and both pipe readers finish.
    let mut exit = vec![u8::from(code.is_some())];
    exit.extend_from_slice(&code.unwrap_or(0).to_be_bytes());
    if emit(&output, EXIT, &exit).is_err() {
        125
    } else {
        0
    }
}

fn emit(output: &Output, tag: u8, bytes: &[u8]) -> io::Result<()> {
    let mut writer = output
        .lock()
        .map_err(|_| io::Error::other("native task frame writer failed"))?;
    write_frame(&mut *writer, tag, bytes)
}

fn forward(
    input: impl Read + Send + 'static,
    output: Output,
    stop: Arc<AtomicBool>,
    tag: u8,
) -> io::Result<std::thread::JoinHandle<io::Result<()>>> {
    std::thread::Builder::new()
        .name("native-task-pipe".into())
        .spawn(move || {
            let result = (|| {
                let mut input = input;
                let mut bytes = [0; FRAME_LIMIT];
                loop {
                    let count = match input.read(&mut bytes) {
                        Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                        result => result?,
                    };
                    if count == 0 {
                        return Ok(());
                    }
                    emit(&output, tag, &bytes[..count])?;
                }
            })();
            if result.is_err() {
                stop.store(true, Ordering::Relaxed);
            }
            result
        })
}

// std::Child::wait must never race this loop: it alone owns waitpid(-1).
// An exited immediate child's PID stays reserved until this same loop reaps it.
#[allow(clippy::zombie_processes)]
fn supervise(arguments: Vec<OsString>, output: &Output) -> io::Result<Option<i32>> {
    let Some(program) = arguments.first() else {
        return Err(io::Error::other("native task program missing"));
    };
    if !std::fs::metadata("/proc/self/fd/0")?.file_type().is_fifo() {
        return Err(io::Error::other("native task lifetime pipe missing"));
    }
    std::fs::read_dir("/proc")?;
    // Unlike a process group, a task-local subreaper adopts double-forked and
    // setsid descendants too. Do this before any task can execute an instruction.
    unsafe {
        let mut action: libc::sigaction = std::mem::zeroed();
        action.sa_sigaction = libc::SIG_DFL;
        libc::sigemptyset(&mut action.sa_mask);
        if libc::sigaction(libc::SIGCHLD, &action, std::ptr::null_mut()) != 0 {
            return Err(io::Error::last_os_error());
        }
        if libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    let stop = Arc::new(AtomicBool::new(false));
    std::thread::Builder::new()
        .name("native-task-lifetime".into())
        .spawn({
            let stop = Arc::clone(&stop);
            move || {
                let mut byte = [0];
                loop {
                    match io::stdin().read(&mut byte) {
                        Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
                        _ => break,
                    }
                }
                stop.store(true, Ordering::Relaxed);
            }
        })?;
    if stop.load(Ordering::Relaxed) {
        return Err(io::Error::other("native task authority ended before spawn"));
    }
    let mut child = Command::new(program)
        .args(&arguments[1..])
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let root = child.id() as libc::pid_t;
    let stdout = forward(
        child.stdout.take().unwrap(),
        Arc::clone(output),
        Arc::clone(&stop),
        STDOUT,
    );
    let stderr = forward(
        child.stderr.take().unwrap(),
        Arc::clone(output),
        Arc::clone(&stop),
        STDERR,
    );
    let mut error = None;
    if stdout.is_err() || stderr.is_err() {
        stop.store(true, Ordering::Relaxed);
        error = Some(io::Error::other(
            "native task output reader could not start",
        ));
    }
    let mut root_status = None;
    let mut reported = false;
    loop {
        if stop.load(Ordering::Relaxed) || root_status.is_some() {
            if let Err(failure) = kill_owned_children() {
                if !reported {
                    eprintln!("native task descendant cleanup delayed: {failure}");
                    reported = true;
                }
            }
        }
        let mut status = 0;
        let pid = unsafe { libc::waitpid(-1, &mut status, libc::WNOHANG) };
        if pid == root && root_status.is_none() {
            root_status = Some(if libc::WIFEXITED(status) {
                Some(libc::WEXITSTATUS(status))
            } else {
                None
            });
        } else if pid < 0 {
            let failure = io::Error::last_os_error();
            match failure.raw_os_error() {
                Some(libc::ECHILD) => {
                    if root_status.is_none() {
                        error = Some(io::Error::other(
                            "native task disappeared without a waitable exit",
                        ));
                    }
                    break;
                }
                Some(libc::EINTR) => continue,
                _ => {
                    stop.store(true, Ordering::Relaxed);
                    if !reported {
                        eprintln!("native task child wait delayed: {failure}");
                        reported = true;
                    }
                }
            }
        }
        if pid <= 0 {
            std::thread::sleep(Duration::from_millis(10));
        }
    }
    drop(child); // No Child wait or competing process reaper runs in this binary role.
    for reader in [stdout, stderr].into_iter().flatten() {
        match reader.join() {
            Ok(Ok(())) => {}
            Ok(Err(failure)) => {
                if error.is_none() {
                    error = Some(failure);
                }
            }
            Err(_) => {
                if error.is_none() {
                    error = Some(io::Error::other("native task output reader failed"));
                }
            }
        }
    }
    match error {
        Some(error) => Err(error),
        None => Ok(root_status.flatten()),
    }
}

fn owned_child(stat: &str, parent: u32) -> bool {
    let Some((_, fields)) = stat.rsplit_once(')') else {
        return false;
    };
    fields
        .split_whitespace()
        .nth(1)
        .and_then(|pid| pid.parse::<u32>().ok())
        == Some(parent)
}

fn kill_owned_children() -> io::Result<()> {
    let parent = std::process::id();
    for entry in std::fs::read_dir("/proc")? {
        let entry = entry?;
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|name| name.parse::<libc::pid_t>().ok())
        else {
            continue;
        };
        let stat = match std::fs::read_to_string(entry.path().join("stat")) {
            Ok(stat) => stat,
            Err(error)
                if matches!(
                    error.kind(),
                    io::ErrorKind::NotFound | io::ErrorKind::PermissionDenied
                ) =>
            {
                continue;
            }
            Err(error) => return Err(error),
        };
        if pid > 0 && owned_child(&stat, parent) && unsafe { libc::kill(pid, libc::SIGKILL) } != 0 {
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::ESRCH) {
                return Err(error);
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::owned_child;

    #[test]
    fn child_custody_parses_kernel_parent_after_the_last_command_parenthesis() {
        assert!(owned_child("42 (a ) confusing ( name) S 123 0 0", 123));
        for stat in [
            "42 (other) S 124 0 0",
            "42 S 123",
            "42 (bad) S",
            "42 (bad) S nope",
        ] {
            assert!(!owned_child(stat, 123));
        }
    }
}
