package lifecycle

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"testing"
	"time"

	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
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
	public, err := client.Get("http://" + addr + "/api/control/status")
	if err != nil {
		t.Fatal(err)
	}
	defer public.Body.Close()
	publicBody, err := io.ReadAll(public.Body)
	if err != nil {
		t.Fatal(err)
	}
	var publicStatus Status
	if err := json.Unmarshal(publicBody, &publicStatus); err != nil {
		t.Fatal(err)
	}
	if public.StatusCode != http.StatusOK || publicStatus != status {
		t.Fatalf("public status %d %+v vs %+v", public.StatusCode, publicStatus, status)
	}
	post, err := http.NewRequest(http.MethodPost, "http://"+addr+"/api/control/status", nil)
	if err != nil {
		t.Fatal(err)
	}
	denied, err := client.Do(post)
	if err != nil {
		t.Fatal(err)
	}
	defer denied.Body.Close()
	if denied.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("post %d", denied.StatusCode)
	}
	configResp, err := client.Get("http://" + addr + "/api/config")
	if err != nil {
		t.Fatal(err)
	}
	defer configResp.Body.Close()
	configBody, err := io.ReadAll(configResp.Body)
	if err != nil {
		t.Fatal(err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(configBody, &cfg); err != nil {
		t.Fatal(err)
	}
	if configResp.StatusCode != http.StatusOK || cfg["owner"] != "go" {
		t.Fatalf("config %d %+v", configResp.StatusCode, cfg)
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

func TestIdleListenerKeepsItsDurableWriterLease(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer func() {
		cancel()
		time.Sleep(200 * time.Millisecond)
	}()
	path := t.TempDir()
	addr, err := Listen(ctx, config.Config{Mode: config.ModeShadow, Listen: "127.0.0.1:0", Owner: "idle-owner", ShadowStoreDir: path})
	if err != nil {
		t.Fatal(err)
	}
	// No control mutations or HTTP requests during a whole initial lease. This
	// tests the actual default listener and durable database, not a fake timer.
	time.Sleep(31 * time.Second)
	client := &http.Client{Timeout: 2 * time.Second}
	response, err := client.Get("http://" + addr + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	successor, err := store.OpenControl(store.OpenOptions{Path: path, Owner: "unexpected-successor"})
	if successor != nil {
		_ = successor.Close()
	}
	if !errors.Is(err, store.ErrWriterFenceHeld) {
		t.Fatalf("idle listener remained healthy but lost its writer lease: %v", err)
	}
}
