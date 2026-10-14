package launchgui

import (
	"context"
	"errors"
	"io"
	"strings"
	"testing"

	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
)

func TestReadinessFailureCancelsAndSettlesTheOwnedRun(t *testing.T) {
	ready := make(chan error, 1)
	cancelled := make(chan struct{})
	c := testController(t, func(ctx context.Context, config launcherconfig.Config, output io.Writer) (Runtime, error) {
		done := make(chan error, 1)
		go func() { <-ctx.Done(); close(cancelled); done <- errors.New("cancelled fixture") }()
		return Runtime{Done: done, Ready: ready}, nil
	})
	if err := c.Start(configFixture(), false); err != nil {
		t.Fatal(err)
	}
	ready <- errors.New("not ready")
	waitState(t, c, "failed")
	select {
	case <-cancelled:
	default:
		t.Fatal("failed readiness left its backend alive")
	}
	if err := c.Stop(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestSavingOrClearingDuringARunOnlyChangesTheNextConfiguration(t *testing.T) {
	ready := make(chan error, 1)
	ready <- nil
	c := testController(t, func(ctx context.Context, config launcherconfig.Config, output io.Writer) (Runtime, error) {
		done := make(chan error, 1)
		go func() { <-ctx.Done(); done <- nil }()
		return Runtime{Done: done, Ready: ready, URL: "http://127.0.0.1:8123/"}, nil
	})
	if err := c.Start(configFixture(), false); err != nil {
		t.Fatal(err)
	}
	waitState(t, c, "running")
	changed := configFixture()
	changed.Port = 8124
	if err := c.Save(changed); err != nil {
		t.Fatal(err)
	}
	if c.State().ComputerURL != "http://127.0.0.1:8123/" {
		t.Fatal("saving mutated the running backend")
	}
	if err := c.Clear(); err != nil {
		t.Fatal(err)
	}
	if c.State().Status != "running" || c.State().ComputerURL != "http://127.0.0.1:8123/" {
		t.Fatal("clearing interrupted the existing run")
	}
	if err := c.Save(launcherconfig.Config{Port: -1}); err == nil {
		t.Fatal("invalid saved configuration was accepted")
	}
	if err := c.Start(launcherconfig.Config{Port: -1}, true); err == nil {
		t.Fatal("invalid run configuration was accepted")
	}
}

func TestOutputIsBoundedAndFlushesOnlyRedactedFinalFragments(t *testing.T) {
	c := testController(t, func(context.Context, launcherconfig.Config, io.Writer) (Runtime, error) {
		return Runtime{}, errors.New("fixture")
	})
	w := &redactingWriter{append: c.appendLog, secrets: []string{"sk-secret"}}
	for i := 0; i < 450; i++ {
		_, _ = w.Write([]byte("ordinary log\n"))
	}
	_, _ = w.Write([]byte(strings.Repeat("x", 17000)))
	w.Flush()
	_, _ = w.Write([]byte("sk-"))
	_, _ = w.Write([]byte("secret"))
	w.Flush()
	state := c.State()
	if len(state.Logs) != 400 || state.Logs[399] != "[redacted]" || !strings.Contains(state.Logs[398], "已省略") {
		t.Fatal("log bound or final-fragment redaction failed")
	}
	_, _ = w.Write([]byte(strings.Repeat("x", 17000) + "\nrecovered\n"))
	state = c.State()
	if state.Logs[399] != "recovered" {
		t.Fatal("overlong line prevented subsequent logs")
	}
}
