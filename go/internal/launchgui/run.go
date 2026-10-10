package launchgui

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/launch"
	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
)

// Run starts the configuration window before a backend is started. Its Go
// service is private IPv4 loopback; only Rust exposes the product's public API.
func Run(parent context.Context, native Native, initial launcherconfig.Config) error {
	return runGUI(parent, native, initial, guiRuntime{listen: net.Listen, random: rand.Reader, run: launch.Run})
}

// The process and network primitives can be supplied by lifecycle tests. The
// production entry always uses the existing owned-tree supervisor.
type guiRuntime struct {
	listen func(string, string) (net.Listener, error)
	random io.Reader
	run    func(context.Context, []launch.Process, []string, io.Writer) error
}

func runGUI(parent context.Context, native Native, initial launcherconfig.Config, runtime guiRuntime) (result error) {
	if err := parent.Err(); err != nil {
		return err
	}
	if strings.TrimSpace(native.DataRoot) == "" {
		return errors.New("launcher data directory is required")
	}
	root, err := filepath.Abs(native.DataRoot)
	if err != nil {
		return errors.New("invalid launcher data directory")
	}
	saved, err := launcherconfig.Open(filepath.Join(root, "go-control", "launcher"))
	if err != nil {
		return err
	}
	defer func() { result = errors.Join(result, saved.Close()) }()
	controller, err := NewWithContext(parent, saved, initial, native.Start)
	if err != nil {
		return err
	}
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		result = errors.Join(result, controller.Close(ctx))
	}()
	listener, err := runtime.listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return errors.New("cannot listen for the private launcher window")
	}
	defer listener.Close()
	var random [32]byte
	if _, err := io.ReadFull(runtime.random, random[:]); err != nil {
		return errors.New("cannot create private launcher session")
	}
	token := hex.EncodeToString(random[:])
	origin := "http://" + listener.Addr().String()
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	handler, err := Handler(controller, HTTPOptions{Origin: origin, Token: token, StaticDir: filepath.Join(native.StaticDir, "ui"), OpenBrowser: native.OpenBrowser, PrepareConfig: AvailableConfig, OnClose: cancel})
	if err != nil {
		return err
	}
	server := &http.Server{Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 35 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16384, BaseContext: func(net.Listener) context.Context { return ctx }}
	served := make(chan error, 1)
	go func() { served <- server.Serve(listener) }()
	defer server.Close()
	plan, err := launch.WithLauncherDesktop(launch.Plan{GatewayURL: origin + "/launcher", Env: append(os.Environ(), "AUTH_TOKEN="+token)}, native.BinDir, root)
	if err != nil {
		return err
	}
	finished := make(chan error, 1)
	output := &redactingWriter{append: controller.appendLog, secrets: []string{token}}
	defer output.Flush()
	go func() { finished <- runtime.run(ctx, plan.Processes, nil, output) }()
	select {
	case err := <-served:
		cancel()
		<-finished
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			return errors.New("private launcher service stopped")
		}
		return nil
	case err := <-finished:
		cancel()
		return err
	case <-ctx.Done():
		cancel()
		return <-finished
	}
}
