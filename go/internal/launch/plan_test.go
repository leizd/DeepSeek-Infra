package launch

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestMain(m *testing.M) {
	if os.Getenv("DEEPSEEK_LAUNCH_HELPER") == "1" {
		os.Exit(runLaunchHelper())
	}
	os.Exit(m.Run())
}

func TestProductionPlanRefusesMissingBinaries(t *testing.T) {
	_, err := ProductionPlan(t.TempDir(), t.TempDir(), t.TempDir())
	if err == nil || !strings.Contains(err.Error(), "native binary missing: deepseekd") {
		t.Fatalf("missing binary error = %v", err)
	}
}

func TestProductionPlanUsesAuthoritativeNativeBinaries(t *testing.T) {
	binDir := t.TempDir()
	dataRoot := t.TempDir()
	staticDir := t.TempDir()
	for _, name := range []string{"deepseekd", "deepseek-worker", "deepseek-gateway"} {
		file := name
		if runtime.GOOS == "windows" {
			file += ".exe"
		}
		if err := os.WriteFile(filepath.Join(binDir, file), []byte("native"), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", strings.Join([]string{
		filepath.Join(t.TempDir(), "Python311"),
		filepath.Join(t.TempDir(), "nodejs"),
		filepath.Join(t.TempDir(), "system"),
	}, string(os.PathListSeparator)))
	t.Setenv("AUTH_TOKEN", "launch-token")
	t.Setenv("DEEPSEEK_API_KEY", "sk-launch")

	plan, err := ProductionPlan(binDir, dataRoot, staticDir)
	if err != nil {
		t.Fatal(err)
	}
	if len(plan.Processes) != 3 {
		t.Fatalf("processes = %#v", plan.Processes)
	}
	got := []string{plan.Processes[0].Name, plan.Processes[1].Name, plan.Processes[2].Name}
	want := []string{"deepseekd", "deepseek-worker", "deepseek-gateway"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order = %v", got)
		}
		if legacyCommand(plan.Processes[i].Path) {
			t.Fatalf("legacy binary %s", plan.Processes[i].Path)
		}
	}
	env := strings.Join(plan.Env, "\n")
	for _, needle := range []string{
		"DEEPSEEK_RUNTIME_MODE=python_disabled",
		"DEEPSEEKD_MODE=authoritative",
		"DEEPSEEKD_SHADOW_STORE=",
		"DEEPSEEKD_PRODUCTION_STORE=" + filepath.Join(dataRoot, "go-control"),
		"GO_CONTROL_ADDR=http://127.0.0.1:8090",
		"GATEWAY_BIND_ADDR=127.0.0.1:8000",
		"AUTH_TOKEN=launch-token",
		"DEEPSEEK_API_KEY=sk-launch",
	} {
		if !strings.Contains(env, needle) {
			t.Fatalf("env missing %s\n%s", needle, env)
		}
	}
	if strings.Contains(strings.ToLower(env), "python311") || strings.Contains(strings.ToLower(env), "nodejs") {
		t.Fatalf("interpreter path leaked into env\n%s", env)
	}
	if plan.GatewayURL != "http://127.0.0.1:8000/" {
		t.Fatalf("gateway url %s", plan.GatewayURL)
	}
}

func TestRunStopsChildrenWhenCancelled(t *testing.T) {
	helper := os.Args[0]
	processes := []Process{
		{Name: "deepseekd", Path: helper},
		{Name: "deepseek-worker", Path: helper},
		{Name: "deepseek-gateway", Path: helper},
	}
	readyDir := t.TempDir()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	env := []string{
		"DEEPSEEK_LAUNCH_HELPER=1",
		"DEEPSEEK_LAUNCH_READY_DIR=" + readyDir,
		"SYSTEMROOT=" + os.Getenv("SYSTEMROOT"),
		"WINDIR=" + os.Getenv("WINDIR"),
		"PATH=" + os.Getenv("PATH"),
	}
	go func() {
		done <- Run(ctx, processes, env, io.Discard)
	}()
	deadline := time.Now().Add(8 * time.Second)
	for {
		entries, err := os.ReadDir(readyDir)
		if err == nil && len(entries) >= len(processes) {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("helpers did not report ready: %v", err)
		}
		time.Sleep(20 * time.Millisecond)
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(8 * time.Second):
		t.Fatal("supervisor did not return after cancel")
	}
}

func TestRunRefusesPythonAndNode(t *testing.T) {
	err := Run(context.Background(), []Process{{Name: "python", Path: filepath.Join(t.TempDir(), "python.exe")}}, nil, io.Discard)
	if err == nil || !strings.Contains(err.Error(), "refusing legacy runtime") {
		t.Fatalf("err = %v", err)
	}
}
