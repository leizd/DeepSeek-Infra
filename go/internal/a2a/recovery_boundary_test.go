package a2a

import (
	"net/url"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/sqlitedb"
)

func TestRestartRecoveryWriteFailurePreservesOriginalTaskAndRetries(t *testing.T) {
	s := openTestStore(t, t.TempDir(), time.Now)
	task, err := s.Submit("reasoner", "recovery-context", input())
	if err != nil {
		t.Fatal(err)
	}
	before, err := task.PublicJSON()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.db.Exec("CREATE TEMP TRIGGER a2a_recovery_failure BEFORE UPDATE ON tasks BEGIN SELECT RAISE(ABORT,'durable recovery failure'); END"); err != nil {
		t.Fatal(err)
	}
	err = s.initialize()
	if err == nil || !strings.Contains(err.Error(), "durable recovery failure") {
		t.Fatalf("failed recovery returned success or a different boundary: %v", err)
	}
	if _, err := s.db.Exec("DROP TRIGGER a2a_recovery_failure"); err != nil {
		t.Fatal(err)
	}
	retained, err := s.Get(task.ID)
	if err != nil {
		t.Fatal(err)
	}
	after, err := retained.PublicJSON()
	if err != nil || string(after) != string(before) || retained.Revision != task.Revision {
		t.Fatalf("failed startup rewrite changed retained task: %v", err)
	}
	if err := s.initialize(); err != nil {
		t.Fatalf("rollback poisoned startup recovery: %v", err)
	}
	recovered, err := s.Get(task.ID)
	if err != nil || recovered.State != "failed" || recovered.TokenDigest != "" || recovered.Revision != task.Revision+1 {
		t.Fatalf("recovery lost its durable result: %+v %v", recovered, err)
	}
}

func TestClaimShutdownAfterReadCannotPersistAnExecutionToken(t *testing.T) {
	root := t.TempDir()
	armed := false
	var s *Store
	now := func() time.Time {
		if armed {
			armed = false
			if err := s.db.Close(); err != nil {
				t.Fatal(err)
			}
		}
		return time.Date(2026, 10, 12, 0, 0, 0, 0, time.UTC)
	}
	s = openTestStore(t, root, now)
	task, err := s.Submit("reasoner", "shutdown-context", input())
	if err != nil {
		t.Fatal(err)
	}
	armed = true
	claim, err := s.Claim(task.ID)
	if err == nil || claim != nil || !strings.Contains(err.Error(), "database is closed") {
		t.Fatal("shutdown after task read returned execution authority")
	}
	dsn := (&url.URL{Scheme: "file", Path: fileURLPath(filepath.ToSlash(filepath.Join(root, "a2a.sqlite3"))), RawQuery: "mode=ro"}).String()
	readOnly, err := sqlitedb.Open(dsn)
	if err != nil {
		t.Fatal(err)
	}
	defer readOnly.Close()
	retained, err := readTask(readOnly.QueryRow("SELECT id,revision,state,document FROM tasks WHERE id=?", task.ID))
	if err != nil || retained.State != "submitted" || retained.TokenDigest != "" || retained.Epoch != task.Epoch || retained.Revision != task.Revision {
		t.Fatalf("failed claim left partial authority: %+v %v", retained, err)
	}
}

func TestTaskListingReadFailureNeverReturnsAPartialList(t *testing.T) {
	s := openTestStore(t, t.TempDir(), time.Now)
	task, err := s.Submit("reasoner", "listing-context", input())
	if err != nil {
		t.Fatal(err)
	}
	// A valid first row and a failing later expression model a read that loses
	// its result midstream. Main rows, their schema and bytes remain intact.
	view := `CREATE TEMP VIEW tasks AS SELECT rowid,id,revision,state,document FROM main.tasks
	 UNION ALL SELECT rowid+100000,json_extract('invalid-json','$'),revision,state,document FROM main.tasks`
	if _, err := s.db.Exec(view); err != nil {
		t.Fatal(err)
	}
	partial, err := s.List(20)
	if err == nil || len(partial) != 0 {
		t.Fatalf("failed list exposed partial durable state: %v", err)
	}
	if _, err := s.db.Exec("DROP VIEW temp.tasks"); err != nil {
		t.Fatal(err)
	}
	complete, err := s.List(20)
	if err != nil || len(complete) != 1 || complete[0].ID != task.ID || complete[0].Revision != task.Revision {
		t.Fatalf("read failure damaged retained list: %v", err)
	}
}
