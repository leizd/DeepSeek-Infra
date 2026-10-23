//go:build linux && (android || deepseek_android) && sqlite3_flock

package main

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
	"golang.org/x/sys/unix"
)

func TestPersistedProcessLeaseObservesTemporaryAndroidContentionWithoutMutation(t *testing.T) {
	root := t.TempDir()
	control, err := store.OpenControl(store.OpenOptions{Path: root, Owner: "lease-observer-contention"})
	if err != nil {
		t.Fatal(err)
	}
	if err := control.Close(); err != nil {
		t.Fatal(err)
	}
	beforeToken, beforeUntil := persistedProcessLease(t, root)
	path := filepath.Join(root, store.ControlDatabaseFilename)
	beforeBytes, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	blocker, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = blocker.Close() })
	// WAL permits readers during BEGIN IMMEDIATE. Hold the actual VFS lock,
	// without changing a SQLite row, to reproduce the reader's BUSY condition.
	if err := unix.Flock(int(blocker.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		t.Fatal("could not acquire the real BSD database lock", err)
	}
	finished := make(chan struct{})
	var unlockErr error
	started := time.Now()
	go func() {
		time.Sleep(200 * time.Millisecond)
		unlockErr = unix.Flock(int(blocker.Fd()), unix.LOCK_UN)
		close(finished)
	}()
	t.Cleanup(func() { <-finished })
	token, until := persistedProcessLease(t, root)
	if elapsed := time.Since(started); elapsed < 150*time.Millisecond {
		t.Fatalf("observer bypassed the actual writer lock: %v", elapsed)
	}
	<-finished
	if unlockErr != nil {
		t.Fatal(unlockErr)
	}
	afterBytes, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if token != beforeToken || until != beforeUntil || !bytes.Equal(beforeBytes, afterBytes) {
		t.Fatal("read-only contention observation changed the durable writer fence or lease")
	}
}
