//go:build linux

package store

import (
	"os"
	"path/filepath"
	"testing"
)

func TestControlBirthRefusesTheNoncreatableOwnProcDirectory(t *testing.T) {
	control, err := OpenControl(OpenOptions{Path: "/proc/self", Owner: "invalid-kernel-directory"})
	if err == nil {
		_ = control.Close()
		t.Fatal("kernel process metadata was admitted as a control store")
	}
	if _, err := os.Stat(filepath.Join("/proc/self", ControlDatabaseFilename)); !os.IsNotExist(err) {
		t.Fatal("failed birth left a database in process metadata:", err)
	}
}
