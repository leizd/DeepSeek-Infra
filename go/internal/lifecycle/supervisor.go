package lifecycle

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

// Server is a running control listener. Done delivers one terminal error (nil on
// normal cancellation) after the HTTP server and its owned store have closed.
type Server struct {
	address string
	done    chan error
}

func (runtime *Server) Addr() string       { return runtime.address }
func (runtime *Server) Done() <-chan error { return runtime.done }

func (runtime *Server) supervise(ctx context.Context, cancel context.CancelFunc, server *http.Server, served <-chan error, control *store.Control, renewEvery time.Duration) {
	var ticks <-chan time.Time
	if control != nil {
		ticker := time.NewTicker(renewEvery)
		defer ticker.Stop()
		ticks = ticker.C
	}
	var renewal <-chan error
	var renewalDeadline <-chan struct{}
	cancelRenewal := func() {}
	var terminal error
	serveFinished := false
monitor:
	for {
		select {
		case <-ctx.Done():
			break monitor
		case err := <-served:
			serveFinished = true
			terminal = fmt.Errorf("control listener stopped: %w", err)
			break monitor
		case <-ticks:
			if renewal != nil {
				continue
			}
			renewCtx, stop := context.WithTimeout(ctx, 5*time.Second)
			cancelRenewal = stop
			results := make(chan error, 1)
			renewal, renewalDeadline = results, renewCtx.Done()
			go func() { results <- control.RenewWriter(renewCtx) }()
		case err := <-renewal:
			cancelRenewal()
			renewal, renewalDeadline = nil, nil
			if err != nil {
				if ctx.Err() == nil {
					terminal = fmt.Errorf("control writer renewal: %w", err)
				}
				break monitor
			}
		case <-renewalDeadline:
			if ctx.Err() == nil {
				terminal = fmt.Errorf("control writer renewal: %w", context.DeadlineExceeded)
			}
			break monitor
		}
	}
	// Close admission even if a renewal is waiting for the store mutex or SQLite.
	// Waiting for that call before closing the listener could keep health green
	// past lease expiry. Cancellation never creates a replacement writer claim.
	cancel()
	cancelRenewal()
	shutdownCtx, stop := context.WithTimeout(context.Background(), 5*time.Second)
	if err := server.Shutdown(shutdownCtx); err != nil {
		terminal = errors.Join(terminal, err, server.Close())
	}
	stop()
	if !serveFinished {
		<-served
	}
	if renewal != nil {
		<-renewal
	}
	if control != nil {
		terminal = errors.Join(terminal, control.Close())
	}
	runtime.done <- terminal
	close(runtime.done)
}
