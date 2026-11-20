package sqlitedb

import (
	"os"
	"path/filepath"
	"testing"
)

func TestControlFileCreationNeverOverwritesAnExistingDatabase(t *testing.T) {
	path := filepath.Join(t.TempDir(), "control.db")
	guard, err := PrepareControlFile(path, false, true)
	if err != nil {
		t.Fatal(err)
	}
	if err := guard.Close(); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("retained database bytes"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := PrepareControlFile(path, false, true); !os.IsExist(err) {
		t.Fatal("existing file was not refused:", err)
	}
	content, err := os.ReadFile(path)
	if err != nil || string(content) != "retained database bytes" {
		t.Fatal("creation refusal changed database bytes", err)
	}
}

func TestControlFileNoncreatingPreparationLeavesMissingFilesAbsent(t *testing.T) {
	path := filepath.Join(t.TempDir(), "missing.db")
	guard, err := PrepareControlFile(path, false, false)
	if err != nil {
		t.Fatal(err)
	}
	if err := guard.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("noncreating preparation created a file:", err)
	}
}
