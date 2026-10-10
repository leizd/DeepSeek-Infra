//go:build !windows

package main

import (
	"syscall"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestTerminationReleasesWriterForImmediateSuccessor(t *testing.T) {
	binary := buildDeepseekd(t)
	root := t.TempDir()
	first := startDeepseekd(t, binary, root, "terminated-owner")
	client := processInternalClient(3 * time.Second)
	defer client.CloseIdleConnections()
	before := processSnapshot(t, client, first.address)
	if err := first.command.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
	select {
	case <-first.done:
		if first.waitErr != nil {
			t.Fatalf("termination did not drain the writer: %v %s", first.waitErr, first.stderr.String())
		}
	case <-time.After(8 * time.Second):
		t.Fatal("termination did not complete within the drain deadline")
	}
	token, leaseUntil := persistedProcessLease(t, root)
	if token != before.Writer.FencingToken || leaseUntil > time.Now().Unix() {
		t.Fatalf("terminated writer still owns a live lease: token=%d until=%d", token, leaseUntil)
	}
	second := startDeepseekd(t, binary, root, "immediate-successor")
	after := processSnapshot(t, client, second.address)
	if after.Writer.OwnerInstanceID != "immediate-successor" || after.Writer.FencingToken != token+1 || after.Digest != before.Digest {
		t.Fatalf("graceful takeover lost writer identity or state: %+v", after)
	}
	if after.Runtime != store.RuntimeGo {
		t.Fatal("successor changed the database owner")
	}
}
