package launch

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
)

func managedFixtureControlEnvironment(t *testing.T, environment []string) []string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	_ = listener.Close()
	return append(environment, "DEEPSEEKD_LISTEN="+address, "GO_CONTROL_ADDR=http://"+address)
}

func TestManagedControlReadinessRequiresTheExpectedLiveWriter(t *testing.T) {
	var calls atomic.Int64
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/healthz" || r.Header.Get("Authorization") != "" {
			t.Error("readiness changed method, path or attached a credential")
		}
		n := calls.Add(1)
		w.Header().Set(config.ControlWriterFenceHeader, "2")
		w.Header().Set(config.ControlWriterOwnerHeader, "current-owner")
		if n == 1 {
			w.Header().Set(config.ControlWriterFenceHeader, "1")
		}
		if n == 2 {
			w.Header().Set(config.ControlWriterOwnerHeader, "stale-owner")
		}
		if n == 3 {
			w.WriteHeader(http.StatusServiceUnavailable)
		}
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if err := waitManagedControl(ctx, server.URL, 2, "current-owner"); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 4 {
		t.Fatal("stale writer or unavailable control admitted")
	}
}
func TestManagedControlReadinessRefusesForeignTargetsAndCancellation(t *testing.T) {
	for _, target := range []string{"", "https://127.0.0.1:8090", "http://example.com:80", "http://0.0.0.0:8090", "http://user@127.0.0.1:8090", "http://127.0.0.1:8090/other", "http://127.0.0.1:8090/?q=1", "http://127.0.0.1:8090/#x", "http://127.0.0.1:0"} {
		if err := waitManagedControl(context.Background(), target, 1, "owner"); err == nil {
			t.Fatalf("foreign target admitted: %s", target)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := waitManagedControl(ctx, "http://127.0.0.1:1", 1, "owner"); err == nil {
		t.Fatal("cancelled control became ready")
	}
	if err := waitManagedControl(nil, "http://127.0.0.1:1", 1, "owner"); err == nil {
		t.Fatal("missing context admitted")
	}
}
func TestManagedGatewayWaitsForActualControlReadiness(t *testing.T) {
	root := t.TempDir()
	processes, environment := managedFailurePlan(t, root)
	environment = append(environment, "DEEPSEEK_MANAGED_FIXTURE_DELAY_CONTROL=1")
	for i := range processes {
		processes[i].Env = append(processes[i].Env, "DEEPSEEK_MANAGED_FIXTURE_DELAY_CONTROL=1")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	done := make(chan error, 1)
	go func() { done <- Run(ctx, processes, environment, io.Discard) }()
	defer func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("managed subprocess cleanup timed out")
		}
	}()
	waitFile := func(name string) {
		t.Helper()
		deadline := time.NewTimer(5 * time.Second)
		defer deadline.Stop()
		ticker := time.NewTicker(20 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-deadline.C:
				t.Fatalf("missing readiness observation %s", name)
			case err := <-done:
				t.Fatalf("managed launch stopped before %s: %v", name, err)
			case <-ticker.C:
				if _, err := os.Stat(filepath.Join(root, name)); err == nil {
					return
				}
			}
		}
	}
	waitFile("fixture-readiness-probed")
	if _, err := os.Stat(filepath.Join(root, "fixture-gateway-ready")); !os.IsNotExist(err) {
		t.Fatal("gateway started before the actual Go endpoint was ready")
	}
	if err := os.WriteFile(filepath.Join(root, "fixture-release-control"), []byte("release"), 0o600); err != nil {
		t.Fatal(err)
	}
	waitFile("fixture-gateway-ready")
}
