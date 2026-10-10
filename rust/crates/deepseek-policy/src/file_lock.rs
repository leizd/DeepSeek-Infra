//! Cross-process advisory file locking, shared by the workspace mutation gate and
//! the memory store.
//!
//! Both the oracle's `mutation_gate._lock_file`/`_unlock_file` and its
//! `memory_file_lock` do the same two things with different wrappers, so this is one
//! implementation rather than two.
//!
//! # Retry semantics are part of the contract
//!
//! On Windows the oracle calls `msvcrt.locking(fd, LK_LOCK, 1)`, which is a retry
//! loop over `LockFileEx`: try, wait a second, give up after ten attempts. Using
//! `LockFileEx` without `LOCKFILE_FAIL_IMMEDIATELY` would block **forever** where the
//! oracle raises, so the loop is reproduced rather than simplified. On Unix both
//! sides call `flock(LOCK_EX)`, which blocks.
//!
//! The lock is taken on **one byte at offset zero**, matching
//! `handle.seek(0)` followed by `locking(fd, …, 1)`.

#[cfg(windows)]
mod imp {
    use std::ffi::c_void;
    use std::fs::File;
    use std::io;
    use std::os::windows::io::AsRawHandle;
    use std::thread::sleep;
    use std::time::Duration;

    #[repr(C)]
    struct Overlapped {
        internal: usize,
        internal_high: usize,
        offset: u32,
        offset_high: u32,
        event: *mut c_void,
    }

    impl Overlapped {
        fn at_start() -> Self {
            Self {
                internal: 0,
                internal_high: 0,
                offset: 0,
                offset_high: 0,
                event: std::ptr::null_mut(),
            }
        }
    }

    #[link(name = "kernel32")]
    unsafe extern "C" {
        fn LockFileEx(
            handle: *mut c_void,
            flags: u32,
            reserved: u32,
            low: u32,
            high: u32,
            overlapped: *mut Overlapped,
        ) -> i32;
        fn UnlockFileEx(
            handle: *mut c_void,
            reserved: u32,
            low: u32,
            high: u32,
            overlapped: *mut Overlapped,
        ) -> i32;
    }

    const LOCKFILE_FAIL_IMMEDIATELY: u32 = 0x0000_0001;
    const LOCKFILE_EXCLUSIVE_LOCK: u32 = 0x0000_0002;
    /// `msvcrt.LK_LOCK`: retry once a second, give up after ten attempts.
    const ATTEMPTS: u32 = 10;

    pub fn lock_exclusive(file: &File) -> io::Result<()> {
        let handle = file.as_raw_handle();
        for attempt in 0..ATTEMPTS {
            let mut overlapped = Overlapped::at_start();
            let locked = unsafe {
                LockFileEx(
                    handle,
                    LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                    0,
                    1,
                    0,
                    &mut overlapped,
                )
            };
            if locked != 0 {
                return Ok(());
            }
            if attempt + 1 < ATTEMPTS {
                sleep(Duration::from_secs(1));
            }
        }
        Err(io::Error::last_os_error())
    }

    pub fn unlock(file: &File) -> io::Result<()> {
        let handle = file.as_raw_handle();
        let mut overlapped = Overlapped::at_start();
        let unlocked = unsafe { UnlockFileEx(handle, 0, 1, 0, &mut overlapped) };
        if unlocked == 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
}

#[cfg(unix)]
mod imp {
    use std::ffi::c_int;
    use std::fs::File;
    use std::io;
    use std::os::fd::AsRawFd;

    unsafe extern "C" {
        fn flock(fd: c_int, operation: c_int) -> c_int;
    }

    // `flock` blocks, exactly as the oracle's `fcntl.flock(..., LOCK_EX)` does.
    const LOCK_EX: c_int = 2;
    const LOCK_UN: c_int = 8;

    pub fn lock_exclusive(file: &File) -> io::Result<()> {
        let result = unsafe { flock(file.as_raw_fd(), LOCK_EX) };
        if result != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }

    pub fn unlock(file: &File) -> io::Result<()> {
        let result = unsafe { flock(file.as_raw_fd(), LOCK_UN) };
        if result != 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(())
    }
}

/// Take an exclusive advisory lock on `file`, blocking or retrying like the oracle.
pub fn lock_exclusive(file: &std::fs::File) -> std::io::Result<()> {
    imp::lock_exclusive(file)
}

/// Release the lock taken by [`lock_exclusive`].
pub fn unlock(file: &std::fs::File) -> std::io::Result<()> {
    imp::unlock(file)
}

/// Open (creating if needed) a lock file and hold an exclusive lock until the guard
/// is dropped.
///
/// Mirrors the shape both call sites share: open `a+b`, ensure the file is non-empty
/// when the platform needs a byte to lock, lock, and unlock on the way out.
pub struct FileLockGuard {
    file: std::fs::File,
}

impl FileLockGuard {
    /// Acquire the lock. `needs_seed_byte` mirrors the memory store's Windows-only
    /// `write(b"\0") if tell() == 0` step.
    pub fn acquire(path: &std::path::Path, needs_seed_byte: bool) -> std::io::Result<Self> {
        use std::io::{Seek, SeekFrom, Write};
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let mut file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        if needs_seed_byte && file.metadata()?.len() == 0 {
            file.write_all(b"\0")?;
            file.flush()?;
        }
        file.seek(SeekFrom::Start(0))?;
        lock_exclusive(&file)?;
        Ok(Self { file })
    }
}

impl Drop for FileLockGuard {
    fn drop(&mut self) {
        use std::io::{Seek, SeekFrom};
        let _ = self.file.seek(SeekFrom::Start(0));
        let _ = unlock(&self.file);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_path(label: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("file-lock-{label}-{}", std::process::id()))
    }

    #[test]
    fn a_guard_locks_and_releases() {
        let path = temp_path("basic");
        let _ = std::fs::remove_file(&path);
        {
            let _guard = FileLockGuard::acquire(&path, false).expect("acquire");
            assert!(path.exists());
        }
        // Re-acquiring after release must succeed.
        let _guard = FileLockGuard::acquire(&path, false).expect("re-acquire");
        let _ = std::fs::remove_file(&path);
    }

    /// The memory store seeds a byte on Windows so there is something to lock.
    #[test]
    fn the_seed_byte_is_written_once() {
        let path = temp_path("seed");
        let _ = std::fs::remove_file(&path);
        for _ in 0..2 {
            let _guard = FileLockGuard::acquire(&path, true).expect("acquire");
        }
        // Windows: one seed byte. Unix: the flag is not passed by callers, so this
        // assertion only applies where the oracle also seeds.
        #[cfg(windows)]
        assert_eq!(std::fs::metadata(&path).expect("stat").len(), 1);
        let _ = std::fs::remove_file(&path);
    }

    /// Two threads must serialize rather than both holding the lock.
    #[test]
    fn locks_serialize_between_threads() {
        use std::sync::Arc;
        use std::sync::atomic::{AtomicI64, Ordering};

        let path = temp_path("threads");
        let _ = std::fs::remove_file(&path);
        let concurrent = Arc::new(AtomicI64::new(0));
        let mut handles = Vec::new();
        for _ in 0..6 {
            let path = path.clone();
            let concurrent = Arc::clone(&concurrent);
            handles.push(std::thread::spawn(move || {
                let _guard = FileLockGuard::acquire(&path, false).expect("acquire");
                // If the lock were not exclusive, two threads would overlap here.
                let now = concurrent.fetch_add(1, Ordering::SeqCst);
                std::thread::sleep(std::time::Duration::from_millis(5));
                concurrent.fetch_sub(1, Ordering::SeqCst);
                assert_eq!(now, 0, "another thread held the lock at the same time");
            }));
        }
        for handle in handles {
            handle.join().expect("thread must not panic");
        }
        let _ = std::fs::remove_file(&path);
    }
}
