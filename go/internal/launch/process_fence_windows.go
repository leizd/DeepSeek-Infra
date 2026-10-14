//go:build windows

package launch

import (
	"errors"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

type processFence struct{ handle windows.Handle }

func newProcessFence() (processFence, error) {
	handle, err := windows.CreateJobObject(nil, nil)
	if err != nil {
		return processFence{}, err
	}
	info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{}
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	if _, err := windows.SetInformationJobObject(handle, windows.JobObjectExtendedLimitInformation, uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info))); err != nil {
		_ = windows.CloseHandle(handle)
		return processFence{}, err
	}
	return processFence{handle: handle}, nil
}
func (f processFence) attributes() *syscall.SysProcAttr {
	return &syscall.SysProcAttr{CreationFlags: windows.CREATE_SUSPENDED}
}
func (f processFence) admit(pid int) error {
	process, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(pid))
	if err != nil {
		return err
	}
	defer windows.CloseHandle(process)
	if err := windows.AssignProcessToJobObject(f.handle, process); err != nil {
		return err
	}
	// The initial thread has not executed a single instruction or created any
	// descendants. Go closes CreateProcess's thread handle, so reopen exactly
	// this suspended process's initial thread after assigning its job.
	snapshot, err := windows.CreateToolhelp32Snapshot(windows.TH32CS_SNAPTHREAD, 0)
	if err != nil {
		return err
	}
	defer windows.CloseHandle(snapshot)
	entry := windows.ThreadEntry32{Size: uint32(unsafe.Sizeof(windows.ThreadEntry32{}))}
	for err = windows.Thread32First(snapshot, &entry); err == nil; err = windows.Thread32Next(snapshot, &entry) {
		if entry.OwnerProcessID != uint32(pid) {
			continue
		}
		thread, err := windows.OpenThread(windows.THREAD_SUSPEND_RESUME, false, entry.ThreadID)
		if err != nil {
			return err
		}
		previous, resumeErr := windows.ResumeThread(thread)
		_ = windows.CloseHandle(thread)
		if resumeErr != nil {
			return resumeErr
		}
		if previous != 1 {
			return errors.New("unexpected native child suspension")
		}
		return nil
	}
	return errors.New("native child initial thread not found")
}
func (f processFence) close() { _ = windows.CloseHandle(f.handle) }

func (processFence) prepare(process Process, env []string) (fencedProcess, error) {
	return fencedProcess{path: process.Path, args: []string{process.Path}, env: env}, nil
}
