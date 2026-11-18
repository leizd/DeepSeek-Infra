package store

import (
	"os"
	"path/filepath"
	"testing"
)

func TestControlOpenRefusesADanglingDataDirectory(t *testing.T) {
	root := t.TempDir()
	link := filepath.Join(root, "data")
	if err := os.Symlink(filepath.Join(root, "missing-target"), link); err != nil {
		t.Skipf("symlink unavailable: %v", err)
	}
	if _, err := OpenControl(OpenOptions{Path: link, Owner: "boundary-owner"}); err == nil {
		t.Fatal("a dangling data directory was accepted")
	}
}
