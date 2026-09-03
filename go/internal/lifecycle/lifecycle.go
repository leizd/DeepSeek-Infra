package lifecycle

import (
	"context"
	"encoding/json"
	"net"
	"net/http"

	"github.com/leizd/DeepSeek-Infra/go/internal/api"
	"github.com/leizd/DeepSeek-Infra/go/internal/config"
	"github.com/leizd/DeepSeek-Infra/go/internal/store"
)

type Status struct {
	OK                 bool   `json:"ok"`
	Mode               string `json:"mode"`
	MutationAuthority  string `json:"mutationAuthority"`
	ProductionMutation bool   `json:"productionMutation"`
	ShadowStore        bool   `json:"shadowStore"`
}

func StatusFrom(cfg config.Config, shadowStore bool) Status {
	return Status{
		OK:                 true,
		Mode:               cfg.Mode,
		MutationAuthority:  config.MutationAuthority,
		ProductionMutation: false,
		ShadowStore:        shadowStore,
	}
}

func Listen(ctx context.Context, cfg config.Config) (string, error) {
	listener, err := net.Listen("tcp", cfg.Listen)
	if err != nil {
		return "", err
	}
	var control *store.Control
	if cfg.ShadowStoreDir != "" {
		owner := cfg.Owner
		if owner == "" {
			owner = "deepseekd"
		}
		control, err = store.OpenControl(store.OpenOptions{Path: cfg.ShadowStoreDir, Owner: owner})
		if err != nil {
			_ = listener.Close()
			return "", err
		}
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(writer http.ResponseWriter, _ *http.Request) {
		writer.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(writer).Encode(StatusFrom(cfg, control != nil))
	})
	api.Register(mux, control)
	server := &http.Server{Handler: mux}
	go func() {
		<-ctx.Done()
		_ = server.Shutdown(context.Background())
		if control != nil {
			_ = control.Close()
		}
	}()
	go func() {
		_ = server.Serve(listener)
	}()
	return listener.Addr().String(), nil
}
