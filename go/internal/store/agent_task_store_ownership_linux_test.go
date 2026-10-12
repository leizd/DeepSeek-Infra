package store

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestAgentStoreRejectsResolvedPythonOwnershipAndUnreadableDatabase(t *testing.T) {
	t.Run("resolved-python-owner", func(t *testing.T) {
		root := t.TempDir()
		python := filepath.Join(root, ".agent-runs")
		if err := os.Mkdir(python, 0o700); err != nil {
			t.Fatal(err)
		}
		alias := filepath.Join(root, "native-alias")
		if err := os.Symlink(python, alias); err != nil {
			t.Fatal(err)
		}
		if control, err := OpenControl(OpenOptions{Path: alias, Owner: "agent-ownership-test"}); err == nil {
			control.Close()
			t.Fatal("native alias acquired a Python-owned store")
		}
		entries, err := os.ReadDir(python)
		if err != nil || len(entries) != 0 {
			t.Fatalf("ownership refusal touched Python storage: %v", err)
		}
	})
	t.Run("unreadable-native-database", func(t *testing.T) {
		root := t.TempDir()
		control, err := OpenControl(OpenOptions{Path: root, Owner: "agent-permission-test"})
		if err != nil {
			t.Fatal(err)
		}
		path := control.databasePath
		if err := control.Close(); err != nil {
			t.Fatal(err)
		}
		before, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(path, 0); err != nil {
			t.Fatal(err)
		}
		candidate, rejected := OpenControl(OpenOptions{Path: root, Owner: "agent-permission-retry"})
		if candidate != nil {
			candidate.Close()
		}
		if err := os.Chmod(path, 0o600); err != nil {
			t.Fatal(err)
		}
		if rejected == nil {
			t.Fatal("unreadable database opened as a new store")
		}
		after, err := os.ReadFile(path)
		if err != nil || string(after) != string(before) {
			t.Fatalf("permission failure changed retained database: %v", err)
		}
		candidate, err = OpenControl(OpenOptions{Path: root, Owner: "agent-permission-recovery"})
		if err != nil {
			t.Fatal(err)
		}
		if err := candidate.Close(); err != nil && !errors.Is(err, ErrWriterFenceHeld) {
			t.Fatal(err)
		}
	})
}
