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
	if !status.OK || status.Mode != config.ModeShadow || status.MutationAuthority != config.MutationAuthority || status.ProductionMutation {
		t.Fatalf("status %+v", status)
	}
}
