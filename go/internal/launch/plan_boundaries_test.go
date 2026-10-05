package launch

import (
	"context"
	"errors"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
)

// These are native Go child processes holding real loopback listeners. The
// supervisor must release their resources before reporting termination.
func runLaunchHelper() int {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 70
	}
	defer listener.Close()
	pid := strconv.Itoa(os.Getpid())
	if dir := os.Getenv("DEEPSEEK_LAUNCH_READY_DIR"); dir != "" {
		if err := os.WriteFile(filepath.Join(dir, pid), []byte(listener.Addr().String()), 0o600); err != nil {
			return 71
		}
	}
	exitDir := os.Getenv("DEEPSEEK_LAUNCH_EXIT_DIR")
	if exitDir == "" {
		time.Sleep(30 * time.Second)
		return 0
	}
	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		if raw, err := os.ReadFile(filepath.Join(exitDir, pid)); err == nil {
			code, err := strconv.Atoi(string(raw))
			if err != nil {
				return 72
			}
			return code
		}
		time.Sleep(20 * time.Millisecond)
	}
	return 73
}

func makeNativePlanBinaries(t *testing.T, binDir string) {
	t.Helper()
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"deepseekd", "deepseek-worker", "deepseek-gateway"} {
		if runtime.GOOS == "windows" {
			name += ".exe"
		}
		if err := os.WriteFile(filepath.Join(binDir, name), []byte("path selection fixture"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
}

func TestProductionPlanRelativeRootsBecomeAbsolute(t *testing.T) {
	root := t.TempDir()
	makeNativePlanBinaries(t, filepath.Join(root, "bin"))
	t.Chdir(root)
	plan, err := ProductionPlan("bin", "data", "static")
	if err != nil {
		t.Fatal(err)
	}
	for _, process := range plan.Processes {
		if !filepath.IsAbs(process.Path) || filepath.Dir(process.Path) != filepath.Join(root, "bin") {
			t.Fatalf("native process path can move with caller cwd: %s", process.Path)
		}
	}
	for _, expected := range []string{
		"DEEPSEEK_INFRA_ROOT=" + filepath.Join(root, "data"),
		"DEEPSEEK_INFRA_STATIC_DIR=" + filepath.Join(root, "static"),
		"DEEPSEEKD_PRODUCTION_STORE=" + filepath.Join(root, "data", "go-control"),
	} {
		found := false
		for _, value := range plan.Env {
			found = found || value == expected
		}
		if !found {
			t.Fatalf("stable absolute launch root missing: %s", expected)
		}
	}
}

func TestProductionPlanRejectsAbsentRootsBeforeSelectingAnyBinary(t *testing.T) {
	for _, test := range []struct{ data, static, message string }{
		{" ", "static", "data root is required"},
		{"data", " ", "static dir is required"},
	} {
		plan, err := ProductionPlan(t.TempDir(), test.data, test.static)
		if err == nil || err.Error() != test.message || len(plan.Processes) != 0 {
			t.Fatalf("invalid roots accepted: %v", err)
		}
	}
}

func TestProductionPlanPATHSelectionAndMissingWorker(t *testing.T) {
	binDir := t.TempDir()
	makeNativePlanBinaries(t, binDir)
	t.Setenv("PATH", binDir)
	plan, err := ProductionPlan("", t.TempDir(), t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	for _, process := range plan.Processes {
		if !filepath.IsAbs(process.Path) || filepath.Dir(process.Path) != binDir {
			t.Fatalf("PATH selected a foreign binary: %s", process.Path)
		}
	}
	worker := "deepseek-worker"
	if runtime.GOOS == "windows" {
		worker += ".exe"
	}
	if err := os.Remove(filepath.Join(binDir, worker)); err != nil {
		t.Fatal(err)
	}
	plan, err = ProductionPlan("", t.TempDir(), t.TempDir())
	if err == nil || !strings.Contains(err.Error(), "native binary missing: deepseek-worker") || len(plan.Processes) != 0 {
		t.Fatalf("missing worker got a partial plan: %v", err)
	}
}

func readLaunchListeners(t *testing.T, dir string, count int) map[string]string {
	t.Helper()
	deadline := time.Now().Add(8 * time.Second)
	for time.Now().Before(deadline) {
		entries, _ := os.ReadDir(dir)
		ready := make(map[string]string)
		for _, entry := range entries {
			raw, err := os.ReadFile(filepath.Join(dir, entry.Name()))
			if err == nil {
				if _, _, err := net.SplitHostPort(string(raw)); err == nil {
					ready[entry.Name()] = string(raw)
				}
			}
		}
		if len(ready) == count {
			return ready
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("native helpers did not bind every listener")
	return nil
}

func TestRunChildExitStopsAllSiblingListeners(t *testing.T) {
	for _, code := range []int{0, 7} {
		t.Run(strconv.Itoa(code), func(t *testing.T) {
			readyDir, exitDir := t.TempDir(), t.TempDir()
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan error, 1)
			completed := false
			defer func() {
				cancel()
				if !completed {
					select {
					case <-done:
					case <-time.After(8 * time.Second):
						t.Error("native helper cleanup did not settle")
					}
				}
			}()
			processes := []Process{{Name: "control", Path: os.Args[0]}, {Name: "worker", Path: os.Args[0]}, {Name: "gateway", Path: os.Args[0]}}
			env := append(os.Environ(), "DEEPSEEK_LAUNCH_HELPER=1", "DEEPSEEK_LAUNCH_READY_DIR="+readyDir, "DEEPSEEK_LAUNCH_EXIT_DIR="+exitDir)
			go func() { done <- Run(ctx, processes, env, nil) }()
			listeners := readLaunchListeners(t, readyDir, len(processes))
			for pid, address := range listeners {
				connection, err := net.DialTimeout("tcp", address, time.Second)
				if err != nil {
					t.Fatalf("native child %s was not live: %v", pid, err)
				}
				connection.Close()
			}
			for pid := range listeners {
				if err := os.WriteFile(filepath.Join(exitDir, pid), []byte(strconv.Itoa(code)), 0o600); err != nil {
					t.Fatal(err)
				}
				break // One exit must cancel the other two, which remain blocked.
			}
			select {
			case err := <-done:
				completed = true
				if code == 0 && (err == nil || err.Error() != "production process exited") {
					t.Fatalf("successful unexpected exit: %v", err)
				}
				if code != 0 {
					var exitError *exec.ExitError
					if !errors.As(err, &exitError) || exitError.ExitCode() != code {
						t.Fatalf("native failure exit code lost: %v", err)
					}
				}
			case <-time.After(8 * time.Second):
				t.Fatal("supervisor did not settle after child exit")
			}
			for pid, address := range listeners {
				connection, err := net.DialTimeout("tcp", address, time.Second)
				if err == nil {
					connection.Close()
					t.Fatalf("child %s still holds its listener after supervisor returned", pid)
				}
			}
		})
	}
}

func TestRunStartFailureAndEmptyPlanAreErrors(t *testing.T) {
	if err := Run(context.Background(), nil, nil, io.Discard); err == nil || err.Error() != "no production processes" {
		t.Fatalf("empty plan: %v", err)
	}
	processes := []Process{{Name: "control", Path: os.Args[0]}, {Name: "missing-worker", Path: filepath.Join(t.TempDir(), "missing-worker")}}
	err := Run(context.Background(), processes, append(os.Environ(), "DEEPSEEK_LAUNCH_HELPER=1"), io.Discard)
	if err == nil || !strings.Contains(err.Error(), "start missing-worker:") {
		t.Fatalf("failed native start was hidden: %v", err)
	}
}
