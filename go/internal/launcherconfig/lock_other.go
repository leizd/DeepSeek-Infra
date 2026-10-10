//go:build !windows

package launcherconfig

import (
	"golang.org/x/sys/unix"
	"os"
)

func lockWriter(file *os.File) error { return unix.Flock(int(file.Fd()), unix.LOCK_EX|unix.LOCK_NB) }
