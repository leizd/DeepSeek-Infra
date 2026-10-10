package a2a

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"github.com/leizd/DeepSeek-Infra/go/internal/sqlitedb"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestDamagedLaterSQLitePageRollsBackAllRecoveryAndReleasesWriter(t *testing.T) {
	root := t.TempDir()
	s := openTestStore(t, root, time.Now)
	first, err := s.Submit("reasoner", "", input())
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 80; i++ {
		if _, err = s.Submit("reasoner", "", input()); err != nil {
			t.Fatal(err)
		}
	}
	var rootPage, pageSize int64
	if err = s.db.QueryRow("PRAGMA page_size").Scan(&pageSize); err != nil {
		t.Fatal(err)
	}
	if err = s.db.QueryRow("SELECT rootpage FROM sqlite_schema WHERE name='tasks'").Scan(&rootPage); err != nil {
		t.Fatal(err)
	}
	if err = s.Close(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "a2a.sqlite3")
	page := lastTableLeafPage(t, path, rootPage, pageSize)
	if page <= 1 || page == rootPage {
		t.Fatal("fixture did not span multiple data pages")
	}
	file, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	offset := (page - 1) * pageSize
	original := make([]byte, 1)
	if _, err = file.ReadAt(original, offset); err != nil {
		t.Fatal(err)
	}
	// Damage an actual later B-tree page while keeping the schema and preceding
	// rows intact. Recovery must not publish their new status before reading all rows.
	if _, err = file.WriteAt([]byte{0xff}, offset); err != nil {
		t.Fatal(err)
	}
	if err = file.Close(); err != nil {
		t.Fatal(err)
	}
	if reopened, err := Open(root, time.Now, time.Minute); err == nil {
		_ = reopened.Close()
		t.Fatal("damaged data page accepted")
	}
	db, err := sqlitedb.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	var state string
	if err = db.QueryRow("SELECT state FROM tasks WHERE id=?", first.ID).Scan(&state); err != nil || state != "submitted" {
		t.Fatalf("partial recovery reached disk: %s %v", state, err)
	}
	_ = db.Close()
	file, err = os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = file.WriteAt(original, offset); err != nil {
		t.Fatal(err)
	}
	if err = file.Close(); err != nil {
		t.Fatal(err)
	}
	s = openTestStore(t, root, time.Now)
	tasks, err := s.List(200)
	if err != nil || len(tasks) != 81 {
		t.Fatalf("repaired store cannot reopen: %d %v", len(tasks), err)
	}
	for _, task := range tasks {
		if task.State != "failed" || task.Error != RestartMessage {
			t.Fatalf("recovery did not resume: %+v", task)
		}
	}
}

// Walk the actual table B-tree after checkpoint/close. This preserves fault
// injection on a later data page without requiring an engine's optional dbstat
// extension. Layout: https://sqlite.org/fileformat2.html#b_tree_pages
func lastTableLeafPage(t *testing.T, path string, page, pageSize int64) int64 {
	t.Helper()
	file, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		t.Fatal(err)
	}
	visited := map[int64]bool{}
	for {
		if page <= 1 || pageSize < 512 || page > info.Size()/pageSize || visited[page] {
			t.Fatalf("invalid fixture B-tree page: %d", page)
		}
		visited[page] = true
		var header [12]byte
		if _, err := file.ReadAt(header[:], (page-1)*pageSize); err != nil {
			t.Fatal(err)
		}
		switch header[0] {
		case 0x0d:
			if binary.BigEndian.Uint16(header[3:5]) == 0 {
				t.Fatal("fixture leaf has no records to damage")
			}
			return page
		case 0x05:
			page = int64(binary.BigEndian.Uint32(header[8:12]))
		default:
			t.Fatalf("unexpected fixture table page type: %x", header[0])
		}
	}
}

func TestExternalSQLiteLocksCannotPartiallyInitializeOrRecover(t *testing.T) {
	for _, existing := range []bool{false, true} {
		t.Run(map[bool]string{false: "journal-switch", true: "recovery"}[existing], func(t *testing.T) {
			root := t.TempDir()
			var taskID string
			if existing {
				s := openTestStore(t, root, time.Now)
				task, err := s.Submit("reasoner", "", input())
				if err != nil {
					t.Fatal(err)
				}
				taskID = task.ID
				_ = s.Close()
			}
			db, err := sqlitedb.Open(filepath.Join(root, "a2a.sqlite3"))
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			db.SetMaxOpenConns(1)
			if !existing {
				if _, err = db.Exec("PRAGMA user_version=0"); err != nil {
					t.Fatal(err)
				}
			}
			// Existing stores retain their version marker; the competing
			// process only holds a writer reservation, never changes task rows.
			begin := "BEGIN"
			if existing {
				begin = "BEGIN IMMEDIATE"
			}
			if _, err = db.Exec(begin); err != nil {
				t.Fatal(err)
			}
			var count int
			if err = db.QueryRow("SELECT count(*) FROM sqlite_schema").Scan(&count); err != nil {
				t.Fatal(err)
			}
			if s, err := Open(root, time.Now, time.Minute); err == nil {
				_ = s.Close()
				t.Fatal("competing lock did not block initialization")
			}
			if _, err = db.Exec("ROLLBACK"); err != nil {
				t.Fatal(err)
			}
			if existing {
				var state string
				if err = db.QueryRow("SELECT state FROM tasks WHERE id=?", taskID).Scan(&state); err != nil || state != "submitted" {
					t.Fatalf("partial recovery: %s %v", state, err)
				}
			}
			_ = db.Close()
			s := openTestStore(t, root, time.Now)
			if existing {
				task, err := s.Get(taskID)
				if err != nil || task.State != "failed" {
					t.Fatalf("recovery did not resume: %+v %v", task, err)
				}
			}
		})
	}
}

func TestBrokenDatabaseAndNonDirectoryStoreFailWithoutLeakingWriterLock(t *testing.T) {
	root := t.TempDir()
	file := filepath.Join(root, "not-a-directory")
	if err := os.WriteFile(file, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	if s, err := Open(filepath.Join(file, "nested"), time.Now, time.Minute); err == nil {
		_ = s.Close()
		t.Fatal("file accepted as directory")
	}
	db := filepath.Join(root, "a2a.sqlite3")
	if err := os.WriteFile(db, []byte("not a SQLite database"), 0o600); err != nil {
		t.Fatal(err)
	}
	if s, err := Open(root, time.Now, time.Minute); err == nil {
		_ = s.Close()
		t.Fatal("damaged database accepted")
	}
	if err := os.Remove(db); err != nil {
		t.Fatal(err)
	}
	// Open must have released the OS lock on the preceding initialization error.
	s := openTestStore(t, root, time.Now)
	if _, err := s.Submit("reasoner", "", input()); err != nil {
		t.Fatal(err)
	}
}

func TestExpiryAndQueuedCancellationStayUncommittedWhenStorageFails(t *testing.T) {
	now := time.Unix(1000, 0)
	s := openTestStore(t, t.TempDir(), func() time.Time { return now })
	running, _ := s.Submit("reasoner", "", input())
	claim, _ := s.Claim(running.ID)
	queued, _ := s.Submit("reasoner", "", input())
	_, _ = s.Cancel(queued.ID)
	if _, err := s.db.Exec("PRAGMA query_only=ON"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Claim(queued.ID); err == nil {
		t.Fatal("uncommitted cancellation acknowledged")
	}
	now = now.Add(31 * time.Second)
	if _, err := s.Get(running.ID); err == nil {
		t.Fatal("uncommitted expiry acknowledged")
	}
	var state string
	var revision uint64
	if err := s.db.QueryRow("SELECT state,revision FROM tasks WHERE id=?", running.ID).Scan(&state, &revision); err != nil || state != "working" || revision != claim.Revision {
		t.Fatalf("partial expiry: %s %d %v", state, revision, err)
	}
	if _, err := s.db.Exec("PRAGMA query_only=OFF"); err != nil {
		t.Fatal(err)
	}
	if task, err := s.Get(running.ID); err != nil || task.State != "failed" {
		t.Fatalf("expiry recovery: %+v %v", task, err)
	}
	if task, err := s.Get(queued.ID); err != nil || task.State != "canceled" {
		t.Fatalf("cancel recovery: %+v %v", task, err)
	}
}

func TestReadPathsNeverReturnPartialResultsFromDamagedRecords(t *testing.T) {
	for _, tc := range []struct{ name, sql string }{
		{"malformed-json", "UPDATE tasks SET document='broken-json'"},
		{"null-identity", "UPDATE tasks SET id=NULL"},
		{"negative-revision", "UPDATE tasks SET revision=-1"},
		{"unknown-state", "UPDATE tasks SET state='unknown', document=json_set(document,'$.state','unknown')"},
		{"bad-epoch", "UPDATE tasks SET document=json_set(document,'$.executionEpoch',2)"},
		{"short-token", "UPDATE tasks SET document=json_set(document,'$.tokenDigest','short')"},
		{"missing-table", "ALTER TABLE tasks RENAME TO damaged_tasks"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := openTestStore(t, t.TempDir(), time.Now)
			task, _ := s.Submit("reasoner", "", input())
			_, _ = s.Claim(task.ID)
			if _, err := s.db.Exec(tc.sql); err != nil {
				t.Fatal(err)
			}
			if tasks, err := s.List(200); err == nil || tasks != nil {
				t.Fatalf("partial damaged listing: %+v %v", tasks, err)
			}
			if tc.name != "null-identity" {
				if _, err := s.Propose(task.ID, "reasoner", "", input()); err == nil {
					t.Fatal("rebound damaged proposal")
				}
			}
		})
	}
}

func TestBoundedPublicHistoryAndInvalidEncodingCannotReplaceDurableState(t *testing.T) {
	s := openTestStore(t, t.TempDir(), time.Now)
	task, _ := s.Submit("reasoner", "", input())
	for i := 0; i < 25; i++ {
		task.History = append(task.History, textMessage(strings.Repeat("x", i)))
	}
	raw, err := task.PublicJSON()
	if err != nil {
		t.Fatal(err)
	}
	var public struct {
		History []json.RawMessage `json:"history"`
	}
	if err = json.Unmarshal(raw, &public); err != nil || len(public.History) != 20 {
		t.Fatalf("unbounded public history: %d %v", len(public.History), err)
	}
	task.History = []json.RawMessage{json.RawMessage("invalid-json")}
	if err = s.save(task); err == nil {
		t.Fatal("invalid record replaced valid data")
	}
	after, err := s.Get(task.ID)
	if err != nil || after.Revision != 1 || len(after.History) != 1 {
		t.Fatalf("partial invalid encoding: %+v %v", after, err)
	}
	if _, err = s.Propose("task_zzzzzzzzzzzzzzzzzzzzzzzz", "reasoner", "", input()); !errors.Is(err, ErrInvalidTask) {
		t.Fatal(err)
	}
}
