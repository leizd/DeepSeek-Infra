//! Windows task custody: admit the suspended root before its first instruction.
use std::io;
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use windows_sys::Win32::Foundation::{CloseHandle, HANDLE, INVALID_HANDLE_VALUE};
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JobObjectBasicAccountingInformation, JobObjectExtendedLimitInformation,
    QueryInformationJobObject, SetInformationJobObject, TerminateJobObject,
};
use windows_sys::Win32::System::Threading::{
    CREATE_NO_WINDOW, CREATE_SUSPENDED, GetProcessIdOfThread, OpenThread, ResumeThread,
    THREAD_QUERY_LIMITED_INFORMATION, THREAD_SUSPEND_RESUME,
};

struct OwnedHandle(HANDLE);

impl Drop for OwnedHandle {
    fn drop(&mut self) {
        // Each wrapper owns exactly one successful, non-inheritable API handle.
        unsafe { CloseHandle(self.0) };
    }
}

pub(crate) struct TaskJob {
    handle: OwnedHandle,
    finished: bool,
}

impl TaskJob {
    pub(crate) fn new(command: &mut Command) -> io::Result<Self> {
        // Null SECURITY_ATTRIBUTES makes the job handle non-inheritable. A
        // parent crash therefore closes its sole handle and kills its members.
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(io::Error::last_os_error());
        }
        let handle = OwnedHandle(handle);
        // The SDK struct is plain C data; zeroes select no optional limits.
        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let ok = unsafe {
            SetInformationJobObject(
                handle.0,
                JobObjectExtendedLimitInformation,
                (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
                std::mem::size_of_val(&limits) as u32,
            )
        };
        if ok == 0 {
            return Err(io::Error::last_os_error());
        }
        // This private runner builds a fresh Command, with no caller flags.
        command.creation_flags(CREATE_SUSPENDED | CREATE_NO_WINDOW);
        Ok(Self {
            handle,
            finished: false,
        })
    }

    pub(crate) fn admit(&self, child: &Child, cancel: &AtomicBool) -> io::Result<()> {
        if cancel.load(Ordering::Relaxed) {
            return Err(io::Error::other("native task cancelled before admission"));
        }
        // Use the actual Child handle, not an independently reopened PID.
        if unsafe { AssignProcessToJobObject(self.handle.0, child.as_raw_handle()) } == 0 {
            return Err(io::Error::last_os_error());
        }
        // std closes CreateProcess's initial thread handle. As in the existing
        // Go launcher, reopen the sole thread while the root is still suspended.
        let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
        if snapshot == INVALID_HANDLE_VALUE || snapshot.is_null() {
            return Err(io::Error::last_os_error());
        }
        let snapshot = OwnedHandle(snapshot);
        let mut entry: THREADENTRY32 = unsafe { std::mem::zeroed() };
        entry.dwSize = std::mem::size_of_val(&entry) as u32;
        let mut available = unsafe { Thread32First(snapshot.0, &mut entry) } != 0;
        while available {
            if entry.th32OwnerProcessID == child.id() {
                let thread = unsafe {
                    OpenThread(
                        THREAD_SUSPEND_RESUME | THREAD_QUERY_LIMITED_INFORMATION,
                        0,
                        entry.th32ThreadID,
                    )
                };
                if thread.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let thread = OwnedHandle(thread);
                if unsafe { GetProcessIdOfThread(thread.0) } != child.id() {
                    return Err(io::Error::other(
                        "native task initial thread custody mismatch",
                    ));
                }
                if cancel.load(Ordering::Relaxed) {
                    return Err(io::Error::other("native task cancelled before admission"));
                }
                let previous = unsafe { ResumeThread(thread.0) };
                if previous == u32::MAX {
                    return Err(io::Error::last_os_error());
                }
                if previous != 1 {
                    return Err(io::Error::other("unexpected native task thread suspension"));
                }
                return Ok(());
            }
            available = unsafe { Thread32Next(snapshot.0, &mut entry) } != 0;
        }
        Err(io::Error::other("native task initial thread not found"))
    }

    pub(crate) fn finish(&mut self) -> io::Result<()> {
        if self.finished {
            return Ok(());
        }
        let mut first_error = None;
        loop {
            // Normal root exit also ends its descendants' authority. Retain the
            // job until the kernel confirms every member has exited.
            if unsafe { TerminateJobObject(self.handle.0, 1) } == 0 && first_error.is_none() {
                first_error = Some(io::Error::last_os_error());
            }
            let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { std::mem::zeroed() };
            let queried = unsafe {
                QueryInformationJobObject(
                    self.handle.0,
                    JobObjectBasicAccountingInformation,
                    (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                    std::mem::size_of_val(&info) as u32,
                    std::ptr::null_mut(),
                )
            };
            if queried != 0 && info.ActiveProcesses == 0 {
                self.finished = true;
                return first_error.map_or(Ok(()), Err);
            }
            if queried == 0 && first_error.is_none() {
                first_error = Some(io::Error::last_os_error());
            }
            // Never abandon live process custody at a cleanup deadline. A task
            // cannot settle while its descendants can still produce effects.
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}
