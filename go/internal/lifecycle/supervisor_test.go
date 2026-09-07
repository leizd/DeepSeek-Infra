package lifecycle

import (
	"bufio"
	"context"
	"database/sql"
	"errors"
	"io"
	"net"
	"net/http"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

func TestStartWaitsForWriterReleaseOnCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	path := t.TempDir()
	runtime, err := Start(ctx, config.Config{Mode: config.ModeShadow, Listen: "127.0.0.1:0", Owner: "first", ShadowStoreDir: path})
	if err != nil {
		t.Fatal(err)
	}
	cancel()
	if err := awaitStopped(t, runtime); err != nil {
		t.Fatal(err)
	}
	successor, err := store.OpenControl(store.OpenOptions{Path: path, Owner: "second"})
	if err != nil {
		t.Fatal(err)
	}
	defer successor.Close()
	if successor.Writer().FencingToken != 2 {
		t.Fatal("writer release did not preserve fencing history")
	}
}

func TestSupervisorStopsListenerWhenWriterLeaseIsLost(t *testing.T) {
	var now atomic.Int64
	now.Store(1000)
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "first", Now: now.Load})
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		_ = control.Close()
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	runtime := serve(ctx, config.Config{Mode: config.ModeShadow}, listener, control, time.Millisecond)
	now.Store(1040)
	if err := awaitStopped(t, runtime); !errors.Is(err, store.ErrWriterFenceHeld) {
		t.Fatalf("lease loss was hidden: %v", err)
	}
	connection, err := net.DialTimeout("tcp", runtime.Addr(), time.Second)
	if connection != nil {
		_ = connection.Close()
	}
	if err == nil {
		t.Fatal("lost writer still accepts connections")
	}
}

func TestSupervisorReportsUnexpectedServeFailure(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	runtime := serve(ctx, config.Config{Mode: config.ModeShadow}, listener, nil, time.Second)
	_ = listener.Close()
	if err := awaitStopped(t, runtime); err == nil {
		t.Fatal("listener failure was discarded")
	}
}

func TestStartRejectsAlreadyCancelledContext(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := Start(ctx, config.Config{Mode: config.ModeShadow, Listen: "127.0.0.1:0"}); !errors.Is(err, context.Canceled) {
		t.Fatalf("cancelled start: %v", err)
	}
}

func TestShutdownClosesAnIncompleteUploadAfterTheDrainDeadline(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	runtime, err := Start(ctx, config.Config{Mode: config.ModeShadow, Listen: "127.0.0.1:0"})
	if err != nil {
		t.Fatal(err)
	}
	connection, err := net.DialTimeout("tcp", runtime.Addr(), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	if err := connection.SetDeadline(time.Now().Add(10 * time.Second)); err != nil {
		t.Fatal(err)
	}
	_, err = io.WriteString(connection, "POST /internal/shadow/evaluate HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 100\r\nExpect: 100-continue\r\n\r\n")
	if err != nil {
		t.Fatal(err)
	}
	// The real server sends 100 only when its handler tries to read the body;
	// this establishes an active request without timing guesses or a fake handler.
	response, err := http.ReadResponse(bufio.NewReader(connection), &http.Request{Method: http.MethodPost})
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusContinue {
		t.Fatalf("handler did not begin reading: %d", response.StatusCode)
	}
	cancel()
	if err := awaitStopped(t, runtime); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("drain deadline not enforced: %v", err)
	}
	var one [1]byte
	if _, err := connection.Read(one[:]); err == nil {
		t.Fatal("incomplete upload connection remains open")
	}
}

func TestRenewalWatchdogClosesListenerWhileSQLiteWriterIsLocked(t *testing.T) {
	control, err := store.OpenControl(store.OpenOptions{Path: t.TempDir(), Owner: "blocked-owner"})
	if err != nil {
		t.Fatal(err)
	}
	blocker, err := sql.Open("sqlite", control.DatabasePath())
	if err != nil {
		_ = control.Close()
		t.Fatal(err)
	}
	defer blocker.Close()
	if _, err := blocker.Exec("BEGIN IMMEDIATE"); err != nil {
		_ = control.Close()
		t.Fatal(err)
	}
	defer blocker.Exec("ROLLBACK")
	underlying, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		_, _ = blocker.Exec("ROLLBACK")
		_ = control.Close()
		t.Fatal(err)
	}
	listener := &closingListener{Listener: underlying, closed: make(chan struct{})}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	runtime := serve(ctx, config.Config{Mode: config.ModeShadow}, listener, control, time.Millisecond)
	// A real Go SQLite connection holds the write transaction. The supervisor
	// must close admission without waiting for that connection to release it.
	select {
	case <-listener.closed:
	case <-time.After(7 * time.Second):
		_, _ = blocker.Exec("ROLLBACK")
		cancel()
		_ = awaitStopped(t, runtime)
		t.Fatal("blocked renewal kept the listener open")
	}
	_, _ = blocker.Exec("ROLLBACK")
	if err := awaitStopped(t, runtime); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("renewal deadline lost: %v", err)
	}
}

type closingListener struct {
	net.Listener
	once   sync.Once
	closed chan struct{}
}

func (listener *closingListener) Close() error {
	err := listener.Listener.Close()
	listener.once.Do(func() { close(listener.closed) })
	return err
}

func awaitStopped(t *testing.T, runtime *Server) error {
	t.Helper()
	select {
	case err := <-runtime.Done():
		return err
	case <-time.After(8 * time.Second):
		t.Fatal("runtime did not stop and release its store")
		return nil
	}
}
