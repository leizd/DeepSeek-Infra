package store

import (
	"os"
	"path/filepath"
	"testing"
)

// OpenControl must refuse a store it cannot prove it owns. Each branch here is a
// distinct reason to refuse, and every one was uncovered: a wrong acceptance
// would let a control plane adopt a foreign or truncated database.

func TestControlOpenRefusesUnusablePaths(t *testing.T) {
	root := t.TempDir()

	// A path that is a regular file cannot become a data directory.
	file := filepath.Join(root, "not-a-dir")
	if err := os.WriteFile(file, []byte("x"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := OpenControl(OpenOptions{Path: file, Owner: "boundary-owner"}); err == nil {
		t.Fatal("a regular file was accepted as a data directory")
	}

	// A missing parent cannot be created without permission.
	if _, err := OpenControl(OpenOptions{Path: filepath.Join(file, "child"), Owner: "boundary-owner"}); err == nil {
		t.Fatal("a path under a regular file was accepted")
	}

	// An empty owner is not a writer identity.
	if _, err := OpenControl(OpenOptions{Path: filepath.Join(root, "empty-owner")}); err == nil {
		t.Fatal("a store opened without an owner")
	}
}

func TestControlOpenRefusesAForeignOrTruncatedDatabase(t *testing.T) {
	// An absent file is simply a new store, so only text that claims to be data
	// but cannot be parsed is a refusal.
	for name, body := range map[string][]byte{
		"garbage":        []byte("this is not a sqlite database, but it is long enough"),
		"foreign-header": append([]byte("SQLite format 3\x00"), make([]byte, 4096)...),
	} {
		t.Run(name, func(t *testing.T) {
			dir := filepath.Join(t.TempDir(), "data")
			if err := os.MkdirAll(dir, 0o700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(dir, "control.sqlite3"), body, 0o600); err != nil {
				t.Fatal(err)
			}
			control, err := OpenControl(OpenOptions{Path: dir, Owner: "boundary-owner"})
			if err == nil {
				_ = control.Close()
				t.Fatalf("%s was accepted as a control store", name)
			}
			if err.Error() == "" {
				t.Fatalf("%s produced an empty refusal", name)
			}
		})
	}
}

func TestControlOpenReopensAStoreForTheSameOwner(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "data")
	first, err := OpenControl(OpenOptions{Path: dir, Owner: "reopen-owner"})
	if err != nil {
		t.Fatal(err)
	}
	if err := first.Close(); err != nil {
		t.Fatal(err)
	}
	// Reopening the same directory is the normal restart path and must work.
	second, err := OpenControl(OpenOptions{Path: dir, Owner: "reopen-owner"})
	if err != nil {
		t.Fatalf("a same-owner reopen was refused: %v", err)
	}
	if err := second.Close(); err != nil {
		t.Fatal(err)
	}
}
