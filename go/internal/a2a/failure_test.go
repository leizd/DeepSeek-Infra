package a2a

import (
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestCancellationBeforeClaimAndDuringExecution(t *testing.T) {
	now := time.Unix(1000, 0)
	s := openTestStore(t, t.TempDir(), func() time.Time { return now })
	queued, _ := s.Submit("reasoner", "", input())
	first, err := s.Cancel(queued.ID)
	if err != nil {
		t.Fatal(err)
	}
	now = now.Add(time.Second)
	again, err := s.Cancel(queued.ID)
	if err != nil || again.Revision != first.Revision || again.CancelRequestedAt != first.CancelRequestedAt {
		t.Fatalf("cancel changed: %+v %v", again, err)
	}
	if _, err = s.Claim(queued.ID); !errors.Is(err, ErrStaleExecution) {
		t.Fatal(err)
	}
	queued, _ = s.Get(queued.ID)
	if queued.State != "canceled" || len(queued.Chunks) != 0 {
		t.Fatalf("%+v", queued)
	}
	running, _ := s.Submit("reasoner", "", input())
	claim, _ := s.Claim(running.ID)
	_, _ = s.Cancel(running.ID)
	if _, err = s.Claim(running.ID); !errors.Is(err, ErrStaleExecution) {
		t.Fatal(err)
	}
	if task, err := s.Renew(running.ID, claim.Token, claim.Epoch); err != nil || task.State != "canceling" {
		t.Fatalf("%+v %v", task, err)
	}
	now = now.Add(31 * time.Second)
	list, err := s.List(0)
	if err != nil || len(list) != 1 || list[0].State != "canceled" {
		t.Fatalf("%+v %v", list, err)
	}
}

func TestStorageWriteFailureDoesNotInstallAnyTransition(t *testing.T) {
	s := openTestStore(t, t.TempDir(), time.Now)
	queued, _ := s.Submit("reasoner", "", input())
	running, _ := s.Submit("reasoner", "", input())
	claim, _ := s.Claim(running.ID)
	if _, err := s.db.Exec("PRAGMA query_only=ON"); err != nil {
		t.Fatal(err)
	}
	operations := []func() error{
		func() error { _, err := s.Submit("reasoner", "", input()); return err },
		func() error { _, err := s.Claim(queued.ID); return err },
		func() error { _, err := s.Cancel(running.ID); return err },
		func() error { _, err := s.Renew(running.ID, claim.Token, claim.Epoch); return err },
		func() error { _, err := s.Finish(running.ID, claim.Token, claim.Epoch, "discard", ""); return err },
	}
	for _, operation := range operations {
		if operation() == nil {
			t.Fatal("read-only store accepted mutation")
		}
	}
	if _, err := s.db.Exec("PRAGMA query_only=OFF"); err != nil {
		t.Fatal(err)
	}
	after, err := s.Get(running.ID)
	if err != nil || after.State != "working" || after.Revision != claim.Revision || len(after.Chunks) != 1 {
		t.Fatalf("%+v %v", after, err)
	}
	if task, _ := s.Get(queued.ID); task.State != "submitted" {
		t.Fatalf("%+v", task)
	}
	if list, _ := s.List(500); len(list) != 2 {
		t.Fatalf("leaked insertion: %d", len(list))
	}
	if _, err := s.Finish(running.ID, claim.Token, claim.Epoch, "", "upstream failed"); err != nil {
		t.Fatal(err)
	}
	if err := s.save(claim.Task); !errors.Is(err, ErrStaleExecution) {
		t.Fatalf("stale snapshot replaced terminal record: %v", err)
	}
}

func TestOversizedResultRollsBackAndNeverPartiallyPublishes(t *testing.T) {
	s := openTestStore(t, t.TempDir(), time.Now)
	task, _ := s.Submit("reasoner", "", input())
	claim, _ := s.Claim(task.ID)
	if _, err := s.Finish(task.ID, claim.Token, claim.Epoch, strings.Repeat("x", maxDocumentBytes), ""); !errors.Is(err, ErrInvalidTask) {
		t.Fatal(err)
	}
	after, _ := s.Get(task.ID)
	if after.State != "working" || len(after.Chunks) != 1 || after.Revision != claim.Revision {
		t.Fatalf("%+v", after)
	}
	if _, err := s.Finish(task.ID, claim.Token, claim.Epoch, " ", ""); err != nil {
		t.Fatal(err)
	}
	after, _ = s.Get(task.ID)
	if after.Artifacts[0].Parts[0]["text"] != "(empty response)" {
		t.Fatal(after.Artifacts)
	}
}

func TestCorruptTaskMakesRecoveryAtomicAndFailsClosed(t *testing.T) {
	for name, corrupt := range map[string]func(*Task){
		"epoch":    func(task *Task) { task.Epoch++ },
		"identity": func(task *Task) { task.ID = "task_wrong" },
		"digest":   func(task *Task) { task.SubmissionDigest = strings.Repeat("z", 64) },
		"token":    func(task *Task) { task.TokenDigest = "" },
		"chunks":   func(task *Task) { task.Chunks[0].TaskID = "different-task" },
		"state":    func(task *Task) { task.State = "unknown" },
		"history":  func(task *Task) { task.History = nil },
	} {
		t.Run(name, func(t *testing.T) {
			root := t.TempDir()
			s := openTestStore(t, root, time.Now)
			good, _ := s.Submit("reasoner", "", input())
			bad, _ := s.Submit("reasoner", "", input())
			claim, _ := s.Claim(bad.ID)
			corrupt(claim.Task)
			raw, _ := json.Marshal(claim.Task)
			if _, err := s.db.Exec("UPDATE tasks SET document=? WHERE id=?", raw, bad.ID); err != nil {
				t.Fatal(err)
			}
			_ = s.Close()
			if reopened, err := Open(root, time.Now, time.Minute); !errors.Is(err, ErrCorruptTask) {
				if reopened != nil {
					_ = reopened.Close()
				}
				t.Fatalf("corrupt store accepted: %v", err)
			}
			db, err := sql.Open("sqlite", filepath.Join(root, "a2a.sqlite3"))
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			var state string
			if err = db.QueryRow("SELECT state FROM tasks WHERE id=?", good.ID).Scan(&state); err != nil || state != "submitted" {
				t.Fatalf("partial recovery: %s %v", state, err)
			}
		})
	}
}

func TestForeignSchemaAndFilesystemEntriesRejected(t *testing.T) {
	for _, change := range []string{"PRAGMA application_id=1", "PRAGMA user_version=2", "CREATE TABLE foreign_table(id TEXT)", "ALTER TABLE tasks ADD COLUMN unexpected TEXT"} {
		t.Run(change, func(t *testing.T) {
			root := t.TempDir()
			s := openTestStore(t, root, time.Now)
			if _, err := s.db.Exec(change); err != nil {
				t.Fatal(err)
			}
			_ = s.Close()
			if reopened, err := Open(root, time.Now, time.Minute); err == nil {
				_ = reopened.Close()
				t.Fatal("foreign schema accepted")
			}
		})
	}
	for _, entry := range []string{"a2a.sqlite3", "a2a.writer.lock", "a2a.sqlite3-wal", "a2a.sqlite3-shm"} {
		root := t.TempDir()
		if err := os.Mkdir(filepath.Join(root, entry), 0o700); err != nil {
			t.Fatal(err)
		}
		if s, err := Open(root, time.Now, time.Minute); !errors.Is(err, ErrCorruptTask) {
			if s != nil {
				_ = s.Close()
			}
			t.Fatal(err)
		}
	}
	if s, err := Open(t.TempDir(), nil, time.Minute); !errors.Is(err, ErrInvalidTask) {
		if s != nil {
			_ = s.Close()
		}
		t.Fatal(err)
	}
	for _, lease := range []time.Duration{0, time.Hour} {
		if s, err := Open(t.TempDir(), time.Now, lease); !errors.Is(err, ErrInvalidTask) {
			if s != nil {
				_ = s.Close()
			}
			t.Fatal(err)
		}
	}
}

func TestClosedStoreAndUnknownTasksCannotBeMutated(t *testing.T) {
	s := openTestStore(t, t.TempDir(), time.Now)
	operations := []func() error{
		func() error { _, err := s.Get("missing"); return err },
		func() error { _, err := s.Claim("missing"); return err },
		func() error { _, err := s.Cancel("missing"); return err },
		func() error { _, err := s.Renew("missing", "token", 1); return err },
		func() error { _, err := s.Finish("missing", "token", 1, "", "failure"); return err },
	}
	for _, operation := range operations {
		if !errors.Is(operation(), ErrTaskNotFound) {
			t.Fatal("missing task accepted")
		}
	}
	_ = s.Close()
	operations = append(operations, func() error { _, err := s.Submit("reasoner", "", input()); return err }, func() error { _, err := s.List(20); return err })
	for _, operation := range operations {
		if !errors.Is(operation(), ErrStoreClosed) {
			t.Fatal("closed store accepted")
		}
	}
}
