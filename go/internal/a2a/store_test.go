package a2a

import (
	"encoding/json"
	"errors"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func input() json.RawMessage {
	return json.RawMessage(`{"role":"user","parts":[{"kind":"text","text":"hello"}],"metadata":{"keep":true}}`)
}

func openTestStore(t *testing.T, root string, now func() time.Time) *Store {
	t.Helper()
	s, err := Open(root, now, 30*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func TestDurableTerminalTasksAndChunksSurviveReopen(t *testing.T) {
	root := filepath.Join(t.TempDir(), "go-control")
	s := openTestStore(t, root, time.Now)
	task, err := s.Submit("reasoner", "ctx-kept", input())
	if err != nil {
		t.Fatal(err)
	}
	claim, err := s.Claim(task.ID)
	if err != nil {
		t.Fatal(err)
	}
	done, err := s.Finish(task.ID, claim.Token, claim.Epoch, "answer", "")
	if err != nil {
		t.Fatal(err)
	}
	if done.State != "completed" || len(done.Chunks) != 2 {
		t.Fatalf("%+v", done)
	}
	before, _ := done.PublicJSON()
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	reopened := openTestStore(t, root, time.Now)
	recovered, err := reopened.Get(task.ID)
	if err != nil {
		t.Fatal(err)
	}
	after, _ := recovered.PublicJSON()
	if string(before) != string(after) {
		t.Fatalf("reopen changed result: %s -> %s", before, after)
	}
	if recovered.ContextID != "ctx-kept" {
		t.Fatal("lost context")
	}
}

func TestRestartFailsUnfinishedTaskWithoutRetryingIt(t *testing.T) {
	root := filepath.Join(t.TempDir(), "go-control")
	s := openTestStore(t, root, time.Now)
	task, _ := s.Submit("reasoner", "", input())
	claim, _ := s.Claim(task.ID)
	_ = s.Close()
	reopened := openTestStore(t, root, time.Now)
	recovered, err := reopened.Get(task.ID)
	if err != nil {
		t.Fatal(err)
	}
	if recovered.State != "failed" || len(recovered.Chunks) != 1 || recovered.Error != RestartMessage {
		t.Fatalf("%+v", recovered)
	}
	if _, err := reopened.Finish(task.ID, claim.Token, claim.Epoch, "late", ""); !errors.Is(err, ErrStaleExecution) {
		t.Fatalf("stale finish: %v", err)
	}
}

func TestCancelAndFinishAreSerializedAndNeverPublishLateAnswer(t *testing.T) {
	s := openTestStore(t, filepath.Join(t.TempDir(), "go-control"), time.Now)
	task, _ := s.Submit("reasoner", "", input())
	claim, _ := s.Claim(task.ID)
	if _, err := s.Cancel(task.ID); err != nil {
		t.Fatal(err)
	}
	done, err := s.Finish(task.ID, claim.Token, claim.Epoch, "must not publish", "")
	if err != nil {
		t.Fatal(err)
	}
	if done.State != "canceled" || len(done.Chunks) != 1 || len(done.Artifacts) != 0 {
		t.Fatalf("%+v", done)
	}
	if _, err := s.Cancel(task.ID); !errors.Is(err, ErrNotCancelable) {
		t.Fatal(err)
	}
}

func TestExpiredExecutorFailsClosedAndCannotRenewOrFinish(t *testing.T) {
	now := time.Unix(1000, 0)
	s := openTestStore(t, filepath.Join(t.TempDir(), "go-control"), func() time.Time { return now })
	task, _ := s.Submit("reasoner", "", input())
	claim, _ := s.Claim(task.ID)
	now = now.Add(20 * time.Second)
	if _, err := s.Renew(task.ID, claim.Token, claim.Epoch); err != nil {
		t.Fatal(err)
	}
	now = now.Add(31 * time.Second)
	if _, err := s.Renew(task.ID, claim.Token, claim.Epoch); !errors.Is(err, ErrStaleExecution) {
		t.Fatal(err)
	}
	task, err := s.Get(task.ID)
	if err != nil || task.State != "failed" {
		t.Fatalf("%+v %v", task, err)
	}
	if _, err := s.Finish(task.ID, claim.Token, claim.Epoch, "late", ""); !errors.Is(err, ErrStaleExecution) {
		t.Fatal(err)
	}
}

func TestOnlyOneWriterAndOneExecutorCanClaim(t *testing.T) {
	root := filepath.Join(t.TempDir(), "go-control")
	s := openTestStore(t, root, time.Now)
	if other, err := Open(root, time.Now, time.Minute); err == nil {
		_ = other.Close()
		t.Fatal("second writer admitted")
	}
	task, _ := s.Submit("reasoner", "", input())
	var wg sync.WaitGroup
	results := make(chan error, 2)
	for range 2 {
		wg.Add(1)
		go func() { defer wg.Done(); _, err := s.Claim(task.ID); results <- err }()
	}
	wg.Wait()
	close(results)
	successes := 0
	for err := range results {
		if err == nil {
			successes++
		} else if !errors.Is(err, ErrStaleExecution) {
			t.Fatal(err)
		}
	}
	if successes != 1 {
		t.Fatalf("claims: %d", successes)
	}
}

func TestInvalidInputsAndPythonStoreAreRejected(t *testing.T) {
	if s, err := Open(filepath.Join(t.TempDir(), ".a2a"), time.Now, time.Minute); err == nil {
		_ = s.Close()
		t.Fatal("Python store accepted")
	}
	s := openTestStore(t, filepath.Join(t.TempDir(), "go-control"), time.Now)
	for _, raw := range []json.RawMessage{nil, json.RawMessage(`[]`), json.RawMessage(`{}`), json.RawMessage(`{"parts":[]}`)} {
		if _, err := s.Submit("reasoner", "", raw); !errors.Is(err, ErrInvalidTask) {
			t.Fatalf("%s: %v", raw, err)
		}
	}
	if _, err := s.Submit("unknown", "", input()); !errors.Is(err, ErrInvalidTask) {
		t.Fatal(err)
	}
	if _, err := s.Get("../escape"); !errors.Is(err, ErrTaskNotFound) {
		t.Fatal(err)
	}
}

func TestSubmissionIdentityIsDurablyIdempotentAndCannotRebind(t *testing.T) {
	s := openTestStore(t, filepath.Join(t.TempDir(), "go-control"), time.Now)
	id := "task_0123456789abcdef01234567"
	first, err := s.Propose(id, "reasoner", "context", input())
	if err != nil {
		t.Fatal(err)
	}
	retry, err := s.Propose(id, "reasoner", "context", input())
	if err != nil || retry.ID != first.ID || retry.Revision != first.Revision || retry.Epoch != 1 {
		t.Fatalf("%+v %v", retry, err)
	}
	if _, err := s.Propose(id, "coder", "context", input()); !errors.Is(err, ErrInvalidTask) {
		t.Fatal(err)
	}
	if _, err := s.Propose("../outside", "reasoner", "context", input()); !errors.Is(err, ErrInvalidTask) {
		t.Fatal(err)
	}
}

func TestFileURLPathAppliesTheDriveLetterRuleOnEveryHost(t *testing.T) {
	for _, testCase := range []struct {
		name string
		path string
		want string
	}{
		{"drive letter gets a leading slash", "C:/state/a2a.sqlite3", "/C:/state/a2a.sqlite3"},
		{"bare drive", "c:", "/c:"},
		{"posix path is untouched", "/var/lib/a2a.sqlite3", "/var/lib/a2a.sqlite3"},
		{"relative path is untouched", "state/a2a.sqlite3", "state/a2a.sqlite3"},
		{"colon that is not the second character", "ab:c", "ab:c"},
		{"single character", "x", "x"},
		{"empty", "", ""},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			if got := fileURLPath(testCase.path); got != testCase.want {
				t.Fatalf("fileURLPath(%q) = %q, want %q", testCase.path, got, testCase.want)
			}
		})
	}
}
