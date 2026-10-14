package desktop

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"sync"
	"testing"
	"time"
)

type lifecycleWindow struct {
	closed    chan struct{}
	once      sync.Once
	entry     string
	navigated chan struct{}
	failure   error
}

func (w *lifecycleWindow) Navigate(entry string) { w.entry = entry; close(w.navigated) }
func (w *lifecycleWindow) Dispatch(f func())     { f() }
func (w *lifecycleWindow) Destroy()              { w.once.Do(func() { close(w.closed) }) }
func (w *lifecycleWindow) Run() error            { <-w.closed; return w.failure }

func TestWindowCancellationPreservesPrivateEntryAndExistingProfile(t *testing.T) {
	profile := t.TempDir()
	keep := filepath.Join(profile, "existing-history")
	if err := os.WriteFile(keep, []byte("retained"), 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	view := &lifecycleWindow{closed: make(chan struct{}), navigated: make(chan struct{})}
	done := make(chan error, 1)
	go func() {
		done <- show(ctx, "http://127.0.0.1:8000/?desktop=1&token=private", profile, func(got string) (window, error) {
			if got != profile {
				t.Error("profile changed")
			}
			return view, nil
		})
	}()
	select {
	case <-view.navigated:
	case <-time.After(2 * time.Second):
		t.Fatal("window not navigated")
	}
	if view.entry != "http://127.0.0.1:8000/?desktop=1&token=private" {
		t.Fatal("private entry changed")
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("window did not close on cancellation")
	}
	if raw, err := os.ReadFile(keep); err != nil || string(raw) != "retained" {
		t.Fatal("existing profile changed")
	}
}

func TestWindowStartupFailuresDoNotNavigateOrRemoveProfile(t *testing.T) {
	called := false
	create := func(string) (window, error) { called = true; return nil, errors.New("runtime unavailable") }
	for _, test := range []struct{ entry, profile string }{{"file:///app", t.TempDir()}, {"http://127.0.0.1:8000/", "relative"}} {
		if err := show(context.Background(), test.entry, test.profile, create); err == nil || called {
			t.Fatal("invalid window startup reached platform")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := show(ctx, "http://127.0.0.1:8000/", t.TempDir(), create); !errors.Is(err, context.Canceled) || called {
		t.Fatal("cancelled startup reached platform")
	}
	file := filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(file, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := show(context.Background(), "http://127.0.0.1:8000/", file, create); err == nil || called {
		t.Fatal("file profile reached platform")
	}
	profile := t.TempDir()
	if err := show(context.Background(), "http://127.0.0.1:8000/", profile, create); err == nil || !called {
		t.Fatal("missing runtime did not fail")
	}
}

func TestWindowNormalCloseAndLinkedProfileBoundary(t *testing.T) {
	profile := t.TempDir()
	view := &lifecycleWindow{closed: make(chan struct{}), navigated: make(chan struct{})}
	view.Destroy() // A normal platform close must return without waiting for cancellation.
	if err := show(context.Background(), "http://127.0.0.1:8000/", profile, func(string) (window, error) {
		return view, nil
	}); err != nil {
		t.Fatal(err)
	}
	linked := filepath.Join(t.TempDir(), "linked-profile")
	if err := os.Symlink(profile, linked); err != nil {
		t.Skip("directory symlinks unavailable to this test account")
	}
	called := false
	if err := show(context.Background(), "http://127.0.0.1:8000/", linked, func(string) (window, error) {
		called = true
		return view, nil
	}); err == nil || called {
		t.Fatal("linked profile reached the platform host")
	}
}

func TestWindowLoopFailurePropagatesAndPreservesProfile(t *testing.T) {
	want := errors.New("platform loop failed")
	view := &lifecycleWindow{closed: make(chan struct{}), navigated: make(chan struct{}), failure: want}
	view.Destroy()
	profile := t.TempDir()
	if err := show(context.Background(), "http://127.0.0.1:8000/", profile, func(string) (window, error) { return view, nil }); !errors.Is(err, want) {
		t.Fatal("platform failure was hidden", err)
	}
	if info, err := os.Stat(profile); err != nil || !info.IsDir() {
		t.Fatal("failed loop removed the profile")
	}
}

func TestUnsupportedPlatformReportsFailureWithoutLosingProfile(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows is exercised by the real SDK integration gate")
	}
	profile := t.TempDir()
	keep := filepath.Join(profile, "retained")
	if err := os.WriteFile(keep, []byte("unchanged"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := Show(context.Background(), "http://127.0.0.1:8000/", profile); err == nil {
		t.Fatal("unimplemented platform was reported as successful")
	}
	if body, err := os.ReadFile(keep); err != nil || string(body) != "unchanged" {
		t.Fatal("failed startup modified the profile")
	}
}
