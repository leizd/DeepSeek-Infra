//go:build linux && (android || deepseek_android) && sqlite3_flock

package main

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/sqlitedb"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestPersistedProcessLeaseObservesTemporaryAndroidContentionWithoutMutation(t *testing.T) {
	root := t.TempDir()
	control, err := store.OpenControl(store.OpenOptions{Path: root, Owner: "lease-observer-contention"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = control.Close() })
	before := control.Writer()
	blocker, err := sqlitedb.Open(filepath.Join(root, store.ControlDatabaseFilename))
	if err != nil {
		t.Fatal(err)
	}
	blocker.SetMaxOpenConns(1)
	blocker.SetMaxIdleConns(1)
	t.Cleanup(func() { _ = blocker.Close() })
	if _, err := blocker.Exec("BEGIN IMMEDIATE"); err != nil {
		t.Fatal("could not hold a real Android BSD SQLite writer transaction", err)
	}
	finished := make(chan struct{})
	var rollbackErr error
	go func() {
		time.Sleep(200 * time.Millisecond)
		_, rollbackErr = blocker.Exec("ROLLBACK")
		close(finished)
	}()
	t.Cleanup(func() { <-finished })
	started := time.Now()
	token, until := persistedProcessLease(t, root)
	if elapsed := time.Since(started); elapsed < 150*time.Millisecond {
		t.Fatalf("observer bypassed the actual writer lock: %v", elapsed)
	}
	<-finished
	if rollbackErr != nil {
		t.Fatal(rollbackErr)
	}
	if token != before.FencingToken || until != before.LeaseUntil || control.Writer() != before {
		t.Fatal("read-only contention observation changed the durable writer fence or lease")
	}
}
