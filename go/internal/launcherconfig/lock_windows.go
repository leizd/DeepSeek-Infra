//go:build windows

package launcherconfig

import (
	"golang.org/x/sys/windows"
	"os"
)

// The kernel releases this lock on process exit, including a hard kill. The
// path is retained: replacing or deleting it would split writer ownership.
func lockWriter(file *os.File) error {
	return windows.LockFileEx(windows.Handle(file.Fd()), windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, &windows.Overlapped{})
}
