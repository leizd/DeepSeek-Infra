package lifecycle

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
)

func TestHealthzIsShadowAndReadOnly(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	addr, err := Listen(ctx, config.Config{Mode: config.ModeShadow, Listen: "127.0.0.1:0"})
	if err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get("http://" + addr + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	var status Status
	if err := json.Unmarshal(body, &status); err != nil {
		t.Fatal(err)
	}
	if !status.OK || status.Mode != config.ModeShadow || status.MutationAuthority != config.MutationAuthority || status.ProductionMutation || status.ShadowStore {
		t.Fatalf("status %+v", status)
	}
}

func TestListenOpensIsolatedShadowStore(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer func() {
		cancel()
		time.Sleep(200 * time.Millisecond)
	}()
	addr, err := Listen(ctx, config.Config{Mode: config.ModeShadow, Listen: "127.0.0.1:0", Owner: "owner-a", ShadowStoreDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	client := &http.Client{Timeout: 2 * time.Second}
	resp, err := client.Get("http://" + addr + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	var status Status
	if err := json.Unmarshal(body, &status); err != nil {
		t.Fatal(err)
	}
	if !status.ShadowStore || status.ProductionMutation {
		t.Fatalf("status %+v", status)
	}
}

func TestListenRejectsBadAddress(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if _, err := Listen(ctx, config.Config{Mode: config.ModeShadow, Listen: "127.0.0.1:99999"}); err == nil {
		t.Fatal("bad listen")
	}
}

func TestListenRejectsPythonShadowStore(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if _, err := Listen(ctx, config.Config{Mode: config.ModeShadow, Listen: "127.0.0.1:0", Owner: "owner-a", ShadowStoreDir: ".backup-control/x"}); err == nil {
		t.Fatal("python shadow store must fail")
	}
}
