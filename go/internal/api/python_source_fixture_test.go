package api

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// copyPythonSourceFixture materializes a checked-in fenced Python source the way
// the production layout does: the SQLite file under `.backup-control`, with the
// sibling legacy projection directory the export bound next to it. The Go
// source read re-derives that directory's digest, so a copied source that
// arrives without it is refused rather than silently imported.
//
// The empty fixture's export bound an empty directory, so it copies no
// projection bytes; the directory itself still has to exist.
func copyPythonSourceFixture(t *testing.T, sourceName, domain string) string {
	t.Helper()
	source, err := os.ReadFile(filepath.Join("..", "store", "testdata", sourceName))
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	control := filepath.Join(root, ".backup-control")
	if err := os.MkdirAll(control, 0o700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(control, "control.sqlite3")
	if err := os.WriteFile(path, source, 0o600); err != nil {
		t.Fatal(err)
	}
	directory, fixtureName := filepath.Join(root, ".backup-policies"), "python_projection_policies"
	if domain == "target" {
		directory, fixtureName = filepath.Join(root, ".backup-targets"), "python_projection_targets"
	}
	if strings.HasPrefix(filepath.Base(sourceName), "python_empty_") {
		fixtureName = ""
	}
	if err := os.MkdirAll(directory, 0o700); err != nil {
		t.Fatal(err)
	}
	if fixtureName == "" {
		return path
	}
	fixture := filepath.Join("..", "store", "testdata", filepath.Dir(sourceName), fixtureName)
	entries, err := os.ReadDir(fixture)
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		content, err := os.ReadFile(filepath.Join(fixture, entry.Name()))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(directory, entry.Name()), content, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return path
}
