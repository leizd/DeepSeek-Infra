package launch

import (
	"context"
	"errors"
	"io"
	"net"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestWithDesktopIsolatesWindowAndRetainsBackendPlan(t *testing.T) {
	bin := t.TempDir()
	name := "deepseek-desktop"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	if err := os.WriteFile(filepath.Join(bin, name), []byte("selection fixture"), 0o755); err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	for _, key := range desktopSessionKeys {
		t.Setenv(key, "isolated-ui-"+key)
	}
	t.Setenv("LD_PRELOAD", "forbidden-loader")
	base := Plan{Processes: []Process{{Name: "deepseekd", Path: "control"}}, GatewayURL: "http://127.0.0.1:8123/", Env: []string{
		"AUTH_TOKEN=private + token", "SYSTEMROOT=" + os.Getenv("SYSTEMROOT"), "TEMP=" + t.TempDir(),
		"DEEPSEEK_API_KEY=provider-secret", "DEEPSEEKD_INTERNAL_BEARER=control-secret", "PATH=untrusted",
	}}
	plan, err := WithDesktop(base, bin, root)
	if err != nil {
		t.Fatal(err)
	}
	if len(base.Processes) != 1 || len(plan.Processes) != 2 || plan.Processes[0].Path != "control" || plan.Processes[1].Name != "deepseek-desktop" {
		t.Fatal("backend plan changed or window missing")
	}
	ui := plan.Processes[1]
	if !filepath.IsAbs(ui.Path) {
		t.Fatal("relative window executable")
	}
	var entry, profile string
	for _, v := range ui.Env {
		key, value, _ := strings.Cut(v, "=")
		switch key {
		case "DEEPSEEK_DESKTOP_URL":
			entry = value
		case "DEEPSEEK_DESKTOP_PROFILE":
			profile = value
		case "SYSTEMROOT", "TEMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE":
		default:
			allowed := false
			for _, session := range desktopSessionKeys {
				if key == session && value == "isolated-ui-"+key {
					allowed = true
				}
			}
			if !allowed {
				t.Fatalf("secret or execution configuration reached UI: %s", key)
			}
		}
	}
	u, err := url.Parse(entry)
	if err != nil || u.Query().Get("token") != "private + token" || u.Query().Get("desktop") != "1" || profile != filepath.Join(root, ".desktop-webview") {
		t.Fatal("private entry or persistent profile changed")
	}
	base.GatewayURL = "file:///unsafe"
	if _, err := WithDesktop(base, bin, root); err == nil {
		t.Fatal("invalid UI URL admitted")
	}
	if _, err := WithDesktop(Plan{}, t.TempDir(), root); err == nil {
		t.Fatal("missing window binary admitted")
	}
}

func TestDesktopPlanRetainsOSExpansionPathsWithoutProviderSecrets(t *testing.T) {
	for _, key := range platformRootKeys {
		t.Setenv(key, "isolated-system-root-"+key)
	}
	t.Setenv("DEEPSEEK_API_KEY", "provider-secret")
	bin := t.TempDir()
	makeNativePlanBinaries(t, bin)
	name := "deepseek-desktop"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	if err := os.WriteFile(filepath.Join(bin, name), []byte("selection fixture"), 0o755); err != nil {
		t.Fatal(err)
	}
	plan, err := ProductionPlan(bin, t.TempDir(), t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	plan, err = WithDesktop(plan, bin, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	ui := strings.Join(plan.Processes[len(plan.Processes)-1].Env, "\n") + "\n"
	for _, key := range platformRootKeys {
		if !strings.Contains(ui, key+"=isolated-system-root-"+key+"\n") {
			t.Fatalf("OS expansion path missing: %s", key)
		}
	}
	if strings.Contains(ui, "provider-secret") {
		t.Fatal("provider credential reached platform UI")
	}
}

func TestDesktopCloseAndFailureStopBackendListeners(t *testing.T) {
	for _, code := range []int{0, 9} {
		t.Run(strconv.Itoa(code), func(t *testing.T) {
			ready, uiReady, exit := t.TempDir(), t.TempDir(), t.TempDir()
			env := append(os.Environ(), "DEEPSEEK_LAUNCH_HELPER=1", "DEEPSEEK_LAUNCH_READY_DIR="+ready, "DEEPSEEK_LAUNCH_EXIT_DIR="+exit)
			uiEnv := append(os.Environ(), "DEEPSEEK_LAUNCH_HELPER=1", "DEEPSEEK_LAUNCH_READY_DIR="+uiReady, "DEEPSEEK_LAUNCH_EXIT_DIR="+exit)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan error, 1)
			go func() {
				done <- Run(ctx, []Process{{Name: "deepseekd", Path: os.Args[0]}, {Name: "deepseek-desktop", Path: os.Args[0], Env: uiEnv}}, env, io.Discard)
			}()
			listeners := readLaunchListeners(t, ready, 1)
			uiListeners := readLaunchListeners(t, uiReady, 1)
			for pid, address := range uiListeners {
				listeners[pid] = address
				if err := os.WriteFile(filepath.Join(exit, pid), []byte(strconv.Itoa(code)), 0o600); err != nil {
					t.Fatal(err)
				}
			}
			select {
			case err := <-done:
				if code == 0 && err != nil {
					t.Fatal(err)
				}
				if code != 0 {
					var failure *exec.ExitError
					if !errors.As(err, &failure) || failure.ExitCode() != code {
						t.Fatalf("UI failure hidden: %v", err)
					}
				}
			case <-time.After(8 * time.Second):
				t.Fatal("window exit left supervisor running")
			}
			for pid, address := range listeners {
				connection, err := net.DialTimeout("tcp", address, time.Second)
				if err == nil {
					connection.Close()
					t.Fatalf("process %s retained its listener", pid)
				}
			}
		})
	}
}
