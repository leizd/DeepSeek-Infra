package launchgui

import (
	"bytes"
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/launch"
	"github.com/leizd/DeepSeek-Infra/go/internal/launcherconfig"
)

func installedGUIFixture(t *testing.T) Native {
	t.Helper()
	native := Native{BinDir: t.TempDir(), DataRoot: t.TempDir(), StaticDir: t.TempDir()}
	for _, name := range []string{"deepseekd", "deepseek-worker", "deepseek-gateway", "deepseek-desktop"} {
		if runtime.GOOS == "windows" {
			name += ".exe"
		}
		if err := os.WriteFile(filepath.Join(native.BinDir, name), []byte("unit plan input; never executed"), 0700); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Mkdir(filepath.Join(native.StaticDir, "ui"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(native.StaticDir, "ui", "index.html"), []byte("owned launcher frontend"), 0600); err != nil {
		t.Fatal(err)
	}
	return native
}

func waitForWindowCancellation(ctx context.Context, _ []launch.Process, _ []string, _ io.Writer) error {
	<-ctx.Done()
	return nil
}

func assertSettingsReleased(t *testing.T, native Native) {
	t.Helper()
	saved, err := launcherconfig.Open(filepath.Join(native.DataRoot, "go-control", "launcher"))
	if err != nil {
		t.Fatal("finished launcher retained writer capability", err)
	}
	if err := saved.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestPrivateWindowReadinessAndCloseUseOnlyTheLauncherSession(t *testing.T) {
	native := installedGUIFixture(t)
	t.Setenv("DEEPSEEK_API_KEY", "sk-do-not-send-to-ui")
	t.Setenv("DEEPSEEKD_INTERNAL_BEARER", "do-not-send-to-ui")
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	err := runGUI(ctx, native, launcherconfig.Config{}, guiRuntime{
		listen: net.Listen, random: bytes.NewReader(bytes.Repeat([]byte{7}, 32)),
		run: func(ctx context.Context, processes []launch.Process, environment []string, output io.Writer) error {
			if len(processes) != 1 || processes[0].Name != "deepseek-desktop" || environment != nil {
				return errors.New("launcher started backend before user action")
			}
			var entry string
			for _, value := range processes[0].Env {
				if strings.Contains(value, "do-not-send-to-ui") {
					return errors.New("UI received control/provider credential")
				}
				if strings.HasPrefix(value, "DEEPSEEK_DESKTOP_URL=") {
					entry = strings.TrimPrefix(value, "DEEPSEEK_DESKTOP_URL=")
				}
			}
			u, err := url.Parse(entry)
			if err != nil || u.Query().Get("token") == "" {
				return errors.New("UI entry lacks its private session")
			}
			token := u.Query().Get("token")
			u.RawQuery = ""
			u.Path = "/launcher/v1/ready"
			requestContext, finishRequests := context.WithTimeout(context.Background(), 2*time.Second)
			defer finishRequests()
			request, _ := http.NewRequestWithContext(requestContext, http.MethodGet, u.String(), nil)
			request.Header.Set("Authorization", "Bearer "+token)
			client := &http.Client{Timeout: time.Second, Transport: &http.Transport{Proxy: nil}}
			defer client.CloseIdleConnections()
			response, err := client.Do(request)
			if err != nil {
				return err
			}
			body, err := io.ReadAll(response.Body)
			response.Body.Close()
			if err != nil || response.StatusCode != 200 || !bytes.Contains(body, []byte(`"scope":"launcher_ui"`)) {
				return errors.New("UI confused private readiness with product authority")
			}
			u.Path = "/launcher/v1/close"
			request, _ = http.NewRequestWithContext(requestContext, http.MethodPost, u.String(), strings.NewReader(`{}`))
			request.Header.Set("Authorization", "Bearer "+token)
			request.Header.Set("Content-Type", "application/json")
			response, err = client.Do(request)
			if err != nil {
				return err
			}
			body, err = io.ReadAll(response.Body)
			response.Body.Close()
			if err != nil || response.StatusCode != 200 || !bytes.Contains(body, []byte(`"status":"stopped"`)) {
				return errors.New("UI closed before its stop response was delivered")
			}
			return waitForWindowCancellation(ctx, processes, environment, output)
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	assertSettingsReleased(t, native)
}

func TestPrivateWindowStartupFailuresReleaseOwnership(t *testing.T) {
	for _, kind := range []string{"listen", "random", "frontend", "binary", "process", "serve", "defaults"} {
		t.Run(kind, func(t *testing.T) {
			native := installedGUIFixture(t)
			settings := launcherconfig.Config{}
			deps := guiRuntime{listen: net.Listen, random: bytes.NewReader(bytes.Repeat([]byte{3}, 32)), run: waitForWindowCancellation}
			switch kind {
			case "listen":
				deps.listen = func(string, string) (net.Listener, error) { return nil, errors.New("unavailable") }
			case "random":
				deps.random = bytes.NewReader(nil)
			case "frontend":
				native.StaticDir = t.TempDir()
			case "binary":
				native.BinDir = t.TempDir()
			case "process":
				deps.run = func(context.Context, []launch.Process, []string, io.Writer) error { return errors.New("window failed") }
			case "serve":
				var listener net.Listener
				deps.listen = func(network, address string) (net.Listener, error) {
					var err error
					listener, err = net.Listen(network, address)
					return listener, err
				}
				deps.run = func(ctx context.Context, p []launch.Process, e []string, w io.Writer) error {
					listener.Close()
					return waitForWindowCancellation(ctx, p, e, w)
				}
			case "defaults":
				settings.Port = -1
			}
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			if err := runGUI(ctx, native, settings, deps); err == nil {
				t.Fatal("failed private window returned success")
			}
			assertSettingsReleased(t, native)
		})
	}
}

func TestRunRejectsCancelledOrUnownedDataRoots(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := Run(ctx, Native{}, launcherconfig.Config{}); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if err := Run(context.Background(), Native{}, launcherconfig.Config{}); err == nil {
		t.Fatal("empty data root selected checkout")
	}
	native := installedGUIFixture(t)
	saved, err := launcherconfig.Open(filepath.Join(native.DataRoot, "go-control", "launcher"))
	if err != nil {
		t.Fatal(err)
	}
	defer saved.Close()
	if err := Run(context.Background(), native, launcherconfig.Config{}); err == nil {
		t.Fatal("second window obtained existing owner")
	}
}

func TestPrivateWindowParentCancellationSettlesOwnedUIProcess(t *testing.T) {
	native := installedGUIFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	err := runGUI(ctx, native, launcherconfig.Config{}, guiRuntime{listen: net.Listen, random: bytes.NewReader(bytes.Repeat([]byte{2}, 32)), run: func(ctx context.Context, p []launch.Process, e []string, w io.Writer) error {
		cancel()
		return waitForWindowCancellation(ctx, p, e, w)
	}})
	if err != nil {
		t.Fatal(err)
	}
	assertSettingsReleased(t, native)
}

func TestNativePlanKeepsAllOptionsAndBoundsAuthenticatedReadiness(t *testing.T) {
	native := installedGUIFixture(t)
	t.Setenv("DEEPSEEKD_INTERNAL_BEARER", "owned-control-fixture")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	logs := []string{}
	output := &redactingWriter{append: func(line string) { logs = append(logs, line) }}
	planned := make(chan launch.Plan, 1)
	runtime, err := native.start(ctx, configFixture(), output, func(ctx context.Context, p []launch.Process, e []string, w io.Writer) error {
		planned <- launch.Plan{Processes: p, Env: e}
		<-ctx.Done()
		return nil
	}, func(ctx context.Context, raw string) error {
		deadline, ok := ctx.Deadline()
		if !ok || time.Until(deadline) > 45*time.Second {
			return errors.New("readiness is unbounded")
		}
		u, err := url.Parse(raw)
		if err != nil || u.Query().Get("token") == "" {
			return errors.New("readiness lost local authentication")
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	plan := <-planned
	if len(plan.Processes) != 3 {
		t.Fatal("incomplete native tree")
	}
	for _, name := range []string{"DEEPSEEK_API_KEY=sk-owned-controller", "TAVILY_API_KEY=tv-owned-controller", "OCR_ENABLED=1"} {
		found := false
		for _, value := range plan.Env {
			found = found || value == name
		}
		if !found {
			t.Fatal("native plan lost retained setting", strings.Split(name, "=")[0])
		}
	}
	if err := <-runtime.Ready; err != nil {
		t.Fatal(err)
	}
	_, _ = output.Write([]byte("owned-control-fixture sk-owned-controller\n"))
	if len(logs) != 1 || logs[0] != "[redacted] [redacted]" {
		t.Fatal("runtime secrets entered launcher logs")
	}
	cancel()
	if err := <-runtime.Done; err != nil {
		t.Fatal(err)
	}
}
