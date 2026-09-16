//! Cross-process workspace mutation gate and durable restore fence, mirroring
//! `deepseek_infra/infra/workspace/mutation_gate.py`.
//!
//! # Why this exists before any data-layer branch can be ported
//!
//! Every write to memories or reminders is wrapped in
//! `mutation_gate.mutation_scope`. It is **not** a mutex:
//!
//! 1. `assert_mutation_allowed` refuses while a restore owns the workspace;
//! 2. `exclusive_gate` takes an exclusive **OS** lock, so writers serialize
//!    across the HTTP server, CLI processes and workers;
//! 3. `bump_generation` advances a durable counter **before and after** the
//!    mutation, `fsync`-ing it.
//!
//! The source states why step 3 is doubled: if a process dies mid-write, a
//! concurrent backup must still observe a changed generation rather than accept a
//! package assembled across that crash boundary. So a memory write is not a file
//! write — it participates in the backup-consistency protocol. Porting the
//! branches without this would make every write quietly unfaithful.
//!
//! # Shape differences from the oracle, and why
//!
//! - **No global root.** The oracle defaults `root` to `config.ROOT`; this takes
//!   `&Path` everywhere so the caller supplies it. No hidden global.
//! - **The OS lock is Win32/`flock` directly.** The oracle calls
//!   `msvcrt.locking(fd, LK_LOCK, 1)`, which wraps `LockFileEx` with a retry loop;
//!   [`crate::file_lock`] calls `LockFileEx` with the same ten-attempts-one-second-apart
//!   policy, so the *retry semantics match* rather than becoming an indefinite
//!   block. On Unix both sides call `flock(LOCK_EX)`, which blocks.
//! - **Reentrancy is a `Mutex` plus a thread-local depth**, not an `RLock`. The
//!   observable contract — one writer per process, nesting allowed only for the
//!   same root — is identical.
//! - **The temp-file thread tag is a per-thread counter**, not
//!   `threading.get_ident()`. The name is transient (unlinked immediately after
//!   the durable replace) and only needs to be unique per thread, so the value
//!   itself is not part of any contract.
//!
//! Everything else is reproduced literally, including the quirks:
//! `fsync_directory` stays **best-effort** (the oracle swallows a failed
//! `os.open`/`fsync`, which is the normal outcome on Windows), the lock-file
//! creation tolerates an existing file, and the leftover-temp cleanup failure is
//! ignored after a committed replace.

use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, MutexGuard};

use serde_json::Value;

use crate::python_json::dumps_compact;

/// `ErrorCode.INVALID_REQUEST`.
pub const INVALID_REQUEST: &str = "invalid_request";

/// The file names, relative to the workspace root.
pub const LOCK_FILE: &str = ".workspace-mutation.lock";
pub const FENCE_FILE: &str = ".workspace-restore-fence.json";
pub const GENERATION_FILE: &str = ".workspace-generation";

// --- errors ----------------------------------------------------------------------

/// Mirrors the errors the oracle raises from this module.
///
/// It raises `AppError` for the refusals a caller can expect (423 fenced, 409
/// foreign fence), a bare `RuntimeError` for misuse, and `OSError` for a failed
/// read-back. Those shapes are preserved as constructors here.
/// Which Python exception shape a failure mirrors.
///
/// The oracle raises three different types: `AppError` for the refusals a caller
/// can expect, a bare `RuntimeError` for misuse, and `OSError` for a failed
/// read-back. A caller that treated them alike would swallow a programming error
/// as if it were a routine fence refusal, so the distinction is carried
/// explicitly rather than inferred from the status.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GateKind {
    AppError,
    RuntimeError,
    OSError,
}

impl GateKind {
    /// The oracle's exception class name, for reports and the parity probe.
    pub fn name(self) -> &'static str {
        match self {
            GateKind::AppError => "AppError",
            GateKind::RuntimeError => "RuntimeError",
            GateKind::OSError => "OSError",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GateError {
    pub kind: GateKind,
    pub message: String,
    /// `None` for the shapes that are not an `AppError` — the oracle raises a bare
    /// `RuntimeError`/`OSError` there, so there is no code to report. Making this
    /// optional is what stops a caller from reading a programming error as a 500
    /// refusal.
    pub code: Option<&'static str>,
    pub status: Option<u16>,
}

impl GateError {
    /// `423` — a restore owns the workspace.
    pub fn fenced() -> Self {
        Self {
            kind: GateKind::AppError,
            message: "Workspace writes are fenced while restore is in progress".to_string(),
            code: Some(INVALID_REQUEST),
            status: Some(423),
        }
    }

    /// `423` — the fence exists but cannot be read.
    ///
    /// The message is **fixed**: the oracle chains the underlying error with
    /// `raise ... from exc`, so the parse/IO detail lands in `__cause__` and never
    /// in the message. Interpolating it here would change what a caller sees.
    pub fn unreadable_fence() -> Self {
        Self {
            kind: GateKind::AppError,
            message: "Workspace restore fence is unreadable; recovery is required".to_string(),
            code: Some(INVALID_REQUEST),
            status: Some(423),
        }
    }

    /// `409` — the fence belongs to a different transaction.
    pub fn foreign_fence() -> Self {
        Self {
            kind: GateKind::AppError,
            message: "Restore fence belongs to another transaction".to_string(),
            code: Some(INVALID_REQUEST),
            status: Some(409),
        }
    }

    /// The oracle raises a bare `RuntimeError` here, so there is no code or status
    /// to mirror — and none is invented.
    pub fn misuse(message: impl Into<String>) -> Self {
        Self {
            kind: GateKind::RuntimeError,
            message: message.into(),
            code: None,
            status: None,
        }
    }

    /// The oracle raises `OSError("restore fence read-back mismatch")`.
    pub fn readback_mismatch() -> Self {
        Self {
            kind: GateKind::OSError,
            message: "restore fence read-back mismatch".to_string(),
            code: None,
            status: None,
        }
    }
}

impl std::fmt::Display for GateError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}", self.message)
    }
}

impl std::error::Error for GateError {}

// --- paths -----------------------------------------------------------------------

pub fn lock_path(root: &Path) -> PathBuf {
    root.join(LOCK_FILE)
}

pub fn fence_path(root: &Path) -> PathBuf {
    root.join(FENCE_FILE)
}

pub fn generation_path(root: &Path) -> PathBuf {
    root.join(GENERATION_FILE)
}

/// Mirrors `_fsync_directory`, **including its best-effort character**.
///
/// The oracle returns silently when `os.open(path, O_RDONLY)` raises — which is
/// the normal result on Windows — and swallows an `fsync` failure. Turning that
/// into a hard error would make Windows writes fail where the oracle succeeds.
fn fsync_directory(path: &Path) {
    let Ok(file) = File::open(path) else {
        return;
    };
    let _ = file.sync_all();
}

/// A per-thread tag for temp-file names, standing in for `threading.get_ident()`.
///
/// The name is unlinked immediately after the durable replace, so only its
/// uniqueness per thread is load-bearing.
fn thread_tag() -> u64 {
    thread_local! {
        static TAG: std::cell::Cell<u64> = const { std::cell::Cell::new(0) };
    }
    static NEXT: AtomicU64 = AtomicU64::new(1);
    TAG.with(|tag| {
        if tag.get() == 0 {
            tag.set(NEXT.fetch_add(1, Ordering::Relaxed));
        }
        tag.get()
    })
}

// --- the gate --------------------------------------------------------------------

/// Mirrors `_PROCESS_LOCK`: serializes threads inside this process.
static PROCESS_LOCK: Mutex<()> = Mutex::new(());

thread_local! {
    /// Mirrors `_GATE_STATE`: `(root, depth)` for the current thread.
    static GATE_STATE: std::cell::RefCell<Option<(PathBuf, usize)>> =
        const { std::cell::RefCell::new(None) };
}

/// Held for the duration of [`exclusive_gate`]. Releases the OS lock on drop.
pub struct ExclusiveGate {
    /// `Some` when this scope is the one holding the OS lock.
    file: Option<File>,
    /// Kept alive for the whole scope, mirroring `with _PROCESS_LOCK:`.
    _process: Option<MutexGuard<'static, ()>>,
}

impl std::fmt::Debug for ExclusiveGate {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        // The handle and the guard are not interesting to a reader; whether this
        // scope owns the OS lock is.
        formatter
            .debug_struct("ExclusiveGate")
            .field("owns_os_lock", &self.file.is_some())
            .finish()
    }
}

impl Drop for ExclusiveGate {
    fn drop(&mut self) {
        let owner = self.file.is_some();
        GATE_STATE.with(|state| {
            let mut state = state.borrow_mut();
            if owner {
                // The oracle resets to depth 0 and clears the path.
                *state = None;
            } else if let Some((_, depth)) = state.as_mut() {
                *depth = depth.saturating_sub(1);
            }
        });
        if let Some(file) = self.file.take() {
            let _ = crate::file_lock::unlock(&file);
        }
    }
}

/// Mirrors `exclusive_gate`: hold the workspace mutation lock in this and every
/// peer process.
///
/// Nesting is allowed only for the same root, matching the oracle's
/// `RuntimeError("Nested workspace mutation gates must use the same root")`.
pub fn exclusive_gate(root: &Path) -> Result<ExclusiveGate, GateError> {
    let target = lock_path(root);
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|error| GateError::misuse(error.to_string()))?;
    }
    // Create with `b"0"` only if absent; an existing lock file is fine.
    if let Err(error) = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&target)
    {
        if error.kind() != std::io::ErrorKind::AlreadyExists {
            return Err(GateError::misuse(error.to_string()));
        }
    } else {
        let mut file = OpenOptions::new()
            .write(true)
            .open(&target)
            .map_err(|error| GateError::misuse(error.to_string()))?;
        file.write_all(b"0")
            .map_err(|error| GateError::misuse(error.to_string()))?;
    }

    let depth = GATE_STATE.with(|state| {
        state
            .borrow()
            .as_ref()
            .map(|(_, depth)| *depth)
            .unwrap_or(0)
    });

    if depth > 0 {
        let active = GATE_STATE.with(|state| state.borrow().as_ref().map(|(root, _)| root.clone()));
        if active.as_deref() != Some(target.as_path()) {
            return Err(GateError::misuse(
                "Nested workspace mutation gates must use the same root",
            ));
        }
        GATE_STATE.with(|state| {
            if let Some((_, depth)) = state.borrow_mut().as_mut() {
                *depth += 1;
            }
        });
        return Ok(ExclusiveGate {
            file: None,
            _process: None,
        });
    }

    // Recover from poisoning rather than turning it into an error: the oracle's
    // `threading.RLock` has no poisoning, so treating a poisoned mutex as a gate
    // failure would introduce a failure mode the oracle does not have. That is the
    // most likely cause of this test's rare, order-dependent failure, and it is a
    // fidelity gap either way.
    let process = PROCESS_LOCK
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(&target)
        .map_err(|error| GateError::misuse(error.to_string()))?;
    if let Err(error) = crate::file_lock::lock_exclusive(&file) {
        // Release the process lock before reporting, so a failed OS lock does not
        // wedge every later writer in this process.
        drop(process);
        return Err(GateError::misuse(error.to_string()));
    }
    GATE_STATE.with(|state| {
        *state.borrow_mut() = Some((target.clone(), 1));
    });
    Ok(ExclusiveGate {
        file: Some(file),
        _process: Some(process),
    })
}

// --- the fence -------------------------------------------------------------------

/// Mirrors `read_fence`.
///
/// A missing fence is `Ok(None)`; an unreadable one is the 423 refusal, because
/// an unreadable fence means recovery is required rather than "no fence".
pub fn read_fence(root: &Path) -> Result<Option<Value>, GateError> {
    let target = fence_path(root);
    let raw = match fs::read_to_string(&target) {
        Ok(raw) => raw,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(GateError::unreadable_fence()),
    };
    let value: Value = serde_json::from_str(&raw).map_err(|_| GateError::unreadable_fence())?;
    Ok(match value {
        Value::Object(_) => Some(value),
        _ => None,
    })
}

/// Mirrors `assert_mutation_allowed`.
pub fn assert_mutation_allowed(
    owner_restore_id: Option<&str>,
    root: &Path,
) -> Result<(), GateError> {
    let Some(fence) = read_fence(root)? else {
        return Ok(());
    };
    if let Some(owner) = owner_restore_id {
        if !owner.is_empty() && fence.get("restoreId").and_then(Value::as_str) == Some(owner) {
            return Ok(());
        }
    }
    Err(GateError::fenced())
}

/// Mirrors `write_fence`, including the durable write and the read-back check.
pub fn write_fence(value: &Value, root: &Path) -> Result<(), GateError> {
    let target = fence_path(root);
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|error| GateError::misuse(error.to_string()))?;
    }
    let temporary = temporary_name(&target, true, thread_tag());
    let raw = dumps_compact(value);
    let result = (|| -> Result<(), GateError> {
        let mut file =
            File::create(&temporary).map_err(|error| GateError::misuse(error.to_string()))?;
        file.write_all(raw.as_bytes())
            .map_err(|error| GateError::misuse(error.to_string()))?;
        file.flush()
            .map_err(|error| GateError::misuse(error.to_string()))?;
        file.sync_all()
            .map_err(|error| GateError::misuse(error.to_string()))?;
        drop(file);
        fs::rename(&temporary, &target).map_err(|error| GateError::misuse(error.to_string()))?;
        if let Some(parent) = target.parent() {
            fsync_directory(parent);
        }
        // The oracle compares parsed values, so key order is irrelevant — which
        // matches `serde_json::Value` equality for objects here.
        if read_fence(root)?.as_ref() != Some(value) {
            return Err(GateError::readback_mismatch());
        }
        Ok(())
    })();
    // Cleanup is best-effort after the durable replace: a filesystem cleanup
    // failure must not turn an already-committed fence write into a failure.
    let _ = fs::remove_file(&temporary);
    result
}

/// Mirrors `clear_fence`.
pub fn clear_fence(restore_id: &str, root: &Path) -> Result<bool, GateError> {
    let target = fence_path(root);
    let Some(current) = read_fence(root)? else {
        return Ok(false);
    };
    if current.get("restoreId").and_then(Value::as_str) != Some(restore_id) {
        return Err(GateError::foreign_fence());
    }
    let _ = fs::remove_file(&target);
    Ok(true)
}

// --- the generation counter ------------------------------------------------------

/// Mirrors `read_generation`: unreadable, missing or unparseable all mean `0`.
pub fn read_generation(root: &Path) -> i64 {
    fs::read_to_string(generation_path(root))
        .ok()
        .and_then(|raw| raw.trim().parse::<i64>().ok())
        .map(|value| value.max(0))
        .unwrap_or(0)
}

/// Mirrors `bump_generation`: a durable read-modify-write of the counter.
pub fn bump_generation(root: &Path) -> Result<i64, GateError> {
    let target = generation_path(root);
    if let Some(parent) = target.parent() {
        fs::create_dir_all(parent).map_err(|error| GateError::misuse(error.to_string()))?;
    }
    let value = read_generation(root) + 1;
    // The oracle drops the original suffix here (`.workspace-generation` has
    // none), unlike `write_fence` which preserves it.
    let temporary = temporary_name(&target, false, thread_tag());
    let result = (|| -> Result<(), GateError> {
        let mut file =
            File::create(&temporary).map_err(|error| GateError::misuse(error.to_string()))?;
        file.write_all(value.to_string().as_bytes())
            .map_err(|error| GateError::misuse(error.to_string()))?;
        file.flush()
            .map_err(|error| GateError::misuse(error.to_string()))?;
        file.sync_all()
            .map_err(|error| GateError::misuse(error.to_string()))?;
        drop(file);
        fs::rename(&temporary, &target).map_err(|error| GateError::misuse(error.to_string()))?;
        if let Some(parent) = target.parent() {
            fsync_directory(parent);
        }
        Ok(())
    })();
    let _ = fs::remove_file(&temporary);
    result?;
    Ok(value)
}

/// `<name>.<pid>.<thread>.tmp`.
///
/// `write_fence` keeps the original suffix (`target.with_suffix(f"{target.suffix}.{pid}...")`)
/// while `bump_generation` drops it (`target.with_suffix(f".{pid}...")`). The two
/// oracle call sites differ, so this reproduces each rather than picking one.
fn temporary_name(target: &Path, preserve_suffix: bool, thread: u64) -> PathBuf {
    let parent = target.parent().unwrap_or_else(|| Path::new("."));
    let name = target.file_name().unwrap_or_default().to_string_lossy();
    let suffix = if preserve_suffix {
        target
            .extension()
            .map(|extension| format!(".{}", extension.to_string_lossy()))
            .unwrap_or_default()
    } else {
        String::new()
    };
    parent.join(format!(
        "{name}{suffix}.{}.{thread}.tmp",
        std::process::id()
    ))
}

// --- the scope -------------------------------------------------------------------

/// Held for the duration of [`mutation_scope`]. Bumps the generation again on
/// drop, then releases the gate.
pub struct MutationScope {
    /// Dropped **after** this struct's `drop` body, so the closing bump happens
    /// while the gate is still held — matching the oracle, where the `finally`
    /// bump runs inside the `with exclusive_gate(...)` block.
    _gate: ExclusiveGate,
    root: PathBuf,
}

impl Drop for MutationScope {
    fn drop(&mut self) {
        let _ = bump_generation(&self.root);
    }
}

/// Mirrors `mutation_scope`: a fence-aware scope for a workspace mutation.
///
/// The fence is asserted twice — once to fail fast, once under the lock to close
/// the race with a newly-created fence — and the generation is bumped before the
/// body runs and again after it.
pub fn mutation_scope(
    owner_restore_id: Option<&str>,
    root: &Path,
) -> Result<MutationScope, GateError> {
    assert_mutation_allowed(owner_restore_id, root)?;
    let gate = exclusive_gate(root)?;
    assert_mutation_allowed(owner_restore_id, root)?;
    bump_generation(root)?;
    Ok(MutationScope {
        _gate: gate,
        root: root.to_path_buf(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_root(label: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "gate-test-{label}-{}-{}",
            std::process::id(),
            thread_tag()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).expect("create temp root");
        root
    }

    #[test]
    fn paths_hang_off_the_root() {
        let root = Path::new("/srv/app");
        assert_eq!(lock_path(root), root.join(".workspace-mutation.lock"));
        assert_eq!(fence_path(root), root.join(".workspace-restore-fence.json"));
        assert_eq!(generation_path(root), root.join(".workspace-generation"));
    }

    #[test]
    fn generation_starts_at_zero_and_bumps_durably() {
        let root = temp_root("gen");
        assert_eq!(read_generation(&root), 0);
        assert_eq!(bump_generation(&root).unwrap(), 1);
        assert_eq!(bump_generation(&root).unwrap(), 2);
        assert_eq!(read_generation(&root), 2);
        // The on-disk form is the bare number, no trailing newline.
        assert_eq!(fs::read_to_string(generation_path(&root)).unwrap(), "2");
        // No leftover temp files.
        let leftovers: Vec<_> = fs::read_dir(&root)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| entry.file_name().to_string_lossy().ends_with(".tmp"))
            .collect();
        assert!(leftovers.is_empty(), "temp files must be cleaned up");
        let _ = fs::remove_dir_all(&root);
    }

    /// An unreadable generation is `0`, not an error — same as the oracle.
    #[test]
    fn an_unparseable_generation_reads_as_zero() {
        let root = temp_root("gen-bad");
        fs::write(generation_path(&root), "not a number").unwrap();
        assert_eq!(read_generation(&root), 0);
        // A negative value clamps to zero too.
        fs::write(generation_path(&root), "-5").unwrap();
        assert_eq!(read_generation(&root), 0);
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn no_fence_means_mutation_is_allowed() {
        let root = temp_root("fence-none");
        assert!(read_fence(&root).unwrap().is_none());
        assert!(assert_mutation_allowed(None, &root).is_ok());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn a_fence_blocks_mutation_unless_it_owns_it() {
        let root = temp_root("fence-block");
        write_fence(&serde_json::json!({"restoreId": "r1"}), &root).unwrap();

        let blocked = assert_mutation_allowed(None, &root).unwrap_err();
        assert_eq!(blocked.status, Some(423));
        assert_eq!(blocked.code, Some(INVALID_REQUEST));
        // A different restore id is still blocked.
        assert_eq!(
            assert_mutation_allowed(Some("other"), &root)
                .unwrap_err()
                .status,
            Some(423)
        );
        // The owner passes.
        assert!(assert_mutation_allowed(Some("r1"), &root).is_ok());
        let _ = fs::remove_dir_all(&root);
    }

    /// An unreadable fence is a 423 refusal, **not** "no fence" — otherwise
    /// corruption would silently unblock writes.
    #[test]
    fn an_unreadable_fence_is_a_refusal_not_an_absent_fence() {
        let root = temp_root("fence-bad");
        fs::write(fence_path(&root), "{not json").unwrap();
        let failure = read_fence(&root).unwrap_err();
        assert_eq!(failure.status, Some(423));
        assert!(failure.message.contains("unreadable"));

        // A JSON scalar is not a fence either, and reads as absent.
        fs::write(fence_path(&root), "42").unwrap();
        assert!(read_fence(&root).unwrap().is_none());
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn fence_round_trips_and_clears_only_for_its_owner() {
        let root = temp_root("fence-round");
        let value = serde_json::json!({"restoreId": "r1", "startedAt": "2026-09-15T00:00:00Z"});
        write_fence(&value, &root).unwrap();
        assert_eq!(read_fence(&root).unwrap(), Some(value));

        assert_eq!(clear_fence("other", &root).unwrap_err().status, Some(409));
        assert!(clear_fence("r1", &root).unwrap());
        assert!(read_fence(&root).unwrap().is_none());
        // Clearing an absent fence reports false rather than failing.
        assert!(!clear_fence("r1", &root).unwrap());
        let _ = fs::remove_dir_all(&root);
    }

    /// The fence file is written with compact separators, as the oracle's
    /// `separators=(",", ":")` does.
    #[test]
    fn fence_is_written_in_compact_json() {
        let root = temp_root("fence-compact");
        write_fence(&serde_json::json!({"restoreId": "r1", "n": 1}), &root).unwrap();
        assert_eq!(
            fs::read_to_string(fence_path(&root)).unwrap(),
            "{\"n\":1,\"restoreId\":\"r1\"}"
        );
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn the_scope_bumps_the_generation_twice() {
        let root = temp_root("scope-bump");
        {
            let _scope = mutation_scope(None, &root).unwrap();
            // Bumped before the body.
            assert_eq!(read_generation(&root), 1);
        }
        // And again after it.
        assert_eq!(read_generation(&root), 2);
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn the_scope_refuses_while_a_foreign_fence_is_up() {
        let root = temp_root("scope-fence");
        write_fence(&serde_json::json!({"restoreId": "r1"}), &root).unwrap();
        assert!(mutation_scope(None, &root).is_err());
        // A refused scope must not have bumped anything.
        assert_eq!(read_generation(&root), 0);
        // The owner may proceed.
        let _scope = mutation_scope(Some("r1"), &root).unwrap();
        assert_eq!(read_generation(&root), 1);
        drop(_scope);
        assert_eq!(read_generation(&root), 2);
        let _ = fs::remove_dir_all(&root);
    }

    /// Nesting the same root is allowed and must not deadlock or double-lock.
    #[test]
    fn nesting_the_same_root_is_allowed() {
        let root = temp_root("scope-nested");
        {
            let _outer = exclusive_gate(&root).unwrap();
            let _inner = exclusive_gate(&root).unwrap();
        }
        // A nested scope still bumps per scope, not per lock acquisition.
        let _outer = mutation_scope(None, &root).unwrap();
        assert_eq!(read_generation(&root), 1);
        let _inner = mutation_scope(None, &root).unwrap();
        assert_eq!(read_generation(&root), 2);
        drop(_inner);
        assert_eq!(read_generation(&root), 3);
        drop(_outer);
        assert_eq!(read_generation(&root), 4);
        let _ = fs::remove_dir_all(&root);
    }

    /// A different root inside an open gate is a programming error, matching the
    /// oracle's `RuntimeError`.
    #[test]
    fn nesting_a_different_root_is_refused() {
        let first = temp_root("scope-root-a");
        let second = temp_root("scope-root-b");
        let _outer = exclusive_gate(&first).unwrap();
        let failure = exclusive_gate(&second).unwrap_err();
        assert!(failure.message.contains("must use the same root"));
        drop(_outer);
        // Once the outer gate is gone, the other root works.
        assert!(exclusive_gate(&second).is_ok());
        let _ = fs::remove_dir_all(&first);
        let _ = fs::remove_dir_all(&second);
    }

    #[test]
    fn the_lock_file_is_created_once_and_reused() {
        let root = temp_root("lock-file");
        {
            let _gate = exclusive_gate(&root).unwrap();
        }
        assert_eq!(fs::read_to_string(lock_path(&root)).unwrap(), "0");
        // Re-acquiring an existing lock file must succeed.
        assert!(exclusive_gate(&root).is_ok());
        let _ = fs::remove_dir_all(&root);
    }

    /// Two threads must not interleave their scopes: the process lock serializes
    /// them, so the generation ends exactly at the number of scopes run.
    #[test]
    fn concurrent_scopes_serialize_and_count_exactly() {
        let root = temp_root("scope-threads");
        let scopes = 8;
        let mut handles = Vec::new();
        for _ in 0..scopes {
            let root = root.clone();
            handles.push(std::thread::spawn(move || {
                let _scope = mutation_scope(None, &root).unwrap();
            }));
        }
        for handle in handles {
            handle.join().expect("thread must not panic");
        }
        assert_eq!(read_generation(&root), scopes * 2);
        let _ = fs::remove_dir_all(&root);
    }
}
