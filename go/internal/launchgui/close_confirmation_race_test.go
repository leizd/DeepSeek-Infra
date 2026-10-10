package launchgui

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
)

// The request deadline is a barrier before the close operation. A real start
// can win while an earlier stopped-status observation is no longer current.
type closeDeadlineGate struct {
	context.Context
	entered, proceed chan struct{}
	once             sync.Once
}

func (g *closeDeadlineGate) Deadline() (time.Time, bool) {
	g.once.Do(func() {
		close(g.entered)
		<-g.proceed
	})
	return time.Time{}, false
}

func TestUnconfirmedHTTPExitCannotStopAConcurrentSuccessfulStart(t *testing.T) {
	ready := make(chan error, 1)
	ready <- nil
	cancelled := make(chan struct{})
	controller := testController(t, func(ctx context.Context, _ launcherconfig.Config, _ io.Writer) (Runtime, error) {
		done := make(chan error, 1)
		go func() {
			<-ctx.Done()
			close(cancelled)
			done <- nil
		}()
		return Runtime{Done: done, Ready: ready, URL: "http://127.0.0.1:8123"}, nil
	})
	static := t.TempDir()
	if err := os.WriteFile(filepath.Join(static, "index.html"), []byte("owned launcher"), 0600); err != nil {
		t.Fatal(err)
	}
	handler, err := Handler(controller, HTTPOptions{Origin: "http://127.0.0.1:18123", Token: "owned-private-launcher-session-with-32-bytes", StaticDir: static})
	if err != nil {
		t.Fatal(err)
	}
	gate := &closeDeadlineGate{Context: context.Background(), entered: make(chan struct{}), proceed: make(chan struct{})}
	var resume sync.Once
	defer resume.Do(func() { close(gate.proceed) })
	r := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:18123/launcher/v1/close", strings.NewReader(`{"confirm":false}`)).WithContext(gate)
	r.Header.Set("Authorization", "Bearer owned-private-launcher-session-with-32-bytes")
	r.Header.Set("Content-Type", "application/json")
	w := httptest.NewRecorder()
	finished := make(chan struct{})
	go func() { handler.ServeHTTP(w, r); close(finished) }()
	select {
	case <-gate.entered:
	case <-time.After(2 * time.Second):
		t.Fatal("close operation did not reach the request deadline")
	}
	if err := controller.Start(configFixture(), false); err != nil {
		t.Fatal(err)
	}
	waitState(t, controller, "running")
	resume.Do(func() { close(gate.proceed) })
	select {
	case <-finished:
	case <-time.After(2 * time.Second):
		t.Fatal("close request did not settle")
	}
	if w.Code != http.StatusConflict || controller.State().Status != "running" {
		t.Fatal("unconfirmed exit stopped a newly started backend", w.Code, controller.State().Status)
	}
	select {
	case <-cancelled:
		t.Fatal("unconfirmed exit cancelled the owned backend")
	default:
	}
}
