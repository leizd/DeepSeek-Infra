package launchgui

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
)

func configFixture() launcherconfig.Config {
	return launcherconfig.Config{Host: "127.0.0.1", Port: 8123, DeepSeekAPIKey: "sk-owned-controller", TavilyAPIKey: "tv-owned-controller", OCREnabled: true}
}

func testController(t *testing.T, start StartFunc) *Controller {
	t.Helper()
	saved, err := launcherconfig.Open(filepath.Join(t.TempDir(), "launcher"))
	if err != nil {
		t.Fatal(err)
	}
	controller, err := New(saved, configFixture(), start)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		controller.Stop(ctx)
		saved.Close()
	})
	return controller
}

func waitState(t *testing.T, c *Controller, want string) State {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		state := c.State()
		if state.Status == want {
			return state
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("launcher did not reach state", want, c.State().Status)
	return State{}
}

func TestStartReadinessStopAndRestartRetainEveryRunOption(t *testing.T) {
	ready := make(chan error, 1)
	started := make(chan launcherconfig.Config, 2)
	controller := testController(t, func(ctx context.Context, c launcherconfig.Config, w io.Writer) (Runtime, error) {
		started <- c
		done := make(chan error, 1)
		go func() { <-ctx.Done(); done <- nil }()
		return Runtime{Done: done, Ready: ready, URL: "http://127.0.0.1:8123/?token=owned-private-token", Secrets: []string{"owned-private-token"}}, nil
	})
	if err := controller.Start(configFixture(), false); err != nil {
		t.Fatal(err)
	}
	if controller.State().Status != "starting" || controller.State().ComputerURL != "" {
		t.Fatal("unready backend was advertised as running")
	}
	if got := <-started; got != configFixture() {
		t.Fatal("configuration fields were lost")
	}
	if err := controller.Start(configFixture(), false); !errors.Is(err, ErrAlreadyRunning) {
		t.Fatal("duplicate run was admitted", err)
	}
	ready <- nil
	state := waitState(t, controller, "running")
	if !strings.Contains(state.ComputerURL, "owned-private-token") {
		t.Fatal("copyable URL lost local authentication")
	}
	if err := controller.Stop(context.Background()); err != nil {
		t.Fatal(err)
	}
	if controller.State().ComputerURL != "" || controller.State().Status != "stopped" {
		t.Fatal("stopped server was still advertised")
	}
	ready <- nil
	if err := controller.Start(configFixture(), false); err != nil {
		t.Fatal(err)
	}
	<-started
	waitState(t, controller, "running")
}

func TestCancelledStartupWaitsForOwnedBackendBeforeAnotherStart(t *testing.T) {
	release := make(chan struct{})
	controller := testController(t, func(ctx context.Context, c launcherconfig.Config, w io.Writer) (Runtime, error) {
		done := make(chan error, 1)
		go func() { <-ctx.Done(); <-release; done <- nil }()
		return Runtime{Done: done, Ready: make(chan error, 1)}, nil
	})
	if err := controller.Start(configFixture(), false); err != nil {
		t.Fatal(err)
	}
	bounded, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if err := controller.Stop(bounded); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("unfinished shutdown was reported as settled", err)
	}
	if controller.State().Status != "stopping" {
		t.Fatal("cancelled startup became idle before process cleanup")
	}
	if err := controller.Start(configFixture(), false); !errors.Is(err, ErrAlreadyRunning) {
		t.Fatal("another run bypassed unfinished shutdown", err)
	}
	close(release)
	waitState(t, controller, "stopped")
}

func TestBackendFailureAndLogChunksNeverExposeCredentials(t *testing.T) {
	done := make(chan error, 1)
	controller := testController(t, func(ctx context.Context, c launcherconfig.Config, w io.Writer) (Runtime, error) {
		for _, part := range []string{"hello sk-owned-", "controller and tv-owned-", "controller\n"} {
			io.WriteString(w, part)
		}
		return Runtime{Done: done, Ready: make(chan error, 1)}, nil
	})
	if err := controller.Start(configFixture(), false); err != nil {
		t.Fatal(err)
	}
	done <- errors.New("failed sk-owned-controller")
	state := waitState(t, controller, "failed")
	joined := strings.Join(state.Logs, "\n") + state.Error
	if strings.Contains(joined, "sk-owned-controller") || strings.Contains(joined, "tv-owned-controller") || !strings.Contains(joined, "[redacted]") {
		t.Fatal("credentials leaked across output chunks")
	}
}

func TestSaveClearAndMissingKeyConfirmationPreserveRunConfiguration(t *testing.T) {
	controller := testController(t, func(context.Context, launcherconfig.Config, io.Writer) (Runtime, error) {
		return Runtime{}, errors.New("fixture has no backend")
	})
	config := configFixture()
	config.DeepSeekAPIKey = ""
	if err := controller.Start(config, false); !errors.Is(err, ErrMissingKeyConfirmation) {
		t.Fatal("missing key confirmation was bypassed", err)
	}
	if err := controller.Save(config); err != nil {
		t.Fatal(err)
	}
	got, err := controller.Settings()
	if err != nil || got != config {
		t.Fatal("saved settings did not reload", err)
	}
	if err := controller.Clear(); err != nil {
		t.Fatal(err)
	}
	if _, err := controller.store.Load(); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("clear retained saved secrets", err)
	}
	defaults, _ := (launcherconfig.Config{}).Normalized()
	if got, err := controller.Settings(); err != nil || got != defaults {
		t.Fatal("clear did not restore original defaults", err)
	}
}

func TestClosingOrCancelledLauncherCannotStartAnotherOwnedTree(t *testing.T) {
	called := false
	c := testController(t, func(context.Context, launcherconfig.Config, io.Writer) (Runtime, error) {
		called = true
		return Runtime{}, nil
	})
	if err := c.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := c.Start(configFixture(), false); err == nil || called {
		t.Fatal("closed launcher created a new run")
	}
	cancelled, cancel := context.WithCancel(context.Background())
	cancel()
	c.context = cancelled
	c.closing = false
	if err := c.Start(configFixture(), false); err == nil || called {
		t.Fatal("cancelled launcher created a new run")
	}
}
