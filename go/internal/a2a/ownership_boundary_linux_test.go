package a2a

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	controlstore "github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestA2AResolvedPythonOwnershipAndWriterPermissionFailuresPreserveData(t *testing.T) {
	t.Run("unsearchable-native-directory", func(t *testing.T) {
		root := t.TempDir()
		if err := os.Chmod(root, 0); err != nil {
			t.Fatal(err)
		}
		candidate, rejected := Open(root, time.Now, 30*time.Second)
		if candidate != nil {
			candidate.Close()
		}
		if err := os.Chmod(root, 0o700); err != nil {
			t.Fatal(err)
		}
		if !errors.Is(rejected, os.ErrPermission) {
			t.Fatalf("inaccessible directory acquired a native writer: %v", rejected)
		}
		entries, err := os.ReadDir(root)
		if err != nil || len(entries) != 0 {
			t.Fatalf("permission refusal wrote native state: %v", err)
		}
		openTestStore(t, root, time.Now)
	})
	t.Run("resolved-python-owner", func(t *testing.T) {
		root := t.TempDir()
		python := filepath.Join(root, ".a2a")
		if err := os.Mkdir(python, 0o700); err != nil {
			t.Fatal(err)
		}
		alias := filepath.Join(root, "native-alias")
		if err := os.Symlink(python, alias); err != nil {
			t.Fatal(err)
		}
		candidate, err := Open(alias, time.Now, 30*time.Second)
		if candidate != nil {
			candidate.Close()
		}
		if !errors.Is(err, controlstore.ErrPythonStorePath) {
			t.Fatalf("resolved legacy owner acquired a native writer: %v", err)
		}
		entries, err := os.ReadDir(python)
		if err != nil || len(entries) != 0 {
			t.Fatalf("ownership denial touched Python state: %v", err)
		}
	})
	t.Run("writer-lock-unreadable", func(t *testing.T) {
		root := t.TempDir()
		s := openTestStore(t, root, time.Now)
		task, err := s.Submit("reasoner", "permission-context", input())
		if err != nil {
			t.Fatal(err)
		}
		claim, err := s.Claim(task.ID)
		if err != nil {
			t.Fatal(err)
		}
		completed, err := s.Finish(task.ID, claim.Token, claim.Epoch, "retained answer", "")
		if err != nil {
			t.Fatal(err)
		}
		before, err := completed.PublicJSON()
		if err != nil {
			t.Fatal(err)
		}
		if err := s.Close(); err != nil {
			t.Fatal(err)
		}
		lock := filepath.Join(root, "a2a.writer.lock")
		if err := os.Chmod(lock, 0); err != nil {
			t.Fatal(err)
		}
		candidate, rejected := Open(root, time.Now, 30*time.Second)
		if candidate != nil {
			candidate.Close()
		}
		if err := os.Chmod(lock, 0o600); err != nil {
			t.Fatal(err)
		}
		if !errors.Is(rejected, os.ErrPermission) {
			t.Fatalf("unreadable writer lock acquired task ownership: %v", rejected)
		}
		candidate = openTestStore(t, root, time.Now)
		retained, err := candidate.Get(task.ID)
		if err != nil {
			t.Fatal(err)
		}
		after, err := retained.PublicJSON()
		if err != nil || string(after) != string(before) || retained.Revision != completed.Revision {
			t.Fatalf("writer failure changed retained result: %v", err)
		}
	})
}
