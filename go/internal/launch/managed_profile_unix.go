//go:build !windows

package launch

import (
	"golang.org/x/sys/unix"
	"os"
)

func lockManagedProfile(file *os.File) error {
	return unix.Flock(int(file.Fd()), unix.LOCK_EX|unix.LOCK_NB)
}
func secureManagedDirectory(path string) error {
	if err := os.MkdirAll(path, 0o700); err != nil {
		return err
	}
	return os.Chmod(path, 0o700)
}
