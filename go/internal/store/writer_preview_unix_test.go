//go:build !windows

package store

import (
	"os"
	"path/filepath"
	"testing"
)

func TestNextWriterFencingTokenCannotCreateRelativeStoreAfterWorkingDirectoryLoss(t *testing.T) {
	root := t.TempDir()
	cwd := filepath.Join(root, "removed-working-directory")
	if err := os.Mkdir(cwd, 0o700); err != nil {
		t.Fatal(err)
	}
	t.Chdir(cwd)
	if err := os.Remove(cwd); err != nil {
		t.Fatal(err)
	}
	if _, err := NextWriterFencingToken("relative-store"); err == nil {
		t.Fatal("lost working directory silently redirected writer prediction")
	}
}
