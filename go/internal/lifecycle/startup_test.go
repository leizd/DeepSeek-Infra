package lifecycle

import (
	"context"
	"errors"
	"net"
	"net/http"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestStartupWaitDoesNotPreemptLiveWriterAndHonoursCancellation(t *testing.T) {
	path := t.TempDir()
	held, err := store.OpenControl(store.OpenOptions{Path: path, Owner: "live-owner"})
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	before := held.Writer()
	cfg := config.Config{Mode: config.ModeShadow, Listen: "127.0.0.1:0", ShadowStoreDir: path, Owner: "waiting-owner"}
	if server, err := StartWithWriterWait(context.Background(), cfg, 40*time.Millisecond); server != nil || !errors.Is(err, store.ErrWriterFenceHeld) || !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("live writer was preempted or timeout hidden", err)
	}
	if held.Writer() != before {
		t.Fatal("live writer fence changed")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := StartWithWriterWait(ctx, cfg, time.Second); !errors.Is(err, context.Canceled) {
		t.Fatal("cancelled startup waited", err)
	}
}

func TestStartupWaitClaimsOnlyAfterReleaseAndServerOutlivesWaitDeadline(t *testing.T) {
	path := t.TempDir()
	held, err := store.OpenControl(store.OpenOptions{Path: path, Owner: "first"})
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	type result struct {
		server *Server
		err    error
	}
	done := make(chan result, 1)
	go func() {
		s, e := StartWithWriterWait(ctx, config.Config{Mode: config.ModeShadow, Listen: "127.0.0.1:0", ShadowStoreDir: path, Owner: "second"}, 350*time.Millisecond)
		done <- result{s, e}
	}()
	select {
	case r := <-done:
		t.Fatal("writer returned before release", r.err)
	case <-time.After(30 * time.Millisecond):
	}
	if err := held.Close(); err != nil {
		t.Fatal(err)
	}
	var server *Server
	select {
	case r := <-done:
		if r.err != nil {
			t.Fatal(r.err)
		}
		server = r.server
	case <-time.After(2 * time.Second):
		t.Fatal("released writer was not claimed")
	}
	time.Sleep(400 * time.Millisecond)
	response, err := http.Get("http://" + server.Addr() + "/healthz")
	if err != nil {
		t.Fatal("startup deadline stopped the running server", err)
	}
	response.Body.Close()
	if response.StatusCode != 200 {
		t.Fatal(response.StatusCode)
	}
	cancel()
	if err := awaitStopped(t, server); err != nil {
		t.Fatal(err)
	}
	successor, err := store.OpenControl(store.OpenOptions{Path: path, Owner: "inspect-after-stop"})
	if err != nil {
		t.Fatal(err)
	}
	defer successor.Close()
	if successor.Writer().FencingToken != 3 {
		t.Fatal("writer history was reset")
	}
}

func TestStartupWaitReturnsNonLeaseErrorsImmediately(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	if server, err := StartWithWriterWait(context.Background(), config.Config{Mode: config.ModeShadow, Listen: listener.Addr().String()}, time.Second); server != nil || err == nil || errors.Is(err, store.ErrWriterFenceHeld) {
		t.Fatal("nonlease startup error was hidden", err)
	}
	if _, err := StartWithWriterWait(context.Background(), config.Config{}, 0); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("zero startup budget ignored", err)
	}
}
